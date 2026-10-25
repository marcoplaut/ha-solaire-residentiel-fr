"""End-to-end tests: Home Assistant + the real client + a simulated portal.

Nothing is mocked between the integration and the HTTP server: the config
flow logs in through the simulated Azure AD B2C, the coordinator and the
statistics importer call the simulated API over a local socket.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from functools import partial
import json
from typing import Any
from unittest.mock import patch

from aiohttp import CookieJar
from aiohttp.test_utils import TestServer
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.edf_enr.api import EdfEnrClient
from custom_components.edf_enr.const import DOMAIN, UPDATE_INTERVAL
from custom_components.edf_enr.statistics import SERIES, floor_hour, statistic_id

from .const import CONTACT_ID, EMAIL, NOW, OTHER_PV_NUMBER, PASSWORD, PV_NUMBER
from .simulator.portal import (
    PARIS,
    Account,
    PortalSimulator,
    local_hour_to_utc,
    profile,
)

HOUR = timedelta(hours=1)
# History start announced by the simulated CRM.
COMMISSIONING = datetime(2026, 10, 23, tzinfo=UTC)
API_KEYS = {
    "solar_production": "solarProduction",
    "self_consumption": "autoConsumption",
    "grid_import": "networkConsumption",
}


@pytest.fixture
async def portal(
    socket_enabled: None, mock_statistics_pause: None
) -> AsyncGenerator[PortalSimulator]:
    """Serve the simulated portal on 127.0.0.1 and point the client to it."""
    simulator = PortalSimulator(
        accounts=[Account(EMAIL, PASSWORD, CONTACT_ID, [PV_NUMBER])]
    )
    server = TestServer(simulator.app(), host="127.0.0.1")
    await server.start_server(access_log=None)
    base_url = str(server.make_url("")).rstrip("/")
    simulator.redirect_uri = f"{base_url}/portal"
    client_factory = partial(
        EdfEnrClient,
        api_base_url=f"{base_url}/api",
        b2c_base_url=f"{base_url}/b2c",
        redirect_uri=simulator.redirect_uri,
    )
    with (
        patch("custom_components.edf_enr.helpers.EdfEnrClient", client_factory),
        # aiohttp ignores cookies set by IP addresses unless "unsafe".
        patch(
            "custom_components.edf_enr.helpers.CookieJar",
            partial(CookieJar, unsafe=True),
        ),
    ):
        yield simulator
    await server.close()


async def configure(hass: HomeAssistant, password: str = PASSWORD) -> dict[str, Any]:
    """Run the user config flow through the simulated login."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: password}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    return result


async def setup_entry(hass: HomeAssistant) -> ConfigEntry:
    result = await configure(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_get_entry(result["result"].entry_id)
    assert entry is not None
    assert entry.state is ConfigEntryState.LOADED
    await async_wait_recording_done(hass)
    return entry


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta
) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def state(hass: HomeAssistant, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{PV_NUMBER}_{key}"
    )
    assert entity_id is not None
    current = hass.states.get(entity_id)
    assert current is not None
    return current.state


def expected_hourly(
    simulator: PortalSimulator, key: str, start: datetime, end: datetime
) -> dict[datetime, float]:
    """Return what the portal reports per UTC hour for a series."""
    values: dict[datetime, float] = {}
    local = start.astimezone(PARIS).replace(tzinfo=None)
    while local_hour_to_utc(local) < end:
        energies = simulator.bucket(local)
        if key == "grid_injection":
            value = energies["solarProduction"] - energies["autoConsumption"]
        elif key == "home_consumption":
            value = energies["autoConsumption"] + energies["networkConsumption"]
        else:
            value = energies[API_KEYS[key]]
        hour = local_hour_to_utc(local)
        values[hour] = round(values.get(hour, 0.0) + max(value, 0.0), 3)
        local += HOUR
    return values


