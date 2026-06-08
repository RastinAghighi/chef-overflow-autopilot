"""
Watch an agent play the faithful Chef Overflow sim (view-only diagnostic tool).

    py -m render.play --agent planner --seed 0
    py -m render.play --agent rl --model train/runs/ppo_ft/best_model.zip --seed 3
    py -m render.play --agent planner --headless        # sandbox smoke test

This renders the simulation so we can SEE why agents die at the 3-strike limit.
It is strictly read-only with respect to game mechanics: it reuses, unchanged,

    * :class:`sim.env.KitchenSim` / :class:`sim.env.ChefOverflowEnv` for the sim and
      its event-driven decision cadence,
    * :class:`agents.planner.Planner` (+ ``agents.benchmark.SimApi``) for the planner,
    * ``sim.encode.encode`` / ``action_mask`` + ``sb3_contrib.MaskablePPO`` for the RL
      policy,

and only ever *reads* their state to draw it.  The two agent drivers reproduce the
exact decision cadence the benchmark / env already use (planner: decide every 3
ticks ≈ 20 Hz; RL: query the policy only at the env's idle-chef decision points,
≥``decision_min_dwell`` apart) while the renderer advances and draws every single
tick so chef movement stays smooth between decisions.

The whole point of the tool is to surface what the agent is BLIND to.  The policy's
observation (real ``getState()``) hides both ``order.vip`` and eating-customer stand
occupancy; the renderer reads those straight from the sim's internal state and flags
them ("VIP" badge, "eat 6s" on an occupied stand) so the gap between what the agent
sees and what is actually happening is visible.

Controls: SPACE pause/resume · RIGHT or "." step one tick (while paused) ·
UP/DOWN playback speed · ESC / window-close quit.
"""

import argparse
import os
import sys

# Hide pygame's import banner (cosmetic); set before pygame is imported anywhere.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

# Make `from sim import ...` work whether launched as `py -m render.play` (root on
# path already) or `py render/play.py` (only render/ on path).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:  # never let a stray non-ASCII char crash a run on a cp1252 Windows console
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import numpy as np

from sim.env import KitchenSim, ChefOverflowEnv
from sim import constants as C
from sim import encode as E
from agents.planner import Planner
from agents.benchmark import SimApi


# ===========================================================================
# Display geometry / palette
# ===========================================================================
CELL = 42                     # pixels per tile (grid is 20x14)
STATUS_H = 46                 # top status bar height
EVENT_H = 104                 # bottom event-flash strip height
PANEL_W = 384                 # right info panel width
GRID_W = C.MAP_WIDTH * CELL   # 840
GRID_H = C.MAP_HEIGHT * CELL  # 588
WIN_W = GRID_W + PANEL_W
WIN_H = STATUS_H + GRID_H + EVENT_H

FADE_MS = 4200                # event chip lifetime
FLASH_MS = 360                # grid-border flash duration
GLOW_MS = 780                 # per-stand event glow duration
MAX_TICKS_PER_FRAME = 40      # cap sim ticks advanced per rendered frame (UI stays live)

# Colors -------------------------------------------------------------------
BG          = (16, 17, 22)
PANEL_BG    = (24, 26, 33)
EVENT_BG    = (20, 21, 27)
GRID_LINE   = (34, 36, 44)
FLOOR       = (44, 47, 56)
WALL        = (10, 11, 15)
COUNTER     = (96, 70, 46)
BIN         = (66, 86, 116)
BOARD_IDLE  = (118, 98, 66)
BOARD_BUSY  = (214, 182, 74)
STOVE_COLD  = (60, 76, 112)
STOVE_BURNT = (112, 32, 32)
PLATING     = (78, 84, 98)
RECEPTION   = (54, 64, 76)
TRASH       = (52, 42, 42)

WHITE = (236, 237, 242)
DIM   = (150, 152, 162)
DIMR  = (108, 110, 120)
GREEN = (86, 214, 122)
YELLOW = (232, 206, 80)
RED   = (236, 86, 86)
ORANGE = (244, 162, 60)
GOLD  = (255, 211, 74)     # VIP (hidden ground truth)
CYAN  = (96, 214, 224)     # eating customer (hidden ground truth)

# Per-chef colors track the in-game names (constants.CHEF_NAMES).
CHEF_COLORS = [
    (224, 78, 78),    # Red
    (78, 206, 96),    # Green
    (78, 146, 232),   # Blue
    (244, 162, 54),   # Orange
    (186, 116, 222),  # Purple
]

ING_COLOR = {
    "tomato": (222, 84, 84), "lettuce": (96, 202, 96), "onion": (176, 116, 206),
    "meat": (158, 96, 74), "dough": (212, 184, 126), "cheese": (240, 212, 92),
}
ING_ABBR = {"tomato": "TOM", "lettuce": "LET", "onion": "ONI",
            "meat": "MEA", "dough": "DOU", "cheese": "CHE"}
STATE_AB = {C.STATE_RAW: "r", C.STATE_CHOPPED: "c", C.STATE_COOKED: "k", C.STATE_BURNT: "x"}


# ===========================================================================
# Small read-only helpers (operate on the sim's internal dicts)
# ===========================================================================
def _is_plate(h):
    return bool(h) and isinstance(h, dict) and h.get("type") == "plate"


