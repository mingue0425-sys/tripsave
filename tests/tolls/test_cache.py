from datetime import datetime, timedelta, timezone

from backend.tolls.cache import TollRateCache
from backend.tolls.models import TollVehicleClass
from backend.tolls.official import OfficialTollLookup


def lookup() -> OfficialTollLookup:
    return OfficialTollLookup(
        entry_name="서울",
        exit_name="부산",
        route_label="서울~부산",
        distance_km=385.8,
        prices={
            TollVehicleClass.CLASS_1: 18_600,
            TollVehicleClass.CLASS_2: 19_000,
            TollVehicleClass.CLASS_3: 19_700,
            TollVehicleClass.CLASS_4: 26_100,
            TollVehicleClass.CLASS_5: 30_700,
            TollVehicleClass.COMPACT: 9_300,
        },
        source_url="https://www.ex.co.kr/portal/usefee/selectUseFeeNList.do",
        fetched_at=datetime.now(timezone.utc),
        raw_evidence_hash="1" * 64,
    )


def test_cache_preserves_direction_and_vehicle_prices(tmp_path) -> None:
    cache = TollRateCache(tmp_path / "tolls.db", ttl_days=30)
    cache.put("서울", "부산", lookup())

    fresh = cache.get("서울", "부산")
    reverse = cache.get("부산", "서울")

    assert fresh is not None and fresh.fresh
    assert fresh.lookup.prices[TollVehicleClass.COMPACT] == 9_300
    assert reverse is None


def test_cache_can_return_expired_entry_as_stale(tmp_path) -> None:
    cache = TollRateCache(tmp_path / "tolls.db", ttl_days=1)
    value = lookup().model_copy(
        update={"fetched_at": datetime.now(timezone.utc) - timedelta(days=2)}
    )
    cache.put("서울", "부산", value)

    # Expiry is calculated when the value is written, so make the row old in
    # the fixture database to exercise the stale path deterministically.
    import sqlite3

    connection = sqlite3.connect(tmp_path / "tolls.db")
    connection.execute(
        "UPDATE toll_rates_cache SET expires_at = ?",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
    )
    connection.commit()
    connection.close()

    assert cache.get("서울", "부산") is None
    stale = cache.get("서울", "부산", allow_stale=True)
    assert stale is not None and not stale.fresh
