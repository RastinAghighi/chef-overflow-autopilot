/**
 * sim/travel.test.mjs — Anti-teleport travel-time validation (sim/core.js).
 *
 * Run with:  node --test sim/travel.test.mjs   (or: node sim/travel.test.mjs)
 *
 * Verifies that cfg.checkTravel:
 *   1. Is OFF by default — does not perturb scoring or flag anything.
 *   2. Flags a "teleport" log (a chef hitting two far-apart stations on
 *      consecutive ticks) as physically impossible.
 *   3. Does NOT flag honestly-paced movement between the same stations.
 *   4. Never flags repeated interactions with the SAME station.
 *   5. Leaves the recomputed score identical to a no-check run (honest log),
 *      so it can layer on top of replay without changing legitimate results.
 *
 * DO NOT import DOM / Date / Math.random in this file.
 */

import assert from 'node:assert/strict';
import { simulate, defaultConfig, TICK_HZ } from './core.js';
import { seedFromRunId } from './prng.js';

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

const SEED     = seedFromRunId('travel-test');
const MAX_TICKS = TICK_HZ * 30;

// bin_3 (1,9) is deep in the kitchen; reception_4 (17,11) is across the divider
// (reachable only through the x=13 pass-through window). The shortest walkable
// path between their standing tiles is ~20 tiles, so the minimum legal tick gap
// is well over 80 ticks.
const FAR_A = 'bin_3';
const FAR_B = 'reception_4';

function run(inputs, checkTravel) {
  return simulate({
    seed: SEED,
    config: { ...defaultConfig(), checkTravel },
    inputs,
    maxTicks: MAX_TICKS,
  });
}

// ============================================================
// 1. Off by default
// ============================================================

test('checkTravel defaults off: teleport log is NOT flagged', () => {
  const teleport = [
    { tick: 300, type: 'interact', chefId: 0, stationId: FAR_A },
    { tick: 301, type: 'interact', chefId: 0, stationId: FAR_B },
  ];
  const r = run(teleport, false);
  assert.equal(r.travelViolations, 0, 'no violations when check disabled');
  assert.equal(r.firstTravelViolation, null, 'no violation detail when disabled');
});

// ============================================================
// 2. Teleport is flagged when enabled
// ============================================================

test('teleport across the map on consecutive ticks is flagged', () => {
  const teleport = [
    { tick: 300, type: 'interact', chefId: 0, stationId: FAR_A },
    { tick: 301, type: 'interact', chefId: 0, stationId: FAR_B },
  ];
  const r = run(teleport, true);
  assert.ok(r.travelViolations >= 1, 'at least one violation flagged');
  assert.equal(r.firstTravelViolation.reason, 'too_fast', 'reason is too_fast');
  assert.equal(r.firstTravelViolation.chefId, 0);
  assert.equal(r.firstTravelViolation.stationId, FAR_B);
  assert.ok(r.firstTravelViolation.need > r.firstTravelViolation.elapsed,
    'required travel ticks exceed the elapsed ticks');
});

// ============================================================
// 3. Honest pacing between the same stations is NOT flagged
// ============================================================

test('honestly-paced movement between the same stations is allowed', () => {
  // 300 ticks (5 s) between interactions — far more than any path needs.
  const honest = [
    { tick: 300, type: 'interact', chefId: 0, stationId: FAR_A },
    { tick: 600, type: 'interact', chefId: 0, stationId: FAR_B },
  ];
  const r = run(honest, true);
  assert.equal(r.travelViolations, 0, 'generously-paced honest run has no violations');
});

// ============================================================
// 4. Same-station spam is never a travel violation
// ============================================================

test('repeated interactions with the SAME station are never flagged', () => {
  const sameStation = [
    { tick: 300, type: 'interact', chefId: 0, stationId: 'plating_0' },
    { tick: 301, type: 'interact', chefId: 0, stationId: 'plating_0' },
    { tick: 302, type: 'interact', chefId: 0, stationId: 'plating_0' },
  ];
  const r = run(sameStation, true);
  assert.equal(r.travelViolations, 0, 'same-station interactions require no travel');
});

// ============================================================
// 5. Enabling the check does not change an honest run's score
// ============================================================

test('checkTravel does not perturb scoring on an honest log', () => {
  const honest = [
    { tick: 120, type: 'interact', chefId: 0, stationId: 'bin_3'     }, // pick up meat
    { tick: 200, type: 'interact', chefId: 0, stationId: 'stove_0'   }, // cook
    { tick: 560, type: 'interact', chefId: 0, stationId: 'stove_0'   }, // collect cooked
    { tick: 700, type: 'interact', chefId: 0, stationId: 'plating_0' }, // plate
    { tick: 760, type: 'interact', chefId: 0, stationId: 'plating_0' }, // pick up plate
  ];
  const withCheck    = run(honest, true);
  const withoutCheck = run(honest, false);
  assert.equal(withCheck.score,     withoutCheck.score,     'score identical with/without check');
  assert.equal(withCheck.delivered, withoutCheck.delivered, 'delivered identical');
  assert.equal(withCheck.bestStreak, withoutCheck.bestStreak, 'streak identical');
  assert.equal(withCheck.travelViolations, 0, 'honest log has no travel violations');
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
