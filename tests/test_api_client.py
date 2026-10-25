"""Tests for the customer portal API client."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import aiohttp
from aioresponses import CallbackResult, aioresponses
from freezegun.api import FrozenDateTimeFactory
import pytest

from custom_components.edf_enr.api import EdfEnrClient
from custom_components.edf_enr.api.client import decode_jwt_claims
from custom_components.edf_enr.api.const import (
    API_BASE_URL,
    B2C_BASE_URL,
    CLIENT_ID,
    REDIRECT_URI,
    TOKEN_EXPIRY_MARGIN,
)
from custom_components.edf_enr.api.exceptions import (
    EdfEnrApiError,
    EdfEnrAuthError,
    EdfEnrConnectionError,
    EdfEnrNotFoundError,
    EdfEnrRateLimitError,
)
from custom_components.edf_enr.api.models import (
    Device,
    Equipment,
    Granularity,
    Installation,
)

from .conftest import load_fixture, load_json_fixture
from .const import (
    CONTACT_ID,
    EMAIL,
    GATEWAY_ID,
    INSTALLATION_ID,
    NOW,
    PASSWORD,
    PV_NUMBER,
)

AUTHORIZE_URL = f"{B2C_BASE_URL}/oauth2/v2.0/authorize"
SELF_ASSERTED_URL = f"{B2C_BASE_URL}/SelfAsserted"
CONFIRMED_URL = f"{B2C_BASE_URL}/api/CombinedSigninAndSignup/confirmed"
TOKEN_URL = f"{B2C_BASE_URL}/oauth2/v2.0/token"


def _pattern(url: str) -> re.Pattern[str]:
    """Match a URL with any query string."""
    return re.compile(re.escape(url) + r"(\?.*)?$")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(claims: dict[str, Any] | None = None, **extra: Any) -> str:
    """Build an unsigned fake JWT."""
    payload = {"extension_IDContact": CONTACT_ID, **(claims or {}), **extra}
    header = _b64(json.dumps({"alg": "none"}).encode())
    return f"{header}.{_b64(json.dumps(payload).encode())}.signature"


def token_body(
    access_token: str | None = None,
    *,
    expires_in: Any = 3600,
    refresh_token: str | None = "refresh-1",  # noqa: S107
    refresh_expires_in: Any = 86400,
) -> dict[str, Any]:
    """Build a token endpoint response."""
    body: dict[str, Any] = {
        "access_token": access_token or make_jwt(),
        "expires_in": expires_in,
    }
    if refresh_token is not None:
        body["refresh_token"] = refresh_token
    if refresh_expires_in is not None:
        body["refresh_token_expires_in"] = refresh_expires_in
    return body


def mock_login(
    m: aioresponses,
    *,
    token: dict[str, Any] | None = None,
    location: str | None = None,
) -> None:
    """Register one successful login round trip."""
    m.get(
        _pattern(AUTHORIZE_URL),
        status=200,
        body=load_fixture("b2c_login_page.html"),
    )
    m.post(_pattern(SELF_ASSERTED_URL), status=200, payload={"status": "200"})
    m.get(
        _pattern(CONFIRMED_URL),
        status=302,
        headers={
            "Location": location or f"{REDIRECT_URI}/#code=the-code&state=whatever"
        },
    )
    m.post(_pattern(TOKEN_URL), status=200, payload=token or token_body())


def requests_to(m: aioresponses, method: str, url: str) -> list[dict[str, Any]]:
    """Return the keyword arguments of every request made to ``url``."""
    return [
        call.kwargs
        for (req_method, req_url), calls in m.requests.items()
        for call in calls
        if req_method == method and str(req_url).split("?")[0] == url
    ]


class _CompatClientResponse(aiohttp.ClientResponse):
    """ClientResponse accepting the arguments aioresponses passes.

    Recent aiohttp versions require a ``stream_writer`` that aioresponses does
    not provide yet.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("stream_writer", SimpleNamespace(output_size=0))
        super().__init__(*args, **kwargs)


@pytest.fixture
def mocked(monkeypatch: pytest.MonkeyPatch) -> Iterator[aioresponses]:
    monkeypatch.setattr("aioresponses.core.ClientResponse", _CompatClientResponse)
    with aioresponses() as m:
        yield m


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(quote_cookie=False)
    ) as session:
        yield session


@pytest.fixture
def client(session: aiohttp.ClientSession) -> EdfEnrClient:
    return EdfEnrClient(session, EMAIL, PASSWORD)


# ---------------------------------------------------------------------- login


