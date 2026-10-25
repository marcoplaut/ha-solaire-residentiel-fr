"""Tests for the data coordinator."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, PropertyMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.edf_enr.api import (
    EdfEnrApiError,
    EdfEnrAuthError,
    EdfEnrConnectionError,
    EdfEnrNotFoundError,
    EdfEnrRateLimitError,
    EnergyReading,
    Granularity,
)
from custom_components.edf_enr.const import DOMAIN, UPDATE_INTERVAL
from custom_components.edf_enr.coordinator import (
    RATE_LIMIT_MIN_RETRY_AFTER,
    RATE_LIMIT_RETRY_AFTER,
    EdfEnrCoordinator,
    EnergyTotals,
)
from custom_components.edf_enr.statistics import EdfEnrStatisticsImporter

from .conftest import FakeTelemetry
from .const import NOW, PV_NUMBER


def _reading(
    solar: float | None,
    self_consumption: float | None,
    grid_import: float | None,
) -> EnergyReading:
    return EnergyReading(
        start=NOW,
        granularity=Granularity.HOURLY,
        solar_production=solar,
        self_consumption=self_consumption,
        grid_import=grid_import,
        ecs_consumption=None,
    )


def _state(hass: HomeAssistant, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{PV_NUMBER}_{key}"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta
) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def test_totals_from_readings() -> None:
    """Known values are summed and rounded, unknown ones are ignored."""
    totals = EnergyTotals.from_readings(
        [
            _reading(1.0004, 0.5, 0.25),
            _reading(2.0004, 0.5004, None),
            _reading(None, None, 0.1),
            _reading(0.1, None, 0.2),
        ]
    )

    assert totals == EnergyTotals(
        solar_production=3.101,
        self_consumption=1.0,
        grid_import=0.55,
        # Only the first two readings know both production and self consumption.
        grid_injection=round(0.5004 + 1.5, 3),
        home_consumption=0.75,
    )


def test_totals_from_no_readings() -> None:
    """No reading gives zero totals and unknown rates."""
    totals = EnergyTotals.from_readings([])

    assert totals == EnergyTotals(0.0, 0.0, 0.0, 0.0, 0.0)
    assert totals.self_consumption_rate is None
    assert totals.self_sufficiency_rate is None


def test_rates() -> None:
    """Rates are percentages of production and consumption."""
    totals = EnergyTotals(
        solar_production=8.0,
        self_consumption=2.0,
        grid_import=4.0,
        grid_injection=6.0,
        home_consumption=6.0,
    )

    assert totals.self_consumption_rate == 25.0
    assert totals.self_sufficiency_rate == 33.3


def test_rates_are_clamped_at_100() -> None:
    """Rounding or portal glitches never give more than 100 %."""
    totals = EnergyTotals(
        solar_production=1.0,
        self_consumption=1.2,
        grid_import=0.0,
        grid_injection=0.0,
        home_consumption=1.0,
    )

    assert totals.self_consumption_rate == 100.0
    assert totals.self_sufficiency_rate == 100.0


def test_rates_without_production_or_consumption() -> None:
    """Rates are unknown rather than a division by zero."""
    totals = EnergyTotals(0.0, 0.0, 1.0, 0.0, 0.0)

    assert totals.self_consumption_rate is None
    assert totals.self_sufficiency_rate is None


async def test_first_refresh_requests_the_local_day(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: AsyncMock,
    telemetry: FakeTelemetry,
) -> None:
    """Today's energy is requested hour by hour for the Paris local day."""
    granularity, start, end = telemetry.calls[0]
    assert granularity is Granularity.HOURLY
    assert start == datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    mock_client.async_get_equipments.assert_awaited_once_with(
        init_integration.runtime_data.installation.installation_id
    )


async def test_scheduled_refresh_updates_sensors(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    telemetry: FakeTelemetry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A refresh after the update interval picks up new measurements."""
    # Keep the hour that starts during the tick unreported.
    telemetry.horizon = dt_util.utcnow()
    before = float(_state(hass, "solar_production_today"))
    telemetry.overrides[datetime(2026, 10, 24, 9, 0, tzinfo=UTC)] = _reading(
        10.0, 1.0, 0.0
    )

    await _tick(hass, freezer, UPDATE_INTERVAL)

    assert init_integration.runtime_data.last_update_success
    after = float(_state(hass, "solar_production_today"))
    assert after == pytest.approx(before - 0.4 + 10.0)


async def test_refresh_error_then_recovery(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: AsyncMock,
    telemetry: FakeTelemetry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Entities become unavailable on errors and come back afterwards."""
    assert _state(hass, "solar_production_today") != STATE_UNAVAILABLE
    mock_client.async_get_energy.side_effect = EdfEnrConnectionError

    await _tick(hass, freezer, UPDATE_INTERVAL)

    coordinator = init_integration.runtime_data
    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception, UpdateFailed)
    for key in ("solar_production_today", "gateway_last_report", "installed_power"):
        assert _state(hass, key) == STATE_UNAVAILABLE

    mock_client.async_get_energy.side_effect = telemetry.__call__
    await _tick(hass, freezer, UPDATE_INTERVAL)

    assert coordinator.last_update_success
    assert _state(hass, "solar_production_today") != STATE_UNAVAILABLE
    assert _state(hass, "installed_power") == "3.96"


