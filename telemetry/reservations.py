"""Deterministic reservation model used by tests and sim instrumentation."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


EXCLUSIVE_KINDS = {"chef", "station", "counter", "plate", "stand", "order"}
SOFT_KINDS = {"approach_tile"}


@dataclass
class Reservation:
    id: str
    owner: str
    chef_id: int | None
    kind: str
    resource_id: str
    order_id: int | None
    purpose: str
    created_tick: int
    expires_tick: int
    status: str = "active"
    release_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReservationManager:
    def __init__(self):
        self._reservations: dict[str, Reservation] = {}
        self._next_id = 1

    def _new_id(self) -> str:
        rid = f"r{self._next_id}"
        self._next_id += 1
        return rid

    def active(self) -> list[Reservation]:
        return [r for r in self._reservations.values() if r.status == "active"]

    def conflicts_for(self, kind: str, resource_id: str, ignore_id: str | None = None) -> list[Reservation]:
        conflicts = []
        for reservation in self.active():
            if ignore_id and reservation.id == ignore_id:
                continue
            if reservation.kind == kind and reservation.resource_id == resource_id:
                conflicts.append(reservation)
        return conflicts

    def can_reserve(self, kind: str, resource_id: str) -> bool:
        if kind in SOFT_KINDS:
            return True
        return len(self.conflicts_for(kind, resource_id)) == 0

    def reserve(
        self,
        *,
        owner: str,
        kind: str,
        resource_id: str,
        created_tick: int,
        ttl_ticks: int,
        chef_id: int | None = None,
        order_id: int | None = None,
        purpose: str = "",
    ) -> tuple[Reservation, list[Reservation]]:
        conflicts = self.conflicts_for(kind, resource_id)
        reservation = Reservation(
            id=self._new_id(),
            owner=owner,
            chef_id=chef_id,
            kind=kind,
            resource_id=resource_id,
            order_id=order_id,
            purpose=purpose,
            created_tick=int(created_tick),
            expires_tick=int(created_tick + max(1, ttl_ticks)),
        )
        self._reservations[reservation.id] = reservation
        return reservation, conflicts

    def release(self, reservation_id: str, reason: str = "release") -> Reservation | None:
        reservation = self._reservations.get(reservation_id)
        if reservation and reservation.status == "active":
            reservation.status = "released"
            reservation.release_reason = reason
        return reservation

    def violate(self, reservation_id: str, reason: str = "violation") -> Reservation | None:
        reservation = self._reservations.get(reservation_id)
        if reservation and reservation.status == "active":
            reservation.status = "violated"
            reservation.release_reason = reason
        return reservation

    def expire(self, current_tick: int) -> list[Reservation]:
        expired = []
        for reservation in self.active():
            if reservation.expires_tick <= current_tick:
                reservation.status = "expired"
                reservation.release_reason = "ttl"
                expired.append(reservation)
        return expired

    def snapshot(self) -> dict[str, Any]:
        active = [r.to_dict() for r in self.active()]
        return {
            "active": active,
            "active_count": len(active),
            "all": [r.to_dict() for r in self._reservations.values()],
        }

