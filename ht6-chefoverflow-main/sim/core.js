/**
 * sim/core.js — Pure, deterministic Chef Overflow simulation core.
 *
 * DO NOT import DOM / Date / performance / Math.random / window / document.
 *
 * ============================================================
 * DETERMINISM RULES
 * ============================================================
 *  1. Fixed timestep only (TICK_HZ = 60, DT = 1/60).  The sim never reads
 *     wall-clock time.
 *  2. All randomness via the rng() closure passed in; drawn in the SAME ORDER
 *     that game.js draws them:
 *       a) orderTimeLimitForSpawn()  (2 rng draws: base jitter + extra)
 *       b) rollUpcomingOrder()       (1 rng draw: recipe index)
 *       c) rollUpcomingOrder()       (1 rng draw: vip flag)
 *       d) spawnOrder()              (1 rng draw: stand selection)
 *       e) rush cooldown after rush ends (1 rng draw)
 *       f) rush duration at rush start   (1 rng draw)
 *     See "RNG draw order" section below for exact details.
 *  3. Deterministic iteration order: all collections are plain arrays with
 *     stable indices; no Object.keys / Map iteration affects logic.
 *  4. No floating-point accumulation drift: game time is tracked as an integer
 *     tick counter multiplied by DT for reads.
 *  5. No module-level mutable singletons: createSim() returns a self-contained
 *     object with all state local to the closure.
 *
 * ============================================================
 * FIDELITY NOTES  (things simplified vs game.js)
 * ============================================================
 *  A. CHEF MOVEMENT (B2a change) — For "preserve gameplay, sim validates":
 *     The client's real timing is authoritative.  An interact event
 *     { tick, type:'interact', chefId, stationId } is applied IMMEDIATELY at
 *     its stamped tick — no internal travel-time simulation.  The client emits
 *     one 'interact' event per actual interactWithStation() call (i.e., when the
 *     A*-walking chef reaches the station and fires the interaction).  Both client
 *     and server run createSim/simulate over the same event log to get identical
 *     scores.  The old 'command' event type is retained for backward compat
 *     (treated as an alias for 'interact').
 *
 *  B. COMMITMENT STALL (COMMITMENT_STALL_SECONDS = 1.5s) — The live game
 *     penalises mid-route redirects.  The sim does not model stalls because the
 *     input log records explicit interactions (not grid-level redirects).
 *     This is exact.
 *
 *  C. BOOST — chef boost (3.5s speed, 12s cooldown) is NOT modelled; it only
 *     affects movement speed, not scoring.  The input log may include a
 *     "boost" event for completeness (it is accepted and ignored).
 *
 *  D. UPCOMING ORDER QUEUE — The live game pre-rolls the next N=3 specs and
 *     stores them in upcomingOrders[].  The sim replicates this exactly so the
 *     rng draw order matches.
 *
 *  E. INITIAL ORDER SPAWNS — game.js fires two setTimeout spawns at 1s and 3s.
 *     The sim skips these pre-game spawns because the run timer starts at tick 0
 *     and the spawn debt system handles early orders organically.  This is an
 *     intentional simplification; remove this note once B2 is verified exact.
 *
 *  F. PLATING AREA / COUNTER — Intermediate item storage on plating areas and
 *     counters is fully modelled.  Chefs can place/pick up ingredients and
 *     plates.
 *
 *  G. NO-STAND-SLOT penalty in high-pressure phases — fully modelled.
 *
 * ============================================================
 * INPUT LOG SCHEMA  (B2a — immediate semantics)
 * ============================================================
 *  inputs is an array of event objects, sorted ascending by tick.
 *  Multiple events may share a tick (processed in array order).
 *
 *  Canonical event type (B2 client emits this):
 *
 *    { tick: number, type: 'interact', chefId: 0-4, stationId: string }
 *      — At this tick, chefId performs interactWithStation against stationId.
 *        The effect depends on station type + chef's current holding + station
 *        items, mirroring game.js's interactWithStation EXACTLY.
 *        stationId values match the station ids defined in the map layout:
 *        'bin_0'–'bin_5', 'stove_0'–'stove_2', 'cutting_0'–'cutting_1',
 *        'plating_0'–'plating_3', 'reception_0'–'reception_4',
 *        'trash_0', 'counter_0'–'counter_N'
 *
 *    { tick: number, type: 'command', chefId: 0-4, stationId: string }
 *      — Legacy alias for 'interact'.  Accepted for backward compatibility.
 *        Treated identically: the interaction fires IMMEDIATELY at the stamped
 *        tick, with no internal travel-time computation.
 *
 *    { tick: number, type: 'boost', chefId: 0-4 }
 *      — Accepted and ignored (see fidelity note C).
 *
 *  The client (B2) emits one 'interact' event each time interactWithStation()
 *  fires in the real game (i.e., at the tick when the chef physically arrives
 *  at the station and the interaction executes).
 *  Ticks are 0-indexed from run start; 60 ticks = 1 second.
 *
 * ============================================================
 * COMPACT WIRE FORMAT  (used in submit payload)
 * ============================================================
 *  To minimise payload size the client serialises the input log as an array of
 *  3-element tuples:
 *    [ tickDelta, chefId, stationCode ]
 *  where:
 *    tickDelta  — tick delta from previous event (first event: absolute tick).
 *                 Using deltas keeps numbers small; most interactions within
 *                 a few hundred ticks of each other.
 *    chefId     — 0–4
 *    stationCode — integer index into STATION_ID_TABLE (defined in game.js and
 *                  known to the server); avoids string overhead.
 *
 *  STATION_ID_TABLE (server must use the same ordering):
 *    0–5:  bin_0 … bin_5
 *    6–8:  stove_0 … stove_2
 *    9–10: cutting_0 … cutting_1
 *   11–14: plating_0 … plating_3
 *   15–19: reception_0 … reception_4
 *       20: trash_0
 *   21–N: counter_0 … counter_N  (N determined by map layout)
 *
 *  Decoding: reconstruct { tick, type:'interact', chefId, stationId } by:
 *    tick      = running sum of tickDeltas
 *    chefId    = tuple[1]
 *    stationId = STATION_ID_TABLE[tuple[2]]
 *  'boost' events are not included in the compact log (server-irrelevant).
 *
 * ============================================================
 * RNG DRAW ORDER (mirrors game.js exactly)
 * ============================================================
 *  Every call to rollUpcomingOrder() draws:
 *    draw 1: recipe index  (Math.floor(rng() * pool.length))
 *    draw 2: vip flag      (rng() < vipChance)
 *  Every call to orderTimeLimitForSpawn() draws:
 *    draw 1: base jitter   (Math.floor(rng() * 6))   [for tutorial/ramp phase]
 *    OR draw 1: extra      (Math.floor(rng() * 5))   [for automation/endurance phase]
 *  Note: in game.js, orderTimeLimitForSpawn() is called INSIDE rollUpcomingOrder()
 *  so the draw order per upcoming-order is:
 *    [timeLimit draw(s)] then [recipe draw] then [vip draw]
 *
 *  Rush cooldown/duration:
 *    After rush ends: rng() for new cooldown  (isHighPressure ? 15+rng()*5 : 30+rng()*25)
 *    At rush start:   rng() for duration      (isHighPressure ? 12+rng()*8 : 10+rng()*6)
 *
 *  Stand selection at spawnOrder():
 *    rng() for availableStands index
 */