async def test_unexpected_response_is_an_update_failure(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A response the client cannot parse fails the refresh with details."""
    mock_client.async_get_energy.side_effect = EdfEnrApiError("bad payload")

    await _tick(hass, freezer, UPDATE_INTERVAL)

    coordinator = init_integration.runtime_data
    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.last_exception.translation_key == "unexpected_response"
    assert coordinator.last_exception.translation_placeholders == {
        "error": "bad payload"
    }


async def test_auth_error_during_refresh_starts_reauth(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Rejected credentials on a later refresh start a reauth flow."""
    assert not hass.config_entries.flow.async_progress()
    mock_client.async_get_energy.side_effect = EdfEnrAuthError

    await _tick(hass, freezer, UPDATE_INTERVAL)

    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert flows[0]["context"]["entry_id"] == init_integration.entry_id
    assert init_integration.state is ConfigEntryState.LOADED


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        (120.0, 120.0),
        (None, RATE_LIMIT_RETRY_AFTER),
        # A tiny server delay must not turn into a tight retry loop.
        (1.0, RATE_LIMIT_MIN_RETRY_AFTER),
    ],
)
async def test_rate_limit_retries_after_the_suggested_delay(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: AsyncMock,
    telemetry: FakeTelemetry,
    freezer: FrozenDateTimeFactory,
    retry_after: float | None,
    expected: float,
) -> None:
    """A 429 reschedules the refresh after the server delay or a default."""
    coordinator = init_integration.runtime_data
    mock_client.async_get_energy.side_effect = EdfEnrRateLimitError(retry_after)

    await _tick(hass, freezer, UPDATE_INTERVAL)

    assert not coordinator.last_update_success
    exception = coordinator.last_exception
    assert isinstance(exception, UpdateFailed)
    assert exception.retry_after == expected
    assert exception.translation_key == "rate_limited"

    # Not retried before the delay, retried right after.
    mock_client.async_get_energy.side_effect = telemetry.__call__
    calls = mock_client.async_get_energy.await_count
    await _tick(hass, freezer, timedelta(seconds=expected - 1))
    assert mock_client.async_get_energy.await_count == calls
    await _tick(hass, freezer, timedelta(seconds=2))
    assert mock_client.async_get_energy.await_count == calls + 1
    assert coordinator.last_update_success


async def test_gateway_not_found(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A gateway the portal no longer knows only makes its sensors unknown."""
    assert _state(hass, "gateway_last_report") != STATE_UNKNOWN
    mock_client.async_get_device.side_effect = EdfEnrNotFoundError

    await _tick(hass, freezer, UPDATE_INTERVAL)

    coordinator = init_integration.runtime_data
    assert coordinator.last_update_success
    assert coordinator.data.gateway is None
    assert _state(hass, "gateway_last_report") == STATE_UNKNOWN
    assert _state(hass, "solar_production_today") != STATE_UNKNOWN


async def test_gateway_error_fails_the_refresh(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Other gateway errors are update failures; the statistics still update."""
    mock_client.async_get_device.side_effect = EdfEnrConnectionError

    with patch.object(
        EdfEnrStatisticsImporter, "async_import", new_callable=AsyncMock
    ) as mock_import:
        await _tick(hass, freezer, UPDATE_INTERVAL)

    assert not init_integration.runtime_data.last_update_success
    mock_import.assert_awaited_once_with()


async def test_statistics_import_runs_on_each_refresh(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every refresh schedules an incremental statistics import."""
    with patch.object(
        EdfEnrStatisticsImporter, "async_import", new_callable=AsyncMock
    ) as mock_import:
        await _tick(hass, freezer, UPDATE_INTERVAL)

    mock_import.assert_awaited_once_with()


async def test_statistics_import_not_scheduled_while_running(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No second import is started while one is running."""
    with (
        patch.object(
            EdfEnrStatisticsImporter,
            "running",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch.object(
            EdfEnrStatisticsImporter, "async_import", new_callable=AsyncMock
        ) as mock_import,
    ):
        await _tick(hass, freezer, UPDATE_INTERVAL)

    mock_import.assert_not_called()
    assert init_integration.runtime_data.last_update_success


async def test_coordinator_attributes(init_integration: MockConfigEntry) -> None:
    """The coordinator exposes the discovered project and gateway."""
    coordinator: EdfEnrCoordinator = init_integration.runtime_data

    assert coordinator.pv_number == PV_NUMBER
    assert coordinator.project.pv_number == PV_NUMBER
    assert coordinator.gateway_equipment is not None
    assert coordinator.gateway_equipment.type == "Passerelle"
    assert coordinator.update_interval == UPDATE_INTERVAL
