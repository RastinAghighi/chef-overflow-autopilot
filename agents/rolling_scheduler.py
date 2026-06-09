"""Milestone 2 deterministic rolling scheduler.

This is a browser-faithful, station-target-only controller that lives beside the
existing fallback planner.  It is intentionally deterministic and conservative:
all decisions are derived from the visible ``get_state()`` surface plus local
constants for recipe definitions in the Python sim.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from itertools import count
from typing import Any

from sim import constants as C


TICKS_PER_TILE = max(1, round(C.MOVE_DELAY * 60))
COOK_TICKS = round(C.COOK_TIME * 60)
CHOP_TICKS = round(C.CHOP_TIME * 60)
CUSTOMER_TICKS = round(C.CUSTOMER_EAT_TIME * 60)

CHOPPABLE = {"tomato", "lettuce", "onion"}
COOKABLE = {"meat", "dough"}
RAW_ONLY = {"cheese"}

IMMINENT_EXPIRY_SEC = 7.0
PRESSURE_RED_OCCUPIED = 4
PRESSURE_BLACKOUT_OCCUPIED = 5
MAX_CANDIDATES_PER_CHEF = 14


def _pos(obj: dict[str, Any]) -> tuple[int, int]:
    p = obj["pos"]
    return (int(p[0]), int(p[1]))


def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _is_plate(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "plate"


def _component_key(item: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not isinstance(item, dict) or _is_plate(item):
        return (None, None)
    return (item.get("ingredient"), item.get("state"))


def _counter(items: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> Counter:
    return Counter(_component_key(item) for item in (items or []) if not _is_plate(item))


def _recipe_counter(dish: str) -> Counter:
    return Counter(C.RECIPES[dish]["components"])


def _recipe_items(dish: str) -> list[tuple[str, str]]:
    return list(C.RECIPES[dish]["components"])


def _processed_form(ingredient: str) -> tuple[str, str] | None:
    if ingredient in CHOPPABLE:
        return (ingredient, "chopped")
    if ingredient in COOKABLE:
        return (ingredient, "cooked")
    return None


def _item_matches_recipe(items: list[dict[str, Any]], dish: str) -> bool:
    have = _counter(items)
    req = _recipe_counter(dish)
    return not (have - req) and not (req - have)


def _matching_dish_for_items(items: list[dict[str, Any]]) -> str | None:
    for dish in C.DISH_NAMES:
        if _item_matches_recipe(items, dish):
            return dish
    return None


def _items_fit_recipe(items: list[dict[str, Any]], dish: str) -> bool:
    have = _counter(items)
    req = _recipe_counter(dish)
    return not (have - req)


def _order_counter(order: dict[str, Any]) -> Counter:
    return Counter((c.get("ingredient"), c.get("state")) for c in order.get("components", []) or [])


def _plate_counter(plate: dict[str, Any]) -> Counter:
    return _counter(plate.get("items", []) or [])


def _cnt_eq(a: Counter, b: Counter) -> bool:
    return not (a - b) and not (b - a)


def _station_lists(stations: dict[str, Any]) -> list[list[dict[str, Any]]]:
    return [
        stations.get("ingredientBins", []) or [],
        stations.get("stoves", []) or [],
        stations.get("cuttingBoards", []) or [],
        stations.get("platingAreas", []) or [],
        stations.get("receptionStands", []) or [],
        stations.get("trashCans", []) or [],
        stations.get("counters", []) or [],
    ]


@dataclass
class Task:
    id: str
    kind: str
    target_id: str
    target_pos: tuple[int, int]
    value: float
    processing_ticks: int = 0
    order_id: int | None = None
    dish: str | None = None
    component: tuple[str, str] | None = None
    area_id: str | None = None
    counter_id: str | None = None
    source: str = "active"
    deadline_sec: float = 9999.0
    hard: tuple[str, ...] = field(default_factory=tuple)
    commitment: dict[str, Any] | None = None
    note: str = ""


@dataclass
class Layout:
    station_by_id: dict[str, dict[str, Any]]
    pos_by_id: dict[str, tuple[int, int]]
    bins_by_ing: dict[str, dict[str, Any]]
    areas: list[dict[str, Any]]
    stoves: list[dict[str, Any]]
    boards: list[dict[str, Any]]
    stands: list[dict[str, Any]]
    counters: list[dict[str, Any]]
    trash_id: str
    handoff_counter_ids: list[str]
    service_handoff_ids: list[str]
    zone_by_station: dict[str, str]


class RollingScheduler:
    """Deterministic rolling scheduler for the Python sim.

    The scheduler keeps a small explicit execution state: one committed task per
    moving/busy chef, one optional component commitment, area plans for active and
    build-ahead dishes, inferred customer timers, and soft corridor windows.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.area_plans: dict[str, dict[str, Any]] = {}
        self.chef_target: dict[int, str] = {}
        self.chef_task: dict[int, Task] = {}
        self.chef_commitment: dict[int, dict[str, Any]] = {}
        self.prev_pos: dict[int, tuple[int, int]] = {}
        self.stuck_time: dict[int, float] = {}
        self.prev_time = 0.0
        self.prev_orders: dict[int, dict[str, Any]] = {}
        self.prev_failed_orders = 0
        self.inferred_customers: dict[str, int] = {}
        self.failed_until: dict[tuple[int, str], int] = {}
        self.corridor_windows: list[tuple[int, int, int, str]] = []
        self.chef_area: dict[int, str] = {}
        self.roles = {
            0: "cook",
            1: "chopper",
            2: "assembler",
            3: "runner",
            4: "flex",
        }
        self._task_seq = count(1)
        self._last_layout_summary: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public diagnostics
    # ------------------------------------------------------------------
    def layout_summary(self) -> dict[str, Any]:
        return dict(self._last_layout_summary)

    # ------------------------------------------------------------------
    # Core decision loop
    # ------------------------------------------------------------------
    def decide(self, state: dict[str, Any], api: Any) -> None:
        if not state.get("running") or state.get("paused") or state.get("gameOver"):
            return

        tick = self._tick(state)
        layout = self._layout(state)
        self._update_customer_inference(state, tick)
        self._purge_transient_state(state, tick)
        self._reconcile_roles(state)
        self._reconcile_area_plans(state, layout, tick)
        self._assign_plan_owners(state, layout)
        self._recover_blocked_chefs(state, api, layout, tick)

        chefs = state.get("chefs", []) or []
        idle = [c for c in chefs if self._is_idle(c)]
        if not idle:
            return

        pressure = self._stand_pressure(state, layout)
        busy_targets = self._busy_targets(state)
        candidates_by_chef: dict[int, list[tuple[float, Task]]] = {}
        for chef in sorted(idle, key=lambda c: int(c["id"])):
            cid = int(chef["id"])
            candidates = self._candidate_tasks(chef, state, layout, pressure, busy_targets, tick)
            useful = [t for t in candidates if t.kind not in {"WAIT", "PARK"} and t.value > 0]
            if useful:
                candidates = useful + [t for t in candidates if t.kind == "WAIT"]
            ranked: list[tuple[float, Task]] = []
            for task in candidates:
                if self.failed_until.get((cid, task.target_id), -1) > tick:
                    continue
                cost = self._task_cost(chef, task, pressure, tick)
                ranked.append((cost, task))
            ranked.sort(key=lambda ct: (ct[0], ct[1].deadline_sec, ct[1].id, ct[1].target_id))
            candidates_by_chef[cid] = ranked[:MAX_CANDIDATES_PER_CHEF]

        assignments = self._match(idle, candidates_by_chef)
        for chef in sorted(idle, key=lambda c: int(c["id"])):
            task = assignments.get(int(chef["id"]))
            if task is not None and task.kind != "WAIT":
                self._issue(api, chef, task, tick)

    # ------------------------------------------------------------------
    # State reconciliation
    # ------------------------------------------------------------------
    @staticmethod
    def _tick(state: dict[str, Any]) -> int:
        return int(round(float(state.get("time", 0.0) or 0.0) * 60.0))

    @staticmethod
    def _is_idle(chef: dict[str, Any]) -> bool:
        return (
            not chef.get("busy")
            and not chef.get("hasPath")
            and float(chef.get("stall", 0.0) or 0.0) <= 0.0
        )

    def _layout(self, state: dict[str, Any]) -> Layout:
        stations = state["stations"]
        station_by_id: dict[str, dict[str, Any]] = {}
        pos_by_id: dict[str, tuple[int, int]] = {}
        for group in _station_lists(stations):
            for station in group:
                station_by_id[station["id"]] = station
                pos_by_id[station["id"]] = _pos(station)

        bins = stations.get("ingredientBins", []) or []
        areas = stations.get("platingAreas", []) or []
        stoves = stations.get("stoves", []) or []
        boards = stations.get("cuttingBoards", []) or []
        stands = stations.get("receptionStands", []) or []
        counters = stations.get("counters", []) or []
        trash = (stations.get("trashCans", []) or [{"id": "trash_0"}])[0]["id"]
        bins_by_ing = {b["ingredient"]: b for b in bins}

        service_gap = [(13, 6), (13, 7)]
        service_handoff = sorted(
            counters,
            key=lambda c: (
                min(_manhattan(_pos(c), g) for g in service_gap),
                _pos(c)[1],
                _pos(c)[0],
                c["id"],
            ),
        )[:4]

        processing = stoves + boards
        right_areas = [a for a in areas if _pos(a)[0] >= 10] or areas
        kitchen_handoff = sorted(
            counters,
            key=lambda c: (
                min((_manhattan(_pos(c), _pos(s)) for s in processing), default=0)
                + min((_manhattan(_pos(c), _pos(a)) for a in right_areas), default=0),
                abs(_pos(c)[0] - 10),
                _pos(c)[1],
                c["id"],
            ),
        )[:4]
        handoff_ids: list[str] = []
        for c in kitchen_handoff + service_handoff:
            if c["id"] not in handoff_ids:
                handoff_ids.append(c["id"])

        zone_by_station = {sid: self._zone(pos) for sid, pos in pos_by_id.items()}
        self._last_layout_summary = {
            "corridor_hot_tiles": [(6, 6), (6, 7), (7, 6), (7, 7)],
            "service_divider_gap": service_gap,
            "kitchen_handoff_counters": [
                {"id": c["id"], "pos": list(_pos(c))} for c in kitchen_handoff
            ],
            "service_handoff_counters": [
                {"id": c["id"], "pos": list(_pos(c))} for c in service_handoff
            ],
            "roles": dict(self.roles),
        }
        return Layout(
            station_by_id=station_by_id,
            pos_by_id=pos_by_id,
            bins_by_ing=bins_by_ing,
            areas=areas,
            stoves=stoves,
            boards=boards,
            stands=stands,
            counters=counters,
            trash_id=trash,
            handoff_counter_ids=handoff_ids,
            service_handoff_ids=[c["id"] for c in service_handoff],
            zone_by_station=zone_by_station,
        )

    @staticmethod
    def _zone(pos: tuple[int, int]) -> str:
        x, y = pos
        if (x, y) in {(13, 6), (13, 7)}:
            return "corridor_gap"
        if x <= 8:
            return "kitchen_left"
        if x <= 12:
            return "assembly_mid"
        return "service_right"

    @staticmethod
    def _near_hotspot(pos: tuple[int, int]) -> bool:
        x, y = pos
        return 5 <= x <= 8 and 5 <= y <= 8

    def _update_customer_inference(self, state: dict[str, Any], tick: int) -> None:
        dt_ticks = max(0, tick - int(round(self.prev_time * 60.0)))
        for sid in list(self.inferred_customers):
            self.inferred_customers[sid] = max(0, self.inferred_customers[sid] - dt_ticks)
            if self.inferred_customers[sid] <= 0:
                self.inferred_customers.pop(sid, None)

        current = {int(o["id"]): dict(o) for o in state.get("orders", []) or []}
        failed_now = int(state.get("failedOrders", 0) or 0)
        failed_delta = max(0, failed_now - self.prev_failed_orders)
        disappeared = [o for oid, o in self.prev_orders.items() if oid not in current]
        disappeared.sort(key=lambda o: (float(o.get("timeLeft", 0.0)), int(o.get("id", 0))))
        for order in disappeared:
            if failed_delta > 0 and float(order.get("timeLeft", 0.0) or 0.0) <= 0.35:
                failed_delta -= 1
                continue
            stand_id = order.get("standId")
            if stand_id:
                self.inferred_customers[str(stand_id)] = CUSTOMER_TICKS

        visible_stands = {str(o.get("standId")) for o in current.values()}
        for sid in visible_stands:
            self.inferred_customers.pop(sid, None)

        self.prev_orders = current
        self.prev_failed_orders = failed_now
        self.prev_time = float(state.get("time", 0.0) or 0.0)

    def _purge_transient_state(self, state: dict[str, Any], tick: int) -> None:
        live_chefs = {int(c["id"]) for c in state.get("chefs", []) or []}
        for cid in list(self.chef_target):
            chef = next((c for c in state.get("chefs", []) or [] if int(c["id"]) == cid), None)
            if chef is None or self._is_idle(chef):
                task = self.chef_task.pop(cid, None)
                self.chef_target.pop(cid, None)
                if task and task.kind in {"DEPOSIT_PLATING", "STAGE_COUNTER", "TRASH", "DELIVER"}:
                    self.chef_commitment.pop(cid, None)
        for cid in list(self.chef_commitment):
            if cid not in live_chefs:
                self.chef_commitment.pop(cid, None)
                continue
            chef = next(c for c in state.get("chefs", []) or [] if int(c["id"]) == cid)
            holding = chef.get("holding")
            if _is_plate(holding):
                self.chef_commitment.pop(cid, None)
            elif holding is None and cid not in self.chef_target:
                self.chef_commitment.pop(cid, None)
            elif holding is not None:
                final = self.chef_commitment[cid].get("component")
                raw_ing = self.chef_commitment[cid].get("raw_ingredient")
                key = _component_key(holding)
                if final and key == tuple(final):
                    continue
                if raw_ing and key == (raw_ing, "raw"):
                    continue
                self.chef_commitment.pop(cid, None)

        for key, until in list(self.failed_until.items()):
            if until <= tick:
                self.failed_until.pop(key, None)
        self.corridor_windows = [w for w in self.corridor_windows if w[1] > tick]

    def _reconcile_roles(self, state: dict[str, Any]) -> None:
        pressure = len(state.get("orders", []) or []) + sum(1 for v in self.inferred_customers.values() if v > 0)
        cook_demand = any(
            comp.get("state") == "cooked"
            for order in state.get("orders", []) or []
            for comp in order.get("components", []) or []
        )
        if pressure >= PRESSURE_RED_OCCUPIED:
            self.roles[4] = "runner"
        elif cook_demand:
            self.roles[4] = "cook"
        else:
            self.roles[4] = "flex"

    def _reconcile_area_plans(self, state: dict[str, Any], layout: Layout, tick: int) -> None:
        active_orders = list(state.get("orders", []) or [])
        active_by_id = {int(o["id"]): o for o in active_orders}
        areas_by_id = {a["id"]: a for a in layout.areas}

        for area_id in list(self.area_plans):
            area = areas_by_id.get(area_id)
            if area is None:
                self.area_plans.pop(area_id, None)
                continue
            plan = self.area_plans[area_id]
            items = area.get("items", []) or []
            oid = plan.get("order_id")
            if oid is not None and int(oid) not in active_by_id:
                plan["order_id"] = None
                plan["source"] = "orphan"
            if items and not _items_fit_recipe(items, plan["dish"]):
                self.area_plans.pop(area_id, None)
            elif not items and plan.get("source") == "orphan":
                self.area_plans.pop(area_id, None)

        planned_orders = {
            int(p["order_id"])
            for p in self.area_plans.values()
            if p.get("order_id") is not None and int(p["order_id"]) in active_by_id
        }
        planned_upcoming_dishes = Counter(
            p["dish"] for p in self.area_plans.values() if p.get("source") == "upcoming"
        )

        for plan in self.area_plans.values():
            if plan.get("order_id") is None and plan.get("source") in {"upcoming", "orphan"}:
                matches = [
                    o for o in active_orders
                    if o["dish"] == plan["dish"] and int(o["id"]) not in planned_orders
                ]
                if matches:
                    order = min(matches, key=lambda o: (float(o["timeLeft"]), int(o["id"])))
                    plan["order_id"] = int(order["id"])
                    plan["source"] = "active"
                    planned_orders.add(int(order["id"]))
                    planned_upcoming_dishes[plan["dish"]] -= 1

        empty_unplanned = [
            a for a in layout.areas
            if a["id"] not in self.area_plans and not (a.get("items") or [])
        ]
        for order in sorted(active_orders, key=lambda o: (float(o["timeLeft"]), int(o["id"]))):
            if int(order["id"]) in planned_orders or not empty_unplanned:
                continue
            area = self._choose_area_for_order(order, empty_unplanned, layout)
            empty_unplanned.remove(area)
            self.area_plans[area["id"]] = {
                "area_id": area["id"],
                "dish": order["dish"],
                "order_id": int(order["id"]),
                "source": "active",
                "created_tick": tick,
            }
            planned_orders.add(int(order["id"]))

        upcoming = list(state.get("upcomingOrders", []) or [])
        for idx, spec in enumerate(upcoming):
            if not empty_unplanned:
                break
            dish = spec.get("dish")
            if not dish or dish not in C.RECIPES:
                continue
            target_count = sum(1 for u in upcoming[: idx + 1] if u.get("dish") == dish)
            active_same = sum(1 for o in active_orders if o.get("dish") == dish and int(o["id"]) not in planned_orders)
            if planned_upcoming_dishes[dish] >= max(1, target_count - active_same):
                continue
            area = self._choose_area_for_upcoming(spec, empty_unplanned)
            empty_unplanned.remove(area)
            self.area_plans[area["id"]] = {
                "area_id": area["id"],
                "dish": dish,
                "order_id": None,
                "source": "upcoming",
                "upcoming_index": idx,
                "eta": float(spec.get("etaSeconds", 999.0) or 999.0),
                "created_tick": tick,
            }
            planned_upcoming_dishes[dish] += 1

        live_area_ids = set(areas_by_id)
        for cid, commitment in list(self.chef_commitment.items()):
            area_id = commitment.get("area_id")
            if area_id and area_id not in live_area_ids:
                self.chef_commitment.pop(cid, None)
            elif area_id and area_id not in self.area_plans:
                self.chef_commitment.pop(cid, None)

        for cid, area_id in list(self.chef_area.items()):
            if area_id not in self.area_plans:
                self.chef_area.pop(cid, None)

    def _assign_plan_owners(self, state: dict[str, Any], layout: Layout) -> None:
        chefs = {int(c["id"]): c for c in state.get("chefs", []) or []}
        for area_id, plan in list(self.area_plans.items()):
            owner = plan.get("owner_id")
            if owner is not None:
                owner = int(owner)
                if owner not in chefs or self.chef_area.get(owner) != area_id:
                    plan.pop("owner_id", None)
                elif plan.get("source") == "orphan":
                    self.chef_area.pop(owner, None)
                    plan.pop("owner_id", None)

        used = {
            int(plan["owner_id"])
            for plan in self.area_plans.values()
            if plan.get("owner_id") is not None
        }
        for cid in list(self.chef_area):
            if cid not in used:
                self.chef_area.pop(cid, None)

        available = [
            c for c in chefs.values()
            if int(c["id"]) not in used
            and int(c["id"]) not in self.chef_area
            and not _is_plate(c.get("holding"))
        ]
        available.sort(key=lambda c: (0 if self._is_idle(c) else 1, self.roles.get(int(c["id"]), "flex") == "runner", int(c["id"])))

        unowned = [
            (area_id, plan)
            for area_id, plan in self.area_plans.items()
            if plan.get("owner_id") is None and plan.get("source") in {"active", "upcoming"}
        ]
        unowned.sort(key=lambda kv: (0 if kv[1].get("source") == "active" else 1, self._plan_deadline(kv[1]), kv[0]))
        for area_id, plan in unowned:
            if not available:
                break
            area = layout.station_by_id.get(area_id)
            if not area:
                continue
            chef = min(
                available,
                key=lambda c: (
                    _manhattan(tuple(c["pos"]), _pos(area)),
                    0 if self.roles.get(int(c["id"])) != "runner" else 1,
                    int(c["id"]),
                ),
            )
            available.remove(chef)
            cid = int(chef["id"])
            plan["owner_id"] = cid
            self.chef_area[cid] = area_id

    @staticmethod
    def _choose_area_for_order(order: dict[str, Any], areas: list[dict[str, Any]], layout: Layout) -> dict[str, Any]:
        stand = next((s for s in layout.stands if s["id"] == order.get("standId")), None)
        stand_pos = _pos(stand) if stand else (17, 7)
        return min(
            areas,
            key=lambda a: (
                0 if _pos(a)[0] >= 10 else 1,
                _manhattan(_pos(a), stand_pos),
                _pos(a)[1],
                a["id"],
            ),
        )

    @staticmethod
    def _choose_area_for_upcoming(spec: dict[str, Any], areas: list[dict[str, Any]]) -> dict[str, Any]:
        return min(
            areas,
            key=lambda a: (
                0 if _pos(a)[0] >= 10 else 1,
                abs(_pos(a)[0] - 10),
                abs(_pos(a)[1] - 6),
                a["id"],
            ),
        )

    def _stand_pressure(self, state: dict[str, Any], layout: Layout) -> dict[str, Any]:
        visible = {s["id"] for s in layout.stands if s.get("order") is not None}
        inferred = {sid for sid, left in self.inferred_customers.items() if left > 0}
        occupied = visible | inferred
        if len(occupied) >= PRESSURE_BLACKOUT_OCCUPIED:
            level = "BLACKOUT"
        elif len(occupied) >= PRESSURE_RED_OCCUPIED:
            level = "RED"
        elif len(occupied) == 3:
            level = "YELLOW"
        else:
            level = "GREEN"
        return {"level": level, "occupied": len(occupied), "visible": len(visible), "inferred": len(inferred)}

    def _busy_targets(self, state: dict[str, Any]) -> set[str]:
        busy = set()
        for chef in state.get("chefs", []) or []:
            cid = int(chef["id"])
            target = self.chef_target.get(cid)
            if target and not self._is_idle(chef):
                busy.add(target)
        return busy

    # ------------------------------------------------------------------
    # Candidate generation
    # ------------------------------------------------------------------
    def _candidate_tasks(
        self,
        chef: dict[str, Any],
        state: dict[str, Any],
        layout: Layout,
        pressure: dict[str, Any],
        busy_targets: set[str],
        tick: int,
    ) -> list[Task]:
        cid = int(chef["id"])
        holding = chef.get("holding")
        tasks: list[Task] = []

        if _is_plate(holding):
            tasks.extend(self._deliver_tasks(holding, layout, state, pressure))
            if not tasks:
                dish = _matching_dish_for_items(holding.get("items", []) or [])
                upcoming_dishes = {u.get("dish") for u in state.get("upcomingOrders", []) or []}
                if dish in upcoming_dishes:
                    empty = [
                        area for area in layout.areas
                        if area["id"] not in busy_targets and not (area.get("items") or [])
                    ]
                    if empty:
                        area = min(empty, key=lambda a: (_pos(a)[0] < 10, _manhattan(tuple(chef["pos"]), _pos(a)), a["id"]))
                        tasks.append(
                            self._task(
                                "DEPOSIT_PLATING",
                                area["id"],
                                layout,
                                value=850,
                                dish=dish,
                                area_id=area["id"],
                                source="upcoming",
                                hard=(f"area:{area['id']}",),
                                note="return build-ahead plate",
                            )
                        )
            tasks.append(self._task("TRASH", layout.trash_id, layout, value=25, note="unmatched plate"))
            return tasks

        if holding is not None:
            tasks.extend(self._held_component_tasks(cid, holding, layout, state, pressure, busy_targets))
            if not tasks:
                tasks.append(self._task("TRASH", layout.trash_id, layout, value=10, note="unusable component"))
            return tasks

        tasks.extend(self._lift_plate_tasks(chef, layout, state, pressure, busy_targets))
        tasks.extend(self._unstage_counter_tasks(cid, layout, state, pressure, busy_targets))
        tasks.extend(self._fetch_tasks(cid, layout, state, pressure, busy_targets))
        park = self._park_task(chef, layout, busy_targets)
        if park:
            tasks.append(park)
        tasks.append(self._task("WAIT", layout.trash_id, layout, value=-5000, note="no-op"))
        return tasks

    def _task(
        self,
        kind: str,
        target_id: str,
        layout: Layout,
        *,
        value: float,
        processing_ticks: int = 0,
        order_id: int | None = None,
        dish: str | None = None,
        component: tuple[str, str] | None = None,
        area_id: str | None = None,
        counter_id: str | None = None,
        source: str = "active",
        deadline_sec: float = 9999.0,
        hard: tuple[str, ...] = (),
        commitment: dict[str, Any] | None = None,
        note: str = "",
    ) -> Task:
        seq = next(self._task_seq)
        if kind == "WAIT":
            base_hard = [f"wait:{seq}"]
        else:
            base_hard = [f"task:{kind}:{target_id}:{area_id}:{counter_id}:{component}:{order_id}", f"station:{target_id}"]
        base_hard.extend(hard)
        return Task(
            id=f"{kind}:{target_id}:{seq}",
            kind=kind,
            target_id=target_id,
            target_pos=layout.pos_by_id[target_id],
            value=value,
            processing_ticks=processing_ticks,
            order_id=order_id,
            dish=dish,
            component=component,
            area_id=area_id,
            counter_id=counter_id,
            source=source,
            deadline_sec=deadline_sec,
            hard=tuple(base_hard),
            commitment=commitment,
            note=note,
        )

    def _deliver_tasks(
        self,
        plate: dict[str, Any],
        layout: Layout,
        state: dict[str, Any],
        pressure: dict[str, Any],
    ) -> list[Task]:
        plate_c = _plate_counter(plate)
        tasks = []
        for order in state.get("orders", []) or []:
            if not _cnt_eq(plate_c, _order_counter(order)):
                continue
            stand = next((s for s in layout.stands if s["id"] == order["standId"]), None)
            if not stand:
                continue
            time_left = float(order.get("timeLeft", 999.0) or 999.0)
            value = 12000 + max(0.0, 60.0 - time_left) * 60.0
            if time_left <= IMMINENT_EXPIRY_SEC:
                value += 9000
            if pressure["occupied"] >= PRESSURE_RED_OCCUPIED:
                value += 4000
            tasks.append(
                self._task(
                    "DELIVER",
                    order["standId"],
                    layout,
                    value=value,
                    order_id=int(order["id"]),
                    dish=order["dish"],
                    deadline_sec=time_left,
                    hard=(f"stand:{order['standId']}", f"order:{order['id']}"),
                )
            )
        return tasks

    def _lift_plate_tasks(
        self,
        chef: dict[str, Any],
        layout: Layout,
        state: dict[str, Any],
        pressure: dict[str, Any],
        busy_targets: set[str],
    ) -> list[Task]:
        tasks = []
        orders = list(state.get("orders", []) or [])
        cid = int(chef["id"])
        chef_pos = tuple(chef["pos"])
        chefs_by_id = {int(c["id"]): c for c in state.get("chefs", []) or []}
        for area in layout.areas:
            if area["id"] in busy_targets:
                continue
            items = area.get("items", []) or []
            if not items:
                continue
            for order in orders:
                if not _cnt_eq(_counter(items), _order_counter(order)):
                    continue
                time_left = float(order.get("timeLeft", 999.0) or 999.0)
                value = 10000 + max(0.0, 55.0 - time_left) * 55.0
                if time_left <= IMMINENT_EXPIRY_SEC + 3.0:
                    value += 7000
                if pressure["occupied"] >= PRESSURE_RED_OCCUPIED:
                    value += 5000
                tasks.append(
                    self._task(
                        "LIFT_PLATE",
                        area["id"],
                        layout,
                        value=value,
                        order_id=int(order["id"]),
                        dish=order["dish"],
                        area_id=area["id"],
                        deadline_sec=time_left,
                        hard=(f"area:{area['id']}", f"order:{order['id']}"),
                    )
                )
                break
        return tasks

    def _held_component_tasks(
        self,
        cid: int,
        item: dict[str, Any],
        layout: Layout,
        state: dict[str, Any],
        pressure: dict[str, Any],
        busy_targets: set[str],
    ) -> list[Task]:
        key = _component_key(item)
        tasks: list[Task] = []
        commitment = self.chef_commitment.get(cid)
        committed_area = commitment.get("area_id") if commitment else None
        committed_final = tuple(commitment.get("component")) if commitment and commitment.get("component") else None

        if key[1] == "raw" and committed_final and key != committed_final:
            tasks.extend(self._process_tasks_for_component(key[0], committed_final, committed_area, layout, busy_targets))
            if tasks:
                return tasks

        if committed_area:
            plan = self.area_plans.get(committed_area)
            area = layout.station_by_id.get(committed_area)
            if plan and area and self._plan_missing(plan, area, ignore_chef=cid)[key] > 0:
                tasks.append(self._deposit_task(plan, area, key, pressure, layout))
                return tasks

        for plan, area, missing in self._plans_with_missing(layout, ignore_chef=cid, chef_id=cid):
            if missing[key] <= 0:
                continue
            tasks.append(self._deposit_task(plan, area, key, pressure, layout))

        if key[1] == "raw":
            final = _processed_form(key[0])
            if final is not None:
                for plan, area, missing in self._plans_with_missing(layout, ignore_chef=cid, chef_id=cid):
                    if missing[final] <= 0:
                        continue
                    tasks.extend(self._process_tasks_for_component(key[0], final, area["id"], layout, busy_targets))

        if (not tasks and key[0] != "meat") or (not tasks and key[0] == "meat" and key[1] != "raw"):
            stage = self._stage_counter_task(key, layout, pressure, busy_targets)
            if stage:
                tasks.append(stage)
        if not tasks:
            tasks.append(self._task("TRASH", layout.trash_id, layout, value=1, component=key))
        return tasks

    def _deposit_task(
        self,
        plan: dict[str, Any],
        area: dict[str, Any],
        component: tuple[str, str],
        pressure: dict[str, Any],
        layout: Layout,
    ) -> Task:
        value = self._plan_value(plan, pressure) + 850
        hard = (f"area:{area['id']}", f"need:{area['id']}:{component[0]}:{component[1]}")
        return self._task(
            "DEPOSIT_PLATING",
            area["id"],
            layout,
            value=value,
            order_id=plan.get("order_id"),
            dish=plan["dish"],
            component=component,
            area_id=area["id"],
            source=plan.get("source", "active"),
            deadline_sec=self._plan_deadline(plan),
            hard=hard,
        )

    def _process_tasks_for_component(
        self,
        ingredient: str,
        final: tuple[str, str],
        area_id: str | None,
        layout: Layout,
        busy_targets: set[str],
    ) -> list[Task]:
        tasks = []
        if final[1] == "cooked":
            for stove in layout.stoves:
                if stove["id"] in busy_targets or stove.get("cooking") is not None:
                    continue
                tasks.append(
                    self._task(
                        "COOK",
                        stove["id"],
                        layout,
                        value=1750,
                        processing_ticks=COOK_TICKS,
                        component=final,
                        area_id=area_id,
                        hard=(f"stove:{stove['id']}",),
                        commitment={"area_id": area_id, "component": final, "raw_ingredient": ingredient},
                    )
                )
        elif final[1] == "chopped":
            for board in layout.boards:
                if board["id"] in busy_targets or board.get("busy") or board.get("processing") is not None:
                    continue
                tasks.append(
                    self._task(
                        "CHOP",
                        board["id"],
                        layout,
                        value=1450,
                        processing_ticks=CHOP_TICKS,
                        component=final,
                        area_id=area_id,
                        hard=(f"board:{board['id']}",),
                        commitment={"area_id": area_id, "component": final, "raw_ingredient": ingredient},
                    )
                )
        return tasks

    def _stage_counter_task(
        self,
        component: tuple[str, str],
        layout: Layout,
        pressure: dict[str, Any],
        busy_targets: set[str],
    ) -> Task | None:
        # V1 trace attribution showed every completed counter handoff lost time
        # versus producer-carry-through.  V2 therefore only allows counter staging
        # from call sites that can prove a net distance win; none of the structural
        # owner-flow paths use it.
        if pressure["occupied"] >= PRESSURE_RED_OCCUPIED:
            return None
        return None
        candidates = [
            c for c in layout.counters
            if c["id"] in layout.handoff_counter_ids
            and c["id"] not in busy_targets
            and not (c.get("items") or [])
        ]
        if not candidates:
            return None
        counter = candidates[0]
        return self._task(
            "STAGE_COUNTER",
            counter["id"],
            layout,
            value=350,
            component=component,
            counter_id=counter["id"],
            hard=(f"counter:{counter['id']}",),
        )

    def _unstage_counter_tasks(
        self,
        cid: int,
        layout: Layout,
        state: dict[str, Any],
        pressure: dict[str, Any],
        busy_targets: set[str],
    ) -> list[Task]:
        tasks = []
        for counter in layout.counters:
            if counter["id"] in busy_targets:
                continue
            items = counter.get("items") or []
            if len(items) != 1 or _is_plate(items[-1]):
                continue
            key = _component_key(items[-1])
            best_plan = None
            best_missing = None
            for plan, area, missing in self._plans_with_missing(layout, chef_id=cid):
                if missing[key] > 0:
                    score = (0 if plan.get("source") == "active" else 1, self._plan_deadline(plan), area["id"])
                    if best_plan is None or score < best_plan[0]:
                        best_plan = (score, plan, area)
                        best_missing = missing
            if best_plan is None or best_missing is None:
                continue
            _score, plan, area = best_plan
            value = self._plan_value(plan, pressure) + 700
            tasks.append(
                self._task(
                    "UNSTAGE_COUNTER",
                    counter["id"],
                    layout,
                    value=value,
                    order_id=plan.get("order_id"),
                    dish=plan["dish"],
                    component=key,
                    area_id=area["id"],
                    counter_id=counter["id"],
                    source=plan.get("source", "active"),
                    deadline_sec=self._plan_deadline(plan),
                    hard=(f"counter:{counter['id']}", f"need:{area['id']}:{key[0]}:{key[1]}"),
                    commitment={"area_id": area["id"], "component": key, "raw_ingredient": key[0]},
                )
            )
        return tasks

    def _fetch_tasks(
        self,
        cid: int,
        layout: Layout,
        state: dict[str, Any],
        pressure: dict[str, Any],
        busy_targets: set[str],
    ) -> list[Task]:
        tasks = []
        assist_free_chef = cid not in self.chef_area
        for plan, area, missing in self._plans_with_missing(layout, chef_id=cid, allow_assist=assist_free_chef):
            for final in sorted(missing, key=lambda x: (x[1] != "cooked", x[1] != "chopped", x[0])):
                count_missing = missing[final]
                if count_missing <= 0:
                    continue
                ingredient, state_name = final
                if ingredient not in layout.bins_by_ing:
                    continue
                bin_station = layout.bins_by_ing[ingredient]
                if bin_station["id"] in busy_targets:
                    continue
                raw_ingredient = ingredient
                value = self._plan_value(plan, pressure)
                if state_name == "cooked":
                    if not any(s.get("cooking") is None and s["id"] not in busy_targets for s in layout.stoves):
                        value -= 350
                    value += 650
                elif state_name == "chopped":
                    if not any((not b.get("busy")) and b.get("processing") is None and b["id"] not in busy_targets for b in layout.boards):
                        value -= 250
                    value += 450
                else:
                    value += 300
                tasks.append(
                    self._task(
                        "FETCH_RAW",
                        bin_station["id"],
                        layout,
                        value=value,
                        order_id=plan.get("order_id"),
                        dish=plan["dish"],
                        component=final,
                        area_id=area["id"],
                        source=plan.get("source", "active"),
                        deadline_sec=self._plan_deadline(plan),
                        hard=(f"bin:{bin_station['id']}", f"need:{area['id']}:{final[0]}:{final[1]}"),
                        commitment={"area_id": area["id"], "component": final, "raw_ingredient": raw_ingredient},
                    )
                )
        return tasks

    def _park_task(self, chef: dict[str, Any], layout: Layout, busy_targets: set[str]) -> Task | None:
        cpos = tuple(chef["pos"])
        if self._zone(cpos) != "corridor_gap" and not (5 <= cpos[0] <= 11 and 5 <= cpos[1] <= 8):
            return None
        safe = []
        for area in layout.areas:
            if area["id"] in busy_targets or area.get("items"):
                continue
            pos = _pos(area)
            if self._zone(pos) != "corridor_gap":
                safe.append(area)
        if safe:
            target = min(safe, key=lambda a: (_pos(a)[0] > 8, _manhattan(cpos, _pos(a)), a["id"]))
            if _manhattan(cpos, _pos(target)) <= 1:
                return None
            return self._task("PARK", target["id"], layout, value=-50, hard=(f"park:{target['id']}",))
        if layout.trash_id not in busy_targets:
            if _manhattan(cpos, layout.pos_by_id[layout.trash_id]) <= 1:
                return None
            return self._task("PARK", layout.trash_id, layout, value=-75, hard=(f"park:{layout.trash_id}",))
        return None

    def _plans_with_missing(
        self,
        layout: Layout,
        ignore_chef: int | None = None,
        chef_id: int | None = None,
        allow_assist: bool = False,
    ) -> list[tuple[dict[str, Any], dict[str, Any], Counter]]:
        out = []
        for area_id, plan in sorted(self.area_plans.items(), key=lambda kv: (self._plan_deadline(kv[1]), kv[0])):
            area = layout.station_by_id.get(area_id)
            if not area:
                continue
            missing = self._plan_missing(plan, area, ignore_chef=ignore_chef)
            if missing:
                out.append((plan, area, missing))
        return out

    def _plan_missing(
        self,
        plan: dict[str, Any],
        area: dict[str, Any],
        ignore_chef: int | None = None,
    ) -> Counter:
        missing = _recipe_counter(plan["dish"]) - _counter(area.get("items") or [])
        for chef_id, commitment in self.chef_commitment.items():
            if ignore_chef is not None and chef_id == ignore_chef:
                continue
            if commitment.get("area_id") != area["id"]:
                continue
            comp = commitment.get("component")
            if comp:
                key = tuple(comp)
                if missing[key] > 0:
                    missing[key] -= 1
        return Counter({k: v for k, v in missing.items() if v > 0})

    def _plan_deadline(self, plan: dict[str, Any]) -> float:
        if plan.get("order_id") is not None:
            order = self.prev_orders.get(int(plan["order_id"]))
            if order:
                return float(order.get("timeLeft", 999.0) or 999.0)
        if plan.get("source") == "upcoming":
            return float(plan.get("eta", 999.0) or 999.0) + 45.0
        return 9999.0

    def _plan_value(self, plan: dict[str, Any], pressure: dict[str, Any]) -> float:
        source = plan.get("source", "active")
        deadline = self._plan_deadline(plan)
        if source == "active":
            value = 1900 + max(0.0, 60.0 - deadline) * 35.0
            if deadline <= IMMINENT_EXPIRY_SEC:
                value += 5000
            if pressure["occupied"] >= PRESSURE_RED_OCCUPIED:
                value += 1400
            return value
        if source == "upcoming":
            eta = float(plan.get("eta", 999.0) or 999.0)
            return 900 + max(0.0, 35.0 - eta) * 20.0
        return 250

    # ------------------------------------------------------------------
    # Costing, matching, issuing
    # ------------------------------------------------------------------
    def _task_cost(self, chef: dict[str, Any], task: Task, pressure: dict[str, Any], tick: int) -> float:
        cpos = tuple(chef["pos"])
        travel = _manhattan(cpos, task.target_pos) * TICKS_PER_TILE
        role = self.roles.get(int(chef["id"]), "flex")
        role_penalty = self._role_penalty(role, task, pressure)
        if task.area_id:
            plan = self.area_plans.get(task.area_id)
            owner = plan.get("owner_id") if plan else None
            if owner is not None and int(owner) != int(chef["id"]):
                role_penalty += 120
        corridor_penalty = self._corridor_penalty(cpos, task.target_pos, role, task, tick)
        deadline_risk = 0.0
        if task.deadline_sec < 999:
            eta_sec = (travel + task.processing_ticks) / 60.0
            if eta_sec > task.deadline_sec:
                deadline_risk = 12000 + (eta_sec - task.deadline_sec) * 1000
            else:
                deadline_risk = max(0.0, 5.0 - (task.deadline_sec - eta_sec)) * 200
        pressure_bonus = 0.0
        if pressure["occupied"] >= PRESSURE_RED_OCCUPIED and task.kind in {"DELIVER", "LIFT_PLATE"}:
            pressure_bonus = 2500
        return travel + task.processing_ticks + role_penalty + corridor_penalty + deadline_risk - task.value - pressure_bonus

    @staticmethod
    def _role_penalty(role: str, task: Task, pressure: dict[str, Any]) -> float:
        if role == "runner":
            if task.kind in {"DELIVER", "LIFT_PLATE", "UNSTAGE_COUNTER", "DEPOSIT_PLATING", "PARK"}:
                return 0
            return 140
        if role == "cook":
            if task.kind == "COOK" or (task.kind == "FETCH_RAW" and task.component and task.component[1] == "cooked"):
                return 0
            if task.kind in {"DELIVER", "LIFT_PLATE"} and pressure["occupied"] < PRESSURE_RED_OCCUPIED:
                return 180
            return 40
        if role == "chopper":
            if task.kind == "CHOP" or (task.kind == "FETCH_RAW" and task.component and task.component[1] == "chopped"):
                return 0
            if task.kind in {"DELIVER", "LIFT_PLATE"} and pressure["occupied"] < PRESSURE_RED_OCCUPIED:
                return 160
            return 35
        if role == "assembler":
            if task.kind in {"DEPOSIT_PLATING", "LIFT_PLATE", "UNSTAGE_COUNTER", "DELIVER"}:
                return 0
            return 80
        return 0

    def _corridor_penalty(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        role: str,
        task: Task,
        tick: int,
    ) -> float:
        if not self._crosses_corridor(start, end):
            return 45.0 if self._near_hotspot(start) or self._near_hotspot(end) else 0.0
        earliest_start = tick + max(1, _manhattan(start, end) * TICKS_PER_TILE // 2)
        duration = 30
        wait = 0
        for s, e, _cid, _direction in sorted(self.corridor_windows):
            if earliest_start + wait < e and earliest_start + wait + duration > s:
                wait = e - earliest_start
        penalty = min(wait, 60)
        if role != "runner" and task.kind not in {"DELIVER", "LIFT_PLATE"}:
            penalty += 80
        if self._near_hotspot(start) or self._near_hotspot(end):
            penalty += 45
        return float(penalty)

    @staticmethod
    def _crosses_corridor(start: tuple[int, int], end: tuple[int, int]) -> bool:
        if start[0] <= 12 < end[0] or end[0] <= 12 < start[0]:
            return True
        return False

    def _match(self, idle: list[dict[str, Any]], candidates_by_chef: dict[int, list[tuple[float, Task]]]) -> dict[int, Task]:
        chefs = sorted([int(c["id"]) for c in idle])
        best_cost = float("inf")
        best_choice: dict[int, Task] = {}

        def rec(i: int, used: set[str], total: float, chosen: dict[int, Task]) -> None:
            nonlocal best_cost, best_choice
            if i >= len(chefs):
                if total < best_cost:
                    best_cost = total
                    best_choice = dict(chosen)
                return
            cid = chefs[i]
            rows = candidates_by_chef.get(cid) or []
            for cost, task in rows:
                hard = set(task.hard)
                if hard & used:
                    continue
                chosen[cid] = task
                rec(i + 1, used | hard, total + cost, chosen)
                chosen.pop(cid, None)

        rec(0, set(), 0.0, {})
        return best_choice

    def _issue(self, api: Any, chef: dict[str, Any], task: Task, tick: int) -> None:
        cid = int(chef["id"])
        if task.kind == "PARK" and _manhattan(tuple(chef["pos"]), task.target_pos) <= 1:
            return
        result = api.command(cid, task.target_id)
        success = bool(result and result.get("success"))
        if not success:
            self.failed_until[(cid, task.target_id)] = tick + 30
            return

        self.chef_target[cid] = task.target_id
        self.chef_task[cid] = task
        if task.commitment:
            self.chef_commitment[cid] = dict(task.commitment)
        elif task.kind in {"DEPOSIT_PLATING", "TRASH", "STAGE_COUNTER", "DELIVER", "LIFT_PLATE"}:
            self.chef_commitment.pop(cid, None)
        if self._crosses_corridor(tuple(chef["pos"]), task.target_pos):
            mid = tick + max(1, _manhattan(tuple(chef["pos"]), task.target_pos) * TICKS_PER_TILE // 2)
            direction = "right" if task.target_pos[0] > chef["pos"][0] else "left"
            self.corridor_windows.append((mid, mid + 30, cid, direction))
        if task.kind == "DELIVER":
            dist = _manhattan(tuple(chef["pos"]), task.target_pos)
            if dist >= 6 and not chef.get("boostActive") and float(chef.get("boostCooldown", 0.0) or 0.0) <= 0.0:
                api.boost(cid)

    # ------------------------------------------------------------------
    # Failure recovery
    # ------------------------------------------------------------------
    def _recover_blocked_chefs(self, state: dict[str, Any], api: Any, layout: Layout, tick: int) -> None:
        now = float(state.get("time", 0.0) or 0.0)
        dt = max(0.0, now - self.prev_time)
        for chef in state.get("chefs", []) or []:
            cid = int(chef["id"])
            pos = tuple(chef["pos"])
            if chef.get("busy") or not chef.get("hasPath") or self.prev_pos.get(cid) != pos:
                self.stuck_time[cid] = 0.0
                self.prev_pos[cid] = pos
                continue
            self.stuck_time[cid] = self.stuck_time.get(cid, 0.0) + max(dt, 1.0 / 60.0)
            self.prev_pos[cid] = pos
            if self.stuck_time[cid] < 1.5:
                continue
            holding = chef.get("holding")
            if _is_plate(holding):
                tasks = self._deliver_tasks(holding, layout, state, self._stand_pressure(state, layout))
                if tasks:
                    task = min(tasks, key=lambda t: self._task_cost(chef, t, self._stand_pressure(state, layout), tick))
                    self._issue(api, chef, task, tick)
                    self.stuck_time[cid] = 0.0
                continue
            if holding is not None:
                stage = self._stage_counter_task(_component_key(holding), layout, self._stand_pressure(state, layout), self._busy_targets(state))
                if stage:
                    self._issue(api, chef, stage, tick)
                    self.stuck_time[cid] = 0.0
                continue
            if self._zone(pos) == "corridor_gap" or self.roles.get(cid) != "runner":
                park = self._park_task(chef, layout, self._busy_targets(state))
                if park:
                    self._issue(api, chef, park, tick)
                    self.stuck_time[cid] = 0.0
