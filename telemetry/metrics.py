"""Metric computation for Chef Overflow traces."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any

from .schema import METRICS_SCHEMA_VERSION
from .storage import Trace


CHEF_BUCKETS = ("idle", "moving", "blocked", "processing", "stall", "holding")


def _seconds_from_tick(tick: int | float | None) -> float:
    return max(0.0, float(tick or 0) / 60.0)


def _event_sec(event: dict[str, Any]) -> float:
    data = event.get("data") or {}
    if "time_sec" in data:
        return float(data["time_sec"])
    tick = event.get("tick")
    return _seconds_from_tick(tick if isinstance(tick, (int, float)) else 0)


def _state_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") or {}
    state = data.get("state")
    return state if isinstance(state, dict) else data


def _pos_key(pos: Any) -> str:
    if not isinstance(pos, (list, tuple)) or len(pos) < 2:
        return "unknown"
    return f"{pos[0]},{pos[1]}"


def _is_plate(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "plate"


def _component_key(item: Any) -> str:
    if not isinstance(item, dict):
        return "unknown"
    if _is_plate(item):
        return "plate"
    return f"{item.get('ingredient', 'unknown')}:{item.get('state', 'unknown')}"


def _cook_demand_visible(state: dict[str, Any]) -> bool:
    for order in state.get("orders", []) or []:
        for comp in order.get("components", []) or []:
            if comp.get("state") == "cooked":
                return True
    for order in state.get("upcomingOrders", []) or []:
        for comp in order.get("components", []) or []:
            if comp.get("state") == "cooked":
                return True
    for chef in state.get("chefs", []) or []:
        h = chef.get("holding")
        if isinstance(h, dict) and h.get("state") == "raw" and h.get("ingredient") in {"meat", "dough"}:
            return True
    return False


def _station_section(stations: dict[str, Any], dt: float, state: dict[str, Any]) -> dict[str, float]:
    stoves = stations.get("stoves", []) or []
    boards = stations.get("cuttingBoards", []) or []
    plating = stations.get("platingAreas", []) or []
    counters = stations.get("counters", []) or []
    stands = stations.get("receptionStands", []) or []

    stove_busy = sum(1 for s in stoves if s.get("cooking") is not None)
    stove_ready = sum(1 for s in stoves if s.get("ready"))
    all_stoves_cold = len(stoves) > 0 and stove_busy == 0
    cook_demand = _cook_demand_visible(state)

    board_busy = sum(1 for b in boards if b.get("busy") or b.get("processing") is not None)
    plating_items = sum(len(p.get("items", []) or []) for p in plating)
    occupied_counters = sum(1 for c in counters if len(c.get("items", []) or []) > 0)
    visible_orders = sum(1 for s in stands if s.get("order") is not None)

    return {
        "stove_unit_sec": dt * max(1, len(stoves)),
        "stove_busy_unit_sec": dt * stove_busy,
        "stove_ready_unit_sec": dt * stove_ready,
        "cook_demand_sec": dt if cook_demand else 0.0,
        "all_stoves_cold_cook_needed_sec": dt if cook_demand and all_stoves_cold else 0.0,
        "board_unit_sec": dt * max(1, len(boards)),
        "board_busy_unit_sec": dt * board_busy,
        "plating_item_sec": dt * plating_items,
        "counter_unit_sec": dt * max(1, len(counters)),
        "counter_occupied_unit_sec": dt * occupied_counters,
        "visible_order_stand_sec": dt * visible_orders,
    }


def _stand_pressure_section(state: dict[str, Any], inferred_customers: dict[str, float], dt: float) -> dict[str, float]:
    stands = ((state.get("stations") or {}).get("receptionStands") or [])
    visible = {s.get("id") for s in stands if s.get("order") is not None}
    inferred = {sid for sid, left in inferred_customers.items() if left > 0}
    occupied = visible | inferred
    return {
        "visible_order_sec": dt * len(visible),
        "inferred_customer_sec": dt * len(inferred),
        "occupied_stand_sec": dt * len(occupied),
        "all_stands_occupied_sec": dt if len(occupied) >= 5 else 0.0,
        "no_slot_risk_sec": dt if len(occupied) >= 4 else 0.0,
    }


def _classify_chef(chef: dict[str, Any], prev: dict[str, Any] | None) -> tuple[str, bool]:
    if chef.get("busy"):
        return "processing", False
    if float(chef.get("stall") or 0) > 0:
        return "stall", False
    if chef.get("hasPath"):
        pos = tuple(chef.get("pos", (None, None)))
        prev_pos = tuple(prev.get("pos", (None, None))) if prev else None
        if prev_pos == pos:
            return "blocked", True
        return "moving", False
    return "idle", False


def _add_counter(dst: dict[str, float], src: dict[str, Any]) -> None:
    for key, value in (src or {}).items():
        if isinstance(value, (int, float)):
            dst[key] = dst.get(key, 0.0) + float(value)


def _pct(num: float, denom: float) -> float:
    return float(num / denom) if denom > 0 else 0.0


def _base_metrics() -> dict[str, Any]:
    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "time_sec": 0.0,
        "throughput": {
            "deliveries_per_min": 0.0,
            "score_per_min": 0.0,
            "by_minute": {},
        },
        "chef_utilization": {
            "per_chef": {},
            "aggregate": {},
        },
        "station_utilization": {},
        "stand_pressure": {},
        "pathing": {
            "blocked_events": 0,
            "blocked_locations": {},
        },
        "executor": {
            "command_attempts": 0,
            "command_successes": 0,
            "command_failures": 0,
            "failures_by_error": {},
            "duplicate_commands": 0,
            "retargets": 0,
            "timeouts": 0,
            "recoveries": 0,
            "arrivals": 0,
            "avg_command_travel_ticks": 0.0,
        },
        "reservations": {
            "created": 0,
            "released": 0,
            "expired": 0,
            "violated": 0,
            "conflicts": 0,
        },
        "orders": {
            "spawned": 0,
            "delivered": 0,
            "expired": 0,
            "failed_wrong": 0,
            "no_slot": 0,
            "latency_sec_median": 0.0,
            "latency_sec_mean": 0.0,
        },
        "waste": {
            "component_count": 0,
            "plate_count": 0,
            "items_by_kind": {},
        },
        "survival": {
            "time_sec": 0.0,
            "game_over": False,
        },
        "streak": {
            "best": 0,
            "progression": [],
        },
    }


def compute_summary(trace: Trace) -> dict[str, Any]:
    start = next((e for e in trace.events if e.get("type") == "trace_start"), None)
    end = next((e for e in reversed(trace.events) if e.get("type") == "trace_end"), None)
    state = None
    for event in reversed(trace.events):
        if event.get("type") == "state_sample":
            state = _state_data(event)
            break

    end_data = (end or {}).get("data", {}) if end else {}
    state = state or {}
    return {
        "trace_id": ((start or {}).get("data") or {}).get("trace_id"),
        "source": ((start or {}).get("data") or {}).get("source"),
        "score": int(math.floor(end_data.get("score", state.get("score", 0)) or 0)),
        "delivered": int(end_data.get("delivered", state.get("delivered", state.get("ordersDelivered", 0)) or 0)),
        "best_streak": int(end_data.get("best_streak", state.get("bestStreak", 0)) or 0),
        "failed_orders": int(end_data.get("failed_orders", state.get("failedOrders", 0)) or 0),
        "time_sec": float(end_data.get("time_sec", state.get("time", 0)) or 0),
        "game_over": bool(end_data.get("game_over", state.get("gameOver", False))),
        "event_count": len(trace.events),
        "command_count": sum(1 for e in trace.events if e.get("type") == "command_attempt"),
        "command_success_rate": 0.0,
        "major_failure_reason": end_data.get("major_failure_reason", "unknown"),
    }


def compute_metrics(trace: Trace) -> dict[str, Any]:
    metrics = _base_metrics()
    summary = compute_summary(trace)
    metrics["time_sec"] = summary["time_sec"]
    metrics["survival"]["time_sec"] = summary["time_sec"]
    metrics["survival"]["game_over"] = summary["game_over"]
    metrics["streak"]["best"] = summary["best_streak"]

    command_travels: list[int] = []
    failures_by_error: Counter[str] = Counter()
    blocked_locations: Counter[str] = Counter()
    waste_by_kind: Counter[str] = Counter()
    order_spawn_ticks: dict[Any, int] = {}
    order_latencies: list[float] = []
    by_minute: dict[int, dict[str, float]] = defaultdict(lambda: {"delivered": 0, "score_delta": 0.0})

    chef_seconds: dict[str, Counter[str]] = defaultdict(Counter)
    station_seconds: Counter[str] = Counter()
    stand_seconds: Counter[str] = Counter()
    inferred_customers: dict[str, float] = {}
    prev_state: dict[str, Any] | None = None
    prev_time: float | None = None
    used_metric_samples = False

    for event in trace.events:
        etype = event.get("type")
        data = event.get("data") or {}

        if etype == "metric_sample":
            used_metric_samples = True
            interval = data.get("interval") or data
            for cid, vals in (interval.get("chefs") or {}).items():
                for bucket in CHEF_BUCKETS:
                    chef_seconds[str(cid)][bucket] += float(vals.get(f"{bucket}_sec", vals.get(bucket, 0.0)) or 0.0)
                for loc, sec in (vals.get("blocked_locations") or {}).items():
                    blocked_locations[str(loc)] += float(sec)
            _add_counter(station_seconds, interval.get("stations") or {})
            _add_counter(stand_seconds, interval.get("stand_pressure") or {})
            continue

        if etype == "state_sample" and not used_metric_samples:
            state = _state_data(event)
            current_time = float(state.get("time", _event_sec(event)) or 0.0)
            if prev_state is not None and prev_time is not None:
                dt = max(0.0, current_time - prev_time)
                for chef in state.get("chefs", []) or []:
                    cid = str(chef.get("id"))
                    prev_chef = next((c for c in prev_state.get("chefs", []) or [] if c.get("id") == chef.get("id")), None)
                    bucket, blocked = _classify_chef(chef, prev_chef)
                    chef_seconds[cid][bucket] += dt
                    if chef.get("holding") is not None:
                        chef_seconds[cid]["holding"] += dt
                    if blocked:
                        blocked_locations[_pos_key(chef.get("pos"))] += dt
                _add_counter(station_seconds, _station_section(state.get("stations") or {}, dt, state))
                for sid in list(inferred_customers):
                    inferred_customers[sid] = max(0.0, inferred_customers[sid] - dt)
                _add_counter(stand_seconds, _stand_pressure_section(state, inferred_customers, dt))
            prev_state = state
            prev_time = current_time

        if etype == "command_attempt":
            metrics["executor"]["command_attempts"] += 1
            if data.get("duplicate"):
                metrics["executor"]["duplicate_commands"] += 1
            if data.get("retarget"):
                metrics["executor"]["retargets"] += 1
        elif etype == "command_result":
            if data.get("success"):
                metrics["executor"]["command_successes"] += 1
            else:
                metrics["executor"]["command_failures"] += 1
                failures_by_error[str(data.get("error") or "unknown")] += 1
        elif etype == "command_arrival":
            metrics["executor"]["arrivals"] += 1
            if isinstance(data.get("ticks_since_command"), int):
                command_travels.append(data["ticks_since_command"])
        elif etype == "command_timeout":
            metrics["executor"]["timeouts"] += 1
        elif etype == "executor_decision" and data.get("kind") == "recovery":
            metrics["executor"]["recoveries"] += 1
        elif etype == "path_blocked":
            metrics["pathing"]["blocked_events"] += 1
            blocked_locations[_pos_key(data.get("pos"))] += float(data.get("duration_sec", 0.0) or 0.0)
        elif etype == "reservation_created":
            metrics["reservations"]["created"] += 1
            if data.get("conflict"):
                metrics["reservations"]["conflicts"] += 1
        elif etype == "reservation_released":
            metrics["reservations"]["released"] += 1
        elif etype == "reservation_expired":
            metrics["reservations"]["expired"] += 1
        elif etype == "reservation_violation":
            metrics["reservations"]["violated"] += 1
            metrics["reservations"]["conflicts"] += 1
        elif etype == "order_spawned":
            metrics["orders"]["spawned"] += 1
            oid = data.get("id", data.get("order_id"))
            order_spawn_ticks[oid] = int(event.get("tick") or 0)
        elif etype == "order_delivered":
            metrics["orders"]["delivered"] += 1
            minute = int(_event_sec(event) // 60)
            by_minute[minute]["delivered"] += 1
            by_minute[minute]["score_delta"] += float(data.get("score_delta", data.get("score", 0)) or 0)
            oid = data.get("id", data.get("order_id"))
            if oid in order_spawn_ticks:
                order_latencies.append(_seconds_from_tick(int(event.get("tick") or 0) - order_spawn_ticks[oid]))
            stand_id = data.get("stand_id") or data.get("standId")
            if stand_id:
                inferred_customers[str(stand_id)] = 10.0
        elif etype == "order_expired":
            metrics["orders"]["expired"] += 1
        elif etype == "order_failed":
            metrics["orders"]["failed_wrong"] += 1
        elif etype == "no_slot_failure":
            metrics["orders"]["no_slot"] += 1
        elif etype == "component_waste":
            item = data.get("item")
            if _is_plate(item):
                metrics["waste"]["plate_count"] += 1
                for plate_item in item.get("items", []) or []:
                    metrics["waste"]["component_count"] += 1
                    waste_by_kind[_component_key(plate_item)] += 1
            elif item is not None:
                metrics["waste"]["component_count"] += 1
                waste_by_kind[_component_key(item)] += 1
        elif etype == "state_sample":
            state = _state_data(event)
            streak = state.get("streak")
            if streak is not None:
                metrics["streak"]["progression"].append([round(float(state.get("time", _event_sec(event))), 3), int(streak)])

    total_chef_sec = sum(sum(v.values()) for v in chef_seconds.values())
    per_chef = {}
    aggregate = Counter()
    for cid, vals in sorted(chef_seconds.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else kv[0]):
        denom = sum(vals[b] for b in ("idle", "moving", "blocked", "processing", "stall"))
        row = {}
        for bucket in CHEF_BUCKETS:
            sec = float(vals[bucket])
            row[f"{bucket}_sec"] = sec
            if bucket != "holding":
                row[f"{bucket}_pct"] = _pct(sec, denom)
            aggregate[bucket] += sec
        per_chef[cid] = row
    aggregate_denom = sum(aggregate[b] for b in ("idle", "moving", "blocked", "processing", "stall"))
    metrics["chef_utilization"]["per_chef"] = per_chef
    metrics["chef_utilization"]["aggregate"] = {
        f"{bucket}_sec": float(aggregate[bucket]) for bucket in CHEF_BUCKETS
    } | {
        f"{bucket}_pct": _pct(float(aggregate[bucket]), aggregate_denom)
        for bucket in ("idle", "moving", "blocked", "processing", "stall")
    }

    metrics["station_utilization"] = {
        "stove_busy_pct": _pct(station_seconds["stove_busy_unit_sec"], station_seconds["stove_unit_sec"]),
        "stove_ready_pct": _pct(station_seconds["stove_ready_unit_sec"], station_seconds["stove_unit_sec"]),
        "all_stoves_cold_while_cook_needed_pct": _pct(
            station_seconds["all_stoves_cold_cook_needed_sec"], station_seconds["cook_demand_sec"]
        ),
        "board_busy_pct": _pct(station_seconds["board_busy_unit_sec"], station_seconds["board_unit_sec"]),
        "avg_plating_items": _pct(station_seconds["plating_item_sec"], max(metrics["time_sec"], 1e-9)),
        "counter_occupied_pct": _pct(station_seconds["counter_occupied_unit_sec"], station_seconds["counter_unit_sec"]),
    }
    metrics["stand_pressure"] = {
        "avg_visible_orders": _pct(stand_seconds["visible_order_sec"], max(metrics["time_sec"], 1e-9)),
        "avg_inferred_customers": _pct(stand_seconds["inferred_customer_sec"], max(metrics["time_sec"], 1e-9)),
        "avg_occupied_stands": _pct(stand_seconds["occupied_stand_sec"], max(metrics["time_sec"], 1e-9)),
        "all_stands_occupied_sec": float(stand_seconds["all_stands_occupied_sec"]),
        "no_slot_risk_sec": float(stand_seconds["no_slot_risk_sec"]),
        "no_slot_risk_pct": _pct(stand_seconds["no_slot_risk_sec"], max(metrics["time_sec"], 1e-9)),
    }
    metrics["pathing"]["blocked_locations"] = dict(blocked_locations.most_common(25))
    metrics["executor"]["failures_by_error"] = dict(failures_by_error)
    metrics["executor"]["avg_command_travel_ticks"] = (
        sum(command_travels) / len(command_travels) if command_travels else 0.0
    )
    metrics["waste"]["items_by_kind"] = dict(waste_by_kind)

    if order_latencies:
        order_latencies.sort()
        n = len(order_latencies)
        mid = n // 2
        metrics["orders"]["latency_sec_median"] = (
            order_latencies[mid] if n % 2 else (order_latencies[mid - 1] + order_latencies[mid]) / 2
        )
        metrics["orders"]["latency_sec_mean"] = sum(order_latencies) / n

    minutes = max(metrics["time_sec"] / 60.0, 1e-9)
    metrics["throughput"]["deliveries_per_min"] = summary["delivered"] / minutes
    metrics["throughput"]["score_per_min"] = summary["score"] / minutes
    metrics["throughput"]["by_minute"] = {str(k): dict(v) for k, v in sorted(by_minute.items())}

    attempts = metrics["executor"]["command_attempts"]
    summary["command_success_rate"] = (
        metrics["executor"]["command_successes"] / attempts if attempts else 0.0
    )
    metrics["summary"] = summary
    return metrics


def merge_metric_dicts(metrics_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a list of per-trace metric dictionaries into one report."""
    if not metrics_list:
        return {"schema_version": METRICS_SCHEMA_VERSION, "runs": 0}

    out = deepcopy(_base_metrics())
    out["runs"] = len(metrics_list)
    scores = []
    survivals = []
    delivered = []
    failures = Counter()
    best_streaks = []

    chef_acc: dict[str, Counter[str]] = defaultdict(Counter)
    blocked = Counter()
    waste = Counter()
    waste_counts = Counter()
    by_minute: dict[int, Counter[str]] = defaultdict(Counter)

    station_acc = Counter()
    stand_acc = Counter()
    executor_acc = Counter()
    reservation_acc = Counter()
    planner_acc = Counter()
    planner_features: dict[str, bool] = {}

    for metrics in metrics_list:
        summary = metrics.get("summary") or {}
        scores.append(float(summary.get("score", 0)))
        survivals.append(float(summary.get("time_sec", 0)))
        delivered.append(float(summary.get("delivered", 0)))
        best_streaks.append(float(summary.get("best_streak", 0)))
        orders = metrics.get("orders") or {}
        failures["expired"] += int(orders.get("expired", 0))
        failures["wrong"] += int(orders.get("failed_wrong", 0))
        failures["no_slot"] += int(orders.get("no_slot", 0))

        for cid, vals in ((metrics.get("chef_utilization") or {}).get("per_chef") or {}).items():
            for k, v in vals.items():
                if k.endswith("_sec"):
                    chef_acc[cid][k] += float(v)
        blocked.update((metrics.get("pathing") or {}).get("blocked_locations") or {})
        waste.update((metrics.get("waste") or {}).get("items_by_kind") or {})
        waste_counts["component_count"] += int((metrics.get("waste") or {}).get("component_count", 0))
        waste_counts["plate_count"] += int((metrics.get("waste") or {}).get("plate_count", 0))

        for k, v in (metrics.get("station_utilization") or {}).items():
            if isinstance(v, (int, float)):
                station_acc[k] += float(v)
        for k, v in (metrics.get("stand_pressure") or {}).items():
            if isinstance(v, (int, float)):
                stand_acc[k] += float(v)
        for k, v in (metrics.get("executor") or {}).items():
            if isinstance(v, (int, float)):
                executor_acc[k] += float(v)
        for k, v in (metrics.get("reservations") or {}).items():
            if isinstance(v, (int, float)):
                reservation_acc[k] += float(v)
        planner = metrics.get("planner") or {}
        for k, v in planner.items():
            if k == "features" and isinstance(v, dict):
                for fk, enabled in v.items():
                    planner_features[fk] = bool(enabled)
            elif isinstance(v, (int, float)):
                planner_acc[k] += float(v)
        for minute, vals in ((metrics.get("throughput") or {}).get("by_minute") or {}).items():
            for k, v in vals.items():
                by_minute[int(minute)][k] += float(v)

    total_time = sum(survivals)
    out["time_sec"] = total_time
    out["summary"] = {
        "runs": len(metrics_list),
        "score_mean": sum(scores) / len(scores),
        "score_median": sorted(scores)[len(scores) // 2],
        "score_min": min(scores),
        "score_max": max(scores),
        "survival_mean_sec": sum(survivals) / len(survivals),
        "delivered_mean": sum(delivered) / len(delivered),
        "best_streak_mean": sum(best_streaks) / len(best_streaks),
        "failures": dict(failures),
    }

    out["chef_utilization"]["per_chef"] = {}
    aggregate = Counter()
    for cid, vals in sorted(chef_acc.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else kv[0]):
        denom = sum(vals.get(f"{b}_sec", 0.0) for b in ("idle", "moving", "blocked", "processing", "stall"))
        row = {}
        for bucket in CHEF_BUCKETS:
            sec = float(vals.get(f"{bucket}_sec", 0.0))
            row[f"{bucket}_sec"] = sec
            if bucket != "holding":
                row[f"{bucket}_pct"] = _pct(sec, denom)
            aggregate[bucket] += sec
        out["chef_utilization"]["per_chef"][cid] = row
    denom = sum(aggregate[b] for b in ("idle", "moving", "blocked", "processing", "stall"))
    out["chef_utilization"]["aggregate"] = {
        f"{bucket}_sec": float(aggregate[bucket]) for bucket in CHEF_BUCKETS
    } | {
        f"{bucket}_pct": _pct(float(aggregate[bucket]), denom)
        for bucket in ("idle", "moving", "blocked", "processing", "stall")
    }

    def avg(counter: Counter[str], key: str) -> float:
        return float(counter[key]) / len(metrics_list)

    out["station_utilization"] = {k: avg(station_acc, k) for k in station_acc}
    out["stand_pressure"] = {k: avg(stand_acc, k) for k in stand_acc}
    out["executor"] = {k: avg(executor_acc, k) for k in executor_acc}
    out["reservations"] = {k: avg(reservation_acc, k) for k in reservation_acc}
    if planner_acc or planner_features:
        out["planner"] = {"features": planner_features} | {
            k: int(v) for k, v in planner_acc.items()
        }
    out["pathing"]["blocked_locations"] = dict(blocked.most_common(25))
    out["waste"]["component_count"] = int(waste_counts["component_count"])
    out["waste"]["plate_count"] = int(waste_counts["plate_count"])
    out["waste"]["items_by_kind"] = dict(waste)
    out["throughput"]["deliveries_per_min"] = sum(delivered) / max(total_time / 60.0, 1e-9)
    out["throughput"]["score_per_min"] = sum(scores) / max(total_time / 60.0, 1e-9)
    out["throughput"]["by_minute"] = {str(k): dict(v) for k, v in sorted(by_minute.items())}
    out["orders"] = {
        "delivered": int(sum(delivered)),
        "expired": int(failures["expired"]),
        "failed_wrong": int(failures["wrong"]),
        "no_slot": int(failures["no_slot"]),
    }
    return out
