"""Tests for the diagnostics."""

import json

from syrupy.assertion import SnapshotAssertion
from syrupy.filters import props

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from .const import EMAIL, GATEWAY_ID, INSTALLATION_ID, PASSWORD, PV_NUMBER


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    """Diagnostics are stable and redacted."""
    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, init_integration
    )

    assert diagnostics == snapshot(
        exclude=props("entry_id", "created_at", "modified_at")
    )

    dump = json.dumps(diagnostics)
    for secret in (EMAIL, PASSWORD, PV_NUMBER, INSTALLATION_ID, GATEWAY_ID):
        assert secret not in dump


async def test_diagnostics_without_installation(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Diagnostics work for a project without installation."""
    mock_client.async_get_installation.return_value = None
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )

    assert diagnostics["installation"] is None
    assert diagnostics["gateway_equipment"] is None
    assert diagnostics["data"]["gateway"] is None
    assert PV_NUMBER not in json.dumps(diagnostics)
