import asyncio
from datetime import datetime, timezone

from backend.models import Location
from backend.routing.models import RouteGeometry, RouteResult
from backend.tolls.cache import TollRateCache
from backend.tolls.models import (
    MatchedTollGate,
    MatchedTollRoad,
    TollAnalysis,
    TollCalculationRequest,
    TollGate,
    TollVehicleClass,
    TollStage,
)
from backend.tolls.official import OfficialTollLookup, OfficialTollStationNotFoundError
from backend.tolls.service import TollCalculator


class FakeIndex:
    def __init__(self, analysis: TollAnalysis) -> None:
        self.analysis = analysis

    @property
    def ready(self) -> bool:
        return True

    def analyze_route(self, coordinates):
        return self.analysis


class FakeCrawler:
    def __init__(self, lookup: OfficialTollLookup) -> None:
        self.lookup_value = lookup
        self.calls = 0

    async def lookup(self, entry_name: str, exit_name: str) -> OfficialTollLookup:
        self.calls += 1
        return self.lookup_value


class DirectionalFakeCrawler(FakeCrawler):
    async def lookup(self, entry_name: str, exit_name: str) -> OfficialTollLookup:
        self.calls += 1
        if entry_name == "하남":
            raise OfficialTollStationNotFoundError("하남 is not a valid directional entry")
        return self.lookup_value.model_copy(
            update={
                "entry_name": entry_name,
                "exit_name": exit_name,
                "route_label": f"{entry_name}~{exit_name}",
            }
        )


def request() -> TollCalculationRequest:
    return TollCalculationRequest(
        origin=Location(lat=36.0, lng=127.0),
        destination=Location(lat=36.0, lng=127.1),
        route=RouteResult(
            distance_m=10_000.0,
            duration_s=600.0,
            geometry=RouteGeometry(
                type="LineString",
                coordinates=[[127.0, 36.0], [127.1, 36.0]],
            ),
        ),
        vehicle_class=TollVehicleClass.COMPACT,
    )


def gate(osm_id: int, name: str, lng: float) -> MatchedTollGate:
    value = TollGate(
        id=f"osm-node-{osm_id}",
        osm_type="node",
        osm_id=osm_id,
        name=name,
        normalized_name=name,
        lat=36.0,
        lng=lng,
        operator="한국도로공사",
        gate_type="toll_booth",
    )
    return MatchedTollGate(
        gate=value,
        distance_to_route_m=5.0,
        position_along_route_m=(lng - 127.0) * 90_000,
        confidence="high",
    )


def lookup() -> OfficialTollLookup:
    return OfficialTollLookup(
        entry_name="서울",
        exit_name="부산",
        route_label="서울~부산",
        distance_km=10.0,
        prices={
            "class_1": 1_000,
            "class_2": 1_100,
            "class_3": 1_200,
            "class_4": 1_300,
            "class_5": 1_400,
            "compact": 500,
        },
        source_url="https://www.ex.co.kr/portal/usefee/selectUseFeeNList.do",
        fetched_at=datetime.now(timezone.utc),
        raw_evidence_hash="2" * 64,
    )


def supported_analysis() -> TollAnalysis:
    return TollAnalysis(
        toll_road_detected=True,
        toll_roads=[
            MatchedTollRoad(
                osm_id=99,
                name="고속도로",
                ref="1",
                operator="한국도로공사",
                distance_to_route_m=5.0,
                position_along_route_m=500.0,
                confidence="high",
            )
        ],
        gates=[gate(1, "서울", 127.02), gate(2, "부산", 127.08)],
    )


def test_service_uses_authoritative_pair_total_and_cache(tmp_path) -> None:
    crawler = FakeCrawler(lookup())
    calculator = TollCalculator(
        index_path=tmp_path / "unused.db",
        cache=TollRateCache(tmp_path / "cache.db", ttl_days=30),
        crawler=crawler,
    )
    calculator.index = FakeIndex(supported_analysis())

    first = asyncio.run(calculator.calculate(request()))
    second = asyncio.run(calculator.calculate(request()))

    assert first.status == "ok"
    assert first.toll.complete is True
    assert first.toll.total_toll_krw == 500
    assert len(first.toll.journeys) == 1
    assert second.toll.total_toll_krw == 500
    assert crawler.calls == 1
    assert first.toll.diagnostics.stage is TollStage.TOLL_COMPLETE
    assert first.toll.diagnostics.failure_stage is None
    assert first.toll.diagnostics.raw_candidates == 2
    assert first.toll.diagnostics.logical_gates == 2
    assert [gate.candidate_role for gate in first.toll.detected_toll_gates] == [
        "entry",
        "exit",
    ]


def test_service_marks_unknown_operator_partial_instead_of_free(tmp_path) -> None:
    analysis = supported_analysis().model_copy(
        update={"unknown_toll_operator": True}
    )
    calculator = TollCalculator(index_path=tmp_path / "unused.db")
    calculator.index = FakeIndex(analysis)

    result = asyncio.run(calculator.calculate(request()))
    assert result.status == "partial"
    assert result.toll.complete is False
    assert result.toll.total_toll_krw is None
    assert result.toll.reason == "unknown_toll_operator"
    assert result.toll.diagnostics.failure_code == "PRIVATE_SEGMENT_UNRESOLVED"
    assert result.toll.diagnostics.failure_stage == TollStage.ENTRY_EXIT_RESOLUTION_OK


def test_service_allows_mixed_corridor_only_after_official_result(tmp_path) -> None:
    analysis = supported_analysis().model_copy(
        update={
            "supported_operator_evidence": True,
            "unsupported_private_road": True,
        }
    )
    crawler = FakeCrawler(lookup())
    calculator = TollCalculator(
        index_path=tmp_path / "unused.db",
        cache=TollRateCache(tmp_path / "cache.db", ttl_days=30),
        crawler=crawler,
    )
    calculator.index = FakeIndex(analysis)

    result = asyncio.run(calculator.calculate(request()))

    assert result.status == "ok"
    assert result.toll.complete is True
    assert result.toll.total_toll_krw == 500
    assert any("mixed operator evidence" in note for note in result.toll.diagnostics.notes)


def test_service_tries_next_official_directional_pair(tmp_path) -> None:
    analysis = supported_analysis().model_copy(
        update={
            "gates": [gate(1, "하남", 127.02), gate(2, "동서울", 127.05), gate(3, "대동", 127.08)]
        }
    )
    crawler = DirectionalFakeCrawler(lookup())
    calculator = TollCalculator(
        index_path=tmp_path / "unused.db",
        cache=TollRateCache(tmp_path / "cache.db", ttl_days=30),
        crawler=crawler,
    )
    calculator.index = FakeIndex(analysis)

    result = asyncio.run(calculator.calculate(request()))

    assert result.status == "ok"
    assert result.toll.complete is True
    assert crawler.calls == 2
    assert result.toll.journeys[0].entry.name == "동서울"
    assert result.toll.journeys[0].exit.name == "대동"
    assert any("official path validation selected 동서울 -> 대동" in note for note in result.toll.diagnostics.notes)


def test_service_does_not_infer_free_from_empty_sparse_index(tmp_path) -> None:
    calculator = TollCalculator(index_path=tmp_path / "unused.db")
    calculator.index = FakeIndex(TollAnalysis(toll_road_detected=False))

    result = asyncio.run(calculator.calculate(request()))
    assert result.status == "partial"
    assert result.toll.complete is False
    assert result.toll.total_toll_krw is None
    assert result.toll.reason == "toll_status_not_proven"
