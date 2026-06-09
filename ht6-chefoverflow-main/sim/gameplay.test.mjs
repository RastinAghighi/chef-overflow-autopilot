// Gameplay-mechanics integration tests for the deterministic sim core.
//
// These exercise the full item pipeline the way real play does — pick up,
// chop, cook, plate (single + multi-item), and deliver — driving the sim with
// the SAME { type:'interact', chefId, stationId } event shape the client emits.
//
// Regression guard: a missing `type` field on client events used to make the
// sim silently drop every interaction (applyInput early-returns on unknown
// type), so nothing could ever be cooked/chopped/plated/delivered. Any test
// that builds events without `type` would have masked that — so these assert
// holding state through each pipeline step.

import assert from 'node:assert';
import { createSim, defaultConfig, TICK_HZ } from './core.js';

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); console.log('PASS ', name); passed++; }
  catch (e) { console.log('FAIL ', name, '->', e.message); failed++; }
}

const DT = 1 / TICK_HZ;
const COOK_TICKS = Math.ceil(4 / DT) + 5;   // maxCookTime 4s
const CHOP_TICKS = Math.ceil(2 / DT) + 5;   // maxProcessTime 2s

function mkSim(seed = 12345) { return createSim({ seed, config: defaultConfig() }); }
function interact(sim, stationId, chefId = 0) { sim.step([{ type: 'interact', chefId, stationId }]); }
function wait(sim, n) { for (let i = 0; i < n; i++) sim.step([]); }
function holding(sim, chefId = 0) { return sim.getState().chefs[chefId].holding; }

// Ingredient bins: bin_0 tomato, bin_1 lettuce, bin_2 onion, bin_3 meat, bin_4 dough, bin_5 cheese.

test('pick up raw ingredient from bin', () => {
  const sim = mkSim();
  interact(sim, 'bin_3'); // meat
  assert.deepStrictEqual(holding(sim), { ingredient: 'meat', state: 'raw' });
});

test('cooking meat on stove yields cooked meat', () => {
  const sim = mkSim();
  interact(sim, 'bin_3');     // raw meat
  interact(sim, 'stove_0');   // place on stove (chef waits, auto-returns)
  wait(sim, COOK_TICKS);
  const h = holding(sim);
  assert.ok(h && h.ingredient === 'meat' && h.state === 'cooked', `expected cooked meat, got ${JSON.stringify(h)}`);
});

test('chopping lettuce on cutting board yields chopped lettuce', () => {
  const sim = mkSim();
  interact(sim, 'bin_1');      // raw lettuce
  interact(sim, 'cutting_0');  // chop (chef waits, auto-returns)
  wait(sim, CHOP_TICKS);
  const h = holding(sim);
  assert.ok(h && h.ingredient === 'lettuce' && h.state === 'chopped', `expected chopped lettuce, got ${JSON.stringify(h)}`);
});

test('multi-item plate accumulates two chopped ingredients (Salad)', () => {
  const sim = mkSim();
  // chop lettuce
  interact(sim, 'bin_1'); interact(sim, 'cutting_0'); wait(sim, CHOP_TICKS);
  interact(sim, 'plating_0'); // drop chopped lettuce
  // chop tomato
  interact(sim, 'bin_0'); interact(sim, 'cutting_0'); wait(sim, CHOP_TICKS);
  interact(sim, 'plating_0'); // drop chopped tomato
  interact(sim, 'plating_0'); // pick up assembled plate
  const h = holding(sim);
  assert.ok(h && h.type === 'plate', 'expected a plate');
  assert.strictEqual(h.items.length, 2, 'plate should have 2 items');
  const names = h.items.map(i => i.ingredient + ':' + i.state).sort();
  assert.deepStrictEqual(names, ['lettuce:chopped', 'tomato:chopped']);
});

test('full delivery of a spawned order increments score + delivered', () => {
  const sim = mkSim(2984569322);
  // Let orders spawn, then find one we can satisfy generically by dish.
  for (let t = 0; t < 70; t++) sim.step([]);
  const steak = sim.getState().orders.find(o => o.dish === 'Steak');
  assert.ok(steak, 'expected a Steak order to have spawned');
  interact(sim, 'bin_3');      // meat
  interact(sim, 'stove_0');    // cook
  wait(sim, COOK_TICKS);
  interact(sim, 'plating_0');  // drop cooked meat
  interact(sim, 'plating_0');  // pick up plate
  const before = sim.getState().ordersDelivered;
  interact(sim, steak.standId);
  const st = sim.getState();
  assert.strictEqual(st.ordersDelivered, before + 1, 'delivered count should increase');
  assert.ok(st.score > 0, 'score should be positive after a delivery');
});

console.log(`\nResults: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