async def test_login_success(mocked: aioresponses, client: EdfEnrClient) -> None:
    mock_login(mocked)

    await client.async_login()

    assert client.contact_id == CONTACT_ID

    (authorize,) = requests_to(mocked, "GET", AUTHORIZE_URL)
    params = authorize["params"]
    assert params["client_id"] == CLIENT_ID
    assert params["response_type"] == "code"
    assert params["response_mode"] == "fragment"
    assert params["code_challenge_method"] == "S256"
    assert params["redirect_uri"] == REDIRECT_URI

    (asserted,) = requests_to(mocked, "POST", SELF_ASSERTED_URL)
    assert asserted["params"] == {
        "tx": "StateProperties=eyJUSUQiOiJmYWtlIn0",
        "p": "B2C_1A_SIGNIN_B2C",
    }
    assert asserted["headers"]["X-CSRF-TOKEN"] == "fake-csrf-token"
    assert asserted["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert asserted["headers"]["Referer"].startswith(AUTHORIZE_URL)
    assert asserted["data"] == {
        "request_type": "RESPONSE",
        "signInName": EMAIL,
        "password": PASSWORD,
    }

    (confirmed,) = requests_to(mocked, "GET", CONFIRMED_URL)
    assert confirmed["allow_redirects"] is False
    assert confirmed["params"]["csrf_token"] == "fake-csrf-token"
    assert confirmed["params"]["tx"] == "StateProperties=eyJUSUQiOiJmYWtlIn0"
    assert confirmed["params"]["p"] == "B2C_1A_SIGNIN_B2C"

    (token,) = requests_to(mocked, "POST", TOKEN_URL)
    assert token["headers"]["Origin"] == REDIRECT_URI
    data = token["data"]
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "the-code"
    assert data["client_id"] == CLIENT_ID
    assert data["redirect_uri"] == REDIRECT_URI
    verifier = data["code_verifier"]
    expected_challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    assert params["code_challenge"] == expected_challenge


async def test_login_clears_cookies(
    mocked: aioresponses, session: aiohttp.ClientSession, client: EdfEnrClient
) -> None:
    session.cookie_jar.update_cookies({"stale": "1"})
    mock_login(mocked)

    await client.async_login()

    assert len(session.cookie_jar) == 0


async def test_login_page_http_error(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mocked.get(_pattern(AUTHORIZE_URL), status=500)

    with pytest.raises(EdfEnrConnectionError, match="HTTP 500"):
        await client.async_login()


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ("<html>nothing here</html>", "without SETTINGS"),
        ("var SETTINGS = {not json};\n", "Unreadable"),
        ('var SETTINGS = {"csrf": "c", "transId": "t"};\n', "csrf/transId/api"),
        ('var SETTINGS = {"csrf": "c", "transId": "t", "api": 3};\n', "csrf/transId"),
        ("var SETTINGS = [];\n", "without SETTINGS"),
    ],
)
async def test_login_page_bad_settings(
    mocked: aioresponses, client: EdfEnrClient, page: str, message: str
) -> None:
    mocked.get(_pattern(AUTHORIZE_URL), status=200, body=page)

    with pytest.raises(EdfEnrApiError, match=message):
        await client.async_login()


def _page_and_asserted(
    m: aioresponses, *, status: int = 200, body: str = '{"status": "200"}'
) -> None:
    m.get(_pattern(AUTHORIZE_URL), status=200, body=load_fixture("b2c_login_page.html"))
    m.post(_pattern(SELF_ASSERTED_URL), status=status, body=body)


async def test_login_self_asserted_http_error(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    _page_and_asserted(mocked, status=500, body="")

    with pytest.raises(EdfEnrConnectionError, match="HTTP 500"):
        await client.async_login()


async def test_login_self_asserted_invalid_json(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    _page_and_asserted(mocked, body="<html>oops</html>")

    with pytest.raises(EdfEnrApiError, match="Unexpected credential"):
        await client.async_login()


@pytest.mark.parametrize(
    "body",
    ['{"status": "400", "message": "bad"}', '{"status": 400}', "[]", '{"x": 1}'],
)
async def test_login_bad_credentials(
    mocked: aioresponses, client: EdfEnrClient, body: str
) -> None:
    _page_and_asserted(mocked, body=body)

    with pytest.raises(EdfEnrAuthError, match="Invalid email or password"):
        await client.async_login()


async def test_login_status_as_integer_is_accepted(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    mocked.clear()
    mocked.get(_pattern(AUTHORIZE_URL), body=load_fixture("b2c_login_page.html"))
    mocked.post(_pattern(SELF_ASSERTED_URL), payload={"status": 200})
    mocked.get(
        _pattern(CONFIRMED_URL),
        status=302,
        headers={"Location": f"{REDIRECT_URI}/#code=c"},
    )
    mocked.post(_pattern(TOKEN_URL), payload=token_body())

    await client.async_login()

    assert client.contact_id == CONTACT_ID


@pytest.mark.parametrize(
    ("location", "message"),
    [
        (
            f"{REDIRECT_URI}/#error=access_denied&error_description=Nope",
            "Nope",
        ),
        (f"{REDIRECT_URI}/#error=access_denied", "access_denied"),
    ],
)
async def test_login_confirmed_error_fragment(
    mocked: aioresponses, client: EdfEnrClient, location: str, message: str
) -> None:
    _page_and_asserted(mocked)
    mocked.get(_pattern(CONFIRMED_URL), status=302, headers={"Location": location})

    with pytest.raises(EdfEnrAuthError, match=message):
        await client.async_login()


@pytest.mark.parametrize("error", ["server_error", "temporarily_unavailable"])
async def test_login_confirmed_transient_error_fragment(
    mocked: aioresponses, client: EdfEnrClient, error: str
) -> None:
    """A temporary B2C failure after the password was accepted is no auth error."""
    _page_and_asserted(mocked)
    mocked.get(
        _pattern(CONFIRMED_URL),
        status=302,
        headers={"Location": f"{REDIRECT_URI}/#error={error}"},
    )

    with pytest.raises(EdfEnrConnectionError, match=error):
        await client.async_login()


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (302, {"Location": f"{REDIRECT_URI}/#state=x"}),
        (302, {}),
        (200, {"Location": f"{REDIRECT_URI}/#code=c"}),
        (500, {}),
    ],
)
async def test_login_confirmed_without_code(
    mocked: aioresponses,
    client: EdfEnrClient,
    status: int,
    headers: dict[str, str],
) -> None:
    _page_and_asserted(mocked)
    mocked.get(_pattern(CONFIRMED_URL), status=status, headers=headers)

    with pytest.raises(EdfEnrApiError, match="without code"):
        await client.async_login()


def _until_token(m: aioresponses) -> None:
    _page_and_asserted(m)
    m.get(
        _pattern(CONFIRMED_URL),
        status=302,
        headers={"Location": f"{REDIRECT_URI}/#code=c"},
    )


@pytest.mark.parametrize("status", [400, 401])
async def test_token_rejected(
    mocked: aioresponses, client: EdfEnrClient, status: int
) -> None:
    """The password was accepted: a rejected code is a portal fault."""
    _until_token(mocked)
    mocked.post(_pattern(TOKEN_URL), status=status, body="{}")

    with pytest.raises(EdfEnrApiError, match=f"code rejected.*HTTP {status}"):
        await client.async_login()


async def test_token_server_error(mocked: aioresponses, client: EdfEnrClient) -> None:
    _until_token(mocked)
    mocked.post(_pattern(TOKEN_URL), status=500)

    with pytest.raises(EdfEnrConnectionError, match="HTTP 500"):
        await client.async_login()


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        "{}",
        '{"access_token": "x"}',
        '{"access_token": "x", "expires_in": "soon"}',
        '{"access_token": "x", "expires_in": null}',
    ],
)
async def test_token_malformed_response(
    mocked: aioresponses, client: EdfEnrClient, body: str
) -> None:
    _until_token(mocked)
    mocked.post(_pattern(TOKEN_URL), status=200, body=body)

    with pytest.raises(EdfEnrApiError, match="Unexpected token response"):
        await client.async_login()


