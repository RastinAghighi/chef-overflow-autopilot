/* Independent JS oracle for the deterministic core.
 *
 * The functions below are copied VERBATIM from reference/core.js (the
 * authoritative deterministic simulation). They are module-private there, so we
 * re-declare them here and run them under real Node/V8 arithmetic — making this
 * a bit-exact oracle for the browser. tools/xcheck_core.py runs this and diffs
 * the output against sim/constants.py.  Do NOT "fix" these to match Python; if
 * they disagree, constants.py is the thing that is wrong.
 */
'use strict';

// ---- copied verbatim from reference/core.js -------------------------------
const RECIPES = {
  'Salad':        { difficulty: 1 },
  'Burger':       { difficulty: 2 },
  'Steak':        { difficulty: 1 },
  'Pizza':        { difficulty: 3 },
  'Deluxe Burger':{ difficulty: 3 },
  'Feast Platter':{ difficulty: 4 },
  'Supreme Pizza':{ difficulty: 4 },
};
const RECIPE_NAMES_BY_PHASE = {
  tutorial: ['Salad', 'Steak'],
  ramp:     ['Salad', 'Steak', 'Burger'],
};

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
// orderTimeLimitForSpawn, but returning the DETERMINISTIC core + the rng range
// (the live fn adds Math.floor(rng()*range); we compare the part that is testable).
function orderTimeLimitCore(time, ordersDelivered, failedOrders, streak) {
  const phase = getPhaseKey(time);
  if (phase === 'tutorial') return [52, 6];
  if (phase === 'ramp')     return [40, 6];
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
  return [sec, 5];
}
function vipProbability(time) {
  return Math.min(0.16, 0.07 + time / 9000);
}
function deliveryScore(difficulty, timeLeft, streak, vip) {
  const timeBonus       = Math.floor(timeLeft * 2);
  const baseScore       = 100 * difficulty;
  const streakMultiplier = 1 + Math.min(1.0, streak * 0.05);
  const vipMultiplier    = vip ? 1.5 : 1;
  return Math.floor((baseScore + timeBonus) * streakMultiplier * vipMultiplier);
}
// ---- end verbatim ----------------------------------------------------------

// Build the same sweeps the Python side uses.
const times = [];
for (const t of [0,1,5,27.5,30,55,57.9,58,59.999,60,94.9,95,148,149.999,150,
                 184.9,185,244.9,245,319.9,320,335,372.5,404.9,405,519,520,
                 579.9,580,595,599.999,600,667,900,1000,1199.9,1200]) times.push(t);
for (let t = 0; t <= 1200; t += 7) times.push(t);

const perfCombos = [[0,0,0],[5,1,3],[10,5,0],[0,3,0],[50,0,20],[100,2,10],
                    [3,0,7],[20,10,0],[1,1,1],[7,2,4],[200,0,50],[0,10,0]];

const out = { difficulty: [], spawn: [], timelimit: [], pool: [], phase: [], vip: [], score: [] };

for (const t of times) {
  out.phase.push([t, getPhaseKey(t)]);
  out.vip.push([t, vipProbability(t)]);
  const names = getRecipeNamesForSpawn(t);
  out.pool.push([t, names === null ? null : names]);
  for (const [d, f, s] of perfCombos) {
    out.difficulty.push([t, d, f, s, computeDifficulty(t, d, f, s)]);
    out.spawn.push([t, 0, d, f, s, baseOrderSpawnInterval(t, false, d, f, s)]);
    out.spawn.push([t, 1, d, f, s, baseOrderSpawnInterval(t, true,  d, f, s)]);
    out.timelimit.push([t, d, f, s, orderTimeLimitCore(t, d, f, s)]);
  }
}

const diffs = [1.0, 1.6, 2.4, 3.2, 5.0, 4.16];
const tls   = [0, 0.4, 0.5, 1, 10, 37, 50, 0.25];
const strks = [0, 1, 3, 10, 19, 20, 25, 50];
for (const d of diffs) for (const tl of tls) for (const s of strks) for (const v of [false, true])
  out.score.push([d, tl, s, v, deliveryScore(d, tl, s, v)]);

process.stdout.write(JSON.stringify(out));
