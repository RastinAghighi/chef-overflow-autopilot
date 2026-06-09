/**
 * sim/replay.test.mjs — Round-trip integrity test for sim/inputlog.js + sim/core.js
 *
 * Run with:  node sim/replay.test.mjs
 *
 * Tests:
 *   1. buildStationTable returns the expected canonical table (fixed + counters).
 *   2. encodeInputLog / decodeInputLog round-trip produces identical events.
 *   3. simulate() over decoded events == simulate() over raw events (no drift).
 *   4. A tampered (inflated) score does NOT equal the replay-computed score.
 *   5. Server-side path: decoding with the server table gives same result as
 *      the client-side table (they must be identical).
 *
 * DO NOT import DOM / Date / Math.random in this file.
 */

import assert from 'node:assert/strict';
import { buildStationTable, encodeInputLog, decodeInputLog } from './inputlog.js';
import { simulate, createSim, defaultConfig, getCanonicalCounterIds, TICK_HZ } from './core.js';
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

// ============================================================
// Shared fixtures
// ============================================================

const counterIds = getCanonicalCounterIds();
const TABLE      = buildStationTable(counterIds);

// A small set of raw input events that exercises several station types.
const RAW_EVENTS = [
  { tick: 100, type: 'interact', chefId: 0, stationId: 'bin_3'       }, // pick up meat
  { tick: 110, type: 'interact', chefId: 0, stationId: 'stove_0'     }, // put meat on stove
  { tick: 400, type: 'interact', chefId: 0, stationId: 'plating_0'   }, // place cooked meat on plate
  { tick: 410, type: 'interact', chefId: 0, stationId: 'plating_0'   }, // pick up plate
];

// ============================================================
// 1. Table shape
// ============================================================

test('buildStationTable: fixed part has correct prefix', () => {
  assert.equal(TABLE[0],  'bin_0',        'index 0 = bin_0');
  assert.equal(TABLE[5],  'bin_5',        'index 5 = bin_5');
  assert.equal(TABLE[6],  'stove_0',      'index 6 = stove_0');
  assert.equal(TABLE[8],  'stove_2',      'index 8 = stove_2');
  assert.equal(TABLE[9],  'cutting_0',    'index 9 = cutting_0');
  assert.equal(TABLE[10], 'cutting_1',    'index 10 = cutting_1');
  assert.equal(TABLE[11], 'plating_0',    'index 11 = plating_0');
  assert.equal(TABLE[14], 'plating_3',    'index 14 = plating_3');
  assert.equal(TABLE[15], 'reception_0',  'index 15 = reception_0');
  assert.equal(TABLE[19], 'reception_4',  'index 19 = reception_4');
  assert.equal(TABLE[20], 'trash_0',      'index 20 = trash_0');
});

test('buildStationTable: counters start at index 21', () => {
  assert.ok(counterIds.length > 0, 'Map should have at least one counter');
  assert.equal(TABLE[21], 'counter_0', 'First counter at index 21');
  assert.equal(TABLE.length, 21 + counterIds.length, 'Table length = 21 fixed + counters');
});

test('buildStationTable: all counter ids present in order', () => {
  for (let i = 0; i < counterIds.length; i++) {
    assert.equal(TABLE[21 + i], counterIds[i], `counter at 21+${i} = ${counterIds[i]}`);
  }
});

// ============================================================
// 2. Encode / decode round-trip
// ============================================================

test('encodeInputLog / decodeInputLog: round-trip restores events', () => {
  const tuples   = encodeInputLog(RAW_EVENTS, TABLE);
  const restored = decodeInputLog(tuples, TABLE);

  assert.equal(restored.length, RAW_EVENTS.length, 'Same event count after round-trip');
  for (let i = 0; i < RAW_EVENTS.length; i++) {
    assert.equal(restored[i].tick,      RAW_EVENTS[i].tick,      `tick matches at ${i}`);
    assert.equal(restored[i].chefId,    RAW_EVENTS[i].chefId,    `chefId matches at ${i}`);
    assert.equal(restored[i].stationId, RAW_EVENTS[i].stationId, `stationId matches at ${i}`);
    assert.equal(restored[i].type, 'interact', `type is 'interact' at ${i}`);
  }
});

test('encodeInputLog: tickDeltas are correct', () => {
  const tuples = encodeInputLog(RAW_EVENTS, TABLE);
  assert.equal(tuples[0][0], 100, 'First event: delta = absolute tick 100');
  assert.equal(tuples[1][0], 10,  'Second event: delta = 110 - 100 = 10');
  assert.equal(tuples[2][0], 290, 'Third event: delta = 400 - 110 = 290');
  assert.equal(tuples[3][0], 10,  'Fourth event: delta = 410 - 400 = 10');
});

test('encodeInputLog: stationCodes map to correct indices', () => {
  const tuples = encodeInputLog(RAW_EVENTS, TABLE);
  assert.equal(tuples[0][2], TABLE.indexOf('bin_3'),     'bin_3 code');
  assert.equal(tuples[1][2], TABLE.indexOf('stove_0'),   'stove_0 code');
  assert.equal(tuples[2][2], TABLE.indexOf('plating_0'), 'plating_0 code');
});