async def test_token_without_contact_claim(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    _until_token(mocked)
    header = _b64(b"{}")
    jwt = f"{header}.{_b64(json.dumps({'sub': 'x'}).encode())}.sig"
    mocked.post(_pattern(TOKEN_URL), payload=token_body(jwt))

    with pytest.raises(EdfEnrApiError, match="without contact id"):
        await client.async_login()


async def test_token_with_empty_contact_claim(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    _until_token(mocked)
    mocked.post(
        _pattern(TOKEN_URL), payload=token_body(make_jwt(extension_IDContact=""))
    )

    with pytest.raises(EdfEnrApiError, match="without contact id"):
        await client.async_login()


async def test_token_not_a_jwt(mocked: aioresponses, client: EdfEnrClient) -> None:
    _until_token(mocked)
    mocked.post(_pattern(TOKEN_URL), payload=token_body("opaque"))

    with pytest.raises(EdfEnrApiError, match="Malformed access token"):
        await client.async_login()


@pytest.mark.parametrize(
    "token",
    [
        "opaque",
        "a.!!!.c",
        f"a.{_b64(b'not json')}.c",
        f"a.{_b64(b'[1, 2]')}.c",
    ],
)
def test_decode_jwt_claims_malformed(token: str) -> None:
    with pytest.raises(EdfEnrApiError, match="Malformed access token"):
        decode_jwt_claims(token)


def test_decode_jwt_claims() -> None:
    assert decode_jwt_claims(make_jwt(foo="bar"))["foo"] == "bar"


@pytest.mark.parametrize(
    "exception", [aiohttp.ClientConnectionError("boom"), TimeoutError()]
)
async def test_login_connection_error(
    mocked: aioresponses, client: EdfEnrClient, exception: Exception
) -> None:
    mocked.get(_pattern(AUTHORIZE_URL), exception=exception)

    with pytest.raises(EdfEnrConnectionError, match=r"b2clogin\.com"):
        await client.async_login()


async def test_login_rate_limited(mocked: aioresponses, client: EdfEnrClient) -> None:
    mocked.get(_pattern(AUTHORIZE_URL), status=429, headers={"Retry-After": "7"})

    with pytest.raises(EdfEnrRateLimitError) as exc_info:
        await client.async_login()
    assert exc_info.value.retry_after == 7.0


# ------------------------------------------------------------ token lifecycle


def _device_url(device_id: str = GATEWAY_ID) -> str:
    return f"{API_BASE_URL}/api/device/v2.0/devices/{quote(device_id, safe='')}"


async def test_token_reused_while_valid(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    await client.async_get_device(GATEWAY_ID)
    await client.async_get_device(GATEWAY_ID)

    assert len(requests_to(mocked, "GET", AUTHORIZE_URL)) == 1
    assert len(requests_to(mocked, "POST", TOKEN_URL)) == 1
    calls = requests_to(mocked, "GET", _device_url())
    assert len(calls) == 2
    token = make_jwt()
    assert calls[0]["headers"]["Authorization"] == f"Bearer {token}"
    assert calls[0]["headers"]["Accept"] == "application/json"
    assert "User-Agent" in calls[0]["headers"]


async def test_refresh_near_expiry(
    mocked: aioresponses, client: EdfEnrClient, frozen_time: FrozenDateTimeFactory
) -> None:
    mock_login(mocked)
    new_jwt = make_jwt(jti="second")
    mocked.post(
        _pattern(TOKEN_URL),
        payload=token_body(new_jwt, refresh_token="refresh-2"),
    )
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    await client.async_get_device(GATEWAY_ID)
    frozen_time.tick(timedelta(seconds=3600 - TOKEN_EXPIRY_MARGIN + 1))
    await client.async_get_device(GATEWAY_ID)

    assert len(requests_to(mocked, "GET", AUTHORIZE_URL)) == 1
    first, second = requests_to(mocked, "POST", TOKEN_URL)
    assert second["data"]["grant_type"] == "refresh_token"
    assert second["data"]["refresh_token"] == "refresh-1"
    assert second["headers"]["Origin"] == REDIRECT_URI
    calls = requests_to(mocked, "GET", _device_url())
    assert calls[1]["headers"]["Authorization"] == f"Bearer {new_jwt}"
    assert first["data"]["grant_type"] == "authorization_code"


async def test_no_refresh_just_before_margin(
    mocked: aioresponses, client: EdfEnrClient, frozen_time: FrozenDateTimeFactory
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    await client.async_get_device(GATEWAY_ID)
    frozen_time.tick(timedelta(seconds=3600 - TOKEN_EXPIRY_MARGIN - 1))
    await client.async_get_device(GATEWAY_ID)

    assert len(requests_to(mocked, "POST", TOKEN_URL)) == 1


async def test_refresh_without_refresh_expiry_info(
    mocked: aioresponses, client: EdfEnrClient, frozen_time: FrozenDateTimeFactory
) -> None:
    mock_login(mocked, token=token_body(refresh_expires_in=None))
    mocked.post(_pattern(TOKEN_URL), payload=token_body())
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    await client.async_login()
    frozen_time.tick(timedelta(days=30))
    await client.async_get_device(GATEWAY_ID)

    assert len(requests_to(mocked, "GET", AUTHORIZE_URL)) == 1
    assert requests_to(mocked, "POST", TOKEN_URL)[1]["data"]["grant_type"] == (
        "refresh_token"
    )


@pytest.mark.parametrize("refresh_expires_in", ["86400", "abc", -5, True, [1]])
async def test_refresh_expiry_formats(
    mocked: aioresponses,
    client: EdfEnrClient,
    frozen_time: FrozenDateTimeFactory,
    refresh_expires_in: Any,
) -> None:
    """Strings of digits are accepted; anything else means "unknown"."""
    mock_login(mocked, token=token_body(refresh_expires_in=refresh_expires_in))
    mocked.post(_pattern(TOKEN_URL), payload=token_body())
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    await client.async_login()
    frozen_time.tick(timedelta(days=30))
    mock_login(mocked)
    await client.async_get_device(GATEWAY_ID)

    grants = [
        call["data"]["grant_type"] for call in requests_to(mocked, "POST", TOKEN_URL)
    ]
    if refresh_expires_in == "86400":
        # Refresh token expired after 30 days: full login.
        assert grants == ["authorization_code", "authorization_code"]
    else:
        # Unknown expiry: a refresh is attempted.
        assert grants == ["authorization_code", "refresh_token"]


@pytest.mark.parametrize(
    ("failure", "exception"),
    [
        ({"status": 400, "body": "{}"}, EdfEnrAuthError),
        ({"status": 200, "body": "garbage"}, EdfEnrApiError),
    ],
)
async def test_refresh_rejected_falls_back_to_login(
    mocked: aioresponses,
    client: EdfEnrClient,
    frozen_time: FrozenDateTimeFactory,
    failure: dict[str, Any],
    exception: type[Exception],
) -> None:
    assert issubclass(exception, Exception)
    mock_login(mocked)
    mocked.post(_pattern(TOKEN_URL), **failure)
    mock_login(mocked, token=token_body(make_jwt(jti="third")))
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    await client.async_login()
    frozen_time.tick(timedelta(seconds=3600))
    await client.async_get_device(GATEWAY_ID)

    grants = [
        call["data"]["grant_type"] for call in requests_to(mocked, "POST", TOKEN_URL)
    ]
    assert grants == ["authorization_code", "refresh_token", "authorization_code"]
    assert len(requests_to(mocked, "GET", AUTHORIZE_URL)) == 2
    (call,) = requests_to(mocked, "GET", _device_url())
    assert call["headers"]["Authorization"] == f"Bearer {make_jwt(jti='third')}"


async def test_refresh_connection_error_is_not_swallowed(
    mocked: aioresponses, client: EdfEnrClient, frozen_time: FrozenDateTimeFactory
) -> None:
    mock_login(mocked)
    mocked.post(_pattern(TOKEN_URL), status=503)

    await client.async_login()
    frozen_time.tick(timedelta(seconds=3600))
    with pytest.raises(EdfEnrConnectionError):
        await client.async_get_device(GATEWAY_ID)


async def test_expired_refresh_token_triggers_login(
    mocked: aioresponses, client: EdfEnrClient, frozen_time: FrozenDateTimeFactory
) -> None:
    mock_login(mocked, token=token_body(refresh_expires_in=7200))
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    await client.async_login()
    frozen_time.tick(timedelta(seconds=7201))
    await client.async_get_device(GATEWAY_ID)

    assert len(requests_to(mocked, "GET", AUTHORIZE_URL)) == 2
    assert len(requests_to(mocked, "POST", TOKEN_URL)) == 2


async def test_no_refresh_token_triggers_login(
    mocked: aioresponses, client: EdfEnrClient, frozen_time: FrozenDateTimeFactory
) -> None:
    mock_login(mocked, token=token_body(refresh_token=None))
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    await client.async_login()
    frozen_time.tick(timedelta(seconds=3600))
    await client.async_get_device(GATEWAY_ID)

    assert len(requests_to(mocked, "GET", AUTHORIZE_URL)) == 2


async def test_concurrent_calls_log_in_once(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    for _ in range(2):
        mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    results = await asyncio.gather(
        client.async_get_device(GATEWAY_ID), client.async_get_device(GATEWAY_ID)
    )

    assert results[0] == results[1]
    assert len(requests_to(mocked, "GET", AUTHORIZE_URL)) == 1
    assert len(requests_to(mocked, "POST", TOKEN_URL)) == 1
    assert len(requests_to(mocked, "GET", _device_url())) == 2


async def test_invalidate_token_ignores_other_tokens(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    # No token yet: nothing to do.
    await client._invalidate_token("whatever")

    mock_login(mocked)
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))
    await client.async_login()
    # A stale token (already replaced by a concurrent call) must not wipe the new one.
    await client._invalidate_token("stale")
    await client.async_get_device(GATEWAY_ID)

    assert len(requests_to(mocked, "POST", TOKEN_URL)) == 1


# --------------------------------------------------------------- API plumbing


async def test_401_then_success_after_refresh(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    new_jwt = make_jwt(jti="renewed")
    mocked.post(_pattern(TOKEN_URL), payload=token_body(new_jwt))
    mocked.get(_pattern(_device_url()), status=401)
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    device = await client.async_get_device(GATEWAY_ID)

    assert device.device_id == GATEWAY_ID
    assert len(requests_to(mocked, "GET", AUTHORIZE_URL)) == 1
    grants = [
        call["data"]["grant_type"] for call in requests_to(mocked, "POST", TOKEN_URL)
    ]
    assert grants == ["authorization_code", "refresh_token"]
    first, second = requests_to(mocked, "GET", _device_url())
    assert first["headers"]["Authorization"] == f"Bearer {make_jwt()}"
    assert second["headers"]["Authorization"] == f"Bearer {new_jwt}"


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_rejected_after_renewal_and_login(
    mocked: aioresponses, client: EdfEnrClient, status: int
) -> None:
    """A resource still refused after a full login is not a credentials error."""
    mock_login(mocked)
    mocked.post(_pattern(TOKEN_URL), payload=token_body(make_jwt(jti="2")))
    mock_login(mocked, token=token_body(make_jwt(jti="3")))
    for _ in range(3):
        mocked.get(_pattern(_device_url()), status=status)

    with pytest.raises(EdfEnrApiError, match=f"HTTP {status}"):
        await client.async_get_device(GATEWAY_ID)

    assert len(requests_to(mocked, "GET", _device_url())) == 3
    grants = [
        call["data"]["grant_type"] for call in requests_to(mocked, "POST", TOKEN_URL)
    ]
    assert grants == ["authorization_code", "refresh_token", "authorization_code"]


async def test_auth_rejected_twice_then_accepted_after_login(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    mocked.post(_pattern(TOKEN_URL), payload=token_body(make_jwt(jti="2")))
    mock_login(mocked, token=token_body(make_jwt(jti="3")))
    for _ in range(2):
        mocked.get(_pattern(_device_url()), status=401)
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    device = await client.async_get_device(GATEWAY_ID)

    assert device.device_id == GATEWAY_ID
    *_, last = requests_to(mocked, "GET", _device_url())
    assert last["headers"]["Authorization"] == f"Bearer {make_jwt(jti='3')}"


async def test_relogin_ignores_replaced_tokens(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    await client.async_login()

    # Another call already logged in again: no second login.
    await client._relogin("stale")

    assert len(requests_to(mocked, "GET", AUTHORIZE_URL)) == 1


async def test_auth_rejected_then_password_rejected(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    """The credentials are reported as rejected when the full login fails."""
    mock_login(mocked)
    mocked.post(_pattern(TOKEN_URL), payload=token_body(make_jwt(jti="2")))
    for _ in range(2):
        mocked.get(_pattern(_device_url()), status=401)
    _page_and_asserted(mocked, body='{"status": "400"}')

    with pytest.raises(EdfEnrAuthError, match="Invalid email or password"):
        await client.async_get_device(GATEWAY_ID)

    assert len(requests_to(mocked, "GET", _device_url())) == 2


async def test_concurrent_rejections_refresh_once(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    """Calls rejected with the same token renew it only once."""
    mock_login(mocked)
    await client.async_login()
    revoked = f"Bearer {make_jwt()}"
    for jti in ("2", "3"):
        mocked.post(_pattern(TOKEN_URL), payload=token_body(make_jwt(jti=jti)))

    async def device(url: Any, **kwargs: Any) -> CallbackResult:
        if kwargs["headers"]["Authorization"] == revoked:
            for _ in range(20):  # both first requests are in flight
                await asyncio.sleep(0)
            return CallbackResult(status=401)
        return CallbackResult(payload=load_json_fixture("device.json"))

    mocked.get(_pattern(_device_url()), callback=device, repeat=True)

    await asyncio.gather(
        client.async_get_device(GATEWAY_ID), client.async_get_device(GATEWAY_ID)
    )

    grants = [
        call["data"]["grant_type"] for call in requests_to(mocked, "POST", TOKEN_URL)
    ]
    assert grants == ["authorization_code", "refresh_token"]


async def test_not_found(mocked: aioresponses, client: EdfEnrClient) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), status=404)

    with pytest.raises(EdfEnrNotFoundError, match="not found"):
        await client.async_get_device(GATEWAY_ID)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "30"}, 30.0),
        ({"Retry-After": "1.5"}, 1.5),
        ({"Retry-After": "-10"}, 0.0),
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
        ({"Retry-After": "soon"}, None),
        ({"Retry-After": "inf"}, None),
        ({"Retry-After": "nan"}, None),
        ({}, None),
    ],
)
async def test_rate_limited(
    mocked: aioresponses,
    client: EdfEnrClient,
    headers: dict[str, str],
    expected: float | None,
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), status=429, headers=headers)

    with pytest.raises(EdfEnrRateLimitError) as exc_info:
        await client.async_get_device(GATEWAY_ID)

    assert exc_info.value.retry_after == expected
    assert isinstance(exc_info.value, EdfEnrConnectionError)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_error(
    mocked: aioresponses, client: EdfEnrClient, status: int
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), status=status)

    with pytest.raises(EdfEnrConnectionError, match=f"HTTP {status}"):
        await client.async_get_device(GATEWAY_ID)


@pytest.mark.parametrize("status", [204, 301, 400, 418])
async def test_unexpected_status(
    mocked: aioresponses, client: EdfEnrClient, status: int
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), status=status)

    with pytest.raises(EdfEnrApiError, match=f"HTTP {status}") as exc_info:
        await client.async_get_device(GATEWAY_ID)
    assert not isinstance(exc_info.value, EdfEnrNotFoundError)


