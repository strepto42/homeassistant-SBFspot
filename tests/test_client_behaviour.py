"""Client behaviours that the captured session does not exercise by itself."""

from __future__ import annotations

import asyncio
import struct

import pytest
import smabt
from smabt import frame as frame_mod
from smabt.client import SmaBluetoothClient
from smabt.const import (
    ANY_SERIAL,
    ANY_SUSY_ID,
    APP_SUSY_ID,
    CMD_L2_LAST,
    CMD_WORD_IDENTIFY,
    Q_GRID_RELAY,
)

from .capture import Capture
from .fake_bridge import FakeBridge, Link, simulate_inverter_session
from .test_client_replay import make_transport

SESSION_ID = 974_336_833


@pytest.fixture(scope="module")
def capture() -> Capture:
    return Capture.load()


def l2_requests(bridge: FakeBridge) -> list[frame_mod.L2Packet]:
    packets = []
    for raw in bridge.requests:
        frame = frame_mod.L1Frame.decode(raw)
        if frame.cmd == CMD_L2_LAST:
            packets.append(frame_mod.L2Packet.decode(frame.payload))
    return packets


def identify_packet(packet_id: int) -> frame_mod.L2Packet:
    return frame_mod.L2Packet(
        dst_susy_id=ANY_SUSY_ID,
        dst_serial=ANY_SERIAL,
        src_susy_id=APP_SUSY_ID,
        src_serial=SESSION_ID,
        packet_id=packet_id | 0x8000,
        payload=struct.pack("<III", CMD_WORD_IDENTIFY, 0, 0),
    )


async def test_packet_id_skips_values_with_an_unsafe_fcs(capture: Capture) -> None:
    """``isCrcValid()``: SBFspot bumps the id until the (unescaped) FCS holds no
    0x7D/0x7E; so must we, or the inverter would misread the frame."""
    seed = next(
        s for s in range(1, 5000) if not identify_packet(s + 1).fcs_is_wire_safe()
    )
    async with FakeBridge(simulate_inverter_session(capture)) as bridge:
        client = SmaBluetoothClient(
            make_transport(bridge, capture),
            "0000",
            timeout=2,
            session_id=SESSION_ID,
            packet_id_seed=seed,
        )
        data = await client.async_read()

    assert bridge.problems == []
    assert data.spot.pac_total == 622.0
    packets = l2_requests(bridge)
    assert all(p.fcs_is_wire_safe() for p in packets)
    ids = [p.sequence for p in packets]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert ids[0] > seed + 1  # the unsafe id was skipped
    assert ids[0] != seed + 1


async def test_grid_relay_is_only_queried_for_solar_inverters(capture: Capture) -> None:
    """Inverter.cpp queries the grid relay only for ``DevClass == SolarInverter``."""

    def hybrid(payload: bytes) -> bytes:
        # device class tag 8001 (solar inverter) -> 8009 (hybrid inverter)
        assert payload.count(bytes.fromhex("411f0001")) == 1
        return payload.replace(bytes.fromhex("411f0001"), bytes.fromhex("491f0001"))

    type_label_key = struct.pack("<III", 0x58000200, 0x00821E00, 0x008220FF)
    async with FakeBridge(
        simulate_inverter_session(capture, {type_label_key: hybrid})
    ) as bridge:
        data = await smabt.async_read_inverter(
            make_transport(bridge, capture), timeout=2
        )

    assert bridge.problems == []
    assert "Hybrid" in (data.info.device_class or "")
    assert data.spot.grid_relay is None
    commands = {(p.payload[:4], p.payload[4:8]) for p in l2_requests(bridge)}
    relay = (
        struct.pack("<I", Q_GRID_RELAY.command),
        struct.pack("<I", Q_GRID_RELAY.first),
    )
    assert relay not in commands
    assert data.spot.pac_total == 622.0  # everything else still works


async def test_cancellation_still_closes_the_transport(capture: Capture) -> None:
    async def silent(link: Link) -> None:
        await link.hang()

    async with FakeBridge(silent) as bridge:
        transport = make_transport(bridge, capture)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                smabt.async_read_inverter(transport, timeout=30), 0.3
            )
        await asyncio.wait_for(bridge.client_closed.wait(), 2)

    assert transport.close_calls >= 1


async def test_logged_on_session_is_logged_off_after_a_failure(
    capture: Capture,
) -> None:
    """If a query times out after logon we still send a logoff before closing."""
    ac_voltage = struct.pack("<III", 0x51000200, 0x00464800, 0x004655FF)
    async with FakeBridge(
        simulate_inverter_session(capture, ignore=frozenset({ac_voltage}))
    ) as bridge:
        transport = make_transport(bridge, capture)
        client = SmaBluetoothClient(transport, "0000", timeout=0.3)
        with pytest.raises(smabt.SmaTimeoutError):
            await client.async_read()
        await asyncio.wait_for(bridge.client_closed.wait(), 2)

    last = l2_requests(bridge)[-1]
    assert last.payload[:4] == struct.pack("<I", 0xFFFD010E)  # logoff
    assert transport.close_calls >= 1


async def test_unanswered_logon_is_not_followed_by_a_logoff(capture: Capture) -> None:
    logon = struct.pack("<I", 0xFFFD040C)
    async with FakeBridge(
        simulate_inverter_session(capture, ignore=frozenset({logon}))
    ) as bridge:
        transport = make_transport(bridge, capture)
        client = SmaBluetoothClient(transport, "0000", timeout=0.3)
        with pytest.raises(smabt.SmaTimeoutError):
            await client.async_read()

    payloads = [p.payload[:4] for p in l2_requests(bridge)]
    # identify, init logoff, logon -- and nothing after the failed logon
    assert payloads[-1] == logon
