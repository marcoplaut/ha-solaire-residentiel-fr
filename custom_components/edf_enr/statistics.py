"""Import of hourly energy into Home Assistant long-term statistics.

The portal publishes measurements with a delay and sometimes revises them, so
regular sensors cannot feed the Energy dashboard reliably. Instead, each energy
series is written as an external statistic (``edf_enr:<pv>_<series>``):

* the first import back-fills the whole history, one month per request;
* every later import rewrites the last ``STATISTICS_REVISION_WINDOW`` hours
  (and any gap since the last import) and rebuilds the cumulative sums from
  there, so revised hours never leave the sums inconsistent.

Rows of a series are contiguous: an hour without a value is stored as 0 when
later hours are known, and trailing unknown hours are not stored until the
portal reports them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import EdfEnrClient, EdfEnrError, EnergyReading, Granularity
from .const import (
    DOMAIN,
    INTEGRATION_NAME,
    LOGGER,
    STATISTICS_DEFAULT_HISTORY,
    STATISTICS_REVISION_WINDOW,
)

HOUR = timedelta(hours=1)
# Size of one telemetry request while back-filling.
FETCH_CHUNK = timedelta(days=31)
# Pause between back-fill requests, to stay gentle with the portal.
FETCH_PAUSE = 0.5
# Seconds to wait for the recorder to delete statistics (as its websocket API).
CLEAR_TIMEOUT = 10


@dataclass(frozen=True, kw_only=True, slots=True)
class StatisticSeries:
    """An energy series imported as an external statistic."""

    key: str
    name: str
    value_fn: Callable[[EnergyReading], float | None]


SERIES: tuple[StatisticSeries, ...] = (
    StatisticSeries(
        key="solar_production",
        name="solar production",
        value_fn=lambda r: r.solar_production,
    ),
    StatisticSeries(
        key="grid_import", name="grid import", value_fn=lambda r: r.grid_import
    ),
    StatisticSeries(
        key="grid_injection",
        name="grid injection",
        value_fn=lambda r: r.grid_injection,
    ),
    StatisticSeries(
        key="self_consumption",
        name="self consumption",
        value_fn=lambda r: r.self_consumption,
    ),
    StatisticSeries(
        key="home_consumption",
        name="home consumption",
        value_fn=lambda r: r.home_consumption,
    ),
)


def floor_hour(value: datetime) -> datetime:
    """Return the start of the UTC hour containing ``value``."""
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def aggregate_hourly(
    readings: list[EnergyReading], series: StatisticSeries
) -> dict[datetime, float]:
    """Sum the known values of a series per UTC hour.

    Around DST changes the portal labels buckets with local times that can map
    to the same UTC hour; their values are added up.
    """
    hours: dict[datetime, float] = {}
    for reading in readings:
        value = series.value_fn(reading)
        if value is None:
            continue
        hour = floor_hour(reading.start)
        hours[hour] = round(hours.get(hour, 0.0) + value, 3)
    return hours


def build_rows(
    hours: dict[datetime, float],
    start: datetime,
    end: datetime,
    base_sum: float,
) -> tuple[list[StatisticData], float]:
    """Build contiguous statistic rows for [start, end) and the final sum.

    Rows stop at the last known hour. Unknown hours before it are stored as 0.
    """
    known = [hour for hour in hours if start <= hour < end]
    if not known:
        return [], base_sum
    last = max(known)
    rows: list[StatisticData] = []
    running = base_sum
    hour = start
    while hour <= last:
        value = hours.get(hour, 0.0)
        running = round(running + value, 3)
        rows.append(StatisticData(start=hour, state=value, sum=running))
        hour += HOUR
    return rows, running


def history_start_hour(
    hourly: list[dict[datetime, float]], *, last_chunk: bool
) -> datetime | None:
    """Return the hour where the history of all series starts, if in this chunk.

    The history starts at the first hour where any series is non zero, so
    months of zeros before commissioning are not stored. If the whole history
    is zero, it starts at the last known hour so that statistics exist.
    """
    non_zero = [hour for hours in hourly for hour, value in hours.items() if value]
    if non_zero:
        return min(non_zero)
    if last_chunk:
        known = [hour for hours in hourly for hour in hours]
        return max(known) if known else None
    return None


def iter_chunks(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
    """Split [start, end) in request-sized chunks."""
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + FETCH_CHUNK, end)
        yield chunk_start, chunk_end
        chunk_start = chunk_end


@dataclass(slots=True)
class _SeriesPlan:
    series: StatisticSeries
    statistic_id: str
    start: datetime
    running_sum: float


class EdfEnrStatisticsImporter:
    """Keep the external statistics of one project up to date."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EdfEnrClient,
        pv_number: str,
        *,
        history_start: datetime | None,
    ) -> None:
        """Initialize the importer.

        ``history_start`` is the commissioning date, if the portal knows it.
        """
        self._hass = hass
        self._client = client
        self._pv_number = pv_number
        # Without a commissioning date the start is a guess, moving at each setup.
        self._history_start_known = history_start is not None
        self._history_start = floor_hour(
            history_start or dt_util.utcnow() - STATISTICS_DEFAULT_HISTORY
        )
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        """Return True while an import is in progress."""
        return self._lock.locked()

    def statistic_id(self, series: StatisticSeries) -> str:
        """Return the statistic id of a series."""
        return statistic_id(self._pv_number, series)

    @property
    def statistic_ids(self) -> list[str]:
        """Return the statistic ids of all series."""
        return statistic_ids(self._pv_number)

    def metadata(self, series: StatisticSeries) -> StatisticMetaData:
        """Return the metadata of a series."""
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{INTEGRATION_NAME} {self._pv_number} {series.name}",
            source=DOMAIN,
            statistic_id=self.statistic_id(series),
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

    async def async_import(self, *, full: bool = False) -> None:
        """Import new and revised hours (or the whole history if ``full``)."""
        async with self._lock:
            await self._async_import_or_log(full=full)

    async def async_reimport(self) -> None:
        """Delete the statistics, then import the whole history again.

        The lock also covers the deletion, so no incremental import (which
        would read the sums being deleted) or second re-import interleaves.
        """
        async with self._lock:
            # A guessed history start moves forward at every setup: keep the
            # older hours already imported (the portal still has them).
            if not self._history_start_known and (
                first := await self._async_first_stored_hour()
            ):
                self._history_start = min(self._history_start, first)
            await self.async_clear()
            await self._async_import_or_log(full=True)

    async def _async_first_stored_hour(self) -> datetime | None:
        """Return the oldest stored hour, if it is older than the history start."""
        # The oldest month first (cheap), then the oldest hour within it.
        month = await self._async_first_row_start(
            datetime.fromtimestamp(0, UTC), self._history_start, "month"
        )
        if month is None:
            return None
        return await self._async_first_row_start(
            month,
            min(month + FETCH_CHUNK + timedelta(days=1), self._history_start),
            "hour",
        )

    async def _async_first_row_start(
        self, start: datetime, end: datetime, period: Literal["hour", "month"]
    ) -> datetime | None:
        stats = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            start,
            end,
            set(self.statistic_ids),
            period,
            None,
            {"sum"},
        )
        starts = [rows[0]["start"] for rows in stats.values() if rows]
        return dt_util.utc_from_timestamp(min(starts)) if starts else None

    async def async_clear(self) -> None:
        """Delete the statistics of all series."""
        await async_clear_statistics(self._hass, self._pv_number)

    async def _async_import_or_log(self, *, full: bool) -> None:
        try:
            await self._async_import(full=full)
        except EdfEnrError as err:
            # The coordinator reports connectivity and auth problems; the
            # next refresh retries the import from where it stopped.
            LOGGER.warning("Statistics import interrupted: %s", err)

    async def _async_import(self, *, full: bool) -> None:
        end = floor_hour(dt_util.utcnow())
        plans = None if full else await self._async_incremental_plans(end)
        if plans is None:
            full = True
            plans = [
                _SeriesPlan(series, self.statistic_id(series), self._history_start, 0.0)
                for series in SERIES
            ]
        fetch_start = min(plan.start for plan in plans)
        if fetch_start >= end:
            return
        LOGGER.debug(
            "Importing %s statistics from %s to %s",
            "all" if full else "recent",
            fetch_start,
            end,
        )
        started = not full
        for index, (chunk_start, chunk_end) in enumerate(iter_chunks(fetch_start, end)):
            if index:
                await asyncio.sleep(FETCH_PAUSE)
            readings = await self._client.async_get_energy(
                self._pv_number, Granularity.HOURLY, chunk_start, chunk_end
            )
            hourly = [aggregate_hourly(readings, plan.series) for plan in plans]
            if not started:
                first_hour = history_start_hour(hourly, last_chunk=chunk_end == end)
                if first_hour is None:
                    continue
                for plan in plans:
                    plan.start = first_hour
                started = True
            for plan, hours in zip(plans, hourly, strict=True):
                # From the hour after the last stored row, even when it lies in
                # a previous chunk: unknown hours become 0 and rows stay
                # contiguous, which _async_incremental_plans relies on.
                rows, plan.running_sum = build_rows(
                    hours, plan.start, chunk_end, plan.running_sum
                )
                if rows:
                    plan.start = rows[-1]["start"] + HOUR
                    async_add_external_statistics(
                        self._hass, self.metadata(plan.series), rows
                    )

    async def _async_incremental_plans(self, end: datetime) -> list[_SeriesPlan] | None:
        """Plan the re-import of recent hours, or None if no series has statistics."""
        recorder = get_instance(self._hass)
        # Never before the history start: the portal can report hours from
        # before commissioning, which the full import did not store.
        window_start = max(end - STATISTICS_REVISION_WINDOW, self._history_start)
        plans: list[_SeriesPlan] = []
        unreported: list[StatisticSeries] = []
        for series in SERIES:
            statistic_id = self.statistic_id(series)
            last = await recorder.async_add_executor_job(
                get_last_statistics, self._hass, 1, statistic_id, True, {"sum"}
            )
            if not last.get(statistic_id):
                unreported.append(series)
                continue
            last_row = last[statistic_id][0]
            last_start = dt_util.utc_from_timestamp(last_row["start"])
            start = min(last_start + HOUR, window_start)
            if start == last_start + HOUR:
                base_sum = last_row["sum"]
            else:
                # Rows are contiguous from the first one to the last one, so a
                # missing row means the series starts inside the window.
                base_sum = await self._async_sum_before(statistic_id, start)
            plans.append(_SeriesPlan(series, statistic_id, start, float(base_sum or 0)))
        if not plans:
            return None
        # A series the portal has never reported (no value at all, e.g. no
        # consumption measurement) starts with the others from 0, instead of
        # forcing a full back-fill of every series on each refresh.
        start = min(plan.start for plan in plans)
        plans.extend(
            _SeriesPlan(series, self.statistic_id(series), start, 0.0)
            for series in unreported
        )
        return plans

    async def _async_sum_before(self, statistic_id: str, start: datetime) -> Any:
        """Return the cumulative sum of the hour preceding ``start``."""
        stats = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            start - HOUR,
            start,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = stats.get(statistic_id)
        if not rows:
            return None
        return rows[0]["sum"]


