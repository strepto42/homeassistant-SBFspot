"""End-to-end tests: the client over a real TCP ``BridgeTransport`` against a fake
bridge that replays the sanitised capture of a real SBFspot session."""

from __future__ import annotations

import asyncio
import logging

import pytest
import smabt
from smabt import frame as frame_mod
from smabt.client import SmaBluetoothClient
from smabt.const import CMD_L2_LAST, MAX_RESYNC_BYTES

from .capture import Capture
from .fake_bridge import (
    FakeBridge,
    ScriptItem,
    rebuild_response,
    replay_session,
    request_index,
    response_span,
    script_from_capture,
    simulate_inverter_session,
)

TIMEOUT = 0.5
"""Per-response timeout used in the replay tests (the replay itself is instant)."""

# Command words / range starts of the requests the tests tamper with.
LOGON = 0xFFFD040C
Q_SW_VERSION = (0x58000200, 0x00823400)
Q_ENERGY = (0x54000200, 0x00260100)
Q_DC_POWER = (0x53800200, 0x00251E00)
Q_DC_VOLTAGE = (0x53800200, 0x00451F00)
Q_AC_POWER = (0x51000200, 0x00464000)
Q_AC_VOLTAGE = (0x51000200, 0x00464800)

EXPECTED_INFO = {
    "serial": 2100000042,
    "susy_id": 113,
    "bt_address": "00:80:25:00:00:01",
    "name": "SN: 2100000042",
    "device_type": "SB 3000TL-20",
    "device_class": "Solar Inverters",
    "sw_version": "03.20.01.R",
}

EXPECTED_SPOT = {
    "pac_total": 622.0,
    "pac1": 622.0,
    "pac2": None,  # SMA NaN: single-phase inverter
    "pac3": None,
    "uac1": 228.94,
    "uac2": None,
    "uac3": None,
    "iac1": 2.72,
    "iac2": None,
    "iac3": None,
    "pdc1": 643.0,
    "pdc2": None,  # second MPPT not reported at all
    "udc1": 336.83,
    "udc2": None,
    "idc1": 1.906,
    "idc2": None,
    "grid_frequency": 49.97,
    "e_today": 4.883,
    "e_total": 60854.831,
    "temperature": 56.96,
    "status": "Ok",
    "grid_relay": "Closed",
}


@pytest.fixture(scope="module")
def capture() -> Capture:
    return Capture.load()


class RecordingTransport(smabt.BridgeTransport):
    """BridgeTransport that counts ``close()`` calls and remembers if it opened."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.close_calls = 0
        self.opened = False

    async def open(self) -> str:
        address = await super().open()
        self.opened = True
        return address

    async def close(self) -> None:
        self.close_calls += 1
        await super().close()


def make_transport(
    bridge: FakeBridge, capture: Capture, **kwargs: float
) -> RecordingTransport:
    return RecordingTransport(
        "127.0.0.1",
        bridge.port,
        capture.identifiers["inverter_bt_address"],
        connect_timeout=2,
        bt_timeout=2,
        **kwargs,
    )


def make_client(transport: smabt.Transport, capture: Capture) -> SmaBluetoothClient:
    """A client fed with the captured session's session id and logon time."""
    ids = capture.identifiers
    return SmaBluetoothClient(
        transport,
        ids["password"],
        timeout=TIMEOUT,
        session_id=ids["session_id"],
        clock=lambda: ids["logon_time"],
    )


async def run_replay(
    capture: Capture, script: list[ScriptItem], *, chunk: int | None = None
) -> tuple[smabt.InverterData, FakeBridge, RecordingTransport]:
    async with FakeBridge(replay_session(script, chunk=chunk)) as bridge:
        transport = make_transport(bridge, capture)
        data = await make_client(transport, capture).async_read()
    return data, bridge, transport


async def run_replay_expecting(
    capture: Capture, script: list[ScriptItem], error: type[Exception]
) -> tuple[FakeBridge, RecordingTransport, Exception]:
    async with FakeBridge(replay_session(script)) as bridge:
        transport = make_transport(bridge, capture)
        with pytest.raises(error) as info:
            await make_client(transport, capture).async_read()
    return bridge, transport, info.value


