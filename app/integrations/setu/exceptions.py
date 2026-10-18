"""Safe, provider-specific errors for the Setu integration."""


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

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Setu API request failed with HTTP {status_code}.")


class SetuMalformedResponseError(SetuError):
    """Setu returned a response that is not a JSON object."""
