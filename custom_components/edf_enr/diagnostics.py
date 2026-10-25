"""Diagnostics support for the Solaire Résidentiel FR integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_PV_NUMBER
from .coordinator import EdfEnrConfigEntry

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PV_NUMBER,
    "device_id",
    "equipment_id",
    "installation_id",
    "name",
    "title",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EdfEnrConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return async_redact_data(
        {
            "entry": entry.as_dict(),
            "project": asdict(coordinator.project),
            "installation": (
                asdict(coordinator.installation) if coordinator.installation else None
            ),
            "gateway_equipment": (
                asdict(coordinator.gateway_equipment)
                if coordinator.gateway_equipment
                else None
            ),
            "data": {
                "today": asdict(data.today),
                "gateway": asdict(data.gateway) if data.gateway else None,
            },
            "statistics": {
                "running": coordinator.statistics.running,
                "series": len(coordinator.statistics.statistic_ids),
            },
            "last_update_success": coordinator.last_update_success,
        },
        TO_REDACT,
    )
