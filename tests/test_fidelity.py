"""
Phase 1 fidelity gate (spec section 4 / 9).

These tests pin the simulator to the literal behaviour of ``reference/game.js``:
the scoring formula, the spawn-interval / order-timer / difficulty curves across
all four phases, phase transitions and recipe-pool unlocks, and full
fetch -> process -> plate -> deliver pipelines for Steak, Salad, and Burger.
Where game.js behaves oddly (wrong deliveries, the 6-stack rejection, cooking
that never burns) we assert the odd behaviour, not the intuitive one.
"""

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import constants as C
from sim import encode as E
from sim.env import KitchenSim

DT = 1.0 / 60.0


# ===========================================================================
# 1. Scoring formula — exact hand-derived values  (game.js:1375-1379)
# ===========================================================================
@pytest.mark.parametrize(
    "difficulty,time_left,streak,vip,expected",
    [
        # timeBonus=floor(10*2)=20; base=320; streakMult=1.5; -> floor(340*1.5)=510
        (3.2, 10.0, 10, False, 510),
        # streakMult caps at 2.0 (streak>=20); vip 1.5 -> floor((320+20)*2*1.5)=1020
        (3.2, 10.0, 20, True, 1020),
        # streak 25 still caps at 2.0 -> identical to streak 20 (no vip): floor(340*2)=680
        (3.2, 10.0, 25, False, 680),
        # base=160; timeBonus=74; streakMult=1.15 -> floor(234*1.15)=floor(269.1)=269
        (1.6, 37.0, 3, False, 269),
        # streak 0 -> streakMult 1.0; difficulty 1.0 -> floor((100+0)*1)=100
        (1.0, 0.0, 0, False, 100),
    ],
)
def test_delivery_score_exact(difficulty, time_left, streak, vip, expected):
    assert C.delivery_score(difficulty, time_left, streak, vip) == expected


def test_delivery_score_floor_semantics():
    # (100*1.0 + floor(0.5*2)) * 1.0 = 101 ; floor stays 101
    assert C.delivery_score(1.0, 0.5, 0, False) == 101
    # timeBonus uses floor(timeLeft*2): timeLeft 0.4 -> floor(0.8)=0
    assert C.delivery_score(1.0, 0.4, 0, False) == 100


# ===========================================================================
# 2. Spawn interval / order timer / difficulty match the spec formulas
#    sampled across all four phases  (perf pinned so the value is deterministic)
# ===========================================================================
def test_js_round_half_up():
    # JS Math.round rounds .5 toward +inf, unlike Python's banker's rounding.
    assert C.js_round(22.5) == 23
    assert C.js_round(2.5) == 3
    assert C.js_round(-2.5) == -2
    assert C.js_round(2.4) == 2


def test_smoothstep_endpoints_and_mid():
    assert C.smoothstep01(0, 0, 55) == 0.0
    assert C.smoothstep01(55, 0, 55) == 1.0
    assert C.smoothstep01(27.5, 0, 55) == pytest.approx(0.5)  # t=0.5 -> 0.5


@pytest.mark.parametrize(
    "time,rush,perf,expected",
    [
        (0.0, False, 0.0, 20.0),       # tutorial start
        (27.5, False, 0.0, 16.0),      # tutorial mid (smoothstep 0.5 -> 20-4)
        (60.0, False, 0.0, 12.0),      # ramp start
        (150.0, False, 0.0, 8.0),      # automation start
        (600.0, False, 0.0, 4.0),      # endurance start
        (1000.0, False, 0.0, 2.8),     # endurance: 4-400*0.003
        (600.0, True, 0.0, 2.8),       # rush tightens 4*0.70=2.8
        (0.0, False, 0.3, 17.9),       # perf: 20*(1-0.3*0.35)
    ],
)
def test_base_spawn_interval(time, rush, perf, expected):
    assert C.base_spawn_interval(time, rush, perf) == pytest.approx(expected)


def test_spawn_interval_floor_at_2p5():
    # endurance + rush + max perf should bottom out at the 2.5s floor.
    assert C.base_spawn_interval(1200.0, True, 0.3) == pytest.approx(2.5)


