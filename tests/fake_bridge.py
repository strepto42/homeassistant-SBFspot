"""In-process fake of the ESP32 Bluetooth bridge (protocol ``SMABT 1``).

:class:`FakeBridge` is a real asyncio TCP server.  What happens after the
``CONNECT``/``OK`` handshake is decided by a *session* coroutine:

* :func:`replay_session` replays a script built from the sanitised capture and
  checks that the client's requests are byte-identical to the captured ones,
* :func:`simulate_inverter_session` answers like the captured inverter but
  renumbers every response to whatever the client sent (random session id, clock,
  packet ids), so the default :func:`smabt.async_read_inverter` path can run.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

import smabt.frame as frame_mod
from smabt.const import (
    CMD_HELLO,
    CMD_L2_LAST,
    CMD_SIGNAL_REQUEST,
    CMD_WORD_LOGON,
)

from .capture import RX, TX, Capture, fragment_stream

LOCAL_MAC = "02:00:00:00:00:02"
NO_REPLY = object()
"""Pass as ``connect_reply`` / ``greeting`` to stay silent."""


class Link:
    """Server side of one client connection after the handshake."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        bridge: FakeBridge,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.bridge = bridge

    async def read_frame(self, wait: float = 5.0) -> bytes:
        """Read one complete L1 frame sent by the client."""
        async with asyncio.timeout(wait):
            head = await self.reader.readexactly(4)
            length = frame_mod.frame_length(head)
            return head + await self.reader.readexactly(length - 4)

    async def send(self, data: bytes, *, chunk: int | None = None) -> None:
        """Send *data* to the client, optionally in small separate TCP writes."""
        if chunk is None:
            self.writer.write(data)
            await self.writer.drain()
            return
        for start in range(0, len(data), chunk):
            self.writer.write(data[start : start + chunk])
            await self.writer.drain()
            await asyncio.sleep(0)

    async def hang(self) -> None:
        """Stay silent until the client goes away."""
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            while await self.reader.read(1024):
                pass


Session = Callable[[Link], Awaitable[None]]


@dataclass
class FakeBridge:
    """A bridge listening on 127.0.0.1 on an ephemeral port."""

    session: Session | None = None
    greeting: bytes | object = b"SMABT 1\n"
    connect_reply: bytes | object | None = None  # None -> OK <local_mac>
    local_mac: str = LOCAL_MAC
    port: int = 0
    connect_lines: list[bytes] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    client_closed: asyncio.Event = field(default_factory=asyncio.Event)
    connections: int = 0
    requests: list[bytes] = field(default_factory=list)
    busy_connections: int = 0
    """The first N connections are answered ``ERR BUSY`` instead of the greeting."""
    busy_replies: int = 0
    """The first N ``CONNECT`` requests are answered ``ERR BUSY``."""
    release_delay: float = 0.0
    """After a served session the bridge stays busy this long (SPP link release)."""
    _busy_until: float = 0.0
    _busy_replies_left: int = 0
    _server: asyncio.Server | None = None
    _writers: list[asyncio.StreamWriter] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._busy_replies_left = self.busy_replies

    async def __aenter__(self) -> FakeBridge:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        for writer in self._writers:
            writer.close()  # a client that never hung up must not block shutdown
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(2):
                await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self._writers.append(writer)
        link = Link(reader, writer, self)
        loop = asyncio.get_running_loop()
        served = False
        try:
            if (
                self.connections <= self.busy_connections
                or loop.time() < self._busy_until
            ):
                writer.write(b"ERR BUSY bridge in use\n")
                await writer.drain()
                return
            if self.greeting is not NO_REPLY:
                assert isinstance(self.greeting, bytes)
                writer.write(self.greeting)
                await writer.drain()
            if self.greeting is NO_REPLY:
                await link.hang()
                return
            if not self.greeting.startswith(b"SMABT"):
                await link.hang()
                return
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line:  # the client hung up without sending CONNECT
                return
            self.connect_lines.append(line)
            if self.connect_reply is NO_REPLY:
                await link.hang()
                return
            reply = self.connect_reply
            if self._busy_replies_left > 0:
                self._busy_replies_left -= 1
                reply = b"ERR BUSY bridge in use\n"
            elif reply is None:
                reply = f"OK {self.local_mac}\n".encode()
            assert isinstance(reply, bytes)
            writer.write(reply)
            await writer.drain()
            if not reply.startswith(b"OK"):
                return
            served = True
            if self.session is not None:
                await self.session(link)
            await link.hang()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as err:
            self.problems.append(f"bridge crashed: {err!r}")
        finally:
            if served and self.release_delay:
                self._busy_until = loop.time() + self.release_delay
            self.client_closed.set()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


