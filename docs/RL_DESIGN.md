# Chef Overflow Autopilot — RL Design Spec

**Status:** source of truth for the build. Claude Code implements *against this doc*; re-read it when context drifts. Everything in §3 marked **[confirmed]** was read out of `game.js`; everything marked **[extract]** must be read from the source and matched exactly — do not guess these.

---

## 1. Objective & constraints

Build an agent that maximizes leaderboard score in Hack the 6ix "Chef Overflow."

- The scored run happens **on their live site** (`hackthe6ix-chefoverflow.vercel.app`), signed into HT6. Writes go through their auth-gated `/api/*` functions, bound to a per-run token, with server-side plausibility validation. **The simulator never submits anything** — it exists only to train and evaluate. The trained policy is deployed *into a real browser session* of their site (§8).
- Therefore everything we build must be faithful to the real game (a policy trained on a wrong sim will fail on submission), and the obs/action encoding used in the sim must be reproduced **identically** in the browser deploy.

## 2. Repo layout (`chef-overflow-autopilot/`)

```
docs/RL_DESIGN.md          this file
reference/game.js          local copy of their game.js — GITIGNORED, never committed (their copyright)
sim/env.py                 the Gym environment (Phase 1)
sim/constants.py           all extracted constants/formulas in one place
sim/encode.py              canonical observation + action-mask builder (shared contract)
agents/planner.py          greedy baseline in sim (Phase 2)
agents/planner.js          same baseline as in-browser JS (real-game fallback)
train/ppo.py               PPO training (Phase 3)
deploy/policy.onnx         exported policy (Phase 4)
deploy/run_agent.js        in-browser inference loop (Phase 4)
tests/test_fidelity.py     sim-vs-real trace checks (Phase 1/2 gate)
```

Add `reference/` to `.gitignore`. The game source is read-only ground truth, not part of our project.

## 3. Game model — ground truth

### 3.1 World & stations
Grid kitchen. Stations: 6 ingredient bins, 3 stoves, 2 cutting boards, 4 plating areas, 5 reception stands, 1 trash, ~15 counters. Bins map fixed: `bin_0` tomato, `bin_1` lettuce, `bin_2` onion, `bin_3` meat, `bin_4` dough, `bin_5` cheese **[confirmed]**. Grid dimensions, wall layout, station coordinates, pathing **[extract]** — or read live coords from `getState()`.

### 3.2 Chefs & the commitment rule
5 chefs. Each holds one thing at a time: `null`, `{ingredient, state}`, or `{type:'plate', items:[...]}`. A chef is **locked in place while processing** at a stove/board (cannot move until it finishes) **[confirmed]**. **Mid-route redirects pay a stall penalty** — the engine "rewards committed plans, punishes per-tick redirects" **[confirmed]**. *This is the single most important design constraint: do not re-issue commands every frame. Commit a chef to a task and leave it.* Exact stall magnitude + deadlock-reassignment logic **[extract]**.

### 3.3 Ingredient → state pipeline
- **raw** — picked up from a bin (FETCH).
- **chopped** — raw → cutting board (blocking). Applies to lettuce, tomato, onion.
- **cooked** — raw → stove (blocking, **never burns**, auto-hands the cooked item back) **[confirmed]**. Applies to meat (always) and dough (pizza base only).
- Dough is used **raw** (burger bun) *and* **cooked** (pizza base), so COOK vs. plate-raw must stay distinct actions. Cheese is raw-only; lettuce/tomato/onion chopped-only.
- Exact cook time and chop time (processing durations) **[extract]** — these are not yet known and are required for the sim.

### 3.4 Recipes (components — [confirmed])

| Dish | Components (ingredient @ state) |
|---|---|
| Salad | lettuce@chopped, tomato@chopped |
| Steak | meat@cooked |
| Burger | meat@cooked, dough@raw |
| Pizza | dough@cooked, tomato@chopped, cheese@raw |
| Deluxe Burger | meat@cooked, dough@raw, onion@chopped |
| Feast Platter | meat@cooked, lettuce@chopped, tomato@chopped, cheese@raw |
| Supreme Pizza | dough@cooked, tomato@chopped, onion@chopped, cheese@raw |

