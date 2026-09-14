import asyncio
import os
import tempfile
from datetime import date
from pathlib import Path

import pytest

from backend.accommodation.models import AccommodationSearchRequest
from backend.accommodation.service import AccommodationService
from backend.models import Location
from crawler.accommodation.booking import BookingComSource


@pytest.mark.integration
@pytest.mark.official
def test_booking_live_seoul_busan_gangneung() -> None:
    if os.getenv("KTO_RUN_LIVE_ACCOMMODATION") != "1":
        pytest.skip("set KTO_RUN_LIVE_ACCOMMODATION=1 to run the live public-page check")

    async def run() -> list[tuple[str, bool, int, int]]:
        with tempfile.TemporaryDirectory(prefix="kto-accommodation-live-") as directory:
            source = BookingComSource(
                executable_path=os.getenv("KTO_ACCOMMODATION_BROWSER_EXECUTABLE_PATH"),
                headless=False,
                request_interval_s=1.5,
            )
            service = AccommodationService(
                database_path=Path(directory) / "accommodation.db",
                sources=[source],
                source_timeout_s=90,
            )
            try:
                values: list[tuple[str, bool, int, int]] = []
                for label, lat, lng in (
                    ("Seoul", 37.5665, 126.9780),
                    ("Busan", 35.1796, 129.0756),
                    ("Gangneung", 37.7519, 128.8761),
                ):
                    response = await service.search(
                        AccommodationSearchRequest(
                            destination=Location(lat=lat, lng=lng, label=label),
                            checkin=date(2026, 10, 1),
                            checkout=date(2026, 10, 2),
                            adults=2,
                            children=0,
                        )
                    )
                    priced = sum(
                        1
                        for result in response.results
                        for offer in result.offers
                        if offer.final_price_krw is not None
                    )
                    values.append((label, response.complete, len(response.results), priced))
                return values
            finally:
                await service.close()

    values = asyncio.run(run())
    assert all(complete and result_count > 0 and priced > 0 for _, complete, result_count, priced in values)
