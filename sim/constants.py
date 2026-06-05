"""
Chef Overflow — extracted constants and formulas (spec section 3).

Single source of truth for every magic number and formula used by the simulator.
Values marked ``# [extract] game.js:<line>`` were read directly out of
``reference/game.js`` (the read-only ground truth) at the cited line; formulas are
re-expressed in Python rather than copied verbatim. Values marked ``# [confirmed]``
were already pinned in the design spec and verified against the source.

JavaScript ``Math.round`` rounds halves toward +inf (``floor(x + 0.5)``) whereas
Python's ``round`` is banker's rounding; the source uses ``Math.round`` when
computing order timers, so :func:`js_round` reproduces the JS behaviour exactly.
"""

import math

# ---------------------------------------------------------------------------
# Small numeric helpers (match JS semantics)
# ---------------------------------------------------------------------------


def js_round(x: float) -> int:
    """Replicate JavaScript ``Math.round`` (round half toward +infinity)."""
    return math.floor(x + 0.5)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def smoothstep01(x: float, edge0: float, edge1: float) -> float:
    """Hermite smoothstep, clamped to [0,1].  game.js:139 ``smoothstep01``."""
    if x <= edge0:
        return 0.0
    if x >= edge1:
        return 1.0
    t = (x - edge0) / (edge1 - edge0)
    return t * t * (3.0 - 2.0 * t)


# ---------------------------------------------------------------------------
# Grid / world geometry            [extract] game.js:9-11, 730-877
# ---------------------------------------------------------------------------
CELL_SIZE = 48          # [extract] game.js:9   (pixels; only matters for px<->tile)
MAP_WIDTH = 20          # [extract] game.js:10
MAP_HEIGHT = 14         # [extract] game.js:11

# Tile type ids — [extract] game.js:730
TILE_FLOOR = 0
TILE_WALL = 1
TILE_COUNTER = 2
TILE_INGREDIENT_BIN = 3
TILE_STOVE = 4
TILE_CUTTING_BOARD = 5
TILE_PLATING_AREA = 6
TILE_RECEPTION_STAND = 7
TILE_TRASH = 9

# Fixed station coordinates, read straight out of the layout block in game.js.
# Each entry is (x, y[, extra]) and ids follow the source's ``<kind>_<i>`` scheme.

# bins map fixed: bin_0 tomato ... bin_5 cheese   [confirmed] / [extract] game.js:786
INGREDIENT_BINS = [
    (1, 3, "tomato"),
    (1, 5, "lettuce"),
    (1, 7, "onion"),
    (1, 9, "meat"),
    (1, 11, "dough"),
    (3, 11, "cheese"),
]
STOVE_POSITIONS = [(4, 1), (6, 1), (8, 1)]                 # [extract] game.js:807
CUTTING_POSITIONS = [(5, 5), (8, 5)]                       # [extract] game.js:825
PLATING_POSITIONS = [(10, 5), (10, 8), (11, 5), (3, 5)]    # [extract] game.js:843
TRASH_POSITION = (3, 9)                                    # [extract] game.js:859
RECEPTION_POSITIONS = [                                    # [extract] game.js:864
    (17, 3), (17, 5), (17, 7), (17, 9), (17, 11),
]
CHEF_START_POSITIONS = [                                   # [extract] game.js:892
    (4, 8), (6, 8), (8, 8), (5, 10), (7, 10),
]
CHEF_NAMES = ["Red", "Green", "Blue", "Orange", "Purple"]  # [extract] game.js:900

NUM_CHEFS = 5           # [extract] game.js:892 (5 start positions)
NUM_STOVES = 3
NUM_BOARDS = 2
NUM_PLATING = 4
NUM_STANDS = 5
NUM_BINS = 6


