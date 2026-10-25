"""Sensors of the Solaire Résidentiel FR integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import EdfEnrConfigEntry, EdfEnrCoordinator, EdfEnrData
from .entity import EdfEnrEntity

# The coordinator serializes the API calls.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EdfEnrSensorEntityDescription(SensorEntityDescription):
    """Describes a sensor fed by the coordinator data."""

    value_fn: Callable[[EdfEnrData], StateType | datetime]


def _energy_today(
    key: str, value_fn: Callable[[EdfEnrData], float]
) -> EdfEnrSensorEntityDescription:
    # No state class on purpose: these values are only for display. The Energy
    # dashboard must use the edf_enr:* external statistics, which place late
    # measurements in the right hour.
    return EdfEnrSensorEntityDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=value_fn,
    )


SENSORS: tuple[EdfEnrSensorEntityDescription, ...] = (
    _energy_today("solar_production_today", lambda d: d.today.solar_production),
    _energy_today("grid_import_today", lambda d: d.today.grid_import),
    _energy_today("grid_injection_today", lambda d: d.today.grid_injection),
    _energy_today("self_consumption_today", lambda d: d.today.self_consumption),
    _energy_today("home_consumption_today", lambda d: d.today.home_consumption),
    EdfEnrSensorEntityDescription(
        key="self_consumption_rate_today",
        translation_key="self_consumption_rate_today",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.today.self_consumption_rate,
    ),
    EdfEnrSensorEntityDescription(
        key="self_sufficiency_rate_today",
        translation_key="self_sufficiency_rate_today",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.today.self_sufficiency_rate,
    ),
)

GATEWAY_SENSORS: tuple[EdfEnrSensorEntityDescription, ...] = (
    EdfEnrSensorEntityDescription(
        key="gateway_last_report",
        translation_key="gateway_last_report",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.gateway.last_reported_at if d.gateway else None,
    ),
    EdfEnrSensorEntityDescription(
        key="gateway_status",
        translation_key="gateway_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.gateway.status if d.gateway else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EdfEnrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors of a config entry."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        EdfEnrSensor(coordinator, description) for description in SENSORS
    ]
    if coordinator.project.installed_power_kwp is not None:
        entities.append(EdfEnrInstalledPowerSensor(coordinator))
    if coordinator.gateway_equipment is not None:
        entities.extend(
            EdfEnrSensor(coordinator, description, on_gateway=True)
            for description in GATEWAY_SENSORS
        )
    async_add_entities(entities)


class EdfEnrSensor(EdfEnrEntity, SensorEntity):
    """A sensor computed from the coordinator data."""

    entity_description: EdfEnrSensorEntityDescription

    @property
    def native_value(self) -> StateType | datetime:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)


INSTALLED_POWER = SensorEntityDescription(
    key="installed_power",
    translation_key="installed_power",
    device_class=SensorDeviceClass.POWER,
    native_unit_of_measurement=UnitOfPower.KILO_WATT,
    entity_category=EntityCategory.DIAGNOSTIC,
    suggested_display_precision=2,
)


class EdfEnrInstalledPowerSensor(EdfEnrEntity, SensorEntity):
    """Peak power of the installation, as declared in the contract."""

    def __init__(self, coordinator: EdfEnrCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, INSTALLED_POWER)
        self._attr_native_value = coordinator.project.installed_power_kwp