@pytest.mark.parametrize(
    "exception",
    [
        aiohttp.ClientError("boom"),
        aiohttp.ClientConnectionError("down"),
        TimeoutError(),
    ],
)
async def test_transport_errors(
    mocked: aioresponses, client: EdfEnrClient, exception: Exception
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), exception=exception)

    with pytest.raises(EdfEnrConnectionError, match="espaceclient"):
        await client.async_get_device(GATEWAY_ID)


async def test_invalid_json(mocked: aioresponses, client: EdfEnrClient) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), status=200, body="<html>")

    with pytest.raises(EdfEnrApiError, match="invalid JSON"):
        await client.async_get_device(GATEWAY_ID)


async def test_custom_base_urls(
    mocked: aioresponses, session: aiohttp.ClientSession
) -> None:
    client = EdfEnrClient(
        session,
        EMAIL,
        PASSWORD,
        api_base_url="http://localhost:1/api/",
        b2c_base_url="http://localhost:2/b2c/",
        redirect_uri="http://localhost:3",
    )
    m = mocked
    m.get(
        _pattern("http://localhost:2/b2c/oauth2/v2.0/authorize"),
        body=load_fixture("b2c_login_page.html"),
    )
    m.post(_pattern("http://localhost:2/b2c/SelfAsserted"), payload={"status": "200"})
    m.get(
        _pattern("http://localhost:2/b2c/api/CombinedSigninAndSignup/confirmed"),
        status=302,
        headers={"Location": "http://localhost:3/#code=c"},
    )
    m.post(_pattern("http://localhost:2/b2c/oauth2/v2.0/token"), payload=token_body())
    m.get(
        _pattern("http://localhost:1/api/api/device/v2.0/devices/d"),
        payload=load_json_fixture("device.json"),
    )

    await client.async_get_device("d")

    (token,) = requests_to(m, "POST", "http://localhost:2/b2c/oauth2/v2.0/token")
    assert token["headers"]["Origin"] == "http://localhost:3"
    assert token["data"]["redirect_uri"] == "http://localhost:3"


