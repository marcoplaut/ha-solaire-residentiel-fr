"""Rejection of physically impossible telemetry values.

The portal sometimes publishes corrupted hourly buckets: a meter index instead
of the energy of the hour (thousands to millions of kWh), followed by a few
hours in Wh instead of kWh. The daily totals of the same days stay right.

Hourly values above what the installation can physically produce or draw are
rejected, and what the daily total has in excess of the accepted hours is
spread over the rejected ones, so that the energy of the day is preserved.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, Any

from .const import API_TIMEZONE
from .models import EnergyReading, Granularity

if TYPE_CHECKING:
    from .client import EdfEnrClient

_LOGGER = logging.getLogger(__name__)

# Highest power of a French residential grid connection (36 kVA).
MAX_GRID_POWER_KW = 36.0
# Margin over the peak power of the panels (irradiance peaks, rounding).
SOLAR_POWER_MARGIN = 1.5
# Hours of the longest local day (the day clocks go back).
MAX_HOURS_PER_DAY = 25

_SOLAR_FIELDS = ("solar_production", "self_consumption")
_GRID_FIELDS = ("grid_import", "ecs_consumption")
FIELDS = _SOLAR_FIELDS + _GRID_FIELDS


@dataclass(frozen=True, slots=True)
class EnergyLimits:
    """Highest power (kW) each measurement of an installation can reach."""

    solar_kw: float
    grid_kw: float = MAX_GRID_POWER_KW

    @classmethod
    def for_installation(cls, peak_power_kwp: float | None) -> EnergyLimits:
        """Return the limits of an installation of the given peak power."""
        if not peak_power_kwp or peak_power_kwp <= 0:
            # Residential installations are connected below the grid limit.
            return cls(solar_kw=MAX_GRID_POWER_KW)
        return cls(solar_kw=peak_power_kwp * SOLAR_POWER_MARGIN)

    def max_energy(self, field: str, hours: float) -> float:
        """Return the highest energy (kWh) a field can measure over ``hours``."""
        power = self.solar_kw if field in _SOLAR_FIELDS else self.grid_kw
        return round(power * hours, 3)


def _local_day(value: datetime) -> date:
    return value.astimezone(API_TIMEZONE).date()


def _rejected(reading: EnergyReading, field: str, limits: EnergyLimits) -> bool:
    value: float | None = getattr(reading, field)
    return value is not None and value > limits.max_energy(field, 1)


def rejected_days(hourly: list[EnergyReading], limits: EnergyLimits) -> set[date]:
    """Return the local days with at least one impossible hourly value."""
    return {
        _local_day(reading.start)
        for reading in hourly
        if any(_rejected(reading, field, limits) for field in FIELDS)
    }


def clean_hourly(
    hourly: list[EnergyReading],
    daily: list[EnergyReading],
    limits: EnergyLimits,
) -> list[EnergyReading]:
    """Replace the impossible hourly values using the daily totals.

    A rejected value becomes an equal share of what the daily total has in
    excess of the accepted hours, capped at the hourly limit. It becomes
    unknown (``None``) when the daily total is missing, impossible too, or
    lower than the accepted hours.
    """
    totals = {_local_day(reading.start): reading for reading in daily}
    days: dict[date, list[int]] = defaultdict(list)
    for index, reading in enumerate(hourly):
        days[_local_day(reading.start)].append(index)
    changes: dict[int, dict[str, Any]] = defaultdict(dict)
    for day, indexes in days.items():
        for field in FIELDS:
            rejected = [i for i in indexes if _rejected(hourly[i], field, limits)]
            if not rejected:
                continue
            accepted = sum(
                value
                for i in indexes
                if i not in rejected
                and (value := getattr(hourly[i], field)) is not None
            )
            share: float | None = None
            total: float | None = getattr(totals.get(day), field, None)
            if total is not None and accepted <= total <= limits.max_energy(
                field, MAX_HOURS_PER_DAY
            ):
                share = round(
                    min(
                        (total - accepted) / len(rejected), limits.max_energy(field, 1)
                    ),
                    3,
                )
            for i in rejected:
                changes[i][field] = share
    return [
        replace(reading, **changes[index]) if index in changes else reading
        for index, reading in enumerate(hourly)
    ]


async def async_get_hourly_energy(
    client: EdfEnrClient,
    pv_number: str,
    start: datetime,
    end: datetime,
    limits: EnergyLimits,
) -> list[EnergyReading]:
    """Return the hourly buckets in [start, end), impossible values replaced.

    The daily totals are only requested when an hourly value is rejected. The
    hours of the rejected days outside [start, end) are requested too: the
    share of a rejected hour depends on all the accepted hours of its day.
    """
    hourly = await client.async_get_energy(pv_number, Granularity.HOURLY, start, end)
    days = rejected_days(hourly, limits)
    if not days:
        return hourly
    _LOGGER.debug("Impossible hourly energy values on %s", sorted(days))
    days_start = datetime.combine(min(days), time(), API_TIMEZONE)
    days_end = datetime.combine(max(days) + timedelta(days=1), time(), API_TIMEZONE)
    daily = await client.async_get_energy(
        pv_number, Granularity.DAILY, days_start, days_end
    )
    if days_start < start or days_end > end:
        hourly = await client.async_get_energy(
            pv_number, Granularity.HOURLY, min(days_start, start), max(days_end, end)
        )
    return [
        reading
        for reading in clean_hourly(hourly, daily, limits)
        if start <= reading.start < end
    ]
