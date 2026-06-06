"""
Phase 3 evaluation — score a trained MaskablePPO policy in the sim and compare it
to the greedy planner baseline on the *same seeds and time cap*.

The Phase 3 gate (docs/RL_DESIGN.md §6/§9): the trained policy's mean score must
clearly beat the planner's mean in sim.  The planner baseline reported elsewhere is
mean ≈ 4392 / max ≈ 6795 over 30 seeds at the 1200 s cap; to avoid comparing against
a stale number we *re-run* the planner here on the identical seeds and cap.

Determinism: each seed is replayed with ``randomize_on_reset=False`` and an explicit
seed, and the policy acts greedily (argmax over the masked logits).  Observations are
the encoder's own normalized features — there is no VecNormalize layer — so a saved
policy needs nothing but its weights to reproduce these numbers (and, in Phase 4, to
run in the browser).

Usage (from the project root):

    py -m train.evaluate --model train/runs/<run>/best_model.zip
    py -m train.evaluate --model train/runs/<run>/best_model.zip --seeds 30 --cap 1200
    py -m train.evaluate --model ... --no-planner          # skip the baseline re-run
"""

import argparse
import os
import statistics
import sys
import time as _wall

import numpy as np

try:  # never let a stray non-ASCII char crash a run on a cp1252 Windows console
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from sim.env import ChefOverflowEnv
from sim import encode as E
from sim import constants as C

# Published planner baseline (30 seeds, 1200 s cap) — also re-measured live below.
PLANNER_REF_MEAN = 4392.0
PLANNER_REF_MAX = 6795.0

# Per-seed metric keys we report (read straight from the env's info dict).
_METRIC_KEYS = ("score", "delivered", "expired", "wrong", "no_slot",
                "best_streak", "survival", "game_over")


# ---------------------------------------------------------------------------
# Batched policy rollout (one model forward per step across all seeds)
# ---------------------------------------------------------------------------
def _ablate_upcoming(obs):
    """Zero the upcoming-orders block (the final UPCOMING_DIM features) in-place — an
    ablation that hides the look-ahead signal from the policy without retraining."""
    obs[:, E.OBS_DIM - E.UPCOMING_DIM:] = 0.0
    return obs


def evaluate_scores(model, seeds, time_cap=C.DEFAULT_TIME_CAP, dt=1.0 / 60.0,
                    deterministic=True, p_expiry=500.0, p_wrong=250.0,
                    reward_scale=0.01, ablate_upcoming=False):
    """Roll the policy through one deterministic episode per seed and return a list
    of per-seed result dicts.

    All envs are stepped in lockstep so the policy's forward pass is batched over the
    whole seed set (fast); an env that has terminated/truncated is simply not stepped
    again.  Reward shaping is irrelevant here (we report raw game score), but the env
    is built with the same params for parity.

    ``ablate_upcoming`` blanks the upcoming-orders features each step — if the policy
    scores worse with them hidden, it is provably *using* the anticipation signal.
    """
    envs = [ChefOverflowEnv(seed=s, time_cap=time_cap, dt=dt, p_expiry=p_expiry,
                            p_wrong=p_wrong, reward_scale=reward_scale,
                            randomize_on_reset=False) for s in seeds]
    obs = np.zeros((len(envs), E.OBS_DIM), dtype=np.float32)
    for i, (env, s) in enumerate(zip(envs, seeds)):
        o, _ = env.reset(seed=s)
        obs[i] = o
    if ablate_upcoming:
        _ablate_upcoming(obs)
    done = [False] * len(envs)
    results = [None] * len(envs)

    while not all(done):
        # action_masks() always allows WAIT, so done envs still yield a legal mask.
        masks = np.stack([env.action_masks() for env in envs])
        actions, _ = model.predict(obs, action_masks=masks, deterministic=deterministic)
        actions = np.asarray(actions).reshape(-1)
        for i, env in enumerate(envs):
            if done[i]:
                continue
            o, _r, terminated, truncated, info = env.step(int(actions[i]))
            obs[i] = o
            if ablate_upcoming:
                obs[i, E.OBS_DIM - E.UPCOMING_DIM:] = 0.0
            if terminated or truncated:
                done[i] = True
                results[i] = {
                    "seed": seeds[i],
                    "score": float(info["score"]),
                    "delivered": int(info["delivered"]),
                    "expired": int(info["expired"]),
                    "wrong": int(info["wrong"]),
                    "no_slot": int(info["no_slot"]),
                    "best_streak": int(info["best_streak"]),
                    "survival": float(info["sim_time"]),
                    "game_over": bool(terminated),
                }
    return results


# ---------------------------------------------------------------------------
# Planner baseline on the same seeds/cap (re-measured, not hard-coded)
# ---------------------------------------------------------------------------
def planner_scores(seeds, time_cap=C.DEFAULT_TIME_CAP, decide_every=3):
    from agents.benchmark import run_episode as _planner_episode
    out = []
    for s in seeds:
        r = _planner_episode(s, time_cap=time_cap, decide_every=decide_every)
        out.append({
            "seed": s, "score": float(r["score"]), "delivered": int(r["delivered"]),
            "expired": int(r["expired"]), "wrong": int(r["wrong"]),
            "no_slot": int(r["no_slot"]), "best_streak": int(r["max_streak"]),
            "survival": float(r["survival"]), "game_over": bool(r["game_over"]),
        })
    return out