'use strict';

import { makeRng } from './prng.js';

// ============================================================
// CONSTANTS
// ============================================================

export const TICK_HZ = 60;
export const DT = 1 / TICK_HZ;  // ~0.01667s

const MAP_WIDTH  = 20;
const MAP_HEIGHT = 14;
const CELL_SIZE  = 48; // pixels — only used for adjacency, not rendering

// ============================================================
// STATIC GAME DATA (extracted from game.js verbatim)
// ============================================================

const INGREDIENT_STATES = { RAW: 'raw', CHOPPED: 'chopped', COOKED: 'cooked', BURNT: 'burnt' };

const RECIPES = {
  'Salad':        { components: [{ ingredient: 'lettuce', state: 'chopped' }, { ingredient: 'tomato', state: 'chopped' }], difficulty: 1 },
  'Burger':       { components: [{ ingredient: 'meat',   state: 'cooked'  }, { ingredient: 'dough',  state: 'raw'     }], difficulty: 2 },
  'Steak':        { components: [{ ingredient: 'meat',   state: 'cooked'  }], difficulty: 1 },
  'Pizza':        { components: [{ ingredient: 'dough',  state: 'cooked'  }, { ingredient: 'cheese', state: 'raw'     }, { ingredient: 'tomato', state: 'chopped' }], difficulty: 3 },
  'Deluxe Burger':{ components: [{ ingredient: 'meat',   state: 'cooked'  }, { ingredient: 'dough',  state: 'raw'     }, { ingredient: 'onion',  state: 'chopped' }], difficulty: 3 },
  'Feast Platter':{ components: [{ ingredient: 'meat',   state: 'cooked'  }, { ingredient: 'lettuce',state: 'chopped' }, { ingredient: 'tomato', state: 'chopped' }, { ingredient: 'cheese', state: 'raw' }], difficulty: 4 },
  'Supreme Pizza':{ components: [{ ingredient: 'dough',  state: 'cooked'  }, { ingredient: 'tomato', state: 'chopped' }, { ingredient: 'onion',  state: 'chopped' }, { ingredient: 'cheese', state: 'raw' }], difficulty: 4 },
};

const RECIPE_NAMES_BY_PHASE = {
  tutorial: ['Salad', 'Steak'],
  ramp:     ['Salad', 'Steak', 'Burger'],
};

const TILE_TYPES = { FLOOR: 0, WALL: 1, COUNTER: 2, INGREDIENT_BIN: 3, STOVE: 4, CUTTING_BOARD: 5, PLATING_AREA: 6, TRASH: 9, RECEPTION_STAND: 7 };

const UPCOMING_QUEUE_SIZE = 3;
const MOVE_DELAY          = 0.18;  // seconds per tile (default, non-boost)
const MAX_FAILED_ORDERS   = 3;
const ORDER_EXPIRED_PENALTY = 50;
const NO_STAND_SLOT_PENALTY = 50;
const CUSTOMER_EAT_TIME   = 10;    // seconds customer sits after delivery

// ============================================================
// TRAVEL-TIME / REACHABILITY VALIDATION (anti-teleport)
// ============================================================
// The sim applies each interact event immediately at its stamped tick and does
// NOT model chef movement (FIDELITY NOTE A).  A *crafted* input log can therefore
// make one chef interact with stations on opposite sides of the map on
// consecutive ticks — physically impossible, yet scored as valid.  When
// cfg.checkTravel is set, checkTravel() enforces a conservative LOWER BOUND on
// the ticks that must elapse between a chef's consecutive interactions, derived
// from the shortest walkable path between the two stations' standing tiles.
//
// The bound is deliberately LOOSE so it never false-rejects honest play:
//   • Fastest real movement is the boosted rate moveDelay*0.5 = 0.09 s/tile.
//     In sim-tick units that is 0.09 * 60 = 5.4 ticks/tile (12 ticks/tile
//     unboosted).  Client movement and interaction tick-stamps share one clock
//     (game.js gameLoop calls _tickSim(dt) and update(dt) with the same dt), so
//     this holds at any framerate.
//   • We assume an even faster 0.075 s/tile (TRAVEL_TICKS_PER_TILE = 4.5) and
//     subtract a flat slack, and we take the MIN path over all standing-tile
//     pairs — three independent margins.  Any honest (even boosted, high-refresh)
//     run clears the bound; a teleporting cheat (≈0-tick delta over a 20+-tile
//     cross-map path) violates it by a wide margin.
const TRAVEL_TICKS_PER_TILE = 4.5;
const TRAVEL_SLACK_TICKS    = 4;

// ============================================================
// MAP CONSTRUCTION (mirrored from game.js layout code)
// ============================================================

/**
 * Build the static tile map.  Returns a 2-D array map[y][x].
 * This is deterministic (no rng) and identical to game.js.
 */