# ------------------------------------------------------------------ resources


def _projects_url() -> str:
    return f"{API_BASE_URL}/crm/api/v2/Contact/{CONTACT_ID}/withopportunities"


async def test_get_projects(mocked: aioresponses, client: EdfEnrClient) -> None:
    mock_login(mocked)
    mocked.get(
        _pattern(_projects_url()),
        payload=load_json_fixture("contact_with_opportunities.json"),
    )

    projects = await client.async_get_projects()

    # The battery upsell sharing the PV number is dropped; the item without
    # pvNumber is skipped.
    assert len(projects) == 1
    assert projects[0].pv_number == PV_NUMBER
    assert projects[0].name == "Jane Doe - PV0000042"
    assert projects[0].installed_power_kwp == 3.96
    assert projects[0].commissioning_date == datetime(2024, 5, 14, 22, 0, tzinfo=UTC)
    assert projects[0].offer_name == "Mon Soleil & Moi toiture V3.1"


async def test_get_projects_prefers_the_installation_opportunity(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    """The opportunity order does not matter: the upsell has no installation data."""
    payload = load_json_fixture("contact_with_opportunities.json")
    payload["opportunities"].reverse()
    mock_login(mocked)
    mocked.get(_pattern(_projects_url()), payload=payload)

    (project,) = await client.async_get_projects()

    assert project.installed_power_kwp == 3.96
    assert project.commissioning_date == datetime(2024, 5, 14, 22, 0, tzinfo=UTC)
    assert project.offer_name == "Mon Soleil & Moi toiture V3.1"


async def test_get_projects_keeps_distinct_pv_numbers(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    mocked.get(
        _pattern(_projects_url()),
        payload={
            "opportunities": [
                {"pvNumber": "PV1"},
                "junk",
                {"pvNumber": "PV2"},
                {"pvNumber": "PV1", "name": "dup"},
            ]
        },
    )

    projects = await client.async_get_projects()

    assert [(p.pv_number, p.name) for p in projects] == [("PV1", None), ("PV2", None)]


@pytest.mark.parametrize(
    "payload", [[], {}, {"opportunities": None}, {"opportunities": {}}, "x"]
)
async def test_get_projects_bad_payload(
    mocked: aioresponses, client: EdfEnrClient, payload: Any
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_projects_url()), payload=payload)

    with pytest.raises(EdfEnrApiError, match="opportunities"):
        await client.async_get_projects()


def _installations_url() -> str:
    return f"{API_BASE_URL}/api/installation/v1.0/installations"


async def test_get_installation(mocked: aioresponses, client: EdfEnrClient) -> None:
    mock_login(mocked)
    mocked.get(
        _pattern(_installations_url()), payload=load_json_fixture("installations.json")
    )

    installation = await client.async_get_installation(PV_NUMBER)

    assert installation == Installation(INSTALLATION_ID, PV_NUMBER)
    (call,) = requests_to(mocked, "GET", _installations_url())
    assert call["params"] == {"NumeroProjet": PV_NUMBER}


async def test_get_installation_skips_unusable_items(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    mocked.get(
        _pattern(_installations_url()),
        payload={
            "items": [
                "junk",
                {"installation": None},
                {"installation": {"idInstallation": 3}},
                {"installation": {"idInstallation": "good"}},
            ]
        },
    )

    assert await client.async_get_installation(PV_NUMBER) == Installation(
        "good", PV_NUMBER
    )


@pytest.mark.parametrize("items", [[], ["junk"], [{"installation": {}}]])
async def test_get_installation_none(
    mocked: aioresponses, client: EdfEnrClient, items: list[Any]
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_installations_url()), payload={"items": items})

    assert await client.async_get_installation(PV_NUMBER) is None


@pytest.mark.parametrize("payload", [[], {}, {"items": None}, {"items": {}}, "x"])
async def test_get_installation_bad_payload(
    mocked: aioresponses, client: EdfEnrClient, payload: Any
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_installations_url()), payload=payload)

    with pytest.raises(EdfEnrApiError, match="items"):
        await client.async_get_installation(PV_NUMBER)


