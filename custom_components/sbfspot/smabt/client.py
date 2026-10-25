"""SMA Bluetooth inverter client.

A port of the Bluetooth code path of SBFspot (https://github.com/SBFspot/SBFspot,
CC BY-NC-SA 3.0): ``initialiseSMAConnection()`` (the Windows/Linux "2.0.6" variant
used when ``MIS_Enabled=0``), ``getBT_SignalStrength()``, ``logonSMAInverter()``,
``getInverterData()``, ``logoffSMAInverter()`` and ``getPacket()``.

One poll is the same conversation SBFspot has with the inverter::

    inverter -> hello (cmd 2)              announces itself, net id, protocol version
    client   -> hello reply (cmd 2)
    inverter -> [cmd 10, cmd 12 ...] topology (cmd 5)   carries our own BT address
    client   -> broadcast identify (L2)    inverter answers with SUSyID + serial
    client   -> logoff (L2)                SBFspot resets the session before logging on
    client   -> signal strength request (cmd 3) / inverter -> cmd 4
    client   -> logon (L2) / inverter -> result (0 = OK, 0x0100 = wrong password)
    client   -> 14 queries (L2), each answered by one or more L2 packets
    client   -> logoff (L2)

Everything is ``asyncio``; the transport is injected (see :mod:`.transport`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import struct
import time
from collections.abc import AsyncIterator, Callable

from .const import (
    ADDR_UNKNOWN,
    ADDR_ZERO,
    ANY_SERIAL,
    ANY_SUSY_ID,
    APP_SUSY_ID,
    CMD_HELLO,
    CMD_L2_FRAGMENT,
    CMD_L2_LAST,
    CMD_SIGNAL_REQUEST,
    CMD_SIGNAL_RESPONSE,
    CMD_TOPOLOGY,
    CMD_WORD_IDENTIFY,
    CMD_WORD_LOGOFF,
    CMD_WORD_LOGON,
    CTRL_REQUEST,
    CTRL_REQUEST_SB240,
    DEVICE_CLASS_SOLAR_INVERTER,
    DEVICE_INFO_QUERIES,
    ENCODE_CHAR_INSTALLER,
    ENCODE_CHAR_USER,
    LOGOFF_ARGUMENT,
    LOGON_RESULT_INVALID_PASSWORD,
    LOGON_TIMEOUT,
    MAX_RESYNC_BYTES,
    MIN_PROTOCOL_VERSION,
    PASSWORD_FIELD_LEN,
    Q_GRID_RELAY,
    SPOT_QUERIES,
    SUSY_ID_SB240,
    USER_GROUP_INSTALLER,
    USER_GROUP_USER,
    Query,
)
from .errors import SmaAuthError, SmaProtocolError, SmaTimeoutError
from .frame import (
    L1Frame,
    L2Packet,
    address_to_bytes,
    address_to_str,
    escape,
    frame_length,
    is_l2_start,
)
from .models import DeviceInfo, InverterData
from .parse import DecodedValues, Record, parse_response, signal_percent
from .transport import Transport

_LOGGER = logging.getLogger(__name__)


def encode_password(password: str, *, installer: bool = False) -> bytes:
    """XOR-ish password obfuscation of ``logonSMAInverter()``.

    Each character is shifted by 0x88 (user) or 0xBB (installer); the field is
    padded to 12 bytes with that same value.  Longer passwords are truncated like
    SBFspot does (SMA passwords have at most 12 characters).
    """
    enc = ENCODE_CHAR_INSTALLER if installer else ENCODE_CHAR_USER
    raw = bytearray([enc] * PASSWORD_FIELD_LEN)
    for index, char in enumerate(password.encode("utf-8")[:PASSWORD_FIELD_LEN]):
        raw[index] = (char + enc) & 0xFF
    return bytes(raw)


def _sender_matches(expected: bytes | None, actual: bytes) -> bool:
    """``isValidSender()``: 0xFF bytes in *expected* are wildcards."""
    if expected is None:
        return True
    return all(e in (a, 0xFF) for e, a in zip(expected, actual, strict=True))


class SmaBluetoothClient:
    """One poll of one inverter over an (already configured) transport.

    All inputs that SBFspot takes from the environment can be injected so that
    tests can reproduce a captured session byte for byte:

    * ``session_id`` - the random "serial number" of this application
      (``genSessionID()``: 900000000 + random),
    * ``clock`` - source of the timestamp sent in the logon request,
    * ``packet_id_seed`` - ``pcktID`` before the first request (SBFspot: 1).
    """

    def __init__(
        self,
        transport: Transport,
        password: str = "0000",
        *,
        timeout: float = 10.0,
        session_id: int | None = None,
        app_susy_id: int = APP_SUSY_ID,
        clock: Callable[[], float] = time.time,
        packet_id_seed: int = 1,
        installer: bool = False,
    ) -> None:
        self._transport = transport
        self._timeout = timeout
        self._session_id = (
            900_000_000 + secrets.randbelow(100_000_000)
            if session_id is None
            else session_id
        )
        self._app_susy_id = app_susy_id
        self._clock = clock
        self._packet_id = packet_id_seed
        self._installer = installer
        self._password = encode_password(password, installer=installer)
        self._secret_hex = escape(self._password).hex(" ")

        self._local = ADDR_ZERO  # source address of our frames; known after open()
        self._inverter = ADDR_UNKNOWN
        self._susy_id = ANY_SUSY_ID
        self._serial = ANY_SERIAL
        self._logged_on = False

    # ------------------------------ public

    async def async_read(self) -> InverterData:
        """Run one full poll; always closes the transport."""
        try:
            return await self._session()
        finally:
            await self._shutdown()

    # ------------------------------ session

    async def _session(self) -> InverterData:
        local = await self._transport.open()
        self._local = address_to_bytes(local)
        _LOGGER.debug("transport open, local Bluetooth address %s", local)

        await self._initialise()
        signal = await self._read_signal()
        await self._logon()

        values = DecodedValues()
        for query in (*DEVICE_INFO_QUERIES, *SPOT_QUERIES):
            if (
                query is Q_GRID_RELAY
                and values.device_class_tag != DEVICE_CLASS_SOLAR_INVERTER
            ):
                continue  # Inverter.cpp: only for DevClass == SolarInverter
            response = await self._query(query)
            if response is not None:
                for record in response:
                    values.add(record)

        await self._logoff()

        info = DeviceInfo(
            serial=self._serial,
            susy_id=self._susy_id,
            bt_address=address_to_str(self._inverter),
            name=values.name,
            device_type=values.device_type,
            device_class=values.device_class,
            sw_version=values.sw_version,
        )
        return InverterData(info=info, spot=values.spot_data(signal))

    async def _shutdown(self) -> None:
        """Best-effort logoff if still logged on, then close the transport."""
        if self._logged_on:
            with contextlib.suppress(Exception):
                await self._logoff()
        try:
            await self._transport.close()
        except Exception:
            _LOGGER.debug("error while closing the transport", exc_info=True)

    # ------------------------------ initialise

    async def _initialise(self) -> None:
        """``initialiseSMAConnection(InverterData*)``."""
        async with self._deadline("the inverter's hello"):
            hello = await self._wait_l1(CMD_HELLO, sender=None)
        self._inverter = hello.src
        if len(hello.payload) < 5:
            raise SmaProtocolError("hello packet too short")
        if hello.payload[1] < MIN_PROTOCOL_VERSION:
            raise SmaProtocolError(
                f"inverter firmware too old (protocol version {hello.payload[1]}, "
                f"need {MIN_PROTOCOL_VERSION})"
            )
        net_id = hello.payload[4]
        _LOGGER.debug(
            "inverter %s, net id %d, protocol %d",
            address_to_str(self._inverter),
            net_id,
            hello.payload[1],
        )

        # Echo of the announcement.  SBFspot does not know its own address yet and
        # sends 00:00:00:00:00:00 as source.
        reply = struct.pack("<IBII", 0x00700400, net_id, 0, 1)
        await self._send(L1Frame(ADDR_ZERO, self._inverter, CMD_HELLO, escape(reply)))

        # The inverter now sends cmd 10 and cmd 12 (ignored) and the topology.
        async with self._deadline("the network topology"):
            topology = await self._wait_l1(CMD_TOPOLOGY, sender=self._inverter)
        self._check_local_address(topology.payload)

        # Broadcast identification request -> SUSyID and serial number.
        frame = self._l2_request(
            dst_l1=ADDR_UNKNOWN,
            dst_susy_id=ANY_SUSY_ID,
            dst_serial=ANY_SERIAL,
            ctrl=CTRL_REQUEST,
            ctrl2=0,
            payload=struct.pack("<III", CMD_WORD_IDENTIFY, 0, 0),
        )
        await self._send(frame)
        async with self._deadline("the identification response"):
            packet = await self._wait_l2_response(sender=self._inverter)
        self._susy_id, self._serial = packet.src_susy_id, packet.src_serial
        _LOGGER.debug("inverter SUSyID %d, serial %d", self._susy_id, self._serial)

        await self._logoff()  # reset any session left over from a previous client

    def _check_local_address(self, topology_payload: bytes) -> None:
        """Cross-check the bridge's address against the one in the topology packet."""
        reported = topology_payload[8:14]
        if len(reported) == 6 and reported != self._local:
            _LOGGER.warning(
                "bridge reports local address %s but the inverter sees %s",
                address_to_str(self._local),
                address_to_str(reported),
            )

    async def _read_signal(self) -> float:
        """``getBT_SignalStrength()``; returns percent."""
        await self._send(
            L1Frame(
                self._local, self._inverter, CMD_SIGNAL_REQUEST, escape(b"\x05\x00")
            )
        )
        async with self._deadline("the signal strength"):
            frame = await self._wait_l1(CMD_SIGNAL_RESPONSE, sender=self._inverter)
        signal = signal_percent(frame.payload)
        if signal is None:
            raise SmaProtocolError("signal strength packet too short")
        return signal

    # ------------------------------ logon/logoff

    async def _logon(self) -> None:
        """``logonSMAInverter()`` for the user (or installer) group."""
        now = int(self._clock())
        group = USER_GROUP_INSTALLER if self._installer else USER_GROUP_USER
        payload = (
            struct.pack("<IIIII", CMD_WORD_LOGON, group, LOGON_TIMEOUT, now, 0)
            + self._password
        )
        frame = self._l2_request(
            dst_l1=ADDR_UNKNOWN,
            dst_susy_id=ANY_SUSY_ID,
            dst_serial=ANY_SERIAL,
            ctrl=CTRL_REQUEST,
            ctrl2=0x0100,
            payload=payload,
        )
        await self._send(frame)
        async with self._deadline("the logon response"):
            while True:
                packet = await self._wait_l2_response(sender=self._inverter)
                # The response echoes our timestamp (pcktBuf + 41).
                if packet.payload[12:16] == struct.pack("<I", now & 0xFFFFFFFF):
                    break
                _LOGGER.debug("ignoring logon response with foreign timestamp")
        self._susy_id, self._serial = packet.src_susy_id, packet.src_serial
        if packet.error == LOGON_RESULT_INVALID_PASSWORD:
            raise SmaAuthError("the inverter rejected the password")
        if packet.error != 0:
            raise SmaAuthError(f"logon rejected by the inverter (code {packet.error})")
        self._logged_on = True
        _LOGGER.debug("logged on")

    async def _logoff(self) -> None:
        """``logoffSMAInverter()``: fire and forget."""
        frame = self._l2_request(
            dst_l1=ADDR_UNKNOWN,
            dst_susy_id=ANY_SUSY_ID,
            dst_serial=ANY_SERIAL,
            ctrl=CTRL_REQUEST,
            ctrl2=0x0300,
            payload=struct.pack("<II", CMD_WORD_LOGOFF, LOGOFF_ARGUMENT),
        )
        self._logged_on = False
        await self._send(frame)

    # ------------------------------ queries

    async def _query(self, query: Query) -> list[Record] | None:
        """``getInverterData(command, first, last)``.

        Returns the records, or ``None`` if the inverter answered with a non-zero
        packet status (e.g. 21 = LRI not available).  Timeouts and malformed
        packets raise.
        """
        ctrl = CTRL_REQUEST_SB240 if self._susy_id == SUSY_ID_SB240 else CTRL_REQUEST
        frame = self._l2_request(
            dst_l1=ADDR_UNKNOWN,
            dst_susy_id=self._susy_id,
            dst_serial=self._serial,
            ctrl=ctrl,
            ctrl2=0,
            payload=struct.pack("<III", query.command, query.first, query.last),
        )
        await self._send(frame)

        records: list[Record] = []
        async with self._deadline(f"the response to the {query.name} query"):
            while True:
                packet = await self._wait_l2_response(sender=self._inverter)
                if (packet.src_susy_id, packet.src_serial) != (
                    self._susy_id,
                    self._serial,
                ):
                    _LOGGER.debug("ignoring packet from another device")
                    continue
                if packet.error != 0:
                    _LOGGER.debug(
                        "%s query: packet status %d", query.name, packet.error
                    )
                    return None
                records.extend(parse_response(packet).records)
                if packet.fragment == 0:  # count-down of remaining packets
                    return records

    # ------------------------------ packet I/O

    def _l2_request(
        self,
        *,
        dst_l1: bytes,
        dst_susy_id: int,
        dst_serial: int,
        ctrl: int,
        ctrl2: int,
        payload: bytes,
    ) -> L1Frame:
        """Build an L2 request in an L1 frame (``writePacketHeader`` + ``writePacket``).

        Increments the packet id first and keeps incrementing while the FCS would
        contain 0x7D/0x7E (``isCrcValid()``).
        """
        while True:
            self._packet_id = (self._packet_id + 1) & 0x7FFF
            packet = L2Packet(
                dst_susy_id=dst_susy_id,
                dst_serial=dst_serial,
                src_susy_id=self._app_susy_id,
                src_serial=self._session_id,
                packet_id=self._packet_id | 0x8000,
                payload=payload,
                ctrl=ctrl,
                dst_ctrl2=ctrl2,
                src_ctrl2=ctrl2,
            )
            if packet.fcs_is_wire_safe():
                return L1Frame(self._local, dst_l1, CMD_L2_LAST, packet.encode())

    async def _send(self, frame: L1Frame) -> None:
        data = frame.encode()
        self._log_frame("->", data)
        await self._transport.write(data)

    async def _read_l1(self) -> L1Frame:
        """Read one L1 frame from the byte stream, resynchronising on junk."""
        head = await self._transport.read(4)
        skipped = 0
        while True:
            try:
                length = frame_length(head)
                break
            except SmaProtocolError:
                skipped += 1
                if skipped > MAX_RESYNC_BYTES:
                    raise
                head = head[1:] + await self._transport.read(1)
        if skipped:
            _LOGGER.warning("skipped %d junk byte(s) before a frame", skipped)
        data = head + await self._transport.read(length - 4)
        self._log_frame("<-", data)
        return L1Frame.decode(data)

    async def _wait_l1(self, cmd: int, *, sender: bytes | None) -> L1Frame:
        """``getPacket(sender, cmd)``: skip everything but the wanted L1 command."""
        while True:
            frame = await self._read_l1()
            if not _sender_matches(sender, frame.src):
                _LOGGER.debug("ignoring frame from %s", address_to_str(frame.src))
            elif frame.cmd == cmd:
                return frame
            else:
                _LOGGER.debug("ignoring L1 command %d (waiting for %d)", frame.cmd, cmd)

    async def _wait_l2_response(self, *, sender: bytes | None) -> L2Packet:
        """Next L2 packet answering our latest request.

        Reassembles ``cmd=8`` fragments + final ``cmd=1`` (``getPacket(..., 1)``)
        and silently drops packets whose id is not the id of the last request
        (stale answers to earlier requests, ``pcktID == rcvpcktID & 0x7FFF``).
        """
        while True:
            packet = await self._receive_l2(sender)
            if packet.sequence == self._packet_id:
                return packet
            _LOGGER.debug(
                "ignoring packet id %d (waiting for %d)",
                packet.sequence,
                self._packet_id,
            )

    async def _receive_l2(self, sender: bytes | None) -> L2Packet:
        buffer = bytearray()
        while True:
            frame = await self._read_l1()
            if not _sender_matches(sender, frame.src):
                _LOGGER.debug("ignoring frame from %s", address_to_str(frame.src))
                continue
            if frame.cmd not in (CMD_L2_LAST, CMD_L2_FRAGMENT):
                _LOGGER.debug("ignoring L1 command %d while waiting for L2", frame.cmd)
                continue
            if is_l2_start(frame.payload):
                if buffer:
                    _LOGGER.debug("dropping an incomplete multi-part packet")
                buffer = bytearray(frame.payload)
            elif buffer:
                buffer += frame.payload
            else:
                _LOGGER.debug("ignoring a fragment without a start")
                continue
            if frame.cmd == CMD_L2_LAST:
                return L2Packet.decode(bytes(buffer))

    @contextlib.asynccontextmanager
    async def _deadline(self, what: str) -> AsyncIterator[None]:
        """Bound a wait for *what*; expiry raises :class:`SmaTimeoutError`."""
        try:
            async with asyncio.timeout(self._timeout):
                yield
        except TimeoutError as err:
            raise SmaTimeoutError(
                f"no response from the inverter: waited {self._timeout:g} s for {what}"
            ) from err

    def _log_frame(self, arrow: str, data: bytes) -> None:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            text = data.hex(" ").upper()
            # Never log the (trivially reversible) encoded password.
            text = text.replace(self._secret_hex.upper(), "<password>")
            _LOGGER.debug("%s %3d bytes: %s", arrow, len(data), text)


async def async_read_inverter(
    transport: Transport,
    password: str = "0000",
    *,
    timeout: float = 10.0,  # noqa: ASYNC109 - per-packet wait, not a task deadline
) -> InverterData:
    """Read device info and spot data from an SMA Bluetooth inverter.

    Opens *transport*, performs SBFspot's Bluetooth initialisation, reads the
    signal strength, logs on as *user*, queries device info and spot values,
    logs off and **always closes the transport**.

    Args:
        transport: an unopened :class:`~.transport.Transport`.
        password: the inverter's user password (SMA default ``0000``).
        timeout: seconds to wait for each expected response.

    Raises:
        SmaConnectionError (and subclasses): the inverter could not be reached or
            stopped answering.
        SmaAuthError: the logon was rejected.
        SmaProtocolError: unexpected or malformed data.
    """
    return await SmaBluetoothClient(transport, password, timeout=timeout).async_read()
