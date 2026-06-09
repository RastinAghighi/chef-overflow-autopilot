/**
 * sim/core.test.mjs — Node determinism test for sim/core.js
 *
 * Run with:  node sim/core.test.mjs
 *
 * Exits 0 on success, non-zero on failure.
 * No test framework required.
 *
 * DO NOT import DOM / Date / Math.random in this file.
 */

import assert from 'node:assert/strict';
import { simulate, createSim, defaultConfig, TICK_HZ } from './core.js';
import { seedFromRunId, makeRng } from './prng.js';

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    console.log(`PASS  ${name}`);
    passed++;
  } catch (err) {
    console.error(`FAIL  ${name}`);
    console.error(`      ${err.message}`);
    failed++;
  }
}

// ============================================================
// 1. PRNG tests
// ============================================================

test('makeRng produces values in [0, 1)', () => {
  const rng = makeRng(42);
  for (let i = 0; i < 1000; i++) {
    const v = rng();
    assert.ok(v >= 0 && v < 1, `value ${v} out of range at iteration ${i}`);
  }
});

test('makeRng is deterministic — same seed same sequence', () => {
  const rng1 = makeRng(999);
  const rng2 = makeRng(999);
  for (let i = 0; i < 200; i++) {
    assert.equal(rng1(), rng2());
  }
});

test('makeRng produces different sequences for different seeds', () => {
  const rng1 = makeRng(1);
  const rng2 = makeRng(2);
  let diff = false;
  for (let i = 0; i < 50; i++) {
    if (rng1() !== rng2()) { diff = true; break; }
  }
  assert.ok(diff, 'Seeds 1 and 2 should produce different sequences');
});

test('seedFromRunId is deterministic', () => {
  const id = 'test-run-id-abc123';
  assert.equal(seedFromRunId(id), seedFromRunId(id));
});

test('seedFromRunId produces different values for different ids', () => {
  assert.notEqual(seedFromRunId('run-abc'), seedFromRunId('run-xyz'));
});

test('seedFromRunId returns uint32 (integer in [0, 2^32))', () => {
  const s = seedFromRunId('some-run-id');
  assert.ok(Number.isInteger(s) && s >= 0 && s < 4294967296, `got ${s}`);
});

// ============================================================
// 2. Determinism test — same seed yields identical summaries
// ============================================================

test('simulate() is deterministic — same seed/inputs = same summary', () => {
  const seed   = seedFromRunId('determinism-test-run');
  const config = defaultConfig();
  const maxTicks = TICK_HZ * 10;

  const a = simulate({ seed, config, inputs: [], maxTicks });
  const b = simulate({ seed, config, inputs: [], maxTicks });

  assert.deepEqual(a, b, 'Two runs with identical seed must yield identical summaries');
});

// ============================================================
// 3. Different seeds → different order stream (sanity)
// ============================================================

test('Different seeds produce different order spawn streams', () => {
  // Step both sims for 30s and collect all order ids/dishes spawned.
  const cfg   = defaultConfig();
  const ticks = TICK_HZ * 30;

  const sim1 = createSim({ seed: seedFromRunId('seed-A') });
  const sim2 = createSim({ seed: seedFromRunId('seed-B') });

  const orders1 = [];
  const orders2 = [];

  let prev1 = 0, prev2 = 0;
  for (let t = 0; t < ticks; t++) {
    sim1.step([]);
    sim2.step([]);
    const s1 = sim1.getState();
    const s2 = sim2.getState();
    if (s1.orders.length !== prev1) {
      orders1.push(...s1.orders.map(o => o.dish));
      prev1 = s1.orders.length;
    }
    if (s2.orders.length !== prev2) {
      orders2.push(...s2.orders.map(o => o.dish));
      prev2 = s2.orders.length;
    }
  }

  // The two seeds should produce at least one different dish name or stand
  // (they may both spawn orders but differ in which recipe/time-limit was rolled).
  // As a reliable proxy: compare the time at which each spawned their first order.
  const finalA = sim1.summary();
  const finalB = sim2.summary();

  // Different seeds must produce at least one difference across score, time, failedOrders.
  const identical =
    finalA.score === finalB.score &&
    finalA.delivered === finalB.delivered &&
    finalA.time_secs === finalB.time_secs &&
    finalA.bestStreak === finalB.bestStreak;

  // If somehow both ended identically (extremely unlikely), check order lists differ.
  if (identical) {
    assert.notDeepEqual(orders1, orders2, 'Different seeds should produce different order sequences');
  }
  // else: they already differ, assertion satisfied implicitly.
});

