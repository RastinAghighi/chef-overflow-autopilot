"""Cross-check sim/constants.py against the JS oracle (tools/xcheck_core.mjs).

The .mjs file runs the deterministic formulas copied verbatim from
reference/core.js under real Node arithmetic. We re-derive the same values from
sim/constants.py and assert exact agreement (difficulty/spawn/score are exact
floats; core.js and Python use identical IEEE-754 doubles, so they must match to
the bit). Prints a PASS/FAIL summary with any mismatches.

    py tools/xcheck_core.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from sim import constants as C  # noqa: E402


def _perf(d, f, s):
    return C.perf(d, f, s)


def main():
    mjs = os.path.join(HERE, "xcheck_core.mjs")
    raw = subprocess.run(["node", mjs], capture_output=True, text=True, check=True).stdout
    js = json.loads(raw)

    mismatches = []
    counts = {}

    def check(name, ok):
        counts[name] = counts.get(name, [0, 0])
        counts[name][0] += 1
        if ok:
            counts[name][1] += 1
        else:
            mismatches.append(name)

    # phase
    for t, phase in js["phase"]:
        check("phase", C.phase_key(t) == phase)

    # vip probability (exact float)
    for t, vp in js["vip"]:
        check("vip_prob", C.vip_probability(t) == vp)

    # recipe pool membership
    for t, names in js["pool"]:
        py = C.recipe_pool(t)
        py_set = set(C.DISH_NAMES) if names is None else set(names)
        # core.js returns null for endurance (full table); constants returns the
        # full list. Compare as sets of names.
        check("pool", set(py) == py_set)

    # difficulty (exact float)
    for t, d, f, s, val in js["difficulty"]:
        got = C.compute_difficulty(t, _perf(d, f, s))
        check("difficulty", got == val)

    # spawn interval (exact float)
    for t, rush, d, f, s, val in js["spawn"]:
        got = C.base_spawn_interval(t, bool(rush), _perf(d, f, s))
        check("spawn_interval", got == val)

    # order-time-limit core (int core + int range)
    for t, d, f, s, pair in js["timelimit"]:
        core, rng = C.order_time_limit_core(t, _perf(d, f, s))
        check("order_time_core", [core, rng] == pair)

    # delivery score (int)
    for d, tl, s, v, val in js["score"]:
        got = C.delivery_score(d, tl, s, bool(v))
        check("delivery_score", got == val)

    print("=" * 70)
    print("CORE FORMULA CROSS-CHECK  (sim/constants.py  vs  reference/core.js)")
    print("=" * 70)
    total = 0
    passed = 0
    for name, (n, ok) in sorted(counts.items()):
        total += n
        passed += ok
        flag = "OK " if ok == n else "FAIL"
        print(f"  [{flag}] {name:<18} {ok}/{n} exact")
    print("-" * 70)
    print(f"  {passed}/{total} checks exact across {len(counts)} formula families")
    if mismatches:
        print("\n  MISMATCH families:", sorted(set(mismatches)))
        sys.exit(1)
    print("  ALL FORMULAS MATCH core.js EXACTLY")


if __name__ == "__main__":
    main()
