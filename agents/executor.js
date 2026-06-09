/* Browser-faithful station-target executor. */
(function (root) {
  'use strict';

  function clone(obj) {
    if (obj == null) return obj;
    try { return JSON.parse(JSON.stringify(obj)); }
    catch (_) { return null; }
  }

  function Executor(api, recorder, reservations, opts) {
    opts = opts || {};
    this.api = api;
    this.recorder = recorder || null;
    this.reservations = reservations || null;
    this.commandTimeoutTicks = opts.commandTimeoutTicks || 60 * 20;
    this.blockedTicks = opts.blockedTicks || 30;
    this.holdUntilMs = 0;
    this.chefs = {};
    for (var i = 0; i < 5; i++) this.chefs[i] = this._fresh(i);
  }

  Executor.prototype._fresh = function (id) {
    return {
      chef_id: id,
      state: 'IDLE',
      target_id: null,
      assigned_tick: null,
      command_tick: null,
      holding_before: null,
      last_pos: null,
      same_pos_ticks: 0,
      reservation_ids: []
    };
  };

  Executor.prototype.reset = function () {
    for (var i = 0; i < 5; i++) this.chefs[i] = this._fresh(i);
  };

  Executor.prototype.snapshot = function () {
    var out = [];
    Object.keys(this.chefs).forEach(function (id) { out.push(clone(this.chefs[id])); }, this);
    return out;
  };

  Executor.prototype._tickOf = function (state) {
    return Math.floor(Number(state && state.time || 0) * 60);
  };

  Executor.prototype._setState = function (rec, toState, state, reason) {
    if (rec.state === toState) return;
    var from = rec.state;
    rec.state = toState;
    if (this.recorder) {
      this.recorder.record('chef_state_changed', this._tickOf(state), performance.now(), {
        chef_id: rec.chef_id,
        from_state: from,
        to_state: toState,
        reason: reason || null,
        target_id: rec.target_id
      });
    }
  };

  Executor.prototype.assign = function (chefId, targetId, intent, opts) {
    opts = opts || {};
    var state = this.api.getState();
    var tick = this._tickOf(state);
    if (performance.now() < this.holdUntilMs) {
      return { success: false, error: 'Executor held for seed/restart stabilization' };
    }
    var chef = (state.chefs || []).find(function (c) { return c.id === chefId; });
    var rec = this.chefs[chefId] || this._fresh(chefId);
    this.chefs[chefId] = rec;
    if (!chef) return { success: false, error: 'Invalid chef_id' };
    if (chef.busy) return { success: false, error: 'Chef is busy' };
    if (rec.state !== 'IDLE' && rec.state !== 'CANCELLED' && rec.state !== 'ERROR') {
      if (rec.target_id === targetId && !opts.allowDuplicate) {
        if (this.recorder) this.recorder.record('executor_decision', tick, performance.now(), {
          chef_id: chefId, kind: 'duplicate_suppressed', target_id: targetId
        });
        return { success: false, error: 'Duplicate active target suppressed' };
      }
      if (!opts.allowRetarget) {
        return { success: false, error: 'Retarget suppressed' };
      }
      this.cancel(chefId, 'retarget');
    }

    rec.target_id = targetId;
    rec.assigned_tick = tick;
    rec.holding_before = clone(chef.holding);
    rec.last_pos = chef.pos && chef.pos.slice();
    rec.same_pos_ticks = 0;
    this._setState(rec, 'ASSIGNED', state, intent && intent.kind || 'assign');

    if (this.reservations) {
      var chefReservation = this.reservations.reserve({
        kind: 'chef', resource_id: String(chefId), chef_id: chefId, purpose: 'executor command', created_tick: tick
      });
      var stationReservation = this.reservations.reserve({
        kind: 'station', resource_id: targetId, chef_id: chefId, purpose: 'executor command', created_tick: tick
      });
      rec.reservation_ids = [chefReservation.id, stationReservation.id];
    }

    if (this.recorder) this.recorder.record('command_attempt', tick, performance.now(), {
      chef_id: chefId,
      target_id: targetId,
      reason: intent && intent.kind || 'executor',
      executor_state_before: 'ASSIGNED',
      chef_pos: chef.pos,
      holding: clone(chef.holding),
      reservation_ids: rec.reservation_ids
    });

    var result = this.api.command(chefId, targetId);
    rec.command_tick = tick;
    if (this.recorder) this.recorder.record('command_result', tick, performance.now(), {
      chef_id: chefId,
      target_id: targetId,
      success: !!(result && result.success),
      error: result && result.error || null,
      executor_state_after: result && result.success ? 'COMMAND_SENT' : 'ERROR'
    });
    if (!(result && result.success)) {
      this._setState(rec, 'ERROR', state, 'command_failed');
      return result;
    }
    this._setState(rec, chef.hasPath ? 'COMMAND_SENT' : 'ARRIVED', state, 'command_sent');
    return result;
  };

  Executor.prototype.cancel = function (chefId, reason) {
    var state = this.api.getState();
    var rec = this.chefs[chefId];
    if (!rec) return;
    if (this.recorder) this.recorder.record('command_cancelled', this._tickOf(state), performance.now(), {
      chef_id: chefId, target_id: rec.target_id, reason: reason || 'cancel'
    });
    if (this.reservations) rec.reservation_ids.forEach(function (id) { this.reservations.release(id, reason || 'cancel'); }, this);
    this._setState(rec, 'CANCELLED', state, reason || 'cancel');
    this.chefs[chefId] = this._fresh(chefId);
  };

  Executor.prototype.tick = function (state) {
    state = state || this.api.getState();
    var tick = this._tickOf(state);
    (state.chefs || []).forEach(function (chef) {
      var rec = this.chefs[chef.id];
      if (!rec || !rec.target_id) return;
      if (chef.busy) {
        this._setState(rec, 'PROCESSING', state, 'busy');
        return;
      }
      if (chef.hasPath) {
        var posKey = String(chef.pos);
        var lastKey = String(rec.last_pos || []);
        if (posKey === lastKey) rec.same_pos_ticks++;
        else rec.same_pos_ticks = 0;
        rec.last_pos = chef.pos && chef.pos.slice();
        if (rec.same_pos_ticks >= this.blockedTicks) this._setState(rec, 'BLOCKED', state, 'position_unchanged');
        else this._setState(rec, 'MOVING', state, 'has_path');
        if (tick - rec.command_tick > this.commandTimeoutTicks && this.recorder) {
          this.recorder.record('command_timeout', tick, performance.now(), {
            chef_id: chef.id, target_id: rec.target_id, ticks_since_command: tick - rec.command_tick
          });
          this._setState(rec, 'ERROR', state, 'timeout');
        }
        return;
      }
      if (rec.command_tick != null) {
        if (this.recorder) this.recorder.record('command_arrival', tick, performance.now(), {
          chef_id: chef.id,
          target_id: rec.target_id,
          ticks_since_command: tick - rec.command_tick,
          expected_min_ticks: null,
          holding_before: rec.holding_before,
          holding_after: clone(chef.holding)
        });
        if (this.reservations) rec.reservation_ids.forEach(function (id) { this.reservations.release(id, 'arrival'); }, this);
        this.chefs[chef.id] = this._fresh(chef.id);
      }
    }, this);
  };

  Executor.prototype.holdForRestart = function (ms) {
    this.holdUntilMs = performance.now() + (ms || 1500);
    this.reset();
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = { Executor: Executor };
  root.ChefExecutor = Executor;
})(typeof window !== 'undefined' ? window : globalThis);