@pytest.mark.parametrize(
    "time,perf,core,rand_count",
    [
        (0.0, 0.0, 52, 6),       # tutorial (perf ignored)
        (30.0, 0.5, 52, 6),      # tutorial: perf ignored, still 52
        (60.0, 0.0, 40, 6),      # ramp (perf ignored)
        (149.0, 0.9, 40, 6),     # ramp: perf ignored
        (150.0, 0.0, 38, 5),     # automation start
        (335.0, 0.0, 30, 5),     # automation mid (smoothstep 0.5 -> round(38-8))
        (520.0, 0.0, 22, 5),     # automation end
        (150.0, 0.3, 35, 5),     # automation w/ perf: round(38*0.934)=35
        (600.0, 0.0, 22, 5),     # endurance start
        (1000.0, 0.0, 17, 5),    # endurance: round(22-4.8)=17
        (1200.0, 0.0, 15, 5),    # endurance: round(22-7.2)=round(14.8)=15
    ],
)
def test_order_time_limit_core(time, perf, core, rand_count):
    c, n = C.order_time_limit_core(time, perf)
    assert (c, n) == (core, rand_count)


@pytest.mark.parametrize(
    "time,perf,expected",
    [
        (0.0, 0.0, 1.0),        # tutorial start
        (60.0, 0.0, 1.1),       # ramp start
        (150.0, 0.0, 1.6),      # automation start
        (372.5, 0.0, 2.4),      # automation mid (smoothstep 0.5 -> 1.6+0.8)
        (600.0, 0.0, 3.2),      # endurance start
        (900.0, 0.0, 5.0),      # endurance: 3.2+300*0.006
        (600.0, 0.3, 4.16),     # perf: 3.2*1.3
    ],
)
def test_compute_difficulty(time, perf, expected):
    assert C.compute_difficulty(time, perf) == pytest.approx(expected)


def test_difficulty_floor_at_1():
    # Worst perf early can't push difficulty below 1.0.
    assert C.compute_difficulty(0.0, -0.2) == pytest.approx(1.0)


def test_perf_default_and_clamp():
    # No deliveries/fails: successRate defaults to 0.6 -> (0.6-0.55)*0.28 = 0.014
    assert C.perf(0, 0, 0) == pytest.approx(0.014)
    # Heavy fails clamp at -0.2
    assert C.perf(0, 10, 0) == pytest.approx(-0.2)
    # Great play clamps at +0.3
    assert C.perf(100, 0, 50) == pytest.approx(0.3)


def test_vip_probability_curve():
    assert C.vip_probability(0.0) == pytest.approx(0.07)
    assert C.vip_probability(9000.0) == pytest.approx(0.16)   # cap reached well before
    assert C.vip_probability(100000.0) == pytest.approx(0.16)  # capped


# ===========================================================================
# 3. Phase transitions and recipe-pool unlocks fire at the right times
# ===========================================================================
@pytest.mark.parametrize(
    "time,phase",
    [
        (0.0, "tutorial"), (59.999, "tutorial"),
        (60.0, "ramp"), (149.999, "ramp"),
        (150.0, "automation"), (599.999, "automation"),
        (600.0, "endurance"), (1200.0, "endurance"),
    ],
)
def test_phase_key(time, phase):
    assert C.phase_key(time) == phase


def test_recipe_pool_unlocks():
    assert C.recipe_pool(0.0) == ["Salad", "Steak"]
    assert C.recipe_pool(60.0) == ["Salad", "Steak", "Burger"]
    assert C.recipe_pool(150.0) == ["Salad", "Steak", "Burger"]
    # Pizza unlocks at rel>=35 (t>=185)
    assert "Pizza" not in C.recipe_pool(184.9)
    assert "Pizza" in C.recipe_pool(185.0)
    # Deluxe Burger at t>=245
    assert "Deluxe Burger" not in C.recipe_pool(244.9)
    assert "Deluxe Burger" in C.recipe_pool(245.0)
    # Feast Platter at t>=320
    assert "Feast Platter" not in C.recipe_pool(319.9)
    assert "Feast Platter" in C.recipe_pool(320.0)
    # Supreme Pizza at t>=405
    assert "Supreme Pizza" not in C.recipe_pool(404.9)
    assert "Supreme Pizza" in C.recipe_pool(405.0)
    # endurance -> all 7
    assert set(C.recipe_pool(600.0)) == set(C.DISH_NAMES)


