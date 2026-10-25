"""In-process simulator of the solar provider's customer portal.

It reproduces what was observed on the real portal:

* Azure AD B2C login: login page with ``var SETTINGS``, cookies whose values
  contain characters that aiohttp quotes by default, CSRF header, PKCE (S256)
  code exchange, rotating refresh tokens, short-lived access tokens;
* telemetry with naive Europe/Paris timestamps carrying a bogus "Z", an
  inclusive end bound, and negative placeholders for buckets not reported yet;
* corrupted hourly buckets (meter index, values in Wh) while the daily totals
  stay right;
* failure injection (HTTP errors, rate limiting, token revocation, password
  change) to exercise the integration end to end.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import secrets
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiohttp import web

from homeassistant.util import dt as dt_util

PARIS = ZoneInfo("Europe/Paris")
POLICY = "B2C_1A_SIGNIN_B2C"
# Base64 values with "/" and "=": quoted by aiohttp's default cookie jar,
# which the real B2C rejects.
TRANS_COOKIE = "x-ms-cpim-trans"
PLACEHOLDER = -34604.208


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(claims: dict[str, Any]) -> str:
    """Return an unsigned JWT carrying the claims."""
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.{_b64url(secrets.token_bytes(8))}"


def profile(local: datetime) -> dict[str, float]:
    """Energy (kWh) of the hour starting at a naive local wall-clock time."""
    solar = {11: 0.4, 12: 1.1, 13: 2.0, 14: 2.5, 15: 2.4, 16: 1.8, 17: 0.9, 18: 0.2}
    production = solar.get(local.hour, 0.0)
    self_consumption = round(min(production, 0.7), 3)
    grid_import = 0.0 if production >= 0.7 else round(0.7 - self_consumption, 3)
    return {
        "ecsConsumption": 0.0,
        "solarProduction": production,
        "autoConsumption": self_consumption,
        "networkConsumption": grid_import,
    }


@dataclass
class Account:
    """A customer account of the simulated portal."""

    email: str
    password: str
    contact_id: str
    pv_numbers: list[str]


@dataclass
class _Transaction:
    challenge: str
    csrf: str
    cookie: str
    authenticated_email: str | None = None


@dataclass
class _Grant:
    email: str
    expires_at: datetime


@dataclass
class PortalSimulator:
    """State and HTTP handlers of the simulated portal."""

    accounts: list[Account]
    redirect_uri: str = ""
    access_token_ttl: timedelta = timedelta(minutes=30)
    refresh_token_ttl: timedelta = timedelta(hours=24)
    # b2CCommissioningDate announced by the CRM.
    commissioning_date: str = "2026-10-23T00:00:00Z"
    # Telemetry is reported up to this time (default: now).
    horizon: datetime | None = None
    # Per naive local hour overrides of the profile.
    overrides: dict[datetime, dict[str, float]] = field(default_factory=dict)
    # Per naive local hour corrupted values, served by the hourly endpoint only
    # (the daily totals stay right, like on the real portal).
    corruptions: dict[datetime, dict[str, float]] = field(default_factory=dict)
    # Injected failures: path fragment -> list of statuses returned in order.
    failures: dict[str, list[int]] = field(default_factory=dict)
    retry_after: str | None = None
    counters: dict[str, int] = field(default_factory=dict)
    _transactions: dict[str, _Transaction] = field(default_factory=dict)
    _codes: dict[str, tuple[str, str]] = field(default_factory=dict)
    _access_tokens: dict[str, _Grant] = field(default_factory=dict)
    _refresh_tokens: dict[str, _Grant] = field(default_factory=dict)

    # -------------------------------------------------------------- control

    def app(self) -> web.Application:
        """Return the aiohttp application serving the portal."""
        app = web.Application(middlewares=[self._failure_middleware])
        app.router.add_get("/b2c/oauth2/v2.0/authorize", self._authorize)
        app.router.add_post("/b2c/SelfAsserted", self._self_asserted)
        app.router.add_get(
            "/b2c/api/CombinedSigninAndSignup/confirmed", self._confirmed
        )
        app.router.add_post("/b2c/oauth2/v2.0/token", self._token)
        app.router.add_get(
            "/api/crm/api/v2/Contact/{contact}/withopportunities", self._contact
        )
        app.router.add_get(
            "/api/api/installation/v1.0/installations", self._installations
        )
        app.router.add_get(
            "/api/api/installation/v1.0/installations/{installation}/equipements",
            self._equipments,
        )
        app.router.add_get("/api/api/device/v2.0/devices/{device}", self._device)
        app.router.add_get(
            "/api/api/installation/telemetries/v4.0/energy/{pv}/{granularity}",
            self._energy,
        )
        return app

    def fail(self, path_fragment: str, *statuses: int) -> None:
        """Make the next requests whose path contains a fragment fail."""
        self.failures.setdefault(path_fragment, []).extend(statuses)

    def revoke_access_tokens(self) -> None:
        """Invalidate all access tokens (refresh tokens stay valid)."""
        self._access_tokens.clear()

    def change_password(self, email: str, password: str) -> None:
        """Change a password and revoke every token of the account."""
        self._account(email).password = password
        for store in (self._access_tokens, self._refresh_tokens):
            for token, grant in list(store.items()):
                if grant.email == email:
                    del store[token]

    def count(self, name: str) -> int:
        """Return how many times an event happened."""
        return self.counters.get(name, 0)

    def _hit(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1

    def _account(self, email: str) -> Account:
        return next(a for a in self.accounts if a.email == email)

    @web.middleware
    async def _failure_middleware(
        self, request: web.Request, handler: Any
    ) -> web.StreamResponse:
        for fragment, statuses in self.failures.items():
            if fragment in request.path and statuses:
                status = statuses.pop(0)
                self._hit(f"failure_{status}")
                headers = {"Retry-After": self.retry_after} if self.retry_after else {}
                return web.Response(status=status, text="injected", headers=headers)
        return await handler(request)

    # ------------------------------------------------------------------ B2C

    async def _authorize(self, request: web.Request) -> web.Response:
        query = request.query
        if (
            query.get("code_challenge_method") != "S256"
            or "code_challenge" not in query
        ):
            return web.Response(status=400, text="PKCE required")
        trans_id = f"StateProperties={_b64url(secrets.token_bytes(12))}"
        transaction = _Transaction(
            challenge=query["code_challenge"],
            csrf=_b64url(secrets.token_bytes(16)),
            cookie=base64.b64encode(secrets.token_bytes(24)).decode() + "/x==",
        )
        self._transactions[trans_id] = transaction
        settings = {
            "api": "CombinedSigninAndSignup",
            "csrf": transaction.csrf,
            "transId": trans_id,
            "hosts": {"policy": POLICY},
        }
        page = (
            "<!DOCTYPE html><html><body><script>\n"
            f"var SETTINGS = {json.dumps(settings)};\n"
            "</script></body></html>"
        )
        response = web.Response(text=page, content_type="text/html")
        response.set_cookie(TRANS_COOKIE, transaction.cookie, secure=False)
        return response

    def _transaction(self, request: web.Request) -> _Transaction | None:
        transaction = self._transactions.get(request.query.get("tx", ""))
        if (
            transaction is None
            or request.query.get("p") != POLICY
            or _raw_cookie(request, TRANS_COOKIE) != transaction.cookie
        ):
            return None
        return transaction

    async def _self_asserted(self, request: web.Request) -> web.Response:
        transaction = self._transaction(request)
        if (
            transaction is None
            or request.headers.get("X-CSRF-TOKEN") != transaction.csrf
        ):
            return web.Response(status=400, text="Bad Request")
        form = await request.post()
        email = str(form.get("signInName", ""))
        account = next((a for a in self.accounts if a.email == email), None)
        if (
            form.get("request_type") != "RESPONSE"
            or account is None
            or account.password != form.get("password")
        ):
            self._hit("login_rejected")
            return web.json_response(
                {"status": "400", "errorCode": "AADB2C90225", "message": "Invalid"}
            )
        transaction.authenticated_email = email
        return web.json_response({"status": "200"})

    async def _confirmed(self, request: web.Request) -> web.Response:
        transaction = self._transaction(request)
        if (
            transaction is None
            or request.query.get("csrf_token") != transaction.csrf
            or transaction.authenticated_email is None
        ):
            location = (
                f"{self.redirect_uri}/#error=access_denied&error_description=nope"
            )
        else:
            code = _b64url(secrets.token_bytes(16))
            self._codes[code] = (transaction.authenticated_email, transaction.challenge)
            location = f"{self.redirect_uri}/#{urlencode({'state': 's', 'code': code})}"
        raise web.HTTPFound(location)

    async def _token(self, request: web.Request) -> web.Response:
        if request.headers.get("Origin") != self.redirect_uri:
            return web.json_response({"error": "invalid_request"}, status=400)
        form = await request.post()
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            email, challenge = self._codes.pop(str(form.get("code")), ("", ""))
            verifier = str(form.get("code_verifier", ""))
            expected = _b64url(hashlib.sha256(verifier.encode()).digest())
            if not email or expected != challenge:
                return web.json_response({"error": "invalid_grant"}, status=400)
            self._hit("login")
        elif grant_type == "refresh_token":
            grant = self._refresh_tokens.pop(str(form.get("refresh_token")), None)
            if grant is None or grant.expires_at <= dt_util.utcnow():
                self._hit("refresh_rejected")
                return web.json_response({"error": "invalid_grant"}, status=400)
            email = grant.email
            self._hit("refresh")
        else:
            return web.json_response({"error": "unsupported_grant_type"}, status=400)
        return web.json_response(self._issue_tokens(email))

    def _issue_tokens(self, email: str) -> dict[str, Any]:
        now = dt_util.utcnow()
        account = self._account(email)
        access_token = make_jwt(
            {
                "extension_IDContact": account.contact_id,
                "exp": int((now + self.access_token_ttl).timestamp()),
                "jti": _b64url(secrets.token_bytes(8)),
            }
        )
        refresh_token = _b64url(secrets.token_bytes(32))
        self._access_tokens[access_token] = _Grant(email, now + self.access_token_ttl)
        self._refresh_tokens[refresh_token] = _Grant(
            email, now + self.refresh_token_ttl
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(self.access_token_ttl.total_seconds()),
            "refresh_token": refresh_token,
            "refresh_token_expires_in": int(self.refresh_token_ttl.total_seconds()),
        }

    # ------------------------------------------------------------------ API

    def _authorized_account(self, request: web.Request) -> Account:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        grant = self._access_tokens.get(token)
        if grant is None or grant.expires_at <= dt_util.utcnow():
            self._hit("api_unauthorized")
            raise web.HTTPUnauthorized(text="token expired or revoked")
        self._hit("api")
        return self._account(grant.email)

    def _authorized_pv(self, request: web.Request, pv_number: str) -> None:
        if pv_number not in self._authorized_account(request).pv_numbers:
            raise web.HTTPNotFound(text=json.dumps(f"Project {pv_number} not found"))

    async def _contact(self, request: web.Request) -> web.Response:
        account = self._authorized_account(request)
        if request.match_info["contact"] != account.contact_id:
            raise web.HTTPForbidden
        return web.json_response(
            {
                "objectId": account.contact_id,
                "opportunities": [
                    {
                        "objectId": f"opp-{pv}",
                        "pvNumber": pv,
                        "name": f"Customer - {pv}",
                        "offer": {"objectId": "offer", "name": "Mon Soleil & Moi"},
                        "powerInstallation": 3.0,
                        "b2CCommissioningDate": self.commissioning_date,
                    }
                    for pv in account.pv_numbers
                ],
            }
        )

    async def _installations(self, request: web.Request) -> web.Response:
        pv_number = request.query.get("NumeroProjet", "")
        self._authorized_pv(request, pv_number)
        return web.json_response(
            {"items": [{"installation": {"idInstallation": f"inst-{pv_number}"}}]}
        )

    async def _equipments(self, request: web.Request) -> web.Response:
        pv_number = request.match_info["installation"].removeprefix("inst-")
        self._authorized_pv(request, pv_number)
        return web.json_response(
            {
                "equipements": [
                    {
                        "type": "Passerelle",
                        "idEquipement": f"{pv_number}|yuzeg1",
                        "sousType": "YuzeG1",
                        "marque": "Fournisseur solaire",
                    }
                ]
            }
        )

    async def _device(self, request: web.Request) -> web.Response:
        device = request.match_info["device"]
        self._authorized_pv(request, device.split("|")[0])
        return web.json_response(
            {
                "id": device,
                "type": "YuzeG1",
                "status": "MisEnService",
                "lastReportedAt": int(dt_util.utcnow().timestamp()) - 600,
            }
        )

    def bucket(self, local: datetime) -> dict[str, float]:
        """Return the energies of an hour bucket labelled with a local time."""
        horizon = (self.horizon or dt_util.utcnow()).astimezone(PARIS)
        if local > horizon.replace(tzinfo=None, minute=0, second=0, microsecond=0):
            return dict.fromkeys(profile(local), PLACEHOLDER)
        return self.overrides.get(local, profile(local))

    def daily_bucket(self, local_day: datetime) -> dict[str, float]:
        """Return the energies of a day: the sum of its reported hour buckets."""
        totals = dict.fromkeys(profile(local_day), 0.0)
        for hour in range(24):
            energies = self.bucket(local_day + timedelta(hours=hour))
            if PLACEHOLDER in energies.values():
                continue
            for key, value in energies.items():
                totals[key] += value
        return {key: round(value, 3) for key, value in totals.items()}

    async def _energy(self, request: web.Request) -> web.Response:
        pv_number = request.match_info["pv"]
        self._authorized_pv(request, pv_number)
        granularity = request.match_info["granularity"]
        if granularity not in ("hourly", "daily"):
            raise web.HTTPBadRequest(text="only hourly and daily are simulated")
        unit = "hour" if granularity == "hourly" else "day"
        try:
            begin = _parse_naive(request.query[f"begin-{unit}"])
            end = _parse_naive(request.query[f"end-{unit}"])
        except (KeyError, ValueError) as err:
            raise web.HTTPBadRequest(text="bad range") from err
        now_local = dt_util.utcnow().astimezone(PARIS).replace(tzinfo=None)
        items = []
        # Naive wall-clock iteration, inclusive end, like the real portal.
        if granularity == "daily":
            local = begin.replace(hour=0, minute=0, second=0, microsecond=0)
            while local <= end and local <= now_local:
                items.append(
                    {
                        "day": local.strftime("%Y-%m-%d"),
                        "dateStart": local.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "dateEnd": (local + timedelta(days=1)).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "energies": self.daily_bucket(local),
                    }
                )
                local += timedelta(days=1)
            return web.json_response({"dailyElecEnergies": items})
        local = begin.replace(minute=0, second=0, microsecond=0)
        while local <= end and local <= now_local:
            items.append(
                {
                    "hour": local.strftime("%Y-%m-%d:%H.%M.%S"),
                    "dateStart": local.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "dateEnd": (local + timedelta(hours=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "energies": self.corruptions.get(local, self.bucket(local)),
                    "analyzes": {"autoConsumptionPercentage": 0, "coverPercentage": 0},
                }
            )
            local += timedelta(hours=1)
        return web.json_response({"hourlyElecEnergies": items})


def _raw_cookie(request: web.Request, name: str) -> str | None:
    """Return a cookie value exactly as sent (B2C does not unquote values)."""
    for part in request.headers.get("Cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _parse_naive(value: str) -> datetime:
    """Parse a query timestamp the way the portal does (the "Z" is ignored)."""
    parsed = datetime.fromisoformat(value.removesuffix("Z"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(PARIS).replace(tzinfo=None)
    return parsed


def local_hour_to_utc(local: datetime) -> datetime:
    """Convert a naive Paris wall-clock time to UTC like the integration does."""
    return local.replace(tzinfo=PARIS).astimezone(UTC)
