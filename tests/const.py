"""Constants shared by the tests (all values are fake)."""

from datetime import UTC, datetime

from custom_components.edf_enr.const import CONF_PV_NUMBER

# Tests run at this frozen time (Paris summer time: UTC+2).
NOW = datetime(2026, 10, 24, 12, 34, 56, tzinfo=UTC)
COMMISSIONING_DATE = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)

EMAIL = "jane.doe@example.com"
PASSWORD = "correct-horse-battery-staple"
PV_NUMBER = "PV0000042"
OTHER_PV_NUMBER = "PV0000099"
CONTACT_ID = "11111111-2222-3333-4444-555555555555"
INSTALLATION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
GATEWAY_ID = "000042|yuzeg1"
WATER_HEATER_ID = "ffffffff-0000-1111-2222-333333333333"

ENTRY_DATA = {"email": EMAIL, "password": PASSWORD, CONF_PV_NUMBER: PV_NUMBER}
