"""Stable failures for the local toll service API."""


class TollServiceError(RuntimeError):
    code = "TOLL_CALCULATION_ERROR"
    http_status = 502
    default_message = "The local toll calculation could not be completed."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class InvalidTollRequestError(TollServiceError):
    code = "INVALID_REQUEST"
    http_status = 422
    default_message = "The toll request is not bound to a valid route."


class TollIndexServiceUnavailableError(TollServiceError):
    code = "TOLL_INDEX_UNAVAILABLE"
    http_status = 503
    default_message = "Local OSM toll index is unavailable. Run the toll index setup procedure."