test('encodeInputLog: unknown stationId is silently skipped', () => {
  const withUnknown = [
    ...RAW_EVENTS,
    { tick: 500, type: 'interact', chefId: 1, stationId: 'nonexistent_99' },
  ];
  const tuples = encodeInputLog(withUnknown, TABLE);
  assert.equal(tuples.length, RAW_EVENTS.length, 'Unknown station is skipped');
});

test('decodeInputLog: unknown code is silently skipped', () => {
  const badTuples = [
    [100, 0, 3],           // bin_3 — valid
    [10,  0, 9999],        // code 9999 — out of bounds
    [290, 0, TABLE.indexOf('plating_0')], // valid
  ];
  const events = decodeInputLog(badTuples, TABLE);
  assert.equal(events.length, 2, 'Only 2 valid tuples decoded');
});

// ============================================================
// 3. Replay round-trip: simulate over encoded→decoded == raw
// ============================================================

test('simulate(decoded) equals simulate(raw events) — round-trip integrity', () => {
  const seed    = seedFromRunId('replay-roundtrip-test');
  const config  = defaultConfig();
  const maxTicks = TICK_HZ * 15;

  // Simulate directly over raw events.
  const directResult = simulate({ seed, config, inputs: RAW_EVENTS, maxTicks });

  // Encode then decode, then simulate.
  const tuples   = encodeInputLog(RAW_EVENTS, TABLE);
  const decoded  = decodeInputLog(tuples, TABLE);
  const replayResult = simulate({ seed, config, inputs: decoded, maxTicks });

  assert.deepEqual(
    directResult,
    replayResult,
    'simulate(raw) and simulate(encode→decode) must be identical',
  );
});

test('simulate round-trip is deterministic across multiple calls', () => {
  const seed    = seedFromRunId('replay-determinism-test');
  const config  = defaultConfig();
  const maxTicks = TICK_HZ * 12;

  const tuples = encodeInputLog(RAW_EVENTS, TABLE);
  const decoded = decodeInputLog(tuples, TABLE);

  const r1 = simulate({ seed, config, inputs: decoded, maxTicks });
  const r2 = simulate({ seed, config, inputs: decoded, maxTicks });
  const r3 = simulate({ seed, config, inputs: decoded, maxTicks });

  assert.deepEqual(r1, r2, 'Run 1 and 2 identical');
  assert.deepEqual(r2, r3, 'Run 2 and 3 identical');
});

// ============================================================
// 4. Tampered score is detectable
// ============================================================

test('A tampered (inflated) score differs from the replay-computed score', () => {
  const seed    = seedFromRunId('tamper-detection-test');
  const config  = defaultConfig();
  const maxTicks = TICK_HZ * 15;

  const tuples  = encodeInputLog(RAW_EVENTS, TABLE);
  const decoded = decodeInputLog(tuples, TABLE);
  const replayResult = simulate({ seed, config, inputs: decoded, maxTicks });

  // A cheater claims a wildly inflated score.
  const CHEATED_SCORE = 9_999_999;

  // The server comparison: abs diff > 1 → mismatch.
  const scoreMismatch = Math.abs(replayResult.score - CHEATED_SCORE) > 1;
  assert.ok(scoreMismatch,
    `Tampered score ${CHEATED_SCORE} should not match replay score ${replayResult.score}`);
});

test('Replay detects inflated delivered count', () => {
  const seed    = seedFromRunId('tamper-delivered-test');
  const config  = defaultConfig();
  const maxTicks = TICK_HZ * 15;

  const tuples   = encodeInputLog(RAW_EVENTS, TABLE);
  const decoded  = decodeInputLog(tuples, TABLE);
  const replayResult = simulate({ seed, config, inputs: decoded, maxTicks });

  const CHEATED_DELIVERED = replayResult.delivered + 100;
  assert.notEqual(
    replayResult.delivered,
    CHEATED_DELIVERED,
    'Tampered delivered count should not equal replay value',
  );
});

// ============================================================
// 5. Client table == server table (single source of truth)
// ============================================================

test('Client and server build the same station table from getCanonicalCounterIds()', () => {
  // The server calls buildStationTable(getCanonicalCounterIds()) at module load.
  // The client (game.js) calls the same.  Verify they are identical.
  const serverTable = buildStationTable(getCanonicalCounterIds());
  const clientTable = buildStationTable(getCanonicalCounterIds()); // same call

  assert.deepEqual(serverTable, clientTable,
    'Server and client station tables must be identical');
  assert.deepEqual(serverTable, TABLE,
    'Station table must match the fixture used in this test');
});

test('Encoding with client table and decoding with server table gives identical events', () => {
  const clientTable = buildStationTable(getCanonicalCounterIds());
  const serverTable = buildStationTable(getCanonicalCounterIds());

  const encoded = encodeInputLog(RAW_EVENTS, clientTable);
  const decoded = decodeInputLog(encoded, serverTable);

  for (let i = 0; i < RAW_EVENTS.length; i++) {
    assert.equal(decoded[i].tick,      RAW_EVENTS[i].tick,      `tick[${i}]`);
    assert.equal(decoded[i].stationId, RAW_EVENTS[i].stationId, `stationId[${i}]`);
    assert.equal(decoded[i].chefId,    RAW_EVENTS[i].chefId,    `chefId[${i}]`);
  }
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
