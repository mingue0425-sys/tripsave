"""Stable API errors for the V0.5 fuel endpoints."""


class FuelServiceError(RuntimeError):
    code = "FUEL_CALCULATION_ERROR"
    http_status = 502
    default_message = "The fuel calculation could not be completed."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class InvalidFuelRequestError(FuelServiceError):
    code = "INVALID_REQUEST"
    http_status = 422
    default_message = "The fuel request is not bound to a valid route."
