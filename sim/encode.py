"""
Canonical observation + action-mask encoder (spec sections 5.2 / 5.3).

THIS IS THE ONE AND ONLY observation/action contract.  Both the Gym env and the
browser deploy must build their inputs through this module so that train-time and
deploy-time vectors are bit-for-bit identical.  ``encode`` and ``action_mask``
take a *state dict shaped exactly like the game's ``KitchenAPI.getState()``* plus
the index of the decision chef — nothing here may read anything the real
``getState()`` does not expose (otherwise the policy would silently break on the
live site).  This module stays pure-numpy (no SB3/torch) precisely so Phase 4 can
port it 1:1 to JS; the *only* normalization the policy ever sees is the one baked
in here (we deliberately do NOT wrap the env in VecNormalize, which would add a
running-stats layer the JS deploy could not reproduce).

Important fidelity note: the real ``getState()`` does **not** expose
``order.vip``.  The vip feature slots below are therefore always 0 for parity;
see the build report.  (The simulator still tracks vip internally for scoring.)

Phase 3 changes (vs the original spec 5.2):
    * ADDED ``upcomingOrders`` — the next ``UPCOMING_K`` dishes (one-hot) and their
      ``etaSeconds``.  Both fields are exposed by the live ``getState()``
      (game.js:3778 maps ``upcomingOrders -> {dish, components, etaSeconds}``); we
      read only dish+eta.  This is what lets the policy learn to *anticipate* /
      pre-stage instead of reacting purely to already-spawned orders.
    * The action mask is NO LONGER appended to the observation.  It is supplied to
      MaskablePPO out-of-band via ``ChefOverflowEnv.action_masks()`` (spec 5.3's
      "apply to the policy logits").  The obs is exactly the feature vector.

Action space (23 macros, spec 5.3):
    0..5   FETCH_{tomato,lettuce,onion,meat,dough,cheese}
    6      CHOP            (route held choppable raw -> a free cutting board)
    7      COOK            (route held cookable raw  -> a free stove)
    8..11  DEPOSIT_{0..3}  (drop held component on plating area i)
    12..15 TAKE_PLATE_{0..3}
    16..20 DELIVER_{0..4}  (route held plate to reception stand i)
    21     TRASH
    22     WAIT
"""

import numpy as np

from . import constants as C

# ---------------------------------------------------------------------------
# Action ids
# ---------------------------------------------------------------------------
FETCH_BASE = 0                      # FETCH_<ingredient> for ingredient i in INGREDIENTS
ACT_CHOP = 6
ACT_COOK = 7
DEPOSIT_BASE = 8                    # DEPOSIT_area0..3
TAKE_PLATE_BASE = 12               # TAKE_PLATE_area0..3
DELIVER_BASE = 16                  # DELIVER_stand0..4
ACT_TRASH = 21
ACT_WAIT = 22
NUM_ACTIONS = 23

# Human-readable names (handy for debugging / the planner / tests).
ACTION_NAMES = (
    [f"FETCH_{ing}" for ing in C.INGREDIENTS]
    + ["CHOP", "COOK"]
    + [f"DEPOSIT_{i}" for i in range(C.NUM_PLATING)]
    + [f"TAKE_PLATE_{i}" for i in range(C.NUM_PLATING)]
    + [f"DELIVER_{i}" for i in range(C.NUM_STANDS)]
    + ["TRASH", "WAIT"]
)
assert len(ACTION_NAMES) == NUM_ACTIONS


# ---------------------------------------------------------------------------
# Holding helpers (state-dict shaped exactly like getState()'s chef.holding)
# ---------------------------------------------------------------------------
def _is_plate(holding) -> bool:
    return bool(holding) and isinstance(holding, dict) and holding.get("type") == "plate"


def _held_component(holding):
    """(ingredient, state) tuple for a non-plate held item, else None."""
    if not holding or _is_plate(holding):
        return None
    return (holding.get("ingredient"), holding.get("state"))


