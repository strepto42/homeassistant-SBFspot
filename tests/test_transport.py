"""``BridgeTransport`` against an in-process fake of the ESP32 bridge."""

from __future__ import annotations

import asyncio
import socket
import time
from itertools import islice

import pytest
import smabt
from smabt.transport import DEFAULT_PORT, busy_backoff_schedule, normalise_address

from .fake_bridge import NO_REPLY, FakeBridge, Link

INVERTER = "00:80:25:00:00:01"


def transport_for(
    bridge: FakeBridge,
    bt_address: str = INVERTER,
    *,
    connect_timeout: float = 1.0,
    bt_timeout: float = 1.0,
    busy_timeout: float = 0.0,
    busy_backoff: float = 0.01,
) -> smabt.BridgeTransport:
    """Transport for *bridge*; ``ERR BUSY`` is not retried unless asked for."""
    return smabt.BridgeTransport(
        "127.0.0.1",
        bridge.port,
        bt_address,
        connect_timeout=connect_timeout,
        bt_timeout=bt_timeout,
        busy_timeout=busy_timeout,
        busy_backoff=busy_backoff,
    )


# ------------------------------ happy path


async def test_open_returns_the_bridges_bluetooth_address() -> None:
    async with FakeBridge(local_mac="11:22:33:44:55:66") as bridge:
        transport = transport_for(bridge)
        try:
            assert await transport.open() == "11:22:33:44:55:66"
        finally:
            await transport.close()

    assert bridge.connect_lines == [b"CONNECT 00:80:25:00:00:01\n"]


async def test_connect_line_is_normalised_to_upper_case_colons() -> None:
    async with FakeBridge() as bridge:
        transport = transport_for(bridge, "aa-bb-cc-dd-ee-0f")
        try:
            await transport.open()
        finally:
            await transport.close()

    assert bridge.connect_lines == [b"CONNECT AA:BB:CC:DD:EE:0F\n"]


async def test_after_the_handshake_the_socket_is_a_transparent_pipe() -> None:
    async def echo(link: Link) -> None:
        # Data glued to the OK line must not be lost, and writes pass unchanged.
        await link.send(b"\x7e\x01\x02")
        data = await link.reader.readexactly(5)
        await link.send(data[::-1])

    async with FakeBridge(echo) as bridge:
        transport = transport_for(bridge)
        try:
            await transport.open()
            assert await transport.read(3) == b"\x7e\x01\x02"
            await transport.write(b"\x00\x7d\x7e\x11\xff")
            assert await transport.read(5) == b"\xff\x11\x7e\x7d\x00"
        finally:
            await transport.close()


async def test_read_returns_exactly_n_bytes_across_tcp_segments() -> None:
    async def trickle(link: Link) -> None:
        await link.send(bytes(range(20)), chunk=3)

    async with FakeBridge(trickle) as bridge:
        transport = transport_for(bridge)
        try:
            await transport.open()
            assert await transport.read(7) == bytes(range(7))
            assert await transport.read(13) == bytes(range(7, 20))
        finally:
            await transport.close()


async def test_close_is_idempotent_and_ends_the_session() -> None:
    async with FakeBridge() as bridge:
        transport = transport_for(bridge)
        await transport.open()
        await transport.close()
        await transport.close()
        await asyncio.wait_for(bridge.client_closed.wait(), 2)

    with pytest.raises(smabt.SmaConnectionError):
        await transport.read(1)
    with pytest.raises(smabt.SmaConnectionError):
        await transport.write(b"x")


async def test_open_twice_is_rejected() -> None:
    async with FakeBridge() as bridge:
        transport = transport_for(bridge)
        try:
            await transport.open()
            with pytest.raises(smabt.SmaConnectionError, match="already open"):
                await transport.open()
        finally:
            await transport.close()


async def test_read_after_the_bridge_hangs_up() -> None:
    async def hang_up(link: Link) -> None:
        await link.send(b"ab")
        link.writer.close()

    async with FakeBridge(hang_up) as bridge:
        transport = transport_for(bridge)
        try:
            await transport.open()
            with pytest.raises(smabt.SmaConnectionError, match="closed"):
                await transport.read(10)
        finally:
            await transport.close()


# ------------------------------ failures


async def test_busy_bridge_refuses_with_an_error_greeting() -> None:
    async with FakeBridge(
        greeting=b"ERR BUSY client 10.0.0.9 is connected\n"
    ) as bridge:
        transport = transport_for(bridge)
        with pytest.raises(smabt.BridgeBusyError, match=r"10\.0\.0\.9"):
            await transport.open()

    assert bridge.connect_lines == []


