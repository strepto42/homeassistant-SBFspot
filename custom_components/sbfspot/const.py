"""Constants for the SBFspot (SMA Bluetooth) integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from .smabt import DEFAULT_PORT

DOMAIN: Final = "sbfspot"

# Config entry data keys
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_BT_ADDRESS: Final = "bt_address"
CONF_PASSWORD: Final = "password"

# Option keys
CONF_SCAN_INTERVAL: Final = "scan_interval"

DEFAULT_BRIDGE_PORT: Final = DEFAULT_PORT
DEFAULT_PASSWORD: Final = "0000"
"""The SMA factory default for the "user" group."""

DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 3600
"""Polling interval bounds in seconds."""

SUN_MARGIN: Final = timedelta(minutes=15)
"""Poll from sunrise - 15 min to sunset + 15 min (SBFspot ``SunRSOffset=900``).

The inverter's Bluetooth radio sleeps without sunlight; outside this window the
coordinator reports the inverter as offline without opening a connection.
"""

MANUFACTURER: Final = "SMA"

# Spot values that are cumulative counters (SensorStateClass.TOTAL_INCREASING).
# They never report 0 and never go down: the last valid value is kept (and
# persisted) while the inverter is offline or returns None / 0 / a lower value.
# ``e_today`` is handled separately: it resets to 0 at local midnight.
CUMULATIVE_FIELDS: Final = ("e_total", "operating_time", "feed_in_time")

# Instantaneous power / current values report 0 while the inverter is offline.
ZERO_WHEN_OFFLINE_FIELDS: Final = (
    "pac_total",
    "pac1",
    "pac2",
    "pac3",
    "pdc1",
    "pdc2",
    "iac1",
    "iac2",
    "iac3",
    "idc1",
    "idc2",
)

STORAGE_VERSION: Final = 1
