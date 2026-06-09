"""
Greedy heuristic planner for Chef Overflow (Phase 2, sim side).

This is the non-RL baseline of the design spec (§7) plus the Phase 1 Findings
(§11).  It is a *pure controller*: :meth:`Planner.decide` takes a state dict
shaped **exactly** like the game's ``KitchenAPI.getState()`` and an ``api`` object
exposing ``command(chef_id, station_id)`` and ``boost(chef_id)``, and issues
commands.  It reads only fields the real ``getState()`` exposes, so the very same
logic is reproduced verbatim in ``agents/planner.js`` against the live game.

Design (see docs/RL_DESIGN.md §7, §11):

* **One chef owns one order, end to end.**  A free chef claims the most-urgent
  feasible order, takes a dedicated plating area, then *alone* fetches → chops/cooks
  → deposits every component, lifts the finished plate and delivers it.  Because no
  two chefs ever build the same plate, the components on an area are always exactly
  its order's recipe (exact-match safety, Finding 3) **and** the lethal "two chefs
  fighting over one plating-area approach tile" deadlock cannot occur.  The kitchen
  is brutally congested (1-wide dead-end bin pockets, a 2-tile divider gap), so
  minimising path-sharing is what keeps it deadlock-free.

We cannot author paths — the game pathfinds and walks each chef, freezing a blocked
chef (~0.5 s to repath, ~5.4 s to abandon) — so collision-avoidance comes entirely
from smarter *target* choice.  Four coordination rules do that:

* **Station reservation.**  Every stove, cutting board, plating area and reception
  stand a chef is *currently walking to or working at* is marked claimed
  (``chef_target``); a new assignment of that station type always picks one that is
  both free and unclaimed, spreading chefs across distinct stations.  A claim is
  released the moment the chef arrives/finishes/drops the task (goes idle).  This
  stops the case where two chefs target the same stove and the second freezes
  behind the first instead of taking a third, free stove.

* **Deadline-aware delivery + claim.**  A finished plate is delivered to the
  most-urgent (least ``timeLeft``) order whose recipe matches it *exactly* and that
  is still physically reachable — preferring an un-claimed stand — not to the chef's
  own (possibly fresher) order.  New orders are likewise claimed most-urgent-first.
  This stops a ready steak going to a fresh steak order while an expiring one
  strikes out.  Exact-match is still enforced on every delivery, so a *wrong*
  delivery remains structurally impossible.

* **Idle-chef placement.**  A chef with nothing to do (no order, nothing to carry,
  nothing to deliver, no orphan to clear) is parked on a peripheral, low-traffic
  anchor instead of sitting in the central crossroads and walling everyone in.

* **Sticky, commitment-safe assignments.**  Only *idle* chefs are commanded, so the
  game's 1.5 s mid-route redirect "STALL" never fires on a healthy plan.  The lone
  exception is invalidation: an en-route plate-carrier whose destination stand no
  longer holds a matching order *would* wrong-deliver on arrival, so it is re-matched
  (or trashed) — paying the stall once is far cheaper than the wasted haul + reset.

* **Bin reservation.**  Each left-column bin is a single-exit dead-end pocket, so a
  bin is reserved for one chef at a time (Finding 4) to prevent the fetch deadlock.

* **Triage.**  An order is only *claimed* if a rough estimate says it can finish
  before it expires; once claimed it is always driven to delivery.

* **Stand flow + boost.**  Boost fires on the long plating→stand haul (Finding 4).
"""

from collections import Counter

# ---------------------------------------------------------------------------
# Tuning constants.  These drive *estimates and biases only* (never correctness:
# exact-match safety is structural).  The JS port uses the same numbers.
# ---------------------------------------------------------------------------
# Rough wall-clock to fetch+process+stage one component (incl. travel), by final
# state.  Used only by the triage feasibility estimate.
COMP_SECS = {"raw": 4.5, "chopped": 6.5, "cooked": 8.5}
DELIVER_HAUL_SECS = 5.0                 # take-plate + carry to stand
FEAS_SLACK = 1.5                        # optimism: attempt anything within this × est
SERIAL_FACTOR = 0.8                     # one chef builds components serially, not parallel
BOOST_MIN_DIST = 6                      # Manhattan tiles to bother boosting a haul
BIN_COLUMN_X = 3                        # a chef at x<=this is still in/near a bin pocket
STUCK_SECONDS = 1.2                     # idle/blocked chef wedged this long -> edge it out
EDGE_SPLIT_X = 9                        # shove wedged chefs left of this left, else right
MOVE_DELAY = 0.18                       # [sim/game] seconds per tile; for delivery-reach est
MAX_BUILDAHEAD_PLATES = 2               # never let speculative plates consume >2 areas
MIN_ACTIVE_FREE_AREAS = 2               # keep active-order assembly room while building ahead
STAND_PRESSURE_OCCUPIED = 4             # visible orders + inferred eaters
INFERRED_EATER_SECONDS = 10.0           # customer stand occupancy after a delivery

CHOPPABLE = {"tomato", "lettuce", "onion"}
COOKABLE = {"meat", "dough"}            # meat always cooked; dough cooked only for pizza

