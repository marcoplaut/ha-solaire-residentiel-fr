"""Tests for the sensors."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)

from custom_components.edf_enr.const import DOMAIN

from .conftest import PROJECT, FakeTelemetry, hourly_profile
from .const import NOW, PV_NUMBER

# Paris is UTC+2 on the frozen day, so the local day starts at 22:00 UTC.
DAY_START = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)

ENERGY_KEYS = (
    "solar_production_today",
    "grid_import_today",
    "grid_injection_today",
    "self_consumption_today",
    "home_consumption_today",
)


def _expected_totals() -> dict[str, float]:
    """Sum the profile over the local day up to the frozen time."""
    totals = dict.fromkeys(ENERGY_KEYS, 0.0)
    hour = DAY_START
    while hour <= NOW:
        reading = hourly_profile(hour)
        totals["solar_production_today"] += reading.solar_production
        totals["grid_import_today"] += reading.grid_import
        totals["grid_injection_today"] += reading.grid_injection
        totals["self_consumption_today"] += reading.self_consumption
        totals["home_consumption_today"] += reading.home_consumption
        hour += timedelta(hours=1)
    return {key: round(value, 3) for key, value in totals.items()}


def _entity_id(hass: HomeAssistant, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{PV_NUMBER}_{key}"
    )
    assert entity_id is not None, key
    return entity_id


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_all_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """Snapshot every entity, including the ones disabled by default."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


async def test_today_totals(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The sensors show the profile summed over the local day so far."""
    expected = _expected_totals()
    # Guard the arithmetic of the helper itself: 9h..12h UTC produce 6 kWh.
    assert expected["solar_production_today"] == pytest.approx(6.0)

    for key, value in expected.items():
        assert float(hass.states.get(_entity_id(hass, key)).state) == pytest.approx(
            value
        ), key

    production = expected["solar_production_today"]
    self_consumption = expected["self_consumption_today"]
    home = expected["home_consumption_today"]
    rate = hass.states.get(_entity_id(hass, "self_consumption_rate_today"))
    sufficiency = hass.states.get(_entity_id(hass, "self_sufficiency_rate_today"))
    assert float(rate.state) == pytest.approx(
        round(self_consumption / production * 100, 1)
    )
    assert float(sufficiency.state) == pytest.approx(
        round(self_consumption / home * 100, 1)
    )


async def test_rates_before_sunrise(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    telemetry: FakeTelemetry,
) -> None:
    """Before sunrise the home consumes from the grid only."""
    telemetry.horizon = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(_entity_id(hass, "solar_production_today")).state == "0.0"
    rate = hass.states.get(_entity_id(hass, "self_consumption_rate_today"))
    sufficiency = hass.states.get(_entity_id(hass, "self_sufficiency_rate_today"))
    assert rate.state == STATE_UNKNOWN
    assert sufficiency.state == "0.0"


async def test_rates_without_any_measurement(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    telemetry: FakeTelemetry,
) -> None:
    """Both rates are unknown when nothing was reported yet."""
    telemetry.horizon = DAY_START
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    for key in ("self_consumption_rate_today", "self_sufficiency_rate_today"):
        assert hass.states.get(_entity_id(hass, key)).state == STATE_UNKNOWN


async def test_energy_sensors_have_no_state_class(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Energy values are display only: the Energy dashboard uses statistics."""
    for key in ENERGY_KEYS:
        state = hass.states.get(_entity_id(hass, key))
        assert ATTR_STATE_CLASS not in state.attributes, key
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "kWh"
    rate = hass.states.get(_entity_id(hass, "self_consumption_rate_today"))
    assert rate.attributes[ATTR_STATE_CLASS] == "measurement"


async def test_installed_power(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, init_integration
) -> None:
    """The peak power is a diagnostic sensor in kW."""
    entity_id = _entity_id(hass, "installed_power")
    state = hass.states.get(entity_id)

    assert state.state == "3.96"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "kW"
    assert entity_registry.async_get(entity_id).entity_category is (
        EntityCategory.DIAGNOSTIC
    )


async def test_no_installed_power_sensor_when_unknown(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The installed power sensor only exists when the portal tells the power."""
    mock_client.async_get_projects.return_value = [
        replace(PROJECT, installed_power_kwp=None)
    ]
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert (
        entity_registry.async_get_entity_id(
            "sensor", DOMAIN, f"{PV_NUMBER}_installed_power"
        )
        is None
    )
    assert _entity_id(hass, "solar_production_today")


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_gateway_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The gateway sensors report its last contact and status."""
    last_report = hass.states.get(_entity_id(hass, "gateway_last_report"))
    status = hass.states.get(_entity_id(hass, "gateway_status"))

    assert last_report.state == "2026-10-24T18:50:30+00:00"
    assert status.state == "MisEnService"


async def test_gateway_status_disabled_by_default(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    init_integration: MockConfigEntry,
) -> None:
    """The gateway status is registered but disabled."""
    entity_id = _entity_id(hass, "gateway_status")

    assert entity_registry.async_get(entity_id).disabled
    assert hass.states.get(entity_id) is None