# ------------------------------ scripts

ScriptItem = tuple[str, bytes | None]
"""``("rx", frame)`` sent to the client, ``("tx", frame)`` expected from it,
``("hang", None)`` silence until the client leaves, ``("close", None)`` hang up."""


def script_from_capture(capture: Capture) -> list[ScriptItem]:
    """The captured session as a script, plus the two frames the log omits."""
    items: list[ScriptItem] = []
    uncaptured = capture.meta["uncaptured"]
    for frame in capture.frames:
        if frame.index in {u["before_frame"] for u in uncaptured}:
            items += [
                ("rx", synthetic_frame(capture, u["cmd"], u["l1_length"]))
                for u in uncaptured
                if u["before_frame"] == frame.index
            ]
        items.append((frame.direction, frame.wire))
    return items


def request_index(
    script: list[ScriptItem], command_word: int, first: int | None = None
) -> int:
    """Index of the client L2 request with *command_word* (and range start *first*)."""
    wanted = struct.pack("<I", command_word)
    wanted_first = None if first is None else struct.pack("<I", first)
    for index, (kind, data) in enumerate(script):
        if kind != TX or data is None:
            continue
        frame = frame_mod.L1Frame.decode(data)
        if frame.cmd == CMD_L2_LAST and frame_mod.is_l2_start(frame.payload):
            payload = frame_mod.L2Packet.decode(frame.payload).payload
            if payload[:4] == wanted and wanted_first in (None, payload[4:8]):
                return index
    raise AssertionError(f"no request {command_word:#010x} in the script")


def response_span(script: list[ScriptItem], request: int) -> slice:
    """Slice of the ``rx`` items that answer the request at index *request*."""
    end = request + 1
    while end < len(script) and script[end][0] == RX:
        end += 1
    return slice(request + 1, end)


def synthetic_frame(capture: Capture, cmd: int, length: int) -> bytes:
    """A made-up L1 frame of *cmd* and total *length* (the log has no payload)."""
    payload = bytes(length - 18)
    return frame_mod.L1Frame(
        capture.inverter_address, capture.local_address, cmd, payload
    ).encode()


def replay_session(script: list[ScriptItem], *, chunk: int | None = None) -> Session:
    """Build a session coroutine replaying *script* strictly."""

    async def run(link: Link) -> None:
        for number, (kind, data) in enumerate(script):
            if kind == RX:
                assert data is not None
                await link.send(data, chunk=chunk)
            elif kind == TX:
                assert data is not None
                actual = await link.read_frame()
                if actual != data:
                    link.bridge.problems.append(
                        f"script item {number}: client sent {actual.hex()}, "
                        f"expected {data.hex()}"
                    )
                    return
            elif kind == "hang":
                await link.hang()
                return
            elif kind == "close":
                link.writer.close()
                return
            else:  # pragma: no cover - test bug
                raise AssertionError(kind)

    return run


# ------------------------------ simulator