RECIPE_COMPONENTS = {
    "Salad": [("lettuce", "chopped"), ("tomato", "chopped")],
    "Burger": [("meat", "cooked"), ("dough", "raw")],
    "Steak": [("meat", "cooked")],
    "Pizza": [("dough", "cooked"), ("cheese", "raw"), ("tomato", "chopped")],
    "Deluxe Burger": [("meat", "cooked"), ("dough", "raw"), ("onion", "chopped")],
    "Feast Platter": [
        ("meat", "cooked"), ("lettuce", "chopped"),
        ("tomato", "chopped"), ("cheese", "raw"),
    ],
    "Supreme Pizza": [
        ("dough", "cooked"), ("tomato", "chopped"),
        ("onion", "chopped"), ("cheese", "raw"),
    ],
}


# ---------------------------------------------------------------------------
# Small pure helpers (operate on getState-shaped dicts; mirror-able in JS)
# ---------------------------------------------------------------------------
def _pos(o):
    p = o["pos"]
    return (p[0], p[1])


def _manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _is_plate(h):
    return bool(h) and isinstance(h, dict) and h.get("type") == "plate"


def _comp_key(item):
    return (item.get("ingredient"), item.get("state"))


def _components_counter(components):
    return Counter((c["ingredient"], c["state"]) for c in components)


def _recipe_counter(dish):
    return Counter(RECIPE_COMPONENTS.get(dish, []))


def _items_counter(items):
    return Counter(_comp_key(it) for it in (items or []))


def _cnt_eq(a, b):
    """Multiset equality with non-positive entries ignored."""
    return not (a - b) and not (b - a)


def _processed_form(ing):
    """The cooked/chopped component this raw ingredient becomes, or None if it is
    only ever used raw (cheese; dough's raw use is handled by the recipe lookup)."""
    if ing in CHOPPABLE:
        return (ing, "chopped")
    if ing == "meat":
        return (ing, "cooked")
    if ing == "dough":
        return (ing, "cooked")          # raw-dough recipes are matched before this
    return None


