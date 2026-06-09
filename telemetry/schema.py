"""Trace schema helpers.

The browser and sim record the same JSONL envelope:

    {"type": "event_name", "tick": 123, "ms": 456.7, "seq": 9, "data": {...}}

This module deliberately keeps validation lightweight.  It catches malformed
traces without turning telemetry into a second game engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = "telemetry.v1"
METRICS_SCHEMA_VERSION = "metrics.v1"
PARITY_SCHEMA_VERSION = "parity.v1"

EVENT_TYPES = {
    "trace_start",
    "trace_end",
    "state_sample",
    "phase_changed",
    "rush_changed",
    "order_spawned",
    "order_expired",
    "order_delivered",
    "order_failed",
    "no_slot_failure",
    "customer_inferred_start",
    "customer_inferred_end",
    "chef_state_changed",
    "executor_decision",
    "command_attempt",
    "command_result",
    "command_arrival",
    "command_timeout",
    "command_cancelled",
    "reservation_created",
    "reservation_released",
    "reservation_expired",
    "reservation_violation",
    "path_blocked",
    "stall_detected",
    "boost_attempt",
    "boost_result",
    "metric_sample",
    "component_waste",
    "sim_seed_restart_detected",
    "telemetry_dropped",
    "error",
}

CHEF_STATES = {
    "IDLE",
    "ASSIGNED",
    "COMMAND_SENT",
    "MOVING",
    "ARRIVED",
    "INTERACTING",
    "PROCESSING",
    "WAITING",
    "RECOVERING",
    "BLOCKED",
    "CANCELLED",
    "ERROR",
}

RESERVATION_KINDS = {
    "chef",
    "station",
    "approach_tile",
    "counter",
    "plate",
    "stand",
    "order",
}

RESERVATION_STATUSES = {"active", "released", "expired", "violated"}


@dataclass(frozen=True)
class ValidationError:
    index: int
    message: str
    event_type: str | None = None


def make_event(event_type: str, tick: int, ms: float, seq: int, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": event_type,
        "tick": int(tick),
        "ms": float(ms),
        "seq": int(seq),
        "data": dict(data or {}),
    }


def validate_event(event: Any, index: int = 0) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if not isinstance(event, dict):
        return [ValidationError(index, "event is not an object")]

    event_type = event.get("type")
    if not isinstance(event_type, str):
        errors.append(ValidationError(index, "type must be a string", None))
    elif event_type not in EVENT_TYPES:
        errors.append(ValidationError(index, f"unknown event type {event_type!r}", event_type))

    tick = event.get("tick")
    if not isinstance(tick, int) or tick < 0:
        errors.append(ValidationError(index, "tick must be a non-negative integer", event_type))

    ms = event.get("ms")
    if not isinstance(ms, (int, float)) or ms < 0:
        errors.append(ValidationError(index, "ms must be a non-negative number", event_type))

    seq = event.get("seq")
    if not isinstance(seq, int) or seq < 0:
        errors.append(ValidationError(index, "seq must be a non-negative integer", event_type))

    if not isinstance(event.get("data"), dict):
        errors.append(ValidationError(index, "data must be an object", event_type))

    if event_type == "trace_start":
        schema_version = (event.get("data") or {}).get("schema_version")
        if schema_version != SCHEMA_VERSION:
            errors.append(ValidationError(index, "trace_start has wrong schema_version", event_type))

    if event_type == "chef_state_changed":
        data = event.get("data") or {}
        if data.get("to_state") not in CHEF_STATES:
            errors.append(ValidationError(index, "invalid to_state", event_type))

    if event_type and event_type.startswith("reservation_"):
        data = event.get("data") or {}
        kind = data.get("kind")
        status = data.get("status")
        if kind is not None and kind not in RESERVATION_KINDS:
            errors.append(ValidationError(index, "invalid reservation kind", event_type))
        if status is not None and status not in RESERVATION_STATUSES:
            errors.append(ValidationError(index, "invalid reservation status", event_type))

    return errors


def validate_trace_events(events: list[dict[str, Any]]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    prev_seq = -1
    prev_tick = -1
    seen_start = False
    seen_end = False

    for index, event in enumerate(events):
        errors.extend(validate_event(event, index))
        seq = event.get("seq")
        tick = event.get("tick")
        if isinstance(seq, int):
            if seq <= prev_seq:
                errors.append(ValidationError(index, "seq must be strictly increasing", event.get("type")))
            prev_seq = seq
        if isinstance(tick, int):
            if tick < prev_tick:
                errors.append(ValidationError(index, "tick must be nondecreasing", event.get("type")))
            prev_tick = tick
        if event.get("type") == "trace_start":
            seen_start = True
        if event.get("type") == "trace_end":
            seen_end = True

    if not seen_start:
        errors.append(ValidationError(-1, "missing trace_start"))
    if not seen_end:
        errors.append(ValidationError(-1, "missing trace_end"))
    return errors

