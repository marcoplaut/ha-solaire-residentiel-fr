"""Shared fixtures for the Solaire Résidentiel FR tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, time, timedelta
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, PropertyMock, patch
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension

from custom_components.edf_enr.api import (
    Device,
    EdfEnrClient,
    EnergyReading,
    Equipment,
    Granularity,
    Installation,
    Project,
)
from custom_components.edf_enr.const import DOMAIN

from .const import (
    COMMISSIONING_DATE,
    ENTRY_DATA,
    GATEWAY_ID,
    INSTALLATION_ID,
    NOW,
    PV_NUMBER,
    WATER_HEATER_ID,
)

FIXTURES = Path(__file__).parent / "fixtures"
PARIS = ZoneInfo("Europe/Paris")

pytest_plugins = ["pytest_homeassistant_custom_component"]


def load_fixture(name: str) -> str:
    """Return the content of a fixture file."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json_fixture(name: str) -> Any:
    """Return the parsed content of a JSON fixture file."""
    return json.loads(load_fixture(name))


@pytest.fixture
def mock_recorder_before_hass(async_setup_recorder_instance: Any) -> None:
    """Prepare the recorder database before the hass fixture is created."""


@pytest.fixture(autouse=True)
async def auto_recorder(
    recorder_mock: Any, hass: HomeAssistant
) -> AsyncGenerator[None]:
    """Run every test with an in-memory recorder (a dependency of the integration).

    Background statistics imports started by the test (e.g. after a reload)
    must finish before the recorder is torn down.
    """
    yield
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Use the Home Assistant snapshot extension (tests/snapshots/*.ambr).

    syrupy and the Home Assistant plugin both define this fixture and the one
    registered last wins, which depends on the plugin load order.
    """
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow Home Assistant to load the custom integration."""


@pytest.fixture(autouse=True)
def frozen_time(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    """Run every test at a fixed time."""
    freezer.move_to(NOW)
    return freezer


@pytest.fixture(autouse=True)
async def paris_time_zone(hass: HomeAssistant) -> None:
    """Use the time zone of the installations."""
    await hass.config.async_set_time_zone("Europe/Paris")


PROJECT = Project(
    pv_number=PV_NUMBER,
    name="Jane Doe - PV0000042",
    installed_power_kwp=3.96,
    commissioning_date=COMMISSIONING_DATE,
    offer_name="Mon Soleil & Moi toiture V3.1",
)
INSTALLATION = Installation(INSTALLATION_ID, PV_NUMBER)
GATEWAY = Equipment(
    equipment_id=GATEWAY_ID,
    type="Passerelle",
    sub_type="YuzeG1",
    brand="Fournisseur solaire",
    last_connection=datetime(2026, 10, 24, 18, 50, 30, tzinfo=UTC),
)
WATER_HEATER = Equipment(
    equipment_id=WATER_HEATER_ID,
    type="Chauffe-eau",
    sub_type="NONE",
    brand="Inconnue",
    last_connection=None,
)
GATEWAY_DEVICE = Device(
    device_id=GATEWAY_ID,
    type="YuzeG1",
    status="MisEnService",
    last_reported_at=datetime(2026, 10, 24, 18, 50, 30, tzinfo=UTC),
)


def hourly_profile(hour: datetime) -> EnergyReading:
    """Return a deterministic, plausible reading for a UTC hour."""
    solar = {9: 0.4, 10: 1.1, 11: 2.0, 12: 2.5, 13: 2.4, 14: 1.8, 15: 0.9, 16: 0.2}
    production = solar.get(hour.hour, 0.0)
    self_consumption = round(min(production, 0.6), 3)
    grid_import = 0.0 if production >= 0.6 else round(0.6 - self_consumption, 3)
    return EnergyReading(
        start=hour,
        granularity=Granularity.HOURLY,
        solar_production=production,
        self_consumption=self_consumption,
        grid_import=grid_import,
        ecs_consumption=0.0,
    )


class FakeTelemetry:
    """Serve readings from a profile, with per-hour overrides and a data horizon.

    Like the real portal, hours starting at or after ``horizon`` (default: now)
    are not reported yet; the current hour is reported.
    """

    def __init__(self) -> None:
        """Initialize with no override."""
        self.overrides: dict[datetime, EnergyReading | None] = {}
        self.horizon: datetime | None = None
        # Corrupted hourly buckets: served by the hourly granularity only, the
        # daily totals stay right (what the real portal does).
        self.corruptions: dict[datetime, EnergyReading] = {}
        self.calls: list[tuple[Granularity, datetime, datetime]] = []

    def reading(self, hour: datetime) -> EnergyReading | None:
        """Return the reading of an hour, or None if not reported."""
        if hour >= (self.horizon or dt_util.utcnow()):
            return None
        if hour in self.overrides:
            return self.overrides[hour]
        return hourly_profile(hour)

    async def __call__(
        self,
        pv_number: str,
        granularity: Granularity,
        start: datetime,
        end: datetime,
    ) -> list[EnergyReading]:
        """Mimic EdfEnrClient.async_get_energy (hourly and daily granularities)."""
        self.calls.append((granularity, start, end))
        assert granularity in (Granularity.HOURLY, Granularity.DAILY)
        if granularity is Granularity.DAILY:
            return self._daily(start, end)
        readings = []
        hour = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        while hour < end:
            if hour >= start and (reading := self.reading(hour)) is not None:
                readings.append(self.corruptions.get(hour, reading))
            hour += timedelta(hours=1)
        return readings

    def _daily(self, start: datetime, end: datetime) -> list[EnergyReading]:
        """Return the sum of the hourly readings of each local (Paris) day."""
        first = start.astimezone(PARIS).date()
        last = end.astimezone(PARIS).date()
        readings = []
        day = first
        while day <= last:
            midnight = datetime.combine(day, time(), PARIS).astimezone(UTC)
            if start <= midnight < end:
                hours = [
                    reading
                    for offset in range(25)
                    if (hour := midnight + timedelta(hours=offset))
                    < datetime.combine(day + timedelta(days=1), time(), PARIS)
                    and (reading := self.reading(hour)) is not None
                ]
                if hours:
                    readings.append(
                        EnergyReading(
                            start=midnight,
                            granularity=Granularity.DAILY,
                            **{
                                field: round(
                                    sum(getattr(r, field) or 0.0 for r in hours), 3
                                )
                                for field in (
                                    "solar_production",
                                    "self_consumption",
                                    "grid_import",
                                    "ecs_consumption",
                                )
                            },
                        )
                    )
            day += timedelta(days=1)
        return readings


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Enable the entities that are disabled by default."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        new_callable=PropertyMock,
        return_value=True,
    ):
        yield


@pytest.fixture
def telemetry() -> FakeTelemetry:
    """Return the fake telemetry served by the mocked client."""
    return FakeTelemetry()


@pytest.fixture
def mock_client(telemetry: FakeTelemetry) -> Generator[AsyncMock]:
    """Return a mocked API client, used by both the flow and the setup."""
    client = AsyncMock(spec=EdfEnrClient)
    client.async_get_projects.return_value = [PROJECT]
    client.async_get_installation.return_value = INSTALLATION
    client.async_get_equipments.return_value = [GATEWAY, WATER_HEATER]
    client.async_get_device.return_value = GATEWAY_DEVICE
    client.async_get_energy.side_effect = telemetry.__call__
    with (
        patch("custom_components.edf_enr.create_client_from_data", return_value=client),
        patch(
            "custom_components.edf_enr.config_flow.create_client_from_data",
            return_value=client,
        ),
    ):
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return the config entry of the test project."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"{PV_NUMBER} (3.96 kWc)",
        unique_id=PV_NUMBER,
        data=ENTRY_DATA,
        version=1,
        minor_version=1,
    )


@pytest.fixture
def mock_statistics_pause() -> Generator[None]:
    """Remove the pause between back-fill requests."""
    with patch("custom_components.edf_enr.statistics.FETCH_PAUSE", 0):
        yield


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_statistics_pause: None,
) -> MockConfigEntry:
    """Set up the integration and wait for the first statistics import."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    # The statistics are written by the recorder thread; wait for them so the
    # next import reads them (and does not start a full back-fill again).
    await async_wait_recording_done(hass)
    return mock_config_entry
