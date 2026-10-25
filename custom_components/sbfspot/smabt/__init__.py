"""Pure-Python client for SMA Bluetooth inverters.

A port of the Bluetooth protocol code of SBFspot
(https://github.com/SBFspot/SBFspot, CC BY-NC-SA 3.0) to ``asyncio``.  The inverter
is reached through a byte :class:`Transport`; :class:`BridgeTransport` speaks to an
ESP32 that bridges TCP to Bluetooth Classic (RFCOMM channel 1).

This package uses the standard library only and has no Home Assistant imports.

Example::

    transport = BridgeTransport("192.168.1.50", 9900, "00:80:25:AA:BB:CC")
    data = await async_read_inverter(transport, "0000")
    print(data.spot.pac_total)
"""

from __future__ import annotations

from .client import SmaBluetoothClient, async_read_inverter
from .errors import (
    BridgeBusyError,
    BridgeUnreachableError,
    InverterUnreachableError,
    SmaAuthError,
    SmaConnectionError,
    SmaError,
    SmaProtocolError,
    SmaTimeoutError,
)
from .models import DeviceInfo, InverterData, SpotData
from .transport import DEFAULT_PORT, BridgeTransport, Transport

__all__ = [
    "DEFAULT_PORT",
    "BridgeBusyError",
    "BridgeTransport",
    "BridgeUnreachableError",
    "DeviceInfo",
    "InverterData",
    "InverterUnreachableError",
    "SmaAuthError",
    "SmaBluetoothClient",
    "SmaConnectionError",
    "SmaError",
    "SmaProtocolError",
    "SmaTimeoutError",
    "SpotData",
    "Transport",
    "async_read_inverter",
]
