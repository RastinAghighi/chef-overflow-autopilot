"""Instrument the current greedy planner over fixed sim seeds.

This is Milestone 1's diagnosis generator.  It does not change the planner; it
wraps the sim API the planner already uses and records telemetry around it.

Run:
    py tools/profile_planner.py --seeds 30 --cap 1200
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.planner import Planner  # noqa: E402
from sim.env import KitchenSim  # noqa: E402
from sim import constants as C  # noqa: E402
from telemetry.metrics import compute_metrics, merge_metric_dicts  # noqa: E402
from telemetry.reservations import ReservationManager  # noqa: E402
from telemetry.schema import SCHEMA_VERSION, make_event  # noqa: E402
from telemetry.storage import Trace, dump_json, write_jsonl  # noqa: E402
from telemetry.summarize import format_report  # noqa: E402


DT = 1.0 / 60.0


def _clone_item(item: Any) -> Any:
    return json.loads(json.dumps(item)) if item is not None else None


def _is_plate(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "plate"


def _item_count(item: Any) -> int:
    if _is_plate(item):
        return len(item.get("items", []) or [])
    return 1 if item is not None else 0


def _state_tick(sim: KitchenSim) -> int:
    return int(sim.tick_count)


def _state_ms(sim: KitchenSim) -> float:
    return float(sim.time * 1000.0)


def _station_lists(stations: dict[str, Any]) -> list[list[dict[str, Any]]]:
    return [
        stations.get("ingredientBins", []),
        stations.get("stoves", []),
        stations.get("cuttingBoards", []),
        stations.get("platingAreas", []),
        stations.get("receptionStands", []),
        stations.get("trashCans", []),
        stations.get("counters", []),
    ]


def _station_pos_by_id(state: dict[str, Any]) -> dict[str, tuple[int, int]]:
    out = {}
    for group in _station_lists(state.get("stations") or {}):
        for station in group:
            pos = station.get("pos")
            if pos is not None:
                out[station["id"]] = (int(pos[0]), int(pos[1]))
    return out


def _cook_demand_visible(state: dict[str, Any]) -> bool:
    for order in state.get("orders", []) or []:
        for comp in order.get("components", []) or []:
            if comp.get("state") == "cooked":
                return True
    for upcoming in state.get("upcomingOrders", []) or []:
        for comp in upcoming.get("components", []) or []:
            if comp.get("state") == "cooked":
                return True
    for chef in state.get("chefs", []) or []:
        h = chef.get("holding")
        if isinstance(h, dict) and h.get("state") == "raw" and h.get("ingredient") in {"meat", "dough"}:
            return True
    return False


class TraceRecorder:
    def __init__(self, seed: int, source: str, trace_id: str):
        self.seed = seed
        self.source = source
        self.trace_id = trace_id
        self.events: list[dict[str, Any]] = []
        self.seq = 0

    def record(self, event_type: str, tick: int, ms: float, data: dict[str, Any] | None = None) -> None:
        self.events.append(make_event(event_type, tick, ms, self.seq, data or {}))
        self.seq += 1

    def start(self, sim: KitchenSim) -> None:
        self.record(
            "trace_start",
            0,
            0.0,
            {
                "schema_version": SCHEMA_VERSION,
                "trace_id": self.trace_id,
                "run_id": None,
                "seed": self.seed,
                "started_at_iso": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "source": self.source,
                "game_url": None,
                "agent_version": "planner.py+milestone1",
                "game_api_version": "sim.env",
                "seed_known": True,
                "tick_hz": 60,
            },
        )

    def end(self, sim: KitchenSim, major_failure_reason: str) -> None:
        self.record(
            "trace_end",
            _state_tick(sim),
            _state_ms(sim),
            {
                "score": int(math.floor(sim.score)),
                "delivered": sim.delivered_total,
                "best_streak": sim.best_streak,
                "failed_orders": sim.failed_orders,
                "time_sec": sim.time,
                "game_over": sim.game_over,
                "major_failure_reason": major_failure_reason,
            },
        )


class IntervalMetrics:
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.chefs: dict[str, Counter[str]] = defaultdict(Counter)
        self.stations: Counter[str] = Counter()
        self.stand_pressure: Counter[str] = Counter()
        self.blocked_locations: dict[str, Counter[str]] = defaultdict(Counter)

    def update(self, sim: KitchenSim, state: dict[str, Any], inferred_customers: dict[str, float], dt: float) -> None:
        for c_live, c_state in zip(sim.chefs, state.get("chefs", []) or []):
            cid = str(c_state["id"])
            if c_live.get("busy"):
                bucket = "processing"
            elif c_live.get("commitmentStall", 0.0) > 0:
                bucket = "stall"
            elif c_live.get("path"):
                bucket = "blocked" if c_live.get("blockedTicks", 0) > 0 else "moving"
            else:
                bucket = "idle"
            self.chefs[cid][f"{bucket}_sec"] += dt
            if c_live.get("holding") is not None:
                self.chefs[cid]["holding_sec"] += dt
            if bucket == "blocked":
                loc = f"{c_live['x']},{c_live['y']}"
                self.chefs[cid].setdefault("blocked_locations", Counter())
                self.chefs[cid]["blocked_locations"][loc] += dt

        stations = state["stations"]
        stoves = stations.get("stoves", [])
        boards = stations.get("cuttingBoards", [])
        plating = stations.get("platingAreas", [])
        counters = stations.get("counters", [])
        stands = stations.get("receptionStands", [])
        stove_busy = sum(1 for s in stoves if s.get("cooking") is not None)
        stove_ready = sum(1 for s in stoves if s.get("ready"))
        cook_demand = _cook_demand_visible(state)
        self.stations["stove_unit_sec"] += dt * max(1, len(stoves))
        self.stations["stove_busy_unit_sec"] += dt * stove_busy
        self.stations["stove_ready_unit_sec"] += dt * stove_ready
        self.stations["cook_demand_sec"] += dt if cook_demand else 0.0
        self.stations["all_stoves_cold_cook_needed_sec"] += dt if cook_demand and stove_busy == 0 else 0.0
        self.stations["board_unit_sec"] += dt * max(1, len(boards))
        self.stations["board_busy_unit_sec"] += dt * sum(1 for b in boards if b.get("busy") or b.get("processing") is not None)
        self.stations["plating_item_sec"] += dt * sum(len(p.get("items", []) or []) for p in plating)
        self.stations["counter_unit_sec"] += dt * max(1, len(counters))
        self.stations["counter_occupied_unit_sec"] += dt * sum(1 for c in counters if c.get("items"))

        for sid in list(inferred_customers):
            inferred_customers[sid] = max(0.0, inferred_customers[sid] - dt)
        visible = {s.get("id") for s in stands if s.get("order") is not None}
        inferred = {sid for sid, left in inferred_customers.items() if left > 0.0}
        occupied = visible | inferred
        self.stand_pressure["visible_order_sec"] += dt * len(visible)
        self.stand_pressure["inferred_customer_sec"] += dt * len(inferred)
        self.stand_pressure["occupied_stand_sec"] += dt * len(occupied)
        self.stand_pressure["all_stands_occupied_sec"] += dt if len(occupied) >= 5 else 0.0
        self.stand_pressure["no_slot_risk_sec"] += dt if len(occupied) >= 4 else 0.0

    def snapshot(self) -> dict[str, Any]:
        chefs: dict[str, Any] = {}
        for cid, counter in self.chefs.items():
            row = {}
            for key, value in counter.items():
                if key == "blocked_locations":
                    row[key] = dict(value)
                else:
                    row[key] = float(value)
            chefs[cid] = row
        return {
            "chefs": chefs,
            "stations": {k: float(v) for k, v in self.stations.items()},
            "stand_pressure": {k: float(v) for k, v in self.stand_pressure.items()},
        }


class InstrumentedApi:
    def __init__(self, sim: KitchenSim, recorder: TraceRecorder, reservations: ReservationManager):
        self.sim = sim
        self.recorder = recorder
        self.reservations = reservations
        self.active: dict[int, dict[str, Any]] = {}

    def _release_active(self, chef_id: int, reason: str) -> None:
        active = self.active.get(chef_id)
        if not active:
            return
        for rid in active.get("reservation_ids", []):
            reservation = self.reservations.release(rid, reason)
            if reservation:
                self.recorder.record("reservation_released", _state_tick(self.sim), _state_ms(self.sim), reservation.to_dict())

    def command(self, chef_id: int, target_id: str):
        tick = _state_tick(self.sim)
        ms = _state_ms(self.sim)
        chef = self.sim.chefs[chef_id]
        previous = self.active.get(chef_id)
        duplicate = previous is not None and previous.get("target_id") == target_id
        retarget = previous is not None and previous.get("target_id") != target_id
        if retarget:
            self.recorder.record(
                "command_cancelled",
                tick,
                ms,
                {"chef_id": chef_id, "target_id": previous.get("target_id"), "reason": "retarget"},
            )
            self._release_active(chef_id, "retarget")

        self.recorder.record(
            "command_attempt",
            tick,
            ms,
            {
                "chef_id": chef_id,
                "target_id": target_id,
                "reason": "planner",
                "executor_state_before": "IDLE" if previous is None else "MOVING",
                "chef_pos": [chef["x"], chef["y"]],
                "holding": _clone_item(chef.get("holding")),
                "reservation_ids": previous.get("reservation_ids", []) if previous else [],
                "duplicate": duplicate,
                "retarget": retarget,
            },
        )
        before_holding = _clone_item(chef.get("holding"))
        result = self.sim.command(chef_id, target_id)
        success = bool(result and result.get("success"))
        self.recorder.record(
            "command_result",
            tick,
            ms,
            {
                "chef_id": chef_id,
                "target_id": target_id,
                "success": success,
                "error": None if success else (result or {}).get("error", "unknown"),
                "executor_state_after": "COMMAND_SENT" if success else "ERROR",
            },
        )
        if not success:
            return result

        reservation_ids = []
        for kind, resource in (("chef", str(chef_id)), ("station", target_id)):
            reservation, conflicts = self.reservations.reserve(
                owner="executor",
                chef_id=chef_id,
                kind=kind,
                resource_id=resource,
                order_id=None,
                purpose=f"command:{target_id}",
                created_tick=tick,
                ttl_ticks=60 * 30,
            )
            reservation_ids.append(reservation.id)
            data = reservation.to_dict()
            data["conflict"] = bool(conflicts)
            if conflicts:
                data["conflicts"] = [c.to_dict() for c in conflicts]
            self.recorder.record("reservation_created", tick, ms, data)
            for conflict in conflicts:
                self.recorder.record(
                    "reservation_violation",
                    tick,
                    ms,
                    {**conflict.to_dict(), "conflict_with": reservation.id, "reason": "double_booking"},
                )

        self.active[chef_id] = {
            "target_id": target_id,
            "start_tick": tick,
            "holding_before": before_holding,
            "reservation_ids": reservation_ids,
            "last_pos": (chef["x"], chef["y"]),
            "blocked_event_open": False,
        }
        if not self.sim.chefs[chef_id]["path"]:
            self.mark_arrival(chef_id)
        return result

    def boost(self, chef_id: int):
        tick = _state_tick(self.sim)
        ms = _state_ms(self.sim)
        self.recorder.record("boost_attempt", tick, ms, {"chef_id": chef_id, "reason": "planner"})
        result = self.sim.boost(chef_id)
        self.recorder.record(
            "boost_result",
            tick,
            ms,
            {"chef_id": chef_id, "success": bool(result and result.get("success")), "error": (result or {}).get("error")},
        )
        return result

    def monitor(self) -> None:
        for chef_id in list(self.active):
            chef = self.sim.chefs[chef_id]
            active = self.active[chef_id]
            if chef.get("blockedTicks", 0) >= 30 and not active.get("blocked_event_open"):
                active["blocked_event_open"] = True
                self.recorder.record(
                    "path_blocked",
                    _state_tick(self.sim),
                    _state_ms(self.sim),
                    {
                        "chef_id": chef_id,
                        "target_id": active["target_id"],
                        "pos": [chef["x"], chef["y"]],
                        "blocked_ticks": chef.get("blockedTicks", 0),
                        "duration_sec": chef.get("blockedTicks", 0) * C.MOVE_DELAY,
                    },
                )
            if not chef.get("path"):
                self.mark_arrival(chef_id)

    def mark_arrival(self, chef_id: int) -> None:
        active = self.active.get(chef_id)
        if not active:
            return
        chef = self.sim.chefs[chef_id]
        tick = _state_tick(self.sim)
        holding_after = _clone_item(chef.get("holding"))
        self.recorder.record(
            "command_arrival",
            tick,
            _state_ms(self.sim),
            {
                "chef_id": chef_id,
                "target_id": active["target_id"],
                "ticks_since_command": tick - active["start_tick"],
                "expected_min_ticks": None,
                "holding_before": active["holding_before"],
                "holding_after": holding_after,
            },
        )
        if active["target_id"].startswith("trash") and active["holding_before"] is not None and holding_after is None:
            self.recorder.record(
                "component_waste",
                tick,
                _state_ms(self.sim),
                {
                    "chef_id": chef_id,
                    "reason": "trash",
                    "item": active["holding_before"],
                    "component_count": _item_count(active["holding_before"]),
                },
            )
        self._release_active(chef_id, "arrival")
        self.active.pop(chef_id, None)


def _major_failure_reason(sim: KitchenSim) -> str:
    counts = {
        "expiry": sim.expired_total,
        "no_slot": sim.no_slot_total,
        "wrong": sim.wrong_total,
    }
    return max(counts, key=counts.get) if any(counts.values()) else "unknown"


def run_instrumented_episode(seed: int, time_cap: float, decide_every: int, out_dir: Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sim = KitchenSim(seed)
    planner = Planner()
    trace_id = f"sim-planner-seed-{seed}"
    recorder = TraceRecorder(seed, "sim", trace_id)
    reservations = ReservationManager()
    api = InstrumentedApi(sim, recorder, reservations)
    interval = IntervalMetrics()
    inferred_customers: dict[str, float] = {}
    recorder.start(sim)

    prev_state = sim.get_state()
    prev_orders = {o["id"]: dict(o) for o in prev_state.get("orders", [])}
    prev_delivered = sim.delivered_total
    prev_expired = sim.expired_total
    prev_wrong = sim.wrong_total
    prev_no_slot = sim.no_slot_total
    prev_score = sim.score
    last_metric_tick = 0
    last_state_tick = 0
    tick = 0
    max_ticks = int(round(time_cap / DT))

    recorder.record("state_sample", 0, 0.0, {"state": prev_state})

    while not sim.game_over and sim.time < time_cap and tick <= max_ticks + 5:
        if tick % decide_every == 0:
            planner.decide(sim.get_state(), api)

        sim.tick(DT)
        tick += 1
        state = sim.get_state()
        api.monitor()

        current_orders = {o["id"]: dict(o) for o in state.get("orders", [])}
        for oid, order in current_orders.items():
            if oid not in prev_orders:
                recorder.record(
                    "order_spawned",
                    _state_tick(sim),
                    _state_ms(sim),
                    {
                        "id": oid,
                        "dish": order["dish"],
                        "stand_id": order["standId"],
                        "time_left": order["timeLeft"],
                        "components": order["components"],
                    },
                )

        disappeared = {oid: order for oid, order in prev_orders.items() if oid not in current_orders}
        delivered_delta = sim.delivered_total - prev_delivered
        expired_delta = sim.expired_total - prev_expired
        wrong_delta = sim.wrong_total - prev_wrong
        no_slot_delta = sim.no_slot_total - prev_no_slot

        if delivered_delta > 0:
            for oid, order in list(disappeared.items())[:delivered_delta]:
                score_delta = sim.score - prev_score
                recorder.record(
                    "order_delivered",
                    _state_tick(sim),
                    _state_ms(sim),
                    {
                        "id": oid,
                        "dish": order["dish"],
                        "stand_id": order["standId"],
                        "score_delta": score_delta,
                        "streak": sim.streak,
                    },
                )
                inferred_customers[order["standId"]] = C.CUSTOMER_EAT_TIME
                recorder.record(
                    "customer_inferred_start",
                    _state_tick(sim),
                    _state_ms(sim),
                    {"stand_id": order["standId"], "duration_sec": C.CUSTOMER_EAT_TIME, "source": "delivery"},
                )

        expired_orders = list(disappeared.items())[delivered_delta: delivered_delta + max(0, expired_delta)]
        for oid, order in expired_orders:
            recorder.record(
                "order_expired",
                _state_tick(sim),
                _state_ms(sim),
                {"id": oid, "dish": order["dish"], "stand_id": order["standId"], "cause": "timer"},
            )

        for _ in range(max(0, wrong_delta)):
            recorder.record("order_failed", _state_tick(sim), _state_ms(sim), {"cause": "wrong_delivery"})
        for _ in range(max(0, no_slot_delta)):
            recorder.record("no_slot_failure", _state_tick(sim), _state_ms(sim), {"cause": "no_stand_slot"})

        interval.update(sim, state, inferred_customers, DT)

        if sim.tick_count - last_metric_tick >= 60:
            recorder.record("metric_sample", _state_tick(sim), _state_ms(sim), {"interval": interval.snapshot(), "time_sec": sim.time})
            interval.reset()
            last_metric_tick = sim.tick_count
        if sim.tick_count - last_state_tick >= 300:
            recorder.record("state_sample", _state_tick(sim), _state_ms(sim), {"state": state})
            last_state_tick = sim.tick_count

        for reservation in reservations.expire(_state_tick(sim)):
            recorder.record("reservation_expired", _state_tick(sim), _state_ms(sim), reservation.to_dict())

        prev_state = state
        prev_orders = current_orders
        prev_delivered = sim.delivered_total
        prev_expired = sim.expired_total
        prev_wrong = sim.wrong_total
        prev_no_slot = sim.no_slot_total
        prev_score = sim.score

    if any(interval.chefs.values()) or interval.stations:
        recorder.record("metric_sample", _state_tick(sim), _state_ms(sim), {"interval": interval.snapshot(), "time_sec": sim.time})
    recorder.record("state_sample", _state_tick(sim), _state_ms(sim), {"state": sim.get_state()})
    recorder.end(sim, _major_failure_reason(sim))

    trace = Trace(path=None, events=recorder.events)
    metrics = compute_metrics(trace)
    if out_dir is not None:
        run_dir = out_dir / f"seed_{seed:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        trace_path = run_dir / "trace.jsonl"
        write_jsonl(trace_path, recorder.events)
        dump_json(run_dir / "summary.json", metrics.get("summary") or {})
        dump_json(run_dir / "metrics.json", metrics)
    return metrics, recorder.events


def _write_report(out_dir: Path, aggregate: dict[str, Any], per_seed: list[dict[str, Any]]) -> None:
    dump_json(out_dir / "aggregate_metrics.json", aggregate)
    rows = []
    for metrics in per_seed:
        s = metrics["summary"]
        rows.append({
            "seed": s.get("trace_id", "").rsplit("-", 1)[-1],
            "score": s.get("score", 0),
            "delivered": s.get("delivered", 0),
            "expired": metrics["orders"].get("expired", 0),
            "no_slot": metrics["orders"].get("no_slot", 0),
            "wrong": metrics["orders"].get("failed_wrong", 0),
            "survival": s.get("time_sec", 0.0),
            "best_streak": s.get("best_streak", 0),
        })
    dump_json(out_dir / "per_seed_summary.json", {"rows": rows})
    report = format_report(aggregate)
    (out_dir / "aggregate_report.txt").write_text(report + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Instrument the current greedy planner over sim seeds.")
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--seed-start", type=int, default=0)
    ap.add_argument("--cap", type=float, default=C.DEFAULT_TIME_CAP)
    ap.add_argument("--decide-hz", type=int, default=20)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    decide_every = max(1, round(60 / max(1, args.decide_hz)))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "traces" / f"{stamp}_sim_planner_m1"
    out_dir.mkdir(parents=True, exist_ok=True)

    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    per_seed = []
    for seed in seeds:
        metrics, _events = run_instrumented_episode(seed, args.cap, decide_every, out_dir)
        per_seed.append(metrics)
        s = metrics["summary"]
        print(
            f"seed {seed:>3}: score {s['score']:>6} deliv {s['delivered']:>3} "
            f"exp {metrics['orders']['expired']} noslot {metrics['orders']['no_slot']} "
            f"wrong {metrics['orders']['failed_wrong']} t {s['time_sec']:>6.1f}s"
        )

    aggregate = merge_metric_dicts(per_seed)
    _write_report(out_dir, aggregate, per_seed)
    print()
    print(format_report(aggregate))
    print()
    print(f"wrote {out_dir}")


if __name__ == "__main__":
    main()