def _manhattan(ax, ay, bx, by):
    return abs(ax - bx) + abs(ay - by)


def station_short(station) -> str:
    """Compact tag for a station dict, e.g. 'rec2', 'stv0', 'bin:MEA'."""
    if station is None:
        return ""
    typ = station.get("type", "")
    sid = station.get("id", "")
    idx = sid.rsplit("_", 1)[-1] if "_" in sid else ""
    if typ == "ingredientBin":
        return "bin:" + ING_ABBR.get(station.get("ingredient"), "?")
    short = {"stove": "stv", "cuttingBoard": "cut", "platingArea": "plt",
             "receptionStand": "rec", "trash": "trash", "counter": "cnt"}.get(typ, typ[:3])
    return f"{short}{idx}"


def hold_text(h) -> str:
    """Full holding description for the side panel."""
    if h is None:
        return "(empty)"
    if _is_plate(h):
        items = h.get("items") or []
        if not items:
            return "plate[ ]"
        parts = [f"{ING_ABBR.get(it.get('ingredient'), '?')}.{STATE_AB.get(it.get('state'), '?')}"
                 for it in items]
        return "plate[" + ",".join(parts) + "]"
    return f"{h.get('ingredient')} {h.get('state')}"


def hold_compact(h) -> str:
    """Tiny on-grid holding glyph text."""
    if h is None:
        return ""
    if _is_plate(h):
        return f"PL{len(h.get('items') or [])}"
    return f"{ING_ABBR.get(h.get('ingredient'), '?')[:2]}{STATE_AB.get(h.get('state'), '?')}"


def chef_status(chef) -> str:
    """One-line activity string derived from the chef's internal control state."""
    if chef["commitmentStall"] > 0:
        return f"STALL {chef['commitmentStall']:.1f}s"
    if chef["busy"]:
        if chef.get("waitingAtStove"):
            return "cook@" + station_short(chef["waitingAtStove"])
        if chef.get("waitingAt"):
            return "chop@" + station_short(chef["waitingAt"])
        return "busy"
    if chef.get("targetStation"):
        return "-> " + station_short(chef["targetStation"]["station"])
    if chef["path"]:
        return "moving"
    return "idle"


# ===========================================================================
# Event detection (purely observational: diff sim counters across one tick)
# ===========================================================================
# Counter semantics in the sim (sim/env.py):
#   * expired_total  ++ in _update_orders   (order removed, stand cleared)  -> STRIKE
#   * no_slot_total  ++ in _fail_no_stand_slot (spawn dropped, no order)    -> STRIKE
#   * delivered_total++ in _interact reception on a correct plate (order removed)
#   * wrong_total    ++ in _interact reception on a wrong plate   (order stays)
# Wrong deliveries are NOT strikes (failed_orders is untouched); only expiries and
# no-slot drops are.  Those are exactly the events that drive the 3-strike death,
# so they are named precisely; wrong deliveries are named best-effort.
class Event:
    __slots__ = ("kind", "label", "stand_id", "t_ms", "sim_time")

    def __init__(self, kind, label, stand_id):
        self.kind = kind
        self.label = label
        self.stand_id = stand_id
        self.t_ms = 0
        self.sim_time = 0.0


def snapshot(sim):
    """Capture the pre-tick state needed to classify what happens this tick."""
    return {
        "delivered": sim.delivered_total,
        "expired": sim.expired_total,
        "wrong": sim.wrong_total,
        "no_slot": sim.no_slot_total,
        "orders": {o["id"]: {"dish": o["dish"], "timeLeft": o["timeLeft"],
                             "standId": o["standId"]} for o in sim.orders},
        "plate_chefs": [(c["id"], c["x"], c["y"]) for c in sim.chefs if _is_plate(c["holding"])],
    }


def detect_events(sim, pre):
    """Return the list of Events that occurred during the tick just executed."""
    d_deliv = sim.delivered_total - pre["delivered"]
    d_exp = sim.expired_total - pre["expired"]
    d_wrong = sim.wrong_total - pre["wrong"]
    d_noslot = sim.no_slot_total - pre["no_slot"]
    if not (d_deliv or d_exp or d_wrong or d_noslot):
        return []

    events = []
    # Removed orders are exactly (expiries + correct deliveries).  Partition by the
    # authoritative counts, assigning the lowest-timeLeft ones as expiries.
    post_ids = {o["id"] for o in sim.orders}
    removed = [pre["orders"][i] for i in pre["orders"] if i not in post_ids]
    removed.sort(key=lambda r: r["timeLeft"])
    for o in removed[:d_exp]:
        events.append(Event("expiry", f"EXPIRED  {o['dish']}", o["standId"]))
    for o in removed[d_exp:d_exp + d_deliv]:
        events.append(Event("delivery", f"DELIVERED  {o['dish']}", o["standId"]))
    for _ in range(d_noslot):
        events.append(Event("noslot", "ORDER LOST  (no free stand)", None))

    if d_wrong > 0:
        # A wrong delivery leaves its order on the stand; the chef that did it held a
        # plate before the tick and is now empty-handed adjacent to that stand.
        named = []
        for cid, px, py in pre["plate_chefs"]:
            if len(named) >= d_wrong:
                break
            c = sim.chefs[cid]
            if c["holding"] is None:
                for s in sim.reception_stands:
                    if s["order"] is not None and _manhattan(c["x"], c["y"], s["x"], s["y"]) == 1:
                        named.append((s["order"]["dish"], s["id"]))
                        break
        for i in range(d_wrong):
            if i < len(named):
                events.append(Event("wrong", f"WRONG  {named[i][0]}", named[i][1]))
            else:
                events.append(Event("wrong", "WRONG DELIVERY", None))
    return events