function buildMap() {
  const map = [];
  for (let y = 0; y < MAP_HEIGHT; y++) {
    map[y] = new Array(MAP_WIDTH).fill(TILE_TYPES.FLOOR);
  }
  // Outer walls
  for (let x = 0; x < MAP_WIDTH; x++) {
    map[0][x] = TILE_TYPES.WALL;
    map[MAP_HEIGHT - 1][x] = TILE_TYPES.WALL;
  }
  for (let y = 0; y < MAP_HEIGHT; y++) {
    map[y][0] = TILE_TYPES.WALL;
    map[y][MAP_WIDTH - 1] = TILE_TYPES.WALL;
  }
  // Kitchen/Reception vertical divider counter
  for (let y = 1; y < MAP_HEIGHT - 1; y++) map[y][13] = TILE_TYPES.COUNTER;
  // Pass-through window
  map[6][13] = TILE_TYPES.FLOOR;
  map[7][13] = TILE_TYPES.FLOOR;
  // Top counter in kitchen
  for (let x = 1; x < 13; x++) map[1][x] = TILE_TYPES.COUNTER;
  // Ingredient bins
  const binPos = [
    { x: 1, y: 3 }, { x: 1, y: 5 }, { x: 1, y: 7 },
    { x: 1, y: 9 }, { x: 1, y: 11 }, { x: 3, y: 11 },
  ];
  binPos.forEach(p => { map[p.y][p.x] = TILE_TYPES.INGREDIENT_BIN; });
  // Stoves
  [{ x: 4, y: 1 }, { x: 6, y: 1 }, { x: 8, y: 1 }].forEach(p => { map[p.y][p.x] = TILE_TYPES.STOVE; });
  // Cutting boards
  [{ x: 5, y: 5 }, { x: 8, y: 5 }].forEach(p => { map[p.y][p.x] = TILE_TYPES.CUTTING_BOARD; });
  // Plating areas
  [{ x: 10, y: 5 }, { x: 10, y: 8 }, { x: 11, y: 5 }, { x: 3, y: 5 }].forEach(p => { map[p.y][p.x] = TILE_TYPES.PLATING_AREA; });
  // Trash
  map[9][3] = TILE_TYPES.TRASH;
  // Reception stands
  [{ x: 17, y: 3 }, { x: 17, y: 5 }, { x: 17, y: 7 }, { x: 17, y: 9 }, { x: 17, y: 11 }].forEach(p => { map[p.y][p.x] = TILE_TYPES.RECEPTION_STAND; });
  return map;
}

/** Build all station objects.  Returns { ingredientBins, stoves, cuttingBoards, platingAreas, counters, trashCans, receptionStands }. */
function buildStations(map) {
  const ingredientDefs = [
    { x: 1, y: 3,  ingredient: 'tomato'  },
    { x: 1, y: 5,  ingredient: 'lettuce' },
    { x: 1, y: 7,  ingredient: 'onion'   },
    { x: 1, y: 9,  ingredient: 'meat'    },
    { x: 1, y: 11, ingredient: 'dough'   },
    { x: 3, y: 11, ingredient: 'cheese'  },
  ];
  const ingredientBins = ingredientDefs.map((pos, i) => ({
    id: `bin_${i}`, x: pos.x, y: pos.y, ingredient: pos.ingredient,
  }));

  const stovePositions = [{ x: 4, y: 1 }, { x: 6, y: 1 }, { x: 8, y: 1 }];
  const stoves = stovePositions.map((pos, i) => ({
    id: `stove_${i}`, x: pos.x, y: pos.y,
    cooking: null, cookTime: 0, maxCookTime: 4, busy: false,
  }));

  const cuttingPositions = [{ x: 5, y: 5 }, { x: 8, y: 5 }];
  const cuttingBoards = cuttingPositions.map((pos, i) => ({
    id: `cutting_${i}`, x: pos.x, y: pos.y,
    processing: null, processTime: 0, maxProcessTime: 2, busy: false,
  }));

  const platingPositions = [{ x: 10, y: 5 }, { x: 10, y: 8 }, { x: 11, y: 5 }, { x: 3, y: 5 }];
  const platingAreas = platingPositions.map((pos, i) => ({
    id: `plating_${i}`, x: pos.x, y: pos.y, items: [], busy: false,
  }));

  const trashCans = [{ id: 'trash_0', x: 3, y: 9 }];

  const receptionPositions = [
    { x: 17, y: 3 }, { x: 17, y: 5 }, { x: 17, y: 7 }, { x: 17, y: 9 }, { x: 17, y: 11 },
  ];
  const receptionStands = receptionPositions.map((pos, i) => ({
    id: `reception_${i}`, x: pos.x, y: pos.y, order: null, customer: null,
  }));

  // Build counter list from the map
  const counters = [];
  for (let y = 0; y < MAP_HEIGHT; y++) {
    for (let x = 0; x < MAP_WIDTH; x++) {
      if (map[y][x] === TILE_TYPES.COUNTER) {
        counters.push({ id: `counter_${counters.length}`, x, y, items: [] });
      }
    }
  }

  return { ingredientBins, stoves, cuttingBoards, platingAreas, counters, trashCans, receptionStands };
}

/** Initial chef positions. */
const CHEF_START_POSITIONS = [
  { x: 4, y: 8 }, { x: 6, y: 8 }, { x: 8, y: 8 }, { x: 5, y: 10 }, { x: 7, y: 10 },
];

// ============================================================
// PURE HELPER FUNCTIONS (no side effects, no rng)
// ============================================================

function getPhaseKey(time) {
  if (time < 60)  return 'tutorial';
  if (time < 150) return 'ramp';
  if (time < 600) return 'automation';
  return 'endurance';
}

function isHighPressurePhase(phase) {
  return phase === 'automation' || phase === 'endurance';
}

function smoothstep01(x, edge0, edge1) {
  if (x <= edge0) return 0;
  if (x >= edge1) return 1;
  const t = (x - edge0) / (edge1 - edge0);
  return t * t * (3 - 2 * t);
}

function getPerformanceAdjustment(ordersDelivered, failedOrders, streak) {
  const delivered = ordersDelivered;
  const failed    = failedOrders;
  const successRate = delivered + failed > 0 ? delivered / (delivered + failed) : 0.6;
  const streakBonus  = Math.min(0.2, streak * 0.01);
  const failPenalty  = Math.min(0.18, failed * 0.05);
  return Math.max(-0.2, Math.min(0.3, (successRate - 0.55) * 0.28 + streakBonus - failPenalty));
}

