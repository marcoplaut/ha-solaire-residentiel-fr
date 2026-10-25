"""Constants for the Solaire Résidentiel FR integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

DOMAIN: Final = "edf_enr"
LOGGER = logging.getLogger(__package__)

INTEGRATION_NAME: Final = "Solaire Résidentiel FR"

CONF_PV_NUMBER: Final = "pv_number"

# The portal publishes 10-minute buckets with a short delay; polling more often
# would only load an unofficial API.
UPDATE_INTERVAL: Final = timedelta(minutes=30)

# Hours of this age or younger are re-imported on every refresh, so late or
# revised measurements end up in the long-term statistics.
STATISTICS_REVISION_WINDOW: Final = timedelta(days=3)
# Used when the portal does not tell when the installation was commissioned.
STATISTICS_DEFAULT_HISTORY: Final = timedelta(days=365)

SERVICE_REIMPORT_STATISTICS: Final = "reimport_statistics"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
