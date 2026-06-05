"""
Chef Overflow simulator (Phase 1).

``KitchenSim`` is a faithful, headless, deterministic port of ``reference/game.js``
(§3 of the design spec): same map, pathfinding, station interactions, order
spawning/timers, difficulty curve, rubber-band, rush/VIP, scoring, and the
commitment stall.  All randomness flows through one seeded RNG.

``ChefOverflowEnv`` wraps the sim as the event-driven semi-MDP of §5.1: the sim is
advanced and the policy is queried only when a chef goes idle.  One decision chef
per ``step`` is assigned one of the 23 macro-actions (§5.3, from ``encode.py``),
the macro is auto-routed to completion, and reward (§5.4) is accrued in between.

The two classes are deliberately split: the sim is the ground-truth mechanics
(what the fidelity tests pin down); the env is the RL formulation on top of it.
"""

import math
import random

import numpy as np

try:
    import gymnasium as gym
    from gymnasium import spaces
    _HAS_GYM = True
except Exception:  # pragma: no cover - gym is a hard dep, but keep sim usable
    gym = None
    spaces = None
    _HAS_GYM = False

from . import constants as C
from . import encode as E


# ===========================================================================
# Faithful simulation engine
# ===========================================================================
class KitchenSim:
    """Headless, deterministic port of game.js's playable kitchen."""

    def __init__(self, seed: int = 0):
        self.grid, self.counter_coords = C.build_map()
        self._reset_state(seed)

    # -- lifecycle ---------------------------------------------------------
    def reset(self, seed: int = 0):
        self._reset_state(seed)
        return self.get_state()

    def _reset_state(self, seed: int):
        self.rng = random.Random(seed)
        self.seed = seed
        self.time = 0.0
        self.score = 0.0
        self.difficulty = 1.0
        self.streak = 0
        self.best_streak = 0
        self.failed_orders = 0
        self.max_failed_orders = C.MAX_FAILED_ORDERS
        self.orders_delivered = 0
        self.running = True
        self.paused = False
        self.game_over = False

        self.rush = {"active": False, "timeLeft": 0.0, "cooldown": C.RUSH_INITIAL_COOLDOWN}
        self.order_spawn_debt = 0.0
        self.upcoming_orders = []

        # metrics for reward shaping / introspection
        self.delivered_total = 0
        self.expired_total = 0
        self.wrong_total = 0
        self.no_slot_total = 0
        self.spawned_log = []   # [(dish, vip)] of every spawned order (introspection)

        self._build_stations()
        self.orders = []
        self.order_id_counter = 0
        self._init_chefs()

        self._refill_upcoming_queue()

        # Two scripted initial spawns (setTimeout 1000/3000ms in game.js:3257).
        self._initial_spawns_done = [False, False]

    def _build_stations(self):
        self.ingredient_bins = [
            {"id": f"bin_{i}", "x": x, "y": y, "ingredient": ing, "type": "ingredientBin"}
            for i, (x, y, ing) in enumerate(C.INGREDIENT_BINS)
        ]
        self.stoves = [
            {"id": f"stove_{i}", "x": x, "y": y, "type": "stove",
             "cooking": None, "cookTime": 0.0, "maxCookTime": C.COOK_TIME, "busy": False}
            for i, (x, y) in enumerate(C.STOVE_POSITIONS)
        ]
        self.cutting_boards = [
            {"id": f"cutting_{i}", "x": x, "y": y, "type": "cuttingBoard",
             "processing": None, "processTime": 0.0, "maxProcessTime": C.CHOP_TIME, "busy": False}
            for i, (x, y) in enumerate(C.CUTTING_POSITIONS)
        ]
        self.plating_areas = [
            {"id": f"plating_{i}", "x": x, "y": y, "type": "platingArea", "items": [], "busy": False}
            for i, (x, y) in enumerate(C.PLATING_POSITIONS)
        ]
        tx, ty = C.TRASH_POSITION
        self.trash_cans = [{"id": "trash_0", "x": tx, "y": ty, "type": "trash"}]
        self.reception_stands = [
            {"id": f"reception_{i}", "x": x, "y": y, "type": "receptionStand",
             "order": None, "customer": None}
            for i, (x, y) in enumerate(C.RECEPTION_POSITIONS)
        ]
        self.counters = [
            {"id": f"counter_{i}", "x": x, "y": y, "type": "counter", "items": []}
            for i, (x, y) in enumerate(self.counter_coords)
        ]
        # id -> (type, station) for command() resolution (search order matters).
        self._station_by_id = {}
        for arr in (self.ingredient_bins, self.stoves, self.cutting_boards,
                    self.plating_areas, self.reception_stands, self.trash_cans,
                    self.counters):
            for st in arr:
                self._station_by_id[st["id"]] = st

    def _init_chefs(self):
        self.chefs = []
        for i, (x, y) in enumerate(C.CHEF_START_POSITIONS):
            self.chefs.append({
                "id": i, "name": C.CHEF_NAMES[i], "x": x, "y": y,
                "path": [], "blockedTicks": 0, "holding": None, "busy": False,
                "actionTimer": 0.0, "moveTimer": 0.0,
                "waitingAt": None, "waitingAtStove": None, "targetStation": None,
                "boostActive": False, "boostTime": 0.0, "boostCooldown": 0.0,
                "commitmentStall": 0.0,
            })
        self._rebuild_reservations()

    # -- performance / spawning -------------------------------------------
    def _perf(self) -> float:
        return C.perf(self.orders_delivered, self.failed_orders, self.streak)

    def _rand_int(self, n: int) -> int:
        """floor(Math.random()*n) — one RNG draw, matching game.js."""
        return int(math.floor(self.rng.random() * n))

    def _roll_upcoming_order(self):
        names = C.recipe_pool(self.time)
        entries = names if names is not None else list(C.DISH_NAMES)
        name = entries[self._rand_int(len(entries))]
        core, rand_count = C.order_time_limit_core(self.time, self._perf())
        time_limit = core + self._rand_int(rand_count)
        vip = self.rng.random() < C.vip_probability(self.time)
        return {"dish": name, "timeLimit": time_limit, "vip": vip, "etaSeconds": 0.0}

    def _refill_upcoming_queue(self):
        while len(self.upcoming_orders) < C.UPCOMING_QUEUE_SIZE:
            self.upcoming_orders.append(self._roll_upcoming_order())

    def _recompute_upcoming_etas(self):
        spawn_every = C.base_spawn_interval(self.time, self.rush["active"], self._perf())
        debt = max(0.0, min(1.0, self.order_spawn_debt))
        for i, u in enumerate(self.upcoming_orders):
            u["etaSeconds"] = max(0.0, (i + 1 - debt) * spawn_every)

    def _stand_free(self, s) -> bool:
        return s["order"] is None and s["customer"] is None

    def _fail_no_stand_slot(self):
        self.failed_orders += 1
        self.no_slot_total += 1
        self.score += -C.NO_STAND_SLOT_PENALTY
        self.streak = 0

    def _make_components(self, dish_name):
        return [{"ingredient": ing, "state": st}
                for (ing, st) in C.RECIPES[dish_name]["components"]]

    def _spawn_order(self) -> bool:
        """Port of spawnOrder (game.js:977). Returns True if a spawn resolved."""
        phase = C.phase_key(self.time)
        available = [s for s in self.reception_stands if self._stand_free(s)]
        if not available:
            if C.is_high_pressure(phase):
                self._fail_no_stand_slot()
                return True
            return False
        stand = available[self._rand_int(len(available))]

        self._refill_upcoming_queue()
        spec = self.upcoming_orders.pop(0)
        self._refill_upcoming_queue()

        time_limit = spec["timeLimit"]
        vip = spec["vip"]
        adjusted = math.floor(time_limit * C.VIP_TIME_FACTOR) if vip else time_limit

        order = {
            "id": self.order_id_counter,
            "dish": spec["dish"],
            "icon": C.RECIPES[spec["dish"]]["icon"],
            "components": self._make_components(spec["dish"]),
            "timeLeft": float(adjusted),
            "maxTime": float(adjusted),
            "vip": vip,
            "standId": stand["id"],
        }
        self.order_id_counter += 1
        stand["order"] = order
        self.orders.append(order)
        self.spawned_log.append((order["dish"], order["vip"]))
        return True

    # -- main update loop --------------------------------------------------
    def tick(self, dt: float):
        """Port of update(dt) (game.js:1540)."""
        if not self.running or self.paused:
            return
        self.time += dt
        self.difficulty = C.compute_difficulty(self.time, self._perf())

        # endurance score trickle
        if self.time >= C.PHASE_ENDURANCE_START:
            self.score += dt * self.difficulty

        phase = C.phase_key(self.time)

        # rush state machine
        if self.rush["active"]:
            self.rush["timeLeft"] -= dt
            if self.rush["timeLeft"] <= 0:
                self.rush["active"] = False
                if C.is_high_pressure(phase):
                    base, span = C.RUSH_COOLDOWN_HIGH
                else:
                    base, span = C.RUSH_COOLDOWN_LOW
                self.rush["cooldown"] = base + self.rng.random() * span
        else:
            self.rush["cooldown"] -= dt
            if self.rush["cooldown"] <= 0:
                self.rush["active"] = True
                if C.is_high_pressure(phase):
                    base, span = C.RUSH_ACTIVE_HIGH
                else:
                    base, span = C.RUSH_ACTIVE_LOW
                self.rush["timeLeft"] = base + self.rng.random() * span
                free_stands = sum(1 for s in self.reception_stands if self._stand_free(s))
                burst_target = min(C.RUSH_BURST_MAX, free_stands)
                for _ in range(burst_target):
                    if not self._spawn_order():
                        break

        # scripted initial spawns (setTimeout in the browser)
        if not self._initial_spawns_done[0] and self.time >= C.INITIAL_SPAWN_TIMES[0]:
            self._initial_spawns_done[0] = True
            self._spawn_order()
        if not self._initial_spawns_done[1] and self.time >= C.INITIAL_SPAWN_TIMES[1]:
            self._initial_spawns_done[1] = True
            self._spawn_order()

        # debt-accumulator spawner
        self._refill_upcoming_queue()
        spawn_every = C.base_spawn_interval(self.time, self.rush["active"], self._perf())
        self.order_spawn_debt += dt / spawn_every
        spawns_this_frame = 0
        while self.order_spawn_debt >= 1 and spawns_this_frame < C.MAX_SPAWNS_PER_FRAME:
            if not self._spawn_order():
                break
            self.order_spawn_debt -= 1
            spawns_this_frame += 1
        self._recompute_upcoming_etas()

        # chefs, stations, orders
        self._rebuild_reservations()
        for chef in self.chefs:
            self._update_chef(chef, dt)
        self._update_stations(dt)
        self._update_orders(dt)

        if self.failed_orders >= self.max_failed_orders:
            self._end_game()

    def _update_chef(self, chef, dt):
        """Port of updateChef (game.js:1642)."""
        if chef["boostCooldown"] > 0:
            chef["boostCooldown"] = max(0.0, chef["boostCooldown"] - dt)
        if chef["boostActive"]:
            chef["boostTime"] -= dt
            if chef["boostTime"] <= 0:
                chef["boostActive"] = False
                chef["boostTime"] = 0.0
        if chef["commitmentStall"] > 0:
            chef["commitmentStall"] = max(0.0, chef["commitmentStall"] - dt)
            return

        if chef["actionTimer"] > 0:
            chef["actionTimer"] -= dt
            if chef["actionTimer"] <= 0:
                chef["busy"] = False
                wa = chef["waitingAt"]
                if wa is not None:
                    if wa["processing"] is not None:
                        chef["holding"] = wa["processing"]
                        chef["holding"]["state"] = C.STATE_CHOPPED
                        wa["processing"] = None
                        wa["busy"] = False
                    chef["waitingAt"] = None
                ws = chef["waitingAtStove"]
                if ws is not None:
                    if ws["cooking"] is not None:
                        chef["holding"] = ws["cooking"]
                        chef["holding"]["state"] = C.STATE_COOKED
                        ws["cooking"] = None
                        ws["busy"] = False
                    chef["waitingAtStove"] = None
            return

        chef["moveTimer"] += dt
        move_delay = C.MOVE_DELAY * (C.BOOST_MOVE_FACTOR if chef["boostActive"] else 1.0)
        if chef["path"] and chef["moveTimer"] >= move_delay:
            nxt = chef["path"][0]
            if self._reserve_step(chef, nxt[0], nxt[1]):
                chef["x"], chef["y"] = nxt[0], nxt[1]
                chef["path"].pop(0)
                chef["blockedTicks"] = 0
                if not chef["path"] and chef["targetStation"] is not None:
                    self._interact(chef, chef["targetStation"])
                    chef["targetStation"] = None
            else:
                chef["blockedTicks"] += 1
                if chef["blockedTicks"] >= 3:
                    dest = chef["path"][-1]
                    avoid = self._build_avoid_set(chef, dest)
                    detour = self._find_path(chef["x"], chef["y"], dest[0], dest[1], avoid)
                    if detour:
                        chef["path"] = detour
                        chef["blockedTicks"] = 0
                    elif chef["blockedTicks"] >= 30:
                        chef["path"] = []
                        chef["targetStation"] = None
                        chef["blockedTicks"] = 0
            chef["moveTimer"] = 0.0

    def _update_stations(self, dt):
        for stove in self.stoves:
            if stove["cooking"] is not None:
                stove["cookTime"] += dt
                if stove["cookTime"] >= stove["maxCookTime"] * C.STOVE_AUTOBURN_FRAC:
                    stove["cooking"]["state"] = C.STATE_BURNT
        for board in self.cutting_boards:
            if board["processing"] is not None and board["busy"]:
                board["processTime"] += dt
        for stand in self.reception_stands:
            if stand["customer"] is not None:
                stand["customer"]["timeLeft"] -= dt
                if stand["customer"]["timeLeft"] <= 0:
                    stand["customer"] = None

    def _update_orders(self, dt):
        for i in range(len(self.orders) - 1, -1, -1):
            self.orders[i]["timeLeft"] -= dt
            if self.orders[i]["timeLeft"] <= 0:
                expired = self.orders[i]
                self.failed_orders += 1
                self.expired_total += 1
                self.score += -C.EXPIRE_PENALTY
                self.streak = 0
                stand = next((s for s in self.reception_stands if s["order"] is expired), None)
                if stand:
                    stand["order"] = None
                self.orders.pop(i)

    def _end_game(self):
        self.running = False
        self.game_over = True

    # -- station interaction ----------------------------------------------
    def _interact(self, chef, station_info):
        """Port of interactWithStation (game.js:1224)."""
        stype = station_info["type"]
        station = station_info["station"]
        holding = chef["holding"]

        if stype == "ingredientBin":
            if holding is None:
                chef["holding"] = {"ingredient": station["ingredient"], "state": C.STATE_RAW}

        elif stype == "counter":
            items = station.setdefault("items", [])
            if holding is not None:
                if len(items) >= 1:
                    top = items[-1]
                    if top.get("type") == "plate" and holding.get("type") != "plate":
                        top["items"].append(holding)
                        chef["holding"] = None
                        return
                    if holding.get("type") == "plate" and top.get("type") != "plate":
                        holding["items"].append(top)
                        items.pop()
                        return
                    return  # full / incompatible
                items.append(holding)
                chef["holding"] = None
            elif items:
                chef["holding"] = items.pop()

        elif stype == "cuttingBoard":
            if station["processing"] is not None and station["processTime"] >= station["maxProcessTime"]:
                if holding is None:
                    chef["holding"] = station["processing"]
                    chef["holding"]["state"] = C.STATE_CHOPPED
                    station["processing"] = None
                    station["processTime"] = 0.0
                    station["busy"] = False
            elif holding is not None and holding.get("state") == C.STATE_RAW and not station["busy"]:
                if holding.get("ingredient") == "dough":
                    return  # no slicing bread
                station["processing"] = holding
                station["processTime"] = 0.0
                station["busy"] = True
                chef["holding"] = None
                chef["busy"] = True
                chef["actionTimer"] = station["maxProcessTime"]
                chef["waitingAt"] = station

        elif stype == "stove":
            if station["cooking"] is not None:
                cook_progress = station["cookTime"] / station["maxCookTime"]
                if cook_progress >= C.STOVE_READY_FRAC and holding is None:
                    chef["holding"] = station["cooking"]
                    if station["cookTime"] >= station["maxCookTime"] * C.STOVE_BURNT_FRAC:
                        chef["holding"]["state"] = C.STATE_BURNT
                    else:
                        chef["holding"]["state"] = C.STATE_COOKED
                    station["cooking"] = None
                    station["cookTime"] = 0.0
                    station["busy"] = False
            elif holding is not None and not station["busy"]:
                station["cooking"] = holding
                station["cookTime"] = 0.0
                station["busy"] = True
                chef["holding"] = None
                chef["busy"] = True
                chef["actionTimer"] = station["maxCookTime"]
                chef["waitingAtStove"] = station

        elif stype == "platingArea":
            if holding is not None and holding.get("type") != "plate":
                if holding.get("ingredient") == "meat" and holding.get("state") == C.STATE_RAW:
                    return  # no serving raw meat
                station["items"].append(holding)
                chef["holding"] = None
            elif holding is not None and holding.get("type") == "plate":
                station["items"] = station["items"] + (holding.get("items") or [])
                chef["holding"] = None
            elif holding is None and station["items"]:
                chef["holding"] = {"type": "plate", "items": list(station["items"])}
                station["items"] = []

        elif stype == "receptionStand":
            if holding is not None and holding.get("type") == "plate" and station["order"] is not None:
                order = station["order"]
                if self._check_delivery(holding, order):
                    total = C.delivery_score(self.difficulty, order["timeLeft"],
                                             self.streak, order["vip"])
                    self.score += total
                    self.streak += 1
                    self.best_streak = max(self.best_streak, self.streak)
                    self.orders_delivered += 1
                    self.delivered_total += 1
                    station["order"] = None
                    idx = next((k for k, o in enumerate(self.orders) if o is order), -1)
                    if idx > -1:
                        self.orders.pop(idx)
                    station["customer"] = {"timeLeft": C.CUSTOMER_EAT_TIME}
                else:
                    self.wrong_total += 1
                    self.streak = 0
                chef["holding"] = None

        elif stype == "trash":
            if holding is not None:
                chef["holding"] = None

    def _check_delivery(self, plate, order) -> bool:
        """Port of checkDelivery (game.js:1419)."""
        required = order["components"]
        delivered = plate["items"]
        if len(delivered) != len(required):
            return False
        for req in required:
            found = any(it.get("ingredient") == req["ingredient"]
                        and it.get("state") == req["state"] for it in delivered)
            if not found:
                return False
        return True

    # -- pathfinding / reservations ---------------------------------------
    def _is_walkable(self, x, y) -> bool:
        return self.grid[y][x] == C.TILE_FLOOR

    @staticmethod
    def _heuristic(x1, y1, x2, y2):
        return abs(x1 - x2) + abs(y1 - y2)

    def _neighbors(self, x, y, avoid):
        out = []
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < C.MAP_WIDTH and 0 <= ny < C.MAP_HEIGHT:
                if self._is_walkable(nx, ny) and not (avoid and f"{nx},{ny}" in avoid):
                    out.append((nx, ny))
        return out

    def _find_path(self, sx, sy, ex, ey, avoid=None):
        open_set = [{"x": sx, "y": sy, "f": self._heuristic(sx, sy, ex, ey)}]
        closed = set()
        came_from = {}
        g = {f"{sx},{sy}": 0}
        while open_set:
            open_set.sort(key=lambda n: n["f"])
            current = open_set.pop(0)
            cx, cy = current["x"], current["y"]
            ck = f"{cx},{cy}"
            if cx == ex and cy == ey:
                return self._reconstruct(came_from, current)
            closed.add(ck)
            for nx, ny in self._neighbors(cx, cy, avoid):
                nk = f"{nx},{ny}"
                if nk in closed:
                    continue
                tentative = g[ck] + 1
                if nk not in g or tentative < g[nk]:
                    came_from[nk] = current
                    g[nk] = tentative
                    fscore = tentative + self._heuristic(nx, ny, ex, ey)
                    if not any(n["x"] == nx and n["y"] == ny for n in open_set):
                        open_set.append({"x": nx, "y": ny, "f": fscore})
        return []

    @staticmethod
    def _reconstruct(came_from, current):
        path = [(current["x"], current["y"])]
        key = f"{current['x']},{current['y']}"
        while key in came_from:
            current = came_from[key]
            path.insert(0, (current["x"], current["y"]))
            key = f"{current['x']},{current['y']}"
        return path[1:]

    def _find_adjacent_walkable(self, sx, sy):
        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            nx, ny = sx + dx, sy + dy
            if 0 <= nx < C.MAP_WIDTH and 0 <= ny < C.MAP_HEIGHT and self._is_walkable(nx, ny):
                return (nx, ny)
        return None

    def _is_chef_adjacent(self, cx, cy, sx, sy) -> bool:
        return abs(cx - sx) + abs(cy - sy) == 1 and self._is_walkable(cx, cy)

    def _rebuild_reservations(self):
        self.reservations = {}
        for chef in self.chefs:
            self.reservations[f"{chef['x']},{chef['y']}"] = chef

    def _build_avoid_set(self, exclude_chef, except_tile):
        except_key = f"{except_tile[0]},{except_tile[1]}" if except_tile else None
        out = set()
        for key, chef in self.reservations.items():
            if chef is exclude_chef:
                continue
            if except_key is not None and key == except_key:
                continue
            out.add(key)
        return out

    def _reserve_step(self, chef, nx, ny) -> bool:
        target_key = f"{nx},{ny}"
        occupant = self.reservations.get(target_key)
        if occupant is not None and occupant is not chef:
            return False
        self.reservations.pop(f"{chef['x']},{chef['y']}", None)
        self.reservations[target_key] = chef
        return True

    def _assign_chef_path(self, chef, new_path, new_target_station):
        """Port of assignChefPath (game.js:1182) — applies the commitment stall."""
        was_in_transit = chef["path"] and len(chef["path"]) >= C.COMMITMENT_STALL_MIN_REMAINING
        old_id = chef["targetStation"]["station"]["id"] if chef["targetStation"] else None
        new_id = new_target_station["station"]["id"] if new_target_station else None
        if was_in_transit and old_id != new_id:
            chef["commitmentStall"] = C.COMMITMENT_STALL_SECONDS
        chef["path"] = new_path
        chef["targetStation"] = new_target_station

    # -- agent API ---------------------------------------------------------
    def command(self, chef_id, target_id):
        """Port of KitchenAPI.command (game.js:3447)."""
        if not self.running or self.paused:
            return {"success": False, "error": "Game not running"}
        chef = next((c for c in self.chefs if c["id"] == chef_id), None)
        if chef is None:
            return {"success": False, "error": "Invalid chef_id"}
        if chef["busy"]:
            return {"success": False, "error": "Chef is busy"}

        station = self._station_by_id.get(target_id)
        if station is None:
            return {"success": False, "error": "Invalid target"}
        station_info = {"type": station["type"], "station": station}
        tx, ty = station["x"], station["y"]

        adjacent = self._find_adjacent_walkable(tx, ty)
        if adjacent is None:
            return {"success": False, "error": "Cannot reach target"}

        if self._is_chef_adjacent(chef["x"], chef["y"], tx, ty):
            chef["path"] = []
            chef["targetStation"] = None
            self._interact(chef, station_info)
            return {"success": True}

        path = self._find_path(chef["x"], chef["y"], adjacent[0], adjacent[1],
                               self._build_avoid_set(chef, adjacent))
        if not path:
            return {"success": False, "error": "No path found"}

        self._assign_chef_path(chef, path, station_info)
        return {"success": True}

    def boost(self, chef_id):
        chef = next((c for c in self.chefs if c["id"] == chef_id), None)
        if chef is None or chef["boostActive"] or chef["boostCooldown"] > 0:
            return {"success": False, "error": "Boost not available"}
        chef["boostActive"] = True
        chef["boostTime"] = C.BOOST_TIME
        chef["boostCooldown"] = C.BOOST_COOLDOWN
        return {"success": True}

    # -- observation snapshot (mirrors getState in game.js:3353) -----------
    def get_state(self):
        return {
            "time": self.time,
            "score": self.score,
            "difficulty": self.difficulty,
            "streak": self.streak,
            "bestStreak": self.best_streak,
            "rush": dict(self.rush),
            "failedOrders": self.failed_orders,
            "maxFailedOrders": self.max_failed_orders,
            "phase": C.phase_key(self.time),
            "running": self.running,
            "paused": self.paused,
            "gameOver": self.game_over,
            "chefs": [{
                "id": c["id"], "name": c["name"], "pos": (c["x"], c["y"]),
                "holding": c["holding"], "busy": c["busy"],
                "hasPath": len(c["path"]) > 0,
                "boostActive": c["boostActive"], "boostTime": c["boostTime"],
                "boostCooldown": c["boostCooldown"], "stall": c["commitmentStall"],
            } for c in self.chefs],
            "stations": {
                "ingredientBins": [{"id": b["id"], "pos": (b["x"], b["y"]),
                                    "ingredient": b["ingredient"]} for b in self.ingredient_bins],
                "stoves": [{
                    "id": s["id"], "pos": (s["x"], s["y"]),
                    "cooking": s["cooking"], "cookTime": s["cookTime"],
                    "maxCookTime": s["maxCookTime"],
                    "ready": s["cookTime"] >= s["maxCookTime"] * C.STOVE_READY_FRAC
                             and s["cookTime"] < s["maxCookTime"] * C.STOVE_BURNT_FRAC,
                    "burnt": s["cookTime"] >= s["maxCookTime"] * C.STOVE_BURNT_FRAC,
                } for s in self.stoves],
                "cuttingBoards": [{
                    "id": b["id"], "pos": (b["x"], b["y"]), "busy": b["busy"],
                    "processing": b["processing"], "processTime": b["processTime"],
                    "maxProcessTime": b["maxProcessTime"],
                } for b in self.cutting_boards],
                "platingAreas": [{"id": p["id"], "pos": (p["x"], p["y"]),
                                  "items": p["items"]} for p in self.plating_areas],
                "receptionStands": [{
                    "id": r["id"], "pos": (r["x"], r["y"]),
                    "order": ({"id": r["order"]["id"], "dish": r["order"]["dish"],
                               "timeLeft": r["order"]["timeLeft"],
                               "components": r["order"]["components"]}
                              if r["order"] is not None else None),
                } for r in self.reception_stands],
                "trashCans": [{"id": t["id"], "pos": (t["x"], t["y"])} for t in self.trash_cans],
                "counters": [{"id": c["id"], "pos": (c["x"], c["y"]), "items": c["items"]}
                             for c in self.counters],
            },
            "orders": [{"id": o["id"], "dish": o["dish"], "timeLeft": o["timeLeft"],
                        "standId": o["standId"], "components": o["components"]}
                       for o in self.orders],
            "upcomingOrders": [{"dish": u["dish"], "etaSeconds": u["etaSeconds"]}
                               for u in self.upcoming_orders],
        }

    # -- convenience for tests --------------------------------------------
    def debug_place_order(self, dish, time_left, vip=False, stand_index=0):
        """Place a fully-controlled order on a stand (tests only). Mirrors the
        order object that spawnOrder builds, minus the RNG.  ``time_left`` is used
        verbatim (no VIP 0.85 tightening — pass the value you want)."""
        stand = self.reception_stands[stand_index]
        order = {
            "id": self.order_id_counter,
            "dish": dish,
            "icon": C.RECIPES[dish]["icon"],
            "components": self._make_components(dish),
            "timeLeft": float(time_left),
            "maxTime": float(time_left),
            "vip": vip,
            "standId": stand["id"],
        }
        self.order_id_counter += 1
        stand["order"] = order
        self.orders.append(order)
        return order

    def advance(self, seconds: float, dt: float = 1.0 / 60.0):
        """Tick forward ``seconds`` in fixed ``dt`` steps."""
        n = int(round(seconds / dt))
        for _ in range(n):
            if self.game_over:
                break
            self.tick(dt)

    def run_until_chef_idle(self, chef_id, dt: float = 1.0 / 60.0, max_seconds: float = 60.0):
        """Tick until ``chef_id`` is idle (no path, not busy, not stalled)."""
        steps = int(round(max_seconds / dt))
        for _ in range(steps):
            c = self.chefs[chef_id]
            if not c["busy"] and not c["path"] and c["commitmentStall"] <= 0:
                return True
            if self.game_over:
                return False
            self.tick(dt)
        c = self.chefs[chef_id]
        return not c["busy"] and not c["path"] and c["commitmentStall"] <= 0


