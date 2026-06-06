"""
Phase 3 training — MaskablePPO over the Chef Overflow sim.

Why MaskablePPO (sb3-contrib): the action space is 23 macros (spec §5.3) and most
are invalid at any given moment (COOK with empty hands, DELIVER with no plate, …).
A validity mask from ``ChefOverflowEnv.action_masks()`` is applied to the policy
logits so the policy never spends probability — or rollout samples — on illegal
moves.  That masking is the single biggest sample-efficiency lever here.

Design notes (see docs/RL_DESIGN.md §5/§6 and the Phase-1 findings in §11):

* **Vectorized, headless, fast.**  ``SubprocVecEnv`` over N pure-numpy sim envs
  (the sim does ~70k ticks/s).  On Windows multiprocessing uses *spawn*, so the env
  thunks are top-level/cloudpickle-able and all real work sits under ``__main__``.
* **No VecNormalize.**  The canonical encoder (``sim/encode.py``) already returns
  normalized features, and Phase 4 ports that *same* encoder to JS.  Adding a
  VecNormalize running-stats layer would be a second, un-portable normalizer and a
  train/deploy skew risk, so we keep the encoder as the only normalization and keep
  rewards O(1) with a fixed ``reward_scale`` inside the env instead.
* **Reward** (env default): ``0.01·(Δscore − 500·expiries − 250·wrongs)``.  Expiry is
  strictly worse than a wrong delivery and is *also* a strike (3 → episode ends), so
  protecting streak / avoiding expiry falls out of maximizing long-horizon return.
* **Anticipation is learned, not coded.**  We add no station-utilization or
  pre-staging bonus; the policy gets ``upcomingOrders`` in the obs and a throughput
  incentive and must discover pre-staging on its own.

Logged to TensorBoard each rollout: episode score / deliveries / expiries / wrong /
no-slot / max-streak / survival, plus rollout reward & length; and at each periodic
eval: the deterministic score distribution over fixed seeds (best policy is
checkpointed by mean eval score).

Run (from project root):

    py -m train.ppo --timesteps 3000000 --n-envs 16
    py -m train.ppo --timesteps 50000 --n-envs 8 --name smoke   # quick pipeline check
    tensorboard --logdir train/runs
"""

import argparse
import os
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
from train.evaluate import evaluate_scores, summarize, PLANNER_REF_MEAN


# ---------------------------------------------------------------------------
# Env factory (top-level so SubprocVecEnv's spawn workers can build it)
# ---------------------------------------------------------------------------
def make_train_env(rank, base_seed, time_cap, p_expiry, p_wrong, reward_scale, shaping_coef):
    """Return a thunk that builds one training env.  Each env gets its own seed
    (base_seed+rank) and ``randomize_on_reset=True`` so the vec envs — and every
    auto-reset — replay a fresh, distinct episode rather than one fixed seed."""
    def _init():
        return ChefOverflowEnv(
            seed=base_seed + rank, time_cap=time_cap, p_expiry=p_expiry,
            p_wrong=p_wrong, reward_scale=reward_scale, randomize_on_reset=True,
            shaping_coef=shaping_coef,
        )
    return _init


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
def _import_callbacks():
    from stable_baselines3.common.callbacks import BaseCallback
    return BaseCallback


def build_metrics_callback():
    BaseCallback = _import_callbacks()

    class MetricsCallback(BaseCallback):
        """Average the game-specific per-episode metrics that VecMonitor stashes in
        each episode's info (via ``info_keywords``) and push them to TensorBoard at
        every rollout end, alongside SB3's own rollout/ep_rew_mean & ep_len_mean."""

        TAGS = {
            "score": "episode/score",
            "delivered": "episode/deliveries",
            "expired": "episode/expiries",
            "wrong": "episode/wrong",
            "no_slot": "episode/no_slot",
            "best_streak": "episode/max_streak",
            "sim_time": "episode/survival_s",
        }

        def _on_step(self):  # required override
            return True

        def _on_rollout_end(self):
            buf = self.model.ep_info_buffer
            if not buf:
                return
            for key, tag in self.TAGS.items():
                vals = [ep[key] for ep in buf if key in ep]
                if vals:
                    self.logger.record(tag, float(np.mean(vals)))

    return MetricsCallback()


