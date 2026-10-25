"""Tests for the rejection of impossible telemetry values."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.edf_enr.api import EdfEnrClient
from custom_components.edf_enr.api.cleaning import (
    EnergyLimits,
    async_get_hourly_energy,
    clean_hourly,
    rejected_days,
)
from custom_components.edf_enr.api.models import EnergyReading, Granularity

HOUR = timedelta(hours=1)
LIMITS = EnergyLimits.for_installation(3.96)  # 5.94 kWh/h solar, 36 kWh/h grid
# Local day 2026-10-24 (CEST) starts at 22:00 UTC the day before.
SUMMER_DAY = datetime(2026, 10, 23, 22, tzinfo=UTC)
# Local day 2026-01-15 (CET) starts at 23:00 UTC the day before.
WINTER_DAY = datetime(2026, 1, 14, 23, tzinfo=UTC)


def hourly(start: datetime, **energies: float | None) -> EnergyReading:
    return EnergyReading(
        start=start,
        granularity=Granularity.HOURLY,
        solar_production=energies.get("solar_production", 0.0),
        self_consumption=energies.get("self_consumption", 0.0),
        grid_import=energies.get("grid_import", 0.0),
        ecs_consumption=energies.get("ecs_consumption", 0.0),
    )


def daily(start: datetime, **energies: float | None) -> EnergyReading:
    return EnergyReading(
        start=start,
        granularity=Granularity.DAILY,
        solar_production=energies.get("solar_production", 0.0),
        self_consumption=energies.get("self_consumption", 0.0),
        grid_import=energies.get("grid_import", 0.0),
        ecs_consumption=energies.get("ecs_consumption", 0.0),
    )


def day_of(first: datetime, hours: int = 24, **energies: float | None) -> list:
    """Return ``hours`` identical hourly readings from ``first``."""
    return [hourly(first + i * HOUR, **energies) for i in range(hours)]


# ---------------------------------------------------------------------- limits


def test_limits_follow_the_peak_power() -> None:
    limits = EnergyLimits.for_installation(3.96)
    assert limits.solar_kw == pytest.approx(5.94)
    assert limits.grid_kw == 36.0


@pytest.mark.parametrize("peak", [None, 0, 0.0, -3.0])
def test_limits_without_a_usable_peak_power(peak: float | None) -> None:
    assert EnergyLimits.for_installation(peak) == EnergyLimits(solar_kw=36.0)


def test_max_energy_depends_on_the_field() -> None:
    limits = EnergyLimits(solar_kw=5.0, grid_kw=36.0)
    for field in ("solar_production", "self_consumption"):
        assert limits.max_energy(field, 1) == 5.0
        assert limits.max_energy(field, 25) == 125.0
    for field in ("grid_import", "ecs_consumption"):
        assert limits.max_energy(field, 1) == 36.0
        assert limits.max_energy(field, 2) == 72.0


# --------------------------------------------------------------- rejected_days


def test_no_rejected_day() -> None:
    readings = day_of(SUMMER_DAY, solar_production=5.9, grid_import=36.0)
    assert rejected_days(readings, LIMITS) == set()


def test_one_rejected_day() -> None:
    readings = day_of(SUMMER_DAY, solar_production=1.0)
    readings[5] = hourly(readings[5].start, solar_production=6790829.9)
    assert rejected_days(readings, LIMITS) == {date(2026, 10, 24)}


def test_several_rejected_days() -> None:
    readings = day_of(SUMMER_DAY, 72, solar_production=1.0)
    readings[3] = hourly(readings[3].start, grid_import=1e6)
    readings[60] = hourly(readings[60].start, ecs_consumption=378000.0)
    assert rejected_days(readings, LIMITS) == {date(2026, 10, 24), date(2026, 10, 26)}


def test_rejected_days_are_local_days() -> None:
    # 22:30 UTC is already the next day in Paris (CEST), 21:30 UTC is not.
    late = datetime(2026, 10, 24, 21, tzinfo=UTC)
    readings = [
        hourly(late, solar_production=100.0),
        hourly(late + 2 * HOUR, solar_production=100.0),
    ]
    assert rejected_days(readings, LIMITS) == {date(2026, 10, 24), date(2026, 10, 25)}


def test_rejected_days_on_a_daylight_saving_time_day() -> None:
    # 2026-10-25 lasts 25 hours: 22:00 UTC the day before to 23:00 UTC.
    readings = day_of(SUMMER_DAY + timedelta(days=1), 25, solar_production=1.0)
    readings[-1] = hourly(readings[-1].start, solar_production=100.0)
    assert rejected_days(readings, LIMITS) == {date(2026, 10, 25)}


def test_unknown_values_are_not_rejected() -> None:
    readings = [hourly(SUMMER_DAY, solar_production=None, grid_import=None)]
    assert rejected_days(readings, LIMITS) == set()


# ----------------------------------------------------------------- clean_hourly


def test_a_spike_becomes_the_residual_of_the_day() -> None:
    readings = day_of(SUMMER_DAY, 4, solar_production=1.0)
    readings[2] = hourly(readings[2].start, solar_production=6790829.9)

    cleaned = clean_hourly(readings, [daily(SUMMER_DAY, solar_production=5.5)], LIMITS)

    assert [r.solar_production for r in cleaned] == [1.0, 1.0, 2.5, 1.0]
    assert [r.start for r in cleaned] == [r.start for r in readings]


def test_rejected_hours_share_the_residual_equally() -> None:
    readings = day_of(SUMMER_DAY, 4, solar_production=1.0)
    readings[1] = hourly(readings[1].start, solar_production=500.0)
    readings[3] = hourly(readings[3].start, solar_production=7000000.0)

    cleaned = clean_hourly(readings, [daily(SUMMER_DAY, solar_production=5.0)], LIMITS)

    assert [r.solar_production for r in cleaned] == [1.0, 1.5, 1.0, 1.5]


def test_the_share_is_rounded_to_three_decimals() -> None:
    readings = day_of(SUMMER_DAY, 3, solar_production=0.0)
    readings[:3] = [hourly(r.start, solar_production=100.0) for r in readings]

    cleaned = clean_hourly(readings, [daily(SUMMER_DAY, solar_production=1.0)], LIMITS)

    assert [r.solar_production for r in cleaned] == [0.333, 0.333, 0.333]


def test_the_share_is_capped_at_the_hourly_limit() -> None:
    readings = day_of(SUMMER_DAY, 3, solar_production=0.0)
    readings[1] = hourly(readings[1].start, solar_production=6790829.9)

    cleaned = clean_hourly(readings, [daily(SUMMER_DAY, solar_production=20.0)], LIMITS)

    assert cleaned[1].solar_production == pytest.approx(5.94)


def test_a_missing_daily_total_makes_the_value_unknown() -> None:
    readings = day_of(SUMMER_DAY, 3, solar_production=1.0)
    readings[1] = hourly(readings[1].start, solar_production=6790829.9)

    cleaned = clean_hourly(readings, [], LIMITS)

    assert [r.solar_production for r in cleaned] == [1.0, None, 1.0]


def test_a_daily_total_of_another_day_is_not_used() -> None:
    readings = day_of(SUMMER_DAY, 3, solar_production=1.0)
    readings[1] = hourly(readings[1].start, solar_production=6790829.9)

    cleaned = clean_hourly(
        readings, [daily(SUMMER_DAY + 24 * HOUR, solar_production=5.0)], LIMITS
    )

    assert cleaned[1].solar_production is None


def test_an_unknown_daily_field_makes_the_value_unknown() -> None:
    readings = day_of(SUMMER_DAY, 3, solar_production=1.0)
    readings[1] = hourly(readings[1].start, solar_production=6790829.9)

    cleaned = clean_hourly(readings, [daily(SUMMER_DAY, solar_production=None)], LIMITS)

    assert cleaned[1].solar_production is None


def test_an_impossible_daily_total_makes_the_value_unknown() -> None:
    readings = day_of(SUMMER_DAY, 3, solar_production=1.0)
    readings[1] = hourly(readings[1].start, solar_production=6790829.9)
    # Above 25 hours at the solar limit (148.5 kWh).
    cleaned = clean_hourly(
        readings, [daily(SUMMER_DAY, solar_production=6790829.9)], LIMITS
    )
    assert cleaned[1].solar_production is None

    # The 25-hour limit itself is accepted.
    cleaned = clean_hourly(
        readings, [daily(SUMMER_DAY, solar_production=148.5)], LIMITS
    )
    assert cleaned[1].solar_production == pytest.approx(5.94)


def test_accepted_hours_above_the_daily_total_make_the_value_unknown() -> None:
    readings = day_of(SUMMER_DAY, 3, solar_production=2.0)
    readings[1] = hourly(readings[1].start, solar_production=6790829.9)

    cleaned = clean_hourly(readings, [daily(SUMMER_DAY, solar_production=3.9)], LIMITS)

    assert [r.solar_production for r in cleaned] == [2.0, None, 2.0]


def test_unknown_accepted_hours_do_not_count() -> None:
    readings = day_of(SUMMER_DAY, 3, solar_production=1.0)
    readings[0] = hourly(readings[0].start, solar_production=None)
    readings[1] = hourly(readings[1].start, solar_production=6790829.9)

    cleaned = clean_hourly(readings, [daily(SUMMER_DAY, solar_production=3.0)], LIMITS)

    assert [r.solar_production for r in cleaned] == [None, 2.0, 1.0]


def test_fields_are_cleaned_independently() -> None:
    readings = day_of(SUMMER_DAY, 3, solar_production=1.0, grid_import=2.0)
    readings[1] = hourly(readings[1].start, solar_production=6790829.9, grid_import=2.0)

    cleaned = clean_hourly(
        readings,
        [daily(SUMMER_DAY, solar_production=4.0, grid_import=99.0)],
        LIMITS,
    )

    assert [r.solar_production for r in cleaned] == [1.0, 2.0, 1.0]
    # The grid is untouched, even though its daily total disagrees.
    assert [r.grid_import for r in cleaned] == [2.0, 2.0, 2.0]


def test_grid_fields_use_the_grid_limit() -> None:
    readings = day_of(SUMMER_DAY, 2, ecs_consumption=1.0, solar_production=1.0)
    readings[1] = hourly(
        readings[1].start, ecs_consumption=378000.0, solar_production=1.0
    )

    cleaned = clean_hourly(readings, [daily(SUMMER_DAY, ecs_consumption=4.0)], LIMITS)

    assert [r.ecs_consumption for r in cleaned] == [1.0, 3.0]
    assert [r.solar_production for r in cleaned] == [1.0, 1.0]


def test_readings_are_unchanged_when_nothing_is_rejected() -> None:
    readings = day_of(SUMMER_DAY, 24, solar_production=1.0)

    cleaned = clean_hourly(readings, [daily(SUMMER_DAY, solar_production=24.0)], LIMITS)

    assert cleaned == readings


def test_each_local_day_uses_its_own_total() -> None:
    readings = day_of(SUMMER_DAY, 48, solar_production=0.0)
    readings[1] = hourly(readings[1].start, solar_production=999.0)
    readings[30] = hourly(readings[30].start, solar_production=999.0)

    cleaned = clean_hourly(
        readings,
        [
            daily(SUMMER_DAY, solar_production=2.0),
            daily(SUMMER_DAY + 24 * HOUR, solar_production=3.0),
        ],
        LIMITS,
    )

    assert cleaned[1].solar_production == 2.0
    assert cleaned[30].solar_production == 3.0


def test_real_world_day() -> None:
    """A meter index, then a Wh tail, while the day total stays right."""
    values = [0.0, 0.0, 6790829.9, 378.0, 322.0, 896.0, 0.0, 0.0]
    readings = [
        hourly(SUMMER_DAY + i * HOUR, solar_production=value)
        for i, value in enumerate(values)
    ]

    cleaned = clean_hourly(
        readings, [daily(SUMMER_DAY, solar_production=5.154)], LIMITS
    )

    share = round(5.154 / 4, 3)
    assert [r.solar_production for r in cleaned] == [
        0.0,
        0.0,
        share,
        share,
        share,
        share,
        0.0,
        0.0,
    ]
    assert sum(r.solar_production or 0.0 for r in cleaned) == pytest.approx(
        5.154, abs=0.002
    )


# -------------------------------------------------------- async_get_hourly_energy


def client_serving(*answers: list[EnergyReading]) -> AsyncMock:
    client = AsyncMock(spec=EdfEnrClient)
    client.async_get_energy.side_effect = list(answers)
    return client


async def test_clean_hours_cost_a_single_request() -> None:
    readings = day_of(SUMMER_DAY, 24, solar_production=1.0)
    client = client_serving(readings)
    end = SUMMER_DAY + 24 * HOUR

    result = await async_get_hourly_energy(client, "PV1", SUMMER_DAY, end, LIMITS)

    assert result == readings
    client.async_get_energy.assert_awaited_once_with(
        "PV1", Granularity.HOURLY, SUMMER_DAY, end
    )


async def test_rejected_hours_are_cleaned_with_the_daily_total_summer() -> None:
    readings = day_of(SUMMER_DAY, 24, solar_production=0.0)
    readings[10] = hourly(readings[10].start, solar_production=6790829.9)
    client = client_serving(readings, [daily(SUMMER_DAY, solar_production=2.5)])
    end = SUMMER_DAY + 24 * HOUR

    result = await async_get_hourly_energy(client, "PV1", SUMMER_DAY, end, LIMITS)

    assert client.async_get_energy.await_args_list[0].args == (
        "PV1",
        Granularity.HOURLY,
        SUMMER_DAY,
        end,
    )
    # Local midnights of 2026-10-24 and 2026-10-25 (the day clocks go back).
    assert client.async_get_energy.await_args_list[1].args == (
        "PV1",
        Granularity.DAILY,
        datetime(2026, 10, 23, 22, tzinfo=UTC),
        datetime(2026, 10, 24, 22, tzinfo=UTC),
    )
    assert result[10].solar_production == 2.5
    assert sum(r.solar_production or 0.0 for r in result) == pytest.approx(2.5)


async def test_rejected_hours_are_cleaned_with_the_daily_total_winter() -> None:
    readings = day_of(WINTER_DAY, 24, solar_production=0.0)
    readings[10] = hourly(readings[10].start, solar_production=378.0)
    client = client_serving(readings, [daily(WINTER_DAY, solar_production=1.2)])

    result = await async_get_hourly_energy(
        client, "PV1", WINTER_DAY, WINTER_DAY + 24 * HOUR, LIMITS
    )

    assert client.async_get_energy.await_args_list[1].args == (
        "PV1",
        Granularity.DAILY,
        datetime(2026, 1, 14, 23, tzinfo=UTC),
        datetime(2026, 1, 15, 23, tzinfo=UTC),
    )
    assert result[10].solar_production == 1.2


async def test_the_daily_request_spans_the_rejected_days() -> None:
    first = SUMMER_DAY - 24 * HOUR  # local 2026-10-23
    # 73 hours: the clocks go back on 2026-10-25.
    readings = day_of(first, 73, solar_production=0.0)
    readings[2] = hourly(readings[2].start, solar_production=999.0)
    readings[50] = hourly(readings[50].start, solar_production=999.0)
    client = client_serving(
        readings,
        [
            daily(first, solar_production=1.0),
            daily(SUMMER_DAY + 24 * HOUR, solar_production=2.0),
        ],
    )

    result = await async_get_hourly_energy(
        client, "PV1", first, first + 73 * HOUR, LIMITS
    )

    # The untouched middle day is within the requested range.
    _, granularity, start, end = client.async_get_energy.await_args_list[1].args
    assert granularity is Granularity.DAILY
    assert start == datetime(2026, 10, 22, 22, tzinfo=UTC)
    # Midnight after 2026-10-25, the clocks went back that day.
    assert end == datetime(2026, 10, 25, 23, tzinfo=UTC)
    assert result[2].solar_production == 1.0
    assert result[50].solar_production == 2.0


async def test_rejected_days_are_completed_outside_the_requested_range() -> None:
    """A range starting mid-day still shares the daily total over the whole day."""
    whole_day = day_of(SUMMER_DAY, 24, solar_production=0.5)
    whole_day[14] = hourly(whole_day[14].start, solar_production=6790829.9)
    start = SUMMER_DAY + 12 * HOUR
    client = client_serving(
        whole_day[12:],
        [daily(SUMMER_DAY, solar_production=12.5)],
        whole_day,
    )

    result = await async_get_hourly_energy(
        client, "PV1", start, SUMMER_DAY + 24 * HOUR, LIMITS
    )

    assert client.async_get_energy.await_args_list[2].args == (
        "PV1",
        Granularity.HOURLY,
        SUMMER_DAY,
        SUMMER_DAY + 24 * HOUR,
    )
    assert [r.start for r in result] == [r.start for r in whole_day[12:]]
    # 23 accepted hours of 0.5 kWh: 1.0 kWh is left for the rejected one.
    assert result[2].solar_production == 1.0
