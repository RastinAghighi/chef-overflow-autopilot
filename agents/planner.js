/* =============================================================================
 * Chef Overflow — greedy heuristic planner (Phase 2, live-game port)
 * =============================================================================
 * Self-contained. Paste this whole file into the browser console on
 * hackthe6ix-chefoverflow.vercel.app, then press the game's Start (or call
 * `KitchenAPI.start()`).  It registers a `run()` agent that drives the 5 chefs.
 *
 * This is a line-for-line port of agents/planner.py (the simulator baseline) so
 * that sim results transfer to the real game.  Design (see docs/RL_DESIGN.md §7,
 * §11), in brief:
 *   - ONE chef owns ONE order end to end (fetch -> chop/cook -> deposit -> lift
 *     plate -> deliver).  No two chefs ever build the same plate, which keeps the
 *     components on an area an exact subset of its recipe (never a wrong delivery)
 *     AND avoids the fatal "two chefs fighting over a plating-area approach tile"
 *     deadlock.  The kitchen is brutally congested (1-wide dead-end bin pockets, a
 *     2-tile divider gap), so minimising path-sharing is what keeps it flowing.
 *   - We cannot author paths (the game walks each chef and freezes a blocked one),
 *     so collision-avoidance comes from smarter TARGET choice.  Four rules:
 *       * STATION RESERVATION: every stove/board/plating-area/stand a chef is
 *         walking to or working at is "claimed" (chefTarget); a new assignment of
 *         that station type always picks a free AND un-claimed one, spreading chefs
 *         across distinct stations.  Released the moment the chef goes idle.  Stops
 *         two chefs targeting one stove (the 2nd freezing behind the 1st).
 *       * DEADLINE-AWARE DELIVERY: a finished plate goes to the MOST URGENT (least
 *         timeLeft) order it matches EXACTLY and can still reach — preferring an
 *         un-claimed stand — not the chef's own (fresher) order.  New orders are
 *         likewise claimed most-urgent-first.  Exact-match still makes a wrong
 *         delivery impossible.
 *       * IDLE PLACEMENT: a task-less chef parks on a peripheral, low-traffic
 *         anchor instead of walling the central crossroads.
 *       * STICKY: only IDLE chefs are commanded, so the game's 1.5s mid-route STALL
 *         never fires — except the one safe redirect: an en-route plate-carrier
 *         whose destination stand no longer holds a matching order is re-matched
 *         (or trashed) so it cannot wrong-deliver on arrival.
 *   - Each dead-end bin is reserved for one chef at a time (else they trap).
 *   - Triage: only claim an order a rough estimate says can finish in time.
 *   - Boost fires on the long plating->stand delivery haul.
 *
 * Controls (returned object, also on window.ChefPlanner):
 *   ChefPlanner.stop()   - unregister
 *   ChefPlanner.start()  - re-register
 *   ChefPlanner.reset()  - clear the planner's memory (do this on a fresh game)
 * ========================================================================== */