def build_score_eval_callback(eval_seeds, eval_cap, eval_freq_calls, save_path,
                              deterministic=True):
    BaseCallback = _import_callbacks()

    class ScoreEvalCallback(BaseCallback):
        """Periodically replay the policy deterministically over a fixed seed set,
        log the *raw game score* distribution to TensorBoard, and checkpoint the
        best-by-mean-score policy (the Phase-3 gate metric).  Compared against the
        published planner reference for a quick gate signal; the rigorous
        same-seed planner re-run lives in train/evaluate.py."""

        def __init__(self):
            super().__init__(verbose=1)
            self.best_mean = -float("inf")

        def _on_step(self):
            if eval_freq_calls > 0 and self.n_calls % eval_freq_calls == 0:
                self._run_eval()
            return True

        def _run_eval(self):
            t0 = _wall.perf_counter()
            results = evaluate_scores(self.model, eval_seeds, time_cap=eval_cap,
                                      deterministic=deterministic)
            s = summarize(results)
            sc = s["score"]
            self.logger.record("eval/score_mean", sc["mean"])
            self.logger.record("eval/score_median", sc["median"])
            self.logger.record("eval/score_min", sc["min"])
            self.logger.record("eval/score_max", sc["max"])
            self.logger.record("eval/deliveries_mean", s["delivered"]["mean"])
            self.logger.record("eval/expiries_mean", s["expired"]["mean"])
            self.logger.record("eval/survival_mean", s["survival"]["mean"])
            self.logger.record("eval/vs_planner_ref", sc["mean"] - PLANNER_REF_MEAN)
            if sc["mean"] > self.best_mean:
                self.best_mean = sc["mean"]
                self.model.save(os.path.join(save_path, "best_model"))
            self.logger.record("eval/best_score_mean", self.best_mean)
            took = _wall.perf_counter() - t0
            if self.verbose:
                print(f"[eval @ {self.num_timesteps:>9d} steps] "
                      f"score mean {sc['mean']:>8.1f}  median {sc['median']:>8.1f}  "
                      f"min {sc['min']:>7.1f}  max {sc['max']:>7.1f}  "
                      f"deliv {s['delivered']['mean']:>5.1f}  surv {s['survival']['mean']:>6.0f}s  "
                      f"best {self.best_mean:>8.1f}  ({took:.1f}s)")

    return ScoreEvalCallback()


# ---------------------------------------------------------------------------
# Vec env
# ---------------------------------------------------------------------------
def build_venv(n_envs, base_seed, time_cap, p_expiry, p_wrong, reward_scale,
               shaping_coef, force_dummy=False):
    from stable_baselines3.common.vec_env import SubprocVecEnv, DummyVecEnv, VecMonitor

    thunks = [make_train_env(i, base_seed, time_cap, p_expiry, p_wrong, reward_scale, shaping_coef)
              for i in range(n_envs)]
    VecCls = DummyVecEnv if (force_dummy or n_envs == 1) else SubprocVecEnv
    venv = VecCls(thunks)
    # info_keywords copy these scalars from each episode's final info into the
    # episode summary so MetricsCallback can average them.
    venv = VecMonitor(venv, info_keywords=("score", "delivered", "expired", "wrong",
                                           "no_slot", "best_streak", "sim_time"))
    return venv