async def stored_rows(
    hass: HomeAssistant, key: str, start: datetime = COMMISSIONING
) -> list[dict[str, Any]]:
    await async_wait_recording_done(hass)
    stat_id = statistic_id(PV_NUMBER, next(s for s in SERIES if s.key == key))
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(days=2),
        None,
        {stat_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return [
        {
            "start": datetime.fromtimestamp(row["start"], UTC),
            "state": row["state"],
            "sum": row["sum"],
        }
        for row in stats.get(stat_id, [])
    ]


async def assert_statistics_match_portal(
    hass: HomeAssistant,
    simulator: PortalSimulator,
    end: datetime,
    start: datetime = COMMISSIONING,
    unchecked: frozenset[datetime] = frozenset(),
) -> None:
    """Compare the stored hourly states with the portal's buckets.

    The states of the ``unchecked`` hours (corrupted buckets, rebuilt from the
    daily totals) are not compared, but the sums must still accumulate them.
    """
    for series in SERIES:
        rows = await stored_rows(hass, series.key, start)
        reported = expected_hourly(simulator, series.key, start, end)
        # Every hour is stored; an hour without bucket (DST) counts as 0.
        hours = int((end - start) / HOUR)
        expected = {
            start + i * HOUR: reported.get(start + i * HOUR, 0.0) for i in range(hours)
        }
        assert [row["start"] for row in rows] == list(expected), series.key
        assert [row["state"] for row in rows if row["start"] not in unchecked] == (
            pytest.approx(
                [value for hour, value in expected.items() if hour not in unchecked]
            )
        ), series.key
        running = 0.0
        for row in rows:
            running += row["state"]
            assert row["sum"] == pytest.approx(running, abs=1e-6)


# ---------------------------------------------------------------- happy path


async def test_full_journey(
    hass: HomeAssistant,
    portal: PortalSimulator,
    hass_client: ClientSessionGenerator,
) -> None:
    entry = await setup_entry(hass)

    assert entry.unique_id == PV_NUMBER
    assert entry.title == f"{PV_NUMBER} (3 kWc)"
    assert portal.count("login") == 2  # config flow + setup
    # Today's totals (Paris day) come from the portal's hourly buckets.
    day_start = NOW.astimezone(PARIS).replace(hour=0, minute=0, second=0)
    today = expected_hourly(portal, "solar_production", day_start, NOW)
    assert float(state(hass, "solar_production_today")) == pytest.approx(
        sum(today.values())
    )
    assert state(hass, "gateway_last_report") != STATE_UNAVAILABLE
    # The history is back-filled up to the last complete hour.
    await assert_statistics_match_portal(hass, portal, floor_hour(NOW))

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    dumped = json.dumps(diagnostics)
    for secret in (EMAIL, PASSWORD, PV_NUMBER, CONTACT_ID):
        assert secret not in dumped


async def test_invalid_credentials_are_reported_by_the_flow(
    hass: HomeAssistant, portal: PortalSimulator
) -> None:
    result = await configure(hass, password="wrong")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert portal.count("login_rejected") == 1


async def test_several_installations(
    hass: HomeAssistant, portal: PortalSimulator
) -> None:
    portal.accounts[0].pv_numbers.append(OTHER_PV_NUMBER)
    result = await configure(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "project"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pv_number": OTHER_PV_NUMBER}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == OTHER_PV_NUMBER
    assert result["result"].state is ConfigEntryState.LOADED


# ----------------------------------------------------------- authentication


async def test_tokens_are_refreshed_then_renewed_by_login(
    hass: HomeAssistant, portal: PortalSimulator, frozen_time: FrozenDateTimeFactory
) -> None:
    portal.refresh_token_ttl = timedelta(hours=1)
    await setup_entry(hass)
    logins = portal.count("login")

    # The access token (30 min) expires: the refresh token is used.
    await tick(hass, frozen_time, UPDATE_INTERVAL)
    assert portal.count("refresh") == 1
    assert portal.count("login") == logins

    # The refresh token expires too: a full login happens silently, without
    # even trying the expired refresh token.
    await tick(hass, frozen_time, timedelta(hours=2))
    assert portal.count("login") == logins + 1
    assert portal.count("refresh_rejected") == 0
    assert state(hass, "solar_production_today") != STATE_UNAVAILABLE


async def test_rejected_refresh_token_falls_back_to_login(
    hass: HomeAssistant, portal: PortalSimulator, frozen_time: FrozenDateTimeFactory
) -> None:
    await setup_entry(hass)
    logins = portal.count("login")
    # The portal forgets the refresh tokens (e.g. a server-side session reset).
    portal._refresh_tokens.clear()

    await tick(hass, frozen_time, UPDATE_INTERVAL)

    assert portal.count("refresh_rejected") == 1
    assert portal.count("login") == logins + 1
    assert state(hass, "solar_production_today") != STATE_UNAVAILABLE


async def test_revoked_token_is_renewed_transparently(
    hass: HomeAssistant, portal: PortalSimulator, frozen_time: FrozenDateTimeFactory
) -> None:
    # Long-lived tokens: the client would not renew them on its own.
    portal.access_token_ttl = timedelta(days=1)
    await setup_entry(hass)
    portal.revoke_access_tokens()

    await tick(hass, frozen_time, UPDATE_INTERVAL)

    assert portal.count("api_unauthorized") >= 1
    assert portal.count("refresh") >= 1
    assert state(hass, "solar_production_today") != STATE_UNAVAILABLE


async def test_password_change_starts_reauth(
    hass: HomeAssistant, portal: PortalSimulator, frozen_time: FrozenDateTimeFactory
) -> None:
    entry = await setup_entry(hass)
    portal.change_password(EMAIL, "new-password")

    await tick(hass, frozen_time, UPDATE_INTERVAL)

    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: "new-password"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"
    assert entry.state is ConfigEntryState.LOADED
    assert state(hass, "solar_production_today") != STATE_UNAVAILABLE


# ------------------------------------------------------------- availability


@pytest.mark.parametrize("status", [503, 429])
async def test_outage_then_recovery(
    hass: HomeAssistant,
    portal: PortalSimulator,
    frozen_time: FrozenDateTimeFactory,
    status: int,
) -> None:
    await setup_entry(hass)
    portal.retry_after = "60" if status == 429 else None
    portal.fail("/telemetries/", status)

    await tick(hass, frozen_time, UPDATE_INTERVAL)
    assert state(hass, "solar_production_today") == STATE_UNAVAILABLE
    assert portal.count(f"failure_{status}") == 1

    await tick(hass, frozen_time, UPDATE_INTERVAL)
    assert state(hass, "solar_production_today") != STATE_UNAVAILABLE


async def test_portal_down_at_startup_retries_setup(
    hass: HomeAssistant, portal: PortalSimulator, frozen_time: FrozenDateTimeFactory
) -> None:
    await setup_entry(hass)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    portal.fail("/crm/", 502, 502)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY

    await tick(hass, frozen_time, timedelta(minutes=1))
    await tick(hass, frozen_time, timedelta(minutes=1))
    assert entry.state is ConfigEntryState.LOADED


# ---------------------------------------------------------------- statistics


async def test_late_and_revised_data_reach_the_statistics(
    hass: HomeAssistant, portal: PortalSimulator, frozen_time: FrozenDateTimeFactory
) -> None:
    # The gateway is 3 hours late when the integration starts.
    portal.horizon = NOW - 3 * HOUR
    await setup_entry(hass)
    rows = await stored_rows(hass, "solar_production")
    assert rows[-1]["start"] == floor_hour(NOW) - 3 * HOUR

    # It catches up, and the portal revises yesterday's noon production.
    portal.horizon = None
    revised = datetime(2026, 10, 23, 14)  # noqa: DTZ001 - naive portal label
    portal.overrides[revised] = {**profile(revised), "solarProduction": 3.5}
    await tick(hass, frozen_time, UPDATE_INTERVAL)

    await assert_statistics_match_portal(
        hass, portal, floor_hour(NOW + UPDATE_INTERVAL)
    )


async def test_corrupted_buckets_are_rebuilt_from_the_daily_total(
    hass: HomeAssistant, portal: PortalSimulator
) -> None:
    # A meter index and a value in Wh instead of the energy of two hours of
    # 2026-10-23, whose daily total stays right (the simulated installation
    # is 3 kWp: 4.5 kWh per hour at most).
    portal.commissioning_date = "2026-10-22T00:00:00Z"
    day = datetime(2026, 10, 23)  # noqa: DTZ001 - naive portal label
    corrupted_hours = [day + 14 * HOUR, day + 15 * HOUR]
    for local, value in zip(corrupted_hours, (6790829.9, 378.0), strict=True):
        portal.corruptions[local] = {**profile(local), "solarProduction": value}
    await setup_entry(hass)

    await assert_statistics_match_portal(
        hass,
        portal,
        floor_hour(NOW),
        datetime(2026, 10, 22, tzinfo=UTC),
        frozenset(local_hour_to_utc(local) for local in corrupted_hours),
    )
    rows = await stored_rows(
        hass, "solar_production", datetime(2026, 10, 22, tzinfo=UTC)
    )
    assert max(row["state"] for row in rows) <= 4.5
    # The Paris day adds up to the portal's daily total.
    day_start = local_hour_to_utc(day)
    stored = sum(
        row["state"]
        for row in rows
        if day_start <= row["start"] < day_start + 24 * HOUR
    )
    assert stored == pytest.approx(
        portal.daily_bucket(day)["solarProduction"], abs=0.002
    )


async def test_daylight_saving_time_change(
    hass: HomeAssistant, portal: PortalSimulator, frozen_time: FrozenDateTimeFactory
) -> None:
    # Paris goes back from UTC+2 to UTC+1 on 2026-10-25 at 01:00 UTC; the
    # portal reports a single 02:00 bucket for the repeated local hour.
    frozen_time.move_to(datetime(2026, 10, 25, 9, 30, tzinfo=UTC))
    await setup_entry(hass)

    await assert_statistics_match_portal(
        hass, portal, datetime(2026, 10, 25, 9, tzinfo=UTC)
    )
    rows = {
        row["start"]: row["state"] for row in await stored_rows(hass, "grid_import")
    }
    # 02:00 CEST (00:00 UTC) holds the bucket, 02:00 CET (01:00 UTC) is empty.
    assert rows[datetime(2026, 10, 25, 0, tzinfo=UTC)] == pytest.approx(0.7)
    assert rows[datetime(2026, 10, 25, 1, tzinfo=UTC)] == 0.0


async def test_spring_daylight_saving_time_change(
    hass: HomeAssistant, portal: PortalSimulator, frozen_time: FrozenDateTimeFactory
) -> None:
    # Paris goes from UTC+1 to UTC+2 on 2027-03-28 at 01:00 UTC; the portal's
    # phantom 02:00 bucket and its 03:00 bucket both belong to that UTC hour.
    commissioning = datetime(2027, 3, 27, tzinfo=UTC)
    portal.commissioning_date = commissioning.strftime("%Y-%m-%dT%H:%M:%SZ")
    frozen_time.move_to(datetime(2027, 3, 31, 0, 40, tzinfo=UTC))
    await setup_entry(hass)

    # 30 minutes later the 3-day revision window starts exactly at that hour.
    await tick(hass, frozen_time, UPDATE_INTERVAL)

    await assert_statistics_match_portal(
        hass, portal, datetime(2027, 3, 31, 1, tzinfo=UTC), commissioning
    )


async def test_removing_the_entry_deletes_statistics(
    hass: HomeAssistant, portal: PortalSimulator
) -> None:
    entry = await setup_entry(hass)
    assert await stored_rows(hass, "solar_production")

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    for series in SERIES:
        assert await stored_rows(hass, series.key) == []


async def test_quoted_cookies_break_the_login(
    hass: HomeAssistant, portal: PortalSimulator
) -> None:
    """Guard the CookieJar(quote_cookie=False) requirement of the B2C login."""

    def default_jar(**kwargs: Any) -> CookieJar:
        return CookieJar(unsafe=True)  # quote_cookie defaults to True

    with patch("custom_components.edf_enr.helpers.CookieJar", default_jar):
        result = await configure(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert portal.count("login") == 0