def build_map():
    """Port of the map-construction block (game.js:754-886).

    Returns ``(grid, counters)`` where ``grid[y][x]`` is a tile id and
    ``counters`` is the ordered list of ``(x, y)`` counter tiles (scan order
    matches the source so ``counter_<i>`` ids line up).
    """
    grid = [[TILE_FLOOR for _ in range(MAP_WIDTH)] for _ in range(MAP_HEIGHT)]

    # Walls around the edges.
    for x in range(MAP_WIDTH):
        grid[0][x] = TILE_WALL
        grid[MAP_HEIGHT - 1][x] = TILE_WALL
    for y in range(MAP_HEIGHT):
        grid[y][0] = TILE_WALL
        grid[y][MAP_WIDTH - 1] = TILE_WALL

    # Kitchen / reception divider (vertical counter at x=13) with a pass-through.
    for y in range(1, MAP_HEIGHT - 1):
        grid[y][13] = TILE_COUNTER
    grid[6][13] = TILE_FLOOR
    grid[7][13] = TILE_FLOOR

    # Top counter in the kitchen (x = 1..12 on row 1).
    for x in range(1, 13):
        grid[1][x] = TILE_COUNTER

    # Stations overwrite whatever tile they sit on.
    for (x, y, _ing) in INGREDIENT_BINS:
        grid[y][x] = TILE_INGREDIENT_BIN
    for (x, y) in STOVE_POSITIONS:
        grid[y][x] = TILE_STOVE
    for (x, y) in CUTTING_POSITIONS:
        grid[y][x] = TILE_CUTTING_BOARD
    for (x, y) in PLATING_POSITIONS:
        grid[y][x] = TILE_PLATING_AREA
    tx, ty = TRASH_POSITION
    grid[ty][tx] = TILE_TRASH
    for (x, y) in RECEPTION_POSITIONS:
        grid[y][x] = TILE_RECEPTION_STAND

    # Counter tiles registered in scan order (y outer, x inner) — game.js:880.
    counters = []
    for y in range(MAP_HEIGHT):
        for x in range(MAP_WIDTH):
            if grid[y][x] == TILE_COUNTER:
                counters.append((x, y))
    return grid, counters


# ---------------------------------------------------------------------------
# Ingredients, states, recipes      [confirmed] game.js:548-642
# ---------------------------------------------------------------------------
INGREDIENTS = ["tomato", "lettuce", "onion", "meat", "dough", "cheese"]  # game.js:548

STATE_RAW = "raw"
STATE_CHOPPED = "chopped"
STATE_COOKED = "cooked"
STATE_BURNT = "burnt"

# Encoder-facing fixed orderings (one-hots).
INGREDIENT_INDEX = {ing: i for i, ing in enumerate(INGREDIENTS)}
STATES_ENCODE = [STATE_RAW, STATE_CHOPPED, STATE_COOKED]  # burnt -> all zeros
STATE_INDEX = {s: i for i, s in enumerate(STATES_ENCODE)}

# Items that the cutting board / stove pipeline produce that recipes actually use.
CHOPPABLE = ("lettuce", "tomato", "onion")   # game.js blocks dough; meat/cheese unused
COOKABLE = ("meat", "dough")                 # meat always, dough -> pizza base

# Dish order matches RECIPES insertion order in game.js (used for dish one-hot).
DISH_NAMES = [
    "Salad", "Burger", "Steak", "Pizza",
    "Deluxe Burger", "Feast Platter", "Supreme Pizza",
]
DISH_INDEX = {d: i for i, d in enumerate(DISH_NAMES)}

# components: list of (ingredient, state); static `difficulty` is display/info only
# (rollUpcomingOrder picks uniformly over the pool — difficulty is NOT a spawn
# weight; it is surfaced by getRecipes()).  [extract] game.js:566-642, 949-957, 3505
RECIPES = {
    "Salad": {
        "components": [("lettuce", "chopped"), ("tomato", "chopped")],
        "difficulty": 1, "icon": "SAL",
    },
    "Burger": {
        "components": [("meat", "cooked"), ("dough", "raw")],
        "difficulty": 2, "icon": "BRG",
    },
    "Steak": {
        "components": [("meat", "cooked")],
        "difficulty": 1, "icon": "STK",
    },
    "Pizza": {
        "components": [("dough", "cooked"), ("cheese", "raw"), ("tomato", "chopped")],
        "difficulty": 3, "icon": "PZZ",
    },
    "Deluxe Burger": {
        "components": [("meat", "cooked"), ("dough", "raw"), ("onion", "chopped")],
        "difficulty": 3, "icon": "DBR",
    },
    "Feast Platter": {
        "components": [
            ("meat", "cooked"), ("lettuce", "chopped"),
            ("tomato", "chopped"), ("cheese", "raw"),
        ],
        "difficulty": 4, "icon": "FST",
    },
    "Supreme Pizza": {
        "components": [
            ("dough", "cooked"), ("tomato", "chopped"),
            ("onion", "chopped"), ("cheese", "raw"),
        ],
        "difficulty": 4, "icon": "SPZ",
    },
}

# The 7 valid plate component types, fixed order (spec 5.2 plating multi-hot).
COMPONENT_TYPES = [
    ("lettuce", "chopped"),
    ("tomato", "chopped"),
    ("onion", "chopped"),
    ("meat", "cooked"),
    ("dough", "raw"),
    ("dough", "cooked"),
    ("cheese", "raw"),
]
COMPONENT_INDEX = {c: i for i, c in enumerate(COMPONENT_TYPES)}
NUM_COMPONENTS = len(COMPONENT_TYPES)


