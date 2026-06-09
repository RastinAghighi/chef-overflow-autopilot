/**
 * sim/behavior.js — Human-realism heuristics over a replayed input log.
 *
 * DO NOT import DOM / Date / performance / Math.random / window / document.
 *
 * ============================================================
 * WHY THIS EXISTS
 * ============================================================
 * The server replay (api/submit-score.js) proves a log is internally consistent
 * and physically plausible, but it CANNOT tell a human-played log from one a
 * solver generated offline from the run_id-derived seed (the sim is fully
 * deterministic, so an attacker knows the future and can synthesize a valid
 * log).  These heuristics look for the fingerprints such a solver leaves:
 *
 *   1. travel_hug            — a high fraction of hops sit right on the
 *                              anti-teleport lower bound (a bot paces just fast
 *                              enough to pass; humans sit well above it).
 *   2. cadence_quantized     — inter-interaction gaps collapse onto a few EXACT
 *                              tick values (a bot uses fixed gaps; a human's
 *                              gaps are A*-arrival + reaction driven and spread
 *                              across a near-continuous range).
 *   3. superhuman_concurrency — many chefs run perfectly-timed pipelines in
 *                              parallel with no idle (a human time-shares
 *                              attention across chefs).
 *
 * This is a SIGNAL, not proof.  It is intentionally conservative and ships in
 * log-only mode first (see BEHAVIOR_CHECK in api/submit-score.js); thresholds
 * are tuned from real-run telemetry before any enforcement.
 *
 * Pure + deterministic: same inputs → same flags.
 */

'use strict';

// ============================================================
// THRESHOLDS  (conservative; honest play sits well clear of each)
// ============================================================

// H1 — travel-bound hugging.
const HUG_EPS_TICKS = 2;     // "hugging" = elapsed within this many ticks of need
const HUG_MIN_HOPS  = 20;    // need enough reachable hops to be meaningful
const HUG_FRACTION  = 0.60;  // > this fraction hugging the bound ⇒ machine-like

// H2 — inter-event gap quantization (fixed cadence).
const QUANT_MIN_GAPS  = 30;   // need enough gaps to judge a distribution
const QUANT_TOP_K     = 3;    // consider the K most frequent EXACT gap values
const QUANT_FRACTION  = 0.70; // > this share on those K values ⇒ fixed-cadence bot

// H3 — superhuman multi-chef concurrency.
const CONC_WINDOW      = 30;       // sample stride in ticks (0.5 s)
const CONC_ENGAGE      = 6 * 60;   // a chef is "engaged" if its surrounding gap ≤ 6 s
const CONC_MIN_CHEFS   = 4;        // this many concurrently engaged = beyond human
const CONC_FRACTION    = 0.50;     // sustained for > this fraction of sampled time
const CONC_MIN_SAMPLES = 20;       // need a run long enough to sample

/**
 * analyzeBehavior({ telemetry, inputs }) → { flags: string[], stats }
 *
 * @param {object}   opts
 * @param {Array}    opts.telemetry — per-hop records from simulate() when
 *   cfg.travelTelemetry was set: { chefId, tick, stationId, tiles, need, elapsed }.
 *   `need` is null for unreachable hops (ignored by H1).
 * @param {Array}    opts.inputs — decoded events { tick, type, chefId, stationId }.
 * @returns {{ flags: string[], stats: object }}
 */
export function analyzeBehavior({ telemetry = [], inputs = [] } = {}) {
  const flags = [];
  const stats = {};

  // ---- H1: travel-bound hugging ----
  const reachable = telemetry.filter(h => typeof h.need === 'number');
  const hugging = reachable.filter(h => (h.elapsed - h.need) <= HUG_EPS_TICKS);
  stats.travelHops = reachable.length;
  stats.travelHugFraction = reachable.length ? hugging.length / reachable.length : 0;
  if (reachable.length >= HUG_MIN_HOPS && stats.travelHugFraction >= HUG_FRACTION) {
    flags.push('travel_hug');
  }

  // Group interaction ticks per chef (sorted ascending).
  const byChef = new Map();
  for (const ev of inputs) {
    if (ev.type !== 'interact' && ev.type !== 'command') continue;
    if (!byChef.has(ev.chefId)) byChef.set(ev.chefId, []);
    byChef.get(ev.chefId).push(ev.tick);
  }
  for (const arr of byChef.values()) arr.sort((a, b) => a - b);

  // ---- H2: inter-event gap quantization ----
  const gaps = [];
  for (const ticks of byChef.values()) {
    for (let i = 1; i < ticks.length; i++) gaps.push(ticks[i] - ticks[i - 1]);
  }
  stats.gapCount = gaps.length;
  if (gaps.length >= QUANT_MIN_GAPS) {
    const freq = new Map();
    for (const g of gaps) freq.set(g, (freq.get(g) || 0) + 1);
    const top = [...freq.values()].sort((a, b) => b - a).slice(0, QUANT_TOP_K);
    const topShare = top.reduce((s, c) => s + c, 0) / gaps.length;
    stats.gapDistinct = freq.size;
    stats.gapTopShare = topShare;
    if (topShare >= QUANT_FRACTION) flags.push('cadence_quantized');
  }

  // ---- H3: superhuman multi-chef concurrency ----
  const allTicks = inputs.map(e => e.tick).filter(t => Number.isFinite(t));
  stats.chefsUsed = byChef.size;
  if (allTicks.length > 0 && byChef.size >= CONC_MIN_CHEFS) {
    const minT = Math.min(...allTicks);
    const maxT = Math.max(...allTicks);
    // Per chef, the intervals during which it is actively working (consecutive
    // events close enough together to count as one continuous engagement).
    const engagedIntervals = [];
    for (const ticks of byChef.values()) {
      const ivs = [];
      for (let i = 1; i < ticks.length; i++) {
        if (ticks[i] - ticks[i - 1] <= CONC_ENGAGE) ivs.push([ticks[i - 1], ticks[i]]);
      }
      engagedIntervals.push(ivs);
    }
    let samples = 0, hot = 0;
    for (let t = minT; t <= maxT; t += CONC_WINDOW) {
      samples++;
      let n = 0;
      for (const ivs of engagedIntervals) {
        if (ivs.some(([a, b]) => a <= t && t <= b)) n++;
      }
      if (n >= CONC_MIN_CHEFS) hot++;
    }
    stats.concurrencySamples = samples;
    stats.concurrencyHotFraction = samples ? hot / samples : 0;
    if (samples >= CONC_MIN_SAMPLES && stats.concurrencyHotFraction >= CONC_FRACTION) {
      flags.push('superhuman_concurrency');
    }
  }

  return { flags, stats };
}