Each recipe object also has a static `difficulty` field (seen in source) — this is **not** the scoring multiplier (that's the time-based `GameState.difficulty`, §3.9); it's likely spawn-weighting/display. Confirm its use **[extract]**.

### 3.5 Plating & delivery
Plating areas hold unlimited items. Depositing a held plate **merges** its items into the area; an empty-handed chef at an area **picks up all items as a plate** **[confirmed]**. Delivery requires the plate's contents to **exactly match** the order's components — no extras (a 6-meat stack is rejected as "wrong order") **[confirmed]**. A **wrong delivery resets streak to 0 AND clears the chef's hands** (you lose the assembled ingredients) **[confirmed]** — so wrong deliveries are doubly catastrophic. After a correct delivery the stand is occupied ~10s while the customer "eats" **[confirmed]**, then frees. Whether the UI's Sink / Dish-Rack are functional plate-lifecycle mechanics or cosmetic **[extract]**.

### 3.6 Order spawning — interval (avg seconds between orders) **[confirmed]**
```
smoothstep(x,a,b): t = clamp((x-a)/(b-a),0,1); return t*t*(3-2t)
spawnInterval(time, rushActive):
  if time<60:   n = 20 - smoothstep(time,0,55)*8        # 20 -> 12
  elif time<150:n = 12 - smoothstep(time,60,145)*4      # 12 -> 8
  elif time<600:n = 8  - smoothstep(time,150,580)*4     # 8  -> 4
  else:         n = max(2.5, 4 - (time-600)*0.003)      # 4  -> 2.5
  if rushActive: n *= 0.70
  return max(2.5, n * (1 - perf()*0.35))                # perf = §3.10
```

### 3.7 Order time limits **[confirmed]**
```
orderTimeLimit(time):
  if time<60:    return 52 + randint(0,5)               # tutorial, no perf
  if time<150:   return 40 + randint(0,5)               # ramp, no perf
  if time>=600:  sec = max(14, 22 - (time-600)*0.012)   # endurance, 22 -> 14
  else:          sec = round(38 - smoothstep(time,150,520)*16)  # automation, 38 -> 22
  sec = max(14, sec)
  sec = round(sec * (1 - perf()*0.22))
  return sec + randint(0,4)
```

### 3.8 Phases & recipe-pool unlocks **[confirmed]**
Phase by elapsed time: `tutorial` <60s, `ramp` <150s, `automation` <600s, `endurance` ≥600s.
```
pool(time):
  tutorial:  [Salad, Steak]
  ramp:      [Salad, Steak, Burger]
  automation: rel = time-150; pool = [Salad, Steak, Burger]
              + Pizza         if rel>=35   (t>=185)
              + Deluxe Burger if rel>=95   (t>=245)
              + Feast Platter if rel>=170  (t>=320)
              + Supreme Pizza if rel>=255  (t>=405)
  endurance: all 7
```

### 3.9 Difficulty multiplier **[confirmed]**
```
difficulty(time):
  if time<60:    base = 1.0 + smoothstep(time,0,58)*0.1     # 1.0 -> 1.1
  elif time<150: base = 1.1 + smoothstep(time,60,148)*0.5   # 1.1 -> 1.6
  elif time<600: base = 1.6 + smoothstep(time,150,595)*1.6  # 1.6 -> 3.2
  else:          base = 3.2 + (time-600)*0.006               # +0.006/s
  return max(1.0, base * (1 + perf()))
```

### 3.10 Performance rubber-band **[confirmed]** — the key dynamic
```
perf():
  sr = (delivered+failed>0) ? delivered/(delivered+failed) : 0.6
  return clamp( (sr-0.55)*0.28 + min(0.2, streak*0.01) - min(0.18, failed*0.05), -0.2, 0.3 )
```
`perf` feeds spawn speed, order timers, **and** difficulty simultaneously. **The better you play, the faster orders arrive, the tighter their clocks, and the higher each delivery scores** — bounded by the clamp. There is no infinite-farm equilibrium; the env is non-stationary w.r.t. the agent's own skill. The policy must learn to ride difficulty up while protecting streak.

### 3.11 Scoring (per correct delivery) **[confirmed]**
```
timeBonus   = floor(order.timeLeft * 2)
baseScore   = 100 * difficulty(time)
streakMult  = 1 + min(1.0, streak*0.05)     # caps at 2.0 at streak 20
vipMult     = order.vip ? 1.5 : 1
total       = floor((baseScore + timeBonus) * streakMult * vipMult)
# then: streak += 1; delivered += 1
```
Streak resets to 0 on wrong delivery or expiry. **Protecting streak is the highest-leverage behavior** — at automation difficulty (≈3.2) with max streak (2.0×) and VIP (1.5×), a single fast delivery is worth an order of magnitude more than an early-game one. Fast delivery also matters (the `2*timeLeft` term).

### 3.12 Rush & VIP
Rush toggles active/cooldown; initial cooldown 20s; high-pressure phases (automation/endurance) get recurring rush; active rush tightens spawn interval ×0.70 and fires a spawn burst (emits `rushBurst`) **[confirmed]**. Exact rush durations/cooldowns per phase + burst size **[extract]**. VIP orders score 1.5×; how `order.vip` is assigned (probability/trigger) **[extract]** — prioritize VIP when feasible.

### 3.13 Failure / episode end
Run ends at **3 expired orders** (`failedOrders >= maxFailedOrders`, max = 3) **[confirmed]**.

### 3.14 Must extract from `game.js` (do not guess)
cook time, chop time, walk speed, pathing algorithm, grid/wall layout + station coords (or use `getState()` coords), exact rush durations/cooldowns/burst size, VIP assignment rule, sink/dish-rack semantics, exact stall magnitude, customer-eat duration (≈10s), and confirm per-recipe `difficulty` usage.

## 4. Simulator (Phase 1)

`sim/env.py` — a Gymnasium-style env porting §3 from `reference/game.js`. Headless, no rendering, deterministic given a seed. Put every constant/formula in `sim/constants.py`. **Faithfulness gate:** `tests/test_fidelity.py` replays a fixed action trace and asserts the sim's resulting state (holdings, plating contents, score, streak, spawns) matches the real game on the same trace. If a behavior is ambiguous in the source, match the source's literal behavior, not what "should" happen.

## 5. RL formulation

### 5.1 Decision model — event-driven semi-MDP
Do **not** act every frame (that triggers the stall penalty and floods the policy with no-ops). The env advances the sim and only queries the policy when a chef needs a new assignment: at each `step`, exactly one idle chef (the "decision chef") is assigned a **macro-action**; the env then auto-routes that chef and resolves the macro (advancing sim time, accruing reward) until the next chef needs a decision. The decision chef's index is part of the observation.

### 5.2 Observation (canonical, in `sim/encode.py`)
A single `encode(state, decision_chef) -> vector` used by **both** the gym and the browser deploy. Fixed-length, normalized. Contents:
- Global: time (norm), phase one-hot (4), difficulty (norm), streak (clamped/norm), failedOrders (/3), rush.active, rush.timeLeft/cooldown (norm).
- Decision-chef index one-hot (5).
- Per chef ×5: position (x,y norm), holding code (type + ingredient one-hot(6) + state one-hot(3)), busy/hasPath/stall flags.
- Per stove ×3: free / cooking(+progress) ; per board ×2: free / busy(+progress).
- Per plating area ×4: multi-hot over the 7 valid component types {lettuce@chopped, tomato@chopped, onion@chopped, meat@cooked, dough@raw, dough@cooked, cheese@raw}.
- Active orders, cap K=6, padded: per order dish one-hot(7), timeLeft (norm), stand one-hot(5), vip flag.
- Append the action mask (§5.3).

Exact dims finalize in code; the only hard rule is gym and browser share one encoder.

### 5.3 Action space — 23 macros + masking
`FETCH_{tomato,lettuce,onion,meat,dough,cheese}` (6), `CHOP`, `COOK` (route held raw to a free board/stove), `DEPOSIT_{area0..3}` (4, drop held component), `TAKE_PLATE_{area0..3}` (4, empty-handed pickup of an area's items as a plate), `DELIVER_{stand0..4}` (5, route held plate to stand), `TRASH` (1), `WAIT` (1). Provide a **validity mask** each step (e.g., CHOP only when holding a choppable raw item; DELIVER only when holding a plate) and apply it to the policy logits — essential for sample-efficiency.

### 5.4 Reward
```
r_t = delta_score_t
      - P_expiry * (#orders expired this step)
      - P_wrong  * (#wrong deliveries this step)
      - c_time   * (decision step cost, optional small)
```
Start with `P_expiry ≈ P_wrong ≈` a typical mid-game delivery value (≈200–400, tunable) to make the agent streak-protective, `c_time` small or 0. Normalize rewards for PPO stability (scale or running-return normalization). Episode ends at `failedOrders>=3` or a sim-time cap (≈1200s). Begin sparse (delivery + expiry/wrong only); add sub-task shaping (+small for chopping/cooking a component an *active* order needs) **only if** learning stalls, and note it biases toward greedy sub-task completion.

## 6. Algorithm & training (Phase 3)
PPO, vectorized envs (16–64). Masked categorical policy over the 23 macros; MLP over the obs vector (start ~2×256). **Starting** hyperparameters (tune): lr 3e-4, γ 0.999 (long horizon), GAE λ 0.95, clip 0.2, entropy coef ~0.01 (decay), value coef 0.5, normalize observations. Optional curriculum: cap sim-time/phase early, then extend. Log per-episode score, deliveries, fails, streak, length; checkpoint best-by-eval. **Gate:** reward curve climbs and the policy beats the planner baseline in sim.

## 7. Greedy planner baseline (Phase 2)
`agents/planner.py` (sim) + `agents/planner.js` (real game). For each active order build its component task list; assign idle chefs to the most valuable feasible next task, where value weights order point-potential by urgency (≈1/timeLeft) and VIP (×1.5); respect station contention (don't over-commit to 3 stoves / 2 boards); **triage** — abandon orders that can't finish in time (estimate remaining tasks × per-task time vs. timeLeft) rather than burning chefs and breaking streak; never deliver a wrong plate. **Gate:** scores decently in sim *and* on the real site — great-in-sim / dead-on-real means the sim is lying, so this is also the Phase 1 fidelity check. It's also the submission safety net if PPO hasn't converged.

## 8. Deployment & sim-to-real (Phase 4)
Export the policy to ONNX (`deploy/policy.onnx`). `deploy/run_agent.js` loads it via onnxruntime-web and, on each decision, reads `getState()`, builds the observation with the **same logic as `sim/encode.py`** (port it to JS and parity-test against recorded sim vectors — train/deploy skew here silently tanks performance), applies the mask, picks a macro, resolves it to `command()` calls, and commits (no re-issue until that chef is idle again — mirror the semi-MDP). Validate on the live site and compare to the planner. For the actual scored run: console + ONNX in a foreground tab, signed into HT6, click submit when the timer hits zero.

## 9. Phase plan & gates

| Phase | Build | Gate |
|---|---|---|
| P1 | `sim/` gym + `constants.py` + `encode.py` | fixed action trace → sim state matches the real game |
| P2 | `planner.py` + `planner.js` | scores well in sim **and** on the real site (also validates P1) |
| P3 | `train/ppo.py` | reward climbs; beats planner in sim |
| P4 | ONNX export + `run_agent.js` | obs parity sim↔browser; validated on live site |

Run phases sequentially with `/clear` and a review between each. Do not start P3 before P1+P2 pass.

## 10. Source paths
- **Game source (read-only reference):** `C:\Users\moham\OneDrive\Documents\Intro to Greatness\HT6-chef\ht6-chefoverflow-main\ht6-chefoverflow-main\` — `game.js`, `api/`, `supabase/migrations/`.
- **Project (build here):** `C:\Users\moham\OneDrive\Documents\Intro to Greatness\chef-overflow-autopilot\`
- Copy `game.js` → `chef-overflow-autopilot\reference\game.js` and gitignore `reference/` (their copyright — local reference only, never committed).
