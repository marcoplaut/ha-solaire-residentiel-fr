"""Data models returned by the customer portal API client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from .const import API_TIMEZONE
from .exceptions import EdfEnrApiError


class Granularity(StrEnum):
    """Bucket size of the telemetry endpoint."""

    MINUTELY = "minutely"
    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"
    YEARLY = "yearly"

    @property
    def unit(self) -> str:
        """Return the unit used in the begin-/end- query parameters."""
        return _UNITS[self]

    @property
    def response_key(self) -> str:
        """Return the key holding the bucket list in the response."""
        return f"{self.value}ElecEnergies"


_UNITS = {
    Granularity.MINUTELY: "minute",
    Granularity.HOURLY: "hour",
    Granularity.DAILY: "day",
    Granularity.MONTHLY: "month",
    Granularity.YEARLY: "year",
}


@dataclass(frozen=True, slots=True)
class TokenSet:
    """OAuth tokens of a session."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: datetime
    refresh_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class Project:
    """A solar installation project (CRM "opportunity") of the account."""

    pv_number: str
    name: str | None
    installed_power_kwp: float | None
    commissioning_date: datetime | None
    offer_name: str | None


@dataclass(frozen=True, slots=True)
class Installation:
    """Installation record of a project."""

    installation_id: str
    pv_number: str


@dataclass(frozen=True, slots=True)
class Equipment:
    """Equipment attached to an installation (gateway, water heater, ...)."""

    equipment_id: str
    type: str
    sub_type: str | None
    brand: str | None
    last_connection: datetime | None


@dataclass(frozen=True, slots=True)
class Device:
    """Monitoring device status."""

    device_id: str
    type: str | None
    status: str | None
    last_reported_at: datetime | None


@dataclass(frozen=True, slots=True)
class EnergyReading:
    """Energy (kWh) measured during one bucket.

    ``None`` means the value is unknown (not reported yet, or rejected because
    the portal returned a negative placeholder).
    """

    start: datetime
    granularity: Granularity
    solar_production: float | None
    self_consumption: float | None
    grid_import: float | None
    ecs_consumption: float | None

    @property
    def grid_injection(self) -> float | None:
        """Energy exported to the grid."""
        if self.solar_production is None or self.self_consumption is None:
            return None
        return max(self.solar_production - self.self_consumption, 0.0)

    @property
    def home_consumption(self) -> float | None:
        """Energy consumed by the home (solar self consumption + grid import)."""
        if self.self_consumption is None or self.grid_import is None:
            return None
        return self.self_consumption + self.grid_import


# The customer area web app reads exactly this value as 0 (other negative
# values are placeholders for unknown measurements).
ZERO_SENTINEL: Final = -0.1


def parse_api_datetime(value: str) -> datetime:
    """Convert a telemetry timestamp (naive Paris time with a bogus "Z") to UTC."""
    try:
        naive = datetime.fromisoformat(value.removesuffix("Z"))
        return naive.replace(tzinfo=API_TIMEZONE).astimezone(UTC)
    except (ValueError, OverflowError) as err:
        raise EdfEnrApiError(f"Invalid timestamp {value!r}") from err


def format_api_datetime(value: datetime) -> str:
    """Convert an aware datetime to the naive Paris time the API expects."""
    if value.tzinfo is None:
        raise ValueError("Datetime must be timezone aware")
    return value.astimezone(API_TIMEZONE).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: Any) -> datetime | None:
    """Parse a real UTC ISO timestamp (CRM and installation endpoints)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError, OverflowError:
        # Also out-of-range sentinels such as "0001-01-01T00:00:00+01:00".
        return None


def parse_epoch(value: Any) -> datetime | None:
    """Parse an epoch timestamp in seconds."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except ValueError, OverflowError, OSError:
        # Out of range, e.g. a value in milliseconds.
        return None


def _energy(value: Any) -> float | None:
    """Return a kWh value, or None for missing or negative placeholders."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value == ZERO_SENTINEL:
        return 0.0
    if value < 0:
        return None
    return round(float(value), 3)


def parse_energy_reading(item: Any, granularity: Granularity) -> EnergyReading:
    """Parse one bucket of the telemetry endpoint."""
    if not isinstance(item, dict) or not isinstance(item.get("dateStart"), str):
        raise EdfEnrApiError("Telemetry bucket without dateStart")
    energies = item.get("energies")
    if not isinstance(energies, dict):
        energies = {}
    return EnergyReading(
        start=parse_api_datetime(item["dateStart"]),
        granularity=granularity,
        solar_production=_energy(energies.get("solarProduction")),
        self_consumption=_energy(energies.get("autoConsumption")),
        grid_import=_energy(energies.get("networkConsumption")),
        ecs_consumption=_energy(energies.get("ecsConsumption")),
    )


def parse_project(item: Any) -> Project | None:
    """Parse a CRM opportunity; return None when it is not a usable project."""
    if not isinstance(item, dict):
        return None
    pv_number = item.get("pvNumber")
    if not isinstance(pv_number, str) or not pv_number:
        return None
    raw_offer = item.get("offer")
    offer: dict[str, Any] = raw_offer if isinstance(raw_offer, dict) else {}
    power = item.get("powerInstallation")
    return Project(
        pv_number=pv_number,
        name=item.get("name") if isinstance(item.get("name"), str) else None,
        installed_power_kwp=(
            float(power)
            if isinstance(power, int | float) and not isinstance(power, bool)
            else None
        ),
        commissioning_date=parse_iso_utc(item.get("b2CCommissioningDate")),
        offer_name=offer.get("name") if isinstance(offer.get("name"), str) else None,
    )


def parse_equipment(item: Any) -> Equipment | None:
    """Parse an installation equipment; return None when unusable."""
    if not isinstance(item, dict) or not isinstance(item.get("idEquipement"), str):
        return None
    return Equipment(
        equipment_id=item["idEquipement"],
        type=str(item.get("type") or "unknown"),
        sub_type=item.get("sousType")
        if isinstance(item.get("sousType"), str)
        else None,
        brand=item.get("marque") if isinstance(item.get("marque"), str) else None,
        last_connection=parse_iso_utc(item.get("dateDerniereConnexion")),
    )


def parse_device(item: Any) -> Device:
    """Parse a monitoring device status."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise EdfEnrApiError("Device without id")
    return Device(
        device_id=item["id"],
        type=item.get("type") if isinstance(item.get("type"), str) else None,
        status=item.get("status") if isinstance(item.get("status"), str) else None,
        last_reported_at=parse_epoch(item.get("lastReportedAt")),
    )
