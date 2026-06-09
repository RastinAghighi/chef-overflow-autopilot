/**
 * sim/inputlog.js — Shared compact input-log encoder/decoder.
 *
 * DO NOT import DOM / Date / Math.random / window / document.
 *
 * This module is the SINGLE SOURCE OF TRUTH for:
 *   1. The canonical station-id table (fixed part + counter part).
 *   2. encodeInputLog / decodeInputLog wire format.
 *
 * Both game.js (browser) and api/submit-score.js (server) import from here so
 * they are guaranteed to use identical encoding logic and an identical table.
 *
 * ============================================================
 * STATION_ID_TABLE ORDERING
 * ============================================================
 *  0– 5: bin_0 … bin_5
 *  6– 8: stove_0 … stove_2
 *  9–10: cutting_0 … cutting_1
 * 11–14: plating_0 … plating_3
 * 15–19: reception_0 … reception_4
 *     20: trash_0
 * 21– N: counter_0 … counter_N  (N determined by the map layout)
 *
 * The counter ids are map-layout-derived.  sim/core.js builds them with the
 * same map iteration (y-major, then x) so the ordering is identical to what
 * game.js produces in initInputLogTable().  The server calls
 * buildStationTable(counterIds) where counterIds comes from
 * getCanonicalCounterIds() exported by sim/core.js.
 *
 * ============================================================
 * WIRE FORMAT
 * ============================================================
 *  Each event → [tickDelta, chefId, stationCode]
 *  tickDelta  — delta from previous event tick (first event: absolute tick)
 *  chefId     — 0–4
 *  stationCode — integer index into the station-id table
 *
 *  Decoding: tick = running sum of tickDeltas; stationId = table[stationCode];
 *  type = 'interact'.
 *
 *  'boost' events are NOT included (server-irrelevant).
 */

'use strict';

// ============================================================
// FIXED (map-independent) station ids, in canonical order.
// ============================================================

const FIXED_STATION_IDS = [
  // bins 0–5
  'bin_0', 'bin_1', 'bin_2', 'bin_3', 'bin_4', 'bin_5',
  // stoves 6–8
  'stove_0', 'stove_1', 'stove_2',
  // cutting boards 9–10
  'cutting_0', 'cutting_1',
  // plating areas 11–14
  'plating_0', 'plating_1', 'plating_2', 'plating_3',
  // reception stands 15–19
  'reception_0', 'reception_1', 'reception_2', 'reception_3', 'reception_4',
  // trash 20
  'trash_0',
];

/**
 * buildStationTable(counterIds = []) → string[]
 *
 * Returns the full station-id table: fixed stations followed by counters.
 *
 * @param {string[]} counterIds — ordered list of counter ids from the map layout
 *                                (e.g. ['counter_0', 'counter_1', …]).
 *                                Obtain via getCanonicalCounterIds() from sim/core.js.
 * @returns {string[]}
 */
export function buildStationTable(counterIds = []) {
  return FIXED_STATION_IDS.concat(counterIds);
}

/**
 * encodeInputLog(log, table) → Array<[number, number, number]>
 *
 * Encode an array of raw input-log events to compact 3-element tuples.
 *
 * @param {Array<{tick: number, chefId: number, stationId: string}>} log
 * @param {string[]} table — station-id table (from buildStationTable)
 * @returns {Array<[number, number, number]>}
 */
export function encodeInputLog(log, table) {
  // Build reverse lookup map once.
  const codeMap = Object.create(null);
  for (let i = 0; i < table.length; i++) {
    codeMap[table[i]] = i;
  }

  const out = [];
  let prevTick = 0;
  for (const ev of log) {
    const code = codeMap[ev.stationId];
    if (code === undefined) continue; // unknown station — skip
    out.push([ev.tick - prevTick, ev.chefId, code]);
    prevTick = ev.tick;
  }
  return out;
}

/**
 * decodeInputLog(tuples, table) → Array<{tick, type, chefId, stationId}>
 *
 * Decode compact tuples back to event objects.
 *
 * @param {Array<[number, number, number]>} tuples
 * @param {string[]} table — station-id table (from buildStationTable)
 * @returns {Array<{tick: number, type: 'interact', chefId: number, stationId: string}>}
 */
export function decodeInputLog(tuples, table) {
  const events = [];
  let tick = 0;
  for (const [delta, chefId, code] of tuples) {
    tick += delta;
    const stationId = table[code];
    if (stationId === undefined) continue; // unknown code — skip
    events.push({ tick, type: 'interact', chefId, stationId });
  }
  return events;
}
