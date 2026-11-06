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

LOGON_REFUSED_BACKOFF: Final = timedelta(seconds=900)
"""How long to stay away after the inverter refused a logon.

The logon request asks for a 900 s session timeout (``LOGON_TIMEOUT`` in ``smabt``).
A refusal means the inverter still holds open sessions; they are gone 900 s after
their last use, and every new logon attempt is wasted until then.
"""

MAX_MISSED_POLLS: Final = 3
"""How many daytime polls in a row may fail before the data is no longer held.

A failed poll (a connection error or a protocol error) is a *missed* poll.  For the
first ``MAX_MISSED_POLLS`` misses in a row the coordinator keeps showing the last
online data, as long as that success is recent: at most ``MAX_MISSED_POLLS + 1``
scan intervals old, and never from before a night.  From the next miss on the
instantaneous values are unknown (connection errors) or the entities are unavailable
(protocol errors).  One success resets the count.
"""

POLL_BUDGET_MARGIN: Final = 2.0
MIN_POLL_BUDGET: Final = 20.0
"""A whole poll (wait for the bridge, attempts, retry pause) must end within
``max(MIN_POLL_BUDGET, scan interval - POLL_BUDGET_MARGIN)`` seconds."""

RETRY_DELAY: Final = 2.0
RETRY_MIN_BUDGET_LEFT: Final = 15.0
"""After a connection error the poll waits ``RETRY_DELAY`` seconds and tries once
more, but only if at least ``RETRY_MIN_BUDGET_LEFT`` seconds of the poll budget are
left.  A slow failure (the bridge gives up on the Bluetooth connect after ~15 s)
therefore gets no retry that the budget would cut; a fast one (a ~5 s page timeout)
does."""

EXHAUSTION_NEAR_SUN: Final = timedelta(minutes=60)
"""Within this time of sunrise or sunset the inverter wakes up or goes to sleep, so
a grace that is used up is logged at INFO instead of WARNING."""

CONFIG_FLOW_LOCK_TIMEOUT: Final = 60.0
"""How long a config flow waits for the bridge while a poll uses it, in seconds."""

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