# ===========================================================================
# Helpers for pipeline tests
# ===========================================================================
def _drive(sim, chef_id, target_id, max_seconds=60.0):
    """Command chef to a station and tick until it goes idle again."""
    res = sim.command(chef_id, target_id)
    assert res["success"], res
    ok = sim.run_until_chef_idle(chef_id, dt=DT, max_seconds=max_seconds)
    assert ok, f"chef {chef_id} never went idle heading to {target_id}"


def _deliver_and_capture(sim, chef_id, stand_id, vip):
    """Issue a deliver command and tick until the delivery resolves, returning
    (expected_score, actual_score_delta, streak_before)."""
    order_id = None
    for s in sim.reception_stands:
        if s["id"] == stand_id and s["order"]:
            order_id = s["order"]["id"]
    assert order_id is not None
    res = sim.command(chef_id, stand_id)
    assert res["success"], res

    delivered_before = sim.delivered_total
    steps = int(60.0 / DT)
    for _ in range(steps):
        # record values that the delivery will use (pre-tick)
        order = next((o for o in sim.orders if o["id"] == order_id), None)
        time_left_pre = order["timeLeft"] if order else None
        streak_pre = sim.streak
        score_pre = sim.score
        sim.tick(DT)
        if sim.delivered_total > delivered_before:
            diff_used = sim.difficulty       # set at top of this tick; unchanged by delivery
            expected = C.delivery_score(diff_used, time_left_pre, streak_pre, vip)
            return expected, sim.score - score_pre, streak_pre
        if sim.game_over:
            break
    raise AssertionError("delivery never resolved")


def _holding(sim, chef_id):
    return sim.chefs[chef_id]["holding"]


def _interact_direct(sim, chef_id, stype, station):
    """Exercise the literal interactWithStation branch without introducing
    unrelated multi-chef path-blocking into small mechanic tests."""
    sim._interact(sim.chefs[chef_id], {"type": stype, "station": station})


# ===========================================================================
# 4. Counter mechanics: staging, handoff, persistence and merge edges
# ===========================================================================
def test_counter_deposit_pickup_persistence_and_handoff():
    sim = KitchenSim(seed=0)
    sim.reset(seed=0)

    item = {"ingredient": "tomato", "state": "raw"}

    _interact_direct(sim, 0, "ingredientBin", sim.ingredient_bins[0])
    assert _holding(sim, 0) == item

    _interact_direct(sim, 0, "counter", sim.counters[0])
    assert _holding(sim, 0) is None
    assert sim.counters[0]["items"] == [item]

    state_counter = sim.get_state()["stations"]["counters"][0]
    assert state_counter["id"] == "counter_0"
    assert state_counter["items"] == [item]

    sim.advance(2.0, dt=DT)
    assert sim.counters[0]["items"] == [item]

    _interact_direct(sim, 0, "counter", sim.counters[0])
    assert _holding(sim, 0) == item
    assert sim.counters[0]["items"] == []

    _interact_direct(sim, 0, "counter", sim.counters[0])
    assert _holding(sim, 0) is None
    assert sim.counters[0]["items"] == [item]

    _interact_direct(sim, 1, "counter", sim.counters[0])
    assert _holding(sim, 1) == item
    assert sim.counters[0]["items"] == []


def test_counter_occupied_component_noop():
    sim = KitchenSim(seed=0)
    sim.reset(seed=0)

    tomato = {"ingredient": "tomato", "state": "raw"}
    lettuce = {"ingredient": "lettuce", "state": "raw"}

    _interact_direct(sim, 0, "ingredientBin", sim.ingredient_bins[0])
    _interact_direct(sim, 0, "counter", sim.counters[1])
    assert sim.counters[1]["items"] == [tomato]

    _interact_direct(sim, 1, "ingredientBin", sim.ingredient_bins[1])
    _interact_direct(sim, 1, "counter", sim.counters[1])
    assert _holding(sim, 1) == lettuce
    assert sim.counters[1]["items"] == [tomato]

    _interact_direct(sim, 2, "counter", sim.counters[1])
    assert _holding(sim, 2) == tomato
    assert sim.counters[1]["items"] == []


