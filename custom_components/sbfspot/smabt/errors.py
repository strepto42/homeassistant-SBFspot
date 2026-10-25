"""Exception hierarchy of the SMA Bluetooth client."""

from __future__ import annotations


class SmaError(Exception):
    """Base class of every error raised by this package."""


class SmaConnectionError(SmaError):
    """Any failure to reach the inverter (or to keep talking to it)."""


class BridgeUnreachableError(SmaConnectionError):
    """The TCP connection to the ESP32 bridge failed, was refused or timed out."""


class BridgeBusyError(SmaConnectionError):
    """The bridge already serves another client (``ERR BUSY``)."""


class InverterUnreachableError(SmaConnectionError):
    """The bridge could not open the Bluetooth link to the inverter.

    Normal at night: the inverter's Bluetooth radio sleeps without sunlight.
    """


class SmaTimeoutError(SmaConnectionError):
    """No (or only a partial) response from the inverter during a session."""


class SmaAuthError(SmaError):
    """The inverter rejected the logon (wrong password)."""


class SmaProtocolError(SmaError):
    """Bad bridge greeting, ``BAD_REQUEST``, or malformed/unexpected packets."""
