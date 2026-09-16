"""OSRM matrix orchestration kept outside the pure optimizer."""

from __future__ import annotations

import asyncio

from backend.async_lock import LoopLocalAsyncLock
from backend.models import Location
from backend.routing.errors import RoutingError
from backend.routing.osrm import OSRMClient

from .models import MatrixEntry, OptimizedRoute, OptimizeRouteRequest
from .optimizer import optimize_matrix


class ItineraryServiceUnavailable(RuntimeError):
    """The local routing matrix could not be fetched."""

    def __init__(self, code: str = "ROUTING_ENGINE_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


class ItineraryService:
    version = "v1.1.0"

    def __init__(self, routing_client: OSRMClient) -> None:
        self.routing_client = routing_client
        self._table_lock = LoopLocalAsyncLock()

    @staticmethod
    def _locations(request: OptimizeRouteRequest) -> list[Location]:
        return [
            Location(lat=point.lat, lng=point.lng, label=point.name, source="itinerary")
            for point in [request.origin, *request.waypoints, request.destination]
        ]

    async def _fetch_matrix(self, request: OptimizeRouteRequest) -> list[MatrixEntry]:
        if request.matrix is not None:
            return list(request.matrix)
        try:
            # OSRMClient exposes the duration companion matrix for backwards
            # compatibility; serialize the short read so concurrent requests
            # cannot observe another request's companion matrix.
            async with self._table_lock:
                values = await self.routing_client.table(self._locations(request))
                durations = [row[:] for row in self.routing_client.last_table_durations]
        except RoutingError as error:
            raise ItineraryServiceUnavailable(error.code) from error
        entries: list[MatrixEntry] = []
        points = [request.origin, *request.waypoints, request.destination]
        for row_index, row in enumerate(values):
            for column_index, value in enumerate(row):
                if row_index == column_index or value is None:
                    continue
                duration = durations[row_index][column_index]
                if duration is None:
                    continue
                entries.append(
                    MatrixEntry(
                        from_id=points[row_index].id,
                        to_id=points[column_index].id,
                        distance_m=value,
                        duration_s=duration,
                        reason="OSRM table does not include toll/fuel prices",
                    )
                )
        return entries

    async def optimize(self, request: OptimizeRouteRequest) -> OptimizedRoute:
        entries = await self._fetch_matrix(request)
        result = await asyncio.to_thread(optimize_matrix, request, entries)
        if not request.include_geometry or not result.segments:
            return result.model_copy(update={"optimizer_version": self.version})

        point_by_id = {
            point.id: point
            for point in [request.origin, *request.waypoints, request.destination]
        }
        async def fetch_geometry(segment):
            start = point_by_id[segment.from_id]
            end = point_by_id[segment.to_id]
            return await self.routing_client.route(
                Location(lat=start.lat, lng=start.lng, label=start.name, source="itinerary"),
                Location(lat=end.lat, lng=end.lng, label=end.name, source="itinerary"),
            )

        try:
            routes = await asyncio.gather(*(fetch_geometry(segment) for segment in result.segments))
        except RoutingError:
            return result.model_copy(
                update={
                    "warnings": [*result.warnings, "ROUTE_GEOMETRY_UNAVAILABLE"],
                    "optimizer_version": self.version,
                }
            )
        segments = [
            segment.model_copy(update={"geometry": route.geometry})
            for segment, route in zip(result.segments, routes, strict=True)
        ]
        from .optimizer import _build_geometry

        return result.model_copy(
            update={
                "segments": segments,
                "geometry": _build_geometry(segments),
                "optimizer_version": self.version,
            }
        )