def _stand_index(stand_id) -> int:
    # "reception_3" -> 3
    try:
        return int(str(stand_id).rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Action validity mask (spec 5.3)
# ---------------------------------------------------------------------------
def action_mask(state, decision_chef: int) -> np.ndarray:
    """1.0 where the macro is currently legal for ``decision_chef``, else 0.0."""
    mask = np.zeros(NUM_ACTIONS, dtype=np.float32)
    chefs = state["chefs"]
    if decision_chef < 0 or decision_chef >= len(chefs):
        mask[ACT_WAIT] = 1.0
        return mask
    chef = chefs[decision_chef]
    holding = chef.get("holding")
    comp = _held_component(holding)
    is_plate = _is_plate(holding)
    stations = state["stations"]

    free_stove = any(s.get("cooking") is None for s in stations["stoves"])
    free_board = any(not b.get("busy") for b in stations["cuttingBoards"])

    if holding is None:
        # FETCH_x: bins only accept an empty-handed chef.
        for i in range(len(C.INGREDIENTS)):
            mask[FETCH_BASE + i] = 1.0
        # TAKE_PLATE_i: pick up a non-empty plating area as a plate.
        for i, area in enumerate(stations["platingAreas"][: C.NUM_PLATING]):
            if len(area.get("items", [])) > 0:
                mask[TAKE_PLATE_BASE + i] = 1.0
    else:
        # Anything in hand can be trashed.
        mask[ACT_TRASH] = 1.0
        if not is_plate and comp is not None:
            ingredient, st = comp
            if st == C.STATE_RAW and ingredient in C.CHOPPABLE and free_board:
                mask[ACT_CHOP] = 1.0
            if st == C.STATE_RAW and ingredient in C.COOKABLE and free_stove:
                mask[ACT_COOK] = 1.0
            # DEPOSIT only valid components (keeps plates representable + clean).
            if comp in C.COMPONENT_INDEX:
                for i in range(C.NUM_PLATING):
                    mask[DEPOSIT_BASE + i] = 1.0
        if is_plate:
            # DELIVER_i: need a held plate and an order waiting at stand i.
            for i, stand in enumerate(stations["receptionStands"][: C.NUM_STANDS]):
                if stand.get("order") is not None:
                    mask[DELIVER_BASE + i] = 1.0

    mask[ACT_WAIT] = 1.0  # waiting is always legal
    return mask


# ---------------------------------------------------------------------------
# Observation dimensions
# ---------------------------------------------------------------------------
GLOBAL_DIM = 11               # time, phase(4), difficulty, streak, failed, rush(3)
DECISION_DIM = C.NUM_CHEFS    # decision-chef one-hot
HOLD_DIM = 1 + len(C.INGREDIENTS) + len(C.STATES_ENCODE) + C.NUM_COMPONENTS  # 1+6+3+7
PER_CHEF_DIM = 2 + HOLD_DIM + 3                    # pos(2) + holding + flags(3)
CHEFS_DIM = PER_CHEF_DIM * C.NUM_CHEFS
STOVES_DIM = 2 * C.NUM_STOVES
BOARDS_DIM = 2 * C.NUM_BOARDS
PLATING_DIM = C.NUM_COMPONENTS * C.NUM_PLATING
ORDER_K = 6                                        # spec 5.2 cap
PER_ORDER_DIM = 1 + len(C.DISH_NAMES) + 1 + C.NUM_STANDS + 1   # present,dish(7),t,stand(5),vip
ORDERS_DIM = PER_ORDER_DIM * ORDER_K

# Upcoming orders (Phase 3 anticipation).  The queue is kept topped to
# UPCOMING_QUEUE_SIZE, so all slots are normally present; short queues pad to zero.
UPCOMING_K = C.UPCOMING_QUEUE_SIZE                  # 3
PER_UPCOMING_DIM = len(C.DISH_NAMES) + 1           # dish one-hot(7) + etaSeconds
UPCOMING_DIM = PER_UPCOMING_DIM * UPCOMING_K
UPCOMING_ETA_NORM = 30.0                           # seconds; eta clamps to [0,1]

FEATURE_DIM = (
    GLOBAL_DIM + DECISION_DIM + CHEFS_DIM
    + STOVES_DIM + BOARDS_DIM + PLATING_DIM + ORDERS_DIM + UPCOMING_DIM
)
# Phase 3: the mask is NOT appended — it flows through env.action_masks().  The
# observation is exactly the normalized feature vector.
OBS_DIM = FEATURE_DIM


def _encode_holding(holding, out, off):
    """Write the HOLD_DIM holding block at offset ``off``; returns new offset."""
    if holding is None:
        return off + HOLD_DIM
    if _is_plate(holding):
        out[off] = 1.0  # is_plate flag
        # plate component multi-hot lives after is_plate + ingredient(6) + state(3)
        comp_off = off + 1 + len(C.INGREDIENTS) + len(C.STATES_ENCODE)
        present = set()
        for it in holding.get("items", []):
            key = (it.get("ingredient"), it.get("state"))
            if key in C.COMPONENT_INDEX:
                present.add(C.COMPONENT_INDEX[key])
        for idx in present:
            out[comp_off + idx] = 1.0
    else:
        ing = holding.get("ingredient")
        st = holding.get("state")
        if ing in C.INGREDIENT_INDEX:
            out[off + 1 + C.INGREDIENT_INDEX[ing]] = 1.0
        if st in C.STATE_INDEX:
            out[off + 1 + len(C.INGREDIENTS) + C.STATE_INDEX[st]] = 1.0
    return off + HOLD_DIM


def encode(state, decision_chef: int) -> np.ndarray:
    """Canonical fixed-length, normalized observation with the action mask
    appended.  Mirrors spec 5.2 and is the shared gym/browser contract."""
    out = np.zeros(OBS_DIM, dtype=np.float32)
    off = 0

    # --- Global ----------------------------------------------------------
    time = float(state.get("time", 0.0))
    out[off] = min(time / C.DEFAULT_TIME_CAP, 1.0); off += 1
    phase = state.get("phase", C.phase_key(time))
    out[off + C.PHASE_INDEX.get(phase, 0)] = 1.0; off += len(C.PHASES)
    out[off] = min(float(state.get("difficulty", 1.0)) / 5.0, 2.0); off += 1
    out[off] = min(float(state.get("streak", 0)), 20.0) / 20.0; off += 1
    out[off] = min(float(state.get("failedOrders", 0)) / C.MAX_FAILED_ORDERS, 1.0); off += 1
    rush = state.get("rush", {}) or {}
    out[off] = 1.0 if rush.get("active") else 0.0; off += 1
    out[off] = min(max(float(rush.get("timeLeft", 0.0)), 0.0) / 20.0, 1.0); off += 1
    out[off] = min(max(float(rush.get("cooldown", 0.0)), 0.0) / 55.0, 1.0); off += 1

    # --- Decision chef one-hot ------------------------------------------
    if 0 <= decision_chef < C.NUM_CHEFS:
        out[off + decision_chef] = 1.0
    off += C.NUM_CHEFS

    # --- Per chef --------------------------------------------------------
    chefs = state["chefs"]
    for i in range(C.NUM_CHEFS):
        c = chefs[i] if i < len(chefs) else None
        if c is not None:
            px, py = c.get("pos", (0, 0))
            out[off] = px / C.MAP_WIDTH
            out[off + 1] = py / C.MAP_HEIGHT
        off += 2
        off = _encode_holding(c.get("holding") if c else None, out, off)
        if c is not None:
            out[off] = 1.0 if c.get("busy") else 0.0
            out[off + 1] = 1.0 if c.get("hasPath") else 0.0
            out[off + 2] = 1.0 if float(c.get("stall", 0.0)) > 0.0 else 0.0
        off += 3

    # --- Stoves ----------------------------------------------------------
    for i in range(C.NUM_STOVES):
        s = state["stations"]["stoves"][i]
        cooking = s.get("cooking") is not None
        out[off] = 0.0 if cooking else 1.0          # free flag
        maxc = float(s.get("maxCookTime", C.COOK_TIME)) or C.COOK_TIME
        out[off + 1] = min(float(s.get("cookTime", 0.0)) / maxc, 1.0) if cooking else 0.0
        off += 2

    # --- Boards ----------------------------------------------------------
    for i in range(C.NUM_BOARDS):
        b = state["stations"]["cuttingBoards"][i]
        busy = bool(b.get("busy"))
        out[off] = 0.0 if busy else 1.0             # free flag
        maxp = float(b.get("maxProcessTime", C.CHOP_TIME)) or C.CHOP_TIME
        out[off + 1] = min(float(b.get("processTime", 0.0)) / maxp, 1.0) if busy else 0.0
        off += 2

    # --- Plating areas (component multi-hot) -----------------------------
    for i in range(C.NUM_PLATING):
        area = state["stations"]["platingAreas"][i]
        present = set()
        for it in area.get("items", []):
            key = (it.get("ingredient"), it.get("state"))
            if key in C.COMPONENT_INDEX:
                present.add(C.COMPONENT_INDEX[key])
        for idx in present:
            out[off + idx] = 1.0
        off += C.NUM_COMPONENTS

    # --- Active orders (cap K, padded) -----------------------------------
    orders = sorted(state.get("orders", []), key=lambda o: o.get("id", 0))[:ORDER_K]
    for k in range(ORDER_K):
        if k < len(orders):
            o = orders[k]
            out[off] = 1.0                                              # present
            di = C.DISH_INDEX.get(o.get("dish"))
            if di is not None:
                out[off + 1 + di] = 1.0
            out[off + 1 + len(C.DISH_NAMES)] = min(
                max(float(o.get("timeLeft", 0.0)), 0.0) / 60.0, 1.0)
            si = _stand_index(o.get("standId"))
            if 0 <= si < C.NUM_STANDS:
                out[off + 1 + len(C.DISH_NAMES) + 1 + si] = 1.0
            # vip: always 0 — real getState() hides it (see module docstring).
            out[off + 1 + len(C.DISH_NAMES) + 1 + C.NUM_STANDS] = float(o.get("vip", 0))
        off += PER_ORDER_DIM

    # --- Upcoming orders (anticipation, Phase 3) -------------------------
    # Next UPCOMING_K dishes the game will spawn, each: dish one-hot(7) + eta.
    upcoming = state.get("upcomingOrders", []) or []
    for k in range(UPCOMING_K):
        if k < len(upcoming):
            u = upcoming[k]
            di = C.DISH_INDEX.get(u.get("dish"))
            if di is not None:
                out[off + di] = 1.0
            out[off + len(C.DISH_NAMES)] = min(
                max(float(u.get("etaSeconds", 0.0)), 0.0) / UPCOMING_ETA_NORM, 1.0)
        off += PER_UPCOMING_DIM

    assert off == OBS_DIM, (off, OBS_DIM)
    return out
