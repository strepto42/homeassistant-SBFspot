"""No two inverter reads run at the same time on one bridge.

The ESP32 bridge serves one client and answers a second with ``ERR BUSY``, so a read
that starts while another runs fails.  One lock per bridge (host + port) is held by
every read: the polls of the coordinators and the validation of the config flows
(user, reconfigure, reauth).

``GatedRead`` stands in for ``async_read_inverter``: it holds chosen calls until the
test releases them and records the order of starts and ends, so the tests check
*when* a read begins, not only that it happens.  The clock is frozen: a frozen clock
freezes ``loop.time()``, so ``freezer.tick`` also moves the timeouts.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sbfspot.const import (
    CONF_BT_ADDRESS,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.sbfspot.smabt import SmaAuthError

from .conftest import (
    BRIDGE_HOST,
    BT_ADDRESS,
    Bridge,
    entity_id,
    number,
    setup_entry,
    state_of,
)
from .poll_helpers import GatedRead, settle, spin, start_poll
from .test_poll_resilience import make_entry

# A long interval: the poll budget (298 s) is not an issue in these tests.
SLOW = 300
ORDERED = [("start", 1), ("end", 1), ("start", 2), ("end", 2)]


def second_entry(host: str = BRIDGE_HOST, port: int = 9900) -> MockConfigEntry:
    """Another inverter, reached through the bridge at *host*:*port*."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="SB 3000TL-20 2100000043",
        unique_id="2100000043",
        data={
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_BT_ADDRESS: "00:80:25:00:00:02",
            CONF_PASSWORD: "0000",
        },
        options={CONF_SCAN_INTERVAL: SLOW},
    )


# -------------------------------------------------------------- the coordinator


async def test_a_refresh_request_during_a_poll_starts_no_second_read(
    hass: HomeAssistant, bridge: Bridge
) -> None:
    entry = make_entry(SLOW)
    await setup_entry(hass, entry)
    assert await async_setup_component(hass, "homeassistant", {})
    gate = GatedRead(hold={1})
    bridge.read.side_effect = gate
    poll = start_poll(hass, entry)
    await spin(lambda: gate.started(1))

    await entry.runtime_data.async_request_refresh()
    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": entity_id(hass, "sensor", "power")},
        blocking=True,
    )
    await settle(100)

    assert gate.calls == 1
    assert gate.max_active == 1
    gate.release_all()
    await spin(poll.done)
    poll.result()
    assert gate.max_active == 1


async def test_a_second_update_of_the_same_coordinator_reads_nothing(
    hass: HomeAssistant, bridge: Bridge
) -> None:
    """Home Assistant's own refresh lock keeps ``async_refresh`` calls apart; this
    pins the coordinator's contract if ``_async_update_data`` runs twice at once:
    the second call returns the current data, reads nothing, and is no miss."""
    entry = make_entry(SLOW)
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    current = coordinator.data
    gate = GatedRead(hold={1})
    bridge.read.side_effect = gate
    first = hass.async_create_task(coordinator._async_update_data())
    await spin(lambda: gate.started(1))

    second = hass.async_create_task(coordinator._async_update_data())
    try:
        await spin(second.done)  # it must not wait for the first one
    finally:
        gate.release_all()
        await spin(first.done)
        second.cancel()

    assert second.result() is current
    assert gate.calls == 1
    assert coordinator.missed_polls == 0
    assert coordinator.statistics.as_dict()["skipped_reentrant_refreshes"] == 1
    assert first.result().online
    assert gate.max_active == 1


