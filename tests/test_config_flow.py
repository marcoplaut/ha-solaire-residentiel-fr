"""Tests for the config flow."""

from collections.abc import Generator
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edf_enr.api import (
    EdfEnrApiError,
    EdfEnrAuthError,
    EdfEnrConnectionError,
)
from custom_components.edf_enr.const import CONF_PV_NUMBER, DOMAIN

from .conftest import PROJECT
from .const import EMAIL, OTHER_PV_NUMBER, PASSWORD, PV_NUMBER

USER_INPUT = {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
NEW_INPUT = {CONF_EMAIL: "john.doe@example.com", CONF_PASSWORD: "new-password"}
OTHER_PROJECT = replace(PROJECT, pv_number=OTHER_PV_NUMBER, installed_power_kwp=None)


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Prevent the real setup of the entry created by a flow."""
    with patch(
        "custom_components.edf_enr.async_setup_entry", return_value=True
    ) as mock_setup:
        yield mock_setup


async def _start_user_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result["flow_id"]


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_single_project(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A single project is added right after the credentials."""
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"{PV_NUMBER} (3.96 kWc)"
    assert result["data"] == {
        CONF_EMAIL: EMAIL,
        CONF_PASSWORD: PASSWORD,
        CONF_PV_NUMBER: PV_NUMBER,
    }
    assert result["result"].unique_id == PV_NUMBER


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_project_without_power(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """The title is the bare PV number when the power is unknown."""
    mock_client.async_get_projects.return_value = [OTHER_PROJECT]
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == OTHER_PV_NUMBER


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_several_projects(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """With several projects the user picks one."""
    mock_client.async_get_projects.return_value = [PROJECT, OTHER_PROJECT]
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "project"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PV_NUMBER: OTHER_PV_NUMBER}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == OTHER_PV_NUMBER
    assert result["data"] == {
        CONF_EMAIL: EMAIL,
        CONF_PASSWORD: PASSWORD,
        CONF_PV_NUMBER: OTHER_PV_NUMBER,
    }
    assert result["result"].unique_id == OTHER_PV_NUMBER


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (EdfEnrAuthError, "invalid_auth"),
        (EdfEnrConnectionError, "cannot_connect"),
        (EdfEnrApiError, "cannot_connect"),
        (RuntimeError, "unknown"),
    ],
)
@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_errors_and_recovery(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    exception: type[Exception],
    error: str,
) -> None:
    """Errors are shown on the form and the flow recovers."""
    mock_client.async_get_projects.side_effect = exception
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}

    mock_client.async_get_projects.side_effect = None
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PV_NUMBER] == PV_NUMBER


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_no_projects(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """An account without project is reported, then the flow recovers."""
    mock_client.async_get_projects.return_value = []
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_projects"}

    mock_client.async_get_projects.return_value = [PROJECT]
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_already_configured(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """The flow aborts when the only project is already configured."""
    mock_config_entry.add_to_hass(hass)
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_skips_configured_projects(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """The remaining project is added directly when the others are configured."""
    mock_config_entry.add_to_hass(hass)
    mock_client.async_get_projects.return_value = [PROJECT, OTHER_PROJECT]
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PV_NUMBER] == OTHER_PV_NUMBER


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_concurrent_duplicate(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A project configured while the flow was open aborts at creation time."""
    mock_client.async_get_projects.return_value = [PROJECT, OTHER_PROJECT]
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["step_id"] == "project"

    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PV_NUMBER: PV_NUMBER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow_success(
    hass: HomeAssistant, mock_client: AsyncMock, init_integration: MockConfigEntry
) -> None:
    """Reauthentication stores the new credentials and reloads the entry."""
    entry = init_integration
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["pv_number"] == PV_NUMBER

    with patch.object(hass.config_entries, "async_schedule_reload") as mock_reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], NEW_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_EMAIL] == NEW_INPUT[CONF_EMAIL]
    assert entry.data[CONF_PASSWORD] == NEW_INPUT[CONF_PASSWORD]
    assert entry.data[CONF_PV_NUMBER] == PV_NUMBER
    mock_reload.assert_called_once_with(entry.entry_id)


async def test_reauth_flow_reloads_entry(
    hass: HomeAssistant, mock_client: AsyncMock, init_integration: MockConfigEntry
) -> None:
    """The entry is really reloaded after a reauthentication."""
    entry = init_integration
    result = await entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], NEW_INPUT
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["reason"] == "reauth_successful"
    assert entry.state is ConfigEntryState.LOADED


async def test_reauth_flow_wrong_account(
    hass: HomeAssistant, mock_client: AsyncMock, init_integration: MockConfigEntry
) -> None:
    """Credentials of an account without the project are refused."""
    entry = init_integration
    mock_client.async_get_projects.return_value = [OTHER_PROJECT]
    result = await entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], NEW_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert result["description_placeholders"]["pv_number"] == PV_NUMBER
    assert entry.data[CONF_PASSWORD] == PASSWORD


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (EdfEnrAuthError, "invalid_auth"),
        (EdfEnrConnectionError, "cannot_connect"),
        (RuntimeError, "unknown"),
    ],
)
async def test_reauth_flow_errors_and_recovery(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    init_integration: MockConfigEntry,
    exception: type[Exception],
    error: str,
) -> None:
    """Reauthentication shows errors, then succeeds."""
    entry = init_integration
    result = await entry.start_reauth_flow(hass)
    mock_client.async_get_projects.side_effect = exception

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], NEW_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}

    mock_client.async_get_projects.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], NEW_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == NEW_INPUT[CONF_PASSWORD]


async def test_reconfigure_flow_success(
    hass: HomeAssistant, mock_client: AsyncMock, init_integration: MockConfigEntry
) -> None:
    """Reconfiguration updates the credentials."""
    entry = init_integration
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], NEW_INPUT
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_EMAIL] == NEW_INPUT[CONF_EMAIL]
    assert entry.data[CONF_PASSWORD] == NEW_INPUT[CONF_PASSWORD]
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_flow_wrong_account(
    hass: HomeAssistant, mock_client: AsyncMock, init_integration: MockConfigEntry
) -> None:
    """Reconfiguration with another account is refused."""
    entry = init_integration
    mock_client.async_get_projects.return_value = [OTHER_PROJECT]
    result = await entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], NEW_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_EMAIL] == EMAIL


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (EdfEnrAuthError, "invalid_auth"),
        (EdfEnrConnectionError, "cannot_connect"),
        (RuntimeError, "unknown"),
    ],
)
async def test_reconfigure_flow_errors_and_recovery(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    init_integration: MockConfigEntry,
    exception: type[Exception],
    error: str,
) -> None:
    """Reconfiguration shows errors, then succeeds."""
    entry = init_integration
    result = await entry.start_reconfigure_flow(hass)
    mock_client.async_get_projects.side_effect = exception

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], NEW_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": error}

    mock_client.async_get_projects.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], NEW_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_EMAIL] == NEW_INPUT[CONF_EMAIL]