def rebuild_response(
    frames: list[bytes],
    *,
    packet_id: int | None = None,
    dst_serial: int | None = None,
    dst_susy_id: int | None = None,
    error: int | None = None,
    payload_patch: Callable[[bytes], bytes] | None = None,
) -> list[bytes]:
    """Re-issue a captured (possibly multi-part) L2 response with changes.

    Reassembles the frames, decodes the packet, applies the changes, re-encodes it
    with a valid FCS and fragments it like the inverter would.
    """
    l1 = [frame_mod.L1Frame.decode(f) for f in frames]
    packet = frame_mod.L2Packet.decode(b"".join(f.payload for f in l1))
    changes: dict[str, int | bytes] = {}
    if packet_id is not None:
        changes["packet_id"] = packet_id
    if dst_serial is not None:
        changes["dst_serial"] = dst_serial
    if dst_susy_id is not None:
        changes["dst_susy_id"] = dst_susy_id
    if error is not None:
        changes["error"] = error
    if payload_patch is not None:
        changes["payload"] = payload_patch(packet.payload)
    packet = replace(packet, **changes)
    stream = packet.encode(escape_fcs=True)
    return fragment_stream(stream, l1[0].src, l1[0].dst)


LOGON_KEY = struct.pack("<I", CMD_WORD_LOGON)


def request_key(packet: frame_mod.L2Packet) -> bytes:
    """What identifies a client request independent of ids and timestamps."""
    if packet.payload[:4] == LOGON_KEY:
        return LOGON_KEY
    return packet.payload[:12]


def _echo_logon_time(request_payload: bytes) -> Callable[[bytes], bytes]:
    """The logon response repeats the request's timestamp (payload bytes 12..16)."""

    def patch(payload: bytes) -> bytes:
        return payload[:12] + request_payload[12:16] + payload[16:]

    return patch


def simulate_inverter_session(
    capture: Capture,
    patches: dict[bytes, Callable[[bytes], bytes]] | None = None,
    ignore: frozenset[bytes] = frozenset(),
) -> Session:
    """Session answering like the captured inverter, whatever ids the client uses.

    *patches* maps a :func:`request_key` to a function rewriting the payload of the
    response(s) to that request; requests whose key is in *ignore* are never answered.
    """
    patches = patches or {}
    responses: dict[bytes, list[list[bytes]]] = {}
    signal_response: list[bytes] = []
    hello_replies: list[bytes] = []
    for request, replies in capture.exchanges():
        first = request.frames[0].l1
        if first.cmd == CMD_HELLO:
            hello_replies = [f.wire for r in replies for f in r.frames]
        elif first.cmd == CMD_SIGNAL_REQUEST:
            signal_response = [f.wire for r in replies for f in r.frames]
        else:
            key = request_key(request.l2)
            responses[key] = [[f.wire for f in r.frames] for r in replies]

    async def run(link: Link) -> None:
        await link.send(capture.announce.frames[0].wire)
        while True:
            try:
                raw = await link.read_frame()
            except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
                return
            frame = frame_mod.L1Frame.decode(raw)
            link.bridge.requests.append(raw)
            if frame.cmd == CMD_HELLO:
                # cmd 10 / cmd 12 (not in the log) then the topology packet.
                for u in capture.meta["uncaptured"]:
                    await link.send(synthetic_frame(capture, u["cmd"], u["l1_length"]))
                for data in hello_replies:
                    await link.send(data)
            elif frame.cmd == CMD_SIGNAL_REQUEST:
                for data in signal_response:
                    await link.send(data)
            elif frame.cmd == CMD_L2_LAST:
                request = frame_mod.L2Packet.decode(frame.payload)
                key = request_key(request)
                if key not in responses:
                    link.bridge.problems.append(f"unexpected request {key.hex()}")
                    return
                for response in [] if key in ignore else responses[key]:
                    patch = patches.get(key)
                    if key == LOGON_KEY:
                        patch = _echo_logon_time(request.payload)
                    for data in rebuild_response(
                        response,
                        packet_id=request.packet_id,
                        dst_serial=request.src_serial,
                        dst_susy_id=request.src_susy_id,
                        payload_patch=patch,
                    ):
                        await link.send(data)
            else:
                link.bridge.problems.append(f"unexpected L1 cmd {frame.cmd}")
                return

    return run
