"""
Behavior-cloning demo collection from the greedy planner (Phase 2).

Why this exists: pure-/shaped-sparse PPO stalls on this task — completing one
delivery is a ~7–12-macro coordinated sequence, so random exploration never
reaches the first delivery reward and the policy collapses to "do nothing" (see the
Phase-3 build report).  The greedy planner already plays competently, so we clone it
to get the policy off the ground, then let PPO fine-tune past it.

We run the planner exactly as ``agents/benchmark`` does (it drives ``sim.command``),
but through a recording adapter that captures, each decision tick, *which station the
planner sends each chef to*.  Each capture becomes one ``(encode(state, chef), macro)``
example: the planner's station id maps 1:1 back onto the env's macro action space
(``bin_i→FETCH_i``, ``stove→COOK``, ``cutting→CHOP``, ``plating_i→DEPOSIT_i`` or
``TAKE_PLATE_i`` by whether the chef holds something, ``reception_i→DELIVER_i``,
``trash→TRASH``).  Examples whose macro is not mask-valid for that state (rare
jam-breaker shoves) are dropped so the cloned targets are always legal.
"""

import numpy as np

from sim.env import KitchenSim
from sim import constants as C
from sim import encode as E
from agents.planner import Planner


def _map_target_to_macro(target_id, holding):
    """Planner station id (+ the chef's holding) -> macro action index, or None."""
    if target_id is None:
        return None
    kind, _, idx = target_id.partition("_")
    if kind == "bin":
        return E.FETCH_BASE + int(idx)
    if kind == "stove":
        return E.ACT_COOK
    if kind == "cutting":
        return E.ACT_CHOP
    if kind == "plating":
        i = int(idx)
        # empty-handed at a plating area -> lift the plate; otherwise drop a component.
        if holding is None:
            return E.TAKE_PLATE_BASE + i
        if isinstance(holding, dict) and holding.get("type") == "plate":
            return None  # planner never deposits a whole plate onto an area
        return E.DEPOSIT_BASE + i
    if kind == "reception":
        return E.DELIVER_BASE + int(idx)
    if kind == "trash":
        return E.ACT_TRASH
    return None


class _RecordingApi:
    """Wrap the sim's command/boost; remember each successful command's target so the
    collector can turn it into a (state, chef, macro) example."""

    def __init__(self, sim):
        self.sim = sim
        self.issued = {}   # chef_id -> target_id, for successful commands this tick

    def reset_tick(self):
        self.issued = {}

    def command(self, chef_id, target_id):
        r = self.sim.command(chef_id, target_id)
        if r and r.get("success"):
            self.issued[chef_id] = target_id
        return r

    def boost(self, chef_id):
        return self.sim.boost(chef_id)


def collect_demos(seeds, time_cap=C.DEFAULT_TIME_CAP, dt=1.0 / 60.0, decide_every=3,
                  verbose=True):
    """Return ``(obs[N, OBS_DIM] f32, actions[N] i64, masks[N, NUM_ACTIONS] bool)`` of
    planner decisions — masks are the validity masks the policy will also see at
    deploy, so BC trains the policy exactly as it will be queried."""
    obs_list, act_list, mask_list = [], [], []
    kept = dropped = 0
    for s in seeds:
        sim = KitchenSim(s)
        planner = Planner()
        api = _RecordingApi(sim)
        max_ticks = int(round(time_cap / dt))
        tick = 0
        while not sim.game_over and sim.time < time_cap:
            if tick % decide_every == 0:
                state = sim.get_state()           # snapshot BEFORE the planner commands
                api.reset_tick()
                planner.decide(state, api)
                for chef_id, target_id in api.issued.items():
                    holding = state["chefs"][chef_id].get("holding")
                    macro = _map_target_to_macro(target_id, holding)
                    if macro is None:
                        dropped += 1
                        continue
                    mask = E.action_mask(state, chef_id)
                    if mask[macro] <= 0.0:           # keep only legal targets
                        dropped += 1
                        continue
                    obs_list.append(E.encode(state, chef_id))
                    act_list.append(macro)
                    mask_list.append(mask.astype(bool))
                    kept += 1
            sim.tick(dt)
            tick += 1
            if tick > max_ticks + 5:
                break
        if verbose:
            print(f"  seed {s:>4}: kept {kept:>7d}  (dropped {dropped})", end="\r")
    if verbose:
        print()
    obs = np.asarray(obs_list, dtype=np.float32)
    actions = np.asarray(act_list, dtype=np.int64)
    masks = np.asarray(mask_list, dtype=bool)
    return obs, actions, masks


def action_histogram(actions):
    """Counts per macro name (sanity check on the demo distribution)."""
    counts = np.bincount(actions, minlength=E.NUM_ACTIONS)
    return {E.ACTION_NAMES[i]: int(counts[i]) for i in range(E.NUM_ACTIONS) if counts[i] > 0}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Collect planner BC demos.")
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--seed-start", type=int, default=1000)
    ap.add_argument("--cap", type=float, default=C.DEFAULT_TIME_CAP)
    args = ap.parse_args()
    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    obs, actions, masks = collect_demos(seeds, time_cap=args.cap)
    print(f"collected {len(actions)} (obs, action) pairs from {len(seeds)} planner episodes")
    print("action histogram:", action_histogram(actions))