function computeDifficulty(time, ordersDelivered, failedOrders, streak) {
  const phase = getPhaseKey(time);
  let base;
  if (phase === 'tutorial')   base = 1.0 + smoothstep01(time, 0,   58)  * 0.1;
  else if (phase === 'ramp')  base = 1.1 + smoothstep01(time, 60,  148) * 0.5;
  else if (phase === 'automation') base = 1.6 + smoothstep01(time, 150, 595) * 1.6;
  else                        base = 3.2 + (time - 600) * 0.006;
  return Math.max(1.0, base * (1 + getPerformanceAdjustment(ordersDelivered, failedOrders, streak)));
}

function getRecipeNamesForSpawn(time) {
  const phase = getPhaseKey(time);
  if (phase === 'tutorial') return RECIPE_NAMES_BY_PHASE.tutorial;
  if (phase === 'ramp')     return RECIPE_NAMES_BY_PHASE.ramp;
  if (phase === 'endurance') return null; // full table
  // automation: graduated pool
  const rel  = time - 150;
  const pool = ['Salad', 'Steak', 'Burger'];
  if (rel >= 35)  pool.push('Pizza');
  if (rel >= 95)  pool.push('Deluxe Burger');
  if (rel >= 170) pool.push('Feast Platter');
  if (rel >= 255) pool.push('Supreme Pizza');
  return pool;
}

function baseOrderSpawnInterval(time, rushActive, ordersDelivered, failedOrders, streak) {
  let normal;
  if (time < 60)       normal = 20 - smoothstep01(time, 0,   55) * 8;
  else if (time < 150) normal = 12 - smoothstep01(time, 60,  145) * 4;
  else if (time < 600) normal = 8  - smoothstep01(time, 150, 580) * 4;
  else                 normal = Math.max(2.5, 4 - (time - 600) * 0.003);
  if (rushActive) normal *= 0.70;
  const perf = getPerformanceAdjustment(ordersDelivered, failedOrders, streak);
  return Math.max(2.5, normal * (1 - perf * 0.35));
}

/**
 * orderTimeLimitForSpawn — draws from rng in the SAME ORDER as game.js.
 *
 * game.js line ~191:
 *   tutorial/ramp:  return base + Math.floor(Math.random() * 6)
 *   endurance/auto: return sec + Math.floor(Math.random() * 5)
 *   (with perf adjustment applied before the final jitter)
 *
 * RNG draws:
 *   tutorial: 1 draw  — jitter ∈ [0,5]
 *   ramp:     1 draw  — jitter ∈ [0,5]
 *   endurance/auto: 1 draw — extra ∈ [0,4]
 */
function orderTimeLimitForSpawn(time, ordersDelivered, failedOrders, streak, rng) {
  const phase = getPhaseKey(time);
  if (phase === 'tutorial') {
    return 52 + Math.floor(rng() * 6);
  }
  if (phase === 'ramp') {
    return 40 + Math.floor(rng() * 6);
  }
  let sec;
  if (phase === 'endurance') {
    sec = Math.max(14, 22 - (time - 600) * 0.012);
  } else {
    const u = smoothstep01(time, 150, 520);
    sec = Math.round(38 - u * 16);
  }
  sec = Math.max(14, sec);
  const perf = getPerformanceAdjustment(ordersDelivered, failedOrders, streak);
  sec = Math.round(sec * (1 - perf * 0.22));
  return sec + Math.floor(rng() * 5);
}

function isWalkable(map, x, y) {
  if (x < 0 || x >= MAP_WIDTH || y < 0 || y >= MAP_HEIGHT) return false;
  return map[y][x] === TILE_TYPES.FLOOR;
}

/** Manhattan adjacency check (same as game.js isChefAdjacentToStation). */
function isAdjacentToStation(chefX, chefY, stationX, stationY) {
  return Math.abs(chefX - stationX) + Math.abs(chefY - stationY) === 1;
}

/** Find first walkable tile adjacent to station (mirrors game.js findAdjacentWalkable). */
function findAdjacentWalkable(map, stationX, stationY) {
  const dirs = [[0, 1], [0, -1], [1, 0], [-1, 0]];
  for (const [dx, dy] of dirs) {
    const nx = stationX + dx;
    const ny = stationY + dy;
    if (isWalkable(map, nx, ny)) return { x: nx, y: ny };
  }
  return null;
}

/** Manhattan distance (approximation of path length for movement delay). */
function manhattanDist(x1, y1, x2, y2) {
  return Math.abs(x1 - x2) + Math.abs(y1 - y2);
}

/** All walkable (FLOOR) tiles orthogonally adjacent to a station — the tiles a
 *  chef can stand on to interact with it. */
function adjacentWalkableTiles(map, sx, sy) {
  const out = [];
  for (const [dx, dy] of [[0, 1], [0, -1], [1, 0], [-1, 0]]) {
    const nx = sx + dx, ny = sy + dy;
    if (isWalkable(map, nx, ny)) out.push({ x: nx, y: ny });
  }
  return out;
}

/**
 * Multi-source BFS over walkable tiles.  Returns a flat distance array indexed
 * by (y * MAP_WIDTH + x); -1 means unreachable.  Used by the travel-time check
 * to find the shortest walkable path (in tiles) between two stations' standing
 * tiles.  Cached per source in createSim, so this runs at most once per station.
 */
function bfsField(map, sources) {
  const dist = new Array(MAP_WIDTH * MAP_HEIGHT).fill(-1);
  const queue = [];
  for (const s of sources) {
    const idx = s.y * MAP_WIDTH + s.x;
    if (dist[idx] === -1) { dist[idx] = 0; queue.push(s); }
  }
  let head = 0;
  while (head < queue.length) {
    const cur = queue[head++];
    const cd  = dist[cur.y * MAP_WIDTH + cur.x];
    for (const [dx, dy] of [[0, 1], [0, -1], [1, 0], [-1, 0]]) {
      const nx = cur.x + dx, ny = cur.y + dy;
      if (!isWalkable(map, nx, ny)) continue;
      const ni = ny * MAP_WIDTH + nx;
      if (dist[ni] !== -1) continue;
      dist[ni] = cd + 1;
      queue.push({ x: nx, y: ny });
    }
  }
  return dist;
}

// ============================================================
// STATION LOOKUP
// ============================================================

