"""Summarize one or more Chef Overflow telemetry traces."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telemetry.storage import dump_json, load_trace, validate_trace  # noqa: E402
from telemetry.summarize import format_report, summarize_many, summarize_trace  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Summarize Chef Overflow trace.jsonl files.")
    ap.add_argument("traces", nargs="+", help="trace.jsonl or trace.jsonl.gz paths")
    ap.add_argument("--json-out", default=None, help="optional metrics JSON output path")
    args = ap.parse_args()

    traces = [load_trace(p) for p in args.traces]
    for trace in traces:
        errors = validate_trace(trace)
        if errors:
            print(f"[warn] {trace.path}: {len(errors)} validation issue(s)", file=sys.stderr)
            for err in errors[:5]:
                print(f"  {err.index}: {err.message}", file=sys.stderr)

    metrics = summarize_many(traces) if len(traces) > 1 else summarize_trace(traces[0])
    print(format_report(metrics))
    if args.json_out:
        dump_json(args.json_out, metrics)


if __name__ == "__main__":
    main()

