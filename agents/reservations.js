/* Browser-side deterministic reservation manager. */
(function (root) {
  'use strict';

  var HARD = { chef: 1, station: 1, counter: 1, plate: 1, stand: 1, order: 1 };

  function ReservationManager(recorder) {
    this.recorder = recorder || null;
    this.nextId = 1;
    this.items = {};
  }

  ReservationManager.prototype._now = function () {
    var s = root.KitchenAPI && root.KitchenAPI.getState ? root.KitchenAPI.getState() : { time: 0 };
    return { tick: Math.floor(Number(s.time || 0) * 60), ms: performance.now() };
  };

  ReservationManager.prototype.active = function () {
    var out = [];
    Object.keys(this.items).forEach(function (id) {
      if (this.items[id].status === 'active') out.push(this.items[id]);
    }, this);
    return out;
  };

  ReservationManager.prototype.conflicts = function (kind, resourceId) {
    return this.active().filter(function (r) {
      return r.kind === kind && r.resource_id === resourceId;
    });
  };

  ReservationManager.prototype.reserve = function (req) {
    var now = this._now();
    var id = 'r' + this.nextId++;
    var ttl = Math.max(1, req.ttl_ticks || 60 * 30);
    var conflicts = this.conflicts(req.kind, req.resource_id);
    var r = {
      id: id,
      owner: req.owner || 'executor',
      chef_id: req.chef_id == null ? null : req.chef_id,
      kind: req.kind,
      resource_id: String(req.resource_id),
      order_id: req.order_id == null ? null : req.order_id,
      purpose: req.purpose || '',
      created_tick: req.created_tick == null ? now.tick : req.created_tick,
      expires_tick: (req.created_tick == null ? now.tick : req.created_tick) + ttl,
      status: 'active'
    };
    this.items[id] = r;
    if (this.recorder) {
      var data = Object.assign({ conflict: conflicts.length > 0 }, r);
      if (conflicts.length) data.conflicts = conflicts.map(function (c) { return c.id; });
      this.recorder.record('reservation_created', now.tick, now.ms, data);
      if (HARD[req.kind]) {
        conflicts.forEach(function (c) {
          this.recorder.record('reservation_violation', now.tick, now.ms, Object.assign({}, c, {
            reason: 'double_booking',
            conflict_with: id
          }));
        }, this);
      }
    }
    return r;
  };

  ReservationManager.prototype.release = function (id, reason) {
    var r = this.items[id];
    if (!r || r.status !== 'active') return null;
    r.status = 'released';
    r.release_reason = reason || 'release';
    if (this.recorder) {
      var now = this._now();
      this.recorder.record('reservation_released', now.tick, now.ms, r);
    }
    return r;
  };

  ReservationManager.prototype.expire = function (tick) {
    var now = this._now();
    var expired = [];
    tick = tick == null ? now.tick : tick;
    this.active().forEach(function (r) {
      if (r.expires_tick <= tick) {
        r.status = 'expired';
        expired.push(r);
        if (this.recorder) this.recorder.record('reservation_expired', tick, now.ms, r);
      }
    }, this);
    return expired;
  };

  ReservationManager.prototype.snapshot = function () {
    return { active: this.active().map(function (r) { return Object.assign({}, r); }) };
  };

  ReservationManager.prototype.reset = function () {
    this.items = {};
    this.nextId = 1;
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = { ReservationManager: ReservationManager };
  root.ChefReservationManager = ReservationManager;
})(typeof window !== 'undefined' ? window : globalThis);