(function () {
  'use strict';

  if (typeof window === 'undefined' || !window.KitchenAPI) {
    console.error('[ChefPlanner] KitchenAPI not found — run this on the Chef Overflow page.');
    return;
  }
  var API = window.KitchenAPI;

  // ---- tuning constants (identical to planner.py) --------------------------
  var COMP_SECS = { raw: 4.5, chopped: 6.5, cooked: 8.5 };
  var DELIVER_HAUL_SECS = 5.0;
  var FEAS_SLACK = 1.5;
  var SERIAL_FACTOR = 0.8;
  var BOOST_MIN_DIST = 6;
  var BIN_COLUMN_X = 3;
  var STUCK_SECONDS = 1.2;
  var EDGE_SPLIT_X = 9;
  var MOVE_DELAY = 0.18;                 // [sim/game] seconds per tile; for delivery-reach est
  var CHOPPABLE = { tomato: 1, lettuce: 1, onion: 1 };
  var COOKABLE = { meat: 1, dough: 1 };

  // ---- pure helpers (mirror planner.py) ------------------------------------
  function px(o) { return o.pos[0]; }
  function manhattan(a, b) { return Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]); }
  function isPlate(h) { return !!h && h.type === 'plate'; }
  function ck(ing, st) { return ing + '|' + st; }
  function stateOf(k) { return k.slice(k.indexOf('|') + 1); }
  function ingOf(k) { return k.slice(0, k.indexOf('|')); }

  function counterOf(list) {              // list of {ingredient,state} -> {key:count}
    var c = {};
    for (var i = 0; i < (list || []).length; i++) {
      var k = ck(list[i].ingredient, list[i].state);
      c[k] = (c[k] || 0) + 1;
    }
    return c;
  }
  function cGet(c, k) { return c[k] || 0; }
  function cMissing(req, have) {           // positive part of req - have
    var m = {};
    for (var k in req) { var d = req[k] - (have[k] || 0); if (d > 0) m[k] = d; }
    return m;
  }
  function cEmpty(c) { for (var k in c) { if (c[k] > 0) return false; } return true; }
  function cEq(a, b) {
    for (var k in a) { if ((a[k] || 0) !== (b[k] || 0)) return false; }
    for (var j in b) { if ((a[j] || 0) !== (b[j] || 0)) return false; }
    return true;
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

  // ---- persistent planner state (survives ticks; reset on a new game) ------
  var S = {
    chefOrder: {},   // cid -> order id it owns
    chefArea: {},    // cid -> plating area index it assembles on
    chefFetch: {},   // cid -> ingredient it is currently walking to fetch
    chefTarget: {},  // cid -> station id it is currently commanded to (en-route claim)
    prevPos: {},     // cid -> "x,y" (jam detection)
    prevTime: 0,
    stuckTime: {},   // cid -> seconds wedged in place
  };
  function reset() {
    S = { chefOrder: {}, chefArea: {}, chefFetch: {}, chefTarget: {}, prevPos: {}, prevTime: 0, stuckTime: {} };
  }

  // ---- command wrapper: record the chef's live target on success -----------
  function cmd(cid, target) {
    var r = API.command(cid, target);
    if (r && r.success) { S.chefTarget[cid] = target; return true; }
    return false;
  }

  // ---- feasibility (triage estimate only) ----------------------------------
  function estSeconds(compKeys) {
    if (!compKeys.length) return 0.0;
    var times = compKeys.map(function (k) { return COMP_SECS[stateOf(k)] || 5.0; })
                        .sort(function (a, b) { return b - a; });
    var rest = 0; for (var i = 1; i < times.length; i++) rest += times[i];
    return DELIVER_HAUL_SECS + times[0] + SERIAL_FACTOR * rest;
  }
  function feasible(order, compKeys) {
    return estSeconds(compKeys) <= Math.max(order.timeLeft, 0.1) * FEAS_SLACK;
  }
  // True iff the order survives at least straight-line travel to its stand (a
  // physical lower bound).  Orders failing this are doomed regardless, so we never
  // burn a haul (or risk a stand-turnover wrong-delivery) chasing them.
  function reachable(order, cpos, stand) {
    return order.timeLeft >= manhattan(cpos, stand.pos) * MOVE_DELAY;
  }

  // Most-urgent exact-component match we can still reach, preferring an un-claimed
  // stand.  Returns the order, or null (nothing live wants it / all matches doomed).
  function bestDeliveryOrder(plate, cpos, ctx) {
    var matches = ctx.orders.filter(function (o) {
      return ctx.standById[o.standId] && cEq(ctx.orderC[o.id], plate);
    });
    var reach = matches.filter(function (o) {
      return reachable(o, cpos, ctx.standById[o.standId]);
    });
    if (!reach.length) return null;
    var unclaimed = reach.filter(function (o) { return !ctx.claimed[o.standId]; });
    var pool = unclaimed.length ? unclaimed : reach;     // race a claimed stand only as last resort
    return pool.reduce(function (best, o) { return o.timeLeft < best.timeLeft ? o : best; });
  }

  // ---- shared: advance one held component toward its area ------------------
  function advanceHeld(c, a, req, have, ctx) {
    var cid = c.id, h = c.holding, k = ck(h.ingredient, h.state);
    if (cGet(req, k) > cGet(have, k)) { cmd(cid, ctx.areas[a].id); return; }   // recipe wants it -> deposit
    var proc = (h.state === 'raw') ? processedForm(h.ingredient) : null;
    if (proc && cGet(req, proc) > cGet(have, proc)) {
      if (stateOf(proc) === 'cooked') {
        var fs = ctx.st.stoves.filter(function (s) { return s.cooking == null && !ctx.claimed[s.id]; });
        if (fs.length) { var s = nearest(fs, c.pos); if (cmd(cid, s.id)) ctx.claimed[s.id] = 1; }
      } else {
        var fb = ctx.st.cuttingBoards.filter(function (b) { return !b.busy && !ctx.claimed[b.id]; });
        if (fb.length) { var b = nearest(fb, c.pos); if (cmd(cid, b.id)) ctx.claimed[b.id] = 1; }
      }
      return;                                       // else wait for a free station
    }
    cmd(cid, ctx.trashId);                          // holding something the recipe doesn't need
  }

  // ---- an owner drives its single order to delivery ------------------------
  function driveOwner(c, ctx) {
    var cid = c.id, oid = S.chefOrder[cid], a = S.chefArea[cid];
    if (a == null) return;
    var req = ctx.orderC[oid], have = ctx.contentsC[a], h = c.holding;

    if (isPlate(h)) {                                // 1) carry plate -> MOST URGENT exact match
      var plate = counterOf(h.items);
      var target = bestDeliveryOrder(plate, c.pos, ctx);
      if (target) {
        var stand = ctx.standById[target.standId];
        if (cmd(cid, target.standId)) {
          ctx.claimed[target.standId] = 1;
          if (manhattan(c.pos, stand.pos) >= BOOST_MIN_DIST && !c.boostActive && (c.boostCooldown || 0) <= 0)
            API.boost(cid);
        }
      } else {
        cmd(cid, ctx.trashId);                       // nothing live wants it — never wrong-deliver
      }
      return;
    }
    if (h != null) {                                 // 2) component in hand -> deposit / process
      advanceHeld(c, a, req, have, ctx);
      return;
    }
    var missing = cMissing(req, have);               // 3) empty-handed
    if (cEmpty(missing)) { cmd(cid, ctx.areas[a].id); return; }   // complete -> lift plate

    // fetch next component: raw-usable first, then cook/chop, only if station is free
    // AND un-claimed now (so we don't queue behind a chef already heading there).
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
      if (cmd(cid, ctx.binByIng[ing].id)) { S.chefFetch[cid] = ing; ctx.fetching[ing] = 1; }
      return;
    }
    // nothing startable (bins reserved / stations busy) -> wait
  }

  // ---- a free chef claims a new order, cleans up, or parks -----------------
  function driveFree(c, ctx) {
    var cid = c.id, h = c.holding;
    if (isPlate(h)) {                                // leftover plate: MOST URGENT exact match, else dump
      var plate = counterOf(h.items);
      var target = bestDeliveryOrder(plate, c.pos, ctx);
      if (target) { if (cmd(cid, target.standId)) ctx.claimed[target.standId] = 1; }
      else { cmd(cid, ctx.trashId); }
      return;
    }
    if (h != null) { cmd(cid, ctx.trashId); return; }   // stray component -> dump

    // claim the MOST URGENT feasible un-owned order onto an empty un-owned area
    var emptyAreas = [];
    for (var i = 0; i < ctx.areas.length; i++)
      if (!ctx.ownedAreas[i] && cEmpty(ctx.contentsC[i])) emptyAreas.push(i);
    var cands = ctx.orders.filter(function (o) {
      return !ctx.ownedOrders[o.id] && feasible(o, ctx.orderKeys[o.id]);
    });
    if (cands.length && emptyAreas.length) {
      var o = cands.reduce(function (best, o) { return o.timeLeft < best.timeLeft ? o : best; });
      var stand = ctx.standById[o.standId];
      var spos = stand ? stand.pos : [0, 0];
      var a = emptyAreas.reduce(function (best, i) {
        return manhattan(ctx.areas[i].pos, spos) < manhattan(ctx.areas[best].pos, spos) ? i : best;
      });
      S.chefOrder[cid] = o.id; S.chefArea[cid] = a;
      ctx.ownedOrders[o.id] = 1; ctx.ownedAreas[a] = 1;
      return;                                        // next tick this chef begins fetching
    }

    // nothing to claim: if an un-owned area holds an orphan plate, clear it
    var orphans = [];
    for (var j = 0; j < ctx.areas.length; j++)
      if (!ctx.ownedAreas[j] && !cEmpty(ctx.contentsC[j])) orphans.push(ctx.areas[j]);
    if (orphans.length) { cmd(cid, nearest(orphans, c.pos).id); return; }

    // genuinely task-less: park on a peripheral, reserved anchor so this chef stops
    // walling the central crossroads (the idle 5th chef otherwise jams it).
    var park = parkTarget(c, ctx);
    if (park != null && cmd(cid, park)) ctx.claimed[park] = 1;
  }

  // ---- choose a low-traffic anchor for a task-less chef --------------------
  // command() only accepts STATION ids (never a bare floor tile), so we anchor on
  // the cleanest peripheral stations, each reserved so several idle chefs spread:
  //   1) the far-left plating area (least-used by the assembler, kitchen-side so
  //      re-tasking is cheap) — only if empty & un-owned (no accidental pickup),
  //   2) the trash (far-left corner),
  //   3) a no-order reception stand (the open reception zone), corner-most.
  // Imperfect — the chef stops one tile *adjacent* to the anchor, not a true dead
  // corner — but any of these clears the crossroads the idle chef would block.
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

  // ---- one decision pass ---------------------------------------------------
  function decide(state) {
    if (!state.running || state.paused || state.gameOver) return;

    var chefs = state.chefs, st = state.stations;
    var areas = st.platingAreas, stands = st.receptionStands, bins = st.ingredientBins;
    var trashId = st.trashCans[0].id;
    var nAreas = areas.length;

    var binByIng = {}; bins.forEach(function (b) { binByIng[b.ingredient] = b; });
    var standById = {}; stands.forEach(function (s) { standById[s.id] = s; });
    var orders = state.orders || [];
    var orderById = {}, orderC = {}, orderKeys = {};
    orders.forEach(function (o) {
      orderById[o.id] = o;
      orderC[o.id] = counterOf(o.components);
      orderKeys[o.id] = o.components.map(function (x) { return ck(x.ingredient, x.state); });
    });
    var contentsC = areas.map(function (a) { return counterOf(a.items); });

    function isIdle(c) { return !c.busy && !c.hasPath && (c.stall || 0) <= 0; }

    // reconcile ownership: release a chef whose order is gone; clear fetch once held
    var live = {}; chefs.forEach(function (c) { live[c.id] = 1; });
    Object.keys(S.chefOrder).forEach(function (cid) {
      if (!live[cid] || !orderById[S.chefOrder[cid]]) {
        delete S.chefOrder[cid]; delete S.chefArea[cid]; delete S.chefFetch[cid];
      }
    });
    Object.keys(S.chefTarget).forEach(function (cid) { if (!live[cid]) delete S.chefTarget[cid]; });
    chefs.forEach(function (c) { if (c.holding != null) delete S.chefFetch[c.id]; });
    var ownedAreas = {}, ownedOrders = {};
    Object.keys(S.chefOrder).forEach(function (cid) { ownedAreas[S.chefArea[cid]] = 1; ownedOrders[S.chefOrder[cid]] = 1; });

    // station reservation: which stoves/boards/areas/stands a chef is walking to or
    // working at.  Built from a clean snapshot BEFORE any command this tick; a chef
    // that has arrived/finished (now idle) releases its claim, the rest hold theirs.
    var claimed = {};
    chefs.forEach(function (c) {
      var tgt = S.chefTarget[c.id];
      if (tgt == null) return;
      if (isIdle(c)) delete S.chefTarget[c.id];      // arrived / finished / dropped -> release
      else claimed[tgt] = 1;                         // still committed -> hold the claim
    });

    var ctx = {
      st: st, areas: areas, stands: stands, binByIng: binByIng, standById: standById,
      orders: orders, orderById: orderById, orderC: orderC, orderKeys: orderKeys,
      contentsC: contentsC, trashId: trashId,
      ownedAreas: ownedAreas, ownedOrders: ownedOrders, fetching: {}, claimed: claimed,
    };

    // sticky, but safe on invalidation: the ONLY redirect of an en-route chef.  A
    // plate-carrier whose destination stand no longer holds a matching order (it
    // expired / was delivered / turned over) would wrong-deliver on arrival, so
    // re-match the plate to the best live order (else trash).  Paying the 1.5s stall
    // once beats wasting the whole haul and breaking the streak.
    chefs.forEach(function (c) {
      var cid = c.id, tgt = S.chefTarget[cid], h = c.holding;
      if (standById[tgt] && isPlate(h) && c.hasPath && !c.busy) {
        var plate = counterOf(h.items);
        var cur = orders.filter(function (o) { return o.standId === tgt; })[0];
        var stillOk = cur && cEq(orderC[cur.id], plate);
        if (!stillOk) {
          var target = bestDeliveryOrder(plate, c.pos, ctx);
          if (target && target.standId !== tgt) { if (cmd(cid, target.standId)) claimed[target.standId] = 1; }
          else if (!target) { cmd(cid, trashId); }
        }
      }
    });

    // jam-breaker: shove a wedged empty unowned chef to the nearer edge
    var now = state.time || 0;
    var dtSeen = Math.max(0, now - S.prevTime); S.prevTime = now;
    var farLeft = 0; for (var i = 1; i < nAreas; i++) if (px(areas[i]) < px(areas[farLeft])) farLeft = i;
    var escaped = {};
    chefs.forEach(function (c) {
      var cid = c.id, key = c.pos[0] + ',' + c.pos[1];
      if (c.busy || S.prevPos[cid] !== key) S.stuckTime[cid] = 0;
      else S.stuckTime[cid] = (S.stuckTime[cid] || 0) + dtSeen;
      S.prevPos[cid] = key;
      if ((S.stuckTime[cid] || 0) >= STUCK_SECONDS && c.holding == null && S.chefOrder[cid] == null) {
        var moved;
        if (c.pos[0] <= EDGE_SPLIT_X) moved = cmd(cid, areas[farLeft].id);
        else {
          var ns = stands.reduce(function (best, s) { return manhattan(c.pos, s.pos) < manhattan(c.pos, best.pos) ? s : best; });
          moved = cmd(cid, ns.id);
        }
        if (moved) { S.stuckTime[cid] = 0; escaped[cid] = 1; }
      }
    });

    // bin reservation: one chef per dead-end pocket
    var fetching = {};
    Object.keys(S.chefFetch).forEach(function (cid) { fetching[S.chefFetch[cid]] = 1; });
    chefs.forEach(function (c) {
      var h = c.holding;
      if (h != null && !isPlate(h) && h.state === 'raw' && c.pos[0] <= BIN_COLUMN_X) fetching[h.ingredient] = 1;
    });
    ctx.fetching = fetching;

    // owners first (deliver/advance), then free chefs (claim/cleanup/park)
    var idle = chefs.filter(function (c) { return isIdle(c) && !escaped[c.id]; });
    idle.sort(function (a, b) { return (S.chefOrder[a.id] != null ? 0 : 1) - (S.chefOrder[b.id] != null ? 0 : 1); });
    idle.forEach(function (c) {
      if (S.chefOrder[c.id] != null) driveOwner(c, ctx);
      else driveFree(c, ctx);
    });
  }

  // ---- register ------------------------------------------------------------
  // The game fires onTick ~60 fps, but deciding every frame makes the planner too
  // eager: all chefs advance their pipeline at once and jam the chokepoints (in the
  // sim, 60 Hz scores ~40% below 10-30 Hz).  So we self-throttle to ~20 Hz of game
  // time, which staggers chef movement and matches the benchmark's best regime.
  var THROTTLE_S = 0.05;                  // 20 Hz; sweet spot is anywhere 0.03-0.10 s
  var lastDecideT = -1e9;
  function tick(state) {
    try {
      var s = state || API.getState();
      var dt = s.time - lastDecideT;
      if (dt >= 0 && dt < THROTTLE_S) return;   // throttle (dt<0 => game restarted)
      lastDecideT = s.time;
      decide(s);
    } catch (e) { console.error('[ChefPlanner] error', e); }
  }
  function start() { lastDecideT = -1e9; API.run(tick); console.log('[ChefPlanner] registered (throttled to ~' + Math.round(1 / THROTTLE_S) + ' Hz). Press Start (or KitchenAPI.start()).'); }
  function stop() { API.stop(); console.log('[ChefPlanner] stopped.'); }

  reset();
  start();

  // `decide` is exposed for console debugging / parity testing against the sim
  // baseline; the live agent is still driven by the throttled `tick` via API.run.
  var ctl = { start: start, stop: stop, reset: reset, decide: decide, _state: S };
  window.ChefPlanner = ctl;
  return ctl;
})();
