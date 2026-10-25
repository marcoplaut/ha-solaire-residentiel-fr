"""Tests for the API client data models and parsers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.edf_enr.api.exceptions import EdfEnrApiError
from custom_components.edf_enr.api.models import (
    Device,
    EnergyReading,
    Equipment,
    Granularity,
    Project,
    format_api_datetime,
    parse_api_datetime,
    parse_device,
    parse_energy_reading,
    parse_epoch,
    parse_equipment,
    parse_iso_utc,
    parse_project,
)

from .conftest import load_json_fixture


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Summer time (CEST, UTC+2).
        ("2026-07-15T12:00:00Z", datetime(2026, 7, 15, 10, 0, tzinfo=UTC)),
        # Winter time (CET, UTC+1).
        ("2026-01-15T12:00:00Z", datetime(2026, 1, 15, 11, 0, tzinfo=UTC)),
        # Without the bogus suffix.
        ("2026-01-15T12:00:00", datetime(2026, 1, 15, 11, 0, tzinfo=UTC)),
        # Spring forward: 02:00 does not exist, the pre-transition offset applies.
        ("2026-03-29T02:00:00Z", datetime(2026, 3, 29, 1, 0, tzinfo=UTC)),
        ("2026-03-29T01:00:00Z", datetime(2026, 3, 29, 0, 0, tzinfo=UTC)),
        ("2026-03-29T03:00:00Z", datetime(2026, 3, 29, 1, 0, tzinfo=UTC)),
        # Fall back: 02:00 is ambiguous, the first occurrence (CEST) applies.
        ("2025-10-26T01:00:00Z", datetime(2025, 10, 25, 23, 0, tzinfo=UTC)),
        ("2025-10-26T02:00:00Z", datetime(2025, 10, 26, 0, 0, tzinfo=UTC)),
        ("2025-10-26T03:00:00Z", datetime(2025, 10, 26, 2, 0, tzinfo=UTC)),
    ],
)
def test_parse_api_datetime(value: str, expected: datetime) -> None:
    result = parse_api_datetime(value)
    assert result == expected
    assert result.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "garbage",
        "2026-13-45T00:00:00Z",
        "Z",
        # Out of range once converted to UTC (Paris was UTC+00:09 in year 1).
        "0001-01-01T00:00:00Z",
    ],
)
def test_parse_api_datetime_invalid(value: str) -> None:
    with pytest.raises(EdfEnrApiError, match="Invalid timestamp"):
        parse_api_datetime(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 7, 15, 10, 0, tzinfo=UTC), "2026-07-15T12:00:00Z"),
        (datetime(2026, 1, 15, 11, 0, tzinfo=UTC), "2026-01-15T12:00:00Z"),
        (datetime(2026, 3, 29, 1, 0, tzinfo=UTC), "2026-03-29T03:00:00Z"),
        (datetime(2025, 10, 26, 0, 30, tzinfo=UTC), "2025-10-26T02:30:00Z"),
        (
            datetime(2026, 7, 15, 12, 0, tzinfo=timezone(timedelta(hours=-5))),
            "2026-07-15T19:00:00Z",
        ),
    ],
)
def test_format_api_datetime(value: datetime, expected: str) -> None:
    assert format_api_datetime(value) == expected


def test_format_api_datetime_naive() -> None:
    with pytest.raises(ValueError, match="timezone aware"):
        format_api_datetime(datetime(2026, 7, 15, 12, 0))  # noqa: DTZ001


def test_format_parse_round_trip() -> None:
    value = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    assert parse_api_datetime(format_api_datetime(value)) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-05-14T22:00:00Z", datetime(2024, 5, 14, 22, 0, tzinfo=UTC)),
        ("2024-05-14T22:00:00.000Z", datetime(2024, 5, 14, 22, 0, tzinfo=UTC)),
        ("2024-05-15T00:00:00+02:00", datetime(2024, 5, 14, 22, 0, tzinfo=UTC)),
        # Naive strings are taken as UTC.
        ("2024-05-14T22:00:00", datetime(2024, 5, 14, 22, 0, tzinfo=UTC)),
        (None, None),
        ("", None),
        ("garbage", None),
        (12345, None),
        (True, None),
        ({}, None),
        # Out-of-range sentinels.
        ("0001-01-01T00:00:00+01:00", None),
        ("9999-12-31T23:59:59-01:00", None),
    ],
)
def test_parse_iso_utc(value: Any, expected: datetime | None) -> None:
    assert parse_iso_utc(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, datetime(1970, 1, 1, tzinfo=UTC)),
        (1792867830, datetime.fromtimestamp(1792867830, UTC)),
        (1.5, datetime(1970, 1, 1, 0, 0, 1, 500000, tzinfo=UTC)),
        (None, None),
        (True, None),
        (False, None),
        ("1792867830", None),
        ([], None),
        # Milliseconds, or otherwise out of range.
        (1792867830123, None),
        (float("inf"), None),
    ],
)
def test_parse_epoch(value: Any, expected: datetime | None) -> None:
    assert parse_epoch(value) == expected


def test_parse_energy_reading() -> None:
    reading = parse_energy_reading(
        {
            "dateStart": "2026-07-15T12:00:00Z",
            "energies": {
                "solarProduction": 2.39,
                "autoConsumption": 1,
                "networkConsumption": 0.12345,
                "ecsConsumption": 0.0,
            },
        },
        Granularity.HOURLY,
    )
    assert reading == EnergyReading(
        start=datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        granularity=Granularity.HOURLY,
        solar_production=2.39,
        self_consumption=1.0,
        grid_import=0.123,
        ecs_consumption=0.0,
    )


def test_parse_energy_reading_zero_sentinel() -> None:
    """Like the customer area web app, -0.1 means 0 (not unknown)."""
    reading = parse_energy_reading(
        {
            "dateStart": "2026-07-15T12:00:00Z",
            "energies": {
                "solarProduction": 2.0,
                "autoConsumption": -0.1,
                "networkConsumption": 0.3,
                "ecsConsumption": -0.1,
            },
        },
        Granularity.HOURLY,
    )
    assert reading.self_consumption == 0.0
    assert reading.ecs_consumption == 0.0
    assert reading.grid_injection == 2.0
    assert reading.home_consumption == 0.3


def test_parse_energy_reading_fixture_negative_placeholder() -> None:
    items = load_json_fixture("hourly.json")["hourlyElecEnergies"]
    readings = [parse_energy_reading(item, Granularity.HOURLY) for item in items]
    assert readings[0].solar_production == 2.39
    assert readings[2].solar_production is None
    assert readings[2].self_consumption is None
    assert readings[2].grid_import is None
    assert readings[2].ecs_consumption == 0.0


@pytest.mark.parametrize(
    "energies",
    [
        None,
        "nope",
        [],
        {},
        {"solarProduction": None},
        {"solarProduction": True, "autoConsumption": False},
        {"solarProduction": "1.0"},
        {"solarProduction": -0.001},
    ],
)
def test_parse_energy_reading_missing_or_invalid_energies(energies: Any) -> None:
    item: dict[str, Any] = {"dateStart": "2026-07-15T12:00:00Z"}
    if energies is not None:
        item["energies"] = energies
    reading = parse_energy_reading(item, Granularity.DAILY)
    assert reading.granularity is Granularity.DAILY
    assert reading.solar_production is None
    assert reading.self_consumption is None
    assert reading.grid_import is None
    assert reading.ecs_consumption is None


@pytest.mark.parametrize(
    "item",
    [
        None,
        "x",
        [],
        {},
        {"energies": {}},
        {"dateStart": None},
        {"dateStart": 12},
    ],
)
def test_parse_energy_reading_without_date_start(item: Any) -> None:
    with pytest.raises(EdfEnrApiError, match="dateStart"):
        parse_energy_reading(item, Granularity.HOURLY)


def test_parse_energy_reading_invalid_date_start() -> None:
    with pytest.raises(EdfEnrApiError, match="Invalid timestamp"):
        parse_energy_reading({"dateStart": "garbage"}, Granularity.HOURLY)


def _reading(
    solar: float | None = None,
    own: float | None = None,
    grid: float | None = None,
) -> EnergyReading:
    return EnergyReading(
        start=datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        granularity=Granularity.HOURLY,
        solar_production=solar,
        self_consumption=own,
        grid_import=grid,
        ecs_consumption=None,
    )


@pytest.mark.parametrize(
    ("solar", "own", "expected"),
    [
        (3.0, 1.0, 2.0),
        (1.0, 1.0, 0.0),
        # Clamped at zero.
        (1.0, 1.5, 0.0),
        (None, 1.0, None),
        (3.0, None, None),
        (None, None, None),
    ],
)
def test_grid_injection(
    solar: float | None, own: float | None, expected: float | None
) -> None:
    assert _reading(solar=solar, own=own).grid_injection == expected


@pytest.mark.parametrize(
    ("own", "grid", "expected"),
    [
        (1.0, 0.5, 1.5),
        (0.0, 0.0, 0.0),
        (None, 0.5, None),
        (1.0, None, None),
        (None, None, None),
    ],
)
def test_home_consumption(
    own: float | None, grid: float | None, expected: float | None
) -> None:
    assert _reading(own=own, grid=grid).home_consumption == expected


@pytest.mark.parametrize(
    ("granularity", "unit", "key"),
    [
        (Granularity.MINUTELY, "minute", "minutelyElecEnergies"),
        (Granularity.HOURLY, "hour", "hourlyElecEnergies"),
        (Granularity.DAILY, "day", "dailyElecEnergies"),
        (Granularity.MONTHLY, "month", "monthlyElecEnergies"),
        (Granularity.YEARLY, "year", "yearlyElecEnergies"),
    ],
)
def test_granularity(granularity: Granularity, unit: str, key: str) -> None:
    assert granularity.unit == unit
    assert granularity.response_key == key


def test_parse_project_fixture() -> None:
    items = load_json_fixture("contact_with_opportunities.json")["opportunities"]
    assert parse_project(items[0]) == Project(
        pv_number="PV0000042",
        name="Jane Doe - PV0000042",
        installed_power_kwp=3.96,
        commissioning_date=datetime(2024, 5, 14, 22, 0, tzinfo=UTC),
        offer_name="Mon Soleil & Moi toiture V3.1",
    )
    assert parse_project(items[1]) == Project(
        pv_number="PV0000042",
        name="Jane Doe - battery upsell",
        installed_power_kwp=None,
        commissioning_date=None,
        offer_name="Batterie",
    )
    assert parse_project(items[2]) is None


@pytest.mark.parametrize(
    "item",
    [None, "x", [], {}, {"pvNumber": None}, {"pvNumber": ""}, {"pvNumber": 42}],
)
def test_parse_project_unusable(item: Any) -> None:
    assert parse_project(item) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"name": 5, "offer": "x", "powerInstallation": "3.9"},
        {"name": None, "offer": None, "powerInstallation": True},
        {"offer": {"name": 3}, "b2CCommissioningDate": "garbage"},
        {"offer": [], "powerInstallation": [], "b2CCommissioningDate": 12},
    ],
)
def test_parse_project_robust(extra: dict[str, Any]) -> None:
    project = parse_project({"pvNumber": "PV1", **extra})
    assert project == Project("PV1", None, None, None, None)


def test_parse_project_integer_power() -> None:
    project = parse_project({"pvNumber": "PV1", "powerInstallation": 4})
    assert project is not None
    assert project.installed_power_kwp == 4.0
    assert isinstance(project.installed_power_kwp, float)


def test_parse_equipment_fixture() -> None:
    items = load_json_fixture("equipements.json")["equipements"]
    assert parse_equipment(items[0]) == Equipment(
        equipment_id="000042|yuzeg1",
        type="Passerelle",
        sub_type="YuzeG1",
        brand="Fournisseur solaire",
        last_connection=datetime(2026, 10, 24, 18, 50, 30, tzinfo=UTC),
    )
    heater = parse_equipment(items[1])
    assert heater is not None
    assert heater.last_connection is None


@pytest.mark.parametrize("item", [None, "x", [], {}, {"idEquipement": 3}])
def test_parse_equipment_unusable(item: Any) -> None:
    assert parse_equipment(item) is None


@pytest.mark.parametrize(
    "item",
    [
        {"idEquipement": "e1"},
        {"idEquipement": "e1", "type": None, "sousType": 1, "marque": 2},
        {"idEquipement": "e1", "type": "", "dateDerniereConnexion": "garbage"},
    ],
)
def test_parse_equipment_defaults(item: dict[str, Any]) -> None:
    assert parse_equipment(item) == Equipment("e1", "unknown", None, None, None)


def test_parse_device_fixture() -> None:
    assert parse_device(load_json_fixture("device.json")) == Device(
        device_id="000042|yuzeg1",
        type="YuzeG1",
        status="MisEnService",
        last_reported_at=datetime.fromtimestamp(1792867830, UTC),
    )


def test_parse_device_minimal() -> None:
    assert parse_device(
        {"id": "d1", "type": 3, "status": None, "lastReportedAt": "x"}
    ) == Device("d1", None, None, None)


@pytest.mark.parametrize("item", [None, "x", [], {}, {"id": 3}])
def test_parse_device_without_id(item: Any) -> None:
    with pytest.raises(EdfEnrApiError, match="Device without id"):
        parse_device(item)
