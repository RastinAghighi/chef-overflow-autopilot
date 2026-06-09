/* Chef Overflow telemetry schema constants. */
(function (root) {
  'use strict';

  var Schema = {
    SCHEMA_VERSION: 'telemetry.v1',
    METRICS_SCHEMA_VERSION: 'metrics.v1',
    PARITY_SCHEMA_VERSION: 'parity.v1',
    CHEF_STATES: [
      'IDLE', 'ASSIGNED', 'COMMAND_SENT', 'MOVING', 'ARRIVED', 'INTERACTING',
      'PROCESSING', 'WAITING', 'RECOVERING', 'BLOCKED', 'CANCELLED', 'ERROR'
    ],
    RESERVATION_KINDS: ['chef', 'station', 'approach_tile', 'counter', 'plate', 'stand', 'order'],
    RESERVATION_STATUSES: ['active', 'released', 'expired', 'violated']
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = Schema;
  root.ChefTelemetrySchema = Schema;
})(typeof window !== 'undefined' ? window : globalThis);