def _equipments_url() -> str:
    return f"{API_BASE_URL}/api/installation/v1.0/installations/{INSTALLATION_ID}/equipements"


async def test_get_equipments(mocked: aioresponses, client: EdfEnrClient) -> None:
    mock_login(mocked)
    mocked.get(
        _pattern(_equipments_url()), payload=load_json_fixture("equipements.json")
    )

    equipments = await client.async_get_equipments(INSTALLATION_ID)

    assert [e.equipment_id for e in equipments] == [
        GATEWAY_ID,
        "ffffffff-0000-1111-2222-333333333333",
    ]
    assert isinstance(equipments[0], Equipment)
    assert equipments[0].last_connection == datetime(
        2026, 10, 24, 18, 50, 30, tzinfo=UTC
    )


async def test_get_equipments_skips_unusable_items(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    mocked.get(
        _pattern(_equipments_url()),
        payload={"equipements": ["junk", {"idEquipement": "e1"}, {}]},
    )

    equipments = await client.async_get_equipments(INSTALLATION_ID)

    assert [e.equipment_id for e in equipments] == ["e1"]


@pytest.mark.parametrize("payload", [[], {}, {"equipements": None}, "x"])
async def test_get_equipments_bad_payload(
    mocked: aioresponses, client: EdfEnrClient, payload: Any
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_equipments_url()), payload=payload)

    with pytest.raises(EdfEnrApiError, match="equipements"):
        await client.async_get_equipments(INSTALLATION_ID)