def test_counter_plate_component_merge_edges():
    sim = KitchenSim(seed=0)
    sim.reset(seed=0)

    tomato = {"ingredient": "tomato", "state": "raw"}
    lettuce = {"ingredient": "lettuce", "state": "raw"}
    onion = {"ingredient": "onion", "state": "raw"}

    _interact_direct(sim, 0, "ingredientBin", sim.ingredient_bins[0])
    _interact_direct(sim, 0, "platingArea", sim.plating_areas[0])
    _interact_direct(sim, 0, "platingArea", sim.plating_areas[0])
    plate = _holding(sim, 0)
    assert plate == {"type": "plate", "items": [tomato]}

    _interact_direct(sim, 0, "counter", sim.counters[2])
    assert _holding(sim, 0) is None
    assert sim.counters[2]["items"] == [{"type": "plate", "items": [tomato]}]

    _interact_direct(sim, 1, "ingredientBin", sim.ingredient_bins[1])
    _interact_direct(sim, 1, "counter", sim.counters[2])
    assert _holding(sim, 1) is None
    assert sim.counters[2]["items"] == [{"type": "plate", "items": [tomato, lettuce]}]

    _interact_direct(sim, 2, "counter", sim.counters[2])
    assert _holding(sim, 2) == {"type": "plate", "items": [tomato, lettuce]}
    assert sim.counters[2]["items"] == []

    _interact_direct(sim, 3, "ingredientBin", sim.ingredient_bins[2])
    _interact_direct(sim, 3, "counter", sim.counters[2])
    assert sim.counters[2]["items"] == [onion]

    _interact_direct(sim, 2, "counter", sim.counters[2])
    assert _holding(sim, 2) == {"type": "plate", "items": [tomato, lettuce, onion]}
    assert sim.counters[2]["items"] == []


# ===========================================================================
# 4a. Full Steak pipeline: fetch meat -> cook -> plate -> deliver
# ===========================================================================
def test_pipeline_steak():
    sim = KitchenSim(seed=0)
    sim.reset(seed=0)
    order = sim.debug_place_order("Steak", time_left=50.0, vip=False, stand_index=0)

    # fetch raw meat (bin_3)
    _drive(sim, 0, "bin_3")
    h = _holding(sim, 0)
    assert h == {"ingredient": "meat", "state": "raw"}

    # cook it (stove) — chef locks for COOK_TIME then auto-receives 'cooked' (never burns)
    _drive(sim, 0, "stove_0")
    h = _holding(sim, 0)
    assert h == {"ingredient": "meat", "state": "cooked"}

    # deposit onto a plating area
    _drive(sim, 0, "plating_3")
    assert _holding(sim, 0) is None
    assert {"ingredient": "meat", "state": "cooked"} in sim.plating_areas[3]["items"]

    # pick the area up as a plate (chef is already adjacent -> instant)
    _drive(sim, 0, "plating_3")
    h = _holding(sim, 0)
    assert h is not None and h["type"] == "plate"
    assert h["items"] == [{"ingredient": "meat", "state": "cooked"}]

    # deliver — exact match -> score & streak
    streak0 = sim.streak
    delivered0 = sim.delivered_total
    expected, delta, streak_pre = _deliver_and_capture(sim, 0, "reception_0", vip=False)
    assert delta == expected
    assert expected > 0
    assert sim.streak == streak_pre + 1 == streak0 + 1
    assert sim.delivered_total == delivered0 + 1
    assert _holding(sim, 0) is None
    # stand now occupied by an eating customer for ~10s
    assert sim.reception_stands[0]["order"] is None
    assert sim.reception_stands[0]["customer"] is not None


# ===========================================================================
# 4b. Full Salad pipeline: chop lettuce + chop tomato -> plate -> deliver
# ===========================================================================
def test_pipeline_salad():
    sim = KitchenSim(seed=1)
    sim.reset(seed=1)
    sim.debug_place_order("Salad", time_left=50.0, vip=False, stand_index=0)

    # lettuce -> chop
    _drive(sim, 0, "bin_1")
    assert _holding(sim, 0) == {"ingredient": "lettuce", "state": "raw"}
    _drive(sim, 0, "cutting_0")
    assert _holding(sim, 0) == {"ingredient": "lettuce", "state": "chopped"}
    _drive(sim, 0, "plating_3")
    assert _holding(sim, 0) is None

    # tomato -> chop
    _drive(sim, 0, "bin_0")
    assert _holding(sim, 0) == {"ingredient": "tomato", "state": "raw"}
    _drive(sim, 0, "cutting_0")
    assert _holding(sim, 0) == {"ingredient": "tomato", "state": "chopped"}
    _drive(sim, 0, "plating_3")
    assert _holding(sim, 0) is None

    # plate has both chopped components
    items = sim.plating_areas[3]["items"]
    assert {"ingredient": "lettuce", "state": "chopped"} in items
    assert {"ingredient": "tomato", "state": "chopped"} in items
    assert len(items) == 2

    _drive(sim, 0, "plating_3")  # take plate
    assert _holding(sim, 0)["type"] == "plate"

    expected, delta, streak_pre = _deliver_and_capture(sim, 0, "reception_0", vip=False)
    assert delta == expected and expected > 0
    assert sim.streak == streak_pre + 1
    assert sim.delivered_total == 1