# ===========================================================================
# Gymnasium environment (event-driven semi-MDP)
# ===========================================================================
_EnvBase = gym.Env if _HAS_GYM else object


class ChefOverflowEnv(_EnvBase):
    """Event-driven semi-MDP over :class:`KitchenSim` (spec §5)."""

    metadata = {"render_modes": []}

    def __init__(self, seed: int = 0, time_cap: float = C.DEFAULT_TIME_CAP,
                 dt: float = 1.0 / 60.0, p_expiry: float = 300.0, p_wrong: float = 300.0,
                 c_time: float = 0.0, reward_scale: float = 1.0):
        super().__init__()
        self.sim = KitchenSim(seed)
        self.time_cap = time_cap
        self.dt = dt
        self.p_expiry = p_expiry
        self.p_wrong = p_wrong
        self.c_time = c_time
        self.reward_scale = reward_scale
        self._seed = seed
        self.decision_chef = 0
        self._decidable_queue = []

        if _HAS_GYM:
            self.action_space = spaces.Discrete(E.NUM_ACTIONS)
            self.observation_space = spaces.Box(
                low=-np.inf, high=np.inf, shape=(E.OBS_DIM,), dtype=np.float32)

    # -- helpers -----------------------------------------------------------
    def _is_decidable(self, i: int) -> bool:
        if self.sim.game_over or not self.sim.running:
            return False
        c = self.sim.chefs[i]
        return (not c["busy"]) and (len(c["path"]) == 0) and (c["commitmentStall"] <= 0)

    def _frame_decidable(self):
        return [i for i in range(C.NUM_CHEFS) if self._is_decidable(i)]

    def _target_for_action(self, action: int, mask: np.ndarray):
        """Resolve a macro to a (chef stays) station id, or None for WAIT/no-op."""
        if action == E.ACT_WAIT or mask[action] == 0.0:
            return None
        if E.FETCH_BASE <= action < E.FETCH_BASE + len(C.INGREDIENTS):
            return f"bin_{action - E.FETCH_BASE}"           # FETCH_i -> bin_i (orders match)
        if action == E.ACT_CHOP:
            for b in self.sim.cutting_boards:
                if not b["busy"]:
                    return b["id"]
            return None
        if action == E.ACT_COOK:
            for s in self.sim.stoves:
                if s["cooking"] is None:
                    return s["id"]
            return None
        if E.DEPOSIT_BASE <= action < E.DEPOSIT_BASE + C.NUM_PLATING:
            return f"plating_{action - E.DEPOSIT_BASE}"
        if E.TAKE_PLATE_BASE <= action < E.TAKE_PLATE_BASE + C.NUM_PLATING:
            return f"plating_{action - E.TAKE_PLATE_BASE}"
        if E.DELIVER_BASE <= action < E.DELIVER_BASE + C.NUM_STANDS:
            return f"reception_{action - E.DELIVER_BASE}"
        if action == E.ACT_TRASH:
            return "trash_0"
        return None

    def _apply_macro(self, chef_id: int, action: int):
        state = self.sim.get_state()
        mask = E.action_mask(state, chef_id)
        target = self._target_for_action(action, mask)
        if target is not None:
            self.sim.command(chef_id, target)
        # WAIT / masked-invalid -> no command issued this frame.

    def _obs(self):
        return E.encode(self.sim.get_state(), self.decision_chef)

    def _info(self):
        return {
            "action_mask": E.action_mask(self.sim.get_state(), self.decision_chef),
            "decision_chef": self.decision_chef,
            "score": self.sim.score,
            "time": self.sim.time,
            "delivered": self.sim.delivered_total,
            "expired": self.sim.expired_total,
            "wrong": self.sim.wrong_total,
            "no_slot": self.sim.no_slot_total,
            "streak": self.sim.streak,
        }

    # -- gym API -----------------------------------------------------------
    def reset(self, *, seed=None, options=None):
        if seed is None:
            seed = self._seed
        self._seed = seed
        self.sim.reset(seed)
        self._decidable_queue = self._frame_decidable()
        # Advance to the first decision point if (somehow) none are ready.
        ticks = 0
        max_ticks = int(self.time_cap / self.dt) + 10
        while not self._decidable_queue and not self.sim.game_over and self.sim.time < self.time_cap:
            self.sim.tick(self.dt)
            self._decidable_queue = self._frame_decidable()
            ticks += 1
            if ticks > max_ticks:
                break
        self.decision_chef = self._decidable_queue.pop(0) if self._decidable_queue else 0
        return self._obs(), self._info()

    def step(self, action: int):
        action = int(action)
        score_before = self.sim.score
        delivered_b = self.sim.delivered_total
        expired_b = self.sim.expired_total
        wrong_b = self.sim.wrong_total
        no_slot_b = self.sim.no_slot_total

        # 1) apply the macro to the current decision chef
        self._apply_macro(self.decision_chef, action)

        # 2) advance to the next decision point
        max_ticks = int(self.time_cap / self.dt) + 10
        ticks = 0
        next_chef = None
        while True:
            # remaining offers in the current frame (zero time advance)
            while self._decidable_queue:
                cand = self._decidable_queue.pop(0)
                if self._is_decidable(cand):
                    next_chef = cand
                    break
            if next_chef is not None:
                break
            if self.sim.game_over or self.sim.time >= self.time_cap or ticks > max_ticks:
                break
            self.sim.tick(self.dt)
            ticks += 1
            self._decidable_queue = self._frame_decidable()

        # 3) reward (spec §5.4)
        score_delta = self.sim.score - score_before
        expired_delta = (self.sim.expired_total - expired_b) + (self.sim.no_slot_total - no_slot_b)
        wrong_delta = self.sim.wrong_total - wrong_b
        reward = (score_delta
                  - self.p_expiry * expired_delta
                  - self.p_wrong * wrong_delta
                  - self.c_time)
        reward *= self.reward_scale

        terminated = bool(self.sim.game_over)
        truncated = bool(self.sim.time >= self.time_cap and not self.sim.game_over)

        if next_chef is not None:
            self.decision_chef = next_chef
        obs = self._obs()
        info = self._info()
        info["score_delta"] = score_delta
        info["ticks_advanced"] = ticks
        return obs, float(reward), terminated, truncated, info


def make_env(**kwargs):
    """Factory for vectorized training (Phase 3)."""
    return ChefOverflowEnv(**kwargs)
