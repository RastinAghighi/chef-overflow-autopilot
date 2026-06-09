"""Human-readable trace summaries."""

from __future__ import annotations

from .metrics import compute_metrics, merge_metric_dicts
from .storage import Trace


def summarize_trace(trace: Trace) -> dict:
    return compute_metrics(trace)


def summarize_many(traces: list[Trace]) -> dict:
    return merge_metric_dicts([compute_metrics(t) for t in traces])


def format_report(metrics: dict) -> str:
    summary = metrics.get("summary") or {}
    chef = (metrics.get("chef_utilization") or {}).get("aggregate") or {}
    station = metrics.get("station_utilization") or {}
    stand = metrics.get("stand_pressure") or {}
    orders = metrics.get("orders") or {}
    pathing = metrics.get("pathing") or {}
    throughput = metrics.get("throughput") or {}
    waste = metrics.get("waste") or {}
    executor = metrics.get("executor") or {}
    reservations = metrics.get("reservations") or {}
    planner = metrics.get("planner") or {}

    lines = []
    if "runs" in metrics:
        lines.append(f"Runs: {metrics['runs']}")
        lines.append(
            f"Score mean/median/min/max: {summary.get('score_mean', 0):.1f} / "
            f"{summary.get('score_median', 0):.1f} / {summary.get('score_min', 0):.1f} / {summary.get('score_max', 0):.1f}"
        )
        lines.append(f"Survival mean: {summary.get('survival_mean_sec', 0):.1f}s")
    else:
        lines.append(f"Trace: {summary.get('trace_id', 'unknown')} source={summary.get('source', 'unknown')}")
        lines.append(
            f"Score {summary.get('score', 0)} | delivered {summary.get('delivered', 0)} | "
            f"best streak {summary.get('best_streak', 0)} | survival {summary.get('time_sec', 0):.1f}s"
        )

    lines.append(
        "Chef aggregate: "
        f"idle {chef.get('idle_pct', 0) * 100:.1f}% | "
        f"moving {chef.get('moving_pct', 0) * 100:.1f}% | "
        f"blocked {chef.get('blocked_pct', 0) * 100:.1f}% | "
        f"processing {chef.get('processing_pct', 0) * 100:.1f}% | "
        f"stall {chef.get('stall_pct', 0) * 100:.1f}%"
    )
    lines.append(
        "Stations: "
        f"stove busy {station.get('stove_busy_pct', 0) * 100:.1f}% | "
        f"all stoves cold while cook needed {station.get('all_stoves_cold_while_cook_needed_pct', 0) * 100:.1f}% | "
        f"board busy {station.get('board_busy_pct', 0) * 100:.1f}% | "
        f"counter occupied {station.get('counter_occupied_pct', 0) * 100:.1f}%"
    )
    lines.append(
        "Stand pressure: "
        f"avg visible orders {stand.get('avg_visible_orders', 0):.2f} | "
        f"avg inferred customers {stand.get('avg_inferred_customers', 0):.2f} | "
        f"avg occupied stands {stand.get('avg_occupied_stands', 0):.2f} | "
        f"no-slot risk {stand.get('no_slot_risk_pct', 0) * 100:.1f}%"
    )
    lines.append(
        "Failures: "
        f"expired {orders.get('expired', 0)} | no-slot {orders.get('no_slot', 0)} | "
        f"wrong {orders.get('failed_wrong', 0)}"
    )
    lines.append(
        "Throughput: "
        f"{throughput.get('deliveries_per_min', 0):.2f} deliveries/min | "
        f"{throughput.get('score_per_min', 0):.1f} score/min"
    )
    lines.append(
        "Commands/reservations: "
        f"attempts {executor.get('command_attempts', 0):.1f} | "
        f"failures {executor.get('command_failures', 0):.1f} | "
        f"duplicates {executor.get('duplicate_commands', 0):.1f} | "
        f"retargets {executor.get('retargets', 0):.1f} | "
        f"reservation conflicts {reservations.get('conflicts', 0):.1f}"
    )
    if planner:
        features = planner.get("features") or {}
        enabled = ",".join(k for k, v in sorted(features.items()) if v) or "baseline"
        lines.append(
            "Planner features: "
            f"{enabled} | build-ahead triggers {planner.get('buildahead_triggers', 0)} | "
            f"build-ahead completions {planner.get('buildahead_completions', 0)} | "
            f"helper assignments {planner.get('helper_assignments', 0)}"
        )
    lines.append(
        f"Waste: components {waste.get('component_count', 0)} | plates {waste.get('plate_count', 0)}"
    )
    top_blocked = list((pathing.get("blocked_locations") or {}).items())[:8]
    if top_blocked:
        lines.append("Top blocked locations: " + ", ".join(f"{loc}={sec:.1f}s" for loc, sec in top_blocked))
    return "\n".join(lines)