# ---------------------------------------------------------------------------
# Processing / movement durations
# ---------------------------------------------------------------------------
COOK_TIME = 4.0          # [extract] game.js:819  stove.maxCookTime
CHOP_TIME = 2.0          # [extract] game.js:836  cuttingBoard.maxProcessTime
MOVE_DELAY = 0.18        # [extract] game.js:31    seconds per tile ("Slower chef movement")

BOOST_TIME = 3.5         # [extract] game.js:3195
BOOST_COOLDOWN = 12.0    # [extract] game.js:3196
BOOST_MOVE_FACTOR = 0.5  # [extract] game.js:1691  moveDelay *= 0.5 while boosting

# Mid-route redirect stall — rewards committed plans.  [extract] game.js:1180-1181
COMMITMENT_STALL_SECONDS = 1.5
COMMITMENT_STALL_MIN_REMAINING = 3

# Stove thresholds expressed as multiples of maxCookTime — [extract] game.js:1321/1324/1737/3392
STOVE_READY_FRAC = 0.8     # cross-chef pickup allowed once cookTime >= 0.8*max
STOVE_BURNT_FRAC = 1.5     # cross-chef pickup yields burnt at >= 1.5*max
STOVE_AUTOBURN_FRAC = 2.0  # updateStations marks burnt at >= 2.0*max
# NB: the same chef that places the item is locked for exactly COOK_TIME and the
# auto-pickup path (game.js:1677) ALWAYS yields 'cooked' — so the normal pipeline
# never burns.  [confirmed]


# ---------------------------------------------------------------------------
# Orders, customers, failure
# ---------------------------------------------------------------------------
CUSTOMER_EAT_TIME = 10.0     # [extract] game.js:1393  stand occupied ~10s after delivery
MAX_FAILED_ORDERS = 3        # [confirmed] game.js:29
EXPIRE_PENALTY = 50          # [extract] game.js:1775 (applied as -50)
NO_STAND_SLOT_PENALTY = 50   # [extract] game.js:941   (applied as -50, high-pressure only)

UPCOMING_QUEUE_SIZE = 3      # [extract] game.js:946
MAX_SPAWNS_PER_FRAME = 2     # [extract] game.js:1599
INITIAL_SPAWN_TIMES = (1.0, 3.0)  # [extract] game.js:3257-3258 (setTimeout 1000/3000 ms)

# Rush — [extract] game.js:35, 1564-1592, 3238
RUSH_INITIAL_COOLDOWN = 20.0
RUSH_SPAWN_FACTOR = 0.70
RUSH_BURST_MAX = 3
RUSH_ACTIVE_HIGH = (12.0, 8.0)   # base, random span  -> 12..20s
RUSH_ACTIVE_LOW = (10.0, 6.0)    #                    -> 10..16s
RUSH_COOLDOWN_HIGH = (15.0, 5.0)  #                   -> 15..20s
RUSH_COOLDOWN_LOW = (30.0, 25.0)  #                   -> 30..55s

# VIP — [extract] game.js:956, 996
VIP_PROB_BASE = 0.07
VIP_PROB_TIME_DIV = 9000.0
VIP_PROB_CAP = 0.16
VIP_TIME_FACTOR = 0.85   # vip orders get floor(timeLimit * 0.85)
VIP_SCORE_MULT = 1.5     # [confirmed] game.js:1378

# Phase boundaries (seconds) — [confirmed] game.js:123
PHASE_RAMP_START = 60
PHASE_AUTOMATION_START = 150
PHASE_ENDURANCE_START = 600

DEFAULT_TIME_CAP = 1200.0  # spec 5.4 sim-time cap (~1200s)


# ---------------------------------------------------------------------------
# Phase / pool helpers
# ---------------------------------------------------------------------------
def phase_key(time: float) -> str:
    """game.js:123 ``getPhaseKey``."""
    if time < PHASE_RAMP_START:
        return "tutorial"
    if time < PHASE_AUTOMATION_START:
        return "ramp"
    if time < PHASE_ENDURANCE_START:
        return "automation"
    return "endurance"


PHASES = ["tutorial", "ramp", "automation", "endurance"]
PHASE_INDEX = {p: i for i, p in enumerate(PHASES)}


def is_high_pressure(phase: str) -> bool:
    """game.js:135 ``isHighPressurePhase``."""
    return phase == "automation" or phase == "endurance"


