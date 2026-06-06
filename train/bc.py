"""
Behavior cloning the greedy planner into a MaskablePPO policy (Phase-3 bootstrap).

Pure-/shaped-sparse PPO stalls on this task (exploration can't reach the first
delivery; see the build report).  So we first *clone* the competent Phase-2 planner
with supervised learning — masked cross-entropy of the policy's action distribution
against the planner's macro at each decision — then hand the warm-started policy to
PPO (``train/ppo.py --init-from``) to fine-tune past the planner on the real reward.

The cloned network is the exact MaskableActorCriticPolicy PPO will use, and the obs
are the canonical encoder's features (no VecNormalize), so the saved ``.zip`` is a
drop-in PPO init and, in Phase 4, a drop-in ONNX export.

Run:
    py -m train.bc --demo-seeds 150 --epochs 12 --name bc
    py -m train.ppo --init-from train/runs/bc/bc_model.zip --name ppo_ft ...
"""

import argparse
import os
import sys
import time as _wall

import numpy as np

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from sim import constants as C
from sim import encode as E
from train.demos import collect_demos, action_histogram


def _build_model(device, seed, tensorboard_log=None):
    """A MaskablePPO whose policy/spaces match training; used here only as the BC
    target network (we never step its env during cloning)."""
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv
    from train.ppo import make_train_env
    venv = DummyVecEnv([make_train_env(0, 999, C.DEFAULT_TIME_CAP, 500.0, 250.0, 0.01, 0.0)])
    model = MaskablePPO(
        "MlpPolicy", venv,
        policy_kwargs=dict(net_arch=dict(pi=[256, 256], vf=[256, 256])),
        seed=seed, device=device, tensorboard_log=tensorboard_log, verbose=0,
    )
    return model, venv


def behavior_clone(model, obs, actions, masks, epochs=12, batch_size=512, lr=3e-4,
                   val_frac=0.1, weight_decay=0.0, verbose=True):
    """Supervised masked cross-entropy of the policy distribution vs planner actions."""
    import torch

    device = model.device
    policy = model.policy
    policy.set_training_mode(True)
    opt = torch.optim.Adam(policy.parameters(), lr=lr, weight_decay=weight_decay)

    n = len(actions)
    rng = np.random.default_rng(0)
    perm = rng.permutation(n)
    n_val = int(n * val_frac)
    val_idx, train_idx = perm[:n_val], perm[n_val:]
    act_t_all = torch.as_tensor(actions, device=device)

    def run_split(idx, train):
        total_loss = total_correct = 0
        for start in range(0, len(idx), batch_size):
            b = idx[start:start + batch_size]
            obs_t = policy.obs_to_tensor(obs[b])[0]
            mask_b = torch.as_tensor(masks[b], device=device)
            act_b = act_t_all[b]
            if train:
                opt.zero_grad()
            dist = policy.get_distribution(obs_t, action_masks=mask_b)
            logp = dist.log_prob(act_b)
            loss = -logp.mean()
            if train:
                loss.backward()
                opt.step()
            with torch.no_grad():
                pred = dist.distribution.probs.argmax(dim=-1)
                total_correct += int((pred == act_b).sum().item())
            total_loss += float(loss.item()) * len(b)
        return total_loss / max(1, len(idx)), total_correct / max(1, len(idx))

    history = []
    for ep in range(epochs):
        rng.shuffle(train_idx)
        tr_loss, tr_acc = run_split(train_idx, train=True)
        policy.set_training_mode(False)
        va_loss, va_acc = (run_split(val_idx, train=False) if n_val else (float("nan"), float("nan")))
        policy.set_training_mode(True)
        history.append((tr_loss, tr_acc, va_loss, va_acc))
        if verbose:
            print(f"  epoch {ep+1:>2d}/{epochs}  train loss {tr_loss:.3f} acc {tr_acc:5.1%}   "
                  f"val loss {va_loss:.3f} acc {va_acc:5.1%}")
    policy.set_training_mode(False)
    return history


def main():
    ap = argparse.ArgumentParser(description="Behavior-clone the planner into a MaskablePPO policy.")
    ap.add_argument("--name", default="bc")
    ap.add_argument("--outdir", default=os.path.join("train", "runs"))
    ap.add_argument("--demo-seeds", type=int, default=150)
    ap.add_argument("--demo-seed-start", type=int, default=1000, help="off the 0..29 eval set")
    ap.add_argument("--demo-cap", type=float, default=C.DEFAULT_TIME_CAP)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch-size", type=int, default=512)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--weight-decay", type=float, default=1e-5)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--eval-seeds", type=int, default=10, help="quick in-sim sanity eval (0=skip)")
    args = ap.parse_args()

    run_dir = os.path.join(args.outdir, args.name)
    os.makedirs(run_dir, exist_ok=True)

    seeds = list(range(args.demo_seed_start, args.demo_seed_start + args.demo_seeds))
    print(f"[1/3] collecting planner demos over {len(seeds)} seeds (cap {args.demo_cap:.0f}s)...")
    t0 = _wall.perf_counter()
    obs, actions, masks = collect_demos(seeds, time_cap=args.demo_cap)
    print(f"      {len(actions)} (obs, action) pairs in {_wall.perf_counter()-t0:.1f}s")
    print(f"      action histogram: {action_histogram(actions)}")
    np.savez_compressed(os.path.join(run_dir, "demos.npz"), obs=obs, actions=actions, masks=masks)

    print(f"[2/3] behavior cloning  (obs_dim={E.OBS_DIM}, {E.NUM_ACTIONS} macros, "
          f"epochs={args.epochs})...")
    model, venv = _build_model(args.device, args.seed, tensorboard_log=run_dir)
    behavior_clone(model, obs, actions, masks, epochs=args.epochs,
                   batch_size=args.batch_size, lr=args.lr, weight_decay=args.weight_decay)
    save_path = os.path.join(run_dir, "bc_model")
    model.save(save_path)
    print(f"      saved {save_path}.zip")

    if args.eval_seeds > 0:
        print(f"[3/3] in-sim sanity eval over {args.eval_seeds} seeds...")
        from train.evaluate import evaluate_scores, summarize
        for det in (True, False):
            res = evaluate_scores(model, list(range(args.eval_seeds)), time_cap=C.DEFAULT_TIME_CAP,
                                  deterministic=det)
            s = summarize(res)
            tag = "argmax " if det else "sampled"
            print(f"      {tag}: score mean {s['score']['mean']:>7.0f}  deliv {s['delivered']['mean']:>4.1f}  "
                  f"wrong {s['wrong']['mean']:>4.1f}  surv {s['survival']['mean']:>5.0f}s  "
                  f"maxstreak {s['best_streak']['mean']:.1f}")
        print(f"      (sampled deliveries>0 means PPO can fine-tune from here; the wrong-stand"
              f" rate is what fine-tuning fixes)")
    venv.close()
    print(f"\nNext: py -m train.ppo --init-from {save_path}.zip --name ppo_ft "
          f"--shaping-coef 40 --ent-coef 0.01 --timesteps 3000000")


if __name__ == "__main__":
    main()
