import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.parity import compare_trace_to_replay, extract_inferred_interactions
from telemetry.schema import SCHEMA_VERSION, make_event
from telemetry.storage import Trace


def test_extract_inferred_interactions_from_command_arrivals():
    trace = Trace(path=None, events=[
        make_event("trace_start", 0, 0, 0, {"schema_version": SCHEMA_VERSION}),
        make_event("command_arrival", 100, 1000, 1, {"chef_id": 2, "target_id": "stove_0"}),
        make_event("trace_end", 101, 1010, 2, {}),
    ])
    assert extract_inferred_interactions(trace) == [
        {"tick": 100, "type": "interact", "chefId": 2, "stationId": "stove_0"}
    ]


def test_summary_parity_uses_tolerance():
    trace = Trace(path=None, events=[
        make_event("trace_start", 0, 0, 0, {"schema_version": SCHEMA_VERSION}),
        make_event("trace_end", 60, 1000, 1, {"score": 1000, "delivered": 5, "best_streak": 4}),
    ])
    report = compare_trace_to_replay(
        trace,
        {"replay_available": True, "inputs_inferred": True, "summary": {"score": 1003, "delivered": 6, "bestStreak": 4}},
        score_tolerance=5,
        delivered_tolerance=1,
        streak_tolerance=0,
    )
    assert report["mismatches"] == []

