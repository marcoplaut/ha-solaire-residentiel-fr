"""Async client for the solar provider's customer portal."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import math
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from .const import (
    API_BASE_URL,
    B2C_BASE_URL,
    B2C_POLICY,
    CLIENT_ID,
    REDIRECT_URI,
    REQUEST_TIMEOUT,
    SCOPE,
    TOKEN_EXPIRY_MARGIN,
    USER_AGENT,
)
from .exceptions import (
    EdfEnrApiError,
    EdfEnrAuthError,
    EdfEnrConnectionError,
    EdfEnrNotFoundError,
    EdfEnrRateLimitError,
)
from .models import (
    Device,
    EnergyReading,
    Equipment,
    Granularity,
    Installation,
    Project,
    TokenSet,
    format_api_datetime,
    parse_device,
    parse_energy_reading,
    parse_equipment,
    parse_project,
)

_LOGGER = logging.getLogger(__name__)

_SETTINGS_RE = re.compile(r"var SETTINGS = (\{.*?\});\s*$", re.MULTILINE)

# OAuth error codes that report a temporary authorization server failure.
_TRANSIENT_OAUTH_ERRORS = frozenset({"server_error", "temporarily_unavailable"})

# The portal iterates in Paris wall-clock time. On the spring DST change its
# phantom 02:00 buckets belong to the UTC hour that starts at 03:00 Paris time,
# so a range starting at that hour would miss them: sub-daily ranges are
# requested from one hour earlier (the extra buckets are filtered out).
_BEGIN_MARGIN = {
    Granularity.MINUTELY: timedelta(hours=1),
    Granularity.HOURLY: timedelta(hours=1),
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Return the (unverified) claims of a JWT."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except (IndexError, ValueError) as err:
        raise EdfEnrApiError("Malformed access token") from err
    if not isinstance(claims, dict):
        raise EdfEnrApiError("Malformed access token")
    return claims


class EdfEnrClient:
    """Client for the solar provider's customer portal.

    The session must be dedicated to this client: the Azure AD B2C login relies
    on cookies, and the cookie jar is cleared before each login.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        *,
        api_base_url: str = API_BASE_URL,
        b2c_base_url: str = B2C_BASE_URL,
        redirect_uri: str = REDIRECT_URI,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._email = email
        self._password = password
        self._api_base_url = api_base_url.rstrip("/")
        self._b2c_base_url = b2c_base_url.rstrip("/")
        self._redirect_uri = redirect_uri
        self._tokens: TokenSet | None = None
        self._contact_id: str | None = None
        self._token_lock = asyncio.Lock()

    @property
    def contact_id(self) -> str | None:
        """Return the CRM contact id of the logged in account."""
        return self._contact_id

    # ------------------------------------------------------------------ auth

    async def async_login(self) -> None:
        """Log in with email and password (Azure AD B2C, code flow + PKCE)."""
        async with self._token_lock:
            await self._login()

    async def _ensure_token(self) -> str:
        async with self._token_lock:
            now = datetime.now(UTC)
            tokens = self._tokens
            if (
                tokens
                and tokens.expires_at - timedelta(seconds=TOKEN_EXPIRY_MARGIN) > now
            ):
                return tokens.access_token
            if (
                tokens
                and tokens.refresh_token
                and (
                    tokens.refresh_expires_at is None or tokens.refresh_expires_at > now
                )
            ):
                try:
                    await self._refresh(tokens.refresh_token)
                except EdfEnrAuthError, EdfEnrApiError:
                    _LOGGER.debug("Token refresh rejected, logging in again")
                    await self._login()
            else:
                await self._login()
            if self._tokens is None:  # pragma: no cover - _login/_refresh set it
                raise EdfEnrApiError("No token after login")
            return self._tokens.access_token

    async def _invalidate_token(self, access_token: str) -> None:
        async with self._token_lock:
            if self._tokens and self._tokens.access_token == access_token:
                # Keep the refresh token: the next call refreshes instead of logging in.
                self._tokens = TokenSet(
                    access_token="",
                    refresh_token=self._tokens.refresh_token,
                    expires_at=datetime.fromtimestamp(0, UTC),
                    refresh_expires_at=self._tokens.refresh_expires_at,
                )

    async def _relogin(self, rejected_token: str) -> None:
        """Log in again, unless a concurrent call already replaced the token."""
        async with self._token_lock:
            if self._tokens is None or self._tokens.access_token == rejected_token:
                await self._login()

    async def _login(self) -> None:
        self._session.cookie_jar.clear()
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        authorize_params = {
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "redirect_uri": self._redirect_uri,
            "response_mode": "fragment",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": _b64url(secrets.token_bytes(16)),
            "nonce": _b64url(secrets.token_bytes(16)),
        }
        status, page, page_url, _ = await self._raw(
            "GET",
            f"{self._b2c_base_url}/oauth2/v2.0/authorize",
            params=authorize_params,
        )
        if status != 200:
            raise EdfEnrConnectionError(f"Login page returned HTTP {status}")
        settings = self._parse_settings(page)

        policy = B2C_POLICY.upper()
        status, body, _, _ = await self._raw(
            "POST",
            f"{self._b2c_base_url}/SelfAsserted",
            params={"tx": settings["transId"], "p": policy},
            headers={
                "X-CSRF-TOKEN": settings["csrf"],
                "X-Requested-With": "XMLHttpRequest",
                "Referer": page_url,
            },
            data={
                "request_type": "RESPONSE",
                "signInName": self._email,
                "password": self._password,
            },
        )
        if status != 200:
            raise EdfEnrConnectionError(f"Credential check returned HTTP {status}")
        try:
            result = json.loads(body)
        except ValueError as err:
            raise EdfEnrApiError("Unexpected credential check response") from err
        if not isinstance(result, dict) or str(result.get("status")) != "200":
            raise EdfEnrAuthError("Invalid email or password")

        status, _, _, headers = await self._raw(
            "GET",
            f"{self._b2c_base_url}/api/{settings['api']}/confirmed",
            params={
                "rememberMe": "false",
                "csrf_token": settings["csrf"],
                "tx": settings["transId"],
                "p": policy,
            },
            allow_redirects=False,
        )
        fragment = parse_qs(urlparse(headers.get("Location", "")).fragment)
        if "error" in fragment:
            message = fragment.get("error_description", fragment["error"])[0]
            # The password was accepted above: OAuth's temporary failures
            # (RFC 6749 4.1.2.1) are not a reason to ask for new credentials.
            if fragment["error"][0] in _TRANSIENT_OAUTH_ERRORS:
                raise EdfEnrConnectionError(message)
            raise EdfEnrAuthError(message)
        if status not in (301, 302, 303) or "code" not in fragment:
            raise EdfEnrApiError(
                f"Login confirmation returned HTTP {status} without code"
            )

        try:
            await self._token_request(
                {
                    "grant_type": "authorization_code",
                    "code": fragment["code"][0],
                    "code_verifier": verifier,
                }
            )
        except EdfEnrAuthError as err:
            # The password was accepted above, so a rejected code is a portal
            # fault (expired code, B2C internal error), not a credentials one.
            raise EdfEnrApiError(f"Authorization code rejected: {err}") from err
        self._session.cookie_jar.clear()

    async def _refresh(self, refresh_token: str) -> None:
        await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def _token_request(self, grant: dict[str, str]) -> None:
        status, body, _, _ = await self._raw(
            "POST",
            f"{self._b2c_base_url}/oauth2/v2.0/token",
            headers={"Origin": self._redirect_uri},
            data={
                "client_id": CLIENT_ID,
                "scope": SCOPE,
                "redirect_uri": self._redirect_uri,
                **grant,
            },
        )
        if status in (400, 401):
            raise EdfEnrAuthError(f"Token request rejected (HTTP {status})")
        if status != 200:
            raise EdfEnrConnectionError(f"Token endpoint returned HTTP {status}")
        try:
            payload = json.loads(body)
            access_token = payload["access_token"]
            expires_in = int(payload["expires_in"])
        except (ValueError, KeyError, TypeError) as err:
            raise EdfEnrApiError("Unexpected token response") from err
        now = datetime.now(UTC)
        refresh_expires_in = payload.get("refresh_token_expires_in")
        self._tokens = TokenSet(
            access_token=access_token,
            refresh_token=payload.get("refresh_token"),
            expires_at=now + timedelta(seconds=expires_in),
            refresh_expires_at=(
                now + timedelta(seconds=int(refresh_expires_in))
                if isinstance(refresh_expires_in, int | str)
                and str(refresh_expires_in).isdigit()
                else None
            ),
        )
        contact_id = decode_jwt_claims(access_token).get("extension_IDContact")
        if not isinstance(contact_id, str) or not contact_id:
            raise EdfEnrApiError("Access token without contact id")
        self._contact_id = contact_id

    @staticmethod
    def _parse_settings(page: str) -> dict[str, str]:
        match = _SETTINGS_RE.search(page)
        if not match:
            raise EdfEnrApiError("Login page without SETTINGS")
        try:
            settings = json.loads(match.group(1))
        except ValueError as err:
            raise EdfEnrApiError("Unreadable login page SETTINGS") from err
        keys = ("csrf", "transId", "api")
        if not isinstance(settings, dict) or not all(
            isinstance(settings.get(key), str) for key in keys
        ):
            raise EdfEnrApiError("Login page SETTINGS without csrf/transId/api")
        return {key: settings[key] for key in keys}

    # --------------------------------------------------------------- transport

    async def _raw(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> tuple[int, str, str, dict[str, str]]:
        """Perform a request and return status, body, final url and headers."""
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                headers={"User-Agent": USER_AGENT, **(headers or {})},
                data=data,
                allow_redirects=allow_redirects,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 429:
                    raise EdfEnrRateLimitError(
                        _retry_after(resp.headers.get("Retry-After"))
                    )
                return resp.status, await resp.text(), str(resp.url), dict(resp.headers)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EdfEnrConnectionError(
                f"Error talking to {urlparse(url).hostname}"
            ) from err

    async def _authorized_get(
        self, path: str, params: dict[str, str] | None
    ) -> tuple[int, str, str]:
        """GET with the current access token; return status, body and token."""
        access_token = await self._ensure_token()
        status, body, _, _ = await self._raw(
            "GET",
            f"{self._api_base_url}{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        return status, body, access_token

    async def _api_get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        """GET an authenticated API resource and return its JSON payload."""
        status, body, access_token = await self._authorized_get(path, params)
        if status in (401, 403):
            # The token may have been revoked early: renew it and retry once.
            # Only the rejected token is dropped: a concurrent call may already
            # have replaced it with a fresh one.
            _LOGGER.debug("API answered HTTP %s, renewing the token", status)
            await self._invalidate_token(access_token)
            status, body, access_token = await self._authorized_get(path, params)
        if status in (401, 403):
            # A renewed token is refused too: check the credentials with a full
            # login, which raises EdfEnrAuthError if they are really rejected.
            _LOGGER.debug("API answered HTTP %s again, logging in again", status)
            await self._relogin(access_token)
            status, body, _ = await self._authorized_get(path, params)
            if status in (401, 403):
                # The credentials are valid: the API refuses this resource.
                raise EdfEnrApiError(f"API refused the request (HTTP {status})")
        if status == 404:
            raise EdfEnrNotFoundError(f"{path.split('?', maxsplit=1)[0]} not found")
        if status >= 500:
            raise EdfEnrConnectionError(f"API returned HTTP {status}")
        if status != 200:
            raise EdfEnrApiError(f"API returned HTTP {status}")
        try:
            return json.loads(body)
        except ValueError as err:
            raise EdfEnrApiError("API returned invalid JSON") from err

    # -------------------------------------------------------------- resources

    async def async_get_projects(self) -> list[Project]:
        """Return the solar projects of the account."""
        await self._ensure_token()
        payload = await self._api_get(
            f"/crm/api/v2/Contact/{self._contact_id}/withopportunities"
        )
        opportunities = (
            payload.get("opportunities") if isinstance(payload, dict) else None
        )
        if not isinstance(opportunities, list):
            raise EdfEnrApiError("Contact without opportunities list")
        # Several opportunities (e.g. a battery upsell) can share one PV number.
        # Keep the one describing the installation (commissioning date, power),
        # whatever the order of the CRM answer.
        projects: dict[str, Project] = {}
        for item in opportunities:
            if (project := parse_project(item)) is None:
                continue
            known = projects.get(project.pv_number)
            if known is None or _completeness(project) > _completeness(known):
                projects[project.pv_number] = project
        return list(projects.values())

    async def async_get_installation(self, pv_number: str) -> Installation | None:
        """Return the installation of a project, if the portal has one."""
        payload = await self._api_get(
            "/api/installation/v1.0/installations", {"NumeroProjet": pv_number}
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise EdfEnrApiError("Installation search without items list")
        for item in items:
            installation = item.get("installation") if isinstance(item, dict) else None
            if isinstance(installation, dict) and isinstance(
                installation.get("idInstallation"), str
            ):
                return Installation(installation["idInstallation"], pv_number)
        return None

    async def async_get_equipments(self, installation_id: str) -> list[Equipment]:
        """Return the equipments of an installation."""
        payload = await self._api_get(
            f"/api/installation/v1.0/installations/{installation_id}/equipements"
        )
        items = payload.get("equipements") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise EdfEnrApiError("Installation without equipements list")
        return [equipment for item in items if (equipment := parse_equipment(item))]

    async def async_get_device(self, device_id: str) -> Device:
        """Return the status of a monitoring device."""
        return parse_device(
            await self._api_get(f"/api/device/v2.0/devices/{device_id}")
        )

    async def async_get_energy(
        self,
        pv_number: str,
        granularity: Granularity,
        start: datetime,
        end: datetime,
    ) -> list[EnergyReading]:
        """Return energy buckets starting in [start, end), sorted by start.

        The portal treats the end bound as inclusive; it is made exclusive here.
        """
        unit = granularity.unit
        payload = await self._api_get(
            f"/api/installation/telemetries/v4.0/energy/{pv_number}/{granularity.value}",
            {
                f"begin-{unit}": format_api_datetime(
                    start - _BEGIN_MARGIN.get(granularity, timedelta(0))
                ),
                f"end-{unit}": format_api_datetime(end),
            },
        )
        items = (
            payload.get(granularity.response_key) if isinstance(payload, dict) else None
        )
        if not isinstance(items, list):
            raise EdfEnrApiError(f"Telemetry without {granularity.response_key}")
        readings: list[EnergyReading] = []
        errors: list[EdfEnrApiError] = []
        for item in items:
            try:
                readings.append(parse_energy_reading(item, granularity))
            except EdfEnrApiError as err:
                errors.append(err)
        if errors:
            if not readings:
                raise errors[0]
            # One unreadable bucket must not block the whole range (and the
            # statistics import) forever: keep the others.
            _LOGGER.warning(
                "Skipped %s unreadable telemetry bucket(s): %s", len(errors), errors[0]
            )
        return sorted(
            (reading for reading in readings if start <= reading.start < end),
            key=lambda reading: reading.start,
        )


def _completeness(project: Project) -> int:
    """Return how many installation details an opportunity carries."""
    return (project.commissioning_date is not None) + (
        project.installed_power_kwp is not None
    )


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        return None
    # float() also accepts "inf" and "nan", which no scheduler can use.
    return max(delay, 0.0) if math.isfinite(delay) else None