async def test_get_device(mocked: aioresponses, client: EdfEnrClient) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), payload=load_json_fixture("device.json"))

    device = await client.async_get_device(GATEWAY_ID)

    assert device == Device(
        GATEWAY_ID,
        "YuzeG1",
        "MisEnService",
        datetime.fromtimestamp(1792867830, UTC),
    )


async def test_get_device_bad_payload(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_device_url()), payload=[])

    with pytest.raises(EdfEnrApiError, match="Device without id"):
        await client.async_get_device(GATEWAY_ID)


def _energy_url(granularity: str = "hourly") -> str:
    return f"{API_BASE_URL}/api/installation/telemetries/v4.0/energy/{PV_NUMBER}/{granularity}"


def _bucket(date_start: str, solar: float) -> dict[str, Any]:
    return {
        "dateStart": date_start,
        "energies": {
            "solarProduction": solar,
            "autoConsumption": 0.5,
            "networkConsumption": 0.25,
            "ecsConsumption": 0.0,
        },
    }


async def test_get_energy(mocked: aioresponses, client: EdfEnrClient) -> None:
    mock_login(mocked)
    # Paris wall-clock times (bogus "Z"), unsorted, with the inclusive end bucket
    # (14:00 Paris = 12:00 UTC) and one before the start (11:00 Paris).
    mocked.get(
        _pattern(_energy_url()),
        payload={
            "hourlyElecEnergies": [
                _bucket("2026-07-15T14:00:00Z", 4.0),
                _bucket("2026-07-15T13:00:00Z", 3.0),
                _bucket("2026-07-15T12:00:00Z", 2.0),
                _bucket("2026-07-15T11:00:00Z", 1.0),
            ]
        },
    )

    readings = await client.async_get_energy(
        PV_NUMBER,
        Granularity.HOURLY,
        datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    )

    (call,) = requests_to(mocked, "GET", _energy_url())
    # One hour early (see test_get_energy_keeps_the_spring_phantom_bucket).
    assert call["params"] == {
        "begin-hour": "2026-07-15T11:00:00Z",
        "end-hour": "2026-07-15T14:00:00Z",
    }
    assert [r.start for r in readings] == [
        datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 15, 11, 0, tzinfo=UTC),
    ]
    assert [r.solar_production for r in readings] == [2.0, 3.0]
    assert all(r.granularity is Granularity.HOURLY for r in readings)
    assert readings[0].grid_injection == 1.5
    assert readings[0].home_consumption == 0.75


