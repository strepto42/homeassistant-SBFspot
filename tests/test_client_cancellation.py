"""A poll that the coordinator cancels at its time budget leaves nothing behind.

The coordinator runs every poll under ``asyncio.timeout(budget)``.  When the budget
ends, the timeout cancels the task that runs ``async_read_inverter``.  The client
must then still send the final logoff (an open session on the inverter lasts 900 s)
and close the transport.  ``test_client_behaviour`` covers a cancellation with
``wait_for`` before the logon; these tests use ``asyncio.timeout`` like the
coordinator and cancel at three different points.
"""

from __future__ import annotations

import asyncio
import struct

import pytest
import smabt

from .capture import Capture
from .fake_bridge import FakeBridge, Link, simulate_inverter_session
from .test_client_behaviour import l2_requests
from .test_client_replay import make_transport

LOGOFF = struct.pack("<I", 0xFFFD010E)
LOGON = struct.pack("<I", 0xFFFD040C)
AC_VOLTAGE = struct.pack("<III", 0x51000200, 0x00464800, 0x004655FF)


@pytest.fixture(scope="module")
def capture() -> Capture:
    return Capture.load()


async def test_a_budget_that_ends_while_logged_on_still_logs_off_and_closes(
    capture: Capture,
) -> None:
    async with FakeBridge(
        simulate_inverter_session(capture, ignore=frozenset({AC_VOLTAGE}))
    ) as bridge:
        transport = make_transport(bridge, capture)

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(1.0):
                await smabt.async_read_inverter(transport, timeout=30)

        await asyncio.wait_for(bridge.client_closed.wait(), 3)

    payloads = [p.payload[:4] for p in l2_requests(bridge)]
    assert LOGON in payloads
    assert payloads[-1] == LOGOFF  # the logoff reached the bridge before the close
    assert transport.close_calls >= 1


async def test_a_budget_that_ends_before_the_logon_closes_without_a_logoff(
    capture: Capture,
) -> None:
    async with FakeBridge(
        simulate_inverter_session(capture, ignore=frozenset({LOGON}))
    ) as bridge:
        transport = make_transport(bridge, capture)

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.5):
                await smabt.async_read_inverter(transport, timeout=30)

        await asyncio.wait_for(bridge.client_closed.wait(), 3)

    # There is no session to log off from: identify, the reset logoff, the logon.
    assert [p.payload[:4] for p in l2_requests(bridge)][-1] == LOGON
    assert transport.close_calls >= 1


async def test_a_budget_that_ends_during_the_handshake_closes_the_connection(
    capture: Capture,
) -> None:
    async def silent(link: Link) -> None:
        await link.hang()

    async with FakeBridge(silent) as bridge:
        transport = make_transport(bridge, capture)

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3):
                await smabt.async_read_inverter(transport, timeout=30)

        await asyncio.wait_for(bridge.client_closed.wait(), 3)

    assert transport.close_calls >= 1
