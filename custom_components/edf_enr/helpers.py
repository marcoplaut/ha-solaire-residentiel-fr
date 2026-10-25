"""Helpers shared by the config flow and the integration setup."""

from __future__ import annotations

from aiohttp import CookieJar

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EdfEnrClient


def create_client(hass: HomeAssistant, email: str, password: str) -> EdfEnrClient:
    """Create a client with a dedicated HTTP session.

    The Azure AD B2C login relies on cookies whose values must be sent
    unquoted, and the client clears the cookie jar before each login, so the
    session cannot be shared.
    """
    session = async_create_clientsession(hass, cookie_jar=CookieJar(quote_cookie=False))
    return EdfEnrClient(session, email, password)


def create_client_from_data(hass: HomeAssistant, data: dict[str, str]) -> EdfEnrClient:
    """Create a client from config entry data or form input."""
    return create_client(hass, data[CONF_EMAIL], data[CONF_PASSWORD])