async def test_busy_after_connect_request() -> None:
    async with FakeBridge(connect_reply=b"ERR BUSY try later\n") as bridge:
        with pytest.raises(smabt.BridgeBusyError):
            await transport_for(bridge).open()


@pytest.mark.parametrize("code", ["BT_CONNECT_FAILED", "BT_TIMEOUT"])
async def test_inverter_unreachable(code: str) -> None:
    async with FakeBridge(connect_reply=f"ERR {code} sleeping\n".encode()) as bridge:
        transport = transport_for(bridge)
        with pytest.raises(smabt.InverterUnreachableError, match=code):
            await transport.open()

    assert bridge.connect_lines == [b"CONNECT 00:80:25:00:00:01\n"]


@pytest.mark.parametrize("reply", [b"ERR BAD_REQUEST nope\n", b"ERR WHATEVER x\n"])
async def test_bad_request_and_unknown_errors_are_protocol_errors(reply: bytes) -> None:
    async with FakeBridge(connect_reply=reply) as bridge:
        with pytest.raises(smabt.SmaProtocolError):
            await transport_for(bridge).open()


@pytest.mark.parametrize("greeting", [b"HELLO\n", b"SMABT 2\n", b"\n", b"smabt 1\n"])
async def test_wrong_greeting_is_a_protocol_error(greeting: bytes) -> None:
    async with FakeBridge(greeting=greeting) as bridge:
        with pytest.raises(smabt.SmaProtocolError, match="greeting"):
            await transport_for(bridge).open()

    assert bridge.connect_lines == []


@pytest.mark.parametrize(
    "reply", [b"OK\n", b"OK 11:22:33\n", b"YES 11:22:33:44:55:66\n"]
)
async def test_malformed_ok_line_is_a_protocol_error(reply: bytes) -> None:
    async with FakeBridge(connect_reply=reply) as bridge:
        with pytest.raises(smabt.SmaProtocolError):
            await transport_for(bridge).open()


async def test_closed_port_is_bridge_unreachable() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    transport = smabt.BridgeTransport("127.0.0.1", port, INVERTER, connect_timeout=1)
    with pytest.raises(smabt.BridgeUnreachableError):
        await transport.open()


async def test_unresolvable_host_is_bridge_unreachable() -> None:
    transport = smabt.BridgeTransport(
        "no-such-host.invalid", 9900, INVERTER, connect_timeout=2
    )
    with pytest.raises(smabt.BridgeUnreachableError):
        await transport.open()


async def test_no_greeting_within_the_timeout() -> None:
    async with FakeBridge(greeting=NO_REPLY) as bridge:
        transport = transport_for(bridge, connect_timeout=0.2)
        with pytest.raises(smabt.BridgeUnreachableError, match="greeting"):
            await transport.open()

    await asyncio.wait_for(bridge.client_closed.wait(), 2)  # we hung up


async def test_bridge_that_never_answers_connect() -> None:
    async with FakeBridge(connect_reply=NO_REPLY) as bridge:
        transport = transport_for(bridge, bt_timeout=0.2)
        with pytest.raises(smabt.InverterUnreachableError, match="within"):
            await transport.open()

    await asyncio.wait_for(bridge.client_closed.wait(), 2)


async def test_bridge_closing_during_the_handshake() -> None:
    async with FakeBridge(connect_reply=b"") as bridge:
        # An empty reply followed by a close, not a line.
        with pytest.raises(smabt.BridgeUnreachableError):
            await transport_for(bridge).open()


async def test_overlong_line_is_a_protocol_error() -> None:
    async with FakeBridge(greeting=b"SMABT 1 " + b"x" * 5000 + b"\n") as bridge:
        with pytest.raises(smabt.SmaProtocolError):
            await transport_for(bridge).open()


async def test_failed_open_leaves_the_transport_closed() -> None:
    async with FakeBridge(connect_reply=b"ERR BT_TIMEOUT x\n") as bridge:
        transport = transport_for(bridge)
        with pytest.raises(smabt.InverterUnreachableError):
            await transport.open()
        with pytest.raises(smabt.SmaConnectionError):
            await transport.read(1)
        await asyncio.wait_for(bridge.client_closed.wait(), 2)


# ------------------------------ ERR BUSY while the bridge releases the link


async def test_busy_greeting_is_retried_until_the_bridge_is_free() -> None:
    """After a session the bridge needs a moment to release the SPP link."""
    async with FakeBridge(busy_connections=2) as bridge:
        transport = transport_for(bridge, busy_timeout=2.0)
        try:
            assert await transport.open() == "02:00:00:00:00:02"
        finally:
            await transport.close()

    assert bridge.connections == 3
    assert bridge.connect_lines == [b"CONNECT 00:80:25:00:00:01\n"]