async def test_two_entries_on_one_bridge_never_read_at_the_same_time(
    hass: HomeAssistant, bridge: Bridge
) -> None:
    first, second = make_entry(SLOW), second_entry()
    await setup_entry(hass, first)
    await setup_entry(hass, second)
    gate = GatedRead(hold={1})
    bridge.read.side_effect = gate

    poll_first = start_poll(hass, first)
    await spin(lambda: gate.started(1))
    poll_second = start_poll(hass, second)
    await settle(100)
    assert gate.calls == 1  # the second poll waits for the bridge

    gate.release_all()
    await spin(lambda: poll_first.done() and poll_second.done())
    assert gate.log == ORDERED
    assert gate.max_active == 1
    assert first.runtime_data.missed_polls == second.runtime_data.missed_polls == 0


async def test_entries_on_different_bridges_do_not_wait_for_each_other(
    hass: HomeAssistant, bridge: Bridge
) -> None:
    first, other_bridge = make_entry(SLOW), second_entry(host="192.168.1.51")
    await setup_entry(hass, first)
    await setup_entry(hass, other_bridge)
    gate = GatedRead(hold={1})
    bridge.read.side_effect = gate

    poll_first = start_poll(hass, first)
    await spin(lambda: gate.started(1))
    poll_other = start_poll(hass, other_bridge)
    await spin(poll_other.done)  # not held up by the first poll

    assert gate.calls == 2
    assert not poll_first.done()
    gate.release_all()
    await spin(poll_first.done)


async def test_a_poll_that_waits_for_the_bridge_reads_when_it_is_free(
    hass: HomeAssistant, bridge: Bridge
) -> None:
    first, second = make_entry(SLOW), second_entry()
    await setup_entry(hass, first)
    await setup_entry(hass, second)
    gate = GatedRead(hold={1})
    bridge.read.side_effect = gate
    poll_first = start_poll(hass, first)
    await spin(lambda: gate.started(1))
    poll_second = start_poll(hass, second)
    await settle(100)

    gate.release_all()
    await spin(lambda: poll_first.done() and poll_second.done())

    # The poll that waited is a normal success, not a miss.
    assert second.runtime_data.data.online
    assert second.runtime_data.statistics.as_dict()["successes"] == 2


# ------------------------------------------------------------------ config flows


@pytest.fixture
async def polling(
    hass: HomeAssistant, bridge: Bridge
) -> AsyncGenerator[tuple[MockConfigEntry, GatedRead]]:
    """A set up entry whose next read (a poll) is held in flight."""
    entry = make_entry(SLOW)
    await setup_entry(hass, entry)
    gate = GatedRead(hold={1})
    bridge.read.side_effect = gate
    start_poll(hass, entry)
    await spin(lambda: gate.started(1))
    yield entry, gate
    gate.release_all()  # never leave a poll behind
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_reauth_validation_waits_for_the_poll_in_flight(
    hass: HomeAssistant, polling: tuple[MockConfigEntry, GatedRead]
) -> None:
    entry, gate = polling
    result = await entry.start_reauth_flow(hass)

    flow = hass.async_create_task(
        hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
    )
    await settle(100)
    assert gate.calls == 1  # the validation has not started its read
    assert not flow.done()

    gate.release_all()
    await spin(flow.done)

    assert flow.result()["type"] is FlowResultType.ABORT
    assert flow.result()["reason"] == "reauth_successful"
    assert gate.log[:4] == ORDERED  # the poll ended before the validation began
    assert gate.args[1][1] == "new-password"  # call 2 is the validation
    assert gate.max_active == 1
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_reconfigure_validation_waits_for_the_poll_in_flight(
    hass: HomeAssistant, polling: tuple[MockConfigEntry, GatedRead]
) -> None:
    entry, gate = polling
    result = await entry.start_reconfigure_flow(hass)

    flow = hass.async_create_task(
        hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: BRIDGE_HOST, CONF_PORT: 9900, CONF_BT_ADDRESS: BT_ADDRESS},
        )
    )
    await settle(100)
    assert gate.calls == 1
    assert not flow.done()

    gate.release_all()
    await spin(flow.done)

    assert flow.result()["reason"] == "reconfigure_successful"
    assert gate.log[:4] == ORDERED
    assert gate.max_active == 1