# ---------------------------------------------------------------------------
# Train
# ---------------------------------------------------------------------------
def train(args):
    import torch
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.callbacks import CheckpointCallback, CallbackList

    if args.torch_threads > 0:
        torch.set_num_threads(args.torch_threads)

    run_dir = os.path.join(args.outdir, args.name)
    os.makedirs(run_dir, exist_ok=True)

    venv = build_venv(args.n_envs, args.base_seed, args.time_cap, args.p_expiry,
                      args.p_wrong, args.reward_scale, args.shaping_coef,
                      force_dummy=args.dummy)

    policy_kwargs = dict(net_arch=dict(pi=[256, 256], vf=[256, 256]))
    model = MaskablePPO(
        "MlpPolicy", venv,
        learning_rate=args.lr,
        n_steps=args.n_steps,
        batch_size=args.batch_size,
        n_epochs=args.n_epochs,
        gamma=args.gamma,
        gae_lambda=args.gae_lambda,
        clip_range=args.clip_range,
        ent_coef=args.ent_coef,
        vf_coef=args.vf_coef,
        max_grad_norm=args.max_grad_norm,
        target_kl=args.target_kl,
        policy_kwargs=policy_kwargs,
        tensorboard_log=run_dir,
        seed=args.seed,
        device=args.device,
        verbose=1,
    )

    if args.init_from:
        # Warm-start PPO from a saved policy (the BC clone): copy its network weights
        # into this fresh model so PPO keeps our hyperparameters but the cloned policy.
        bc = MaskablePPO.load(args.init_from, device=args.device)
        model.policy.load_state_dict(bc.policy.state_dict())
        del bc
        print(f"  init policy weights from {args.init_from}")

    eval_seeds = list(range(args.n_eval_seeds))
    eval_freq_calls = max(1, args.eval_freq // args.n_envs)
    metrics_cb = build_metrics_callback()
    score_cb = build_score_eval_callback(eval_seeds, args.eval_cap, eval_freq_calls,
                                          run_dir, deterministic=True)
    ckpt_cb = CheckpointCallback(
        save_freq=max(1, args.checkpoint_freq // args.n_envs),
        save_path=os.path.join(run_dir, "checkpoints"), name_prefix="ppo")
    callbacks = CallbackList([metrics_cb, score_cb, ckpt_cb])

    print("=" * 80)
    print(f"MaskablePPO  |  run_dir={run_dir}")
    print(f"  obs_dim={E.OBS_DIM}  n_actions={E.NUM_ACTIONS}  net=pi/vf[256,256]")
    print(f"  n_envs={args.n_envs} ({'Dummy' if (args.dummy or args.n_envs==1) else 'Subproc'})  "
          f"n_steps={args.n_steps}  rollout={args.n_envs*args.n_steps}  "
          f"batch={args.batch_size}  epochs={args.n_epochs}")
    print(f"  lr={args.lr}  gamma={args.gamma}  gae={args.gae_lambda}  clip={args.clip_range}  "
          f"ent={args.ent_coef}  vf={args.vf_coef}")
    print(f"  reward={args.reward_scale}*(dScore - {args.p_expiry:.0f}*expiry "
          f"- {args.p_wrong:.0f}*wrong + {args.shaping_coef:.0f}*dPhi)  "
          f"train_cap={args.time_cap:.0f}s")
    print(f"  eval: {args.n_eval_seeds} seeds @ cap {args.eval_cap:.0f}s every "
          f"{args.eval_freq} steps (planner ref mean {PLANNER_REF_MEAN:.0f})")
    print(f"  total_timesteps={args.timesteps}  device={args.device}")
    print("=" * 80)

    t0 = _wall.perf_counter()
    model.learn(total_timesteps=args.timesteps, callback=callbacks,
                tb_log_name="tb", progress_bar=False)
    elapsed = _wall.perf_counter() - t0

    model.save(os.path.join(run_dir, "final_model"))
    venv.close()
    sps = args.timesteps / elapsed if elapsed > 0 else 0.0
    print(f"\nDONE in {elapsed/60:.1f} min  ({sps:.0f} steps/s).  "
          f"best eval mean score {score_cb.best_mean:.1f}.")
    print(f"  saved: {os.path.join(run_dir, 'best_model.zip')} (best), "
          f"{os.path.join(run_dir, 'final_model.zip')} (final)")
    print(f"  evaluate: py -m train.evaluate --model {os.path.join(run_dir, 'best_model.zip')}")
    return run_dir, score_cb.best_mean


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_argparser():
    ap = argparse.ArgumentParser(description="Train MaskablePPO on Chef Overflow.")
    # run
    ap.add_argument("--name", default="ppo", help="run name under --outdir")
    ap.add_argument("--outdir", default=os.path.join("train", "runs"))
    ap.add_argument("--timesteps", type=int, default=3_000_000)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--torch-threads", type=int, default=0, help="0 = leave torch default")
    ap.add_argument("--init-from", default=None,
                    help="load policy weights from a saved .zip (e.g. a BC model) before PPO")
    ap.add_argument("--target-kl", type=float, default=None,
                    help="early-stop a PPO epoch past this KL (stabilizes BC fine-tuning)")
    # vec env
    ap.add_argument("--n-envs", type=int, default=16)
    ap.add_argument("--base-seed", type=int, default=100_000, help="env seeds; kept off eval 0..29")
    ap.add_argument("--dummy", action="store_true", help="DummyVecEnv (debug, no subprocs)")
    ap.add_argument("--time-cap", type=float, default=1800.0, help="training episode cap (s)")
    # reward
    ap.add_argument("--p-expiry", type=float, default=500.0)
    ap.add_argument("--p-wrong", type=float, default=250.0)
    ap.add_argument("--reward-scale", type=float, default=0.01)
    ap.add_argument("--shaping-coef", type=float, default=30.0,
                    help="potential-based assembly shaping (raw pts/component; 0=pure sparse)")
    # PPO hyperparams
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--n-steps", type=int, default=512)
    ap.add_argument("--batch-size", type=int, default=1024)
    ap.add_argument("--n-epochs", type=int, default=4)
    ap.add_argument("--gamma", type=float, default=0.999)
    ap.add_argument("--gae-lambda", type=float, default=0.95)
    ap.add_argument("--clip-range", type=float, default=0.2)
    ap.add_argument("--ent-coef", type=float, default=0.01)
    ap.add_argument("--vf-coef", type=float, default=0.5)
    ap.add_argument("--max-grad-norm", type=float, default=0.5)
    # eval / checkpoint
    ap.add_argument("--n-eval-seeds", type=int, default=30, help="eval seeds 0..N-1 (planner set)")
    ap.add_argument("--eval-cap", type=float, default=C.DEFAULT_TIME_CAP, help="eval cap (s); planner-aligned")
    ap.add_argument("--eval-freq", type=int, default=100_000, help="eval every N timesteps")
    ap.add_argument("--checkpoint-freq", type=int, default=500_000)
    return ap


def main():
    args = build_argparser().parse_args()
    train(args)


if __name__ == "__main__":
    main()
