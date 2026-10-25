"""Base entity for the Solaire Résidentiel FR integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, INTEGRATION_NAME
from .coordinator import EdfEnrCoordinator


def installation_device_info(coordinator: EdfEnrCoordinator) -> DeviceInfo:
    """Return the device of the solar installation (a cloud service)."""
    pv_number = coordinator.pv_number
    return DeviceInfo(
        identifiers={(DOMAIN, pv_number)},
        entry_type=DeviceEntryType.SERVICE,
        manufacturer=INTEGRATION_NAME,
        model=coordinator.project.offer_name,
        name=f"{INTEGRATION_NAME} {pv_number}",
        serial_number=pv_number,
    )


class EdfEnrEntity(CoordinatorEntity[EdfEnrCoordinator]):
    """An entity of a solar installation."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EdfEnrCoordinator,
        description: EntityDescription,
        *,
        on_gateway: bool = False,
    ) -> None:
        """Initialize the entity on the installation or on its gateway device."""
        super().__init__(coordinator)
        self.entity_description = description
        pv_number = coordinator.pv_number
        self._attr_unique_id = f"{pv_number}_{description.key}"
        gateway = coordinator.gateway_equipment
        if on_gateway and gateway is not None:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{pv_number}_gateway")},
                manufacturer=INTEGRATION_NAME,
                model=gateway.sub_type,
                translation_key="gateway",
                via_device_id=coordinator.device_id,
            )
        else:
            self._attr_device_info = installation_device_info(coordinator)
