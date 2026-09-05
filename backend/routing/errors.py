"""Typed failures returned by the local routing client."""


class RoutingError(Exception):
    """Base error with a stable API code and HTTP status."""

    code = "INTERNAL_ERROR"
    http_status = 502
    default_message = "The local routing engine returned an invalid response."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class InvalidRouteInputError(RoutingError):
    code = "INVALID_REQUEST"
    http_status = 422
    default_message = "선택한 위치가 자동차 도로에서 너무 멀리 떨어져 있습니다."


class NoRouteError(RoutingError):
    code = "ROUTE_NOT_FOUND"
    http_status = 404
    default_message = "선택한 위치 사이에서 자동차 경로를 찾을 수 없습니다."


class NoSegmentError(RoutingError):
    code = "NO_SEGMENT"
    http_status = 422
    default_message = "선택한 위치를 자동차 도로에 연결할 수 없습니다."


class RoutingEngineUnavailableError(RoutingError):
    code = "ROUTING_ENGINE_UNAVAILABLE"
    http_status = 503
    default_message = "로컬 경로 엔진을 사용할 수 없습니다. OSRM을 실행하세요."


class RoutingTimeoutError(RoutingError):
    code = "ROUTING_TIMEOUT"
    http_status = 504
    default_message = "로컬 경로 엔진의 응답 시간이 초과되었습니다."


class InvalidRouteResponseError(RoutingError):
    code = "INTERNAL_ERROR"
    http_status = 502
    default_message = "로컬 경로 엔진이 잘못된 경로 데이터를 반환했습니다."
