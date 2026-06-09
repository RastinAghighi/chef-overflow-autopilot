"""Instrument the Milestone 2 rolling scheduler over fixed sim seeds.

Run:
    py tools/profile_scheduler.py --seeds 30 --cap 1200

The output format intentionally matches ``tools/profile_planner.py`` so Milestone
1 and Milestone 2 reports can be compared side by side.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.rolling_scheduler import RollingScheduler  # noqa: E402
from sim.env import KitchenSim  # noqa: E402
from sim import constants as C  # noqa: E402
from telemetry.metrics import compute_metrics, merge_metric_dicts  # noqa: E402
from telemetry.reservations import ReservationManager  # noqa: E402
from telemetry.storage import Trace, dump_json, write_jsonl  # noqa: E402
from telemetry.summarize import format_report  # noqa: E402
from tools.profile_planner import (  # noqa: E402
    DT,
    InstrumentedApi,
    IntervalMetrics,
    TraceRecorder,
    _major_failure_reason,
    _state_ms,
    _state_tick,
    _write_report,
)


def run_instrumented_episode(
    seed: int,
    time_cap: float,
    decide_every: int,
    out_dir: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    sim = KitchenSim(seed)
    scheduler = RollingScheduler()
    trace_id = f"sim-scheduler-seed-{seed}"
    recorder = TraceRecorder(seed, "sim", trace_id, agent_version="rolling_scheduler.py+milestone2")
    reservations = ReservationManager()
    api = InstrumentedApi(sim, recorder, reservations, reason="scheduler")
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
            scheduler.decide(sim.get_state(), api)

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
    layout = scheduler.layout_summary()
    if out_dir is not None:
        run_dir = out_dir / f"seed_{seed:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(run_dir / "trace.jsonl", recorder.events)
        dump_json(run_dir / "summary.json", metrics.get("summary") or {})
        dump_json(run_dir / "metrics.json", metrics)
        dump_json(run_dir / "layout.json", layout)
    return metrics, recorder.events, layout


def main() -> None:
    ap = argparse.ArgumentParser(description="Instrument the Milestone 2 rolling scheduler over sim seeds.")
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--seed-start", type=int, default=0)
    ap.add_argument("--cap", type=float, default=C.DEFAULT_TIME_CAP)
    ap.add_argument("--decide-hz", type=int, default=20)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    decide_every = max(1, round(60 / max(1, args.decide_hz)))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "traces" / f"{stamp}_sim_scheduler_m2"
    out_dir.mkdir(parents=True, exist_ok=True)

    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    per_seed = []
    layouts = []
    for seed in seeds:
        metrics, _events, layout = run_instrumented_episode(seed, args.cap, decide_every, out_dir)
        per_seed.append(metrics)
        layouts.append(layout)
        s = metrics["summary"]
        print(
            f"seed {seed:>3}: score {s['score']:>6} deliv {s['delivered']:>3} "
            f"exp {metrics['orders']['expired']} noslot {metrics['orders']['no_slot']} "
            f"wrong {metrics['orders']['failed_wrong']} t {s['time_sec']:>6.1f}s"
        )

    aggregate = merge_metric_dicts(per_seed)
    _write_report(out_dir, aggregate, per_seed)
    if layouts:
        dump_json(out_dir / "layout_summary.json", layouts[-1])
    print()
    print(format_report(aggregate))
    print()
    print(f"wrote {out_dir}")


if __name__ == "__main__":
    main()
