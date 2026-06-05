"""
Benchmark the greedy planner (agents/planner.py) across many seeds in the sim.

Drives :class:`sim.env.KitchenSim` exactly the way ``agents/planner.js`` will drive
the live game: every tick we hand the planner ``sim.get_state()`` (the same shape as
``KitchenAPI.getState()``) and a thin ``api`` adapter wrapping ``command``/``boost``.

Run from the project root:

    py -m agents.benchmark            # 30 seeds, default 1200 s cap
    py -m agents.benchmark --seeds 50 --cap 1200 --decide-hz 20

Reports mean / median of score, deliveries, fails (expiry + wrong + no-slot),
max streak and survival time.
"""

import argparse
import statistics
import time as _wall

from sim.env import KitchenSim
from sim import constants as C
from agents.planner import Planner


class SimApi:
    """``command`` / ``boost`` adapter so the planner is byte-for-byte the same code
    here as against the browser's ``KitchenAPI``."""

    def __init__(self, sim):
        self.sim = sim

    def command(self, chef_id, target_id):
        return self.sim.command(chef_id, target_id)

    def boost(self, chef_id):
        return self.sim.boost(chef_id)


def run_episode(seed, time_cap=C.DEFAULT_TIME_CAP, dt=1.0 / 60.0, decide_every=3):
    """Run one full episode under the planner. ``decide_every`` throttles planner
    calls to every N ticks (decisions only matter when a chef goes idle, so this is
    behaviourally ~identical to every-tick while running far faster).  At 60 fps,
    decide_every=3 ≈ 20 Hz."""
    sim = KitchenSim(seed)
    planner = Planner()
    api = SimApi(sim)

    max_ticks = int(round(time_cap / dt))
    tick = 0
    while not sim.game_over and sim.time < time_cap:
        if tick % decide_every == 0:
            # Call every cycle (mirrors planner.js's onTick): the planner also needs
            # to run its jam-breaker when chefs are walking-blocked, i.e. not "idle".
            planner.decide(sim.get_state(), api)
        sim.tick(dt)
        tick += 1
        if tick > max_ticks + 5:
            break

    fails = sim.expired_total + sim.wrong_total + sim.no_slot_total
    return {
        "seed": seed,
        "score": sim.score,
        "delivered": sim.delivered_total,
        "expired": sim.expired_total,
        "wrong": sim.wrong_total,
        "no_slot": sim.no_slot_total,
        "fails": fails,
        "max_streak": sim.best_streak,
        "survival": sim.time,
        "game_over": sim.game_over,
    }


def _fmt(label, xs, prec=1):
    mean = statistics.mean(xs)
    med = statistics.median(xs)
    return f"  {label:<14} mean {mean:>10.{prec}f}   median {med:>10.{prec}f}   min {min(xs):>9.{prec}f}   max {max(xs):>9.{prec}f}"


def benchmark(seeds, time_cap=C.DEFAULT_TIME_CAP, decide_every=3, verbose=True):
    results = []
    t0 = _wall.perf_counter()
    for s in seeds:
        r = run_episode(s, time_cap=time_cap, decide_every=decide_every)
        results.append(r)
        if verbose:
            print(f"seed {s:>3}: score {r['score']:>10.1f}  deliv {r['delivered']:>3}  "
                  f"exp {r['expired']}  wrong {r['wrong']}  noslot {r['no_slot']}  "
                  f"streak {r['max_streak']:>3}  t {r['survival']:>7.1f}  "
                  f"{'OVER' if r['game_over'] else 'cap'}")
    elapsed = _wall.perf_counter() - t0

    print("\n" + "=" * 78)
    print(f"PLANNER BENCHMARK — {len(seeds)} seeds, cap {time_cap:.0f}s, "
          f"decide@{60 // decide_every}Hz, {elapsed:.1f}s wall")
    print("=" * 78)
    print(_fmt("score", [r["score"] for r in results]))
    print(_fmt("deliveries", [r["delivered"] for r in results], prec=1))
    print(_fmt("fails(total)", [r["fails"] for r in results], prec=1))
    print(_fmt("  expiry", [r["expired"] for r in results], prec=1))
    print(_fmt("  wrong", [r["wrong"] for r in results], prec=1))
    print(_fmt("  no-slot", [r["no_slot"] for r in results], prec=1))
    print(_fmt("max streak", [r["max_streak"] for r in results], prec=1))
    print(_fmt("survival(s)", [r["survival"] for r in results], prec=1))
    n_over = sum(1 for r in results if r["game_over"])
    print(f"\n  reached 3-strike game-over: {n_over}/{len(seeds)}   "
          f"survived to cap: {len(seeds) - n_over}/{len(seeds)}")
    return results


def main():
    ap = argparse.ArgumentParser(description="Benchmark the greedy Chef Overflow planner.")
    ap.add_argument("--seeds", type=int, default=30, help="number of seeds (0..N-1)")
    ap.add_argument("--cap", type=float, default=C.DEFAULT_TIME_CAP, help="sim-time cap (s)")
    ap.add_argument("--decide-hz", type=int, default=20, help="planner decision rate (Hz)")
    ap.add_argument("--seed-start", type=int, default=0)
    args = ap.parse_args()

    decide_every = max(1, round(60 / max(1, args.decide_hz)))
    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    benchmark(seeds, time_cap=args.cap, decide_every=decide_every)


if __name__ == "__main__":
    main()