def recipe_pool(time: float):
    """game.js:159 ``getRecipeNamesForSpawn``.  Returns list of dish names, or the
    full ``DISH_NAMES`` for endurance."""
    phase = phase_key(time)
    if phase == "tutorial":
        return ["Salad", "Steak"]
    if phase == "ramp":
        return ["Salad", "Steak", "Burger"]
    if phase == "endurance":
        return list(DISH_NAMES)
    rel = time - PHASE_AUTOMATION_START
    pool = ["Salad", "Steak", "Burger"]
    if rel >= 35:
        pool.append("Pizza")
    if rel >= 95:
        pool.append("Deluxe Burger")
    if rel >= 170:
        pool.append("Feast Platter")
    if rel >= 255:
        pool.append("Supreme Pizza")
    return pool


# ---------------------------------------------------------------------------
# Performance rubber-band  — game.js:146 ``getPerformanceAdjustment``  [confirmed]
# ---------------------------------------------------------------------------
def perf(delivered: int, failed: int, streak: int) -> float:
    total = delivered + failed
    success_rate = (delivered / total) if total > 0 else 0.6
    streak_bonus = min(0.2, streak * 0.01)
    fail_penalty = min(0.18, failed * 0.05)
    return clamp((success_rate - 0.55) * 0.28 + streak_bonus - fail_penalty, -0.2, 0.3)


# ---------------------------------------------------------------------------
# Spawn interval — game.js:175 ``baseOrderSpawnInterval``  [confirmed]
# Deterministic given (time, rush_active, performance).
# ---------------------------------------------------------------------------
def base_spawn_interval(time: float, rush_active: bool, performance: float) -> float:
    if time < 60:
        normal = 20.0 - smoothstep01(time, 0, 55) * 8.0      # 20 -> 12
    elif time < 150:
        normal = 12.0 - smoothstep01(time, 60, 145) * 4.0    # 12 -> 8
    elif time < 600:
        normal = 8.0 - smoothstep01(time, 150, 580) * 4.0    # 8 -> 4
    else:
        normal = max(2.5, 4.0 - (time - 600) * 0.003)        # 4 -> 2.5
    if rush_active:
        normal *= RUSH_SPAWN_FACTOR
    return max(2.5, normal * (1.0 - performance * 0.35))


# ---------------------------------------------------------------------------
# Order time limit — game.js:191 ``orderTimeLimitForSpawn``  [confirmed]
# Split into a deterministic core (testable) plus a uniform integer addend that
# the simulator rolls from its seeded RNG.  Returns (core, rand_count): the final
# limit is ``core + randint(0, rand_count - 1)``.  tutorial/ramp ignore perf.
# ---------------------------------------------------------------------------
def order_time_limit_core(time: float, performance: float):
    phase = phase_key(time)
    if phase == "tutorial":
        return 52, 6          # 52 + floor(rand*6)  -> 52..57
    if phase == "ramp":
        return 40, 6          # 40 + floor(rand*6)  -> 40..45
    if phase == "endurance":
        sec = max(14.0, 22.0 - (time - 600) * 0.012)   # 22 -> 14
    else:
        u = smoothstep01(time, 150, 520)
        sec = js_round(38.0 - u * 16.0)                # 38 -> 22
    sec = max(14.0, sec)
    sec = js_round(sec * (1.0 - performance * 0.22))
    return sec, 5             # sec + floor(rand*5) -> sec..sec+4


# ---------------------------------------------------------------------------
# Difficulty multiplier — game.js:212 ``computeDifficulty``  [confirmed]
# ---------------------------------------------------------------------------
def compute_difficulty(time: float, performance: float) -> float:
    if time < 60:
        base = 1.0 + smoothstep01(time, 0, 58) * 0.1      # 1.0 -> 1.1
    elif time < 150:
        base = 1.1 + smoothstep01(time, 60, 148) * 0.5    # 1.1 -> 1.6
    elif time < 600:
        base = 1.6 + smoothstep01(time, 150, 595) * 1.6   # 1.6 -> 3.2
    else:
        base = 3.2 + (time - 600) * 0.006                 # +0.006/s
    return max(1.0, base * (1.0 + performance))


# ---------------------------------------------------------------------------
# VIP probability — game.js:956
# ---------------------------------------------------------------------------
def vip_probability(time: float) -> float:
    return min(VIP_PROB_CAP, VIP_PROB_BASE + time / VIP_PROB_TIME_DIV)


# ---------------------------------------------------------------------------
# Scoring — game.js:1375-1379  [confirmed]
# ---------------------------------------------------------------------------
def delivery_score(difficulty: float, time_left: float, streak: int, vip: bool) -> int:
    time_bonus = math.floor(time_left * 2)
    base_score = 100.0 * difficulty
    streak_mult = 1.0 + min(1.0, streak * 0.05)     # caps at 2.0 at streak 20
    vip_mult = VIP_SCORE_MULT if vip else 1.0
    return math.floor((base_score + time_bonus) * streak_mult * vip_mult)
