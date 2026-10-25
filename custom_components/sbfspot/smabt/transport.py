"""Byte transports for the SMA Bluetooth client.

:class:`BridgeTransport` talks to the ESP32 Bluetooth bridge over TCP.  Its wire
protocol (version 1) is line based until the Bluetooth link is up, then a
transparent byte pipe::

    bridge -> client   SMABT 1\\n                  (or ERR BUSY <text>\\n + close)
    client -> bridge   CONNECT AA:BB:CC:DD:EE:FF\\n (inverter address, within 5 s)
    bridge -> client   OK 11:22:33:44:55:66\\n      (bridge's own BT address)
                       or ERR <CODE> <text>\\n + close
    ...                raw SMA Bluetooth frames in both directions ...

``CODE`` is one of ``BAD_REQUEST``, ``BT_CONNECT_FAILED``, ``BT_TIMEOUT``, ``BUSY``;
the text after it is included in the exception message.

Either side closing the TCP connection ends the session; the bridge then drops the
Bluetooth link.  The bridge needs a moment (tens of milliseconds, at most a few
seconds) to release that link, so a client that connects again right after a session
may be answered ``ERR BUSY``: :meth:`BridgeTransport.open` therefore retries with a
short backoff before it raises :class:`BridgeBusyError`.

The bridge also hangs up *without a message* if no ``CONNECT`` arrives in time or if
a client reads so slowly that its buffer overflows; mid-session that surfaces as a
:class:`SmaConnectionError` from :meth:`BridgeTransport.read` (not a timeout).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .errors import (
    BridgeBusyError,
    BridgeUnreachableError,
    InverterUnreachableError,
    SmaConnectionError,
    SmaProtocolError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 9900
PROTOCOL_GREETING = "SMABT 1"
_MAX_LINE = 512
_CLOSE_TIMEOUT = 2.0
BUSY_RETRY_TIMEOUT = 6.0
"""Total time :meth:`BridgeTransport.open` keeps retrying ``ERR BUSY``."""
BUSY_BACKOFF = 0.25
BUSY_BACKOFF_CAP = 1.0
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


@runtime_checkable
class Transport(Protocol):
    """A bidirectional byte pipe to one inverter."""

    async def open(self) -> str:
        """Connect and return the local Bluetooth address (``AA:BB:CC:DD:EE:FF``)."""
        ...

    async def read(self, n: int) -> bytes:
        """Return exactly *n* bytes, waiting as long as necessary.

        Raises :class:`SmaConnectionError` if the connection ends first.  Callers
        impose their own timeouts.
        """
        ...

    async def write(self, data: bytes) -> None:
        """Send *data*."""
        ...

    async def close(self) -> None:
        """Close the connection; must be idempotent and must not raise."""
        ...


def busy_backoff_schedule(
    initial: float = BUSY_BACKOFF, cap: float = BUSY_BACKOFF_CAP
) -> Iterator[float]:
    """Delays between ``ERR BUSY`` retries: *initial*, doubling, capped at *cap*."""
    delay = initial
    while True:
        yield delay
        delay = min(delay * 2, max(cap, initial))


def normalise_address(address: str) -> str:
    """Return *address* as upper-case ``AA:BB:CC:DD:EE:FF`` or raise ``ValueError``."""
    if not _MAC_RE.match(address.strip()):
        raise ValueError(f"not a Bluetooth address: {address!r}")
    return address.strip().replace("-", ":").upper()


class BridgeTransport:
    """TCP client for the ESP32 SMA Bluetooth bridge (protocol ``SMABT 1``)."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        bt_address: str | None = None,
        *,
        connect_timeout: float = 10.0,
        bt_timeout: float = 30.0,
        busy_timeout: float = BUSY_RETRY_TIMEOUT,
        busy_backoff: float = BUSY_BACKOFF,
    ) -> None:
        """Create the transport.

        *bt_address* (the inverter's Bluetooth MAC) is required; it only has a
        default so that ``port`` can keep its own default before it.

        *connect_timeout* bounds the TCP connect and the bridge's greeting,
        *bt_timeout* the wait for the bridge to open the Bluetooth link.  If the
        bridge answers ``ERR BUSY`` (it is still releasing the previous session),
        :meth:`open` reconnects after *busy_backoff* seconds (doubling, at most
        1 s) for up to *busy_timeout* seconds in total before giving up.
        """
        if bt_address is None:
            raise TypeError("BridgeTransport() missing required argument: 'bt_address'")
        self._host = host
        self._port = port
        self._bt_address = normalise_address(bt_address)
        self._connect_timeout = connect_timeout
        self._bt_timeout = bt_timeout
        self._busy_timeout = busy_timeout
        self._busy_backoff = busy_backoff
        self._local_address = ""
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def bt_address(self) -> str:
        """The inverter address this transport asks the bridge to connect to."""
        return self._bt_address

    async def open(self) -> str:
        """Connect to the bridge and have it open the Bluetooth link.

        Returns the bridge's own Bluetooth address.  ``ERR BUSY`` (greeting or reply
        to ``CONNECT``) is retried with backoff for ``busy_timeout`` seconds.

        Raises:
            BridgeUnreachableError: TCP connect failed/timed out, or no greeting.
            BridgeBusyError: the bridge still serves another client after retrying.
            InverterUnreachableError: the bridge could not reach the inverter.
            SmaProtocolError: unexpected greeting or reply.
        """
        if self._writer is not None:
            raise SmaConnectionError("transport is already open")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._busy_timeout
        delays = busy_backoff_schedule(self._busy_backoff)
        attempt = 1
        while True:
            try:
                return await self._open_once()
            except BridgeBusyError as err:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise
                delay = min(next(delays), remaining)
                _LOGGER.debug(
                    "bridge busy (%s); retrying in %.2f s (attempt %d)",
                    err,
                    delay,
                    attempt,
                )
                attempt += 1
                await asyncio.sleep(delay)

    async def _open_once(self) -> str:
        """One TCP connection attempt including the handshake."""
        try:
            async with asyncio.timeout(self._connect_timeout):
                reader, writer = await asyncio.open_connection(self._host, self._port)
        except (OSError, TimeoutError) as err:
            raise BridgeUnreachableError(
                f"cannot connect to bridge {self._host}:{self._port}: "
                f"{err or type(err).__name__}"
            ) from err
        self._reader, self._writer = reader, writer
        try:
            await self._handshake()
        except BaseException:
            await self.close()
            raise
        return self._local_address

    async def _handshake(self) -> None:
        greeting = await self._read_line(
            self._connect_timeout,
            on_timeout=lambda: BridgeUnreachableError(
                f"bridge {self._host}:{self._port} did not send its greeting"
            ),
        )
        _LOGGER.debug("bridge greeting: %s", greeting)
        if greeting.startswith("ERR "):
            raise self._error_for(greeting)
        if greeting != PROTOCOL_GREETING:
            raise SmaProtocolError(f"unexpected bridge greeting: {greeting[:80]!r}")

        await self.write(f"CONNECT {self._bt_address}\n".encode("ascii"))
        reply = await self._read_line(
            self._bt_timeout,
            on_timeout=lambda: InverterUnreachableError(
                f"bridge did not report the Bluetooth connection to "
                f"{self._bt_address} within {self._bt_timeout:g} s"
            ),
        )
        _LOGGER.debug("bridge reply: %s", reply)
        if reply.startswith("ERR "):
            raise self._error_for(reply)
        match = re.fullmatch(r"OK ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", reply)
        if not match:
            raise SmaProtocolError(f"unexpected bridge reply: {reply[:80]!r}")
        self._local_address = match.group(1).upper()

    async def _read_line(
        self, wait: float, *, on_timeout: Callable[[], Exception]
    ) -> str:
        assert self._reader is not None
        try:
            async with asyncio.timeout(wait):
                raw = await self._reader.readline()
        except TimeoutError:
            raise on_timeout() from None
        except (ValueError, asyncio.LimitOverrunError) as err:
            raise SmaProtocolError("bridge sent an over-long line") from err
        except OSError as err:
            raise BridgeUnreachableError(f"bridge connection failed: {err}") from err
        if not raw.endswith(b"\n"):
            raise BridgeUnreachableError(
                "bridge closed the connection during handshake"
            )
        if len(raw) > _MAX_LINE:
            raise SmaProtocolError("bridge sent an over-long line")
        return raw.decode("ascii", errors="replace").rstrip("\r\n")

    @staticmethod
    def _error_for(line: str) -> Exception:
        """Map a bridge ``ERR <CODE> <text>`` line to an exception."""
        _, _, rest = line.partition(" ")
        code, _, text = rest.partition(" ")
        message = f"bridge error {code}: {text}".strip()
        if code == "BUSY":
            return BridgeBusyError(message)
        if code in ("BT_CONNECT_FAILED", "BT_TIMEOUT"):
            return InverterUnreachableError(message)
        return SmaProtocolError(message)

    async def read(self, n: int) -> bytes:
        """Read exactly *n* bytes from the Bluetooth link."""
        if self._reader is None:
            raise SmaConnectionError("transport is not open")
        try:
            return await self._reader.readexactly(n)
        except asyncio.IncompleteReadError as err:
            raise SmaConnectionError(
                f"bridge closed the connection ({len(err.partial)} of {n} bytes read)"
            ) from err
        except OSError as err:
            raise SmaConnectionError(f"bridge connection failed: {err}") from err

    async def write(self, data: bytes) -> None:
        """Write *data* to the Bluetooth link."""
        if self._writer is None:
            raise SmaConnectionError("transport is not open")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except OSError as err:
            raise SmaConnectionError(f"bridge connection failed: {err}") from err

    async def close(self) -> None:
        """Close the TCP connection (idempotent, never raises)."""
        writer, self._reader, self._writer = self._writer, None, None
        if writer is None:
            return
        writer.close()
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT):
                await writer.wait_closed()
        except TimeoutError:
            writer.transport.abort()
        except Exception as err:
            _LOGGER.debug("ignoring error while closing the bridge connection: %s", err)
