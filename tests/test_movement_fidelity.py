"""
Movement / collision / deadlock fidelity (the highest sim-to-real risk).

reference/core.js models NO movement — it applies interactions at stamped ticks.
The walking, A* pathing, tile-reservation collisions and the resulting
chokepoint deadlocks live ONLY in reference/game.js's client control layer, which
sim/env.py ports.  These tests pin the emergent dynamics a trained policy must
learn to avoid: two chefs can never share or pass through a tile, a chef wedged
in a chokepoint with no detour abandons its route after ~30 blocked move-attempts
(game.js: blockedTicks>=30), and it recovers via a detour the moment one opens.

Also pins the reconciled scripted-spawn timing: core.js fires the two early
spawns at EXACT ticks 60 and 180.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import constants as C
from sim.env import KitchenSim
from agents.planner import Planner

DT = 1.0 / 60.0


def _advance_movement(sim, n):
    """Drive only the movement layer (rebuild reservations + update chefs) so
    position assertions aren't perturbed by order spawns. Returns max tile-share
    violations seen (must stay 0)."""
    for _ in range(n):
        sim._rebuild_reservations()
        for c in sim.chefs:
            sim._update_chef(c, DT)
        _assert_no_tile_share(sim)


def _assert_no_tile_share(sim):
    seen = {}
    for c in sim.chefs:
        key = (c["x"], c["y"])
        assert key not in seen, f"two chefs share tile {key}: {seen[key]} & {c['id']}"
        seen[key] = c["id"]


def _park(sim, cid, x, y):
    c = sim.chefs[cid]
    c["x"], c["y"] = x, y
    c["path"] = []
    c["targetStation"] = None


# ---------------------------------------------------------------------------
# 1. No two chefs ever occupy the same tile, even under heavy planner congestion
# ---------------------------------------------------------------------------
def test_planner_run_never_shares_a_tile():
    for seed in (0, 7, 21):
        sim = KitchenSim(seed)
        planner = Planner()

        class _Api:
            def command(self, cid, tid):
                return sim.command(cid, tid)

            def boost(self, cid):
                return sim.boost(cid)

        api = _Api()
        tick = 0
        # 90 s of real congestion is plenty to exercise the chokepoints.
        while not sim.game_over and sim.time < 90.0:
            if tick % 3 == 0:
                planner.decide(sim.get_state(), api)
            sim.tick(DT)
            _assert_no_tile_share(sim)
            tick += 1


# ---------------------------------------------------------------------------
# 2. Chokepoint freeze + abandon (game.js blockedTicks>=30 with no detour)
# ---------------------------------------------------------------------------
def test_chokepoint_freeze_then_abandon():
    sim = KitchenSim(0)
    # Pass-through window is the only divider gap: tiles (13,6) and (13,7).
    _park(sim, 1, 13, 6)          # block both gap tiles -> no detour east
    _park(sim, 2, 13, 7)
    _park(sim, 3, 2, 2)
    _park(sim, 4, 2, 3)
    c0 = sim.chefs[0]
    c0["x"], c0["y"] = 12, 6
    c0["path"] = [(13, 6), (14, 6), (15, 6)]   # wants to cross to the service side
    c0["targetStation"] = None

    abandoned_tick = None
    max_blocked = 0
    for t in range(1, 600):
        sim._rebuild_reservations()
        for c in sim.chefs:
            sim._update_chef(c, DT)
        _assert_no_tile_share(sim)
        max_blocked = max(max_blocked, c0["blockedTicks"])
        if not c0["path"]:
            abandoned_tick = t
            break

    assert (c0["x"], c0["y"]) == (12, 6), "frozen chef must not have advanced"
    assert abandoned_tick is not None, "chef never abandoned a hopeless route"
    # 30 blocked move-attempts at MOVE_DELAY=0.18s each ~= 5.4s; allow a small band.
    assert 4.5 <= abandoned_tick * DT <= 6.5, abandoned_tick * DT
    assert c0["targetStation"] is None


# ---------------------------------------------------------------------------
# 3. Head-on: chefs cannot swap tiles / pass through each other
# ---------------------------------------------------------------------------
def test_headon_no_passthrough():
    sim = KitchenSim(0)
    _park(sim, 2, 2, 2)
    _park(sim, 3, 2, 3)
    _park(sim, 4, 2, 4)
    a, b = sim.chefs[0], sim.chefs[1]
    a["x"], a["y"] = 5, 6
    a["path"] = [(6, 6)]
    a["targetStation"] = None
    b["x"], b["y"] = 6, 6
    b["path"] = [(5, 6)]
    b["targetStation"] = None

    _advance_movement(sim, 120)   # 2 s — far longer than one move tick
    # Neither could enter the other's tile; both stay put (a 1-wide head-on lock).
    assert (a["x"], a["y"]) == (5, 6)
    assert (b["x"], b["y"]) == (6, 6)
    assert a["blockedTicks"] > 0 and b["blockedTicks"] > 0


# ---------------------------------------------------------------------------
# 4. Recovery: a frozen chef detours the instant a blocker clears
# ---------------------------------------------------------------------------
def test_frozen_chef_recovers_when_gap_opens():
    sim = KitchenSim(0)
    _park(sim, 1, 13, 6)
    _park(sim, 2, 13, 7)
    _park(sim, 3, 2, 2)
    _park(sim, 4, 2, 3)
    c0 = sim.chefs[0]
    c0["x"], c0["y"] = 12, 6
    c0["path"] = [(13, 6), (14, 6), (15, 6)]
    c0["targetStation"] = None

    _advance_movement(sim, 90)                 # ~1.5 s wedged, well before abandon
    assert (c0["x"], c0["y"]) == (12, 6)
    assert c0["path"], "should still be trying (not yet abandoned)"

    _park(sim, 2, 9, 10)                        # open the (13,7) gap
    _advance_movement(sim, 120)                 # give it time to detour through it
    assert (c0["x"], c0["y"]) != (12, 6), "chef should have moved once a gap opened"


# ---------------------------------------------------------------------------
# 5. Scripted early spawns land on EXACT ticks 60 & 180 (core.js parity)
# ---------------------------------------------------------------------------
def test_scripted_spawns_fire_on_exact_ticks():
    sim = KitchenSim(0)
    spawn_ticks = []
    prev = sim.order_id_counter
    for _ in range(240):                        # 4 s
        sim.tick(DT)
        if sim.order_id_counter > prev:
            spawn_ticks.append(sim.tick_count)
            prev = sim.order_id_counter
    # The only spawns in the first 4 s are the two scripted ones (debt is still
    # far below 1 at the ~20 s tutorial interval).
    assert C.INITIAL_SPAWN_TICKS[0] in spawn_ticks
    assert C.INITIAL_SPAWN_TICKS[1] in spawn_ticks
    assert 180 in spawn_ticks and 181 not in spawn_ticks   # the reconciled fix
