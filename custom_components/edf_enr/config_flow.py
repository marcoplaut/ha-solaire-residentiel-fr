"""Config flow for the Solaire Résidentiel FR integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import probatio

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import EdfEnrAuthError, EdfEnrError, Project
from .const import CONF_PV_NUMBER, DOMAIN, LOGGER
from .helpers import create_client_from_data

CREDENTIALS_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        probatio.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


class EdfEnrConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Solaire Résidentiel FR."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._credentials: dict[str, str] = {}
        self._projects: list[Project] = []

    async def _async_fetch_projects(
        self, user_input: dict[str, Any]
    ) -> tuple[list[Project], dict[str, str]]:
        """Log in and list the projects; return them with form errors."""
        client = create_client_from_data(self.hass, user_input)
        try:
            projects = await client.async_get_projects()
        except EdfEnrAuthError:
            return [], {"base": "invalid_auth"}
        except EdfEnrError as err:
            LOGGER.debug("Cannot connect to the customer portal: %s", err)
            return [], {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001
            LOGGER.exception("Unexpected error while logging in")
            return [], {"base": "unknown"}
        if not projects:
            return [], {"base": "no_projects"}
        return projects, {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the portal credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            projects, errors = await self._async_fetch_projects(user_input)
            if not errors:
                self._credentials = {
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                configured = self._async_current_ids(include_ignore=False)
                self._projects = [p for p in projects if p.pv_number not in configured]
                if not self._projects:
                    return self.async_abort(reason="already_configured")
                if len(self._projects) == 1:
                    return await self._async_create_project_entry(self._projects[0])
                return await self.async_step_project()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_project(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick one of several projects."""
        if user_input is not None:
            project = next(
                p for p in self._projects if p.pv_number == user_input[CONF_PV_NUMBER]
            )
            return await self._async_create_project_entry(project)
        return self.async_show_form(
            step_id="project",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_PV_NUMBER): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=p.pv_number,
                                    label=_project_label(p),
                                )
                                for p in self._projects
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def _async_create_project_entry(self, project: Project) -> ConfigFlowResult:
        await self.async_set_unique_id(project.pv_number)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=_project_label(project),
            data={**self._credentials, CONF_PV_NUMBER: project.pv_number},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start a reauthentication after the credentials were rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials."""
        return await self._async_step_update_credentials(
            "reauth_confirm", self._get_reauth_entry(), user_input
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the credentials of an entry."""
        return await self._async_step_update_credentials(
            "reconfigure", self._get_reconfigure_entry(), user_input
        )

    async def _async_step_update_credentials(
        self,
        step_id: str,
        entry: ConfigEntry,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            projects, errors = await self._async_fetch_projects(user_input)
            if not errors:
                if entry.data[CONF_PV_NUMBER] not in {p.pv_number for p in projects}:
                    return self.async_abort(
                        reason="wrong_account",
                        description_placeholders={
                            "pv_number": entry.data[CONF_PV_NUMBER]
                        },
                    )
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA,
                user_input or {CONF_EMAIL: entry.data[CONF_EMAIL]},
            ),
            description_placeholders={"pv_number": entry.data[CONF_PV_NUMBER]},
            errors=errors,
        )


def _project_label(project: Project) -> str:
    if project.installed_power_kwp:
        return f"{project.pv_number} ({project.installed_power_kwp:g} kWc)"
    return project.pv_number
