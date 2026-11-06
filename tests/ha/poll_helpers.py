"""Helpers for the poll resilience tests: reads that can be held, and a spinner.

The test clock is frozen (``freezer``) in most of these tests, and a frozen clock
also freezes ``loop.time()``: ``asyncio.wait_for(..., 1)`` would then never expire
and a test that fails would hang instead.  :func:`spin` therefore only yields to
the event loop (``asyncio.sleep(0)``), which needs no clock.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sbfspot.smabt import InverterData

from .conftest import make_inverter_data


async def spin(until: Callable[[], bool], *, steps: int = 500) -> None:
    """Let the event loop run until *until()* is true (never waits on a clock)."""
    for _ in range(steps):
        if until():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition was not reached")


async def settle(steps: int = 50) -> None:
    """Let everything that is ready to run, run."""
    for _ in range(steps):
        await asyncio.sleep(0)


class GatedRead:
    """A stand-in for ``async_read_inverter`` that records and can hold its calls.

    ``bridge.read.side_effect = gate``.  Calls are numbered from 1.  The calls in
    *hold* wait until :meth:`release_all` (or ``gate.release``) is set; the others
    return *result* at once.  The class records the order of starts and ends, how
    many calls ran at the same time, and which calls were cancelled.
    """

    def __init__(
        self, result: InverterData | None = None, *, hold: Iterable[int] = ()
    ) -> None:
        self.result = result if result is not None else make_inverter_data()
        self.hold = set(hold)
        self.release = asyncio.Event()
        self.log: list[tuple[str, int]] = []
        self.args: list[tuple[Any, ...]] = []
        self.cancelled: list[int] = []
        self.calls = 0
        self.active = 0
        self.max_active = 0
        # ``AsyncMock`` awaits a side effect only if it is a coroutine function.
        inspect.markcoroutinefunction(self)

    def started(self, n: int) -> bool:
        return ("start", n) in self.log

    def ended(self, n: int) -> bool:
        return ("end", n) in self.log

    def release_all(self) -> None:
        self.release.set()

    async def __call__(self, *args: Any, **kwargs: Any) -> InverterData:
        self.calls += 1
        n = self.calls
        self.args.append(args)
        self.log.append(("start", n))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if n in self.hold:
                await self.release.wait()
            return self.result
        except asyncio.CancelledError:
            self.cancelled.append(n)
            raise
        finally:
            self.active -= 1
            self.log.append(("end", n))


def start_poll(hass: HomeAssistant, entry: MockConfigEntry) -> asyncio.Task[None]:
    """Start one poll of *entry* in a task (it runs until the first await)."""
    return hass.async_create_task(entry.runtime_data.async_refresh())