function getStationById(stations, id) {
  for (const bin of stations.ingredientBins) if (bin.id === id) return { type: 'ingredientBin', station: bin };
  for (const s   of stations.stoves)         if (s.id   === id) return { type: 'stove',        station: s };
  for (const b   of stations.cuttingBoards)  if (b.id   === id) return { type: 'cuttingBoard', station: b };
  for (const p   of stations.platingAreas)   if (p.id   === id) return { type: 'platingArea',  station: p };
  for (const r   of stations.receptionStands) if (r.id  === id) return { type: 'receptionStand', station: r };
  for (const t   of stations.trashCans)      if (t.id   === id) return { type: 'trash',        station: t };
  for (const c   of stations.counters)       if (c.id   === id) return { type: 'counter',      station: c };
  return null;
}

// ============================================================
// INTERACTION LOGIC (mirrored from game.js interactWithStation)
// ============================================================

/**
 * Interact a chef with a station.
 * Returns a score delta (0 in most cases; positive on delivery).
 * Mutates chef + station state.
 * @param {object} chef
 * @param {object} stationInfo — { type, station }
 * @param {object} gs — mutable game-state reference for score/streak/etc.
 * @returns {number} score delta
 */
function interactWithStation(chef, stationInfo, gs) {
  const { type, station } = stationInfo;
  let scoreDelta = 0;

  switch (type) {
    case 'ingredientBin':
      if (!chef.holding) {
        chef.holding = { ingredient: station.ingredient, state: INGREDIENT_STATES.RAW };
      }
      break;

    case 'counter':
      station.items = station.items || [];
      if (chef.holding) {
        if (station.items.length >= 1) {
          const top = station.items[station.items.length - 1];
          if (top.type === 'plate' && chef.holding.type !== 'plate') {
            top.items.push(chef.holding);
            chef.holding = null;
            return 0;
          }
          if (chef.holding.type === 'plate' && top.type !== 'plate') {
            chef.holding.items.push(top);
            station.items.pop();
            return 0;
          }
          // Counter full / incompatible — no-op
          return 0;
        }
        station.items.push(chef.holding);
        chef.holding = null;
      } else if (station.items && station.items.length > 0) {
        chef.holding = station.items.pop();
      }
      break;

    case 'cuttingBoard':
      if (station.processing && station.processTime >= station.maxProcessTime) {
        if (!chef.holding) {
          chef.holding = station.processing;
          chef.holding.state = INGREDIENT_STATES.CHOPPED;
          station.processing = null;
          station.processTime = 0;
          station.busy = false;
        }
      } else if (chef.holding && chef.holding.state === INGREDIENT_STATES.RAW && !station.busy) {
        if (chef.holding.ingredient === 'dough') return 0; // no slicing bread
        station.processing = chef.holding;
        station.processTime = 0;
        station.busy = true;
        chef.holding = null;
        chef.busy = true;
        chef.actionTimer = station.maxProcessTime;
        chef.waitingAt = station;
      }
      break;

    case 'stove':
      if (station.cooking) {
        const cookProgress = station.cookTime / station.maxCookTime;
        if (cookProgress >= 0.8 && !chef.holding) {
          chef.holding = station.cooking;
          chef.holding.state = station.cookTime >= station.maxCookTime * 1.5
            ? INGREDIENT_STATES.BURNT
            : INGREDIENT_STATES.COOKED;
          station.cooking = null;
          station.cookTime = 0;
          station.busy = false;
        }
      } else if (chef.holding && !station.busy) {
        station.cooking = chef.holding;
        station.cookTime = 0;
        station.busy = true;
        chef.holding = null;
        chef.busy = true;
        chef.actionTimer = station.maxCookTime;
        chef.waitingAtStove = station;
      }
      break;

    case 'platingArea':
      if (chef.holding && chef.holding.type !== 'plate') {
        // Reject raw meat
        if (chef.holding.ingredient === 'meat' && chef.holding.state === INGREDIENT_STATES.RAW) return 0;
        station.items.push(chef.holding);
        chef.holding = null;
      } else if (chef.holding && chef.holding.type === 'plate') {
        station.items = station.items.concat(chef.holding.items || []);
        chef.holding = null;
      } else if (!chef.holding && station.items.length > 0) {
        chef.holding = { type: 'plate', items: [...station.items] };
        station.items = [];
      }
      break;

    case 'receptionStand':
      if (chef.holding && chef.holding.type === 'plate' && station.order) {
        const success = checkDelivery(chef.holding, station.order);
        if (success) {
          const timeBonus       = Math.floor(station.order.timeLeft * 2);
          const baseScore       = 100 * gs.difficulty;
          const streakMultiplier = 1 + Math.min(1.0, gs.streak * 0.05);
          const vipMultiplier    = station.order.vip ? 1.5 : 1;
          const totalScore = Math.floor((baseScore + timeBonus) * streakMultiplier * vipMultiplier);

          scoreDelta = totalScore;
          gs.streak += 1;
          gs.bestStreak = Math.max(gs.bestStreak, gs.streak);
          gs.ordersDelivered += 1;

          const deliveredOrder = station.order;
          station.order    = null;
          station.customer = { timeLeft: CUSTOMER_EAT_TIME };

          // Remove from orders array
          const idx = gs.orders.indexOf(deliveredOrder);
          if (idx > -1) gs.orders.splice(idx, 1);
        } else {
          // Wrong dish
          gs.streak = 0;
        }
        chef.holding = null;
      }
      break;

    case 'trash':
      if (chef.holding) chef.holding = null;
      break;
  }

  return scoreDelta;
}

function checkDelivery(plate, order) {
  const required  = order.recipe.components;
  const delivered = plate.items;
  if (delivered.length !== required.length) return false;
  for (const req of required) {
    const found = delivered.find(i => i.ingredient === req.ingredient && i.state === req.state);
    if (!found) return false;
  }
  return true;
}

// ============================================================
// UPCOMING ORDER QUEUE
// ============================================================

/**
 * Roll one upcoming-order spec.
 * RNG draw order (MUST match game.js):
 *   1. orderTimeLimitForSpawn draws (1 draw)
 *   2. recipe index (1 draw)
 *   3. vip flag     (1 draw)
 */
