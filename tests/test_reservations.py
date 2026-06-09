import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.reservations import ReservationManager


def test_reservation_conflict_and_release():
    mgr = ReservationManager()
    first, conflicts = mgr.reserve(
        owner="executor",
        kind="station",
        resource_id="stove_0",
        created_tick=10,
        ttl_ticks=60,
        chef_id=0,
    )
    assert conflicts == []
    second, conflicts = mgr.reserve(
        owner="executor",
        kind="station",
        resource_id="stove_0",
        created_tick=11,
        ttl_ticks=60,
        chef_id=1,
    )
    assert [c.id for c in conflicts] == [first.id]
    mgr.release(first.id, "arrival")
    assert first.status == "released"
    assert second.status == "active"


def test_reservation_expiry():
    mgr = ReservationManager()
    res, _ = mgr.reserve(
        owner="executor",
        kind="chef",
        resource_id="0",
        created_tick=5,
        ttl_ticks=10,
        chef_id=0,
    )
    assert mgr.expire(14) == []
    expired = mgr.expire(15)
    assert [r.id for r in expired] == [res.id]
    assert res.status == "expired"