async def test_get_energy_keeps_the_spring_phantom_bucket(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    """On 2026-03-29 the phantom 02:00 (Paris) bucket belongs to 01:00 UTC.

    A range starting at 01:00 UTC (03:00 Paris) must still include it.
    """
    mock_login(mocked)
    mocked.get(
        _pattern(_energy_url()),
        payload={
            "hourlyElecEnergies": [
                _bucket("2026-03-29T01:00:00Z", 1.0),  # 00:00 UTC
                _bucket("2026-03-29T02:00:00Z", 2.0),  # phantom, 01:00 UTC
                _bucket("2026-03-29T03:00:00Z", 3.0),  # 01:00 UTC
                _bucket("2026-03-29T04:00:00Z", 4.0),  # 02:00 UTC
            ]
        },
    )

    readings = await client.async_get_energy(
        PV_NUMBER,
        Granularity.HOURLY,
        datetime(2026, 3, 29, 1, 0, tzinfo=UTC),
        datetime(2026, 3, 29, 3, 0, tzinfo=UTC),
    )

    (call,) = requests_to(mocked, "GET", _energy_url())
    assert call["params"]["begin-hour"] == "2026-03-29T01:00:00Z"
    assert [(r.start.hour, r.solar_production) for r in readings] == [
        (1, 2.0),
        (1, 3.0),
        (2, 4.0),
    ]


async def test_get_energy_fixture(mocked: aioresponses, client: EdfEnrClient) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_energy_url()), payload=load_json_fixture("hourly.json"))

    readings = await client.async_get_energy(
        PV_NUMBER,
        Granularity.HOURLY,
        datetime(2026, 7, 15, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 16, 0, 0, tzinfo=UTC),
    )

    assert len(readings) == 3
    assert readings[2].solar_production is None


async def test_get_energy_other_granularity(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    mocked.get(
        _pattern(_energy_url("daily")),
        payload={"dailyElecEnergies": [_bucket("2026-07-15T00:00:00Z", 12.0)]},
    )

    readings = await client.async_get_energy(
        PV_NUMBER,
        Granularity.DAILY,
        datetime(2026, 7, 14, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 16, 0, 0, tzinfo=UTC),
    )

    (call,) = requests_to(mocked, "GET", _energy_url("daily"))
    assert set(call["params"]) == {"begin-day", "end-day"}
    assert len(readings) == 1


async def test_get_energy_empty(mocked: aioresponses, client: EdfEnrClient) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_energy_url()), payload={"hourlyElecEnergies": []})

    assert (
        await client.async_get_energy(
            PV_NUMBER, Granularity.HOURLY, NOW, NOW + timedelta(hours=1)
        )
        == []
    )


@pytest.mark.parametrize(
    "payload",
    [[], {}, {"hourlyElecEnergies": None}, {"dailyElecEnergies": []}, "x"],
)
async def test_get_energy_bad_payload(
    mocked: aioresponses, client: EdfEnrClient, payload: Any
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_energy_url()), payload=payload)

    with pytest.raises(EdfEnrApiError, match="hourlyElecEnergies"):
        await client.async_get_energy(
            PV_NUMBER, Granularity.HOURLY, NOW, NOW + timedelta(hours=1)
        )


async def test_get_energy_bad_bucket(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)
    mocked.get(_pattern(_energy_url()), payload={"hourlyElecEnergies": [{"x": 1}]})

    with pytest.raises(EdfEnrApiError, match="dateStart"):
        await client.async_get_energy(
            PV_NUMBER, Granularity.HOURLY, NOW, NOW + timedelta(hours=1)
        )


async def test_get_energy_skips_unreadable_buckets(
    mocked: aioresponses, client: EdfEnrClient, caplog: pytest.LogCaptureFixture
) -> None:
    mock_login(mocked)
    mocked.get(
        _pattern(_energy_url()),
        payload={
            "hourlyElecEnergies": [
                _bucket("2026-07-15T12:00:00Z", 2.0),
                {"dateStart": None},
                _bucket("not-a-date", 1.0),
                _bucket("2026-07-15T13:00:00Z", 3.0),
            ]
        },
    )

    readings = await client.async_get_energy(
        PV_NUMBER,
        Granularity.HOURLY,
        datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    )

    assert [r.solar_production for r in readings] == [2.0, 3.0]
    assert "Skipped 2 unreadable telemetry bucket(s)" in caplog.text


async def test_get_energy_naive_bounds(
    mocked: aioresponses, client: EdfEnrClient
) -> None:
    mock_login(mocked)

    with pytest.raises(ValueError, match="timezone aware"):
        await client.async_get_energy(
            PV_NUMBER,
            Granularity.HOURLY,
            datetime(2026, 7, 15, 10, 0),  # noqa: DTZ001
            datetime(2026, 7, 15, 12, 0),  # noqa: DTZ001
        )
