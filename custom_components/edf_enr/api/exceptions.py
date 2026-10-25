"""Exceptions raised by the customer portal API client."""

from __future__ import annotations


class EdfEnrError(Exception):
    """Base class for all client errors."""


class EdfEnrConnectionError(EdfEnrError):
    """The portal could not be reached or answered with a server error."""


class EdfEnrAuthError(EdfEnrError):
    """The credentials were rejected."""


class EdfEnrRateLimitError(EdfEnrConnectionError):
    """The portal asked us to slow down (HTTP 429)."""

    def __init__(self, retry_after: float | None = None) -> None:
        """Store the delay suggested by the server, if any."""
        super().__init__("Rate limited by the customer portal")
        self.retry_after = retry_after


class EdfEnrApiError(EdfEnrError):
    """The portal answered with something we do not understand."""


class EdfEnrNotFoundError(EdfEnrApiError):
    """The requested resource does not exist or is not visible to this account."""