async def test_busy_reply_to_connect_is_retried() -> None:
    async with FakeBridge(busy_replies=2) as bridge:
        transport = transport_for(bridge, busy_timeout=2.0)
        try:
            await transport.open()
        finally:
            await transport.close()

    assert bridge.connections == 3
    assert len(bridge.connect_lines) == 3  # every attempt sent its CONNECT


async def test_a_bridge_that_stays_busy_gives_up_after_the_budget() -> None:
    async with FakeBridge(busy_connections=10_000) as bridge:
        transport = transport_for(bridge, busy_timeout=0.3, busy_backoff=0.05)
        started = time.monotonic()
        with pytest.raises(smabt.BridgeBusyError, match="in use"):
            await transport.open()
        elapsed = time.monotonic() - started

    assert 0.25 <= elapsed < 2.0
    assert bridge.connections >= 3  # it really kept trying


async def test_zero_budget_means_no_retry() -> None:
    async with FakeBridge(busy_connections=10_000) as bridge:
        with pytest.raises(smabt.BridgeBusyError):
            await transport_for(bridge, busy_timeout=0).open()

    assert bridge.connections == 1


async def test_other_errors_are_not_retried() -> None:
    async with FakeBridge(
        connect_reply=b"ERR BT_CONNECT_FAILED page timeout\n"
    ) as bridge:
        with pytest.raises(smabt.InverterUnreachableError):
            await transport_for(bridge, busy_timeout=5.0).open()

    assert bridge.connections == 1


def test_busy_backoff_schedule_doubles_up_to_the_cap() -> None:
    assert list(islice(busy_backoff_schedule(), 6)) == [0.25, 0.5, 1.0, 1.0, 1.0, 1.0]
    assert list(islice(busy_backoff_schedule(0.01), 3)) == [0.01, 0.02, 0.04]
    assert list(islice(busy_backoff_schedule(2.0), 2)) == [
        2.0,
        2.0,
    ]  # never below start


def test_default_busy_budget_is_about_six_seconds() -> None:
    transport = smabt.BridgeTransport("h", 9900, INVERTER)
    assert transport._busy_timeout == 6.0
    assert transport._busy_backoff == 0.25


# ------------------------------ error details


@pytest.mark.parametrize(
    ("reply", "error", "detail"),
    [
        (
            b"ERR BT_CONNECT_FAILED page timeout\n",
            smabt.InverterUnreachableError,
            "page timeout",
        ),
        (
            b"ERR BT_TIMEOUT no SDP answer\n",
            smabt.InverterUnreachableError,
            "no SDP answer",
        ),
        (b"ERR BAD_REQUEST bad address\n", smabt.SmaProtocolError, "bad address"),
        (b"ERR BUSY bridge in use\n", smabt.BridgeBusyError, "bridge in use"),
    ],
)
async def test_bridge_error_detail_is_in_the_exception_message(
    reply: bytes, error: type[Exception], detail: str
) -> None:
    async with FakeBridge(connect_reply=reply) as bridge:
        with pytest.raises(error) as info:
            await transport_for(bridge).open()

    assert detail in str(info.value)


# ------------------------------ construction


def test_constructor_accepts_the_documented_call_styles() -> None:
    positional = smabt.BridgeTransport("h", 1234, "aa:bb:cc:dd:ee:ff")
    keyword = smabt.BridgeTransport(host="h", bt_address="AA:BB:CC:DD:EE:FF")
    assert positional.bt_address == keyword.bt_address == "AA:BB:CC:DD:EE:FF"
    assert DEFAULT_PORT == 9900


def test_constructor_requires_the_inverter_address() -> None:
    with pytest.raises(TypeError, match="bt_address"):
        smabt.BridgeTransport("h", 9900)


@pytest.mark.parametrize(
    "bad", ["", "00:80:25:00:00", "00:80:25:00:00:0g", "0080250000001"]
)
def test_constructor_rejects_malformed_addresses(bad: str) -> None:
    with pytest.raises(ValueError, match="Bluetooth address"):
        smabt.BridgeTransport("h", 9900, bad)


def test_normalise_address() -> None:
    assert normalise_address(" 00-80-25-aa-bb-cc ") == "00:80:25:AA:BB:CC"


def test_transport_satisfies_the_protocol() -> None:
    assert isinstance(smabt.BridgeTransport("h", 9900, INVERTER), smabt.Transport)