// ============================================================
// 4. Handcrafted input log — deliver at least one Steak (B2a: immediate semantics)
//
// B2a change: events are now type:'interact' and are applied IMMEDIATELY at their
// stamped tick.  There is no internal movement simulation — each event fires its
// interaction at the exact tick it is emitted (as the client would after A* walk).
//
// The test discovers the first spawned order (shadow pass) and crafts an event
// sequence that:
//   a) picks up the right raw ingredient
//   b) puts it on the processing station (stove/cutting board)
//   c) [for stove] lets actionTimer expire (auto-pickup) — still works because
//      the sim ticks forward and the timer fires automatically
//   d) picks up the cooked/chopped item
//   e) places it on a plating area
//   f) picks up the plate
//   g) delivers to the correct reception stand
//
// Timing is now ARBITRARY — we just need enough ticks between each 'interact'
// event for station timers to elapse.  We use generous spacing.
// ============================================================

test('Handcrafted input log: deliver one Steak order → score > 0 and delivered >= 1', () => {
  const seed   = seedFromRunId('delivery-test-run');
  const config = defaultConfig();

  // ---- SHADOW PASS: find which stand gets the first order and its dish ----
  const shadow = createSim({ seed, config });
  let firstStandId    = null;
  let firstOrderDish  = null;
  let spawnTick       = null;

  for (let t = 0; t < TICK_HZ * 60; t++) {
    shadow.step([]);
    const st = shadow.getState();
    if (firstStandId === null && st.orders.length > 0) {
      firstStandId   = st.orders[0].standId;
      firstOrderDish = st.orders[0].dish;
      spawnTick      = t;
      break;
    }
  }

  assert.ok(firstStandId !== null, 'An order should spawn within 60 seconds');

  // Build input log using immediate-semantics 'interact' events.
  // Spacing between events is generous to ensure station timers (stove=4s=240t,
  // cutting=2s=120t) have enough ticks to elapse before the pickup interact fires.
  const inputs = [];
  const BASE = spawnTick + 30; // a few ticks after first order spawns

  if (firstOrderDish === 'Steak') {
    // 1. Pick up raw meat from bin_3
    const tBin    = BASE;
    // 2. Place meat on stove_0.  Fires interactWithStation → stove accepts it,
    //    sets actionTimer=4s=240 ticks.
    const tStove  = tBin + 10;
    // 3. After 4s stove timer fires auto-pickup inside updateChef, chef holds cooked meat.
    //    We fire a plating interact 270 ticks after the stove interact (safe margin).
    const tPlate1 = tStove + 270;
    // 4. Pick up plate from plating area.
    const tPlate2 = tPlate1 + 10;
    // 5. Deliver to stand.
    const tDeliver = tPlate2 + 10;

    inputs.push({ tick: tBin,     type: 'interact', chefId: 0, stationId: 'bin_3' });
    inputs.push({ tick: tStove,   type: 'interact', chefId: 0, stationId: 'stove_0' });
    // No interact needed between stove and plating — auto-pickup via actionTimer.
    inputs.push({ tick: tPlate1,  type: 'interact', chefId: 0, stationId: 'plating_0' });
    inputs.push({ tick: tPlate2,  type: 'interact', chefId: 0, stationId: 'plating_0' });
    inputs.push({ tick: tDeliver, type: 'interact', chefId: 0, stationId: firstStandId });

  } else if (firstOrderDish === 'Salad') {
    // Salad: chopped lettuce + chopped tomato → plating → deliver
    // 1. Pick lettuce from bin_1
    const tBin1       = BASE;
    // 2. Place on cutting_0 → actionTimer=2s=120 ticks
    const tChop1      = tBin1 + 10;
    // 3. After 2s auto-pickup of chopped lettuce.  Then place on plating.
    const tPlate1     = tChop1 + 140;   // 140 > 120 → cutting timer expired
    // 4. Pick tomato from bin_0
    const tBin0       = tPlate1 + 10;
    // 5. Place on cutting_0 → actionTimer=2s
    const tChop2      = tBin0 + 10;
    // 6. After 2s auto-pickup of chopped tomato.  Place on plating.
    const tPlate2     = tChop2 + 140;
    // 7. Pick up plate (both ingredients now on plating)
    const tPickup     = tPlate2 + 10;
    // 8. Deliver
    const tDeliver    = tPickup + 10;

    inputs.push({ tick: tBin1,    type: 'interact', chefId: 0, stationId: 'bin_1' });
    inputs.push({ tick: tChop1,   type: 'interact', chefId: 0, stationId: 'cutting_0' });
    inputs.push({ tick: tPlate1,  type: 'interact', chefId: 0, stationId: 'plating_0' });
    inputs.push({ tick: tBin0,    type: 'interact', chefId: 0, stationId: 'bin_0' });
    inputs.push({ tick: tChop2,   type: 'interact', chefId: 0, stationId: 'cutting_0' });
    inputs.push({ tick: tPlate2,  type: 'interact', chefId: 0, stationId: 'plating_0' });
    inputs.push({ tick: tPickup,  type: 'interact', chefId: 0, stationId: 'plating_0' });
    inputs.push({ tick: tDeliver, type: 'interact', chefId: 0, stationId: firstStandId });

  } else {
    // Other dishes — just verify no crash.
    const r = simulate({ seed, config, inputs: [], maxTicks: TICK_HZ * 40 });
    assert.ok(r.score !== undefined, 'simulate() must return a score property');
    return;
  }

  const maxTicks = inputs[inputs.length - 1].tick + TICK_HZ * 20;
  const result   = simulate({ seed, config, inputs, maxTicks });

  assert.ok(result.delivered >= 1,
    `Expected at least 1 delivery for ${firstOrderDish} (stand=${firstStandId}), ` +
    `got delivered=${result.delivered}, score=${result.score}`);
  assert.ok(result.score > 0,
    `Expected score > 0 for ${firstOrderDish}, got ${result.score}`);
});

