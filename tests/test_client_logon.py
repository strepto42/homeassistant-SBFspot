"""Logon refusal and logoff delivery, through a real ``BridgeTransport`` and a fake
bridge that replays the sanitised capture of a real SBFspot session.

* Logon result ``0x0100`` is the wrong password (``SmaAuthError``).  Any other
  non-zero result is a *refusal* (``SmaLogonRefusedError``): on real hardware the
  inverter answered every logon with code 1 once ~15 sessions were open, although
  the password was right.
* The last message of a session is the logoff.  The ESP32 bridge can only forward it
  over Bluetooth if the TCP connection stays open for a moment after it, so the
  client waits ``logoff_settle`` seconds before it closes the transport - but only
  if a logon happened.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest
import smabt
from smabt import frame as frame_mod
from smabt.client import SmaBluetoothClient
from smabt.const import CMD_L2_LAST, CMD_WORD_LOGOFF, LOGOFF_SETTLE

from .capture import Capture
from .fake_bridge import (
    LOGON_KEY,
    FakeBridge,
    Link,
    Session,
    rebuild_response,
    replay_session,
    request_index,
    response_span,
    script_from_capture,
    simulate_inverter_session,
)
from .test_client_replay import (
    LOGON,
    Q_AC_VOLTAGE,
    RecordingTransport,
    make_client,
    make_transport,
)

TIMEOUT = 0.5
LOGOFF_KEY = struct.pack("<I", CMD_WORD_LOGOFF)


@pytest.fixture(scope="module")
def capture() -> Capture:
    return Capture.load()


# ------------------------------ helpers


def logon_response_with(capture: Capture, code: int) -> list[tuple[str, bytes | None]]:
    """The script up to and including a logon answer that carries error *code*."""
    script = script_from_capture(capture)
    span = response_span(script, request_index(script, LOGON))
    answer = rebuild_response([data for _, data in script[span] if data], error=code)
    return [*script[: span.start], *(("rx", frame) for frame in answer)]


async def expect_error(
    capture: Capture, script: list[tuple[str, bytes | None]], error: type[Exception]
) -> tuple[FakeBridge, RecordingTransport, Exception]:
    async with FakeBridge(replay_session(script)) as bridge:
        transport = make_transport(bridge, capture)
        with pytest.raises(error) as info:
            await make_client(transport, capture).async_read()
        await asyncio.wait_for(bridge.client_closed.wait(), 2)
    return bridge, transport, info.value


@dataclass
class Trace:
    """What the fake bridge saw, with the loop time of each event."""

    frames: list[tuple[float, bytes]] = field(default_factory=list)
    closed_at: float | None = None
    second_logoff: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the second logoff arrives: the one before the logon is the first."""

    def l2(self) -> list[tuple[float, frame_mod.L2Packet]]:
        found = []
        for at, raw in self.frames:
            frame = frame_mod.L1Frame.decode(raw)
            if frame.cmd == CMD_L2_LAST:
                found.append((at, frame_mod.L2Packet.decode(frame.payload)))
        return found

    def last_logoff_at(self) -> float:
        times = [at for at, packet in self.l2() if packet.payload[:4] == LOGOFF_KEY]
        assert times, "no logoff reached the bridge"
        return times[-1]


def traced(inner: Session, trace: Trace) -> Session:
    """Wrap a bridge session: note when each client frame and the TCP close arrive."""

    async def run(link: Link) -> None:
        loop = asyncio.get_running_loop()
        read_frame = link.read_frame

        async def timed_read_frame(wait: float = 5.0) -> bytes:
            try:
                raw = await read_frame(wait)
            except (asyncio.IncompleteReadError, ConnectionError):
                trace.closed_at = loop.time()  # EOF: the client closed the socket
                raise
            trace.frames.append((loop.time(), raw))
            if sum(1 for _, p in trace.l2() if p.payload[:4] == LOGOFF_KEY) >= 2:
                trace.second_logoff.set()
            return raw

        link.read_frame = timed_read_frame  # type: ignore[method-assign]
        await inner(link)

    return run


def silent_logon(capture: Capture) -> Session:
    """Inverter that answers everything except the logon."""
    return simulate_inverter_session(capture, ignore=frozenset({LOGON_KEY}))


# ------------------------------ logon result codes