function rollUpcomingOrder(time, ordersDelivered, failedOrders, streak, rng) {
  const timeLimit = orderTimeLimitForSpawn(time, ordersDelivered, failedOrders, streak, rng);
  const names = getRecipeNamesForSpawn(time);
  const entries = names
    ? names.map(name => [name, RECIPES[name]])
    : Object.entries(RECIPES);
  const [dishName, recipe] = entries[Math.floor(rng() * entries.length)];
  const vipChance = Math.min(0.16, 0.07 + time / 9000);
  const vip = rng() < vipChance;
  return { dish: dishName, recipe, timeLimit, vip };
}

function refillUpcomingQueue(gs, rng) {
  while (gs.upcomingOrders.length < UPCOMING_QUEUE_SIZE) {
    gs.upcomingOrders.push(rollUpcomingOrder(
      gs.time, gs.ordersDelivered, gs.failedOrders, gs.streak, rng,
    ));
  }
}

// ============================================================
// SPAWN ORDER
// ============================================================

/**
 * Attempt to spawn one order.
 * Returns true if a spawn was resolved (order placed or no-stand-slot penalty).
 * RNG draw: 1 draw for stand selection (if stands available).
 */
function spawnOrder(gs, stations, rng) {
  const phase = getPhaseKey(gs.time);
  const availableStands = stations.receptionStands.filter(s => !s.order && !s.customer);

  if (availableStands.length === 0) {
    if (isHighPressurePhase(phase)) {
      gs.failedOrders++;
      gs.score -= NO_STAND_SLOT_PENALTY;
      gs.streak = 0;
      return true;
    }
    return false;
  }

  const stand = availableStands[Math.floor(rng() * availableStands.length)];

  refillUpcomingQueue(gs, rng);
  const spec = gs.upcomingOrders.shift();
  refillUpcomingQueue(gs, rng);

  const { dish: dishName, recipe, timeLimit, vip } = spec;
  const adjusted = vip ? Math.floor(timeLimit * 0.85) : timeLimit;

  const order = {
    id: gs.orderIdCounter++,
    dish: dishName,
    recipe,
    timeLeft: adjusted,
    maxTime:  adjusted,
    vip,
    standId: stand.id,
  };

  stand.order = order;
  gs.orders.push(order);
  return true;
}

// ============================================================
// UPDATE FUNCTIONS
// ============================================================

function updateStations(stations, dt) {
  for (const stove of stations.stoves) {
    if (stove.cooking) {
      stove.cookTime += dt;
      if (stove.cookTime >= stove.maxCookTime * 2) {
        stove.cooking.state = INGREDIENT_STATES.BURNT;
      }
    }
  }
  for (const board of stations.cuttingBoards) {
    if (board.processing && board.busy) {
      board.processTime += dt;
    }
  }
  for (const stand of stations.receptionStands) {
    if (stand.customer) {
      stand.customer.timeLeft -= dt;
      if (stand.customer.timeLeft <= 0) stand.customer = null;
    }
  }
}

function updateOrders(gs, stations, dt) {
  for (let i = gs.orders.length - 1; i >= 0; i--) {
    gs.orders[i].timeLeft -= dt;
    if (gs.orders[i].timeLeft <= 0) {
      const expired = gs.orders[i];
      gs.failedOrders++;
      gs.score -= ORDER_EXPIRED_PENALTY;
      gs.streak = 0;
      // Clear from stand
      const stand = stations.receptionStands.find(s => s.order === expired);
      if (stand) stand.order = null;
      gs.orders.splice(i, 1);
    }
  }
}

function updateChef(chef, dt) {
  // Action timer (chopping/cooking wait).
  // NOTE (B2a): In the "preserve gameplay, sim validates" architecture, the sim
  // no longer models movement or pendingCommand travel-time.  The client emits
  // an 'interact' event at the real tick when the interaction fires, so the sim
  // simply applies it immediately in applyInput().  The actionTimer / waitingAt
  // / waitingAtStove fields are still updated here because they affect WHAT
  // happens when the next 'interact' event arrives (e.g. whether a stove has
  // cooked long enough to be picked up).
  if (chef.actionTimer > 0) {
    chef.actionTimer -= dt;
    if (chef.actionTimer <= 0) {
      chef.busy = false;
      if (chef.waitingAt) {
        if (chef.waitingAt.processing) {
          chef.holding = chef.waitingAt.processing;
          chef.holding.state = INGREDIENT_STATES.CHOPPED;
          chef.waitingAt.processing = null;
          chef.waitingAt.busy = false;
        }
        chef.waitingAt = null;
      }
      if (chef.waitingAtStove) {
        if (chef.waitingAtStove.cooking) {
          chef.holding = chef.waitingAtStove.cooking;
          chef.holding.state = INGREDIENT_STATES.COOKED;
          chef.waitingAtStove.cooking = null;
          chef.waitingAtStove.busy = false;
        }
        chef.waitingAtStove = null;
      }
    }
  }
  // Movement / pendingCommand: no longer modelled in B2a.
  // The client records the real interaction tick; travel time is client-side only.
}

// ============================================================
// RUSH LOGIC
// ============================================================

function updateRush(gs, stations, rng, dt) {
  const phase = getPhaseKey(gs.time);

  if (gs.rush.active) {
    gs.rush.timeLeft -= dt;
    if (gs.rush.timeLeft <= 0) {
      gs.rush.active = false;
      // RNG draw: new cooldown
      gs.rush.cooldown = isHighPressurePhase(phase)
        ? 15 + rng() * 5
        : 30 + rng() * 25;
    }
  } else {
    gs.rush.cooldown -= dt;
    if (gs.rush.cooldown <= 0) {
      gs.rush.active = true;
      // RNG draw: rush duration
      gs.rush.timeLeft = isHighPressurePhase(phase)
        ? 12 + rng() * 8
        : 10 + rng() * 6;

      // Burst: spawn up to 3 orders
      const freeStands = stations.receptionStands.filter(s => !s.order && !s.customer).length;
      const burstTarget = Math.min(3, freeStands);
      for (let i = 0; i < burstTarget; i++) {
        if (!spawnOrder(gs, stations, rng)) break;
      }
    }
  }
}

// ============================================================
// PUBLIC API
// ============================================================

/**
 * getCanonicalCounterIds() — returns the ordered list of counter ids as built
 * by the map layout (y-major, then x scan).  Used by sim/inputlog.js so that
 * the server's station-id table matches the client's without duplicating the
 * map-iteration logic.
 */