# ---------------------------------------------------------------------------
# Aggregation / reporting
# ---------------------------------------------------------------------------
def summarize(results):
    s = {}
    for k in ("score", "delivered", "expired", "wrong", "no_slot", "best_streak", "survival"):
        xs = [r[k] for r in results]
        s[k] = {"mean": statistics.mean(xs), "median": statistics.median(xs),
                "min": min(xs), "max": max(xs)}
    s["n"] = len(results)
    s["game_over"] = sum(1 for r in results if r["game_over"])
    return s


def _row(label, st, prec=1):
    d = st
    return (f"  {label:<13} mean {d['mean']:>9.{prec}f}   median {d['median']:>9.{prec}f}"
            f"   min {d['min']:>8.{prec}f}   max {d['max']:>8.{prec}f}")


def print_report(agent, planner=None, seeds=None, time_cap=C.DEFAULT_TIME_CAP):
    a = summarize(agent)
    print("\n" + "=" * 80)
    print(f"AGENT (MaskablePPO) - {a['n']} seeds, cap {time_cap:.0f}s, deterministic")
    print("=" * 80)
    print(_row("score", a["score"]))
    print(_row("deliveries", a["delivered"]))
    print(_row("expiries", a["expired"]))
    print(_row("wrong", a["wrong"]))
    print(_row("no-slot", a["no_slot"]))
    print(_row("max streak", a["best_streak"]))
    print(_row("survival(s)", a["survival"]))
    print(f"  reached 3-strike game-over: {a['game_over']}/{a['n']}   "
          f"survived to cap: {a['n'] - a['game_over']}/{a['n']}")

    if planner is not None:
        p = summarize(planner)
        print("\n" + "-" * 80)
        print(f"PLANNER baseline (re-measured, same seeds/cap)")
        print("-" * 80)
        print(_row("score", p["score"]))
        print(_row("deliveries", p["delivered"]))
        print(_row("max streak", p["best_streak"]))
        print(_row("survival(s)", p["survival"]))
        amean, pmean = a["score"]["mean"], p["score"]["mean"]
        delta = amean - pmean
        pct = (delta / pmean * 100.0) if pmean else float("nan")
        print("\n" + "=" * 80)
        print("PHASE 3 GATE - agent mean must clearly beat planner mean")
        print("=" * 80)
        print(f"  agent mean   {amean:>10.1f}")
        print(f"  planner mean {pmean:>10.1f}   (published ref {PLANNER_REF_MEAN:.0f})")
        print(f"  delta        {delta:>+10.1f}  ({pct:+.1f}%)")
        verdict = "PASS" if amean > pmean else "NOT MET"
        print(f"  GATE: {verdict}")
    else:
        print(f"\n  (planner baseline skipped; published ref mean {PLANNER_REF_MEAN:.0f} / "
              f"max {PLANNER_REF_MAX:.0f})")
    print()
    return a


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Evaluate a trained Chef Overflow policy.")
    ap.add_argument("--model", required=True, help="path to a saved MaskablePPO .zip")
    ap.add_argument("--seeds", type=int, default=30, help="number of seeds (0..N-1)")
    ap.add_argument("--seed-start", type=int, default=0)
    ap.add_argument("--cap", type=float, default=C.DEFAULT_TIME_CAP, help="sim-time cap (s)")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--stochastic", action="store_true", help="sample instead of argmax")
    ap.add_argument("--ablate-upcoming", action="store_true",
                    help="zero the upcomingOrders features (anticipation ablation)")
    ap.add_argument("--no-planner", action="store_true", help="skip the planner re-run")
    ap.add_argument("--per-seed", action="store_true", help="print every seed's row")
    args = ap.parse_args()

    from sb3_contrib import MaskablePPO
    model = MaskablePPO.load(args.model, device=args.device)
    seeds = list(range(args.seed_start, args.seed_start + args.seeds))

    t0 = _wall.perf_counter()
    agent = evaluate_scores(model, seeds, time_cap=args.cap,
                            deterministic=not args.stochastic,
                            ablate_upcoming=args.ablate_upcoming)
    t_agent = _wall.perf_counter() - t0
    if args.ablate_upcoming:
        print("\n[ablation] upcomingOrders features ZEROED for this run.")

    if args.per_seed:
        print(f"\nmodel: {os.path.abspath(args.model)}")
        for r in agent:
            print(f"  seed {r['seed']:>3}: score {r['score']:>9.1f}  deliv {r['delivered']:>3}  "
                  f"exp {r['expired']}  wrong {r['wrong']}  noslot {r['no_slot']}  "
                  f"streak {r['best_streak']:>3}  t {r['survival']:>7.1f}  "
                  f"{'OVER' if r['game_over'] else 'cap'}")

    planner = None
    if not args.no_planner:
        planner = planner_scores(seeds, time_cap=args.cap)

    print_report(agent, planner, seeds=seeds, time_cap=args.cap)
    print(f"  (agent eval {t_agent:.1f}s wall over {len(seeds)} seeds)")


if __name__ == "__main__":
    main()