def _nearest(items, pos):
    best, bestd = None, None
    for it in items:
        d = _manhattan(_pos(it), pos)
        if bestd is None or d < bestd:
            best, bestd = it, d
    return best


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
class Planner:
    """Greedy one-chef-per-order controller.  Persistent state is the
    chef→(order, area) ownership, which ingredient each chef is fetching (bin
    reservation), which station each chef is currently committed to (station
    reservation), plus a jam-detection clock."""

    def __init__(self, features=None, *, build_ahead=False, stand_pressure=False,
                 parallel_assembly=False):
        features = set(features or ())
        self.enable_build_ahead = bool(build_ahead or "build_ahead" in features or "f1" in features)
        self.enable_stand_pressure = bool(stand_pressure or "stand_pressure" in features or "f2" in features)
        self.enable_parallel_assembly = bool(parallel_assembly or "parallel_assembly" in features or "f3" in features)
        self.reset()

    def reset(self):
        self.chef_order = {}      # chef_id -> order_id it owns (builds end-to-end)
        self.chef_area = {}       # chef_id -> plating area index an owner assembles on
        self.chef_buildahead = {}  # chef_id -> pending upcoming slot plan
        self.chef_helper = {}      # helper chef_id -> one-component assist plan
        self.order_helper = {}     # order_id -> helper chef_id
        self.chef_fetch = {}      # chef_id -> ingredient it is currently walking to fetch
        self.chef_target = {}     # chef_id -> station id it is currently commanded to
        self.prev_pos = {}        # chef_id -> last tile (jam detection)
        self.prev_time = 0.0
        self.stuck_time = {}      # chef_id -> seconds wedged in place
        self.inferred_eaters = {}  # stand_id -> occupied-until sim time
        self.prev_pressure_orders = {}
        self.prev_pressure_score = 0.0
        self.prev_pressure_failed = 0
        self.stats = Counter()

    def feature_config(self):
        return {
            "build_ahead": self.enable_build_ahead,
            "stand_pressure": self.enable_stand_pressure,
            "parallel_assembly": self.enable_parallel_assembly,
        }

    def get_metrics(self):
        return {
            "features": self.feature_config(),
            "buildahead_triggers": int(self.stats.get("buildahead_triggers", 0)),
            "buildahead_completions": int(self.stats.get("buildahead_completions", 0)),
            "helper_assignments": int(self.stats.get("helper_assignments", 0)),
        }

    # -- feasibility (triage estimate only) ---------------------------------
    @staticmethod
    def _est_seconds(comps):
        if not comps:
            return 0.0
        times = sorted((COMP_SECS.get(st, 5.0) for (_ing, st) in comps), reverse=True)
        return DELIVER_HAUL_SECS + times[0] + SERIAL_FACTOR * sum(times[1:])

    def _feasible(self, order, comps):
        return self._est_seconds(comps) <= max(float(order["timeLeft"]), 0.1) * FEAS_SLACK

    @staticmethod
    def _reachable(order, cpos, stand):
        """True iff the order survives at least the straight-line travel time to its
        stand (Manhattan × per-tile delay) — a physical lower bound.  Orders that
        fail this are doomed regardless, so we never burn a haul (or risk a
        stand-turnover wrong-delivery) chasing them."""
        return float(order["timeLeft"]) >= _manhattan(cpos, _pos(stand)) * MOVE_DELAY

    def _mark_delivery_command(self, stand_id, now):
        if self.enable_stand_pressure and stand_id is not None:
            self.inferred_eaters[stand_id] = max(
                self.inferred_eaters.get(stand_id, 0.0),
                float(now) + INFERRED_EATER_SECONDS,
            )

    def _cmd(self, api, chef_id, target_id, *, delivery_stand=None, now=None):
        """Issue a command and report whether it took, recording the chef's live
        target on success.  The kitchen is congested, so ``command`` often returns
        ``No path found`` when another chef blocks the route; committing on a failed
        command would create an infinite retry loop, so every caller checks this."""
        r = api.command(chef_id, target_id)
        if bool(r and r.get("success")):
            self.chef_target[chef_id] = target_id
            if delivery_stand is not None:
                self._mark_delivery_command(delivery_stand, 0.0 if now is None else now)
            return True
        return False

    def _update_stand_pressure(self, state, orders):
        if not self.enable_stand_pressure:
            return 0

        now = float(state.get("time", 0.0) or 0.0)
        for sid, until in list(self.inferred_eaters.items()):
            if float(until) <= now:
                self.inferred_eaters.pop(sid, None)

        current = {o["id"]: dict(o) for o in orders}
        score = float(state.get("score", 0.0) or 0.0)
        score_increased = score > self.prev_pressure_score
        if score_increased:
            for oid, order in self.prev_pressure_orders.items():
                if oid not in current:
                    stand_id = order.get("standId")
                    if stand_id:
                        self.inferred_eaters[stand_id] = max(
                            self.inferred_eaters.get(stand_id, 0.0),
                            now + INFERRED_EATER_SECONDS,
                        )

        visible = {o.get("standId") for o in orders if o.get("standId")}
        inferred = {sid for sid, until in self.inferred_eaters.items() if float(until) > now}
        occupied = visible | inferred
        self.prev_pressure_orders = current
        self.prev_pressure_score = score
        self.prev_pressure_failed = int(state.get("failedOrders", 0) or 0)
        return len(occupied)

    def _best_delivery_order(self, plate, cpos, orders, order_c, stand_by_id, claimed):
        """Pick the order to deliver ``plate`` to: the most-urgent (least timeLeft)
        order whose recipe matches the plate EXACTLY and is still reachable in time,
        preferring a stand no other chef has claimed.  Returns the order or None
        (None ⇒ nothing wants this plate, or every match is already doomed — trash
        it)."""
        matches = [o for o in orders
                   if stand_by_id.get(o["standId"]) is not None
                   and _cnt_eq(order_c[o["id"]], plate)]
        reach = [o for o in matches
                 if self._reachable(o, cpos, stand_by_id[o["standId"]])]
        if not reach:
            return None
        unclaimed = [o for o in reach if o["standId"] not in claimed]
        pool = unclaimed if unclaimed else reach        # race a claimed stand only as last resort
        return min(pool, key=lambda o: float(o["timeLeft"]))

    def _release_helper(self, cid):
        plan = self.chef_helper.pop(cid, None)
        if plan is not None:
            self.order_helper.pop(plan.get("order_id"), None)
        self.chef_fetch.pop(cid, None)

    def _missing_for_area(self, req, have, area, ignore_helper=None):
        missing = req - have
        if self.enable_parallel_assembly:
            for hcid, plan in self.chef_helper.items():
                if hcid == ignore_helper or plan.get("area") != area:
                    continue
                comp = tuple(plan.get("component", ()))
                if missing[comp] > 0:
                    missing[comp] -= 1
        return Counter({k: v for k, v in missing.items() if v > 0})

    def _chef_pipeline_component(self, chef, req, have):
        """Best-effort component a chef is already producing.  Used only to avoid
        assigning a helper to duplicate the owner's current pipeline."""
        cid = chef["id"]
        h = chef.get("holding")
        if h is not None and not _is_plate(h):
            comp = _comp_key(h)
            if req[comp] > have[comp]:
                return comp
            proc = _processed_form(comp[0]) if comp[1] == "raw" else None
            if proc is not None and req[proc] > have[proc]:
                return proc
        ing = self.chef_fetch.get(cid)
        if ing:
            for state_name in ("raw", "cooked", "chopped"):
                comp = (ing, state_name)
                if req[comp] > have[comp]:
                    return comp
        return None

    @staticmethod
    def _component_pipeline_sort(comp):
        state_rank = {"cooked": 0, "chopped": 1, "raw": 2}
        return (state_rank.get(comp[1], 3), comp[0])

    def _component_startable(self, comp, req, have, bins_by_ing, stoves, boards, fetching, claimed):
        ing, state_name = comp
        if ing in fetching or ing not in bins_by_ing:
            return False
        if state_name == "cooked":
            return any(s.get("cooking") is None and s["id"] not in claimed for s in stoves)
        if state_name == "chopped":
            return any(not b.get("busy") and b["id"] not in claimed for b in boards)
        return True

    def _start_component(self, api, c, comp, bins_by_ing, fetching):
        cid = c["id"]
        ing = comp[0]
        if self._cmd(api, cid, bins_by_ing[ing]["id"]):
            self.chef_fetch[cid] = ing
            fetching.add(ing)
            return True
        return False

    def _try_pressure_lift(self, api, c, orders, order_c, contents_c, areas,
                           stand_by_id, owned_areas, claimed):
        if not self.enable_stand_pressure:
            return False
        if c.get("holding") is not None:
            return False
        cpos = _pos(c)
        candidates = []
        for i, area in enumerate(areas):
            if i in owned_areas or area["id"] in claimed:
                continue
            have = contents_c[i]
            if not have:
                continue
            target = self._best_delivery_order(have, cpos, orders, order_c, stand_by_id, claimed)
            if target is None:
                continue
            candidates.append((float(target["timeLeft"]), _manhattan(cpos, _pos(area)), i))
        if not candidates:
            return False
        _time_left, _dist, area_idx = min(candidates)
        if self._cmd(api, c["id"], areas[area_idx]["id"]):
            claimed.add(areas[area_idx]["id"])
            return True
        return False

    def _reconcile_buildaheads(self, live_ids, orders, upcoming, contents_c):
        if not self.enable_build_ahead:
            return
        active_owned = set(self.chef_order.values())
        claimed_slots = set()
        for cid in list(self.chef_buildahead):
            plan = self.chef_buildahead.get(cid)
            if cid not in live_ids:
                self.chef_buildahead.pop(cid, None)
                self.chef_area.pop(cid, None)
                continue
            area = self.chef_area.get(cid)
            if area is None or area >= len(contents_c):
                self.chef_buildahead.pop(cid, None)
                self.chef_area.pop(cid, None)
                continue
            dish = plan.get("dish")
            req = _recipe_counter(dish)
            have = contents_c[area]
            if have and (have - req):
                self.chef_buildahead.pop(cid, None)
                self.chef_area.pop(cid, None)
                continue

            matches = [o for o in orders if o.get("dish") == dish]
            unowned = [o for o in matches if o["id"] not in active_owned]
            if unowned:
                order = min(unowned, key=lambda o: float(o["timeLeft"]))
                self.chef_order[cid] = order["id"]
                active_owned.add(order["id"])
                self.chef_buildahead.pop(cid, None)
                continue

            if matches:
                plan["pending"] = False
                continue

            slot_cands = [
                (idx, spec) for idx, spec in enumerate(upcoming)
                if spec.get("dish") == dish and idx not in claimed_slots
            ]
            if not slot_cands:
                self.chef_buildahead.pop(cid, None)
                self.chef_area.pop(cid, None)
                continue
            old_slot = int(plan.get("slot", 999))
            idx, spec = min(slot_cands, key=lambda pair: (abs(pair[0] - old_slot), pair[0]))
            plan["slot"] = idx
            plan["eta"] = float(spec.get("etaSeconds", 999.0) or 999.0)
            plan["pending"] = True
            claimed_slots.add(idx)

    def _try_start_buildahead(self, c, upcoming, contents_c, areas, owned_areas,
                              empty_free_areas, active_candidates):
        if not self.enable_build_ahead or active_candidates:
            return False
        if len(self.chef_buildahead) >= MAX_BUILDAHEAD_PLATES:
            return False
        if len(empty_free_areas) <= MIN_ACTIVE_FREE_AREAS:
            return False
        claimed_slots = {
            int(plan.get("slot"))
            for plan in self.chef_buildahead.values()
            if plan.get("pending", True)
        }
        slot_cands = []
        for idx, spec in enumerate(upcoming):
            dish = spec.get("dish")
            if idx in claimed_slots or dish not in RECIPE_COMPONENTS:
                continue
            slot_cands.append((float(spec.get("etaSeconds", 999.0) or 999.0), idx, spec))
        if not slot_cands:
            return False
        _eta, idx, spec = min(slot_cands)
        area = min(
            empty_free_areas,
            key=lambda i: (
                0 if _pos(areas[i])[0] >= 10 else 1,
                abs(_pos(areas[i])[0] - 10),
                abs(_pos(areas[i])[1] - 6),
                areas[i]["id"],
            ),
        )
        cid = c["id"]
        self.chef_buildahead[cid] = {
            "dish": spec["dish"],
            "slot": idx,
            "eta": float(spec.get("etaSeconds", 999.0) or 999.0),
            "pending": True,
            "completed": False,
        }
        self.chef_area[cid] = area
        owned_areas.add(area)
        self.stats["buildahead_triggers"] += 1
        return True

    def _drive_buildahead(self, api, c, orders, order_c, contents_c, areas,
                          stoves, boards, stand_by_id, bins_by_ing, trash_id,
                          fetching, claimed, now):
        cid = c["id"]
        plan = self.chef_buildahead.get(cid)
        a = self.chef_area.get(cid)
        if plan is None or a is None:
            return
        req = _recipe_counter(plan.get("dish"))
        have = contents_c[a]
        h = c.get("holding")

        if _is_plate(h):
            plate = Counter(_comp_key(it) for it in h.get("items", []))
            target = self._best_delivery_order(plate, _pos(c), orders, order_c,
                                               stand_by_id, claimed)
            if target is not None:
                sid = target["standId"]
                stand = stand_by_id[sid]
                if self._cmd(api, cid, sid, delivery_stand=sid, now=now):
                    claimed.add(sid)
                    if (_manhattan(_pos(c), _pos(stand)) >= BOOST_MIN_DIST
                            and not c.get("boostActive")
                            and float(c.get("boostCooldown", 0.0) or 0.0) <= 0.0):
                        api.boost(cid)
            elif plan.get("pending", True):
                if sum(contents_c[a].values()) == 0:
                    self._cmd(api, cid, areas[a]["id"])
            else:
                self._cmd(api, cid, trash_id)
            return

        if h is not None:
            self._advance_held(api, c, a, req, have, areas, stoves, boards, claimed, trash_id)
            return

        missing = req - have
        if not missing:
            if not plan.get("completed"):
                plan["completed"] = True
                self.stats["buildahead_completions"] += 1
            target = self._best_delivery_order(have, _pos(c), orders, order_c, stand_by_id, claimed)
            if target is not None:
                self._cmd(api, cid, areas[a]["id"])
            return

        order_pref = sorted(missing, key=lambda comp: (0 if comp[1] == "raw" else 1))
        for comp in order_pref:
            ing = comp[0]
            if ing in fetching or ing not in bins_by_ing:
                continue
            needs_cook = comp[1] == "cooked"
            needs_chop = comp[1] == "chopped"
            if needs_cook and not any(s.get("cooking") is None and s["id"] not in claimed for s in stoves):
                continue
            if needs_chop and not any(not b.get("busy") and b["id"] not in claimed for b in boards):
                continue
            if self._cmd(api, cid, bins_by_ing[ing]["id"]):
                self.chef_fetch[cid] = ing
                fetching.add(ing)
            return

    def _try_assign_helper(self, c, chefs_by_id, orders, order_by_id, order_c,
                           contents_c, areas, bins_by_ing, stoves, boards,
                           fetching, claimed):
        if not self.enable_parallel_assembly or c.get("holding") is not None:
            return False
        best = None
        for owner_id, oid in self.chef_order.items():
            if oid in self.order_helper or oid not in order_by_id:
                continue
            owner = chefs_by_id.get(owner_id)
            area = self.chef_area.get(owner_id)
            if owner is None or area is None:
                continue
            req = order_c[oid]
            if sum(req.values()) < 2:
                continue
            have = contents_c[area]
            raw_missing = req - have
            if sum(v for v in raw_missing.values() if v > 0) < 2:
                continue
            missing = Counter({k: v for k, v in raw_missing.items() if v > 0})
            owner_comp = self._chef_pipeline_component(owner, req, have)
            if owner_comp is not None and missing[owner_comp] > 0:
                missing[owner_comp] -= 1
            missing = Counter({k: v for k, v in missing.items() if v > 0})
            options = [
                comp for comp in sorted(missing, key=self._component_pipeline_sort)
                if self._component_startable(comp, req, have, bins_by_ing, stoves,
                                             boards, fetching, claimed)
            ]
            if not options:
                continue
            order = order_by_id[oid]
            comp = options[0]
            score = (float(order["timeLeft"]), _manhattan(_pos(c), _pos(areas[area])), oid)
            if best is None or score < best[0]:
                best = (score, owner_id, oid, area, comp)
        if best is None:
            return False
        _score, owner_id, oid, area, comp = best
        cid = c["id"]
        self.chef_helper[cid] = {
            "owner": owner_id,
            "order_id": oid,
            "area": area,
            "component": comp,
            "stage": "assigned",
        }
        self.order_helper[oid] = cid
        self.stats["helper_assignments"] += 1
        return True

    def _drive_helper(self, api, c, orders, order_by_id, order_c, contents_c,
                      areas, stoves, boards, bins_by_ing, trash_id, fetching,
                      claimed):
        cid = c["id"]
        plan = self.chef_helper.get(cid)
        if plan is None:
            return
        oid = plan.get("order_id")
        area = plan.get("area")
        comp = tuple(plan.get("component", ()))
        if oid not in order_by_id or area is None:
            self._release_helper(cid)
            return
        req = order_c[oid]
        have = contents_c[area]
        h = c.get("holding")
        current_missing = req - have

        if h is None:
            if plan.get("stage") == "deposit":
                self._release_helper(cid)
                return
            if current_missing[comp] <= 0:
                self._release_helper(cid)
                return
            if self._component_startable(comp, req, have, bins_by_ing, stoves, boards,
                                         fetching, claimed):
                if self._cmd(api, cid, bins_by_ing[comp[0]]["id"]):
                    self.chef_fetch[cid] = comp[0]
                    fetching.add(comp[0])
                    plan["stage"] = "fetch"
            return

        if _is_plate(h):
            self._release_helper(cid)
            return

        held = _comp_key(h)
        if held == comp:
            if current_missing[comp] > 0:
                if self._cmd(api, cid, areas[area]["id"]):
                    plan["stage"] = "deposit"
            else:
                if self._cmd(api, cid, trash_id):
                    self._release_helper(cid)
            return

        if held[1] == "raw" and held[0] == comp[0] and current_missing[comp] > 0:
            if comp[1] == "cooked":
                free = [s for s in stoves if s.get("cooking") is None and s["id"] not in claimed]
                if free:
                    s = _nearest(free, _pos(c))
                    if self._cmd(api, cid, s["id"]):
                        claimed.add(s["id"])
                        plan["stage"] = "process"
                return
            if comp[1] == "chopped":
                free = [b for b in boards if not b.get("busy") and b["id"] not in claimed]
                if free:
                    b = _nearest(free, _pos(c))
                    if self._cmd(api, cid, b["id"]):
                        claimed.add(b["id"])
                        plan["stage"] = "process"
                return

        if self._cmd(api, cid, trash_id):
            self._release_helper(cid)

    # -- main decision ------------------------------------------------------
    def decide(self, state, api):
        if not state.get("running") or state.get("paused") or state.get("gameOver"):
            return

        chefs = state["chefs"]
        st = state["stations"]
        areas = st["platingAreas"]
        stoves = st["stoves"]
        boards = st["cuttingBoards"]
        stands = st["receptionStands"]
        bins = st["ingredientBins"]
        trash_id = st["trashCans"][0]["id"]
        n_areas = len(areas)

        bins_by_ing = {b["ingredient"]: b for b in bins}
        stand_by_id = {s["id"]: s for s in stands}
        orders = list(state.get("orders", []))
        order_by_id = {o["id"]: o for o in orders}
        order_c = {o["id"]: _components_counter(o["components"]) for o in orders}
        contents_c = [_items_counter(a.get("items")) for a in areas]
        upcoming = list(state.get("upcomingOrders", []) or [])
        now = float(state.get("time", 0.0) or 0.0)
        pressure_occupied = self._update_stand_pressure(state, orders)

        def is_idle(c):
            return ((not c.get("busy")) and (not c.get("hasPath"))
                    and float(c.get("stall", 0.0) or 0.0) <= 0.0)

        # --- reconcile ownership ---------------------------------------------
        live_ids = {c["id"] for c in chefs}
        chefs_by_id = {c["id"]: c for c in chefs}
        for cid in list(self.chef_order):
            if cid not in live_ids or self.chef_order[cid] not in order_by_id:
                # order delivered or expired (or chef gone): release the chef
                oid = self.chef_order.get(cid)
                self.chef_order.pop(cid, None)
                self.chef_area.pop(cid, None)
                self.chef_fetch.pop(cid, None)
                helper = self.order_helper.pop(oid, None)
                if helper is not None:
                    self.chef_helper.pop(helper, None)
        for cid in list(self.chef_target):
            if cid not in live_ids:
                self.chef_target.pop(cid, None)
        for c in chefs:
            if c.get("holding") is not None:
                self.chef_fetch.pop(c["id"], None)   # has the item now (or carrying a plate)
        self._reconcile_buildaheads(live_ids, orders, upcoming, contents_c)
        if self.enable_parallel_assembly:
            for hcid, plan in list(self.chef_helper.items()):
                owner = plan.get("owner")
                oid = plan.get("order_id")
                helper = chefs_by_id.get(hcid)
                if (helper is None or owner not in self.chef_order
                        or self.chef_order.get(owner) != oid
                        or oid not in order_by_id
                        or self.chef_area.get(owner) != plan.get("area")):
                    self._release_helper(hcid)
                elif is_idle(helper) and helper.get("holding") is None and plan.get("stage") == "deposit":
                    self._release_helper(hcid)
        owned_areas = {self.chef_area[cid] for cid in self.chef_order if cid in self.chef_area}
        owned_areas.update(self.chef_area[cid] for cid in self.chef_buildahead if cid in self.chef_area)
        owned_orders = set(self.chef_order.values())

        # --- station reservation: which stoves/boards/areas/stands a chef is
        #     currently walking to or working at.  Derived from a clean snapshot
        #     BEFORE any command this tick; a chef that has arrived/finished (now
        #     idle) releases its claim, while a chef still en route or busy holds it.
        claimed = set()
        for c in chefs:
            cid = c["id"]
            tgt = self.chef_target.get(cid)
            if tgt is None:
                continue
            if is_idle(c):
                self.chef_target.pop(cid, None)        # arrived / finished / dropped -> release
            else:
                claimed.add(tgt)                       # still committed -> hold the claim

        # --- sticky, but safe on invalidation: the ONLY time we redirect an
        #     en-route chef.  A plate-carrier whose destination stand no longer holds
        #     a matching order (it expired, was delivered by another chef, or the
        #     stand turned over) would wrong-deliver on arrival, so re-match the plate
        #     to the best live order (else trash it).  Paying the 1.5 s commitment
        #     stall once beats wasting the whole haul and breaking the streak.
        for c in chefs:
            cid = c["id"]
            tgt = self.chef_target.get(cid)
            h = c.get("holding")
            if (tgt in stand_by_id and _is_plate(h)
                    and c.get("hasPath") and not c.get("busy")):
                plate = Counter(_comp_key(it) for it in h.get("items", []))
                cur = next((o for o in orders if o["standId"] == tgt), None)
                still_ok = cur is not None and _cnt_eq(order_c[cur["id"]], plate)
                if not still_ok:
                    target = self._best_delivery_order(plate, _pos(c), orders, order_c,
                                                       stand_by_id, claimed)
                    if target is not None and target["standId"] != tgt:
                        if self._cmd(api, cid, target["standId"],
                                     delivery_stand=target["standId"], now=now):
                            claimed.add(target["standId"])
                    elif target is None:
                        self._cmd(api, cid, trash_id)

        # --- jam-breaker: an idle/blocked *empty, unowned* chef sitting in a
        #     chokepoint stalls everyone behind it.  Shove it to the nearer edge
        #     (far-left staging if on the left/centre, else a reception stand).
        dt_seen = max(0.0, now - self.prev_time)
        self.prev_time = now
        far_left_area = min(range(n_areas), key=lambda i: _pos(areas[i])[0])
        escaped = set()
        for c in chefs:
            cid = c["id"]
            p = tuple(c["pos"])
            if c.get("busy") or self.prev_pos.get(cid) != p:
                self.stuck_time[cid] = 0.0
            else:
                self.stuck_time[cid] = self.stuck_time.get(cid, 0.0) + dt_seen
            self.prev_pos[cid] = p
            if (self.stuck_time.get(cid, 0.0) >= STUCK_SECONDS
                    and c.get("holding") is None
                    and cid not in self.chef_order
                    and cid not in self.chef_buildahead
                    and cid not in self.chef_helper):
                if p[0] <= EDGE_SPLIT_X:
                    moved = self._cmd(api, cid, areas[far_left_area]["id"])
                else:
                    nearest_stand = min(stands, key=lambda s: _manhattan(p, _pos(s)))
                    moved = self._cmd(api, cid, nearest_stand["id"])
                if moved:
                    self.stuck_time[cid] = 0.0
                    escaped.add(cid)

        # --- bin reservation set: one chef per dead-end pocket ----------------
        # An ingredient is "in use" while a chef walks to its bin empty, or holds it
        # raw still inside the bin column (not yet clear of the pocket).
        fetching = set(self.chef_fetch.values())
        for c in chefs:
            h = c.get("holding")
            if (h is not None and not _is_plate(h) and h.get("state") == "raw"
                    and _pos(c)[0] <= BIN_COLUMN_X):
                fetching.add(h.get("ingredient"))

        # process idle, non-escaped chefs: owners first (deliver/advance), then free
        idle = [c for c in chefs if is_idle(c) and c["id"] not in escaped]
        def idle_rank(c):
            cid = c["id"]
            if cid in self.chef_order or cid in self.chef_buildahead:
                return 0
            if cid in self.chef_helper:
                return 1
            return 2
        idle.sort(key=idle_rank)

        for c in idle:
            cid = c["id"]
            if (self.enable_stand_pressure and pressure_occupied >= STAND_PRESSURE_OCCUPIED
                    and c.get("holding") is None
                    and self._try_pressure_lift(api, c, orders, order_c, contents_c,
                                                areas, stand_by_id, owned_areas, claimed)):
                continue
            if cid in self.chef_order:
                self._drive_owner(api, c, orders, order_by_id, order_c, contents_c, areas,
                                  stoves, boards, stand_by_id, bins_by_ing, trash_id,
                                  fetching, claimed, now)
            elif cid in self.chef_buildahead:
                self._drive_buildahead(api, c, orders, order_c, contents_c, areas,
                                       stoves, boards, stand_by_id, bins_by_ing, trash_id,
                                       fetching, claimed, now)
            elif cid in self.chef_helper:
                self._drive_helper(api, c, orders, order_by_id, order_c, contents_c,
                                   areas, stoves, boards, bins_by_ing, trash_id,
                                   fetching, claimed)
            else:
                if self._try_assign_helper(c, chefs_by_id, orders, order_by_id, order_c,
                                           contents_c, areas, bins_by_ing, stoves, boards,
                                           fetching, claimed):
                    continue
                self._drive_free(api, c, orders, order_c, contents_c, areas,
                                 stands, stand_by_id, trash_id,
                                 owned_areas, owned_orders, n_areas, claimed, upcoming, now)

    # -- shared: advance one held component toward its area -----------------
    def _advance_held(self, api, c, a, req, have, areas, stoves, boards, claimed, trash_id):
        cid = c["id"]
        comp = _comp_key(c["holding"])
        if req[comp] > have[comp]:
            self._cmd(api, cid, areas[a]["id"])      # recipe wants this exact form -> deposit
            return
        proc = _processed_form(comp[0]) if comp[1] == "raw" else None
        if proc is not None and req[proc] > have[proc]:
            if proc[1] == "cooked":
                free = [s for s in stoves if s.get("cooking") is None and s["id"] not in claimed]
                if free:
                    s = _nearest(free, _pos(c))
                    if self._cmd(api, cid, s["id"]):
                        claimed.add(s["id"])
            else:
                free = [b for b in boards if not b.get("busy") and b["id"] not in claimed]
                if free:
                    b = _nearest(free, _pos(c))
                    if self._cmd(api, cid, b["id"]):
                        claimed.add(b["id"])
            return                                   # else wait for a free station
        self._cmd(api, cid, trash_id)                # holding something the recipe doesn't need

    # -- an owner drives its single order to delivery -----------------------
    def _drive_owner(self, api, c, orders, order_by_id, order_c, contents_c, areas,
                     stoves, boards, stand_by_id, bins_by_ing, trash_id,
                     fetching, claimed, now):
        cid = c["id"]
        oid = self.chef_order[cid]
        a = self.chef_area.get(cid)
        if a is None:
            return
        order = order_by_id[oid]
        req = order_c[oid]
        have = contents_c[a]
        h = c.get("holding")

        # 1) carrying the finished plate -> deliver to the MOST URGENT exact match
        #    (reachable, prefer un-claimed stand), with boost on a long haul.
        if _is_plate(h):
            plate = Counter(_comp_key(it) for it in h.get("items", []))
            target = self._best_delivery_order(plate, _pos(c), orders, order_c,
                                               stand_by_id, claimed)
            if target is not None:
                sid = target["standId"]
                stand = stand_by_id[sid]
                if self._cmd(api, cid, sid, delivery_stand=sid, now=now):
                    claimed.add(sid)
                    if (_manhattan(_pos(c), _pos(stand)) >= BOOST_MIN_DIST
                            and not c.get("boostActive")
                            and float(c.get("boostCooldown", 0.0) or 0.0) <= 0.0):
                        api.boost(cid)
            else:
                self._cmd(api, cid, trash_id)        # nothing live wants it — never wrong-deliver
            return

        # 2) a component in hand -> deposit it, or process it first
        if h is not None:
            self._advance_held(api, c, a, req, have, areas, stoves, boards, claimed, trash_id)
            return

        # 3) empty-handed
        missing = self._missing_for_area(req, have, a) if self.enable_parallel_assembly else req - have
        if not missing:
            self._cmd(api, cid, areas[a]["id"])      # plate complete -> lift it
            return

        # fetch the next component; prefer raw-usable ones (cheapest), then those
        # needing a cook/chop, and only start a component whose station is free AND
        # unclaimed now (so we don't queue behind a chef already heading there).
        order_pref = sorted(missing, key=lambda comp: (0 if comp[1] == "raw" else 1))
        for comp in order_pref:
            ing = comp[0]
            if ing in fetching or ing not in bins_by_ing:
                continue
            needs_cook = (req[(ing, "cooked")] > have[(ing, "cooked")]) and ing in COOKABLE and req[(ing, "raw")] <= have[(ing, "raw")]
            needs_chop = (req[(ing, "chopped")] > have[(ing, "chopped")]) and ing in CHOPPABLE
            if needs_cook and not any(s.get("cooking") is None and s["id"] not in claimed for s in stoves):
                continue
            if needs_chop and not any(not b.get("busy") and b["id"] not in claimed for b in boards):
                continue
            if self._cmd(api, cid, bins_by_ing[ing]["id"]):
                self.chef_fetch[cid] = ing
                fetching.add(ing)
            return
        # nothing startable right now (bins reserved / stations busy) -> wait

    # -- a free chef claims a new order, cleans up, or parks ----------------
    def _drive_free(self, api, c, orders, order_c, contents_c, areas,
                    stands, stand_by_id, trash_id,
                    owned_areas, owned_orders, n_areas, claimed, upcoming, now):
        cid = c["id"]
        h = c.get("holding")

        # carrying something with no order (just escaped / leftover): a plate goes to
        # the most-urgent exact match (reachable, un-claimed stand preferred); a stray
        # component is dumped.  Exact-match keeps a wrong delivery impossible.
        if _is_plate(h):
            plate = Counter(_comp_key(it) for it in h.get("items", []))
            target = self._best_delivery_order(plate, _pos(c), orders, order_c,
                                               stand_by_id, claimed)
            if target is not None:
                if self._cmd(api, cid, target["standId"],
                             delivery_stand=target["standId"], now=now):
                    claimed.add(target["standId"])
            else:
                self._cmd(api, cid, trash_id)
            return
        if h is not None:
            self._cmd(api, cid, trash_id)
            return

        # claim the MOST URGENT feasible un-owned order, with an empty un-owned area
        empty_free_areas = [i for i in range(n_areas)
                            if i not in owned_areas and sum(contents_c[i].values()) == 0]
        cands = [o for o in orders if o["id"] not in owned_orders
                 and self._feasible(o, list(order_c[o["id"]].elements()))]
        if cands and empty_free_areas:
            o = min(cands, key=lambda o: float(o["timeLeft"]))   # urgency-first (avoid expiry)
            stand = stand_by_id.get(o["standId"])
            spos = _pos(stand) if stand else (0, 0)
            a = min(empty_free_areas, key=lambda i: _manhattan(_pos(areas[i]), spos))
            self.chef_order[cid] = o["id"]
            self.chef_area[cid] = a
            owned_orders.add(o["id"])
            owned_areas.add(a)
            return                                    # next tick this chef begins fetching

        if self._try_start_buildahead(c, upcoming, contents_c, areas, owned_areas,
                                      empty_free_areas, cands):
            return

        # nothing to claim: if an un-owned area holds an orphan plate, clear it
        orphan = [i for i in range(n_areas)
                  if i not in owned_areas and sum(contents_c[i].values()) > 0]
        if orphan:
            i = _nearest([areas[k] for k in orphan], _pos(c))
            self._cmd(api, cid, i["id"])              # lift it; delivered/trashed as a free plate
            return

        # genuinely task-less: park on a peripheral, reserved anchor so this chef
        # stops walling the central crossroads (Finding: the idle 5th chef jams it).
        park = self._park_target(c, areas, contents_c, owned_areas, stands, trash_id,
                                 claimed, n_areas)
        if park is not None and self._cmd(api, cid, park):
            claimed.add(park)

    # -- choose a low-traffic anchor for a task-less chef -------------------
    def _park_target(self, c, areas, contents_c, owned_areas, stands, trash_id,
                     claimed, n_areas):
        """A peripheral station to idle beside, away from the central lanes.
        ``command`` only accepts STATION ids (never a bare floor tile), so we anchor
        on the cleanest peripheral stations, each reserved so several idle chefs
        spread out instead of stacking:
          1) the far-left plating area — least-used by the assembler (which prefers
             areas near the right-side stands) and kitchen-side, so re-tasking stays
             cheap — but only if empty & un-owned (no accidental plate pickup);
          2) the trash (far-left corner);
          3) a reception stand with no order (the open reception zone, fully out of
             the kitchen), corner-most to dodge the delivery flow.
        Imperfect by necessity — the chef stops one tile *adjacent* to the anchor,
        not in a true dead corner — but any of these clears the crossroads the idle
        chef would otherwise block."""
        free_areas = [i for i in range(n_areas)
                      if i not in owned_areas and sum(contents_c[i].values()) == 0
                      and areas[i]["id"] not in claimed]
        if free_areas:
            i = min(free_areas, key=lambda i: _pos(areas[i])[0])
            return areas[i]["id"]
        if trash_id not in claimed:
            return trash_id
        free_stands = [s for s in stands if s.get("order") is None and s["id"] not in claimed]
        if free_stands:
            return max(free_stands, key=lambda s: abs(_pos(s)[1] - 6.5))["id"]
        return None
