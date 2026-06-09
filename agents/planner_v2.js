/* =============================================================================
 * Chef Overflow - greedy planner v2 (winning ablation: F1 build-ahead only)
 * =============================================================================
 * Self-contained console agent. Paste into the Chef Overflow page console.
 *
 * This mirrors agents/planner.py with exactly the winning feature set from the
 * 30-seed gate: baseline greedy planner + F1 build-ahead. F2 stand pressure and
 * F3 helper assembly are intentionally not included here.
 * ========================================================================== */
(function () {
  'use strict';

  if (typeof window === 'undefined' || !window.KitchenAPI) {
    console.error('[ChefPlannerV2] KitchenAPI not found - run this on the Chef Overflow page.');
    return;
  }
  var API = window.KitchenAPI;

  var COMP_SECS = { raw: 4.5, chopped: 6.5, cooked: 8.5 };
  var DELIVER_HAUL_SECS = 5.0;
  var FEAS_SLACK = 1.5;
  var SERIAL_FACTOR = 0.8;
  var BOOST_MIN_DIST = 6;
  var BIN_COLUMN_X = 3;
  var STUCK_SECONDS = 1.2;
  var EDGE_SPLIT_X = 9;
  var MOVE_DELAY = 0.18;
  var MAX_BUILDAHEAD_PLATES = 2;
  var MIN_ACTIVE_FREE_AREAS = 2;

  var CHOPPABLE = { tomato: 1, lettuce: 1, onion: 1 };
  var COOKABLE = { meat: 1, dough: 1 };
  var RECIPE_COMPONENTS = {
    Salad: [['lettuce', 'chopped'], ['tomato', 'chopped']],
    Burger: [['meat', 'cooked'], ['dough', 'raw']],
    Steak: [['meat', 'cooked']],
    Pizza: [['dough', 'cooked'], ['cheese', 'raw'], ['tomato', 'chopped']],
    'Deluxe Burger': [['meat', 'cooked'], ['dough', 'raw'], ['onion', 'chopped']],
    'Feast Platter': [['meat', 'cooked'], ['lettuce', 'chopped'], ['tomato', 'chopped'], ['cheese', 'raw']],
    'Supreme Pizza': [['dough', 'cooked'], ['tomato', 'chopped'], ['onion', 'chopped'], ['cheese', 'raw']]
  };

  function px(o) { return o.pos[0]; }
  function manhattan(a, b) { return Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]); }
  function isPlate(h) { return !!h && h.type === 'plate'; }
  function ck(ing, st) { return ing + '|' + st; }
  function stateOf(k) { return k.slice(k.indexOf('|') + 1); }
  function ingOf(k) { return k.slice(0, k.indexOf('|')); }
  function cGet(c, k) { return c[k] || 0; }
  function cEmpty(c) { for (var k in c) { if (c[k] > 0) return false; } return true; }
  function cSum(c) { var n = 0; for (var k in c) n += c[k] || 0; return n; }
  function cEq(a, b) {
    for (var k in a) { if ((a[k] || 0) !== (b[k] || 0)) return false; }
    for (var j in b) { if ((a[j] || 0) !== (b[j] || 0)) return false; }
    return true;
  }
  function cFits(have, req) {
    for (var k in have) { if ((have[k] || 0) > (req[k] || 0)) return false; }
    return true;
  }
  function cMissing(req, have) {
    var m = {};
    for (var k in req) {
      var d = req[k] - (have[k] || 0);
      if (d > 0) m[k] = d;
    }
    return m;
  }
  function counterOf(list) {
    var c = {};
    for (var i = 0; i < (list || []).length; i++) {
      var k = ck(list[i].ingredient, list[i].state);
      c[k] = (c[k] || 0) + 1;
    }
    return c;
  }
  function recipeCounter(dish) {
    var arr = RECIPE_COMPONENTS[dish] || [];
    var c = {};
    for (var i = 0; i < arr.length; i++) {
      var k = ck(arr[i][0], arr[i][1]);
      c[k] = (c[k] || 0) + 1;
    }
    return c;
  }
  function processedForm(ing) {
    if (CHOPPABLE[ing]) return ck(ing, 'chopped');
    if (ing === 'meat' || ing === 'dough') return ck(ing, 'cooked');
    return null;
  }
  function nearest(items, pos) {
    var best = null, bd = Infinity;
    for (var i = 0; i < items.length; i++) {
      var d = manhattan(items[i].pos, pos);
      if (d < bd) { bd = d; best = items[i]; }
    }
    return best;
  }

  var S = {};
  function reset() {
    S = {
      chefOrder: {},
      chefArea: {},
      chefBuildahead: {},
      chefFetch: {},
      chefTarget: {},
      prevPos: {},
      prevTime: 0,
      stuckTime: {},
      stats: { buildaheadTriggers: 0, buildaheadCompletions: 0 }
    };
  }

  function cmd(cid, target) {
    var r = API.command(cid, target);
    if (r && r.success) { S.chefTarget[cid] = target; return true; }
    return false;
  }
  function estSeconds(compKeys) {
    if (!compKeys.length) return 0.0;
    var times = compKeys.map(function (k) { return COMP_SECS[stateOf(k)] || 5.0; })
      .sort(function (a, b) { return b - a; });
    var rest = 0;
    for (var i = 1; i < times.length; i++) rest += times[i];
    return DELIVER_HAUL_SECS + times[0] + SERIAL_FACTOR * rest;
  }
  function feasible(order, compKeys) {
    return estSeconds(compKeys) <= Math.max(order.timeLeft, 0.1) * FEAS_SLACK;
  }
  function reachable(order, cpos, stand) {
    return order.timeLeft >= manhattan(cpos, stand.pos) * MOVE_DELAY;
  }
  function bestDeliveryOrder(plate, cpos, ctx) {
    var reach = [];
    for (var i = 0; i < ctx.orders.length; i++) {
      var o = ctx.orders[i];
      var stand = ctx.standById[o.standId];
      if (stand && cEq(ctx.orderC[o.id], plate) && reachable(o, cpos, stand)) reach.push(o);
    }
    if (!reach.length) return null;
    var pool = reach.filter(function (o) { return !ctx.claimed[o.standId]; });
    if (!pool.length) pool = reach;
    return pool.reduce(function (best, o) { return o.timeLeft < best.timeLeft ? o : best; });
  }

  function reconcileBuildahead(ctx, live) {
    var activeOwned = {};
    Object.keys(S.chefOrder).forEach(function (cid) { activeOwned[S.chefOrder[cid]] = 1; });
    var claimedSlots = {};
    Object.keys(S.chefBuildahead).forEach(function (cid) {
      var plan = S.chefBuildahead[cid];
      if (!live[cid]) {
        delete S.chefBuildahead[cid]; delete S.chefArea[cid]; return;
      }
      var area = S.chefArea[cid];
      if (area == null || area >= ctx.contentsC.length) {
        delete S.chefBuildahead[cid]; delete S.chefArea[cid]; return;
      }
      var req = recipeCounter(plan.dish);
      var have = ctx.contentsC[area];
      if (!cEmpty(have) && !cFits(have, req)) {
        delete S.chefBuildahead[cid]; delete S.chefArea[cid]; return;
      }
      var matches = ctx.orders.filter(function (o) { return o.dish === plan.dish; });
      var unowned = matches.filter(function (o) { return !activeOwned[o.id]; });
      if (unowned.length) {
        var o = unowned.reduce(function (best, x) { return x.timeLeft < best.timeLeft ? x : best; });
        S.chefOrder[cid] = o.id;
        activeOwned[o.id] = 1;
        delete S.chefBuildahead[cid];
        return;
      }
      if (matches.length) {
        plan.pending = false;
        return;
      }
      var slotCands = [];
      for (var i = 0; i < ctx.upcoming.length; i++) {
        if (ctx.upcoming[i].dish === plan.dish && !claimedSlots[i]) slotCands.push([i, ctx.upcoming[i]]);
      }
      if (!slotCands.length) {
        delete S.chefBuildahead[cid]; delete S.chefArea[cid]; return;
      }
      var oldSlot = plan.slot == null ? 999 : plan.slot;
      var chosen = slotCands.reduce(function (best, pair) {
        if (!best) return pair;
        var bd = Math.abs(best[0] - oldSlot), pd = Math.abs(pair[0] - oldSlot);
        return (pd < bd || (pd === bd && pair[0] < best[0])) ? pair : best;
      }, null);
      plan.slot = chosen[0];
      plan.eta = Number(chosen[1].etaSeconds || 999);
      plan.pending = true;
      claimedSlots[chosen[0]] = 1;
    });
  }

  function tryStartBuildahead(c, ctx, emptyAreas, activeCands) {
    if (activeCands.length) return false;
    if (Object.keys(S.chefBuildahead).length >= MAX_BUILDAHEAD_PLATES) return false;
    if (emptyAreas.length <= MIN_ACTIVE_FREE_AREAS) return false;
    var claimedSlots = {};
    Object.keys(S.chefBuildahead).forEach(function (cid) {
      var p = S.chefBuildahead[cid];
      if (p.pending !== false) claimedSlots[p.slot] = 1;
    });
    var slots = [];
    for (var i = 0; i < ctx.upcoming.length; i++) {
      var spec = ctx.upcoming[i];
      if (!claimedSlots[i] && RECIPE_COMPONENTS[spec.dish]) {
        slots.push([Number(spec.etaSeconds || 999), i, spec]);
      }
    }
    if (!slots.length) return false;
    slots.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    var chosen = slots[0], spec = chosen[2];
    var area = emptyAreas.reduce(function (best, i) {
      var ap = ctx.areas[i].pos, bp = ctx.areas[best].pos;
      var ka = [(ap[0] >= 10 ? 0 : 1), Math.abs(ap[0] - 10), Math.abs(ap[1] - 6), ctx.areas[i].id];
      var kb = [(bp[0] >= 10 ? 0 : 1), Math.abs(bp[0] - 10), Math.abs(bp[1] - 6), ctx.areas[best].id];
      for (var x = 0; x < ka.length; x++) if (ka[x] !== kb[x]) return ka[x] < kb[x] ? i : best;
      return best;
    });
    S.chefBuildahead[c.id] = {
      dish: spec.dish,
      slot: chosen[1],
      eta: Number(spec.etaSeconds || 999),
      pending: true,
      completed: false
    };
    S.chefArea[c.id] = area;
    ctx.ownedAreas[area] = 1;
    S.stats.buildaheadTriggers++;
    return true;
  }

  function advanceHeld(c, a, req, have, ctx) {
    var cid = c.id, h = c.holding, k = ck(h.ingredient, h.state);
    if (cGet(req, k) > cGet(have, k)) { cmd(cid, ctx.areas[a].id); return; }
    var proc = h.state === 'raw' ? processedForm(h.ingredient) : null;
    if (proc && cGet(req, proc) > cGet(have, proc)) {
      if (stateOf(proc) === 'cooked') {
        var fs = ctx.st.stoves.filter(function (s) { return s.cooking == null && !ctx.claimed[s.id]; });
        if (fs.length) { var s = nearest(fs, c.pos); if (cmd(cid, s.id)) ctx.claimed[s.id] = 1; }
      } else {
        var fb = ctx.st.cuttingBoards.filter(function (b) { return !b.busy && !ctx.claimed[b.id]; });
        if (fb.length) { var b = nearest(fb, c.pos); if (cmd(cid, b.id)) ctx.claimed[b.id] = 1; }
      }
      return;
    }
    cmd(cid, ctx.trashId);
  }

  function fetchNext(c, req, have, missing, ctx) {
    var keys = Object.keys(missing).sort(function (k1, k2) {
      return (stateOf(k1) === 'raw' ? 0 : 1) - (stateOf(k2) === 'raw' ? 0 : 1);
    });
    for (var i = 0; i < keys.length; i++) {
      var ing = ingOf(keys[i]);
      if (ctx.fetching[ing] || !ctx.binByIng[ing]) continue;
      var needsCook = (cGet(req, ck(ing, 'cooked')) > cGet(have, ck(ing, 'cooked'))) && COOKABLE[ing]
        && cGet(req, ck(ing, 'raw')) <= cGet(have, ck(ing, 'raw'));
      var needsChop = (cGet(req, ck(ing, 'chopped')) > cGet(have, ck(ing, 'chopped'))) && CHOPPABLE[ing];
      if (needsCook && !ctx.st.stoves.some(function (s) { return s.cooking == null && !ctx.claimed[s.id]; })) continue;
      if (needsChop && !ctx.st.cuttingBoards.some(function (b) { return !b.busy && !ctx.claimed[b.id]; })) continue;
      if (cmd(c.id, ctx.binByIng[ing].id)) { S.chefFetch[c.id] = ing; ctx.fetching[ing] = 1; }
      return true;
    }
    return false;
  }

  function driveOwner(c, ctx) {
    var cid = c.id, oid = S.chefOrder[cid], a = S.chefArea[cid];
    if (a == null) return;
    var req = ctx.orderC[oid], have = ctx.contentsC[a], h = c.holding;
    if (isPlate(h)) {
      var target = bestDeliveryOrder(counterOf(h.items), c.pos, ctx);
      if (target) {
        var stand = ctx.standById[target.standId];
        if (cmd(cid, target.standId)) {
          ctx.claimed[target.standId] = 1;
          if (manhattan(c.pos, stand.pos) >= BOOST_MIN_DIST && !c.boostActive && (c.boostCooldown || 0) <= 0)
            API.boost(cid);
        }
      } else {
        cmd(cid, ctx.trashId);
      }
      return;
    }
    if (h != null) { advanceHeld(c, a, req, have, ctx); return; }
    var missing = cMissing(req, have);
    if (cEmpty(missing)) { cmd(cid, ctx.areas[a].id); return; }
    fetchNext(c, req, have, missing, ctx);
  }

  function driveBuildahead(c, ctx) {
    var cid = c.id, plan = S.chefBuildahead[cid], a = S.chefArea[cid];
    if (!plan || a == null) return;
    var req = recipeCounter(plan.dish), have = ctx.contentsC[a], h = c.holding;
    if (isPlate(h)) {
      var target = bestDeliveryOrder(counterOf(h.items), c.pos, ctx);
      if (target) {
        var stand = ctx.standById[target.standId];
        if (cmd(cid, target.standId)) {
          ctx.claimed[target.standId] = 1;
          if (manhattan(c.pos, stand.pos) >= BOOST_MIN_DIST && !c.boostActive && (c.boostCooldown || 0) <= 0)
            API.boost(cid);
        }
      } else if (plan.pending !== false) {
        if (cSum(ctx.contentsC[a]) === 0) cmd(cid, ctx.areas[a].id);
      } else {
        cmd(cid, ctx.trashId);
      }
      return;
    }
    if (h != null) { advanceHeld(c, a, req, have, ctx); return; }
    var missing = cMissing(req, have);
    if (cEmpty(missing)) {
      if (!plan.completed) { plan.completed = true; S.stats.buildaheadCompletions++; }
      if (bestDeliveryOrder(have, c.pos, ctx)) cmd(cid, ctx.areas[a].id);
      return;
    }
    fetchNext(c, req, have, missing, ctx);
  }

  function driveFree(c, ctx) {
    var cid = c.id, h = c.holding;
    if (isPlate(h)) {
      var target = bestDeliveryOrder(counterOf(h.items), c.pos, ctx);
      if (target) { if (cmd(cid, target.standId)) ctx.claimed[target.standId] = 1; }
      else { cmd(cid, ctx.trashId); }
      return;
    }
    if (h != null) { cmd(cid, ctx.trashId); return; }

    var emptyAreas = [];
    for (var i = 0; i < ctx.areas.length; i++)
      if (!ctx.ownedAreas[i] && cEmpty(ctx.contentsC[i])) emptyAreas.push(i);
    var cands = ctx.orders.filter(function (o) {
      return !ctx.ownedOrders[o.id] && feasible(o, ctx.orderKeys[o.id]);
    });
    if (cands.length && emptyAreas.length) {
      var o = cands.reduce(function (best, x) { return x.timeLeft < best.timeLeft ? x : best; });
      var stand = ctx.standById[o.standId];
      var spos = stand ? stand.pos : [0, 0];
      var a = emptyAreas.reduce(function (best, i) {
        return manhattan(ctx.areas[i].pos, spos) < manhattan(ctx.areas[best].pos, spos) ? i : best;
      });
      S.chefOrder[cid] = o.id; S.chefArea[cid] = a;
      ctx.ownedOrders[o.id] = 1; ctx.ownedAreas[a] = 1;
      return;
    }
    if (tryStartBuildahead(c, ctx, emptyAreas, cands)) return;

    var orphans = [];
    for (var j = 0; j < ctx.areas.length; j++)
      if (!ctx.ownedAreas[j] && !cEmpty(ctx.contentsC[j])) orphans.push(ctx.areas[j]);
    if (orphans.length) { cmd(cid, nearest(orphans, c.pos).id); return; }

    var park = parkTarget(c, ctx);
    if (park != null && cmd(cid, park)) ctx.claimed[park] = 1;
  }

  function parkTarget(c, ctx) {
    var freeAreas = [];
    for (var i = 0; i < ctx.areas.length; i++)
      if (!ctx.ownedAreas[i] && cEmpty(ctx.contentsC[i]) && !ctx.claimed[ctx.areas[i].id]) freeAreas.push(i);
    if (freeAreas.length) {
      var bi = freeAreas[0];
      for (var k = 1; k < freeAreas.length; k++) if (px(ctx.areas[freeAreas[k]]) < px(ctx.areas[bi])) bi = freeAreas[k];
      return ctx.areas[bi].id;
    }
    if (!ctx.claimed[ctx.trashId]) return ctx.trashId;
    var freeStands = ctx.stands.filter(function (s) { return s.order == null && !ctx.claimed[s.id]; });
    if (freeStands.length) {
      return freeStands.reduce(function (best, s) {
        return Math.abs(s.pos[1] - 6.5) > Math.abs(best.pos[1] - 6.5) ? s : best;
      }).id;
    }
    return null;
  }

  function decide(state) {
    if (!state.running || state.paused || state.gameOver) return;
    var chefs = state.chefs, st = state.stations;
    var areas = st.platingAreas, stands = st.receptionStands, bins = st.ingredientBins;
    var trashId = st.trashCans[0].id, nAreas = areas.length;
    var binByIng = {}; bins.forEach(function (b) { binByIng[b.ingredient] = b; });
    var standById = {}; stands.forEach(function (s) { standById[s.id] = s; });
    var orders = state.orders || [], upcoming = state.upcomingOrders || [];
    var orderById = {}, orderC = {}, orderKeys = {};
    orders.forEach(function (o) {
      orderById[o.id] = o;
      orderC[o.id] = counterOf(o.components);
      orderKeys[o.id] = o.components.map(function (x) { return ck(x.ingredient, x.state); });
    });
    var contentsC = areas.map(function (a) { return counterOf(a.items); });
    function isIdle(c) { return !c.busy && !c.hasPath && (c.stall || 0) <= 0; }

    var live = {}; chefs.forEach(function (c) { live[c.id] = 1; });
    Object.keys(S.chefOrder).forEach(function (cid) {
      if (!live[cid] || !orderById[S.chefOrder[cid]]) {
        delete S.chefOrder[cid]; delete S.chefArea[cid]; delete S.chefFetch[cid];
      }
    });
    Object.keys(S.chefTarget).forEach(function (cid) { if (!live[cid]) delete S.chefTarget[cid]; });
    chefs.forEach(function (c) { if (c.holding != null) delete S.chefFetch[c.id]; });

    var ctx = {
      st: st, areas: areas, stands: stands, binByIng: binByIng, standById: standById,
      orders: orders, orderById: orderById, orderC: orderC, orderKeys: orderKeys,
      contentsC: contentsC, upcoming: upcoming, trashId: trashId,
      ownedAreas: {}, ownedOrders: {}, fetching: {}, claimed: {}
    };
    reconcileBuildahead(ctx, live);
    Object.keys(S.chefOrder).forEach(function (cid) {
      ctx.ownedAreas[S.chefArea[cid]] = 1; ctx.ownedOrders[S.chefOrder[cid]] = 1;
    });
    Object.keys(S.chefBuildahead).forEach(function (cid) { ctx.ownedAreas[S.chefArea[cid]] = 1; });

    chefs.forEach(function (c) {
      var tgt = S.chefTarget[c.id];
      if (tgt == null) return;
      if (isIdle(c)) delete S.chefTarget[c.id];
      else ctx.claimed[tgt] = 1;
    });

    chefs.forEach(function (c) {
      var cid = c.id, tgt = S.chefTarget[cid], h = c.holding;
      if (standById[tgt] && isPlate(h) && c.hasPath && !c.busy) {
        var plate = counterOf(h.items);
        var cur = orders.filter(function (o) { return o.standId === tgt; })[0];
        var stillOk = cur && cEq(orderC[cur.id], plate);
        if (!stillOk) {
          var target = bestDeliveryOrder(plate, c.pos, ctx);
          if (target && target.standId !== tgt) { if (cmd(cid, target.standId)) ctx.claimed[target.standId] = 1; }
          else if (!target) { cmd(cid, trashId); }
        }
      }
    });

    var now = state.time || 0;
    var dtSeen = Math.max(0, now - S.prevTime); S.prevTime = now;
    var farLeft = 0; for (var i = 1; i < nAreas; i++) if (px(areas[i]) < px(areas[farLeft])) farLeft = i;
    var escaped = {};
    chefs.forEach(function (c) {
      var cid = c.id, key = c.pos[0] + ',' + c.pos[1];
      if (c.busy || S.prevPos[cid] !== key) S.stuckTime[cid] = 0;
      else S.stuckTime[cid] = (S.stuckTime[cid] || 0) + dtSeen;
      S.prevPos[cid] = key;
      if ((S.stuckTime[cid] || 0) >= STUCK_SECONDS && c.holding == null
          && S.chefOrder[cid] == null && S.chefBuildahead[cid] == null) {
        var moved;
        if (c.pos[0] <= EDGE_SPLIT_X) moved = cmd(cid, areas[farLeft].id);
        else {
          var ns = stands.reduce(function (best, s) { return manhattan(c.pos, s.pos) < manhattan(c.pos, best.pos) ? s : best; });
          moved = cmd(cid, ns.id);
        }
        if (moved) { S.stuckTime[cid] = 0; escaped[cid] = 1; }
      }
    });

    Object.keys(S.chefFetch).forEach(function (cid) { ctx.fetching[S.chefFetch[cid]] = 1; });
    chefs.forEach(function (c) {
      var h = c.holding;
      if (h != null && !isPlate(h) && h.state === 'raw' && c.pos[0] <= BIN_COLUMN_X) ctx.fetching[h.ingredient] = 1;
    });

    var idle = chefs.filter(function (c) { return isIdle(c) && !escaped[c.id]; });
    idle.sort(function (a, b) {
      var ra = (S.chefOrder[a.id] != null || S.chefBuildahead[a.id] != null) ? 0 : 1;
      var rb = (S.chefOrder[b.id] != null || S.chefBuildahead[b.id] != null) ? 0 : 1;
      return ra - rb;
    });
    idle.forEach(function (c) {
      if (S.chefOrder[c.id] != null) driveOwner(c, ctx);
      else if (S.chefBuildahead[c.id] != null) driveBuildahead(c, ctx);
      else driveFree(c, ctx);
    });
  }

  var THROTTLE_S = 0.05;
  var lastDecideT = -1e9;
  function tick(state) {
    try {
      var s = state || API.getState();
      var dt = s.time - lastDecideT;
      if (dt >= 0 && dt < THROTTLE_S) return;
      lastDecideT = s.time;
      decide(s);
    } catch (e) { console.error('[ChefPlannerV2] error', e); }
  }
  function start() {
    lastDecideT = -1e9;
    API.run(tick);
    console.log('[ChefPlannerV2] registered: baseline + F1 build-ahead only.');
  }
  function stop() { API.stop(); console.log('[ChefPlannerV2] stopped.'); }

  reset();
  start();

  var ctl = { start: start, stop: stop, reset: reset, decide: decide, _state: S };
  window.ChefPlannerV2 = ctl;
  return ctl;
})();
