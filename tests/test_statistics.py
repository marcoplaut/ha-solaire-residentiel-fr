"""Tests of the import of energy into long-term statistics."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from itertools import pairwise
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.recorder.models import StatisticData, StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.edf_enr.api import (
    EdfEnrConnectionError,
    EnergyReading,
    Granularity,
)
from custom_components.edf_enr.const import STATISTICS_REVISION_WINDOW
from custom_components.edf_enr.statistics import (
    FETCH_CHUNK,
    SERIES,
    EdfEnrStatisticsImporter,
    aggregate_hourly,
    build_rows,
    floor_hour,
    history_start_hour,
    iter_chunks,
    statistic_id,
    statistic_slug,
)

from .conftest import PROJECT, FakeTelemetry, hourly_profile
from .const import COMMISSIONING_DATE, NOW, PV_NUMBER

HOUR = timedelta(hours=1)
SERIES_BY_KEY = {series.key: series for series in SERIES}
PRODUCTION = SERIES_BY_KEY["solar_production"]


def reading(start: datetime, production: float | None, **kwargs: Any) -> EnergyReading:
    return EnergyReading(
        start=start,
        granularity=Granularity.HOURLY,
        solar_production=production,
        self_consumption=kwargs.get("self_consumption", 0.0),
        grid_import=kwargs.get("grid_import", 0.0),
        ecs_consumption=0.0,
    )


async def get_rows(
    hass: HomeAssistant, key: str, start: datetime = COMMISSIONING_DATE
) -> list[dict[str, Any]]:
    """Return the stored hourly rows of a series, oldest first."""
    await async_wait_recording_done(hass)
    stat_id = statistic_id(PV_NUMBER, SERIES_BY_KEY[key])
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        None,
        {stat_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return [
        {
            "start": datetime.fromtimestamp(row["start"], UTC),
            "state": row["state"],
            "sum": row["sum"],
        }
        for row in stats.get(stat_id, [])
    ]


def expected_values(key: str, start: datetime, end: datetime) -> list[float]:
    """Return the profile values of a series for each hour of [start, end)."""
    values = []
    hour = start
    while hour < end:
        values.append(SERIES_BY_KEY[key].value_fn(hourly_profile(hour)) or 0.0)
        hour += HOUR
    return values


def assert_consistent(rows: list[dict[str, Any]]) -> None:
    """Assert rows are contiguous hours whose sums accumulate their states."""
    for previous, row in pairwise(rows):
        assert row["start"] - previous["start"] == HOUR
        assert row["sum"] == pytest.approx(previous["sum"] + row["state"], abs=1e-6)


def importer_of(entry: MockConfigEntry) -> EdfEnrStatisticsImporter:
    return entry.runtime_data.statistics


# ----------------------------------------------------------------- pure helpers


def test_floor_hour_converts_to_utc() -> None:
    local = datetime.fromisoformat("2026-10-24T14:45:12+02:00")
    assert floor_hour(local) == datetime(2026, 10, 24, 12, tzinfo=UTC)


def test_aggregate_hourly_skips_unknown_and_sums_same_hour() -> None:
    hour = datetime(2026, 3, 29, 1, tzinfo=UTC)
    readings = [
        # The portal's phantom 02:00 and the real 03:00 (CEST) both map to 01:00 UTC.
        reading(hour, 0.4),
        reading(hour, 0.2),
        reading(hour + HOUR, None),
        reading(hour + 2 * HOUR, 0.0),
    ]
    assert aggregate_hourly(readings, PRODUCTION) == {
        hour: 0.6,
        hour + 2 * HOUR: 0.0,
    }


def test_build_rows_fills_gaps_and_stops_at_last_known_hour() -> None:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    hours = {start + HOUR: 1.0, start + 3 * HOUR: 2.0}
    rows, total = build_rows(hours, start, start + 10 * HOUR, 5.0)
    assert [(row["start"] - start, row["state"], row["sum"]) for row in rows] == [
        (timedelta(0), 0.0, 5.0),
        (HOUR, 1.0, 6.0),
        (2 * HOUR, 0.0, 6.0),
        (3 * HOUR, 2.0, 8.0),
    ]
    assert total == 8.0


def test_build_rows_ignores_hours_outside_the_range() -> None:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    hours = {start - HOUR: 1.0, start + 2 * HOUR: 1.0}
    assert build_rows(hours, start, start + 2 * HOUR, 3.0) == ([], 3.0)


def test_history_start_hour() -> None:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    zeros = [{start: 0.0, start + HOUR: 0.0}, {start + 2 * HOUR: 0.0}]
    assert history_start_hour(zeros, last_chunk=False) is None
    assert history_start_hour(zeros, last_chunk=True) == start + 2 * HOUR
    assert history_start_hour([{}, {}], last_chunk=True) is None
    mixed = [{start: 0.0, start + 5 * HOUR: 1.0}, {start + 3 * HOUR: 0.2}]
    assert history_start_hour(mixed, last_chunk=False) == start + 3 * HOUR


def test_iter_chunks() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + FETCH_CHUNK * 2 + HOUR
    assert list(iter_chunks(start, end)) == [
        (start, start + FETCH_CHUNK),
        (start + FETCH_CHUNK, start + 2 * FETCH_CHUNK),
        (start + 2 * FETCH_CHUNK, end),
    ]
    assert list(iter_chunks(end, end)) == []


@pytest.mark.parametrize(
    ("pv_number", "slug"),
    [("PV0000042", "pv0000042"), ("PV-12 34", "pv_12_34"), ("--", "unknown")],
)
def test_statistic_slug(pv_number: str, slug: str) -> None:
    assert statistic_slug(pv_number) == slug


def test_statistic_ids_are_valid() -> None:
    for series in SERIES:
        assert statistic_id(PV_NUMBER, series) == f"edf_enr:pv0000042_{series.key}"


# ------------------------------------------------------------- recorder import


async def test_first_import_backfills_history(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    end = floor_hour(NOW)
    for series in SERIES:
        rows = await get_rows(hass, series.key)
        assert_consistent(rows)
        # Every series starts at the first hour where any series is non zero
        # (grid import at night) and stops before the current hour.
        assert rows[0]["start"] == COMMISSIONING_DATE
        assert rows[-1]["start"] == end - HOUR
        values = expected_values(series.key, COMMISSIONING_DATE, end)
        assert [row["state"] for row in rows] == pytest.approx(values)
        assert rows[-1]["sum"] == pytest.approx(sum(values))


async def test_metadata(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    await async_wait_recording_done(hass)
    stat_ids = set(importer_of(init_integration).statistic_ids)
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids=stat_ids)
    )
    assert set(metadata) == stat_ids
    for _, meta in metadata.values():
        assert meta["source"] == "edf_enr"
        assert meta["has_sum"] is True
        assert meta["mean_type"] is StatisticMeanType.NONE
        assert meta["unit_of_measurement"] == "kWh"
        assert meta["unit_class"] == "energy"
        assert meta["name"].startswith(f"Solaire Résidentiel FR {PV_NUMBER} ")


async def test_incremental_import_does_not_double_count(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    frozen_time: FrozenDateTimeFactory,
    telemetry: FakeTelemetry,
) -> None:
    # Only the importer runs: no scheduled coordinator refresh in between.
    await init_integration.runtime_data.async_shutdown()
    frozen_time.tick(timedelta(hours=5))
    telemetry.calls.clear()
    await importer_of(init_integration).async_import()

    end = floor_hour(NOW + timedelta(hours=5))
    # Only the revision window is fetched again.
    assert telemetry.calls == [
        (Granularity.HOURLY, end - STATISTICS_REVISION_WINDOW, end)
    ]
    rows = await get_rows(hass, "solar_production")
    assert_consistent(rows)
    assert rows[-1]["start"] == end - HOUR
    assert rows[-1]["sum"] == pytest.approx(
        sum(expected_values("solar_production", COMMISSIONING_DATE, end))
    )


async def test_revised_hour_shifts_later_sums(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    telemetry: FakeTelemetry,
) -> None:
    before = await get_rows(hass, "solar_production")
    revised_hour = datetime(2026, 10, 23, 11, tzinfo=UTC)
    original = hourly_profile(revised_hour)
    telemetry.overrides[revised_hour] = reading(
        revised_hour,
        original.solar_production + 1.5,
        self_consumption=original.self_consumption,
        grid_import=original.grid_import,
    )

    await importer_of(init_integration).async_import()

    after = await get_rows(hass, "solar_production")
    assert_consistent(after)
    assert len(after) == len(before)
    for old, new in zip(before, after, strict=True):
        delta = 1.5 if new["start"] >= revised_hour else 0.0
        assert new["sum"] == pytest.approx(old["sum"] + delta)
    # The derived injection series follows the revision too.
    injection = {row["start"]: row for row in await get_rows(hass, "grid_injection")}
    assert injection[revised_hour]["state"] == pytest.approx(
        original.grid_injection + 1.5
    )


async def test_late_hours_are_imported_when_reported(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_statistics_pause: None,
    telemetry: FakeTelemetry,
) -> None:
    telemetry.horizon = floor_hour(NOW) - 4 * HOUR
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    rows = await get_rows(hass, "solar_production")
    assert rows[-1]["start"] == telemetry.horizon - HOUR

    telemetry.horizon = None
    await importer_of(mock_config_entry).async_import()

    rows = await get_rows(hass, "solar_production")
    assert_consistent(rows)
    assert rows[-1]["start"] == floor_hour(NOW) - HOUR
    assert rows[-1]["sum"] == pytest.approx(
        sum(expected_values("solar_production", COMMISSIONING_DATE, floor_hour(NOW)))
    )


async def test_unknown_hours_are_zero_until_revised(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    telemetry: FakeTelemetry,
) -> None:
    missing = datetime(2026, 10, 23, 12, tzinfo=UTC)
    telemetry.overrides[missing] = None
    await importer_of(init_integration).async_import()
    rows = {row["start"]: row for row in await get_rows(hass, "solar_production")}
    assert rows[missing]["state"] == 0.0

    del telemetry.overrides[missing]
    await importer_of(init_integration).async_import()
    rows = {row["start"]: row for row in await get_rows(hass, "solar_production")}
    assert rows[missing]["state"] == hourly_profile(missing).solar_production
    assert_consistent(list(rows.values()))


async def test_gap_after_downtime_is_filled(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    frozen_time: FrozenDateTimeFactory,
    telemetry: FakeTelemetry,
) -> None:
    last_before = (await get_rows(hass, "solar_production"))[-1]["start"]
    # Simulate Home Assistant being stopped: no refresh while time flies.
    await init_integration.runtime_data.async_shutdown()
    frozen_time.tick(STATISTICS_REVISION_WINDOW + timedelta(days=2))
    telemetry.calls.clear()

    await importer_of(init_integration).async_import()

    end = floor_hour(NOW + STATISTICS_REVISION_WINDOW + timedelta(days=2))
    assert telemetry.calls == [(Granularity.HOURLY, last_before + HOUR, end)]
    rows = await get_rows(hass, "solar_production")
    assert_consistent(rows)
    assert rows[-1]["sum"] == pytest.approx(
        sum(expected_values("solar_production", COMMISSIONING_DATE, end))
    )


async def test_long_history_is_fetched_in_chunks(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_statistics_pause: None,
    telemetry: FakeTelemetry,
) -> None:
    commissioning = NOW - timedelta(days=5)
    mock_client.async_get_projects.return_value = [
        replace(PROJECT, commissioning_date=commissioning)
    ]
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.edf_enr.statistics.FETCH_CHUNK", timedelta(days=2)):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    # The first call is the coordinator's "today" refresh.
    import_calls = telemetry.calls[1:]
    assert [end - start for _, start, end in import_calls] == [
        timedelta(days=2),
        timedelta(days=2),
        floor_hour(NOW) - floor_hour(commissioning) - timedelta(days=4),
    ]
    rows = await get_rows(hass, "home_consumption", floor_hour(commissioning))
    assert_consistent(rows)
    assert rows[0]["start"] == floor_hour(commissioning)


async def test_all_zero_history_still_creates_statistics(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_statistics_pause: None,
    telemetry: FakeTelemetry,
) -> None:
    hour = COMMISSIONING_DATE
    while hour < NOW:
        telemetry.overrides[hour] = reading(hour, 0.0)
        hour += HOUR
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    for series in SERIES:
        rows = await get_rows(hass, series.key)
        assert rows == [{"start": floor_hour(NOW) - HOUR, "state": 0.0, "sum": 0.0}]

    # The next import is incremental (statistics exist now).
    telemetry.calls.clear()
    await importer_of(mock_config_entry).async_import()
    assert telemetry.calls[0][1] == floor_hour(NOW) - STATISTICS_REVISION_WINDOW


async def test_import_errors_are_logged_and_retried(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_statistics_pause: None,
    telemetry: FakeTelemetry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_client.async_get_energy.side_effect = [
        [],  # coordinator refresh (today)
        EdfEnrConnectionError("boom"),  # back-fill
    ]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "Statistics import interrupted: boom" in caplog.text
    assert await get_rows(hass, "solar_production") == []

    mock_client.async_get_energy.side_effect = telemetry.__call__
    await importer_of(mock_config_entry).async_import()
    rows = await get_rows(hass, "solar_production")
    assert rows[0]["start"] == COMMISSIONING_DATE
    assert_consistent(rows)


async def test_full_import_overwrites_existing_rows(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    telemetry: FakeTelemetry,
) -> None:
    old_hour = COMMISSIONING_DATE + 5 * HOUR
    telemetry.overrides[old_hour] = reading(old_hour, 10.0, grid_import=0.6)
    await importer_of(init_integration).async_import(full=True)
    rows = {row["start"]: row for row in await get_rows(hass, "solar_production")}
    assert rows[old_hour]["state"] == 10.0
    assert_consistent(list(rows.values()))


async def test_import_is_skipped_when_nothing_to_fetch(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    telemetry: FakeTelemetry,
) -> None:
    importer = importer_of(init_integration)
    telemetry.calls.clear()
    with patch(
        "custom_components.edf_enr.statistics.STATISTICS_REVISION_WINDOW",
        timedelta(0),
    ):
        await importer.async_import()
    assert telemetry.calls == []


async def test_clear_deletes_statistics(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    importer = importer_of(init_integration)
    await importer.async_clear()
    for series in SERIES:
        assert await get_rows(hass, series.key) == []
    # Without statistics, the next import is a full back-fill again.
    await importer.async_import()
    assert (await get_rows(hass, "solar_production"))[0]["start"] == COMMISSIONING_DATE


async def test_reimport_starts_at_the_commissioning_date(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    telemetry: FakeTelemetry,
) -> None:
    """With a commissioning date, hours stored before it are dropped."""
    importer = importer_of(init_integration)
    early = COMMISSIONING_DATE - 5 * HOUR
    stat_id = statistic_id(PV_NUMBER, PRODUCTION)
    async_add_external_statistics(
        hass,
        importer.metadata(PRODUCTION),
        [StatisticData(start=early, state=1.0, sum=1.0)],
    )
    await async_wait_recording_done(hass)
    assert (await get_rows(hass, "solar_production", early))[0]["start"] == early

    await importer.async_reimport()

    rows = await get_rows(hass, "solar_production", early)
    assert rows[0]["start"] == COMMISSIONING_DATE
    assert stat_id in importer.statistic_ids
    assert_consistent(rows)


async def test_no_stored_hour_before_the_history_start(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    importer = importer_of(init_integration)
    assert await importer._async_first_stored_hour() is None
    await importer.async_clear()
    assert await importer._async_first_stored_hour() is None


async def test_running_flag(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    importer = importer_of(init_integration)
    assert not importer.running
    states: list[bool] = []

    async def fake_import(*, full: bool) -> None:
        states.append(importer.running)

    with patch.object(importer, "_async_import", fake_import):
        await importer.async_import()
    assert states == [True]
    assert not importer.running


async def test_leading_zero_chunks_are_skipped(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_statistics_pause: None,
    telemetry: FakeTelemetry,
) -> None:
    commissioning = floor_hour(NOW - timedelta(days=4))
    first_data = commissioning + timedelta(days=2, hours=3)
    hour = commissioning
    while hour < first_data:
        telemetry.overrides[hour] = reading(hour, 0.0)
        hour += HOUR
    mock_client.async_get_projects.return_value = [
        replace(PROJECT, commissioning_date=commissioning)
    ]
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.edf_enr.statistics.FETCH_CHUNK", timedelta(days=2)):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    rows = await get_rows(hass, "grid_import", commissioning)
    assert rows[0]["start"] == first_data
    assert_consistent(rows)


async def test_window_without_data_keeps_existing_rows(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    telemetry: FakeTelemetry,
) -> None:
    before = await get_rows(hass, "solar_production")
    # The gateway stopped reporting before the revision window.
    telemetry.horizon = floor_hour(NOW) - STATISTICS_REVISION_WINDOW
    await importer_of(init_integration).async_import()
    assert await get_rows(hass, "solar_production") == before


async def test_revision_window_starts_at_history_start(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_statistics_pause: None,
    telemetry: FakeTelemetry,
) -> None:
    # Commissioned yesterday: the revision window would reach before it, and
    # the portal reports hours from before commissioning.
    commissioning = floor_hour(NOW - timedelta(days=1))
    mock_client.async_get_projects.return_value = [
        replace(PROJECT, commissioning_date=commissioning)
    ]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    telemetry.calls.clear()

    await importer_of(mock_config_entry).async_import()

    assert telemetry.calls == [(Granularity.HOURLY, commissioning, floor_hour(NOW))]
    rows = await get_rows(hass, "grid_import", commissioning - timedelta(days=3))
    assert rows[0]["start"] == commissioning
    assert_consistent(rows)


async def test_reimport_keeps_history_older_than_the_default_start(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_statistics_pause: None,
    telemetry: FakeTelemetry,
    frozen_time: FrozenDateTimeFactory,
) -> None:
    """Without a commissioning date, a re-import keeps the hours already stored.

    The default history start (now - STATISTICS_DEFAULT_HISTORY) moves forward
    at every setup.
    """
    mock_client.async_get_projects.return_value = [
        replace(PROJECT, commissioning_date=None)
    ]
    history = timedelta(days=5)
    since = NOW - 2 * history
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.edf_enr.statistics.STATISTICS_DEFAULT_HISTORY", history
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        first = (await get_rows(hass, "solar_production", since))[0]["start"]
        assert first == floor_hour(NOW - history)

        # Home Assistant restarts two days later, then the user re-imports.
        frozen_time.tick(timedelta(days=2))
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
        await importer_of(mock_config_entry).async_reimport()

    rows = await get_rows(hass, "solar_production", since)
    assert rows[0]["start"] == first
    assert rows[-1]["start"] == floor_hour(NOW + timedelta(days=2)) - HOUR
    assert_consistent(rows)


async def test_unknown_hours_before_a_chunk_boundary_keep_rows_contiguous(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_statistics_pause: None,
    telemetry: FakeTelemetry,
) -> None:
    """Unknown hours at the end of a back-fill chunk are stored as 0.

    Otherwise the row before a later revision window can be missing, and the
    window's sums would restart from 0.
    """
    commissioning = floor_hour(NOW - timedelta(days=5))
    mock_client.async_get_projects.return_value = [
        replace(PROJECT, commissioning_date=commissioning)
    ]
    # The gateway did not report the last hours of the first chunk, which
    # ends where the next revision window starts.
    boundary = commissioning + timedelta(days=2)
    assert boundary == floor_hour(NOW) - STATISTICS_REVISION_WINDOW
    for hours in range(1, 4):
        telemetry.overrides[boundary - hours * HOUR] = None
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.edf_enr.statistics.FETCH_CHUNK", timedelta(days=2)):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    await mock_config_entry.runtime_data.async_shutdown()
    await importer_of(mock_config_entry).async_import()

    for series in SERIES:
        rows = await get_rows(hass, series.key, commissioning)
        assert rows[0]["start"] == commissioning
        assert_consistent(rows)
    rows = await get_rows(hass, "grid_import", commissioning)
    assert rows[-1]["sum"] == pytest.approx(
        sum(expected_values("grid_import", commissioning, floor_hour(NOW)))
        - sum(hourly_profile(boundary - h * HOUR).grid_import for h in range(1, 4))
    )


async def test_series_never_reported_does_not_restart_the_back_fill(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_statistics_pause: None,
    telemetry: FakeTelemetry,
) -> None:
    """A series the portal never reports (no row) does not force full imports."""
    hour = COMMISSIONING_DATE
    while hour < NOW:
        telemetry.overrides[hour] = replace(hourly_profile(hour), grid_import=None)
        hour += HOUR
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    assert await get_rows(hass, "grid_import") == []
    assert await get_rows(hass, "home_consumption") == []

    telemetry.calls.clear()
    await importer_of(mock_config_entry).async_import()

    # Only the revision window, not the whole history again.
    end = floor_hour(NOW)
    assert telemetry.calls == [
        (Granularity.HOURLY, end - STATISTICS_REVISION_WINDOW, end)
    ]
    rows = await get_rows(hass, "solar_production")
    # Without grid import, the history starts with the first production.
    assert rows[0]["start"] == COMMISSIONING_DATE + 9 * HOUR
    assert_consistent(rows)


async def test_series_reported_late_starts_with_the_others(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    telemetry: FakeTelemetry,
) -> None:
    """A series without rows yet starts at the others' re-import start, from 0."""
    importer = importer_of(init_integration)
    grid_import = statistic_id(PV_NUMBER, SERIES_BY_KEY["grid_import"])
    get_instance(hass).async_clear_statistics([grid_import])
    await async_wait_recording_done(hass)

    await importer.async_import()

    rows = await get_rows(hass, "grid_import")
    window_start = floor_hour(NOW) - STATISTICS_REVISION_WINDOW
    assert rows[0]["start"] == window_start
    assert rows[-1]["sum"] == pytest.approx(
        sum(expected_values("grid_import", window_start, floor_hour(NOW)))
    )
    assert_consistent(rows)
