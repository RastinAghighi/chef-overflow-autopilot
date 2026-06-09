"""Trace replay/parity helpers.

Browser traces cannot read the module-scoped input log from game.js.  For pasted
console agents, replay input must therefore be inferred from observed command
arrivals and state transitions.  This is useful for diagnosis, but it is not the
server's compact input log and should not be treated as tick-exact proof.

Default parity tolerance is intentionally summary-level:
  * score within +/-5 points
  * delivered within +/-1 order
  * best streak within +/-1

When a trace was produced by the sim instrumentation, these tolerances normally
collapse to exact equality because the trace can record arrival ticks directly.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .schema import PARITY_SCHEMA_VERSION
from .storage import Trace


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "ht6-chefoverflow-main" / "sim"


def extract_inferred_interactions(trace: Trace) -> list[dict[str, Any]]:
    inputs = []
    for event in trace.events:
        if event.get("type") != "command_arrival":
            continue
        data = event.get("data") or {}
        chef_id = data.get("chef_id")
        station_id = data.get("target_id") or data.get("station_id")
        tick = event.get("tick")
        if isinstance(tick, int) and isinstance(chef_id, int) and isinstance(station_id, str):
            inputs.append({"tick": tick, "type": "interact", "chefId": chef_id, "stationId": station_id})
    inputs.sort(key=lambda e: (e["tick"], e["chefId"], e["stationId"]))
    return inputs


def _run_core_replay(seed_or_run_id: str | int, inputs: list[dict[str, Any]], max_ticks: int) -> dict[str, Any]:
    payload = {"seed_or_run_id": seed_or_run_id, "inputs": inputs, "max_ticks": max_ticks}
    code = """
import { simulate, defaultConfig } from './core.js';
import { seedFromRunId } from './prng.js';
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const payload = JSON.parse(chunks.join(''));
const raw = payload.seed_or_run_id;
const seed = Number.isInteger(raw) ? raw : seedFromRunId(String(raw));
const result = simulate({
  seed,
  config: defaultConfig(),
  inputs: payload.inputs || [],
  maxTicks: payload.max_ticks,
});
process.stdout.write(JSON.stringify(result));
""".strip()
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", code],
        cwd=CORE_DIR,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def replay_trace(trace: Trace, *, max_ticks_margin: int = 60 * 30) -> dict[str, Any]:
    header = trace.header
    run_id = header.get("run_id")
    seed = header.get("seed")
    if run_id is None and seed is None:
        return {
            "replay_available": False,
            "reason": "missing run_id_or_seed",
        }

    inputs = extract_inferred_interactions(trace)
    last_tick = max([e["tick"] for e in inputs], default=0)
    end_tick = max(last_tick + max_ticks_margin, int(float(header.get("time_sec", 0) or 0) * 60) + max_ticks_margin)
    summary = _run_core_replay(run_id if run_id is not None else int(seed), inputs, end_tick)
    return {
        "replay_available": True,
        "input_count": len(inputs),
        "inputs_inferred": True,
        "summary": summary,
    }


def compare_trace_to_replay(
    trace: Trace,
    replay: dict[str, Any],
    *,
    score_tolerance: int = 5,
    delivered_tolerance: int = 1,
    streak_tolerance: int = 1,
) -> dict[str, Any]:
    live = {}
    for event in reversed(trace.events):
        if event.get("type") == "trace_end":
            live = event.get("data") or {}
            break
    if not live:
        for event in reversed(trace.events):
            if event.get("type") == "state_sample":
                data = event.get("data") or {}
                live = data.get("state") if isinstance(data.get("state"), dict) else data
                break

    if not replay.get("replay_available"):
        return {
            "schema_version": PARITY_SCHEMA_VERSION,
            "replay_available": False,
            "reason": replay.get("reason", "unknown"),
            "mismatches": [],
        }

    rs = replay.get("summary") or {}
    live_score = int(live.get("score", 0) or 0)
    live_delivered = int(live.get("delivered", live.get("ordersDelivered", 0)) or 0)
    live_streak = int(live.get("best_streak", live.get("bestStreak", 0)) or 0)

    mismatches = []
    if abs(int(rs.get("score", 0)) - live_score) > score_tolerance:
        mismatches.append({"field": "score", "trace": live_score, "replay": int(rs.get("score", 0)), "tolerance": score_tolerance})
    if abs(int(rs.get("delivered", 0)) - live_delivered) > delivered_tolerance:
        mismatches.append({"field": "delivered", "trace": live_delivered, "replay": int(rs.get("delivered", 0)), "tolerance": delivered_tolerance})
    if abs(int(rs.get("bestStreak", 0)) - live_streak) > streak_tolerance:
        mismatches.append({"field": "best_streak", "trace": live_streak, "replay": int(rs.get("bestStreak", 0)), "tolerance": streak_tolerance})

    return {
        "schema_version": PARITY_SCHEMA_VERSION,
        "replay_available": True,
        "inputs_inferred": bool(replay.get("inputs_inferred")),
        "tolerance": {
            "score": score_tolerance,
            "delivered": delivered_tolerance,
            "best_streak": streak_tolerance,
        },
        "score_match": not any(m["field"] == "score" for m in mismatches),
        "delivered_match": not any(m["field"] == "delivered" for m in mismatches),
        "streak_match": not any(m["field"] == "best_streak" for m in mismatches),
        "first_mismatch_tick": None,
        "mismatches": mismatches,
        "replay": replay.get("summary"),
    }

