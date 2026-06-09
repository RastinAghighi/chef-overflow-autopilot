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
function holding(sim, chefId) {
  return sim.getState().chefs.find(c => c.id === chefId)?.holding || null;
}
function itemKey(item) {
  if (!item) return 'null';
  if (item.type === 'plate') {
    return 'plate:' + (item.items || []).map(itemKey).join('|');
  }
  return `${item.ingredient}:${item.state}`;
}
function sameItem(a, b) {
  return itemKey(a) === itemKey(b);
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

function driveCounterHandoff(seed) {
  const sim = createSim({ seed });
  const tomato = { ingredient: 'tomato', state: 'raw' };
  const lettuce = { ingredient: 'lettuce', state: 'raw' };
  const onion = { ingredient: 'onion', state: 'raw' };

  interact(sim, 0, 'bin_0');
  const fetchedTomato = sameItem(holding(sim, 0), tomato);
  interact(sim, 0, 'counter_0');
  const deposited = holding(sim, 0) === null;

  for (let i = 0; i < 20; i++) sim.step([]);
  interact(sim, 0, 'counter_0');
  const sameChefPickup = sameItem(holding(sim, 0), tomato);
  interact(sim, 0, 'counter_0');
  interact(sim, 1, 'counter_0');
  const differentChefPickup = sameItem(holding(sim, 1), tomato);

  interact(sim, 1, 'counter_0');
  interact(sim, 2, 'bin_1');
  interact(sim, 2, 'counter_0');
  const occupiedNoopLeavesHeld = sameItem(holding(sim, 2), lettuce);
  interact(sim, 3, 'counter_0');
  const occupiedNoopPreservedTop = sameItem(holding(sim, 3), tomato);

  interact(sim, 3, 'plating_0');
  interact(sim, 3, 'plating_0');
  const plateFromCounterTop = holding(sim, 3);
  interact(sim, 3, 'counter_0');
  interact(sim, 2, 'counter_0');
  const componentIntoCounterPlate = holding(sim, 2) === null;
  interact(sim, 4, 'counter_0');
  const mergedPlate = holding(sim, 4);
  const plateMergeOk = itemKey(mergedPlate) === itemKey({ type: 'plate', items: [tomato, lettuce] });

  interact(sim, 0, 'bin_2');
  interact(sim, 0, 'counter_0');
  interact(sim, 4, 'counter_0');
  const heldPlateMerge = itemKey(holding(sim, 4)) === itemKey({ type: 'plate', items: [tomato, lettuce, onion] });
  interact(sim, 0, 'counter_0');
  const counterWasPopped = holding(sim, 0) === null;

  return {
    kind: 'counter_handoff',
    checks: {
      fetchedTomato,
      deposited,
      sameChefPickup,
      differentChefPickup,
      occupiedNoopLeavesHeld,
      occupiedNoopPreservedTop,
      componentIntoCounterPlate,
      plateMergeOk,
      heldPlateMerge,
      counterWasPopped,
      plateWasLiftable: plateFromCounterTop?.type === 'plate',
    },
  };
}

const results = [];
results.push(driveCounterHandoff(777));
for (let seed = 1; seed <= 12; seed++) {
  results.push(driveSteak(seed));
  results.push(driveSalad(seed + 1000));
}
process.stdout.write(JSON.stringify(results));
