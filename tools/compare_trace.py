"""Compare a trace's inferred interactions against core.js replay summary."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telemetry.parity import compare_trace_to_replay, replay_trace  # noqa: E402
from telemetry.storage import dump_json, load_trace  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Run diagnostic replay parity for a telemetry trace.")
    ap.add_argument("trace", help="trace.jsonl path")
    ap.add_argument("--json-out", default=None, help="optional parity JSON output")
    ap.add_argument("--score-tolerance", type=int, default=5)
    ap.add_argument("--delivered-tolerance", type=int, default=1)
    ap.add_argument("--streak-tolerance", type=int, default=1)
    args = ap.parse_args()

    trace = load_trace(args.trace)
    replay = replay_trace(trace)
    report = compare_trace_to_replay(
        trace,
        replay,
        score_tolerance=args.score_tolerance,
        delivered_tolerance=args.delivered_tolerance,
        streak_tolerance=args.streak_tolerance,
    )
    if args.json_out:
        dump_json(args.json_out, report)
    print(report)
    if report.get("mismatches"):
        sys.exit(1)


if __name__ == "__main__":
    main()

