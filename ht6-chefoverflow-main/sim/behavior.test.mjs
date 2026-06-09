/**
 * sim/behavior.test.mjs — Human-realism heuristics (sim/behavior.js).
 *
 * Run with:  node sim/behavior.test.mjs
 *
 * Verifies analyzeBehavior():
 *   1. Honest logs (the gameplay/travel-test shapes, and a varied single-chef
 *      run) produce NO flags — guards against false-rejecting real players.
 *   2. A machine-generated bot log (sim/botlog.mjs) IS flagged.
 *
 * DO NOT import DOM / Date / Math.random in this file.
 */

import assert from 'node:assert/strict';
import { simulate, defaultConfig, TICK_HZ } from './core.js';
import { seedFromRunId } from './prng.js';
import { analyzeBehavior } from './behavior.js';
import { generateBotLog } from './botlog.mjs';

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); console.log(`PASS  ${name}`); passed++; }
  catch (err) { console.error(`FAIL  ${name}\n      ${err.message}`); failed++; }
}

// Run a log through the sim with telemetry on, then analyze it the way the
// server does (replaySummary.travelTelemetry + decoded inputs).
function analyzeLog(inputs, { seed = seedFromRunId('behavior-test'), maxTicks = TICK_HZ * 200 } = {}) {
  const replay = simulate({
    seed,
    config: { ...defaultConfig(), checkTravel: true, travelTelemetry: true },
    inputs,
    maxTicks,
  });
  return analyzeBehavior({ telemetry: replay.travelTelemetry || [], inputs });
}

// ============================================================
// 1. Honest logs are NOT flagged
// ============================================================

test('honest single full-delivery log has no flags', () => {
  // bin -> cook -> plate -> deliver, generously paced (like gameplay.test).
  const honest = [
    { tick: 120, type: 'interact', chefId: 0, stationId: 'bin_3'     },
    { tick: 320, type: 'interact', chefId: 0, stationId: 'stove_0'   },
    { tick: 640, type: 'interact', chefId: 0, stationId: 'plating_0' },
    { tick: 760, type: 'interact', chefId: 0, stationId: 'plating_0' },
    { tick: 980, type: 'interact', chefId: 0, stationId: 'reception_0' },
  ];
  const { flags } = analyzeLog(honest);
  assert.deepEqual(flags, [], `expected no flags, got ${flags}`);
});

test('honest varied single-chef run (many events) has no flags', () => {
  // 40 hops alternating stations with VARIED, generous gaps (A*-arrival-like).
  // A human produces a near-continuous spread of gaps and works one chef at a
  // time, so none of H1/H2/H3 should fire.
  const stations = ['bin_3', 'plating_0', 'bin_1', 'cutting_0', 'stove_0'];
  const inputs = [];
  let tick = 100;
  for (let i = 0; i < 40; i++) {
    inputs.push({ tick, type: 'interact', chefId: 0, stationId: stations[i % stations.length] });
    tick += 150 + ((i * 53) % 150); // varied gap in [150, 299]
  }
  const { flags, stats } = analyzeLog(inputs, { maxTicks: tick + TICK_HZ * 5 });
  assert.deepEqual(flags, [], `expected no flags, got ${flags} (stats ${JSON.stringify(stats)})`);
  // sanity: enough gaps to have exercised the quantization check
  assert.ok(stats.gapCount >= 30, 'should have judged the gap distribution');
  assert.ok(stats.gapTopShare < 0.7, 'human gaps should not collapse onto few values');
});

// ============================================================
// 2. A machine-generated bot log IS flagged
// ============================================================

test('offline bot log is flagged as machine-generated', () => {
  const { replay, log } = generateBotLog('b7f3a1c2-dead-beef-cafe-000000000001');
  const { flags, stats } = analyzeBehavior({ telemetry: replay.travelTelemetry || [], inputs: log });
  assert.ok(flags.length >= 1, `expected the bot log to be flagged, got none (stats ${JSON.stringify(stats)})`);
  // It drives all 5 chefs in lockstep on fixed gaps, so we expect at least one
  // of the cadence/concurrency fingerprints.
  assert.ok(
    flags.includes('cadence_quantized') || flags.includes('superhuman_concurrency'),
    `expected cadence/concurrency flag, got ${flags}`,
  );
  // The bot must still pass the travel check (it's travel-legal) — the whole
  // point is that the prior layers DON'T catch it.
  assert.equal(replay.travelViolations, 0, 'bot log is travel-legal (prior layers miss it)');
});

test('bot log flagging is deterministic across seeds', () => {
  for (const id of ['aaaa1111-2222-3333-4444-555566667777', 'deadbeef-0000-1111-2222-333344445555']) {
    const { replay, log } = generateBotLog(id);
    const { flags } = analyzeBehavior({ telemetry: replay.travelTelemetry || [], inputs: log });
    assert.ok(flags.length >= 1, `expected flags for ${id}, got none`);
  }
});

console.log('');
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
else console.log('All tests passed.');