def assert_inverter_data(data: smabt.InverterData) -> None:
    for name, expected in EXPECTED_INFO.items():
        assert getattr(data.info, name) == expected, name
    for name, expected in EXPECTED_SPOT.items():
        value = getattr(data.spot, name)
        if isinstance(expected, float):
            assert value == pytest.approx(expected, abs=1e-9), name
        else:
            assert value == expected, name
    assert data.spot.operating_time == pytest.approx(58826.405, abs=1e-3)
    assert data.spot.feed_in_time == pytest.approx(57238.146, abs=1e-3)
    assert data.spot.bt_signal == pytest.approx(72.5, abs=0.1)


# ------------------------------ success


@pytest.mark.parametrize("chunk", [None, 7, 1], ids=["whole", "7-byte", "1-byte"])
async def test_replay_requests_are_byte_exact(
    capture: Capture, chunk: int | None
) -> None:
    """With the captured session id, clock and packet-id seed the client sends
    exactly SBFspot's bytes; the replayed answers (also trickled in tiny TCP writes)
    decode into the complete result."""
    script = script_from_capture(capture)
    data, bridge, transport = await run_replay(capture, script, chunk=chunk)

    assert bridge.problems == []  # every request matched the capture byte for byte
    assert bridge.connect_lines == [b"CONNECT 00:80:25:00:00:01\n"]
    assert_inverter_data(data)
    assert transport.close_calls >= 1
    await asyncio.wait_for(bridge.client_closed.wait(), 2)


async def test_public_entry_point_with_random_session(capture: Capture) -> None:
    """``async_read_inverter`` (random session id, real clock) against an inverter
    that renumbers its captured answers to whatever ids the client uses."""
    async with FakeBridge(simulate_inverter_session(capture)) as bridge:
        transport = make_transport(bridge, capture)
        data = await smabt.async_read_inverter(transport, "0000", timeout=2)

    assert bridge.problems == []
    assert_inverter_data(data)
    assert transport.close_calls >= 1
    await asyncio.wait_for(bridge.client_closed.wait(), 2)

    # Same L1 commands and L2 queries, in the same order, as the SBFspot session.
    def signature(raw: bytes) -> tuple[int, bytes]:
        frame = frame_mod.L1Frame.decode(raw)
        if frame.cmd == CMD_L2_LAST:
            return frame.cmd, frame_mod.L2Packet.decode(frame.payload).payload[:12]
        return frame.cmd, frame.payload

    captured = [signature(request.frames[0].wire) for request, _ in capture.exchanges()]
    assert [signature(raw) for raw in bridge.requests] == captured


async def test_client_works_for_other_session_ids(capture: Capture) -> None:
    """Different session id / clock / packet ids still give the same readings."""
    async with FakeBridge(simulate_inverter_session(capture)) as bridge:
        client = SmaBluetoothClient(
            make_transport(bridge, capture),
            "0000",
            timeout=2,
            session_id=999_999_999,
            clock=lambda: 1_700_000_000,
            packet_id_seed=0x1234,
        )
        data = await client.async_read()

    assert bridge.problems == []
    assert_inverter_data(data)


# ------------------------------ errors


@pytest.mark.parametrize("code", [0x0100, 0x0102], ids=["wrong-password", "other-code"])
async def test_logon_rejected(capture: Capture, code: int) -> None:
    script = script_from_capture(capture)
    logon = request_index(script, LOGON)
    span = response_span(script, logon)
    rejected = rebuild_response([data for _, data in script[span] if data], error=code)
    script[span] = [("rx", frame) for frame in rejected]

    bridge, transport, _ = await run_replay_expecting(
        capture, script[: span.start + len(rejected)], smabt.SmaAuthError
    )

    assert bridge.problems == []
    assert transport.close_calls >= 1
    await asyncio.wait_for(bridge.client_closed.wait(), 2)


async def test_mid_session_hang_is_a_timeout(capture: Capture) -> None:
    script = script_from_capture(capture)
    request = request_index(script, *Q_AC_VOLTAGE)
    script = [*script[: request + 1], ("hang", None)]

    bridge, transport, _ = await run_replay_expecting(
        capture, script, smabt.SmaTimeoutError
    )

    assert bridge.problems == []
    assert transport.close_calls >= 1
    await asyncio.wait_for(bridge.client_closed.wait(), 2)