def test_the_refusal_error_is_part_of_the_api() -> None:
    assert issubclass(smabt.SmaLogonRefusedError, smabt.SmaError)
    assert "SmaLogonRefusedError" in smabt.__all__
    # Not a wrong password (that would start re-authentication) ...
    assert not issubclass(smabt.SmaLogonRefusedError, smabt.SmaAuthError)
    # ... and not "the inverter is asleep" (that would be offline data).
    assert not issubclass(smabt.SmaLogonRefusedError, smabt.SmaConnectionError)
    assert smabt.SmaLogonRefusedError(7).code == 7
    assert "7" in str(smabt.SmaLogonRefusedError(7))


@pytest.mark.parametrize("code", [1, 0x0102, 0xFFFF])
async def test_a_refused_logon_is_not_a_wrong_password(
    capture: Capture, code: int
) -> None:
    """Real inverter, too many open sessions: logon result 1 with the right password."""
    script = logon_response_with(capture, code)

    bridge, transport, error = await expect_error(
        capture, script, smabt.SmaLogonRefusedError
    )

    assert isinstance(error, smabt.SmaLogonRefusedError)
    assert error.code == code
    assert not isinstance(error, smabt.SmaAuthError)
    assert str(code) in str(error)
    assert bridge.problems == []  # the client sent exactly the captured requests
    assert transport.close_calls >= 1


async def test_the_refusal_is_read_from_the_real_logon_response(
    capture: Capture,
) -> None:
    """The bytes after the source serial are ``00 01 | 01 00 | 00 00 | pkt id``
    (ctrl2, error = 0x0001, fragment, packet id): error 1 sits where SBFspot reads
    ``get_short(pcktBuf + 23)``."""
    script = logon_response_with(capture, 1)
    kind, raw = script[-1]
    assert kind == "rx" and raw is not None
    packet = frame_mod.L2Packet.decode(frame_mod.L1Frame.decode(raw).payload)
    # Decoding checked the FCS: the response was re-encoded with a valid one.
    assert packet.error == 1
    assert packet.fragment == 0
    assert packet._body()[20:26] == bytes.fromhex("0001 0100 0000")

    _, _, error = await expect_error(capture, script, smabt.SmaLogonRefusedError)

    assert isinstance(error, smabt.SmaLogonRefusedError)
    assert error.code == 1


async def test_the_wrong_password_is_still_an_auth_error(capture: Capture) -> None:
    script = logon_response_with(capture, 0x0100)

    bridge, transport, error = await expect_error(capture, script, smabt.SmaAuthError)

    assert not isinstance(error, smabt.SmaLogonRefusedError)
    assert bridge.problems == []
    assert transport.close_calls >= 1


# ------------------------------ logoff delivery


def test_the_settle_time_is_a_named_constant() -> None:
    assert 0 < LOGOFF_SETTLE <= 1.0


def test_the_default_settle_time_is_the_constant() -> None:
    client = SmaBluetoothClient(object(), "0000")  # type: ignore[arg-type]
    assert client._logoff_settle == LOGOFF_SETTLE


async def test_the_bridge_gets_the_logoff_before_it_sees_the_tcp_close(
    capture: Capture,
) -> None:
    """The ESP32 forwards a frame over Bluetooth only if it reads it before it sees
    the close: so the close must come ``settle`` seconds after the logoff."""
    settle = 0.4
    trace = Trace()
    async with FakeBridge(traced(simulate_inverter_session(capture), trace)) as bridge:
        transport = make_transport(bridge, capture)
        client = SmaBluetoothClient(
            transport, "0000", timeout=2, logoff_settle=settle, session_id=999_999_999
        )
        data = await client.async_read()
        await asyncio.wait_for(bridge.client_closed.wait(), 2)

    assert bridge.problems == []
    assert data.spot.pac_total == 622.0
    last_packet = trace.l2()[-1][1]
    assert last_packet.payload[:4] == LOGOFF_KEY  # the end
    assert trace.closed_at is not None
    assert trace.closed_at - trace.last_logoff_at() >= settle - 0.05
    assert transport.close_calls >= 1


async def test_the_settle_time_can_be_switched_off(capture: Capture) -> None:
    trace = Trace()
    async with FakeBridge(traced(simulate_inverter_session(capture), trace)) as bridge:
        client = SmaBluetoothClient(
            make_transport(bridge, capture), "0000", timeout=2, logoff_settle=0
        )
        await client.async_read()
        await asyncio.wait_for(bridge.client_closed.wait(), 2)

    assert trace.closed_at is not None
    assert trace.closed_at - trace.last_logoff_at() < 0.2


