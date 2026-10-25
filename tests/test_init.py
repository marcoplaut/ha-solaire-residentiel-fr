"""Tests for the setup and teardown of the integration."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.edf_enr.api import (
    EdfEnrAuthError,
    EdfEnrClient,
    EdfEnrConnectionError,
    EdfEnrRateLimitError,
)
from custom_components.edf_enr.const import DOMAIN
from custom_components.edf_enr.helpers import create_client, create_client_from_data
from custom_components.edf_enr.statistics import statistic_ids

from .conftest import PROJECT
from .const import EMAIL, ENTRY_DATA, OTHER_PV_NUMBER, PASSWORD, PV_NUMBER


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The entry loads and unloads cleanly."""
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, "reimport_statistics")

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_devices(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    init_integration: MockConfigEntry,
) -> None:
    """The installation is a service device, the gateway hangs off it."""
    installation = device_registry.async_get_device_by_identifier(
        (DOMAIN, PV_NUMBER), init_integration.entry_id
    )
    assert installation is not None
    assert installation.entry_type is dr.DeviceEntryType.SERVICE
    assert installation.manufacturer == "Solaire Résidentiel FR"
    assert installation.model == PROJECT.offer_name
    assert installation.serial_number == PV_NUMBER

    gateway = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{PV_NUMBER}_gateway"), init_integration.entry_id
    )
    assert gateway is not None
    assert gateway.via_device_id == installation.id
    assert gateway.model == "YuzeG1"
    assert gateway.manufacturer == "Solaire Résidentiel FR"
    assert gateway.name == "Monitoring gateway"


async def test_no_installation(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Without installation there is no gateway device nor gateway entity."""
    mock_client.async_get_installation.return_value = None
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    mock_client.async_get_equipments.assert_not_called()
    mock_client.async_get_device.assert_not_called()
    keys = {
        e.unique_id
        for e in er.async_entries_for_config_entry(
            entity_registry, mock_config_entry.entry_id
        )
    }
    assert f"{PV_NUMBER}_solar_production_today" in keys
    assert not {k for k in keys if "gateway" in k}
    assert (
        device_registry.async_get_device_by_identifier(
            (DOMAIN, f"{PV_NUMBER}_gateway"), mock_config_entry.entry_id
        )
        is None
    )


async def test_no_gateway_equipment(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An installation without gateway only has the installation entities."""
    mock_client.async_get_equipments.return_value = []
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    mock_client.async_get_device.assert_not_called()
    assert not [
        e
        for e in er.async_entries_for_config_entry(
            entity_registry, mock_config_entry.entry_id
        )
        if "gateway" in e.unique_id
    ]


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Rejected credentials put the entry in error and start a reauth flow."""
    mock_client.async_get_projects.side_effect = EdfEnrAuthError
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert flows[0]["context"]["entry_id"] == mock_config_entry.entry_id


async def test_auth_error_while_fetching_energy(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A rejection on a later request also starts a reauth flow."""
    mock_client.async_get_energy.side_effect = EdfEnrAuthError
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(hass.config_entries.flow.async_progress()) == 1


@pytest.mark.parametrize(
    "exception", [EdfEnrConnectionError, EdfEnrRateLimitError(retry_after=60)]
)
async def test_connection_errors_retry(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    exception: Exception,
) -> None:
    """Network errors and rate limits are retried later."""
    mock_client.async_get_projects.side_effect = exception
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


async def test_project_not_found(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A project that vanished from the account is a setup error."""
    mock_client.async_get_projects.return_value = [
        replace(PROJECT, pv_number=OTHER_PV_NUMBER)
    ]
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert PV_NUMBER in (mock_config_entry.reason or "")


async def test_remove_entry_clears_statistics(
    hass: HomeAssistant, recorder_mock: Recorder, init_integration: MockConfigEntry
) -> None:
    """Removing the entry deletes the imported statistics."""
    entry = init_integration
    await async_wait_recording_done(hass)
    ids = set(statistic_ids(PV_NUMBER))
    present = {s["statistic_id"] for s in await list_statistic_ids_(hass)}
    assert ids <= present

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    present = {s["statistic_id"] for s in await list_statistic_ids_(hass)}
    assert not ids & present


async def test_remove_entry_when_the_recorder_cannot_clear(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The removal completes even if the recorder never confirms the deletion."""
    await async_wait_recording_done(hass)
    with (
        patch(
            "homeassistant.components.recorder.statistics.clear_statistics",
            side_effect=SQLAlchemyError("database is locked"),
        ),
        patch("custom_components.edf_enr.statistics.CLEAR_TIMEOUT", 0),
    ):
        await hass.config_entries.async_remove(init_integration.entry_id)
        await async_wait_recording_done(hass)

    assert hass.config_entries.async_get_entry(init_integration.entry_id) is None
    assert "did not confirm the deletion of the statistics" in caplog.text


async def test_remove_entry_calls_clear(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The statistics of the removed project are the ones cleared."""
    with patch(
        "custom_components.edf_enr.async_clear_statistics", new_callable=AsyncMock
    ) as mock_clear:
        await hass.config_entries.async_remove(init_integration.entry_id)

    mock_clear.assert_awaited_once_with(hass, PV_NUMBER)


async def list_statistic_ids_(hass: HomeAssistant) -> list[dict]:
    """List the statistic ids from the recorder executor."""
    return await get_instance(hass).async_add_executor_job(list_statistic_ids, hass)


async def test_create_client_uses_a_dedicated_session(hass: HomeAssistant) -> None:
    """Each client gets its own session with an unquoting cookie jar."""
    first = create_client_from_data(hass, ENTRY_DATA)
    second = create_client(hass, EMAIL, PASSWORD)

    assert isinstance(first, EdfEnrClient)
    assert first is not second
    assert first._session is not second._session
    assert first._session.cookie_jar is not second._session.cookie_jar
    assert first._session.cookie_jar.quote_cookie is False