def statistic_id(pv_number: str, series: StatisticSeries) -> str:
    """Return the statistic id of a series of a project."""
    return f"{DOMAIN}:{statistic_slug(pv_number)}_{series.key}"


def statistic_ids(pv_number: str) -> list[str]:
    """Return the statistic ids of all series of a project."""
    return [statistic_id(pv_number, series) for series in SERIES]


async def async_clear_statistics(hass: HomeAssistant, pv_number: str) -> None:
    """Delete the statistics of all series of a project."""
    done = asyncio.Event()

    def on_done() -> None:
        hass.loop.call_soon_threadsafe(done.set)

    get_instance(hass).async_clear_statistics(statistic_ids(pv_number), on_done=on_done)
    # The recorder only calls on_done when the deletion succeeds. Do not wait
    # forever: tasks queued afterwards (e.g. a re-import) still run after it.
    try:
        async with asyncio.timeout(CLEAR_TIMEOUT):
            await done.wait()
    except TimeoutError:
        LOGGER.warning(
            "The recorder did not confirm the deletion of the statistics of %s",
            pv_number,
        )


def statistic_slug(pv_number: str) -> str:
    """Return a statistic id compatible slug of a PV number."""
    slug = "".join(char if char.isalnum() else "_" for char in pv_number.lower())
    return "_".join(part for part in slug.split("_") if part) or "unknown"
