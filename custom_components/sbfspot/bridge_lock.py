"""One lock per ESP32 bridge: no two inverter reads run at the same time.

The bridge serves one client at a time and answers a second one with ``ERR BUSY``;
the read of the second client then fails.  Everything in this integration that
reads the inverter - the polls of the coordinator and the validation steps of the
config flows - therefore holds the lock of its bridge for the whole read.  The
lock key is the bridge (host and port), so two config entries that share one
bridge are serialised too, and a reloaded entry waits for the poll that its old
coordinator still has in flight.

``asyncio.Lock`` is not re-entrant: take it once per read, never inside a read.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from homeassistant.core import HomeAssistant
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

_BRIDGE_LOCKS: HassKey[dict[str, asyncio.Lock]] = HassKey(f"{DOMAIN}_bridge_locks")
"""``hass.data``: ``"host:port"`` -> the lock of that bridge."""


def bridge_lock(hass: HomeAssistant, host: str, port: int) -> asyncio.Lock:
    """The lock of the bridge at *host*:*port* (created on first use)."""
    key = f"{host.strip().lower()}:{int(port)}"
    return hass.data.setdefault(_BRIDGE_LOCKS, {}).setdefault(key, asyncio.Lock())


@asynccontextmanager
async def hold_bridge(
    hass: HomeAssistant, host: str, port: int, *, max_wait: float | None = None
) -> AsyncIterator[None]:
    """Hold the lock of a bridge for the duration of the ``async with`` block.

    Waits for the lock without a limit, or for at most *max_wait* seconds, then
    raises ``TimeoutError``.  A caller that runs under its own
    ``asyncio.timeout()`` passes no *max_wait*: the wait then counts against that
    timeout.  The lock is released when the block ends, also when it is cancelled.
    """
    lock = bridge_lock(hass, host, port)
    if max_wait is None:
        await lock.acquire()
    else:
        async with asyncio.timeout(max_wait):
            await lock.acquire()
    try:
        yield
    finally:
        lock.release()
