"""
Greedy heuristic planner for Chef Overflow (Phase 2, sim side).

This is the non-RL baseline of the design spec (§7) plus the Phase 1 Findings
(§11).  It is a *pure controller*: :meth:`Planner.decide` takes a state dict
shaped **exactly** like the game's ``KitchenAPI.getState()`` and an ``api`` object
exposing ``command(chef_id, station_id)`` and ``boost(chef_id)``, and issues
commands.  It reads only fields the real ``getState()`` exposes, so the very same
logic is reproduced verbatim in ``agents/planner.js`` against the live game.

Design (see docs/RL_DESIGN.md §7, §11):

* **One chef owns one order, end to end.**  A free chef claims the highest-value
  feasible order, takes a dedicated plating area, then *alone* fetches → chops/cooks
  → deposits every component, lifts the finished plate and delivers it.  Because no
  two chefs ever build the same plate, the components on an area are always exactly
  its order's recipe (exact-match safety, Finding 3) **and** the lethal "two chefs
  fighting over one plating-area approach tile" deadlock cannot occur.  The kitchen
  is brutally congested (1-wide dead-end bin pockets, a 2-tile divider gap), so
  minimising path-sharing is what keeps it deadlock-free.

* **Value = point-potential × urgency, no VIP.**  ``100·difficulty + 2·timeLeft``
  weighted by ``1/timeLeft``.  VIP is invisible (Finding 1) so it is never
  referenced; fast delivery captures its upside.  Simpler dishes are preferred
  implicitly (same base score, less labour — Finding 6).

* **Station contention.**  An owner only starts a cook/chop component when a stove
  (3) / board (2) is free; otherwise it works a different component or waits.

* **Bin reservation.**  Each left-column bin is a single-exit dead-end pocket, so a
  bin is reserved for one chef at a time (Finding 4) to prevent the fetch deadlock.

* **Triage.**  An order is only *claimed* if a rough estimate says it can finish
  before it expires; once claimed it is always driven to delivery.

* **Stand flow + boost.**  Delivery is an owner's top priority (free the stand, bank
  the streak); boost fires on the long plating→stand haul (Finding 4).

* **Commitment-safe.**  Only ever commands *idle* chefs (no path, not busy, not
  stalled); never redirects a committed chef — so the 1.5 s stall never triggers
  (Finding 5).  A wedged idle/blocked chef is shoved to the nearer map edge so it
  stops blocking a lane.
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
    """Greedy one-chef-per-order controller.  Persistent state is just the
    chef→(order, area) ownership plus which ingredient each chef is fetching (for
    the bin reservation) and a jam-detection clock."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.chef_order = {}      # chef_id -> order_id it owns (builds end-to-end)
        self.chef_area = {}       # chef_id -> plating area index an owner assembles on
        self.chef_fetch = {}      # chef_id -> ingredient it is currently walking to fetch
        self.prev_pos = {}        # chef_id -> last tile (jam detection)
        self.prev_time = 0.0
        self.stuck_time = {}      # chef_id -> seconds wedged in place

    # -- value / feasibility ------------------------------------------------
    @staticmethod
    def _order_value(order, difficulty):
        tl = max(float(order["timeLeft"]), 0.1)
        base = 100.0 * difficulty + 2.0 * tl     # actual delivery points (streak mult is global)
        return base / tl                          # × urgency (1/timeLeft): avoid expiry first

    @staticmethod
    def _est_seconds(comps):
        if not comps:
            return 0.0
        times = sorted((COMP_SECS.get(st, 5.0) for (_ing, st) in comps), reverse=True)
        return DELIVER_HAUL_SECS + times[0] + SERIAL_FACTOR * sum(times[1:])

    def _feasible(self, order, comps):
        return self._est_seconds(comps) <= max(float(order["timeLeft"]), 0.1) * FEAS_SLACK

    @staticmethod
    def _cmd(api, chef_id, target_id):
        """Issue a command and report whether it took.  The kitchen is congested,
        so ``command`` often returns ``No path found`` when another chef blocks the
        route; committing on a failed command would create an infinite retry loop,
        so every caller checks this."""
        r = api.command(chef_id, target_id)
        return bool(r and r.get("success"))

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
        difficulty = float(state.get("difficulty", 1.0))
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
        for c in chefs:
            if c.get("holding") is not None:
                self.chef_fetch.pop(c["id"], None)   # has the item now (or carrying a plate)
        owned_areas = {self.chef_area[cid] for cid in self.chef_order if cid in self.chef_area}
        owned_orders = set(self.chef_order.values())

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

        # within-tick claims so two chefs don't grab the same station/area
        claimed_stoves = set()
        claimed_boards = set()

        # process idle, non-escaped chefs: owners first (deliver/advance), then free
        idle = [c for c in chefs if is_idle(c) and c["id"] not in escaped]
        idle.sort(key=lambda c: 0 if c["id"] in self.chef_order else 1)

        for c in idle:
            cid = c["id"]
            if cid in self.chef_order:
                self._drive_owner(api, c, order_by_id, order_c, contents_c, areas,
                                  stoves, boards, stands, stand_by_id, bins_by_ing,
                                  trash_id, fetching, claimed_stoves, claimed_boards)
            else:
                self._drive_free(api, c, orders, order_by_id, order_c, contents_c, areas,
                                 stands, stand_by_id, difficulty, trash_id,
                                 owned_areas, owned_orders, n_areas)

    # -- shared: advance one held component toward its area -----------------
    def _advance_held(self, api, c, a, req, have, areas, stoves, boards,
                      claimed_stoves, claimed_boards, trash_id):
        cid = c["id"]
        comp = _comp_key(c["holding"])
        if req[comp] > have[comp]:
            self._cmd(api, cid, areas[a]["id"])      # recipe wants this exact form -> deposit
            return
        proc = _processed_form(comp[0]) if comp[1] == "raw" else None
        if proc is not None and req[proc] > have[proc]:
            if proc[1] == "cooked":
                free = [s for s in stoves if s.get("cooking") is None and s["id"] not in claimed_stoves]
                if free:
                    s = _nearest(free, _pos(c))
                    if self._cmd(api, cid, s["id"]):
                        claimed_stoves.add(s["id"])
            else:
                free = [b for b in boards if not b.get("busy") and b["id"] not in claimed_boards]
                if free:
                    b = _nearest(free, _pos(c))
                    if self._cmd(api, cid, b["id"]):
                        claimed_boards.add(b["id"])
            return                                   # else wait for a free station
        self._cmd(api, cid, trash_id)                # holding something the recipe doesn't need

    # -- an owner drives its single order to delivery -----------------------
    def _drive_owner(self, api, c, order_by_id, order_c, contents_c, areas,
                     stoves, boards, stands, stand_by_id, bins_by_ing, trash_id,
                     fetching, claimed_stoves, claimed_boards):
        cid = c["id"]
        oid = self.chef_order[cid]
        a = self.chef_area.get(cid)
        if a is None:
            return
        order = order_by_id[oid]
        req = order_c[oid]
        have = contents_c[a]
        h = c.get("holding")

        # 1) carrying the finished plate -> deliver (with boost on a long haul)
        if _is_plate(h):
            plate = Counter(_comp_key(it) for it in h.get("items", []))
            if _cnt_eq(plate, req) and stand_by_id.get(order["standId"]) is not None:
                stand = stand_by_id[order["standId"]]
                if self._cmd(api, cid, order["standId"]):
                    if (_manhattan(_pos(c), _pos(stand)) >= BOOST_MIN_DIST
                            and not c.get("boostActive") and float(c.get("boostCooldown", 0.0) or 0.0) <= 0.0):
                        api.boost(cid)
            else:
                self._cmd(api, cid, trash_id)        # mismatch (order changed) — never wrong-deliver
            return

        # 2) a component in hand -> deposit it, or process it first
        if h is not None:
            self._advance_held(api, c, a, req, have, areas, stoves, boards,
                               claimed_stoves, claimed_boards, trash_id)
            return

        # 3) empty-handed
        missing = req - have
        if not missing:
            self._cmd(api, cid, areas[a]["id"])      # plate complete -> lift it
            return

        # fetch the next component; prefer raw-usable ones (cheapest), then those
        # needing a cook/chop, and only start a component whose station is free now.
        order_pref = sorted(missing, key=lambda comp: (0 if comp[1] == "raw" else 1))
        for comp in order_pref:
            ing = comp[0]
            if ing in fetching or ing not in bins_by_ing:
                continue
            # respect station capacity for the processing this component will need
            needs_cook = (req[(ing, "cooked")] > have[(ing, "cooked")]) and ing in COOKABLE and req[(ing, "raw")] <= have[(ing, "raw")]
            needs_chop = (req[(ing, "chopped")] > have[(ing, "chopped")]) and ing in CHOPPABLE
            if needs_cook and not any(s.get("cooking") is None and s["id"] not in claimed_stoves for s in stoves):
                continue
            if needs_chop and not any(not b.get("busy") and b["id"] not in claimed_boards for b in boards):
                continue
            if self._cmd(api, cid, bins_by_ing[ing]["id"]):
                self.chef_fetch[cid] = ing
                fetching.add(ing)
            return
        # nothing startable right now (bins reserved / stations busy) -> wait

    # -- a free chef claims a new order, cleans up, or idles ----------------
    def _drive_free(self, api, c, orders, order_by_id, order_c, contents_c, areas,
                    stands, stand_by_id, difficulty, trash_id,
                    owned_areas, owned_orders, n_areas):
        cid = c["id"]
        h = c.get("holding")

        # carrying something with no order (just escaped / leftover): a plate that
        # matches a live order is delivered, otherwise dump it; a stray component is
        # dumped.  Never risk a wrong delivery.
        if _is_plate(h):
            plate = Counter(_comp_key(it) for it in h.get("items", []))
            match = [o for o in orders
                     if _cnt_eq(order_c[o["id"]], plate) and stand_by_id.get(o["standId"]) is not None]
            if match:
                o = min(match, key=lambda o: _manhattan(_pos(c), _pos(stand_by_id[o["standId"]])))
                self._cmd(api, cid, o["standId"])
            else:
                self._cmd(api, cid, trash_id)
            return
        if h is not None:
            self._cmd(api, cid, trash_id)
            return

        # claim the best feasible un-owned order, with an empty un-owned area
        empty_free_areas = [i for i in range(n_areas)
                            if i not in owned_areas and sum(contents_c[i].values()) == 0]
        cands = [o for o in orders if o["id"] not in owned_orders
                 and self._feasible(o, list(order_c[o["id"]].elements()))]
        if cands and empty_free_areas:
            o = max(cands, key=lambda o: self._order_value(o, difficulty))
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
        # otherwise idle (the jam-breaker will edge it out if it blocks a lane)
