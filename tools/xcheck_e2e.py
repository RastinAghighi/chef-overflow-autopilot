"""Validate the REAL core.js delivery pipeline against sim/constants.py.

Runs tools/xcheck_e2e.mjs (which drives genuine core.js Steak/Salad pipelines)
and asserts the score core.js actually awarded equals
C.delivery_score(difficulty, timeLeft, streak, vip) for the captured inputs.

    py tools/xcheck_e2e.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from sim import constants as C  # noqa: E402


def main():
    mjs = os.path.join(HERE, "xcheck_e2e.mjs")
    raw = subprocess.run(["node", mjs], capture_output=True, text=True, check=True).stdout
    rows = json.loads(raw)

    checked = 0
    skipped = 0
    failed = []
    for r in rows:
        if "fail" in r:
            failed.append(("driver-fail", r["fail"]))
            continue
        if "skip" in r:
            skipped += 1
            continue
        cap = r["cap"]
        expected = C.delivery_score(cap["difficulty"], cap["timeLeft"], cap["streak"], bool(cap["vip"]))
        ok = expected == r["actualDelta"]
        checked += 1
        if not ok:
            failed.append((r["dish"], f"core.js awarded {r['actualDelta']} but constants={expected} for {cap}"))

    print("=" * 70)
    print("END-TO-END CROSS-CHECK  (real core.js pipeline  vs  constants.delivery_score)")
    print("=" * 70)
    print(f"  deliveries validated: {checked}    skipped (expired/no-spawn): {skipped}")
    if failed:
        print(f"  FAILURES: {len(failed)}")
        for dish, msg in failed:
            print(f"    [{dish}] {msg}")
        sys.exit(1)
    print("  Every core.js delivery score matched constants.delivery_score EXACTLY")


if __name__ == "__main__":
    main()
