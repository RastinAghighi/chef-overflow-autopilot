// sim/botlog.mjs — Offline solver / bot-log generator (test + PoC fixture).
//
// Threat model: an attacker calls POST /api/start-run, gets {run_id, token}.
// Because seed = FNV-1a(run_id) and the sim is fully deterministic, they know
// every future order before "playing" and can synthesize a travel-LEGAL input
// log entirely offline. generateBotLog() plays that role with a greedy 5-chef
// Steak/Salad/Burger factory.
//
// Used by sim/behavior.test.mjs (as the positive "machine-generated" fixture)
// and by sim/exploit2.mjs (CLI demonstration).

import { createSim, defaultConfig, simulate, getCanonicalCounterIds, TICK_HZ } from './core.js';
import { seedFromRunId } from './prng.js';
import { buildStationTable, encodeInputLog } from './inputlog.js';

const GAP  = 140;  // ticks between different-station hops (> max travel need ~108)
const COOK = 260;  // stove auto-returns cooked at ~240t
const CHOP = 140;  // cutting board auto-returns chopped at ~120t

const STOVE = c => `stove_${c % 3}`;
const CUT   = c => `cutting_${c % 2}`;
const PLATE = c => `plating_${c % 4}`;
const BIN = { tomato:'bin_0', lettuce:'bin_1', onion:'bin_2', meat:'bin_3', dough:'bin_4', cheese:'bin_5' };
const CAN_MAKE = new Set(['Steak', 'Salad', 'Burger']);

function planFor(dish, c, stand) {
  const P = PLATE(c), S = STOVE(c), K = CUT(c);
  const A = [];
  const hop = id => A.push({ dt: GAP, stationId: id });
  switch (dish) {
    case 'Steak':
      hop(BIN.meat); A.push({ dt: GAP, stationId: S });
      A.push({ dt: COOK, stationId: P });
      hop(P); hop(stand); return A;
    case 'Salad':
      hop(BIN.lettuce); A.push({ dt: GAP, stationId: K });
      A.push({ dt: CHOP, stationId: P });
      hop(BIN.tomato);  A.push({ dt: GAP, stationId: K });
      A.push({ dt: CHOP, stationId: P });
      hop(P); hop(stand); return A;
    case 'Burger':
      hop(BIN.meat); A.push({ dt: GAP, stationId: S });
      A.push({ dt: COOK, stationId: P });
      hop(BIN.dough);  A.push({ dt: GAP, stationId: P });
      hop(P); hop(stand); return A;
    default: return null;
  }
}

/**
 * generateBotLog(runId, { maxSeconds }) → {
 *   seed, log (decoded events), tuples (compact wire form), table, ticks, replay
 * }
 * `replay` is the server-equivalent summary (checkTravel + travelTelemetry on).
 */
export function generateBotLog(runId, { maxSeconds = 150 } = {}) {
  const seed = seedFromRunId(runId);
  const table = buildStationTable(getCanonicalCounterIds());

  const sim = createSim({ seed, config: { ...defaultConfig(), checkTravel: true } });
  const log = [];
  let tick = 0;

  const chefs = Array.from({ length: 5 }, () => ({ queue: [], busy: false, freeAt: 30, servingOrderId: null }));
  const claimed = new Set();
  const planLen = plan => plan.reduce((s, a) => s + a.dt, 0);

  const MAX_TICK = maxSeconds * TICK_HZ;
  while (tick < MAX_TICK && !sim.getState().gameOver) {
    const st = sim.getState();
    const live = st.orders
      .filter(o => !claimed.has(o.id) && CAN_MAKE.has(o.dish))
      .sort((a, b) => a.timeLeft - b.timeLeft);
    for (const chef of chefs) {
      if (chef.busy) continue;
      const order = live.find(o => !claimed.has(o.id));
      if (!order) break;
      const c = chefs.indexOf(chef);
      const plan = planFor(order.dish, c, order.standId);
      if (!plan) continue;
      if (planLen(plan) / TICK_HZ > order.timeLeft - 2) continue; // can't finish in time
      let at = Math.max(tick + 1, chef.freeAt);
      for (const a of plan) { at += a.dt; chef.queue.push({ tick: at, stationId: a.stationId, chefId: c }); }
      chef.busy = true; chef.servingOrderId = order.id; chef.freeAt = at;
      claimed.add(order.id);
    }

    const due = [];
    for (const chef of chefs) {
      while (chef.queue.length && chef.queue[0].tick === tick) {
        due.push(chef.queue.shift());
        if (chef.queue.length === 0) { chef.busy = false; chef.servingOrderId = null; }
      }
    }
    if (due.length) {
      sim.step(due.map(d => ({ type: 'interact', chefId: d.chefId, stationId: d.stationId })));
      for (const d of due) log.push({ tick, type: 'interact', chefId: d.chefId, stationId: d.stationId });
    } else {
      sim.step([]);
    }
    tick++;
  }

  const maxTicks = Math.min(Math.ceil(maxSeconds * TICK_HZ * 1.1) + TICK_HZ * 30, Math.ceil(3600 * TICK_HZ * 1.5));
  const replay = simulate({
    seed,
    config: { ...defaultConfig(), checkTravel: true, travelTelemetry: true },
    inputs: log,
    maxTicks,
  });

  return { seed, log, tuples: encodeInputLog(log, table), table, ticks: tick, replay };
}
