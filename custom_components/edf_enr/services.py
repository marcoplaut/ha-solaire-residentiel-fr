"""Services of the Solaire Résidentiel FR integration."""

from __future__ import annotations

import probatio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import ATTR_CONFIG_ENTRY_ID, DOMAIN, SERVICE_REIMPORT_STATISTICS
from .coordinator import EdfEnrConfigEntry

REIMPORT_STATISTICS_SCHEMA = probatio.Schema(
    {probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string}
)


def _get_entry(hass: HomeAssistant, entry_id: str) -> EdfEnrConfigEntry:
    entry: EdfEnrConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    return entry


async def _async_reimport_statistics(call: ServiceCall) -> None:
    """Delete and rebuild the statistics of a project in the background."""
    entry = _get_entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    importer = entry.runtime_data.statistics
    if importer.running:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="import_in_progress"
        )

    entry.async_create_background_task(
        call.hass,
        importer.async_reimport(),
        name=f"{DOMAIN} statistics reimport {entry.entry_id}",
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_REIMPORT_STATISTICS,
        _async_reimport_statistics,
        schema=REIMPORT_STATISTICS_SCHEMA,
    )
