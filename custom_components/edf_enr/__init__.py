"""The Solaire Résidentiel FR integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import CONF_PV_NUMBER, DOMAIN
from .coordinator import EdfEnrConfigEntry, EdfEnrCoordinator
from .entity import installation_device_info
from .helpers import create_client_from_data
from .services import async_setup_services
from .statistics import async_clear_statistics

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EdfEnrConfigEntry) -> bool:
    """Set up a solar project from a config entry."""
    client = create_client_from_data(hass, dict(entry.data))
    coordinator = EdfEnrCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    # Registered before the platforms so the gateway can reference it.
    coordinator.device_id = (
        dr.async_get(hass)
        .async_get_or_create(
            config_entry_id=entry.entry_id, **installation_device_info(coordinator)
        )
        .id
    )
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EdfEnrConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: EdfEnrConfigEntry) -> None:
    """Delete the imported statistics when the entry is removed."""
    await async_clear_statistics(hass, entry.data[CONF_PV_NUMBER])
