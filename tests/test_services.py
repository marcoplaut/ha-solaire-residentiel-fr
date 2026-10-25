"""Tests for the integration services."""

from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edf_enr.const import (
    ATTR_CONFIG_ENTRY_ID,
    DOMAIN,
    SERVICE_REIMPORT_STATISTICS,
)
from custom_components.edf_enr.statistics import EdfEnrStatisticsImporter


async def _reimport(hass: HomeAssistant, entry_id: str) -> None:
    await hass.services.async_call(
        DOMAIN,
        SERVICE_REIMPORT_STATISTICS,
        {ATTR_CONFIG_ENTRY_ID: entry_id},
        blocking=True,
    )


async def test_reimport_statistics(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The statistics are deleted, then fully imported again."""
    importer = init_integration.runtime_data.statistics
    running_during_clear: list[bool] = []

    async def clear() -> None:
        running_during_clear.append(importer.running)

    manager = Mock()
    manager.clear = AsyncMock(side_effect=clear)
    manager.import_ = AsyncMock()
    with (
        patch.object(EdfEnrStatisticsImporter, "async_clear", manager.clear),
        patch.object(EdfEnrStatisticsImporter, "_async_import", manager.import_),
    ):
        await _reimport(hass, init_integration.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert [c[0] for c in manager.mock_calls] == ["clear", "import_"]
    manager.import_.assert_awaited_once_with(full=True)
    # The deletion is covered by the import lock: no other import interleaves.
    assert running_during_clear == [True]


async def test_reimport_statistics_end_to_end(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The real importer rebuilds the statistics without error."""
    importer = init_integration.runtime_data.statistics

    await _reimport(hass, init_integration.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not importer.running
    assert init_integration.runtime_data.last_update_success


async def test_reimport_unknown_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An unknown entry id is a validation error."""
    with pytest.raises(ServiceValidationError) as err:
        await _reimport(hass, "does-not-exist")

    assert err.value.translation_key == "entry_not_found"
    assert err.value.translation_placeholders == {"entry_id": "does-not-exist"}


async def test_reimport_entry_of_another_domain(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Entries of other integrations are rejected."""
    other = MockConfigEntry(domain="other_domain")
    other.add_to_hass(hass)

    with pytest.raises(ServiceValidationError) as err:
        await _reimport(hass, other.entry_id)

    assert err.value.translation_key == "entry_not_found"


async def test_reimport_entry_not_loaded(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """An entry that is not loaded cannot be re-imported."""
    # The services are registered by the first set up of the domain.
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.config_entries.async_unload(mock_config_entry.entry_id)

    with pytest.raises(ServiceValidationError) as err:
        await _reimport(hass, mock_config_entry.entry_id)

    assert err.value.translation_key == "entry_not_loaded"
    assert err.value.translation_placeholders == {"title": mock_config_entry.title}


async def test_reimport_import_in_progress(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A running import is not disturbed."""
    with (
        patch.object(
            EdfEnrStatisticsImporter,
            "running",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch.object(
            EdfEnrStatisticsImporter, "async_clear", new_callable=AsyncMock
        ) as clear,
        pytest.raises(ServiceValidationError) as err,
    ):
        await _reimport(hass, init_integration.entry_id)

    assert err.value.translation_key == "import_in_progress"
    clear.assert_not_called()