# ===========================================================================
# 4c. Full Burger pipeline: cook meat + raw dough -> plate -> deliver
# ===========================================================================
def test_pipeline_burger():
    sim = KitchenSim(seed=2)
    sim.reset(seed=2)
    sim.debug_place_order("Burger", time_left=50.0, vip=False, stand_index=0)

    # cook meat
    _drive(sim, 0, "bin_3")
    _drive(sim, 0, "stove_0")
    assert _holding(sim, 0) == {"ingredient": "meat", "state": "cooked"}
    _drive(sim, 0, "plating_3")

    # raw dough (the bun is used RAW — distinct from cooked pizza base)
    _drive(sim, 0, "bin_4")
    assert _holding(sim, 0) == {"ingredient": "dough", "state": "raw"}
    _drive(sim, 0, "plating_3")
    assert _holding(sim, 0) is None

    items = sim.plating_areas[3]["items"]
    assert {"ingredient": "meat", "state": "cooked"} in items
    assert {"ingredient": "dough", "state": "raw"} in items
    assert len(items) == 2

    _drive(sim, 0, "plating_3")  # take plate
    assert _holding(sim, 0)["type"] == "plate"

    expected, delta, streak_pre = _deliver_and_capture(sim, 0, "reception_0", vip=False)
    assert delta == expected and expected > 0
    assert sim.streak == streak_pre + 1
    assert sim.delivered_total == 1


# ===========================================================================
# 5. Literal odd behaviours
# ===========================================================================
def test_wrong_delivery_resets_streak_and_clears_hands():
    """game.js: a wrong dish sets streak=0 AND clears the chef's hands, but does
    NOT count as a failed order, and leaves the order on the stand."""
    sim = KitchenSim(seed=3)
    sim.reset(seed=3)
    sim.debug_place_order("Steak", time_left=50.0, vip=False, stand_index=0)
    sim.streak = 7
    failed_before = sim.failed_orders

    # Build a WRONG plate: a single chopped tomato (Steak wants [meat@cooked]).
    chef = sim.chefs[0]
    chef["holding"] = {"type": "plate", "items": [{"ingredient": "tomato", "state": "chopped"}]}

    res = sim.command(0, "reception_0")
    assert res["success"]
    sim.run_until_chef_idle(0, dt=DT)

    assert sim.streak == 0                       # streak nuked
    assert _holding(sim, 0) is None              # hands cleared
    assert sim.failed_orders == failed_before    # NOT a failed order
    assert sim.wrong_total == 1
    assert sim.reception_stands[0]["order"] is not None  # order still waiting


def test_six_meat_stack_rejected():
    """A 6-meat stack must be rejected for a Steak (length mismatch)."""
    sim = KitchenSim(seed=4)
    sim.reset(seed=4)
    sim.debug_place_order("Steak", time_left=50.0, vip=False, stand_index=0)
    sim.streak = 3
    chef = sim.chefs[0]
    chef["holding"] = {"type": "plate",
                       "items": [{"ingredient": "meat", "state": "cooked"} for _ in range(6)]}
    sim.command(0, "reception_0")
    sim.run_until_chef_idle(0, dt=DT)
    assert sim.streak == 0
    assert sim.delivered_total == 0
    assert sim.wrong_total == 1


def test_cooking_never_burns_in_normal_pipeline():
    """Same-chef cook locks for exactly COOK_TIME and yields 'cooked', never
    'burnt' — even though the stove would burn at 2x maxCookTime if abandoned."""
    sim = KitchenSim(seed=5)
    sim.reset(seed=5)
    _drive(sim, 0, "bin_3")           # raw meat
    _drive(sim, 0, "stove_0")         # cook + auto-pickup
    assert _holding(sim, 0)["state"] == "cooked"


