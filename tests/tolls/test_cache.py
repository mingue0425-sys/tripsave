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
    assert fresh.lookup.route_label == "서울~부산"
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


def test_cache_ignores_results_from_an_old_parser_version(tmp_path) -> None:
    cache = TollRateCache(tmp_path / "tolls.db", ttl_days=30)
    cache.put("서울", "부산", lookup())

    import sqlite3

    connection = sqlite3.connect(tmp_path / "tolls.db")
    connection.execute(
        "UPDATE toll_rates_cache SET parser_version = ?", ("old-parser",)
    )
    connection.commit()
    connection.close()

    assert cache.get("서울", "부산") is None
    assert cache.get("서울", "부산", allow_stale=True) is None


def test_cache_uses_official_ids_as_identity_and_name_index_for_first_read(tmp_path) -> None:
    cache = TollRateCache(tmp_path / "tolls.db", ttl_days=30)
    value = lookup().model_copy(
        update={"entry_official_id": "101", "exit_official_id": "140"}
    )
    cache.put("서울", "부산", value)

    by_ids = cache.get(
        "서울", "부산", entry_official_id="101", exit_official_id="140"
    )
    by_names_before_station_resolution = cache.get("서울", "부산")
    import json
    import sqlite3

    connection = sqlite3.connect(tmp_path / "tolls.db")
    connection.execute(
        """
        INSERT INTO official_stations(
            official_id, official_name, normalized_name, aliases_json,
            verified_at, source_url
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "140",
            "부산",
            "부산",
            json.dumps(["부산", "부산 톨게이트 하이패스"], ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
            "https://www.ex.co.kr/portal/usefee/selectUseFeeNList.do",
        ),
    )
    connection.execute(
        """
        INSERT INTO official_stations(
            official_id, official_name, normalized_name, aliases_json,
            verified_at, source_url
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "101",
            "서울",
            "서울",
            json.dumps(["서울"], ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
            "https://www.ex.co.kr/portal/usefee/selectUseFeeNList.do",
        ),
    )
    connection.commit()
    connection.close()
    by_observed_alias = cache.get("서울", "부산 톨게이트 하이패스")
    wrong_direction = cache.get(
        "부산", "서울", entry_official_id="140", exit_official_id="101"
    )

    assert by_ids is not None and by_ids.lookup.entry_official_id == "101"
    assert by_names_before_station_resolution is not None
    assert by_observed_alias is not None and by_observed_alias.lookup.exit_official_id == "140"
    assert wrong_direction is None
