import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.metrics import compute_metrics
from telemetry.schema import SCHEMA_VERSION, make_event
from telemetry.storage import Trace


def test_metrics_from_interval_samples():
    events = [
        make_event("trace_start", 0, 0, 0, {"schema_version": SCHEMA_VERSION, "trace_id": "t", "source": "sim"}),
        make_event("order_spawned", 60, 1000, 1, {"id": 1, "dish": "Steak"}),
        make_event("metric_sample", 120, 2000, 2, {
            "interval": {
                "chefs": {"0": {"idle_sec": 1, "moving_sec": 1, "blocked_sec": 0.5}},
                "stations": {
                    "stove_unit_sec": 6,
                    "stove_busy_unit_sec": 2,
                    "cook_demand_sec": 2,
                    "all_stoves_cold_cook_needed_sec": 1,
                    "board_unit_sec": 4,
                    "board_busy_unit_sec": 1,
                    "counter_unit_sec": 10,
                    "counter_occupied_unit_sec": 2,
                },
                "stand_pressure": {
                    "visible_order_sec": 2,
                    "inferred_customer_sec": 1,
                    "occupied_stand_sec": 3,
                    "no_slot_risk_sec": 0.5,
                },
            }
        }),
        make_event("order_delivered", 180, 3000, 3, {"id": 1, "score_delta": 100, "stand_id": "reception_0"}),
        make_event("trace_end", 240, 4000, 4, {"score": 100, "delivered": 1, "best_streak": 1, "time_sec": 4}),
    ]
    metrics = compute_metrics(Trace(path=None, events=events))
    assert metrics["orders"]["latency_sec_mean"] == 2
    assert metrics["station_utilization"]["stove_busy_pct"] == 2 / 6
    assert metrics["station_utilization"]["all_stoves_cold_while_cook_needed_pct"] == 0.5
    assert metrics["stand_pressure"]["avg_occupied_stands"] == 3 / 4
    assert metrics["throughput"]["deliveries_per_min"] == 15

