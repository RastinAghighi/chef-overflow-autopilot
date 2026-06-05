/* End-to-end validation against the REAL reference/core.js.
 *
 * core.js imports makeRng from './prng.js', which is not shipped in reference/.
 * We must not touch reference/, so we read core.js as text, swap the prng import
 * for an inline deterministic LCG, and import the result from a data: URL. This
 * runs the genuine authoritative createSim()/step() with zero edits to disk.
 *
 * We then drive full Steak and Salad pipelines (bin -> cook/chop -> plate ->
 * deliver) purely through interact events and confirm the score core.js awards
 * equals the documented formula. The captured (difficulty,timeLeft,streak,vip)
 * are emitted as JSON so xcheck_e2e.py can confirm sim/constants.py agrees too.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const CORE = join(HERE, '..', 'reference', 'core.js');

let src = readFileSync(CORE, 'utf8');
// Replace the prng import with a deterministic inline LCG. Values need not match
// the real prng — we read whatever core.js spawns and adapt to it.
src = src.replace(
  /import\s*\{\s*makeRng\s*\}\s*from\s*['"]\.\/prng\.js['"];?/,
  'function makeRng(seed){let s=(seed>>>0)||1;return function(){s=(Math.imul(s,1664525)+1013904223)>>>0;return s/4294967296;};}'
);
const mod = await import('data:text/javascript,' + encodeURIComponent(src));
const { createSim } = mod;

const DT = 1 / 60;

function stepUntilOrder(sim, dish, cap = 8000) {
  for (let i = 0; i < cap; i++) {
    const st = sim.getState();
    const o = st.orders.find(o => o.dish === dish);
    if (o) return o;
    sim.step([]);
  }
  return null;
}
function stepUntilHolding(sim, chefId, cap = 800) {
  for (let i = 0; i < cap; i++) {
    sim.step([]);
    if (sim.getState().chefs[chefId].holding) return true;
  }
  return false;
}
function interact(sim, chefId, stationId) {
  sim.step([{ type: 'interact', chefId, stationId }]);
}

// Drive one order to delivery; return the captured scoring inputs + deltas.
function driveSteak(seed) {
  const sim = createSim({ seed });
  const order = stepUntilOrder(sim, 'Steak');
  if (!order) return { skip: 'no Steak spawned' };
  const standId = order.standId;

  interact(sim, 0, 'bin_3');                 // raw meat
  if (sim.getState().chefs[0].holding?.ingredient !== 'meat') return { fail: 'no meat held' };
  interact(sim, 0, 'stove_0');               // start cooking
  if (!stepUntilHolding(sim, 0)) return { fail: 'cook never finished' };
  const cooked = sim.getState().chefs[0].holding;
  if (cooked.state !== 'cooked') return { fail: `expected cooked, got ${cooked.state}` };
  interact(sim, 0, 'plating_0');             // deposit
  interact(sim, 0, 'plating_0');             // lift plate

  // capture state core.js will use when the deliver interact fires at start of step
  const pre = sim.getState();
  const o = pre.orders.find(x => x.id === order.id);
  if (!o) return { skip: 'steak expired before delivery' };
  const cap = { difficulty: pre.difficulty, timeLeft: o.timeLeft, streak: pre.streak, vip: o.vip };
  const scoreBefore = pre.score, failBefore = pre.failedOrders, delivBefore = pre.ordersDelivered;

  interact(sim, 0, standId);                  // deliver
  const post = sim.getState();
  if (post.failedOrders !== failBefore) return { skip: 'an order expired on the deliver tick' };
  if (post.ordersDelivered !== delivBefore + 1) return { fail: 'delivery did not register' };
  return { dish: 'Steak', cap, actualDelta: post.score - scoreBefore };
}

function driveSalad(seed) {
  const sim = createSim({ seed });
  const order = stepUntilOrder(sim, 'Salad');
  if (!order) return { skip: 'no Salad spawned' };
  const standId = order.standId;

  for (const [bin] of [['bin_1', 'lettuce'], ['bin_0', 'tomato']]) {
    interact(sim, 0, bin);                    // raw veg
    interact(sim, 0, 'cutting_0');            // start chop
    if (!stepUntilHolding(sim, 0)) return { fail: 'chop never finished' };
    if (sim.getState().chefs[0].holding.state !== 'chopped') return { fail: 'not chopped' };
    interact(sim, 0, 'plating_0');            // deposit
  }
  interact(sim, 0, 'plating_0');              // lift plate

  const pre = sim.getState();
  const o = pre.orders.find(x => x.id === order.id);
  if (!o) return { skip: 'salad expired before delivery' };
  const cap = { difficulty: pre.difficulty, timeLeft: o.timeLeft, streak: pre.streak, vip: o.vip };
  const scoreBefore = pre.score, failBefore = pre.failedOrders, delivBefore = pre.ordersDelivered;

  interact(sim, 0, standId);
  const post = sim.getState();
  if (post.failedOrders !== failBefore) return { skip: 'an order expired on the deliver tick' };
  if (post.ordersDelivered !== delivBefore + 1) return { fail: 'delivery did not register' };
  return { dish: 'Salad', cap, actualDelta: post.score - scoreBefore };
}

const results = [];
for (let seed = 1; seed <= 12; seed++) {
  results.push(driveSteak(seed));
  results.push(driveSalad(seed + 1000));
}
process.stdout.write(JSON.stringify(results));
