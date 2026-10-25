"""Constants of the solar provider's customer portal (reverse engineered)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

PORTAL_URL = "https://espaceclient.edf-solutions-solaires.com"
API_BASE_URL = f"{PORTAL_URL}/api"

B2C_TENANT = "enrb2ccustomerwebsite"
B2C_POLICY = "b2c_1a_signin_b2c"
B2C_BASE_URL = (
    f"https://{B2C_TENANT}.b2clogin.com/{B2C_TENANT}.onmicrosoft.com/{B2C_POLICY}"
)
CLIENT_ID = "f10d3a50-a5d3-41f6-8e08-9233a3c08240"
REDIRECT_URI = PORTAL_URL
SCOPE = (
    "openid "
    "https://ENRB2CCustomerWebsite.onmicrosoft.com/"
    "8ded11b4-8afd-4cec-b133-d7fb404e1b9c/Data.Read "
    "profile offline_access"
)

# The telemetry API reads and returns naive Europe/Paris wall-clock times, even
# though the timestamps carry a "Z" suffix.
API_TIMEZONE = ZoneInfo("Europe/Paris")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30
# Refresh the access token this many seconds before it expires.
TOKEN_EXPIRY_MARGIN = 120