def test_dough_cannot_be_chopped():
    """game.js blocks chopping dough ('no slicing bread'); the chef keeps it."""
    sim = KitchenSim(seed=6)
    sim.reset(seed=6)
    _drive(sim, 0, "bin_4")           # raw dough
    assert _holding(sim, 0) == {"ingredient": "dough", "state": "raw"}
    # command to a board: chef walks there and the interaction is a no-op for dough
    res = sim.command(0, "cutting_0")
    assert res["success"]
    sim.run_until_chef_idle(0, dt=DT)
    assert _holding(sim, 0) == {"ingredient": "dough", "state": "raw"}  # unchanged
    assert sim.cutting_boards[0]["busy"] is False


def test_expired_order_counts_as_failure_and_penalizes():
    """An order that runs out of time: failedOrders++, streak=0, score -=50."""
    sim = KitchenSim(seed=7)
    sim.reset(seed=7)
    sim.streak = 5
    sim.debug_place_order("Steak", time_left=0.5, vip=False, stand_index=0)
    score_before = sim.score
    failed_before = sim.failed_orders
    sim.advance(1.0, dt=DT)   # let the 0.5s clock expire
    assert sim.failed_orders == failed_before + 1
    assert sim.expired_total == 1
    assert sim.streak == 0
    assert sim.score == pytest.approx(score_before - C.EXPIRE_PENALTY)
    assert sim.reception_stands[0]["order"] is None


def test_commitment_stall_on_midroute_redirect():
    """Redirecting a chef that still has >=3 path tiles to a DIFFERENT station
    pays a 1.5s stall; the first assignment and same-target re-commands do not."""
    sim = KitchenSim(seed=20)
    sim.reset(seed=20)
    # First assignment from an empty path -> no stall.
    assert sim.command(0, "reception_4")["success"]
    assert len(sim.chefs[0]["path"]) >= C.COMMITMENT_STALL_MIN_REMAINING
    assert sim.chefs[0]["commitmentStall"] == 0.0
    # Redirect to a different station while mid-route -> stall.
    assert sim.command(0, "bin_0")["success"]
    assert sim.chefs[0]["commitmentStall"] == pytest.approx(C.COMMITMENT_STALL_SECONDS)

    # Same target re-command does not stall.
    sim.reset(seed=20)
    sim.command(0, "reception_4")
    sim.command(0, "reception_4")
    assert sim.chefs[0]["commitmentStall"] == 0.0


def test_commitment_stall_freezes_chef():
    """While stalled the chef does not advance along its path."""
    sim = KitchenSim(seed=21)
    sim.reset(seed=21)
    sim.command(0, "reception_4")
    sim.command(0, "bin_0")            # triggers the stall
    pos_before = (sim.chefs[0]["x"], sim.chefs[0]["y"])
    sim.advance(1.0, dt=DT)            # less than the 1.5s stall
    assert sim.chefs[0]["commitmentStall"] > 0.0
    assert (sim.chefs[0]["x"], sim.chefs[0]["y"]) == pos_before  # frozen in place


def test_game_over_at_three_failures():
    sim = KitchenSim(seed=8)
    sim.reset(seed=8)
    for i in range(3):
        sim.debug_place_order("Steak", time_left=0.2, vip=False, stand_index=i)
    sim.advance(1.0, dt=DT)
    assert sim.failed_orders >= 3
    assert sim.game_over is True
    assert sim.running is False


# ===========================================================================
# Determinism + encoder/env smoke checks
# ===========================================================================
def test_determinism_same_seed_same_trace():
    def run(seed):
        sim = KitchenSim(seed=seed)
        sim.reset(seed=seed)
        sim.advance(120.0, dt=DT)
        return (round(sim.score, 6), sim.orders_delivered, sim.failed_orders,
                sim.order_id_counter, len(sim.orders))
    assert run(42) == run(42)


def test_different_seeds_diverge():
    # The spawn *schedule* is deterministic (so the order count can match across
    # seeds), but which dishes/VIP get rolled is RNG-driven and must diverge.
    def run(seed):
        sim = KitchenSim(seed=seed)
        sim.reset(seed=seed)
        sim.advance(120.0, dt=DT)
        return tuple(sim.spawned_log)
    assert len({run(1), run(2), run(3), run(4)}) > 1


