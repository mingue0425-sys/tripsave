"""Typed failures for public place source access and parsing."""

from __future__ import annotations


class PlaceSourceError(RuntimeError):
    """An expected source failure safe to expose as a diagnostic code."""

    code = "SOURCE_ERROR"
    public_message = "The place source could not be read."
    retriable = True

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        public_message: str | None = None,
        retriable: bool | None = None,
    ) -> None:
        super().__init__(message or public_message or self.public_message)
        if code is not None:
            self.code = code
        if public_message is not None:
            self.public_message = public_message
        if retriable is not None:
            self.retriable = retriable


class SourceTimeoutError(PlaceSourceError):
    code = "SOURCE_TIMEOUT"
    public_message = "The public place page timed out."
    retriable = True


class SourceHTTPError(PlaceSourceError):
    code = "SOURCE_HTTP_ERROR"
    public_message = "The public place page returned an HTTP error."
    retriable = True

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        if status_code in {401, 403}:
            code = "ACCESS_DENIED"
            public_message = "The public place source denied access."
        else:
            code = "SOURCE_HTTP_ERROR"
            public_message = f"The public place page returned HTTP {status_code}."
        super().__init__(
            f"Place source returned HTTP {status_code}.",
            code=code,
            public_message=public_message,
            retriable=status_code in {408, 425, 429} or status_code >= 500,
        )


class SourcePageChangedError(PlaceSourceError):
    code = "PARSER_ERROR"
    public_message = "The public place page structure changed."
    retriable = False


class SourceParseError(SourcePageChangedError):
    """A page was readable but a required semantic field could not be parsed."""
