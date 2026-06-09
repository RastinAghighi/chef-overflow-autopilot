/* Browser-side bounded telemetry recorder. */
(function (root) {
  'use strict';

  var Schema = root.ChefTelemetrySchema || { SCHEMA_VERSION: 'telemetry.v1' };

  function clone(obj) {
    if (obj == null) return obj;
    try { return JSON.parse(JSON.stringify(obj)); }
    catch (_) { return null; }
  }

  function TelemetryRecorder(opts) {
    opts = opts || {};
    this.traceId = opts.traceId || ('trace-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8));
    this.source = opts.source || 'local_browser';
    this.agentVersion = opts.agentVersion || 'milestone1-browser';
    this.gameUrl = opts.gameUrl || (root.location ? String(root.location.href) : null);
    this.maxBytes = opts.maxBytes || 25 * 1024 * 1024;
    this.events = [];
    this.seq = 0;
    this.bytes = 0;
    this.dropped = { state_sample: 0, metric_sample: 0, other: 0 };
    this.started = false;
  }

  TelemetryRecorder.prototype._estimate = function (event) {
    try { return JSON.stringify(event).length + 1; }
    catch (_) { return 128; }
  };

  TelemetryRecorder.prototype._dropOne = function () {
    var priorities = ['state_sample', 'metric_sample'];
    for (var p = 0; p < priorities.length; p++) {
      var type = priorities[p];
      for (var i = 1; i < this.events.length; i++) {
        if (this.events[i].type === type) {
          this.bytes -= this._estimate(this.events[i]);
          this.events.splice(i, 1);
          this.dropped[type]++;
          return true;
        }
      }
    }
    if (this.events.length > 2) {
      this.bytes -= this._estimate(this.events[1]);
      this.events.splice(1, 1);
      this.dropped.other++;
      return true;
    }
    return false;
  };

  TelemetryRecorder.prototype.record = function (type, tick, ms, data) {
    var event = {
      type: type,
      tick: Math.max(0, Math.floor(tick || 0)),
      ms: Math.max(0, Number(ms || 0)),
      seq: this.seq++,
      data: data || {}
    };
    var size = this._estimate(event);
    while (this.bytes + size > this.maxBytes && this._dropOne()) {}
    if (this.bytes + size <= this.maxBytes || type === 'trace_end') {
      this.events.push(event);
      this.bytes += size;
    } else {
      this.dropped.other++;
    }
  };

  TelemetryRecorder.prototype.start = function (state, extra) {
    if (this.started) return;
    this.started = true;
    extra = extra || {};
    this.record('trace_start', 0, 0, {
      schema_version: Schema.SCHEMA_VERSION,
      trace_id: this.traceId,
      run_id: extra.run_id || null,
      seed: extra.seed == null ? null : extra.seed,
      started_at_iso: new Date().toISOString(),
      source: this.source,
      game_url: this.gameUrl,
      agent_version: this.agentVersion,
      game_api_version: root.KitchenAPI && root.KitchenAPI.version || null,
      seed_known: !!(extra.run_id || extra.seed != null),
      tick_hz: 60,
      notes: extra.notes || 'Pasted console telemetry; browser input log is not directly readable.'
    });
    if (state) this.sampleState(state, null, null);
  };

  TelemetryRecorder.prototype.end = function (state, majorFailureReason) {
    var t = state && Number(state.time || 0) || 0;
    this.record('trace_end', Math.floor(t * 60), performance.now(), {
      score: Math.floor(state && state.score || 0),
      delivered: state && (state.ordersDelivered || state.delivered || 0) || 0,
      best_streak: state && state.bestStreak || 0,
      failed_orders: state && state.failedOrders || 0,
      time_sec: t,
      game_over: !!(state && state.gameOver),
      major_failure_reason: majorFailureReason || 'unknown',
      dropped: clone(this.dropped),
      bytes: this.bytes
    });
  };

  TelemetryRecorder.prototype.sampleState = function (state, executorState, reservations) {
    var s = clone(state || {});
    if (s && executorState) s.executor = clone(executorState);
    if (s && reservations) s.reservations = clone(reservations);
    var t = s && Number(s.time || 0) || 0;
    this.record('state_sample', Math.floor(t * 60), performance.now(), { state: s });
  };

  TelemetryRecorder.prototype.flush = function () {
    return this.events.map(function (e) { return JSON.stringify(e); }).join('\n') + '\n';
  };

  TelemetryRecorder.prototype.download = function (filename) {
    filename = filename || 'trace.jsonl';
    var blob = new Blob([this.flush()], { type: 'application/jsonl' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 1000);
  };

  TelemetryRecorder.prototype.compactSummary = function () {
    var end = null;
    for (var i = this.events.length - 1; i >= 0; i--) {
      if (this.events[i].type === 'trace_end') { end = this.events[i].data; break; }
    }
    return {
      trace_id: this.traceId,
      events: this.events.length,
      bytes: this.bytes,
      dropped: clone(this.dropped),
      final: end || null
    };
  };

  TelemetryRecorder.prototype.copySummary = function () {
    var text = JSON.stringify(this.compactSummary(), null, 2);
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    if (typeof root.copy === 'function') return root.copy(text);
    console.log(text);
    return Promise.resolve();
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = { TelemetryRecorder: TelemetryRecorder };
  root.ChefTelemetryRecorder = TelemetryRecorder;
})(typeof window !== 'undefined' ? window : globalThis);