def test_encoder_dims_and_mask_not_appended():
    # Phase 3 contract: the obs is exactly the feature vector (mask is NOT appended;
    # it flows through env.action_masks()).  OBS_DIM == FEATURE_DIM.
    sim = KitchenSim(seed=9)
    sim.reset(seed=9)
    state = sim.get_state()
    obs = E.encode(state, 0)
    assert obs.shape == (E.OBS_DIM,)
    assert E.OBS_DIM == E.FEATURE_DIM
    assert obs.dtype.name == "float32"
    assert np.isfinite(obs).all()
    # The mask is no longer concatenated onto the observation tail.
    mask = E.action_mask(state, 0)
    assert not np.array_equal(obs[-E.NUM_ACTIONS:], mask)
    # At reset, an empty-handed chef can FETCH and WAIT but not DELIVER/CHOP/COOK.
    assert mask[E.FETCH_BASE + 3] == 1.0   # FETCH meat
    assert mask[E.ACT_WAIT] == 1.0
    assert mask[E.ACT_CHOP] == 0.0
    assert mask[E.ACT_COOK] == 0.0
    assert mask[E.DELIVER_BASE] == 0.0


def test_encoder_upcoming_orders_block():
    # The new anticipation block encodes the next UPCOMING_K dishes (one-hot) + eta.
    sim = KitchenSim(seed=9)
    sim.reset(seed=9)
    sim.advance(2.0, dt=DT)
    state = sim.get_state()
    upcoming = state["upcomingOrders"]
    assert len(upcoming) == E.UPCOMING_K                  # queue kept topped up
    obs = E.encode(state, 0)
    block = obs[E.OBS_DIM - E.UPCOMING_DIM:]              # upcoming is the final block
    for k, u in enumerate(upcoming):
        off = k * E.PER_UPCOMING_DIM
        di = C.DISH_INDEX[u["dish"]]
        assert block[off + di] == 1.0                    # dish one-hot set
        eta_slot = block[off + len(C.DISH_NAMES)]
        assert 0.0 <= eta_slot <= 1.0                    # eta normalized + clamped
        if u["etaSeconds"] < E.UPCOMING_ETA_NORM:
            assert eta_slot == pytest.approx(u["etaSeconds"] / E.UPCOMING_ETA_NORM)


def test_env_action_masks_method_matches_encoder():
    from sim.env import ChefOverflowEnv
    env = ChefOverflowEnv(seed=9, time_cap=120.0)
    env.reset(seed=9)
    am = env.action_masks()
    assert am.shape == (E.NUM_ACTIONS,)
    assert am.dtype == np.bool_
    expected = E.action_mask(env.sim.get_state(), env.decision_chef).astype(bool)
    assert np.array_equal(am, expected)


def test_mask_cook_and_chop_gating():
    sim = KitchenSim(seed=10)
    sim.reset(seed=10)
    # Hand the chef raw meat -> COOK should be valid, CHOP should not.
    sim.chefs[0]["holding"] = {"ingredient": "meat", "state": "raw"}
    mask = E.action_mask(sim.get_state(), 0)
    assert mask[E.ACT_COOK] == 1.0
    assert mask[E.ACT_CHOP] == 0.0
    assert mask[E.FETCH_BASE] == 0.0       # hands full -> cannot fetch
    assert mask[E.ACT_TRASH] == 1.0
    # Raw lettuce -> CHOP valid, COOK not.
    sim.chefs[0]["holding"] = {"ingredient": "lettuce", "state": "raw"}
    mask = E.action_mask(sim.get_state(), 0)
    assert mask[E.ACT_CHOP] == 1.0
    assert mask[E.ACT_COOK] == 0.0


def test_env_steps_and_terminates():
    from sim.env import ChefOverflowEnv
    env = ChefOverflowEnv(seed=11, time_cap=200.0)
    obs, info = env.reset()
    assert obs.shape == (E.OBS_DIM,)
    assert "action_mask" in info
    terminated = truncated = False
    steps = 0
    total_r = 0.0
    rng = __import__("random").Random(123)
    while not (terminated or truncated) and steps < 20000:
        mask = info["action_mask"]
        valid = [a for a in range(E.NUM_ACTIONS) if mask[a] > 0]
        action = rng.choice(valid)
        obs, r, terminated, truncated, info = env.step(action)
        total_r += r
        steps += 1
    assert terminated or truncated
    assert steps > 0