async def test_the_logoff_after_a_failed_query_is_settled_too(capture: Capture) -> None:
    """The best-effort logoff of a failed session waits like the normal one."""
    settle = 0.4
    ac_voltage = struct.pack("<III", Q_AC_VOLTAGE[0], Q_AC_VOLTAGE[1], 0x004655FF)
    trace = Trace()
    inner = simulate_inverter_session(capture, ignore=frozenset({ac_voltage}))
    async with FakeBridge(traced(inner, trace)) as bridge:
        client = SmaBluetoothClient(
            make_transport(bridge, capture),
            "0000",
            timeout=0.3,
            logoff_settle=settle,
        )
        with pytest.raises(smabt.SmaTimeoutError):
            await client.async_read()
        await asyncio.wait_for(bridge.client_closed.wait(), 2)

    last_packet = trace.l2()[-1][1]
    assert last_packet.payload[:4] == LOGOFF_KEY
    assert trace.closed_at is not None
    assert trace.closed_at - trace.last_logoff_at() >= settle - 0.05


@pytest.mark.parametrize("fails_first", [False, True], ids=["normal", "after-failure"])
async def test_cancellation_during_the_settle_still_closes_the_transport(
    capture: Capture, fails_first: bool
) -> None:
    """Home Assistant can cancel a poll at any time (unload, stop): the pause after
    the logoff must not leave the connection open."""
    trace = Trace()
    ac_voltage = struct.pack("<III", Q_AC_VOLTAGE[0], Q_AC_VOLTAGE[1], 0x004655FF)
    inner = simulate_inverter_session(
        capture, ignore=frozenset({ac_voltage} if fails_first else ())
    )
    async with FakeBridge(traced(inner, trace)) as bridge:
        transport = make_transport(bridge, capture)
        client = SmaBluetoothClient(transport, "0000", timeout=0.3, logoff_settle=30.0)
        task = asyncio.ensure_future(client.async_read())
        await asyncio.wait_for(trace.second_logoff.wait(), 3)  # the final logoff
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(bridge.client_closed.wait(), 2)

    assert transport.close_calls >= 1
    assert trace.closed_at is not None


async def _elapsed(run: Callable[[], Awaitable[None]]) -> float:
    loop = asyncio.get_running_loop()
    start = loop.time()
    await run()
    return loop.time() - start


async def test_no_settle_delay_when_the_session_failed_before_the_logon(
    capture: Capture,
) -> None:
    """Nothing to deliver: a logon that gets no answer ends the session at once."""
    trace = Trace()
    async with FakeBridge(traced(silent_logon(capture), trace)) as bridge:
        client = SmaBluetoothClient(
            make_transport(bridge, capture), "0000", timeout=0.2, logoff_settle=3.0
        )

        async def read() -> None:
            with pytest.raises(smabt.SmaTimeoutError):
                await client.async_read()

        elapsed = await _elapsed(read)
        await asyncio.wait_for(bridge.client_closed.wait(), 2)

    assert elapsed < 1.5  # the deadline (0.2 s) plus connecting - not 3 s more
    # The last thing sent is the logon, there was no logoff after it.
    assert trace.l2()[-1][1].payload[:4] == LOGON_KEY


@pytest.mark.parametrize(
    ("code", "error"),
    [(0x0100, smabt.SmaAuthError), (1, smabt.SmaLogonRefusedError)],
    ids=["wrong-password", "refused"],
)
async def test_no_settle_delay_after_a_rejected_logon(
    capture: Capture, code: int, error: type[Exception]
) -> None:
    """A rejected logon opened no session: no logoff, so nothing to wait for."""
    script = logon_response_with(capture, code)
    async with FakeBridge(replay_session(script)) as bridge:
        client = SmaBluetoothClient(
            make_transport(bridge, capture),
            "0000",
            timeout=0.5,
            session_id=capture.identifiers["session_id"],
            clock=lambda: capture.identifiers["logon_time"],
            logoff_settle=3.0,
        )

        async def read() -> None:
            with pytest.raises(error):
                await client.async_read()

        elapsed = await _elapsed(read)

    assert elapsed < 1.5
    assert bridge.problems == []


async def test_no_settle_delay_when_the_inverter_cannot_be_reached(
    capture: Capture,
) -> None:
    async with FakeBridge(
        connect_reply=b"ERR BT_CONNECT_FAILED page timeout\n"
    ) as bridge:
        client = SmaBluetoothClient(
            make_transport(bridge, capture), "0000", timeout=1, logoff_settle=3.0
        )

        async def read() -> None:
            with pytest.raises(smabt.InverterUnreachableError):
                await client.async_read()

        assert await _elapsed(read) < 1.5
