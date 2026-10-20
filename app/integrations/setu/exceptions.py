"""Safe, provider-specific errors for the Setu integration."""

import re


class SetuError(Exception):
    """Base class for controlled Setu integration errors."""


class SetuConfigurationError(SetuError):
    """Required Setu configuration is missing or invalid."""


class SetuTimeoutError(SetuError):
    """A Setu request timed out."""


class SetuTransportError(SetuError):
    """A Setu request could not be completed."""


class SetuAPIError(SetuError):
    """Setu returned an unsuccessful HTTP response."""

    _SAFE_ERROR_CODES = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")
    _SAFE_REQUEST_IDS = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
    _SAFE_MESSAGES = {
        "Invalid request body",
        "Invalid response from upstream",
        "An internal server error has occurred, please try again in some time.",
    }

    def __init__(
        self,
        status_code: int,
        *,
        error_code: str | None = None,
        message: str | None = None,
        request_id: str | None = None,
    ):
        self.status_code = status_code
        self.error_code = (
            error_code
            if isinstance(error_code, str)
            and self._SAFE_ERROR_CODES.fullmatch(error_code)
            else None
        )
        self.message = (
            message
            if isinstance(message, str) and message in self._SAFE_MESSAGES
            else None
        )
        self.request_id = (
            request_id
            if isinstance(request_id, str)
            and self._SAFE_REQUEST_IDS.fullmatch(request_id)
            else None
        )
        details = [f"Setu API request failed with HTTP {status_code}."]
        if self.error_code:
            details.append(f"error_code={self.error_code}")
        if self.message:
            details.append(f"message={self.message}")
        if self.request_id:
            details.append(f"request_id={self.request_id}")
        super().__init__(" ".join(details))


class SetuMalformedResponseError(SetuError):
    """Setu returned a response that is not a JSON object."""


class SetuDataSessionPollingTimeoutError(SetuError):
    """Setu data session did not reach a retrievable state within the polling bound."""


class SetuDataSessionTerminalError(SetuError):
    """Setu data session reached a terminal non-success state."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(f"Setu data session ended with status {status}.")


class SetuTestDatabaseError(SetuError):
    """A Setu database write was attempted outside the TEST database."""
