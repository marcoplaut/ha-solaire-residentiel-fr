"""Client for the solar provider's customer portal.

This package has no Home Assistant dependency so that it can be extracted to a
standalone library later.
"""

from .cleaning import EnergyLimits, async_get_hourly_energy
from .client import EdfEnrClient
from .exceptions import (
    EdfEnrApiError,
    EdfEnrAuthError,
    EdfEnrConnectionError,
    EdfEnrError,
    EdfEnrNotFoundError,
    EdfEnrRateLimitError,
)
from .models import Device, EnergyReading, Equipment, Granularity, Installation, Project

__all__ = [
    "Device",
    "EdfEnrApiError",
    "EdfEnrAuthError",
    "EdfEnrClient",
    "EdfEnrConnectionError",
    "EdfEnrError",
    "EdfEnrNotFoundError",
    "EdfEnrRateLimitError",
    "EnergyLimits",
    "EnergyReading",
    "Equipment",
    "Granularity",
    "Installation",
    "Project",
    "async_get_hourly_energy",
]