# ===========================================================================
# Agent drivers — each advances the sim one tick and queries its agent at the
# exact cadence its non-rendering counterpart (benchmark / env) uses.
# ===========================================================================
class PlannerDriver:
    """Mirrors ``agents.benchmark.run_episode``: call ``planner.decide`` every
    ``decide_every`` ticks (default 3 ≈ 20 Hz), tick every tick."""

    def __init__(self, seed, time_cap, decide_every=3, dt=1.0 / 60.0):
        self.sim = KitchenSim(seed)
        self.planner = Planner()
        self.api = SimApi(self.sim)
        self.seed = seed
        self.time_cap = time_cap
        self.dt = dt
        self.decide_every = decide_every
        self.tick_index = 0
        self.decision_chef = None          # planner commands many chefs; no single one
        self.last_action = None
        hz = round(round(1.0 / dt) / decide_every)
        self.agent_label = f"PLANNER  (decide@{hz}Hz)"

    def finished(self):
        return self.sim.game_over or self.sim.time >= self.time_cap

    def step_tick(self):
        if self.finished():
            return []
        if self.tick_index % self.decide_every == 0:
            self.planner.decide(self.sim.get_state(), self.api)
        pre = snapshot(self.sim)
        self.sim.tick(self.dt)
        self.tick_index += 1
        return detect_events(self.sim, pre)


class RLDriver:
    """Reproduces :meth:`ChefOverflowEnv.step`'s event-driven cadence, but unrolled
    one tick at a time so the renderer can draw between decisions.

    The policy is queried ONLY at the env's decision points (a chef idle, and ≥
    ``decision_min_dwell`` of sim time since the last decision), the chosen macro is
    applied to that decision chef via the env's own ``_apply_macro`` (which routes
    through ``sim.command`` exactly as training does), then the sim is advanced tick
    by tick until the next decision point — identical to ``_advance_to_decision``.
    """

    def __init__(self, seed, time_cap, model_path, deterministic=True, dt=1.0 / 60.0):
        from sb3_contrib import MaskablePPO   # lazy: keep planner mode free of torch/sb3
        self.env = ChefOverflowEnv(seed=seed, time_cap=time_cap, dt=dt,
                                   randomize_on_reset=False)
        self.model = MaskablePPO.load(model_path, device="cpu")
        self.deterministic = deterministic
        self.seed = seed
        self.time_cap = time_cap
        self.dt = dt
        self.env.reset(seed=seed)           # advances to the first decision point
        self.sim = self.env.sim
        self.decision_pending = True        # env.decision_chef is set and awaiting a macro
        self.target_time = self.env.sim.time
        self.decision_chef = self.env.decision_chef
        self.last_action = None
        sel = "argmax" if deterministic else "sampled"
        self.agent_label = f"RL {os.path.basename(model_path)}  ({sel})"

    def finished(self):
        return self.env.sim.game_over or self.env.sim.time >= self.time_cap

    def step_tick(self):
        env = self.env
        if self.finished():
            return []

        if self.decision_pending:
            # Query the policy for the current decision chef and apply the macro.
            obs = env._obs()[None, :]
            mask = env.action_masks()[None, :]
            action, _ = self.model.predict(obs, action_masks=mask,
                                           deterministic=self.deterministic)
            a = int(np.asarray(action).reshape(-1)[0])
            env._apply_macro(env.decision_chef, a)
            self.last_action = f"C{env.decision_chef}:{E.ACTION_NAMES[a]}"
            self.decision_chef = env.decision_chef
            self.target_time = env.sim.time + env.decision_min_dwell
            self.decision_pending = False

        pre = snapshot(env.sim)
        env.sim.tick(self.dt)
        events = detect_events(env.sim, pre)

        # Reached the next decision point? (mirror _advance_to_decision's stop test)
        if env.sim.time >= self.target_time:
            idle = env._frame_decidable()
            if idle:
                env.decision_chef = env._pick_decision_chef(idle)
                self.decision_chef = env.decision_chef
                self.decision_pending = True
        return events


def build_driver(args):
    if args.agent == "planner":
        return PlannerDriver(args.seed, args.cap)
    model_path = args.model
    if not os.path.exists(model_path):
        raise SystemExit(f"[render] RL model not found: {model_path}\n"
                         f"          pass --model <path-to.zip> (default "
                         f"train/runs/ppo_ft/best_model.zip)")
    return RLDriver(args.seed, args.cap, model_path, deterministic=args.deterministic)


