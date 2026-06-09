/**
 * sim/prng.js — Seeded PRNG for Chef Overflow deterministic simulation.
 *
 * DO NOT import DOM / Date / Math.random in this file.
 *
 * DETERMINISM RULES:
 *  - Pure functions only; no module-level mutable state.
 *  - makeRng() returns a closure; all state is local to that closure.
 *  - seedFromRunId() uses synchronous pure-JS FNV-1a so the result is
 *    IDENTICAL in Node and in the browser with NO async, NO node:crypto,
 *    NO crypto.subtle. If you later want the sha256 variant the plan mentions,
 *    derive it outside this module and pass the resulting uint32 to makeRng().
 *
 * ALGORITHM CHOICES:
 *  - PRNG: mulberry32 (32-bit state, period 2^32, excellent statistical quality
 *    for a game simulation, fits in a single 32-bit seed).
 *    Reference: https://gist.github.com/tommyettinger/46a874533244883189143505d203312c
 *
 *  - seedFromRunId: FNV-1a (32-bit) over the UTF-16 code units of the runId
 *    string. FNV-1a is synchronous, dependency-free, and produces a stable
 *    uint32 for any given string. The output is the same in every JS runtime
 *    because we use only bitwise ops on numbers within the 32-bit range.
 *    FNV offset basis: 2166136261 (0x811c9dc5)
 *    FNV prime: 16777619 (0x01000193)
 *
 *    NOTE on seed-shopping: a cheater who calls start-run repeatedly can observe
 *    different seeds and pick a "favorable" one. This is an acceptable residual
 *    risk documented in the anti-cheat plan — they still must produce a valid
 *    full replay to pass server validation.
 */

'use strict';

/**
 * makeRng(seed) → () => float
 *
 * Returns a function that, on each call, produces a float in [0, 1).
 * The same seed always yields the same sequence.
 *
 * @param {number} seed - unsigned 32-bit integer
 * @returns {() => number}
 */
export function makeRng(seed) {
  // mulberry32 — state is a single 32-bit unsigned integer.
  let s = seed >>> 0; // force uint32
  return function () {
    s = (s + 0x6d2b79f5) >>> 0;
    let z = s;
    z = Math.imul(z ^ (z >>> 15), z | 1);
    z ^= z + Math.imul(z ^ (z >>> 7), z | 61);
    z = ((z ^ (z >>> 14)) >>> 0);
    return z / 4294967296; // z / 2^32  →  [0, 1)
  };
}

/**
 * seedFromRunId(runId) → uint32
 *
 * Deterministic, synchronous, identical in Node and browsers.
 * Uses FNV-1a (32-bit) over the UTF-16 code units of the string.
 *
 * @param {string} runId
 * @returns {number} unsigned 32-bit integer seed
 */
export function seedFromRunId(runId) {
  const FNV_OFFSET = 2166136261; // 0x811c9dc5
  const FNV_PRIME  = 16777619;   // 0x01000193
  let hash = FNV_OFFSET >>> 0;
  for (let i = 0; i < runId.length; i++) {
    hash ^= runId.charCodeAt(i);
    // Math.imul keeps the 32-bit multiply exact without BigInt.
    hash = Math.imul(hash, FNV_PRIME) >>> 0;
  }
  return hash;
}