async def test_partial_response_is_a_timeout(capture: Capture) -> None:
    script = script_from_capture(capture)
    request = request_index(script, *Q_ENERGY)
    _, first_response = script[request + 1]
    assert first_response is not None
    script = [
        *script[: request + 1],
        ("rx", first_response[: len(first_response) // 2]),
        ("hang", None),
    ]

    _, transport, _ = await run_replay_expecting(capture, script, smabt.SmaTimeoutError)

    assert transport.close_calls >= 1


async def test_inverter_that_never_speaks_is_a_timeout(capture: Capture) -> None:
    _, transport, _ = await run_replay_expecting(
        capture, [("hang", None)], smabt.SmaTimeoutError
    )
    assert transport.close_calls >= 1


async def test_bridge_hanging_up_is_a_connection_error(capture: Capture) -> None:
    script = script_from_capture(capture)
    request = request_index(script, *Q_DC_POWER)
    script = [*script[: request + 1], ("close", None)]

    _, transport, error = await run_replay_expecting(
        capture, script, smabt.SmaConnectionError
    )

    assert not isinstance(error, smabt.SmaTimeoutError)
    assert transport.close_calls >= 1


async def test_unreachable_inverter_closes_transport(capture: Capture) -> None:
    async with FakeBridge(
        connect_reply=b"ERR BT_CONNECT_FAILED page timeout\n"
    ) as bridge:
        transport = make_transport(bridge, capture)
        with pytest.raises(smabt.InverterUnreachableError):
            await smabt.async_read_inverter(transport, timeout=1)

    assert transport.close_calls >= 1
    assert not transport.opened


async def test_unreachable_bridge_closes_transport(capture: Capture) -> None:
    transport = RecordingTransport(
        "127.0.0.1", 1, capture.identifiers["inverter_bt_address"], connect_timeout=1
    )
    with pytest.raises(smabt.BridgeUnreachableError):
        await smabt.async_read_inverter(transport, timeout=1)

    assert transport.close_calls >= 1


async def test_old_firmware_is_a_protocol_error(capture: Capture) -> None:
    script = script_from_capture(capture)
    _, announce = script[0]
    assert announce is not None
    original = frame_mod.L1Frame.decode(announce)
    payload = bytearray(original.payload)
    payload[1] = 3  # protocol version < 4 means firmware older than 1.71
    older = frame_mod.L1Frame(original.src, original.dst, original.cmd, bytes(payload))
    script[0] = ("rx", older.encode())

    _, transport, error = await run_replay_expecting(
        capture, script, smabt.SmaProtocolError
    )

    assert "firmware" in str(error)
    assert transport.close_calls >= 1


async def test_corrupted_response_is_a_protocol_error(capture: Capture) -> None:
    script = script_from_capture(capture)
    request = request_index(script, *Q_SW_VERSION)
    index = request + 1
    kind, raw = script[index]
    assert kind == "rx" and raw is not None
    corrupted = bytearray(raw)
    corrupted[60] ^= 0x01  # inside the record data
    script[index] = ("rx", bytes(corrupted))

    _, transport, error = await run_replay_expecting(
        capture, script, smabt.SmaProtocolError
    )

    assert "FCS" in str(error)
    assert transport.close_calls >= 1


async def test_endless_junk_is_a_protocol_error(capture: Capture) -> None:
    script = script_from_capture(capture)
    script.insert(1, ("rx", b"\x01" * (MAX_RESYNC_BYTES + 50)))

    _, transport, _ = await run_replay_expecting(
        capture, script, smabt.SmaProtocolError
    )

    assert transport.close_calls >= 1


# ------------------------------ tolerance


async def test_failed_query_only_blanks_its_own_values(capture: Capture) -> None:
    """A non-zero packet status must not abort the session."""
    script = script_from_capture(capture)
    request = request_index(script, *Q_DC_VOLTAGE)
    span = response_span(script, request)
    failed = rebuild_response([d for _, d in script[span] if d], error=21)
    script[span] = [("rx", frame) for frame in failed]

    data, bridge, _ = await run_replay(capture, script)

    assert bridge.problems == []
    assert data.spot.udc1 is None
    assert data.spot.idc1 is None
    assert data.spot.pdc1 == 643.0  # the query before
    assert data.spot.pac_total == 622.0  # and after it
    assert data.spot.grid_frequency == pytest.approx(49.97)


async def test_real_sunny_boy_status_21_query_is_survived(capture: Capture) -> None:
    """The captured session itself contains a status-21 answer (grid metering)."""
    messages = [m for m in capture.messages if m.direction == "rx" and m.is_l2]
    assert 21 in {m.l2.error for m in messages}
    data, _, _ = await run_replay(capture, script_from_capture(capture))
    assert data.spot.pac_total == 622.0


async def test_stale_packet_ids_are_ignored(capture: Capture) -> None:
    """A late answer to an earlier request must not be taken for the current one.

    If it were, every following query would consume its predecessor's answer and the
    last one would never be read - so the complete result is the proof.
    """
    script = script_from_capture(capture)
    request = request_index(script, *Q_AC_POWER)
    span = response_span(script, request)
    real = [d for _, d in script[span] if d]
    stale = rebuild_response(real, packet_id=0x8000 | 0x0042)
    script[span.start : span.start] = [("rx", frame) for frame in stale]

    data, bridge, _ = await run_replay(capture, script)

    assert bridge.problems == []
    assert_inverter_data(data)


async def test_junk_before_a_frame_is_skipped(
    capture: Capture, caplog: pytest.LogCaptureFixture
) -> None:
    script = script_from_capture(capture)
    script.insert(1, ("rx", b"\x00\x01\x02garbage"))

    with caplog.at_level(logging.WARNING, logger="smabt"):
        data, bridge, _ = await run_replay(capture, script)

    assert bridge.problems == []
    assert data.spot.pac_total == 622.0
    assert any("junk" in r.getMessage() for r in caplog.records)


async def test_local_address_mismatch_only_warns(
    capture: Capture, caplog: pytest.LogCaptureFixture
) -> None:
    """The bridge's address is used; if the inverter sees another one we log it."""
    async with FakeBridge(
        simulate_inverter_session(capture), local_mac="02:00:00:00:00:99"
    ) as bridge:
        with caplog.at_level(logging.WARNING, logger="smabt"):
            data = await smabt.async_read_inverter(
                make_transport(bridge, capture), timeout=2
            )

    assert data.spot.pac_total == 622.0
    assert any("local address" in r.getMessage() for r in caplog.records)
    # ... and our L1 frames carry the bridge's address as source.
    sources = {frame_mod.L1Frame.decode(raw).src for raw in bridge.requests[1:]}
    assert sources == {frame_mod.address_to_bytes("02:00:00:00:00:99")}


async def test_debug_log_dumps_frames_but_hides_the_password(
    capture: Capture, caplog: pytest.LogCaptureFixture
) -> None:
    script = script_from_capture(capture)
    with caplog.at_level(logging.DEBUG, logger="smabt"):
        await run_replay(capture, script)

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "<password>" in text
    assert "B8 B8 B8 B8 88 88 88 88 88 88 88 88" not in text  # the encoded "0000"
    assert any(r.getMessage().startswith("->") for r in caplog.records)
    assert any(r.getMessage().startswith("<-") for r in caplog.records)


async def test_back_to_back_sessions_wait_for_the_bridge_to_release_the_link(
    capture: Capture,
) -> None:
    """HA's config-flow check is followed right away by the first refresh; the
    bridge answers ``ERR BUSY`` until it has released the Bluetooth link."""
    async with FakeBridge(
        simulate_inverter_session(capture), release_delay=0.3
    ) as bridge:
        first = await smabt.async_read_inverter(
            make_transport(bridge, capture), timeout=2
        )
        second_transport = make_transport(bridge, capture, busy_backoff=0.05)
        second = await smabt.async_read_inverter(second_transport, timeout=2)

    assert bridge.problems == []
    assert first == second
    assert bridge.connections >= 3  # the second read had to retry at least once
    assert_inverter_data(second)
