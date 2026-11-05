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


class SmaLogonRefusedError(SmaError):
    """The inverter refused the logon with a result other than "wrong password".

    The password may well be right.  Real case: an SB 3000TL-20 with ~15 open
    sessions (each logon asks for a 900 s timeout) answers every logon with code 1
    until the old sessions have timed out.  Do not start re-authentication; wait
    for the sessions to expire.  ``code`` is the inverter's result (``packet.error``).
    """

    def __init__(self, code: int, message: str | None = None) -> None:
        super().__init__(message or f"the inverter refused the logon (code {code})")
        self.code = code


class SmaProtocolError(SmaError):
    """Bad bridge greeting, ``BAD_REQUEST``, or malformed/unexpected packets."""
