import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.schema import SCHEMA_VERSION, make_event, validate_trace_events


def test_valid_minimal_trace_schema():
    events = [
        make_event("trace_start", 0, 0.0, 0, {"schema_version": SCHEMA_VERSION}),
        make_event("trace_end", 1, 16.6, 1, {"score": 0}),
    ]
    assert validate_trace_events(events) == []


def test_schema_rejects_bad_tick_and_missing_end():
    events = [
        {"type": "trace_start", "tick": -1, "ms": 0, "seq": 0, "data": {"schema_version": SCHEMA_VERSION}},
    ]
    errors = validate_trace_events(events)
    assert any("tick" in e.message for e in errors)
    assert any("missing trace_end" in e.message for e in errors)

