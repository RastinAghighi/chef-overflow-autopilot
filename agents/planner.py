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

CHOPPABLE = {"tomato", "lettuce", "onion"}
COOKABLE = {"meat", "dough"}            # meat always cooked; dough cooked only for pizza


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

    def __init__(self):
        self.reset()

    def reset(self):
        self.chef_order = {}      # chef_id -> order_id it owns (builds end-to-end)
        self.chef_area = {}       # chef_id -> plating area index an owner assembles on
        self.chef_fetch = {}      # chef_id -> ingredient it is currently walking to fetch
        self.chef_target = {}     # chef_id -> station id it is currently commanded to
        self.prev_pos = {}        # chef_id -> last tile (jam detection)
        self.prev_time = 0.0
        self.stuck_time = {}      # chef_id -> seconds wedged in place

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

    def _cmd(self, api, chef_id, target_id):
        """Issue a command and report whether it took, recording the chef's live
        target on success.  The kitchen is congested, so ``command`` often returns
        ``No path found`` when another chef blocks the route; committing on a failed
        command would create an infinite retry loop, so every caller checks this."""
        r = api.command(chef_id, target_id)
        if bool(r and r.get("success")):
            self.chef_target[chef_id] = target_id
            return True
        return False

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

        def is_idle(c):
            return ((not c.get("busy")) and (not c.get("hasPath"))
                    and float(c.get("stall", 0.0) or 0.0) <= 0.0)

        # --- reconcile ownership ---------------------------------------------
        live_ids = {c["id"] for c in chefs}
        for cid in list(self.chef_order):
            if cid not in live_ids or self.chef_order[cid] not in order_by_id:
                # order delivered or expired (or chef gone): release the chef
                self.chef_order.pop(cid, None)
                self.chef_area.pop(cid, None)
                self.chef_fetch.pop(cid, None)
        for cid in list(self.chef_target):
            if cid not in live_ids:
                self.chef_target.pop(cid, None)
        for c in chefs:
            if c.get("holding") is not None:
                self.chef_fetch.pop(c["id"], None)   # has the item now (or carrying a plate)
        owned_areas = {self.chef_area[cid] for cid in self.chef_order if cid in self.chef_area}
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
                        if self._cmd(api, cid, target["standId"]):
                            claimed.add(target["standId"])
                    elif target is None:
                        self._cmd(api, cid, trash_id)

        # --- jam-breaker: an idle/blocked *empty, unowned* chef sitting in a
        #     chokepoint stalls everyone behind it.  Shove it to the nearer edge
        #     (far-left staging if on the left/centre, else a reception stand).
        now = float(state.get("time", 0.0))
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
                    and c.get("holding") is None and cid not in self.chef_order):
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
        idle.sort(key=lambda c: 0 if c["id"] in self.chef_order else 1)

        for c in idle:
            cid = c["id"]
            if cid in self.chef_order:
                self._drive_owner(api, c, orders, order_by_id, order_c, contents_c, areas,
                                  stoves, boards, stand_by_id, bins_by_ing, trash_id,
                                  fetching, claimed)
            else:
                self._drive_free(api, c, orders, order_c, contents_c, areas,
                                 stands, stand_by_id, trash_id,
                                 owned_areas, owned_orders, n_areas, claimed)

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
                     fetching, claimed):
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
                if self._cmd(api, cid, sid):
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
        missing = req - have
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
                    owned_areas, owned_orders, n_areas, claimed):
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
                if self._cmd(api, cid, target["standId"]):
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