async def test_a_user_flow_validation_waits_for_the_poll_in_flight(
    hass: HomeAssistant, polling: tuple[MockConfigEntry, GatedRead]
) -> None:
    _, gate = polling
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    flow = hass.async_create_task(
        hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: BRIDGE_HOST,
                CONF_PORT: 9900,
                CONF_BT_ADDRESS: BT_ADDRESS,
                CONF_PASSWORD: "0000",
            },
        )
    )
    await settle(100)
    assert gate.calls == 1
    assert not flow.done()

    gate.release_all()
    await spin(flow.done)

    assert gate.log[:4] == ORDERED
    assert gate.max_active == 1
    # The same inverter is already configured.
    assert flow.result()["type"] is FlowResultType.ABORT
    assert flow.result()["reason"] == "already_configured"


async def test_a_validation_on_another_bridge_does_not_wait(
    hass: HomeAssistant, polling: tuple[MockConfigEntry, GatedRead]
) -> None:
    entry, gate = polling
    result = await entry.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "192.168.1.99", CONF_PORT: 9900, CONF_BT_ADDRESS: BT_ADDRESS},
    )

    assert result["reason"] == "reconfigure_successful"
    assert gate.max_active == 2  # a different bridge: both reads ran at once


async def test_a_validation_that_cannot_get_the_bridge_shows_bridge_busy(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    polling: tuple[MockConfigEntry, GatedRead],
) -> None:
    entry, gate = polling
    result = await entry.start_reauth_flow(hass)
    flow = hass.async_create_task(
        hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
    )
    await settle(100)

    freezer.tick(timedelta(seconds=59))
    async_fire_time_changed(hass)
    await settle()
    assert not flow.done()  # it waits for 60 s ...

    freezer.tick(timedelta(seconds=1))
    async_fire_time_changed(hass)
    await spin(flow.done)

    result = flow.result()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "bridge_busy"}
    assert gate.calls == 1  # ... and then gives up without a read
    assert entry.data[CONF_PASSWORD] == "0000"


async def test_a_failed_validation_releases_the_bridge(
    hass: HomeAssistant, bridge: Bridge
) -> None:
    entry = make_entry(SLOW)
    await setup_entry(hass, entry)
    gate = GatedRead()
    bridge.read.side_effect = SmaAuthError("rejected")
    result = await entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    bridge.read.side_effect = gate
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "right"}
    )  # does not wait for a lock that was never released
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done(wait_background_tasks=True)


# ----------------------------------------------------------------------- reload


@pytest.mark.parametrize("how", ["task", "scheduled"])
async def test_a_reload_waits_for_the_poll_in_flight(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    how: str,
) -> None:
    """The new coordinator must not start a read while the old one is in flight.

    ``task``: the old poll is a plain task (it survives the unload).  ``scheduled``:
    it is the timer's background task, which the unload cancels: the new read must
    still wait until the cancelled one has ended.
    """
    entry = make_entry(SLOW)
    await setup_entry(hass, entry)
    old = entry.runtime_data
    gate = GatedRead(hold={1})
    bridge.read.side_effect = gate
    if how == "task":
        start_poll(hass, entry)
    else:
        freezer.tick(timedelta(seconds=SLOW + 2))
        async_fire_time_changed(hass)
    await spin(lambda: gate.started(1))

    reload_task = hass.async_create_task(
        hass.config_entries.async_reload(entry.entry_id)
    )
    await settle(200)
    if how == "task":
        # Still in flight, so the new coordinator waits.
        assert gate.calls == 1
        assert not reload_task.done()
        gate.release_all()
    await spin(reload_task.done, steps=2000)
    reload_task.result()
    gate.release_all()
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not old
    assert gate.calls == 2
    assert gate.log[:4] == ORDERED  # the old read ended before the new one began
    assert gate.max_active == 1
    assert number(hass, "power") == 622
    assert state_of(hass, "inverter_online", "binary_sensor") == "on"
