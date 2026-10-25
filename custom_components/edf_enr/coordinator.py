"""Data update coordinator for the Solaire Résidentiel FR integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    Device,
    EdfEnrAuthError,
    EdfEnrClient,
    EdfEnrConnectionError,
    EdfEnrError,
    EdfEnrNotFoundError,
    EdfEnrRateLimitError,
    EnergyReading,
    Equipment,
    Granularity,
    Installation,
    Project,
)
from .const import CONF_PV_NUMBER, DOMAIN, LOGGER, UPDATE_INTERVAL
from .statistics import EdfEnrStatisticsImporter

type EdfEnrConfigEntry = ConfigEntry[EdfEnrCoordinator]

GATEWAY_EQUIPMENT_TYPE = "Passerelle"
# Used when the portal rate limits us without suggesting a delay: longer than
# the normal interval, so that being rate limited never means polling faster.
RATE_LIMIT_RETRY_AFTER = 2 * UPDATE_INTERVAL.total_seconds()
# Bounds of the delay the portal can ask for: no tight retry loop, no endless wait.
RATE_LIMIT_MIN_RETRY_AFTER = 60.0
RATE_LIMIT_MAX_RETRY_AFTER = 86400.0


@dataclass(frozen=True, slots=True)
class EnergyTotals:
    """Energy (kWh) accumulated over a period."""

    solar_production: float
    self_consumption: float
    grid_import: float
    grid_injection: float
    home_consumption: float

    @classmethod
    def from_readings(cls, readings: Iterable[EnergyReading]) -> EnergyTotals:
        """Sum the known values of a set of readings."""
        readings = list(readings)

        def total(value_fn: Callable[[EnergyReading], float | None]) -> float:
            return round(
                sum(value for r in readings if (value := value_fn(r)) is not None), 3
            )

        return cls(
            solar_production=total(lambda r: r.solar_production),
            self_consumption=total(lambda r: r.self_consumption),
            grid_import=total(lambda r: r.grid_import),
            grid_injection=total(lambda r: r.grid_injection),
            home_consumption=total(lambda r: r.home_consumption),
        )

    @property
    def self_consumption_rate(self) -> float | None:
        """Share (%) of the solar production consumed by the home."""
        if not self.solar_production:
            return None
        return round(min(self.self_consumption / self.solar_production, 1.0) * 100, 1)

    @property
    def self_sufficiency_rate(self) -> float | None:
        """Share (%) of the home consumption covered by solar production."""
        if not self.home_consumption:
            return None
        return round(min(self.self_consumption / self.home_consumption, 1.0) * 100, 1)


@dataclass(frozen=True, slots=True)
class EdfEnrData:
    """Data refreshed by the coordinator."""

    today: EnergyTotals
    gateway: Device | None


class EdfEnrCoordinator(DataUpdateCoordinator[EdfEnrData]):
    """Fetch today's energy and keep long-term statistics up to date."""

    config_entry: EdfEnrConfigEntry
    project: Project
    installation: Installation | None
    gateway_equipment: Equipment | None
    statistics: EdfEnrStatisticsImporter
    device_id: str

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: EdfEnrConfigEntry,
        client: EdfEnrClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.pv_number: str = config_entry.data[CONF_PV_NUMBER]

    async def _async_setup(self) -> None:
        """Discover the project, its installation and its gateway."""
        projects = await self._call(self.client.async_get_projects)
        project = next((p for p in projects if p.pv_number == self.pv_number), None)
        if project is None:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="project_not_found",
                translation_placeholders={"pv_number": self.pv_number},
            )
        self.project = project
        self.installation = await self._call(
            lambda: self.client.async_get_installation(self.pv_number)
        )
        self.gateway_equipment = None
        if self.installation is not None:
            installation_id = self.installation.installation_id
            equipments = await self._call(
                lambda: self.client.async_get_equipments(installation_id)
            )
            self.gateway_equipment = next(
                (e for e in equipments if e.type == GATEWAY_EQUIPMENT_TYPE), None
            )
        self.statistics = EdfEnrStatisticsImporter(
            self.hass,
            self.client,
            self.pv_number,
            history_start=project.commissioning_date,
        )

    async def _async_update_data(self) -> EdfEnrData:
        """Fetch today's hourly energy and the gateway status."""
        day_start = dt_util.start_of_local_day()
        readings = await self._call(
            lambda: self.client.async_get_energy(
                self.pv_number,
                Granularity.HOURLY,
                day_start,
                day_start + timedelta(days=1),
            )
        )
        # The import only needs the telemetry: a failing gateway status (a
        # diagnostic) must not hold back the Energy dashboard statistics.
        self._schedule_statistics_import()
        gateway = await self._call(
            lambda: self._async_get_gateway(self.gateway_equipment)
        )
        return EdfEnrData(
            today=EnergyTotals.from_readings(readings),
            gateway=gateway,
        )

    async def _async_get_gateway(self, equipment: Equipment | None) -> Device | None:
        if equipment is None:
            return None
        try:
            return await self.client.async_get_device(equipment.equipment_id)
        except EdfEnrNotFoundError:
            return None

    def _schedule_statistics_import(self) -> None:
        if self.statistics.running:
            return
        self.config_entry.async_create_background_task(
            self.hass,
            self.statistics.async_import(),
            name=f"{DOMAIN} statistics import {self.config_entry.entry_id}",
        )

    async def _call[T](self, request: Callable[[], Awaitable[T]]) -> T:
        """Run an API request and translate its errors for Home Assistant."""
        try:
            return await request()
        except EdfEnrAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except EdfEnrRateLimitError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                retry_after=min(
                    max(
                        err.retry_after or RATE_LIMIT_RETRY_AFTER,
                        RATE_LIMIT_MIN_RETRY_AFTER,
                    ),
                    RATE_LIMIT_MAX_RETRY_AFTER,
                ),
            ) from err
        except EdfEnrConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="cannot_connect"
            ) from err
        except EdfEnrError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="unexpected_response",
                translation_placeholders={"error": str(err)},
            ) from err