export function getCanonicalCounterIds() {
  const map = buildMap();
  const ids = [];
  for (let y = 0; y < MAP_HEIGHT; y++) {
    for (let x = 0; x < MAP_WIDTH; x++) {
      if (map[y][x] === TILE_TYPES.COUNTER) {
        ids.push(`counter_${ids.length}`);
      }
    }
  }
  return ids;
}

/**
 * defaultConfig() — returns a copy of all gameplay constants.
 * Pass this (or a modified copy) to createSim() for easy overriding in tests.
 */
export function defaultConfig() {
  return {
    tickHz:          TICK_HZ,
    maxFailedOrders: MAX_FAILED_ORDERS,
    // NOTE (B2a): moveDelay kept for backward compat (tests may reference it) but
    // is no longer used by the sim — movement timing is client-side only.
    moveDelay:       MOVE_DELAY,
  };
}

/**
 * createSim({ seed, config }) — create a self-contained simulation instance.
 *
 * The returned object exposes:
 *   step(inputsThisTick)  — advance one fixed timestep
 *   getState()            — current (read-only snapshot) of simulation state
 *   summary()             — { score, delivered, bestStreak, time_secs, gameOver }
 *
 * @param {{ seed: number, config?: object }} opts
 */
export function createSim({ seed, config = {} }) {
  const cfg = Object.assign(defaultConfig(), config);
  const rng = makeRng(seed);

  const map      = buildMap();
  const stations = buildStations(map);

  // Mutable game state
  const gs = {
    // Time tracking
    tick:     0,
    time:     0,   // seconds: tick * DT
    // Score
    score:    0,
    difficulty: 1.0,
    // Streak
    streak:   0,
    bestStreak: 0,
    // Failures
    failedOrders:    0,
    maxFailedOrders: cfg.maxFailedOrders,
    ordersDelivered: 0,
    // Orders
    orders:        [],
    orderIdCounter: 0,
    upcomingOrders: [],
    orderSpawnDebt: 0,
    // Rush
    rush: { active: false, timeLeft: 0, cooldown: 20 },
    // Game-over flag
    gameOver: false,
    // Anti-teleport travel validation (only populated when cfg.checkTravel).
    travelViolations:     0,
    firstTravelViolation: null,
    // Per-hop travel telemetry (only populated when cfg.travelTelemetry).
    travelTelemetry:      [],
  };

  // Chefs array — _gsRef is a back-pointer so interactWithStation can mutate gs.
  // NOTE (B2a): pendingCommand removed — movement is no longer modelled in the sim.
  const chefs = CHEF_START_POSITIONS.map((pos, i) => ({
    id:           i,
    x:            pos.x,
    y:            pos.y,
    holding:      null,
    busy:         false,
    actionTimer:  0,
    waitingAt:    null,
    waitingAtStove: null,
    _gsRef:       gs,  // back-reference; not serialised in summary/snapshot
    // Anti-teleport tracking (only used when cfg.checkTravel).  Starts null so
    // the first interaction is measured from the chef's spawn tile (x, y).
    _lastStationId:    null,
    _lastInteractTick: 0,
  }));

  // Pre-fill the upcoming queue (mirrors startGame calling refillUpcomingQueue())
  refillUpcomingQueue(gs, rng);

  // ---- Travel-time validation (only active when cfg.checkTravel) ----
  // BFS distance fields are cached per source key; station positions are static
  // so each field is computed at most once per run.
  const travelFieldCache = new Map();

  function travelFieldFor(chef) {
    const key = chef._lastStationId === null ? `start_${chef.id}` : chef._lastStationId;
    let field = travelFieldCache.get(key);
    if (field) return field;

    let sources;
    if (chef._lastStationId === null) {
      sources = [{ x: chef.x, y: chef.y }];  // spawn tile (a walkable FLOOR tile)
    } else {
      const info = getStationById(stations, chef._lastStationId);
      sources = info ? adjacentWalkableTiles(map, info.station.x, info.station.y) : [];
    }
    field = bfsField(map, sources);
    travelFieldCache.set(key, field);
    return field;
  }

  // Records a violation (does NOT drop the interaction — score is still computed
  // so the caller can report both the score and the travel violation).
  function checkTravel(chef, station, tick) {
    if (chef._lastStationId === station.id) return; // same station: no travel

    const field   = travelFieldFor(chef);
    const targets = adjacentWalkableTiles(map, station.x, station.y);
    let minTiles = Infinity;
    for (const t of targets) {
      const d = field[t.y * MAP_WIDTH + t.x];
      if (d >= 0 && d < minTiles) minTiles = d;
    }

    const elapsed = tick - chef._lastInteractTick;
    let need = null;
    let detail = null;
    if (minTiles === Infinity) {
      detail = { reason: 'unreachable' };
    } else {
      need = Math.max(0, Math.floor(minTiles * TRAVEL_TICKS_PER_TILE) - TRAVEL_SLACK_TICKS);
      if (elapsed < need) detail = { reason: 'too_fast', tiles: minTiles, need, elapsed };
    }

    // Per-hop telemetry (only when requested): records EVERY different-station
    // hop, not just violations, so downstream behavior heuristics can reason
    // about how tightly a run hugs the travel lower bound.
    if (cfg.travelTelemetry) {
      gs.travelTelemetry.push({
        chefId:    chef.id,
        tick,
        stationId: station.id,
        tiles:     minTiles === Infinity ? null : minTiles,
        need,
        elapsed,
      });
    }

    if (detail) {
      gs.travelViolations++;
      if (!gs.firstTravelViolation) {
        gs.firstTravelViolation = { chefId: chef.id, tick, stationId: station.id, ...detail };
      }
    }
  }

  // ---- Internal helpers ----

  /**
   * applyInput(ev) — B2a: apply interaction IMMEDIATELY at its stamped tick.
   *
   * For the "preserve gameplay, sim validates" architecture the client's real
   * timing is authoritative.  The client emits one 'interact' event per actual
   * interactWithStation() call (when the A*-walking chef physically arrives at
   * the station).  We therefore apply the interaction unconditionally at the
   * stamped tick; no internal movement simulation is needed.
   *
   * 'command' is accepted as a legacy alias.
   */
  function applyInput(ev) {
    if (ev.type === 'boost') return; // ignored (fidelity note C)
    if (ev.type !== 'interact' && ev.type !== 'command') return;

    const chef = chefs[ev.chefId];
    if (!chef) return;

    const stationInfo = getStationById(stations, ev.stationId);
    if (!stationInfo) return;

    // Anti-teleport: enforce a conservative minimum travel time between this
    // chef's consecutive interactions.  Counted, not dropped (see checkTravel).
    // Also runs when only telemetry is requested (cfg.travelTelemetry), since the
    // per-hop need/elapsed it records is what the behavior heuristics consume.
    const trackTravel = cfg.checkTravel || cfg.travelTelemetry;
    if (trackTravel) {
      checkTravel(chef, stationInfo.station, ev.tick);
    }

    // Apply the interaction immediately at the stamped tick.
    const delta = interactWithStation(chef, stationInfo, gs);
    if (delta !== 0) gs.score += delta;

    // Record where this chef now stands, for the next travel check.
    if (trackTravel) {
      chef._lastStationId    = ev.stationId;
      chef._lastInteractTick = ev.tick;
    }
  }

  // ---- Exposed API ----

  return {
    /**
     * step(inputsThisTick) — advance one DT tick.
     * @param {Array} inputsThisTick — array of input events for this tick (may be empty)
     */
    step(inputsThisTick = []) {
      if (gs.gameOver) return;

      // Apply inputs
      for (const ev of inputsThisTick) {
        applyInput(ev);
      }

      gs.tick++;
      gs.time = gs.tick * DT;

      // Recompute difficulty (uses current time + perf metrics)
      gs.difficulty = computeDifficulty(gs.time, gs.ordersDelivered, gs.failedOrders, gs.streak);

      // Endurance tick (score passively increases after 600s)
      if (gs.time >= 600) {
        gs.score += DT * gs.difficulty;
      }

      // Rush logic
      updateRush(gs, stations, rng, DT);

      // Order spawning
      refillUpcomingQueue(gs, rng);
      const spawnEvery = baseOrderSpawnInterval(
        gs.time, gs.rush.active, gs.ordersDelivered, gs.failedOrders, gs.streak,
      );
      gs.orderSpawnDebt += DT / spawnEvery;
      let spawnsThisFrame = 0;
      const maxSpawnsPerFrame = 2;
      while (gs.orderSpawnDebt >= 1 && spawnsThisFrame < maxSpawnsPerFrame) {
        if (!spawnOrder(gs, stations, rng)) break;
        gs.orderSpawnDebt -= 1;
        spawnsThisFrame++;
      }

      // Initial early spawns — deterministically replicate game.js startGame()'s
      // setTimeout(spawnOrder, 1000) and setTimeout(spawnOrder, 3000).  Without
      // these the early-game spawn interval (~20 s) leaves the kitchen EMPTY for
      // the first ~19 s, which looks like "no orders / broken".  These are extra
      // spawns on top of the debt cadence (they do not consume orderSpawnDebt),
      // fired at fixed ticks so client and server replay identically.
      if (gs.tick === 60 || gs.tick === 180) {
        spawnOrder(gs, stations, rng);
      }

      // Update chefs
      for (const chef of chefs) {
        updateChef(chef, DT);
      }

      // Update stations
      updateStations(stations, DT);

      // Update orders (decay + expiry)
      updateOrders(gs, stations, DT);

      // Check game-over
      if (gs.failedOrders >= gs.maxFailedOrders) {
        gs.gameOver = true;
      }
    },

    /** Read-only snapshot of current state (for rendering or inspection). */
    getState() {
      return {
        tick:           gs.tick,
        time:           gs.time,
        score:          gs.score,
        difficulty:     gs.difficulty,
        streak:         gs.streak,
        bestStreak:     gs.bestStreak,
        failedOrders:   gs.failedOrders,
        ordersDelivered:gs.ordersDelivered,
        gameOver:       gs.gameOver,
        rush:           { active: gs.rush.active, timeLeft: gs.rush.timeLeft, cooldown: gs.rush.cooldown },
        orders: gs.orders.map(o => ({
          id: o.id, dish: o.dish, timeLeft: o.timeLeft, maxTime: o.maxTime,
          vip: o.vip, standId: o.standId,
        })),
        chefs: chefs.map(c => ({
          id: c.id, x: c.x, y: c.y,
          holding: c.holding ? { ...c.holding } : null,
          busy: c.busy,
        })),
      };
    },

    /** Final summary — what the server stores. */
    summary() {
      return {
        score:       Math.floor(gs.score),
        delivered:   gs.ordersDelivered,
        bestStreak:  gs.bestStreak,
        time_secs:   gs.time,
        gameOver:    gs.gameOver,
        // Anti-teleport: 0 / null unless cfg.checkTravel was set.
        travelViolations:     gs.travelViolations,
        firstTravelViolation: gs.firstTravelViolation,
        // Per-hop telemetry: [] unless cfg.travelTelemetry was set.
        travelTelemetry:      gs.travelTelemetry,
      };
    },
  };
}

/**
 * simulate({ seed, config, inputs, maxTicks }) — run a full input log and return summary.
 *
 * This is the function the server calls for replay validation.
 *
 * @param {{ seed: number, config?: object, inputs: Array, maxTicks?: number }} opts
 * @returns {{ score, delivered, bestStreak, time_secs, gameOver }}
 */
export function simulate({ seed, config = {}, inputs = [], maxTicks }) {
  const sim = createSim({ seed, config });

  // Build a per-tick index for fast lookup
  const inputsByTick = new Map();
  for (const ev of inputs) {
    if (!inputsByTick.has(ev.tick)) inputsByTick.set(ev.tick, []);
    inputsByTick.get(ev.tick).push(ev);
  }

  // Determine run length
  const lastInputTick = inputs.length > 0 ? Math.max(...inputs.map(e => e.tick)) : 0;
  const runTicks = maxTicks !== undefined
    ? maxTicks
    : Math.max(lastInputTick + TICK_HZ * 10, TICK_HZ * 30); // run at least 30s past last input

  for (let t = 0; t < runTicks; t++) {
    const evs = inputsByTick.get(t) || [];
    sim.step(evs);
    if (sim.getState().gameOver) break;
  }

  return sim.summary();
}
