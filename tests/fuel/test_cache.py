import sqlite3
from datetime import datetime, timedelta, timezone

from backend.fuel.cache import FuelPriceCache
from backend.fuel.models import FuelType

from .helpers import make_price


def test_cache_round_trips_fresh_verified_price(tmp_path) -> None:
    cache = FuelPriceCache(tmp_path / "fuel.db", ttl_s=3_600)
    cache.put(make_price(FuelType.DIESEL, price=1_844.02))

    entry = cache.get(FuelType.DIESEL)

    assert entry is not None
    assert entry.fresh is True
    assert entry.result.cache_hit is True
    assert entry.result.price_krw_per_l == 1844.02
    assert entry.result.source_status == "fresh"


def test_cache_expiry_is_explicit_and_stale_is_not_default(tmp_path) -> None:
    database = tmp_path / "fuel.db"
    cache = FuelPriceCache(database, ttl_s=3_600)
    cache.put(make_price())
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE fuel_prices_cache SET expires_at = ?",
        ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
    )
    connection.commit()
    connection.close()

    assert cache.get(FuelType.GASOLINE) is None
    stale = cache.get(FuelType.GASOLINE, allow_stale=True)
    assert stale is not None
    assert stale.fresh is False
    assert stale.result.source_status == "stale"


def test_cache_does_not_read_an_old_parser_version(tmp_path) -> None:
    database = tmp_path / "fuel.db"
    cache = FuelPriceCache(database, ttl_s=3_600)
    cache.put(make_price())
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE fuel_prices_cache SET parser_version = ?", ("old-parser",)
    )
    connection.commit()
    connection.close()

    assert cache.get(FuelType.GASOLINE) is None