# ===========================================================================
# Headless smoke test (no window) — for the sandbox / CI
# ===========================================================================
def run_headless(driver, max_ticks=300):
    n = 0
    while n < max_ticks and not driver.finished():
        driver.step_tick()
        n += 1
    sim = driver.sim
    fails = sim.expired_total + sim.wrong_total + sim.no_slot_total
    print(f"[headless OK] {driver.agent_label}  seed={driver.seed}  "
          f"ticks={n}  t={sim.time:.2f}s  score={sim.score:.0f}  "
          f"deliv={sim.delivered_total}  strikes={sim.failed_orders}/{sim.max_failed_orders}  "
          f"(exp={sim.expired_total} wrong={sim.wrong_total} noslot={sim.no_slot_total} "
          f"fails={fails})  {'GAME-OVER' if sim.game_over else 'running'}")
    return 0


# ===========================================================================
# Renderer (pygame imported lazily so headless never touches the video layer)
# ===========================================================================
class Renderer:
    SPEEDS_MIN, SPEEDS_MAX = 0.1, 16.0

    def __init__(self, driver, speed=1.0):
        import pygame
        self.pg = pygame
        self.driver = driver
        self.speed = float(speed)
        self.paused = False
        self.tick_accum = 0.0
        self.events = []          # list[Event], faded by wall-clock age

        pygame.init()
        pygame.display.set_caption(
            f"Chef Overflow — {driver.agent_label} — seed {driver.seed}")
        self.screen = pygame.display.set_mode((WIN_W, WIN_H))
        self.clock = pygame.time.Clock()
        self.gx0, self.gy0 = 0, STATUS_H

        def mono(sz, bold=False):
            return pygame.font.SysFont("consolas,couriernew,monospace", sz, bold=bold)
        self.f_tiny = mono(11)
        self.f_small = mono(13)
        self.f_med = mono(15)
        self.f_big = mono(18, bold=True)
        self.f_huge = mono(40, bold=True)

    # -- geometry ----------------------------------------------------------
    def cell_rect(self, x, y):
        return self.pg.Rect(self.gx0 + x * CELL, self.gy0 + y * CELL, CELL, CELL)

    def cell_center(self, x, y):
        return (self.gx0 + x * CELL + CELL // 2, self.gy0 + y * CELL + CELL // 2)

    def text(self, s, x, y, font, color, anchor="topleft"):
        img = font.render(s, True, color)
        rect = img.get_rect()
        setattr(rect, anchor, (x, y))
        self.screen.blit(img, rect)
        return rect

    # -- input -------------------------------------------------------------
    def handle_input(self):
        pg = self.pg
        for ev in pg.event.get():
            if ev.type == pg.QUIT:
                return False
            if ev.type == pg.KEYDOWN:
                if ev.key in (pg.K_ESCAPE, pg.K_q):
                    return False
                elif ev.key == pg.K_SPACE:
                    self.paused = not self.paused
                elif ev.key in (pg.K_RIGHT, pg.K_PERIOD):
                    if self.paused:
                        self._advance_one_tick()
                elif ev.key == pg.K_UP:
                    self.speed = min(self.SPEEDS_MAX, self.speed * 1.5)
                elif ev.key == pg.K_DOWN:
                    self.speed = max(self.SPEEDS_MIN, self.speed / 1.5)
        return True

    def _advance_one_tick(self):
        evs = self.driver.step_tick()
        if evs:
            now = self.pg.time.get_ticks()
            for e in evs:
                e.t_ms = now
                e.sim_time = self.driver.sim.time
            self.events.extend(evs)

    # -- main loop ---------------------------------------------------------
    def run(self):
        running = True
        while running:
            running = self.handle_input()
            if not self.paused and not self.driver.finished():
                self.tick_accum += self.speed
                steps = 0
                while self.tick_accum >= 1.0 and steps < MAX_TICKS_PER_FRAME:
                    self._advance_one_tick()
                    self.tick_accum -= 1.0
                    steps += 1
                    if self.driver.finished():
                        break
            # expire old event chips
            now = self.pg.time.get_ticks()
            self.events = [e for e in self.events if now - e.t_ms < FADE_MS]
            self.draw()
            self.clock.tick(60)
        self.pg.quit()

    # -- drawing -----------------------------------------------------------
    def draw(self):
        self.screen.fill(BG)
        self.draw_grid()
        self.draw_stations()
        self.draw_paths()
        self.draw_chefs()
        self.draw_event_glows()
        self.draw_border_flash()
        self.draw_status_bar()
        self.draw_panel()
        self.draw_event_strip()
        if self.driver.finished():
            self.draw_finished_overlay()
        self.pg.display.flip()

    def draw_grid(self):
        pg = self.pg
        sim = self.driver.sim
        base = {
            C.TILE_FLOOR: FLOOR, C.TILE_WALL: WALL, C.TILE_COUNTER: COUNTER,
            C.TILE_INGREDIENT_BIN: BIN, C.TILE_STOVE: STOVE_COLD,
            C.TILE_CUTTING_BOARD: BOARD_IDLE, C.TILE_PLATING_AREA: PLATING,
            C.TILE_RECEPTION_STAND: RECEPTION, C.TILE_TRASH: TRASH,
        }
        for y in range(C.MAP_HEIGHT):
            for x in range(C.MAP_WIDTH):
                r = self.cell_rect(x, y)
                pg.draw.rect(self.screen, base.get(sim.grid[y][x], FLOOR), r)
                pg.draw.rect(self.screen, GRID_LINE, r, 1)

    def _progress_bar(self, rect, frac, color, bg=(30, 30, 36)):
        pg = self.pg
        bar = pg.Rect(rect.x + 3, rect.bottom - 7, rect.w - 6, 5)
        pg.draw.rect(self.screen, bg, bar)
        fw = int((rect.w - 6) * max(0.0, min(1.0, frac)))
        if fw > 0:
            pg.draw.rect(self.screen, color, pg.Rect(bar.x, bar.y, fw, bar.h))

    def _comp_chips(self, comps, x, y, size=9, gap=2, max_per_row=6):
        """Draw a row of tiny ingredient chips (ingredient color + state letter)."""
        pg = self.pg
        cx, cy, n = x, y, 0
        for c in comps:
            ing, st = c.get("ingredient"), c.get("state")
            r = pg.Rect(cx, cy, size, size)
            pg.draw.rect(self.screen, ING_COLOR.get(ing, DIM), r)
            pg.draw.rect(self.screen, (20, 20, 24), r, 1)
            self.text(STATE_AB.get(st, "?"), r.centerx, r.centery, self.f_tiny,
                      (15, 15, 18), anchor="center")
            n += 1
            cx += size + gap
            if n % max_per_row == 0:
                cx = x
                cy += size + gap

    def draw_stations(self):
        pg = self.pg
        sim = self.driver.sim

        for b in sim.ingredient_bins:
            r = self.cell_rect(b["x"], b["y"])
            self.text(ING_ABBR.get(b["ingredient"], "?"), r.centerx, r.centery,
                      self.f_small, WHITE, anchor="center")

        for s in sim.stoves:
            r = self.cell_rect(s["x"], s["y"])
            if s["cooking"] is not None:
                frac = s["cookTime"] / s["maxCookTime"] if s["maxCookTime"] else 0.0
                burnt = s["cookTime"] >= s["maxCookTime"] * C.STOVE_BURNT_FRAC
                col = STOVE_BURNT if burnt else (244, max(40, int(150 - 90 * min(frac, 1.0))), 40)
                pg.draw.rect(self.screen, col, r.inflate(-4, -4))
                ing = s["cooking"].get("ingredient")
                self.text(ING_ABBR.get(ing, "?"), r.centerx, r.top + 11, self.f_tiny,
                          (20, 20, 20), anchor="center")
                ready = s["cookTime"] >= s["maxCookTime"] * C.STOVE_READY_FRAC and not burnt
                tag = "BURNT" if burnt else ("READY" if ready else "cook")
                self.text(tag, r.centerx, r.centery + 2, self.f_tiny,
                          (20, 20, 20) if not burnt else WHITE, anchor="center")
                self._progress_bar(r, frac, WHITE if not burnt else RED)
            else:
                self.text("stove", r.centerx, r.centery, self.f_tiny, (200, 210, 230),
                          anchor="center")

        for b in sim.cutting_boards:
            r = self.cell_rect(b["x"], b["y"])
            if b["busy"] and b["processing"] is not None:
                pg.draw.rect(self.screen, BOARD_BUSY, r.inflate(-4, -4))
                ing = b["processing"].get("ingredient")
                self.text(ING_ABBR.get(ing, "?"), r.centerx, r.top + 11, self.f_tiny,
                          (20, 20, 20), anchor="center")
                self.text("chop", r.centerx, r.centery + 2, self.f_tiny, (20, 20, 20),
                          anchor="center")
                frac = b["processTime"] / b["maxProcessTime"] if b["maxProcessTime"] else 0.0
                self._progress_bar(r, frac, (40, 40, 40))
            else:
                self.text("board", r.centerx, r.centery, self.f_tiny, (220, 200, 150),
                          anchor="center")

        for i, p in enumerate(sim.plating_areas):
            r = self.cell_rect(p["x"], p["y"])
            self.text(f"plt{i}", r.centerx, r.top + 9, self.f_tiny, (210, 214, 224),
                      anchor="center")
            if p["items"]:
                self._comp_chips(p["items"], r.x + 4, r.y + 18, size=9, max_per_row=3)
            else:
                self.text("·", r.centerx, r.centery + 6, self.f_small, DIMR, anchor="center")

        for t in sim.trash_cans:
            r = self.cell_rect(t["x"], t["y"])
            self.text("TRASH", r.centerx, r.centery, self.f_tiny, (200, 150, 150),
                      anchor="center")

        self.draw_reception_stands()

    def draw_reception_stands(self):
        pg = self.pg
        sim = self.driver.sim
        blink = (self.pg.time.get_ticks() // 250) % 2 == 0
        for i, stand in enumerate(sim.reception_stands):
            r = self.cell_rect(stand["x"], stand["y"])
            order = stand["order"]
            self.text(f"rec{i}", r.centerx, r.top + 9, self.f_tiny, (210, 214, 224),
                      anchor="center")
            if order is not None:
                ratio = order["timeLeft"] / max(order["maxTime"], 1e-6)
                col = GREEN if ratio > 0.5 else (YELLOW if ratio > 0.25 else RED)
                # urgency border (blink when imminent)
                if ratio < 0.13 and blink:
                    pg.draw.rect(self.screen, RED, r, 4)
                else:
                    pg.draw.rect(self.screen, col, r, 3)
                self.text(C.RECIPES[order["dish"]]["icon"], r.centerx, r.centery - 4,
                          self.f_small, WHITE, anchor="center")
                self.text(f"{order['timeLeft']:.0f}s", r.centerx, r.centery + 11,
                          self.f_tiny, col, anchor="center")
                self._comp_chips(order["components"], r.x + 3, r.bottom - 12, size=8,
                                 max_per_row=4)
                if order.get("vip"):   # hidden ground truth: gold VIP badge
                    badge = pg.Rect(r.right - 22, r.top + 2, 20, 11)
                    pg.draw.rect(self.screen, GOLD, badge)
                    self.text("VIP", badge.centerx, badge.centery, self.f_tiny,
                              (40, 30, 0), anchor="center")
            # hidden ground truth: eating-customer occupancy
            if stand["customer"] is not None:
                tag = pg.Rect(r.x + 2, r.bottom - 13, r.w - 4, 12)
                pg.draw.rect(self.screen, (12, 40, 44), tag)
                self.text(f"eat {stand['customer']['timeLeft']:.0f}s", tag.centerx,
                          tag.centery, self.f_tiny, CYAN, anchor="center")

    def draw_paths(self):
        pg = self.pg
        for c in self.driver.sim.chefs:
            if not c["path"]:
                continue
            col = CHEF_COLORS[c["id"] % len(CHEF_COLORS)]
            dim = tuple(int(v * 0.55) for v in col)
            pts = [self.cell_center(c["x"], c["y"])] + \
                  [self.cell_center(px, py) for px, py in c["path"]]
            if len(pts) >= 2:
                pg.draw.lines(self.screen, dim, False, pts, 2)
            for px, py in c["path"]:
                cx, cy = self.cell_center(px, py)
                pg.draw.circle(self.screen, dim, (cx, cy), 2)

    def draw_chefs(self):
        pg = self.pg
        sim = self.driver.sim
        dchef = self.driver.decision_chef
        for c in sim.chefs:
            col = CHEF_COLORS[c["id"] % len(CHEF_COLORS)]
            cx, cy = self.cell_center(c["x"], c["y"])
            rad = CELL // 2 - 6
            # decision-chef highlight (RL: who is being commanded right now)
            if dchef is not None and c["id"] == dchef:
                pg.draw.circle(self.screen, WHITE, (cx, cy), rad + 5, 2)
            # status rings
            if c["boostActive"]:
                pg.draw.circle(self.screen, CYAN, (cx, cy), rad + 3, 3)
            elif c["commitmentStall"] > 0:
                pg.draw.circle(self.screen, ORANGE, (cx, cy), rad + 3, 3)
            pg.draw.circle(self.screen, col, (cx, cy), rad)
            pg.draw.circle(self.screen, (12, 12, 14), (cx, cy), rad, 2)
            self.text(str(c["id"]), cx, cy, self.f_med, (12, 12, 14), anchor="center")
            # holding glyph (top-right) + compact label below the chef
            if c["holding"] is not None:
                h = c["holding"]
                gcol = (235, 235, 235) if _is_plate(h) else ING_COLOR.get(h.get("ingredient"), DIM)
                gx, gy = cx + rad - 2, cy - rad - 1
                pg.draw.rect(self.screen, gcol, pg.Rect(gx, gy, 11, 11))
                pg.draw.rect(self.screen, (15, 15, 18), pg.Rect(gx, gy, 11, 11), 1)
            label = hold_compact(c["holding"])
            tgt = station_short(c["targetStation"]["station"]) if c.get("targetStation") else ""
            line = (label + " " + tgt).strip()
            if line:
                self.text(line, cx, cy + rad + 7, self.f_tiny, WHITE, anchor="center")

    # -- event visuals -----------------------------------------------------
    def _kind_color(self, kind):
        return {"delivery": GREEN, "expiry": RED, "wrong": ORANGE, "noslot": RED}.get(kind, WHITE)

    def draw_event_glows(self):
        pg = self.pg
        now = self.pg.time.get_ticks()
        stands = {s["id"]: s for s in self.driver.sim.reception_stands}
        for e in self.events:
            if e.stand_id is None or e.stand_id not in stands:
                continue
            age = now - e.t_ms
            if age >= GLOW_MS:
                continue
            s = stands[e.stand_id]
            cx, cy = self.cell_center(s["x"], s["y"])
            frac = age / GLOW_MS
            rad = int(CELL * (0.5 + 0.9 * frac))
            col = self._kind_color(e.kind)
            ring = pg.Surface((rad * 2 + 4, rad * 2 + 4), pg.SRCALPHA)
            a = int(220 * (1 - frac))
            pg.draw.circle(ring, (*col, a), (rad + 2, rad + 2), rad, 3)
            self.screen.blit(ring, (cx - rad - 2, cy - rad - 2))

    def draw_border_flash(self):
        now = self.pg.time.get_ticks()
        # most-severe recent event drives the border tint
        sev = {"expiry": 3, "noslot": 3, "wrong": 2, "delivery": 1}
        best = None
        for e in self.events:
            if now - e.t_ms < FLASH_MS:
                if best is None or sev.get(e.kind, 0) > sev.get(best.kind, 0):
                    best = e
        if best is None:
            return
        age = now - best.t_ms
        a = int(170 * (1 - age / FLASH_MS))
        col = self._kind_color(best.kind)
        pg = self.pg
        overlay = pg.Surface((GRID_W, GRID_H), pg.SRCALPHA)
        pg.draw.rect(overlay, (*col, a), pg.Rect(0, 0, GRID_W, GRID_H), 8)
        self.screen.blit(overlay, (self.gx0, self.gy0))

    # -- status bar --------------------------------------------------------
    def draw_status_bar(self):
        pg = self.pg
        sim = self.driver.sim
        pg.draw.rect(self.screen, PANEL_BG, pg.Rect(0, 0, WIN_W, STATUS_H))
        pg.draw.line(self.screen, GRID_LINE, (0, STATUS_H), (WIN_W, STATUS_H), 1)
        strikes = sim.failed_orders
        fcol = GREEN if strikes == 0 else (YELLOW if strikes == 1 else
                                           (ORANGE if strikes == 2 else RED))
        phase = C.phase_key(sim.time)
        segs = [
            (f"t {sim.time:6.1f}s", WHITE),
            (f"score {sim.score:8.0f}", WHITE),
            (f"streak {sim.streak:2d} (best {sim.best_streak})", YELLOW if sim.streak else DIM),
            (f"strikes {strikes}/{sim.max_failed_orders}", fcol),
            (f"diff {sim.difficulty:.2f}x", WHITE),
            (f"deliv {sim.delivered_total}", GREEN),
            (f"phase {phase}", CYAN),
        ]
        x = 10
        for s, col in segs:
            r = self.text(s, x, STATUS_H // 2, self.f_med, col, anchor="midleft")
            x = r.right + 16
        if sim.rush["active"]:
            self.text(f"RUSH {sim.rush['timeLeft']:.0f}s", x, STATUS_H // 2, self.f_med,
                      ORANGE, anchor="midleft")
        # right-aligned playback state
        right = WIN_W - 10
        if self.paused:
            r = self.text("PAUSED", right, STATUS_H // 2, self.f_big, RED, anchor="midright")
            right = r.left - 14
        self.text(f"x{self.speed:.2f}", right, STATUS_H // 2, self.f_med, WHITE,
                  anchor="midright")

    # -- right info panel --------------------------------------------------
    def draw_panel(self):
        pg = self.pg
        sim = self.driver.sim
        px0 = GRID_W
        pg.draw.rect(self.screen, PANEL_BG, pg.Rect(px0, STATUS_H, PANEL_W, GRID_H + EVENT_H))
        pg.draw.line(self.screen, GRID_LINE, (px0, STATUS_H), (px0, WIN_H), 1)
        x = px0 + 12
        y = STATUS_H + 8

        self.text(self.driver.agent_label, x, y, self.f_med, WHITE); y += 18
        sub = f"seed {self.driver.seed}   cap {self.driver.time_cap:.0f}s"
        if self.driver.last_action:
            sub += f"   last {self.driver.last_action}"
        self.text(sub, x, y, self.f_small, DIM); y += 20

        # Chefs
        self.text("CHEFS", x, y, self.f_small, CYAN); y += 16
        for c in sim.chefs:
            col = CHEF_COLORS[c["id"] % len(CHEF_COLORS)]
            pg.draw.rect(self.screen, col, pg.Rect(x, y + 2, 10, 10))
            self.text(f"C{c['id']} {c['name']:<6} {chef_status(c)}", x + 16, y,
                      self.f_small, WHITE)
            y += 15
            flags = []
            if c["boostActive"]:
                flags.append(f"BOOST {c['boostTime']:.1f}s")
            elif c["boostCooldown"] > 0:
                flags.append(f"bcd {c['boostCooldown']:.0f}s")
            if c["path"]:
                flags.append(f"path:{len(c['path'])}")
            extra = ("  " + " ".join(flags)) if flags else ""
            self.text(f"   hold: {hold_text(c['holding'])}{extra}", x + 16, y,
                      self.f_tiny, DIM)
            y += 15

        y += 6
        # Orders (with hidden ground truth)
        self.text("ORDERS  (VIP / eating = agent-hidden)", x, y, self.f_small, CYAN)
        y += 16
        any_order = False
        for i, stand in enumerate(sim.reception_stands):
            order = stand["order"]
            cust = stand["customer"]
            if order is None and cust is None:
                continue
            any_order = True
            if order is not None:
                ratio = order["timeLeft"] / max(order["maxTime"], 1e-6)
                col = GREEN if ratio > 0.5 else (YELLOW if ratio > 0.25 else RED)
                vip = "  *VIP*" if order.get("vip") else ""
                self.text(f"rec{i}: {order['dish']:<14} {order['timeLeft']:5.1f}s",
                          x, y, self.f_small, col)
                if vip:
                    self.text(vip, x + 270, y, self.f_small, GOLD)
                y += 14
                comps = " ".join(f"{ING_ABBR.get(c['ingredient'], '?')}.{STATE_AB.get(c['state'], '?')}"
                                 for c in order["components"])
                self.text("   need: " + comps, x, y, self.f_tiny, DIM)
                y += 14
            if cust is not None:
                self.text(f"   [eating {cust['timeLeft']:.1f}s — stand blocked]",
                          x, y, self.f_tiny, CYAN)
                y += 14
        if not any_order:
            self.text("  (none active)", x, y, self.f_small, DIMR); y += 14

        y += 6
        # Upcoming
        self.text("UPCOMING", x, y, self.f_small, CYAN); y += 16
        for u in sim.upcoming_orders[:C.UPCOMING_QUEUE_SIZE]:
            vip = "  *VIP*" if u.get("vip") else ""
            self.text(f"  {u['dish']:<14} ~{u['etaSeconds']:4.1f}s{vip}", x, y,
                      self.f_tiny, GOLD if u.get("vip") else DIM)
            y += 13

        # Controls (anchored near the bottom of the panel)
        cy = STATUS_H + GRID_H + 8
        self.text("CONTROLS", x, cy, self.f_small, CYAN); cy += 15
        for line in ("SPACE pause   RIGHT/.  step (paused)",
                     "UP/DOWN speed   ESC/Q quit"):
            self.text(line, x, cy, self.f_tiny, DIM); cy += 13

    # -- bottom event strip ------------------------------------------------
    def draw_event_strip(self):
        pg = self.pg
        y0 = STATUS_H + GRID_H
        pg.draw.rect(self.screen, EVENT_BG, pg.Rect(0, y0, GRID_W, EVENT_H))
        pg.draw.line(self.screen, GRID_LINE, (0, y0), (GRID_W, y0), 1)
        self.text("EVENTS  (red=strike, orange=wrong, green=delivery)", 10, y0 + 6,
                  self.f_small, DIM)
        now = self.pg.time.get_ticks()
        chip_w, chip_h, gap = 196, 22, 6
        per_row = max(1, (GRID_W - 16) // (chip_w + gap))
        x0, ys = 8, y0 + 26
        # newest first
        for idx, e in enumerate(reversed(self.events[-(per_row * 3):])):
            row, coln = divmod(idx, per_row)
            cx = x0 + coln * (chip_w + gap)
            cyp = ys + row * (chip_h + gap)
            if cyp + chip_h > y0 + EVENT_H:
                break
            age = now - e.t_ms
            alpha = max(0, int(255 * (1 - age / FADE_MS)))
            col = self._kind_color(e.kind)
            chip = pg.Surface((chip_w, chip_h))
            chip.fill(EVENT_BG)
            pg.draw.rect(chip, tuple(int(v * 0.34) for v in col),
                         pg.Rect(0, 0, chip_w, chip_h), border_radius=5)
            pg.draw.rect(chip, col, pg.Rect(0, 0, chip_w, chip_h), 1, border_radius=5)
            label = f"{e.sim_time:5.1f}s  {e.label}"
            img = self.f_small.render(label, True, col)
            chip.blit(img, img.get_rect(midleft=(8, chip_h // 2)))
            chip.set_alpha(alpha)
            self.screen.blit(chip, (cx, cyp))

    def draw_finished_overlay(self):
        pg = self.pg
        sim = self.driver.sim
        overlay = pg.Surface((GRID_W, GRID_H), pg.SRCALPHA)
        overlay.fill((0, 0, 0, 150))
        self.screen.blit(overlay, (self.gx0, self.gy0))
        if sim.game_over:
            msg, col = "GAME OVER — 3 STRIKES", RED
        else:
            msg, col = "REACHED TIME CAP", CYAN
        cx, cy = self.gx0 + GRID_W // 2, self.gy0 + GRID_H // 2
        self.text(msg, cx, cy - 24, self.f_huge, col, anchor="center")
        self.text(f"score {sim.score:.0f}   deliveries {sim.delivered_total}   "
                  f"t {sim.time:.1f}s", cx, cy + 18, self.f_big, WHITE, anchor="center")
        self.text("ESC / Q to quit", cx, cy + 48, self.f_small, DIM, anchor="center")


# ===========================================================================
# CLI
# ===========================================================================
def main():
    ap = argparse.ArgumentParser(
        description="Watch the greedy planner or a trained RL policy play the sim.")
    ap.add_argument("--agent", choices=["planner", "rl"], default="planner")
    ap.add_argument("--model", default="train/runs/ppo_ft/best_model.zip",
                    help="path to the MaskablePPO .zip (for --agent rl)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cap", type=float, default=C.DEFAULT_TIME_CAP, help="sim-time cap (s)")
    ap.add_argument("--speed", type=float, default=1.0, help="initial playback multiplier")
    ap.add_argument("--sample", dest="deterministic", action="store_false",
                    help="RL: sample actions instead of argmax")
    ap.add_argument("--deterministic", dest="deterministic", action="store_true",
                    help="RL: greedy argmax over masked logits (default)")
    ap.set_defaults(deterministic=True)
    ap.add_argument("--headless", action="store_true",
                    help="run a few hundred ticks with no window and exit (smoke test)")
    ap.add_argument("--headless-ticks", type=int, default=300)
    args = ap.parse_args()

    driver = build_driver(args)

    if args.headless:
        return run_headless(driver, max_ticks=args.headless_ticks)

    Renderer(driver, speed=args.speed).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