// ============================================================
// 5. Repeated simulate() calls — byte-identical
// ============================================================

test('simulate() with inputs is byte-identical on repeated calls', () => {
  const seed  = seedFromRunId('repeat-exact-test');
  const cfg   = defaultConfig();
  // B2a: use type:'interact' — interactions fire immediately at stamped tick.
  // Get meat → place on stove (auto-pickup fires after 240 ticks) → place on plating → pick up plate.
  const inputs = [
    { tick: 100, type: 'interact', chefId: 0, stationId: 'bin_3' },
    { tick: 110, type: 'interact', chefId: 0, stationId: 'stove_0' },
    // auto-pickup fires at ~tick 110+240=350; interact with plating after that
    { tick: 400, type: 'interact', chefId: 0, stationId: 'plating_0' },
    { tick: 410, type: 'interact', chefId: 0, stationId: 'plating_0' },
  ];
  const maxTicks = TICK_HZ * 15;

  const r1 = simulate({ seed, config: cfg, inputs, maxTicks });
  const r2 = simulate({ seed, config: cfg, inputs, maxTicks });
  const r3 = simulate({ seed, config: cfg, inputs, maxTicks });

  assert.deepEqual(r1, r2, 'Run 1 and 2 must be identical');
  assert.deepEqual(r2, r3, 'Run 2 and 3 must be identical');
});

// ============================================================
// 6. defaultConfig() returns expected shape
// ============================================================

test('defaultConfig() returns expected shape', () => {
  const cfg = defaultConfig();
  assert.ok(typeof cfg.tickHz === 'number' && cfg.tickHz === 60);
  assert.ok(typeof cfg.maxFailedOrders === 'number' && cfg.maxFailedOrders === 3);
  assert.ok(typeof cfg.moveDelay === 'number');
});

// ============================================================
// 7. createSim step count matches time
// ============================================================

test('createSim: tick count and time match after N steps', () => {
  const sim = createSim({ seed: 1 });
  const N = TICK_HZ * 5; // 5 seconds
  for (let i = 0; i < N; i++) sim.step([]);
  const st = sim.getState();
  assert.equal(st.tick, N);
  // time should be N * DT = 5.0 (within floating-point tolerance)
  assert.ok(Math.abs(st.time - 5.0) < 1e-9, `Expected time ~5.0, got ${st.time}`);
});

// ============================================================
// 8. Game-over stops progressing
// ============================================================

test('Game stops when failedOrders reaches maxFailedOrders', () => {
  const result = simulate({
    seed:      seedFromRunId('game-over-test'),
    config:    defaultConfig(),
    inputs:    [],
    maxTicks:  TICK_HZ * 300,
  });
  assert.ok(result.gameOver, 'Expected gameOver=true');
  assert.ok(result.time_secs < 300, `Expected game to end before 5 min, got ${result.time_secs}s`);
});

// ============================================================
// Results
// ============================================================

console.log('');
console.log(`Results: ${passed} passed, ${failed} failed`);

if (failed > 0) {
  process.exit(1);
} else {
  console.log('All tests passed.');
  process.exit(0);
}
