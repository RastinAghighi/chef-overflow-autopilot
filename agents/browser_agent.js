/* =============================================================================
 * Chef Overflow Milestone 1 browser telemetry agent.
 *
 * Paste this file on the Chef Overflow page. It does NOT use KitchenAPI.run(), so
 * agents/planner.js can remain the live-submission safety net.
 *
 * Controls:
 *   ChefM1.start()          observe commands/state
 *   ChefM1.stop()           stop observing and restore command wrappers
 *   ChefM1.download()       Blob-download trace.jsonl
 *   ChefM1.copySummary()    copy compact summary JSON when available
 *   ChefM1.executor.assign(chefId, stationId, intent)  optional executor use
 * ========================================================================== */
(function () {
  'use strict';

  if (typeof window === 'undefined' || !window.KitchenAPI) {
    console.error('[ChefM1] KitchenAPI not found.');
    return;
  }

  var API = window.KitchenAPI;
  var Recorder = window.ChefTelemetryRecorder;
  var ReservationManager = window.ChefReservationManager;
  var Executor = window.ChefExecutor;

  if (!Recorder || !ReservationManager || !Executor) {
    console.warn('[ChefM1] For full executor support, paste telemetry_schema.js, telemetry.js, reservations.js, and executor.js before browser_agent.js.');
  }

  function clone(obj) {
    if (obj == null) return obj;
    try { return JSON.parse(JSON.stringify(obj)); }
    catch (_) { return null; }
  }

  function isPlate(x) { return !!x && typeof x === 'object' && x.type === 'plate'; }
  function itemCount(x) { return isPlate(x) ? (x.items || []).length : (x ? 1 : 0); }
  function tickOf(state) { return Math.floor(Number(state && state.time || 0) * 60); }
  function orderKey(order) { return String(order && order.id); }

  // Minimal fallback recorder so browser_agent.js remains paste-runnable for
  // observation even if the module files were not pasted first.
  if (!Recorder) {
    Recorder = function (opts) {
      opts = opts || {};
      this.traceId = opts.traceId || ('trace-' + Date.now().toString(36));
      this.events = [];
      this.seq = 0;
      this.maxBytes = opts.maxBytes || 25 * 1024 * 1024;
      this.bytes = 0;
      this.dropped = { state_sample: 0, metric_sample: 0, other: 0 };
    };
    Recorder.prototype.record = function (type, tick, ms, data) {
      var ev = { type: type, tick: tick || 0, ms: ms || performance.now(), seq: this.seq++, data: data || {} };
      var s = JSON.stringify(ev).length + 1;
      while (this.bytes + s > this.maxBytes && this.events.length > 2) {
        var idx = this.events.findIndex(function (e, i) { return i > 0 && e.type === 'state_sample'; });
        if (idx < 0) idx = 1;
        this.bytes -= JSON.stringify(this.events[idx]).length + 1;
        this.dropped[this.events[idx].type] = (this.dropped[this.events[idx].type] || 0) + 1;
        this.events.splice(idx, 1);
      }
      this.events.push(ev);
      this.bytes += s;
    };
    Recorder.prototype.start = function (state) {
      this.record('trace_start', 0, 0, {
        schema_version: 'telemetry.v1',
        trace_id: this.traceId,
        run_id: null,
        started_at_iso: new Date().toISOString(),
        source: 'local_browser',
        game_url: location.href,
        agent_version: 'milestone1-browser-fallback',
        game_api_version: API.version || null,
        seed_known: false,
        tick_hz: 60
      });
      if (state) this.sampleState(state);
    };
    Recorder.prototype.sampleState = function (state) {
      this.record('state_sample', tickOf(state), performance.now(), { state: clone(state) });
    };
    Recorder.prototype.end = function (state) {
      this.record('trace_end', tickOf(state), performance.now(), {
        score: Math.floor(state.score || 0),
        delivered: state.__m1_delivered || 0,
        best_streak: state.bestStreak || 0,
        failed_orders: state.failedOrders || 0,
        time_sec: state.time || 0,
        game_over: !!state.gameOver,
        dropped: clone(this.dropped)
      });
    };
    Recorder.prototype.flush = function () { return this.events.map(function (e) { return JSON.stringify(e); }).join('\n') + '\n'; };
    Recorder.prototype.download = function () {
      var blob = new Blob([this.flush()], { type: 'application/jsonl' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = 'trace.jsonl'; document.body.appendChild(a); a.click();
      setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 1000);
    };
    Recorder.prototype.copySummary = function () {
      var text = JSON.stringify({ trace_id: this.traceId, events: this.events.length, dropped: this.dropped }, null, 2);
      if (typeof copy === 'function') copy(text);
      else console.log(text);
    };
  }

  if (!ReservationManager) {
    ReservationManager = function () { this.reset(); };
    ReservationManager.prototype.reset = function () { this.items = {}; this.nextId = 1; };
    ReservationManager.prototype.reserve = function (req) {
      var r = { id: 'r' + this.nextId++, kind: req.kind, resource_id: String(req.resource_id), chef_id: req.chef_id, status: 'active' };
      this.items[r.id] = r; return r;
    };
    ReservationManager.prototype.release = function (id) { if (this.items[id]) this.items[id].status = 'released'; };
    ReservationManager.prototype.expire = function () { return []; };
    ReservationManager.prototype.snapshot = function () {
      var out = [];
      Object.keys(this.items).forEach(function (k) { if (this.items[k].status === 'active') out.push(this.items[k]); }, this);
      return { active: out };
    };
  }

  if (!Executor) {
    Executor = function (api, recorder, reservations) { this.api = api; this.recorder = recorder; this.reservations = reservations; this.holdUntilMs = 0; };
    Executor.prototype.assign = function (chefId, targetId, intent) {
      if (performance.now() < this.holdUntilMs) return { success: false, error: 'held for restart stabilization' };
      var s = this.api.getState();
      this.recorder.record('executor_decision', tickOf(s), performance.now(), { chef_id: chefId, kind: intent && intent.kind || 'assign', target_id: targetId });
      return this.api.command(chefId, targetId);
    };
    Executor.prototype.tick = function () {};
    Executor.prototype.snapshot = function () { return []; };
    Executor.prototype.reset = function () {};
    Executor.prototype.holdForRestart = function (ms) { this.holdUntilMs = performance.now() + (ms || 1500); };
  }

  var cfg = {
    pollMs: 100,
    stateSampleMs: 5000,
    metricSampleMs: 1000,
    maxBytes: 25 * 1024 * 1024,
    blockedMs: 600,
    seedSettleMs: 1800
  };

  var recorder = null;
  var reservations = null;
  var executor = null;
  var timer = null;
  var originalCommand = null;
  var originalBoost = null;
  var originalConsoleInfo = null;
  var activeCommands = {};
  var prevState = null;
  var prevOrders = {};
  var inferredCustomers = {};
  var deliveredCount = 0;
  var failureCounts = { expired: 0, no_slot: 0, wrong: 0 };
  var blockedMsByChef = {};
  var lastMetricMs = 0;
  var lastStateMs = 0;
  var metric = null;
  var running = false;
  var seedStarts = 0;
  var lastSeedLine = null;

  function resetMetric() {
    metric = { chefs: {}, stations: {}, stand_pressure: {} };
  }

  function add(obj, key, value) { obj[key] = (obj[key] || 0) + value; }

  function stationGroups(state) {
    var st = state.stations || {};
    return [].concat(st.ingredientBins || [], st.stoves || [], st.cuttingBoards || [], st.platingAreas || [], st.receptionStands || [], st.trashCans || [], st.counters || []);
  }

  function cookDemand(state) {
    var orders = (state.orders || []).concat(state.upcomingOrders || []);
    for (var i = 0; i < orders.length; i++) {
      var comps = orders[i].components || [];
      for (var j = 0; j < comps.length; j++) if (comps[j].state === 'cooked') return true;
    }
    for (var c = 0; c < (state.chefs || []).length; c++) {
      var h = state.chefs[c].holding;
      if (h && h.state === 'raw' && (h.ingredient === 'meat' || h.ingredient === 'dough')) return true;
    }
    return false;
  }

  function updateMetric(state, dt) {
    var chefs = state.chefs || [];
    chefs.forEach(function (chef) {
      var cid = String(chef.id);
      if (!metric.chefs[cid]) metric.chefs[cid] = {};
      var row = metric.chefs[cid];
      var bucket = 'idle';
      var prev = prevState && (prevState.chefs || []).find(function (c) { return c.id === chef.id; });
      var samePos = prev && String(prev.pos) === String(chef.pos);
      if (chef.busy) bucket = 'processing';
      else if ((chef.stall || 0) > 0) bucket = 'stall';
      else if (chef.hasPath) {
        blockedMsByChef[cid] = samePos ? (blockedMsByChef[cid] || 0) + dt * 1000 : 0;
        bucket = blockedMsByChef[cid] >= cfg.blockedMs ? 'blocked' : 'moving';
      } else {
        blockedMsByChef[cid] = 0;
      }
      add(row, bucket + '_sec', dt);
      if (chef.holding) add(row, 'holding_sec', dt);
      if (bucket === 'blocked') {
        row.blocked_locations = row.blocked_locations || {};
        add(row.blocked_locations, String(chef.pos), dt);
      }
    });

    var st = state.stations || {};
    var stoves = st.stoves || [];
    var boards = st.cuttingBoards || [];
    var plating = st.platingAreas || [];
    var counters = st.counters || [];
    var stands = st.receptionStands || [];
    var stoveBusy = stoves.filter(function (s) { return s.cooking != null; }).length;
    var demand = cookDemand(state);
    add(metric.stations, 'stove_unit_sec', dt * Math.max(1, stoves.length));
    add(metric.stations, 'stove_busy_unit_sec', dt * stoveBusy);
    add(metric.stations, 'stove_ready_unit_sec', dt * stoves.filter(function (s) { return s.ready; }).length);
    add(metric.stations, 'cook_demand_sec', demand ? dt : 0);
    add(metric.stations, 'all_stoves_cold_cook_needed_sec', demand && stoveBusy === 0 ? dt : 0);
    add(metric.stations, 'board_unit_sec', dt * Math.max(1, boards.length));
    add(metric.stations, 'board_busy_unit_sec', dt * boards.filter(function (b) { return b.busy || b.processing != null; }).length);
    add(metric.stations, 'plating_item_sec', dt * plating.reduce(function (n, p) { return n + (p.items || []).length; }, 0));
    add(metric.stations, 'counter_unit_sec', dt * Math.max(1, counters.length));
    add(metric.stations, 'counter_occupied_unit_sec', dt * counters.filter(function (c) { return (c.items || []).length > 0; }).length);

    Object.keys(inferredCustomers).forEach(function (sid) { inferredCustomers[sid] = Math.max(0, inferredCustomers[sid] - dt); });
    var visible = {};
    stands.forEach(function (s) { if (s.order) visible[s.id] = 1; });
    var occupied = {};
    Object.keys(visible).forEach(function (sid) { occupied[sid] = 1; });
    Object.keys(inferredCustomers).forEach(function (sid) { if (inferredCustomers[sid] > 0) occupied[sid] = 1; });
    var visibleN = Object.keys(visible).length;
    var inferredN = Object.keys(inferredCustomers).filter(function (sid) { return inferredCustomers[sid] > 0; }).length;
    var occupiedN = Object.keys(occupied).length;
    add(metric.stand_pressure, 'visible_order_sec', dt * visibleN);
    add(metric.stand_pressure, 'inferred_customer_sec', dt * inferredN);
    add(metric.stand_pressure, 'occupied_stand_sec', dt * occupiedN);
    add(metric.stand_pressure, 'all_stands_occupied_sec', occupiedN >= 5 ? dt : 0);
    add(metric.stand_pressure, 'no_slot_risk_sec', occupiedN >= 4 ? dt : 0);
  }

  function currentOrders(state) {
    var out = {};
    (state.orders || []).forEach(function (o) { out[orderKey(o)] = clone(o); });
    return out;
  }

  function observeOrders(state) {
    var nowOrders = currentOrders(state);
    Object.keys(nowOrders).forEach(function (id) {
      if (!prevOrders[id]) {
        var o = nowOrders[id];
        recorder.record('order_spawned', tickOf(state), performance.now(), {
          id: o.id, dish: o.dish, stand_id: o.standId, time_left: o.timeLeft, components: o.components
        });
      }
    });
    var disappeared = [];
    Object.keys(prevOrders).forEach(function (id) { if (!nowOrders[id]) disappeared.push(prevOrders[id]); });
    var scoreDelta = prevState ? (state.score || 0) - (prevState.score || 0) : 0;
    var failedDelta = prevState ? (state.failedOrders || 0) - (prevState.failedOrders || 0) : 0;
    var streakUp = prevState ? (state.streak || 0) > (prevState.streak || 0) : false;
    if (disappeared.length && (scoreDelta > 0 || streakUp)) {
      disappeared.forEach(function (o) {
        deliveredCount++;
        inferredCustomers[o.standId] = 10;
        recorder.record('order_delivered', tickOf(state), performance.now(), {
          id: o.id, dish: o.dish, stand_id: o.standId, score_delta: scoreDelta, streak: state.streak
        });
        recorder.record('customer_inferred_start', tickOf(state), performance.now(), {
          stand_id: o.standId, duration_sec: 10, source: 'visible_order_disappeared'
        });
      });
    } else if (disappeared.length && failedDelta > 0) {
      disappeared.forEach(function (o) {
        failureCounts.expired++;
        recorder.record('order_expired', tickOf(state), performance.now(), {
          id: o.id, dish: o.dish, stand_id: o.standId, cause: 'visible_order_disappeared_with_failure'
        });
      });
    } else if (failedDelta > 0) {
      for (var i = 0; i < failedDelta; i++) {
        failureCounts.no_slot++;
        recorder.record('no_slot_failure', tickOf(state), performance.now(), { cause: 'failedOrders_increment_without_visible_expiry' });
      }
    }
    prevOrders = nowOrders;
  }

  function monitorArrivals(state) {
    (state.chefs || []).forEach(function (chef) {
      var active = activeCommands[chef.id];
      if (!active) return;
      if (chef.hasPath) return;
      recorder.record('command_arrival', tickOf(state), performance.now(), {
        chef_id: chef.id,
        target_id: active.target_id,
        ticks_since_command: tickOf(state) - active.tick,
        expected_min_ticks: null,
        holding_before: active.holding_before,
        holding_after: clone(chef.holding)
      });
      if (active.target_id.indexOf('trash') === 0 && active.holding_before && !chef.holding) {
        recorder.record('component_waste', tickOf(state), performance.now(), {
          chef_id: chef.id,
          reason: 'trash',
          item: active.holding_before,
          component_count: itemCount(active.holding_before)
        });
      }
      if (active.target_id.indexOf('reception') === 0 && isPlate(active.holding_before) && !chef.holding) {
        var deliveredRecently = recorder.events.slice(-8).some(function (e) { return e.type === 'order_delivered'; });
        if (!deliveredRecently) {
          failureCounts.wrong++;
          recorder.record('order_failed', tickOf(state), performance.now(), { cause: 'plate_cleared_at_reception_without_delivery' });
          recorder.record('component_waste', tickOf(state), performance.now(), {
            chef_id: chef.id,
            reason: 'wrong_delivery',
            item: active.holding_before,
            component_count: itemCount(active.holding_before)
          });
        }
      }
      activeCommands[chef.id] = null;
    });
  }

  function resetForSeed(reason, line) {
    seedStarts++;
    lastSeedLine = line || lastSeedLine;
    activeCommands = {};
    inferredCustomers = {};
    prevState = null;
    prevOrders = {};
    blockedMsByChef = {};
    if (reservations) reservations.reset();
    if (executor) executor.holdForRestart(cfg.seedSettleMs);
    if (recorder) recorder.record('sim_seed_restart_detected', 0, performance.now(), {
      reason: reason || 'seed_restart',
      line: line || null,
      seed_start_count: seedStarts,
      hold_ms: cfg.seedSettleMs
    });
  }

  function installConsoleHook() {
    if (originalConsoleInfo) return;
    originalConsoleInfo = console.info;
    console.info = function () {
      var text = Array.prototype.slice.call(arguments).join(' ');
      if (text.indexOf('[sim] started with seed') >= 0) resetForSeed('console_seed_line', text);
      return originalConsoleInfo.apply(console, arguments);
    };
  }

  function wrapApi() {
    if (!originalCommand) originalCommand = API.command;
    if (!originalBoost) originalBoost = API.boost;
    if (API.__chefM1Wrapped) return;
    API.command = function (chefId, targetId) {
      var state = API.getState();
      var chef = (state.chefs || []).find(function (c) { return c.id === chefId; });
      var prev = activeCommands[chefId];
      recorder.record('command_attempt', tickOf(state), performance.now(), {
        chef_id: chefId,
        target_id: targetId,
        reason: 'external_api_wrapper',
        executor_state_before: prev ? 'MOVING' : 'IDLE',
        chef_pos: chef && chef.pos || null,
        holding: clone(chef && chef.holding),
        reservation_ids: [],
        duplicate: !!(prev && prev.target_id === targetId),
        retarget: !!(prev && prev.target_id !== targetId)
      });
      var result = originalCommand.call(API, chefId, targetId);
      recorder.record('command_result', tickOf(state), performance.now(), {
        chef_id: chefId,
        target_id: targetId,
        success: !!(result && result.success),
        error: result && result.error || null,
        executor_state_after: result && result.success ? 'COMMAND_SENT' : 'ERROR'
      });
      if (result && result.success) {
        activeCommands[chefId] = {
          target_id: targetId,
          tick: tickOf(state),
          holding_before: clone(chef && chef.holding)
        };
      }
      return result;
    };
    API.boost = function (chefId) {
      var state = API.getState();
      recorder.record('boost_attempt', tickOf(state), performance.now(), { chef_id: chefId, reason: 'external_api_wrapper' });
      var result = originalBoost.call(API, chefId);
      recorder.record('boost_result', tickOf(state), performance.now(), {
        chef_id: chefId,
        success: !!(result && result.success),
        error: result && result.error || null
      });
      return result;
    };
    API.__chefM1Wrapped = true;
  }

  function unwrapApi() {
    if (originalCommand) API.command = originalCommand;
    if (originalBoost) API.boost = originalBoost;
    API.__chefM1Wrapped = false;
  }

  function poll() {
    var state = API.getState();
    var now = performance.now();
    if (!recorder) return;
    if (!prevState && state.running) {
      recorder.sampleState(state, executor && executor.snapshot(), reservations && reservations.snapshot());
      prevOrders = currentOrders(state);
    }
    if (prevState && state.time + 0.25 < prevState.time) resetForSeed('state_time_rewind', null);
    if (prevState) {
      var dt = Math.max(0, Number(state.time || 0) - Number(prevState.time || 0));
      if (dt > 0 && dt < 5) updateMetric(state, dt);
    }
    observeOrders(state);
    monitorArrivals(state);
    if (executor) executor.tick(state);
    if (reservations) reservations.expire(tickOf(state));
    if (now - lastMetricMs >= cfg.metricSampleMs) {
      recorder.record('metric_sample', tickOf(state), now, { interval: clone(metric), time_sec: state.time });
      resetMetric();
      lastMetricMs = now;
    }
    if (now - lastStateMs >= cfg.stateSampleMs) {
      recorder.sampleState(state, executor && executor.snapshot(), reservations && reservations.snapshot());
      lastStateMs = now;
    }
    if (state.gameOver && running) stop(true);
    prevState = clone(state);
  }

  function majorFailureReason() {
    var best = 'unknown';
    var val = -1;
    Object.keys(failureCounts).forEach(function (k) {
      if (failureCounts[k] > val) { val = failureCounts[k]; best = k; }
    });
    return val > 0 ? best : 'unknown';
  }

  function start(options) {
    options = options || {};
    if (running) return controller;
    cfg = Object.assign(cfg, options);
    recorder = new Recorder({ source: 'local_browser', agentVersion: 'milestone1-browser', maxBytes: cfg.maxBytes });
    reservations = new ReservationManager(recorder);
    executor = new Executor(API, recorder, reservations, { blockedTicks: Math.ceil(cfg.blockedMs / cfg.pollMs) });
    resetMetric();
    deliveredCount = 0;
    failureCounts = { expired: 0, no_slot: 0, wrong: 0 };
    activeCommands = {};
    inferredCustomers = {};
    prevState = null;
    prevOrders = {};
    seedStarts = 0;
    recorder.start(API.getState(), { notes: 'Pasted browser trace; interactions inferred from public state and command arrivals.' });
    installConsoleHook();
    wrapApi();
    executor.holdForRestart(cfg.seedSettleMs);
    timer = setInterval(poll, cfg.pollMs);
    running = true;
    console.log('[ChefM1] telemetry started. Planner.js may still use KitchenAPI.run(). Use ChefM1.download() at the end.');
    return controller;
  }

  function stop(restore) {
    if (!recorder) return controller;
    var state = clone(API.getState());
    state.__m1_delivered = deliveredCount;
    if (timer) clearInterval(timer);
    timer = null;
    running = false;
    recorder.end(state, majorFailureReason());
    if (restore !== false) unwrapApi();
    console.log('[ChefM1] telemetry stopped.', recorder.compactSummary ? recorder.compactSummary() : '');
    return controller;
  }

  var controller = {
    start: start,
    stop: stop,
    download: function () {
      if (running) stop(true);
      recorder.download('trace.jsonl');
    },
    copySummary: function () { return recorder && recorder.copySummary(); },
    summary: function () { return recorder && recorder.compactSummary ? recorder.compactSummary() : null; },
    get recorder() { return recorder; },
    get reservations() { return reservations; },
    get executor() { return executor; }
  };

  window.ChefM1 = controller;
  start();
  return controller;
})();
