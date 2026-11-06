"""A few missed polls are not a visible glitch.

* A failed daytime poll (a connection error or a protocol error) is a *miss*.  For
  ``MAX_MISSED_POLLS`` misses in a row the entities keep the last online data, if
  that success is recent.  After that a connection error gives *unknown* values
  (never 0: only the night skip reports 0 W) and a protocol error makes the
  entities unavailable.  One success resets the count.
* A failed attempt is retried once, after a pause, if enough of the poll budget is
  left.
* The whole poll runs under a budget (scan interval - 2 s, at least 20 s).
* Every miss and every recovery is logged; ``diagnostics`` shows poll statistics.

Time is the frozen test clock (``freezer``).  A frozen clock freezes ``loop.time()``
too, so ``freezer.tick`` moves the poll budget as well (see ``poll_helpers``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sbfspot.bridge_lock import bridge_lock
from custom_components.sbfspot.const import (
    CONF_SCAN_INTERVAL,
    DOMAIN,
    MAX_MISSED_POLLS,
    RETRY_MIN_BUDGET_LEFT,
)
from custom_components.sbfspot.coordinator import storage_key
from custom_components.sbfspot.diagnostics import async_get_config_entry_diagnostics
from custom_components.sbfspot.smabt import (
    BridgeBusyError,
    BridgeUnreachableError,
    InverterUnreachableError,
    SmaAuthError,
    SmaLogonRefusedError,
    SmaProtocolError,
    SmaTimeoutError,
)

from .conftest import (
    BRIDGE_HOST,
    OFFLINE_ERRORS,
    REAL_SPOT,
    SERIAL,
    TITLE,
    Bridge,
    make_inverter_data,
    number,
    refresh,
    refresh_past_the_grace,
    setup_entry,
    state_of,
)
from .poll_helpers import GatedRead, settle, spin, start_poll

COORDINATOR_LOGGER = "custom_components.sbfspot.coordinator"
ENERGY_TOTAL = REAL_SPOT["e_total"]

# Every sensor that shows an instantaneous value: unknown when a sample is missed.
INSTANTANEOUS_KEYS = (
    "power",
    "ac_power_l1",
    "ac_power_l2",
    "ac_power_l3",
    "ac_voltage_l1",
    "ac_voltage_l2",
    "ac_voltage_l3",
    "ac_current_l1",
    "ac_current_l2",
    "ac_current_l3",
    "grid_frequency",
    "dc_power_a",
    "dc_power_b",
    "dc_voltage_a",
    "dc_voltage_b",
    "dc_current_a",
    "dc_current_b",
    "temperature",
    "status",
    "grid_relay",
    "bluetooth_signal",
)
ZERO_AT_NIGHT_KEYS = (
    "power",
    "ac_power_l1",
    "ac_power_l2",
    "ac_power_l3",
    "dc_power_a",
    "dc_power_b",
    "ac_current_l1",
    "ac_current_l2",
    "ac_current_l3",
    "dc_current_a",
    "dc_current_b",
)
PRESERVED_KEYS = ("energy_today", "energy_total", "operating_time", "feed_in_time")
ALL_KEYS = (*INSTANTANEOUS_KEYS, *PRESERVED_KEYS)


def make_entry(interval: int = 30) -> MockConfigEntry:
    """A configured SB 3000TL-20 that is polled every *interval* seconds."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=str(SERIAL),
        data={
            "host": BRIDGE_HOST,
            "port": 9900,
            "bt_address": "00:80:25:00:00:01",
            "password": "0000",
        },
        options={CONF_SCAN_INTERVAL: interval},
    )


@pytest.fixture
def entry() -> MockConfigEntry:
    """The usual 30 s poll: a budget of 28 s, held data for 4 x 30 = 120 s."""
    return make_entry(30)


def values(hass: HomeAssistant) -> dict[str, str]:
    """The state of every sensor (needs ``entity_registry_enabled_by_default``)."""
    return {key: state_of(hass, key) for key in ALL_KEYS}


def records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    """The messages of exactly *level* that the coordinator logged."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == COORDINATOR_LOGGER and r.levelno == level
    ]


def missed(messages: list[str]) -> list[str]:
    return [m for m in messages if m.startswith("Missed poll")]


async def tick(freezer: FrozenDateTimeFactory, hass: HomeAssistant, seconds: float):
    """Move the clock and fire what is due."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await settle()


# ------------------------------------------------------ the grace: misses 1..3


@pytest.mark.parametrize("error", OFFLINE_ERRORS)
async def test_up_to_three_missed_polls_keep_the_last_online_values(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    hass_storage: dict,
    entity_registry_enabled_by_default: None,
    error: Exception,
) -> None:
    await setup_entry(hass, entry)
    before = values(hass)
    held = entry.runtime_data.data
    assert held.online
    assert before["power"] == "622.0"
    stored = hass_storage[storage_key(entry.entry_id)]
    bridge.read.side_effect = error

    for miss in range(1, MAX_MISSED_POLLS + 1):
        await refresh(hass, entry)

        assert values(hass) == before, f"miss {miss}"
        assert state_of(hass, "inverter_online", "binary_sensor") == "on"
        assert entry.runtime_data.data is held  # the same data, unchanged
        assert entry.runtime_data.last_update_success is True
        assert entry.runtime_data.missed_polls == miss
        assert hass_storage[storage_key(entry.entry_id)] is stored  # not written


async def test_the_held_data_does_not_touch_the_preserved_counters(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    counters = dict(coordinator._counters)
    e_today = coordinator._e_today
    key = storage_key(entry.entry_id)
    stored = hass_storage[key]
    bridge.read.side_effect = InverterUnreachableError("asleep")

    for _ in range(MAX_MISSED_POLLS):
        await refresh(hass, entry)

    assert coordinator._counters == counters
    assert coordinator._e_today == e_today
    assert hass_storage[key] is stored


@pytest.mark.parametrize("error", OFFLINE_ERRORS)
async def test_the_fourth_missed_poll_gives_unknown_values_never_zero(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    entity_registry_enabled_by_default: None,
    error: Exception,
) -> None:
    await setup_entry(hass, entry)
    bridge.read.side_effect = error

    await refresh_past_the_grace(hass, entry)

    coordinator = entry.runtime_data
    assert coordinator.last_update_success is True
    assert not coordinator.data.online
    # A missed sample is unknown, not 0: the entities stay available.
    for key in INSTANTANEOUS_KEYS:
        assert state_of(hass, key) == STATE_UNKNOWN, key
    assert state_of(hass, "inverter_online", "binary_sensor") == "off"
    # The totals keep their values.
    assert number(hass, "energy_total") == pytest.approx(60854.831)
    assert number(hass, "operating_time") == pytest.approx(58826.405)
    assert number(hass, "feed_in_time") == pytest.approx(57238.146)
    assert number(hass, "energy_today") == pytest.approx(4.883)


async def test_the_unknown_values_go_on_while_the_failures_go_on(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, entry)
    bridge.read.side_effect = InverterUnreachableError("asleep")

    await refresh_past_the_grace(hass, entry)
    for _ in range(5):
        await refresh(hass, entry)
        assert state_of(hass, "power") == STATE_UNKNOWN
        assert number(hass, "energy_total") == pytest.approx(ENERGY_TOTAL)
    assert entry.runtime_data.missed_polls == MAX_MISSED_POLLS + 1 + 5


async def test_protocol_errors_are_misses_too(
    hass: HomeAssistant, bridge: Bridge, entry: MockConfigEntry
) -> None:
    await setup_entry(hass, entry)
    bridge.read.side_effect = SmaProtocolError("garbage")

    for _ in range(MAX_MISSED_POLLS):
        await refresh(hass, entry)
        assert entry.runtime_data.last_update_success is True
        assert number(hass, "power") == 622
        assert state_of(hass, "inverter_online", "binary_sensor") == "on"

    await refresh(hass, entry)  # the grace is used up
    assert entry.runtime_data.last_update_success is False
    assert state_of(hass, "power") == STATE_UNAVAILABLE
    assert state_of(hass, "energy_total") == STATE_UNAVAILABLE
    assert state_of(hass, "inverter_online", "binary_sensor") == STATE_UNAVAILABLE
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    bridge.read.side_effect = None
    await refresh(hass, entry)
    assert number(hass, "power") == 622


@pytest.mark.parametrize("error", OFFLINE_ERRORS)
async def test_a_success_restores_fresh_values_and_resets_the_count(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    error: Exception,
) -> None:
    await setup_entry(hass, entry)
    bridge.read.side_effect = error
    for _ in range(MAX_MISSED_POLLS):
        await refresh(hass, entry)
    assert entry.runtime_data.missed_polls == MAX_MISSED_POLLS

    fresh = make_inverter_data(pac_total=700.0, pac1=700.0, e_total=60860.0)
    bridge.read.side_effect = None
    bridge.read.return_value = fresh
    await refresh(hass, entry)

    assert entry.runtime_data.missed_polls == 0
    assert number(hass, "power") == 700
    assert number(hass, "energy_total") == pytest.approx(60860.0)
    assert state_of(hass, "inverter_online", "binary_sensor") == "on"

    # The count starts again at 1: three more misses show the fresh values ...
    bridge.read.side_effect = error
    for _ in range(MAX_MISSED_POLLS):
        await refresh(hass, entry)
        assert number(hass, "power") == 700
    # ... and the fourth does not.
    await refresh(hass, entry)
    assert state_of(hass, "power") == STATE_UNKNOWN
    assert number(hass, "energy_total") == pytest.approx(60860.0)

    # A success after the grace was used up restores the values, too.
    bridge.read.side_effect = None
    await refresh(hass, entry)
    assert number(hass, "power") == 700
    assert state_of(hass, "inverter_online", "binary_sensor") == "on"
    assert entry.runtime_data.missed_polls == 0


@pytest.mark.parametrize("error", OFFLINE_ERRORS)
async def test_there_is_no_grace_before_the_first_success(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    error: Exception,
) -> None:
    """Nothing to hold: unknown at once (and never 0), and setup still succeeds."""
    bridge.read.side_effect = error

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert not entry.runtime_data.data.online
    assert state_of(hass, "power") == STATE_UNKNOWN
    assert state_of(hass, "ac_voltage_l1") == STATE_UNKNOWN
    assert state_of(hass, "inverter_online", "binary_sensor") == "off"
    assert state_of(hass, "energy_total") == STATE_UNKNOWN  # never read

    # The first success starts the grace.
    bridge.read.side_effect = None
    await refresh(hass, entry)
    assert number(hass, "power") == 622
    bridge.read.side_effect = error
    await refresh(hass, entry)
    assert number(hass, "power") == 622


async def test_a_protocol_error_before_the_first_success_is_not_held(
    hass: HomeAssistant, bridge: Bridge, entry: MockConfigEntry
) -> None:
    bridge.read.side_effect = SmaProtocolError("garbage")

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY


# ----------------------------------------------- what is not a miss: auth, refusal


async def test_a_wrong_password_is_not_a_miss(
    hass: HomeAssistant, bridge: Bridge, entry: MockConfigEntry
) -> None:
    await setup_entry(hass, entry)
    bridge.read.side_effect = InverterUnreachableError("asleep")
    await refresh(hass, entry)
    assert entry.runtime_data.missed_polls == 1

    bridge.read.side_effect = SmaAuthError("rejected")
    await refresh(hass, entry)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    assert entry.runtime_data.last_update_success is False
    assert entry.runtime_data.missed_polls == 1  # not counted
    misses = entry.runtime_data.statistics.as_dict()["misses_by_exception"]
    assert misses == {"InverterUnreachableError": 1}


async def test_a_refused_logon_is_not_a_miss_and_still_backs_off(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, entry)
    bridge.read.side_effect = InverterUnreachableError("asleep")
    await refresh(hass, entry)

    bridge.read.side_effect = SmaLogonRefusedError(1)
    bridge.read.reset_mock()
    await refresh(hass, entry)

    assert entry.runtime_data.last_update_success is False
    assert state_of(hass, "power") == STATE_UNAVAILABLE
    assert entry.runtime_data.missed_polls == 1  # not counted
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert bridge.reads == 1  # a refusal is not retried

    freezer.tick(timedelta(seconds=60))
    bridge.read.reset_mock()
    await refresh(hass, entry)
    bridge.read.assert_not_awaited()  # the 15 minute back-off


# --------------------------------------------------------------------- the night


async def test_the_night_skip_wins_during_the_grace_and_drops_the_held_data(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    sun_is_up,
) -> None:
    await setup_entry(hass, entry)
    bridge.read.side_effect = InverterUnreachableError("asleep")
    for _ in range(2):
        await refresh(hass, entry)
    assert number(hass, "power") == 622  # held
    bridge.read.reset_mock()

    sun_is_up.return_value = False
    await refresh(hass, entry)

    # Night: offline data with a real 0 W, and no connection.
    bridge.read.assert_not_awaited()
    assert not entry.runtime_data.data.online
    assert number(hass, "power") == 0
    assert state_of(hass, "inverter_online", "binary_sensor") == "off"
    assert entry.runtime_data.missed_polls == 0
    assert number(hass, "energy_total") == pytest.approx(ENERGY_TOTAL)

    # The sun is up again and the inverter still asleep: not 622, not 0.
    sun_is_up.return_value = True
    await refresh(hass, entry)
    assert state_of(hass, "power") == STATE_UNKNOWN
    assert entry.runtime_data.missed_polls == 1


async def test_only_the_night_skip_reports_zero_power_and_current(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    sun_is_up,
    entity_registry_enabled_by_default: None,
) -> None:
    """At night 0 W is real; the sample is not missing, the inverter sleeps."""
    sun_is_up.return_value = False

    await setup_entry(hass, entry)

    bridge.read.assert_not_awaited()
    for key in ZERO_AT_NIGHT_KEYS:
        assert number(hass, key) == 0, key
    for key in INSTANTANEOUS_KEYS:
        if key not in ZERO_AT_NIGHT_KEYS:
            assert state_of(hass, key) == STATE_UNKNOWN, key
    assert state_of(hass, "inverter_online", "binary_sensor") == "off"


# --------------------------------------------------- recent successes only (B)

SUNRISE = datetime(2026, 6, 21, 6, 0, tzinfo=UTC)
SUNSET = datetime(2026, 6, 21, 18, 0, tzinfo=UTC)


def _fake_sun(hass: HomeAssistant, utc_point_in_time: datetime | None = None) -> bool:
    assert utc_point_in_time is not None
    # The same hours on every day of the test.
    time_of_day = utc_point_in_time.timetz()
    return SUNRISE.timetz() <= time_of_day < SUNSET.timetz()


@pytest.mark.parametrize("night_polls", [True, False], ids=["polled", "slept_through"])
async def test_yesterdays_data_is_never_held_in_the_morning(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    sun_is_up,
    freezer: FrozenDateTimeFactory,
    night_polls: bool,
) -> None:
    sun_is_up.side_effect = _fake_sun
    freezer.move_to("2026-06-21 17:30:00+00:00")
    await setup_entry(hass, entry)
    assert number(hass, "power") == 622

    if night_polls:
        freezer.move_to("2026-06-21 21:00:00+00:00")
        await refresh(hass, entry)
        assert number(hass, "power") == 0  # the night skip reports 0 W
    freezer.move_to("2026-06-22 05:50:00+00:00")  # sunrise - 10 min
    bridge.read.side_effect = InverterUnreachableError("asleep")
    await refresh(hass, entry)

    assert entry.runtime_data.data is not None
    assert not entry.runtime_data.data.online
    assert state_of(hass, "power") == STATE_UNKNOWN  # not 622, not 0
    assert state_of(hass, "inverter_online", "binary_sensor") == "off"
    assert number(hass, "energy_total") == pytest.approx(ENERGY_TOTAL)


@pytest.mark.parametrize(
    ("interval", "age", "held"),
    [
        (30, 100, True),
        (30, 120, True),  # exactly (3 + 1) x 30 s: still recent
        (30, 121, False),
        (30, 3600, False),
        (60, 230, True),  # the window follows the scan interval: 4 x 60 s
        (60, 241, False),
    ],
)
async def test_a_success_older_than_four_intervals_is_not_held(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    interval: int,
    age: int,
    held: bool,
) -> None:
    entry = make_entry(interval)
    await setup_entry(hass, entry)
    assert number(hass, "power") == 622

    freezer.tick(timedelta(seconds=age))
    bridge.read.side_effect = InverterUnreachableError("asleep")
    await refresh(hass, entry)

    if held:
        assert number(hass, "power") == 622
        assert entry.runtime_data.data.online
    else:
        assert state_of(hass, "power") == STATE_UNKNOWN
        assert not entry.runtime_data.data.online
    assert number(hass, "energy_total") == pytest.approx(ENERGY_TOTAL)


async def test_an_old_success_does_not_hold_a_protocol_error(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, entry)
    freezer.tick(timedelta(seconds=500))
    bridge.read.side_effect = SmaProtocolError("garbage")

    await refresh(hass, entry)

    assert entry.runtime_data.last_update_success is False
    assert state_of(hass, "power") == STATE_UNAVAILABLE


# ------------------------------------------------------------------- the retry


async def test_a_failed_attempt_is_retried_once_after_a_pause(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    retry_pause: None,
) -> None:
    await setup_entry(hass, entry)
    bridge.read.reset_mock()
    bridge.read.side_effect = [
        InverterUnreachableError("asleep"),
        make_inverter_data(pac_total=700.0, pac1=700.0),
    ]

    poll = start_poll(hass, entry)
    await spin(lambda: bridge.read.await_count == 1)
    await settle()
    assert bridge.read.await_count == 1  # waiting out the pause

    await tick(freezer, hass, 1)
    assert bridge.read.await_count == 1  # 1 s of 2 s
    await tick(freezer, hass, 1)
    await spin(poll.done)
    poll.result()

    assert bridge.read.await_count == 2
    assert number(hass, "power") == 700
    assert state_of(hass, "inverter_online", "binary_sensor") == "on"
    coordinator = entry.runtime_data
    assert coordinator.missed_polls == 0  # a retry that works is no miss
    stats = coordinator.statistics.as_dict()
    assert stats["successes_after_retry"] == 1
    assert stats["misses_by_exception"] == {}


async def test_two_failed_attempts_are_one_missed_poll(
    hass: HomeAssistant, bridge: Bridge, entry: MockConfigEntry
) -> None:
    await setup_entry(hass, entry)
    bridge.read.reset_mock()
    bridge.read.side_effect = InverterUnreachableError("asleep")

    await refresh(hass, entry)

    assert bridge.read.await_count == 2  # one retry, not more
    assert entry.runtime_data.missed_polls == 1
    misses = entry.runtime_data.statistics.as_dict()["misses_by_exception"]
    assert misses == {"InverterUnreachableError": 1}


def test_the_retry_threshold_is_fifteen_seconds() -> None:
    assert RETRY_MIN_BUDGET_LEFT == 15


@pytest.mark.parametrize(
    ("spent", "attempts"),
    [
        pytest.param(0, 2, id="nothing_spent"),
        pytest.param(5, 2, id="fast_failure_23_s_left"),
        pytest.param(13, 2, id="15_s_left"),
        pytest.param(14, 1, id="14_s_left"),
        pytest.param(15, 1, id="slow_bluetooth_connect_failure_13_s_left"),
        pytest.param(26, 1, id="2_s_left"),
    ],
)
async def test_the_retry_needs_fifteen_seconds_of_the_budget(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    spent: int,
    attempts: int,
) -> None:
    await setup_entry(hass, entry)  # the budget is 28 s
    bridge.read.reset_mock()
    calls = 0

    async def failing_read(*args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            freezer.tick(timedelta(seconds=spent))  # the first attempt was slow
        raise InverterUnreachableError("asleep")

    bridge.read.side_effect = failing_read

    await refresh(hass, entry)

    assert bridge.read.await_count == attempts
    assert entry.runtime_data.missed_polls == 1


async def test_a_slow_connect_failure_is_not_retried_and_is_logged_as_a_miss(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bridge gives up on the Bluetooth connect after ~15 s: 13 s of the 28 s
    budget are left, a retry would be cut."""
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.reset_mock()

    async def slow_failure(*args: Any) -> None:
        freezer.tick(timedelta(seconds=15))
        raise InverterUnreachableError("bridge could not reach the inverter")

    bridge.read.side_effect = slow_failure

    await refresh(hass, entry)

    assert bridge.read.await_count == 1
    stats = entry.runtime_data.statistics.as_dict()
    assert stats["misses_by_exception"] == {"InverterUnreachableError": 1}
    (message,) = missed(records(caplog, logging.INFO))
    assert message == (
        "Missed poll 1/3: InverterUnreachableError: "
        "bridge could not reach the inverter (after 15.0 s)"
    )


# ------------------------------------------------- a retry cut by the budget


async def test_a_retry_cut_by_the_budget_is_a_miss_with_the_first_error(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, entry)
    held = entry.runtime_data.data
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    calls = 0
    cancelled: list[int] = []

    async def read(*args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InverterUnreachableError("asleep")
        try:
            await asyncio.Event().wait()  # the retry never answers
        except asyncio.CancelledError:
            cancelled.append(calls)
            raise

    bridge.read.side_effect = read

    poll = start_poll(hass, entry)
    await spin(lambda: calls == 2)
    await tick(freezer, hass, 28)
    await spin(poll.done)
    poll.result()

    assert cancelled == [2]  # the budget cancelled the retry
    coordinator = entry.runtime_data
    assert coordinator.missed_polls == 1
    assert coordinator.data is held  # grace
    stats = coordinator.statistics.as_dict()
    # Counted under the first attempt's class, not as a timeout.
    assert stats["misses_by_exception"] == {"InverterUnreachableError": 1}
    (message,) = missed(records(caplog, logging.INFO))
    assert re.fullmatch(
        r"Missed poll 1/3: InverterUnreachableError: asleep "
        r"\(retry cut by the poll budget\) \(after 28\.0 s\)",
        message,
    ), message


async def test_the_exception_class_of_a_cut_retry_follows_the_first_error(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, entry)
    calls = 0

    async def read(*args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BridgeBusyError("busy")
        await asyncio.Event().wait()

    bridge.read.side_effect = read
    poll = start_poll(hass, entry)
    await spin(lambda: calls == 2)
    await tick(freezer, hass, 28)
    await spin(poll.done)
    poll.result()

    stats = entry.runtime_data.statistics.as_dict()
    assert stats["misses_by_exception"] == {"BridgeBusyError": 1}


async def test_the_budget_ending_on_the_first_attempt_is_still_a_timeout(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    gate = GatedRead(hold={1})
    bridge.read.side_effect = gate
    poll = start_poll(hass, entry)
    await spin(lambda: gate.started(1))
    await tick(freezer, hass, 28)
    await spin(poll.done)
    poll.result()

    stats = entry.runtime_data.statistics.as_dict()
    assert stats["misses_by_exception"] == {"SmaTimeoutError": 1}
    (message,) = missed(records(caplog, logging.INFO))
    assert message.startswith("Missed poll 1/3: SmaTimeoutError: ")
    assert "retry cut" not in message


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(SmaProtocolError("garbage"), id="protocol"),
        pytest.param(SmaAuthError("rejected"), id="auth"),
        pytest.param(SmaLogonRefusedError(1), id="logon_refused"),
    ],
)
async def test_only_connection_errors_are_retried(
    hass: HomeAssistant, bridge: Bridge, entry: MockConfigEntry, error: Exception
) -> None:
    await setup_entry(hass, entry)
    bridge.read.reset_mock()
    bridge.read.side_effect = error

    await refresh(hass, entry)

    assert bridge.read.await_count == 1


# ------------------------------------------------------------------ the budget


async def test_a_read_that_never_returns_is_cancelled_at_the_budget(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, entry)  # 30 s interval: a budget of 28 s
    held = entry.runtime_data.data
    gate = GatedRead(hold={1})
    bridge.read.side_effect = gate

    poll = start_poll(hass, entry)
    await spin(lambda: gate.started(1))
    await tick(freezer, hass, 27)
    assert not poll.done()
    assert gate.cancelled == []

    await tick(freezer, hass, 1)  # the budget is used up
    await spin(poll.done)
    poll.result()

    assert gate.cancelled == [1]  # the cancellation reached the read
    assert gate.active == 0
    coordinator = entry.runtime_data
    assert coordinator.last_update_success is True  # a miss, not a failure
    assert coordinator.missed_polls == 1
    assert coordinator.data is held  # grace
    stats = coordinator.statistics.as_dict()
    assert stats["misses_by_exception"] == {"SmaTimeoutError": 1}
    assert stats["last_poll_duration_seconds"] == pytest.approx(28, abs=0.5)
    assert stats["max_poll_duration_seconds"] == pytest.approx(28, abs=0.5)

    # The next poll happens as usual.
    await tick(freezer, hass, 32)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert gate.calls == 2
    assert coordinator.missed_polls == 0


async def test_the_budget_follows_the_scan_interval(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = make_entry(300)  # a budget of 298 s
    await setup_entry(hass, entry)
    gate = GatedRead(hold={1})
    bridge.read.side_effect = gate

    poll = start_poll(hass, entry)
    await spin(lambda: gate.started(1))
    await tick(freezer, hass, 100)
    assert not poll.done()
    await tick(freezer, hass, 198)
    await spin(poll.done)
    assert gate.cancelled == [1]


@pytest.mark.parametrize(
    ("interval", "budget"),
    [(30, 28.0), (45, 43.0), (60, 58.0), (22, 20.0), (5, 20.0), (3600, 3598.0)],
)
async def test_the_budget_formula(
    hass: HomeAssistant, bridge: Bridge, interval: int, budget: float
) -> None:
    """``max(20 s, scan interval - 2 s)``."""
    entry = make_entry(interval)
    await setup_entry(hass, entry)

    assert entry.runtime_data.poll_budget == budget


async def test_waiting_for_the_bridge_counts_against_the_budget(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, entry)
    held = entry.runtime_data.data
    bridge.read.reset_mock()
    lock = bridge_lock(hass, BRIDGE_HOST, 9900)
    await lock.acquire()  # another read uses the bridge for a long time
    try:
        poll = start_poll(hass, entry)
        await settle()
        assert not poll.done()
        bridge.read.assert_not_awaited()

        await tick(freezer, hass, 28)
        await spin(poll.done)
        poll.result()
    finally:
        lock.release()

    bridge.read.assert_not_awaited()  # it never got the bridge
    coordinator = entry.runtime_data
    assert coordinator.missed_polls == 1
    assert coordinator.data is held
    assert coordinator.statistics.as_dict()["misses_by_exception"] == {
        "SmaTimeoutError": 1
    }
    assert not lock.locked()


# --------------------------------------------------------------------- logging


async def test_every_missed_poll_is_logged_at_info(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.side_effect = InverterUnreachableError("asleep")

    await refresh_past_the_grace(hass, entry)

    infos = missed(records(caplog, logging.INFO))
    assert len(infos) == MAX_MISSED_POLLS + 1
    for number_, message in enumerate(infos, start=1):
        assert re.fullmatch(
            rf"Missed poll {number_}/3: InverterUnreachableError: asleep "
            r"\(after \d+\.\d s\)",
            message,
        ), message


async def test_a_protocol_error_is_logged_with_its_class_and_text(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.side_effect = SmaProtocolError("short packet")

    await refresh(hass, entry)

    (message,) = missed(records(caplog, logging.INFO))
    assert message.startswith("Missed poll 1/3: SmaProtocolError: short packet (after ")


async def test_one_warning_per_episode_when_the_grace_is_used_up(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.side_effect = InverterUnreachableError("asleep")

    for _ in range(MAX_MISSED_POLLS):
        await refresh(hass, entry)
    assert records(caplog, logging.WARNING) == []  # the grace holds: no warning

    for _ in range(4):  # the 4th miss and three more
        await refresh(hass, entry)
    (warning,) = records(caplog, logging.WARNING)
    assert "4 polls in a row" in warning
    assert "unknown" in warning
    # Only the first misses beyond the grace are INFO: a long outage stays quiet.
    assert len(missed(records(caplog, logging.INFO))) == MAX_MISSED_POLLS + 1
    assert any(m.startswith("Missed poll 7/3") for m in records(caplog, logging.DEBUG))

    # Recovery: one INFO.
    caplog.clear()
    bridge.read.side_effect = None
    await refresh(hass, entry)
    assert [
        m for m in records(caplog, logging.INFO) if m.startswith("Poll succeeded")
    ] == ["Poll succeeded again after 7 missed polls"]
    assert records(caplog, logging.WARNING) == []

    # A new episode warns again.
    caplog.clear()
    bridge.read.side_effect = InverterUnreachableError("asleep")
    await refresh_past_the_grace(hass, entry)
    assert len(records(caplog, logging.WARNING)) == 1


def exhaustion(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The records that tell the grace is used up."""
    return [
        r
        for r in caplog.records
        if r.name == COORDINATOR_LOGGER and "polls in a row" in r.getMessage()
    ]


@pytest.mark.parametrize(
    ("utc_time", "level"),
    [
        pytest.param("05:50", logging.INFO, id="10_min_before_sunrise"),
        pytest.param("06:30", logging.INFO, id="30_min_after_sunrise"),
        pytest.param("06:59", logging.INFO, id="59_min_after_sunrise"),
        pytest.param("07:01", logging.WARNING, id="61_min_after_sunrise"),
        pytest.param("12:00", logging.WARNING, id="midday"),
        pytest.param("16:59", logging.WARNING, id="61_min_before_sunset"),
        pytest.param("17:01", logging.INFO, id="59_min_before_sunset"),
        pytest.param("17:50", logging.INFO, id="10_min_before_sunset"),
        pytest.param("18:10", logging.INFO, id="10_min_after_sunset"),
    ],
)
async def test_a_used_up_grace_near_sunrise_or_sunset_is_info_else_warning(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    sun_is_up,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    utc_time: str,
    level: int,
) -> None:
    """The inverter wakes up and goes to sleep around the sun: expected, no warning."""
    sun_is_up.side_effect = _fake_sun
    freezer.move_to(f"2026-06-21 {utc_time}:00+00:00")
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.side_effect = InverterUnreachableError("asleep")

    for _ in range(MAX_MISSED_POLLS + 3):  # one episode, reported once
        await refresh(hass, entry)

    (record,) = exhaustion(caplog)
    assert record.levelno == level
    assert "Its values are unknown" in record.getMessage()
    if level == logging.INFO:
        assert records(caplog, logging.WARNING) == []


async def test_a_protocol_error_episode_near_sunset_is_info_too(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    sun_is_up,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sun_is_up.side_effect = _fake_sun
    freezer.move_to("2026-06-21 17:30:00+00:00")
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.side_effect = SmaProtocolError("garbage")

    await refresh_past_the_grace(hass, entry)

    (record,) = exhaustion(caplog)
    assert record.levelno == logging.INFO
    assert "unavailable" in record.getMessage()


async def test_an_unreachable_bridge_near_sunset_is_still_a_warning(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    sun_is_up,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ESP32 does not sleep with the sun: that is a real problem at any time."""
    sun_is_up.side_effect = _fake_sun
    freezer.move_to("2026-06-21 17:30:00+00:00")
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.side_effect = BridgeUnreachableError("connection refused")

    await refresh_past_the_grace(hass, entry)

    (warning,) = records(caplog, logging.WARNING)
    assert "192.168.1.50:9900" in warning
    assert exhaustion(caplog) == []


async def test_a_sun_that_cannot_be_computed_keeps_the_warning(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    sun_is_up,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Polar regions: no sunrise or sunset to compare with, so warn."""
    sun_is_up.side_effect = ValueError("Unable to find event after one year")
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.side_effect = InverterUnreachableError("asleep")

    await refresh_past_the_grace(hass, entry)

    (record,) = exhaustion(caplog)
    assert record.levelno == logging.WARNING


async def test_a_recovery_inside_the_grace_is_logged_too(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    bridge.read.side_effect = InverterUnreachableError("asleep")
    await refresh(hass, entry)
    await refresh(hass, entry)
    caplog.clear()

    bridge.read.side_effect = None
    await refresh(hass, entry)

    assert [
        m for m in records(caplog, logging.INFO) if m.startswith("Poll succeeded")
    ] == ["Poll succeeded again after 2 missed polls"]
    assert records(caplog, logging.WARNING) == []


async def test_an_unreachable_bridge_gives_one_warning_not_two(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.side_effect = BridgeUnreachableError("connection refused")

    for _ in range(MAX_MISSED_POLLS):
        await refresh(hass, entry)
    assert records(caplog, logging.WARNING) == []

    for _ in range(4):
        await refresh(hass, entry)
    (warning,) = records(caplog, logging.WARNING)
    assert "192.168.1.50:9900" in warning
    assert "connection refused" in warning

    # The bridge answers again: INFO, and the next outage warns again.
    caplog.clear()
    bridge.read.side_effect = None
    await refresh(hass, entry)
    infos = records(caplog, logging.INFO)
    assert "The SMA Bluetooth bridge is reachable again" in infos
    assert "Poll succeeded again after 7 missed polls" in infos
    caplog.clear()
    bridge.read.side_effect = BridgeUnreachableError("timed out")
    await refresh_past_the_grace(hass, entry)
    assert len(records(caplog, logging.WARNING)) == 1


async def test_a_protocol_error_episode_warns_once(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.side_effect = SmaProtocolError("garbage")

    for _ in range(MAX_MISSED_POLLS + 3):
        await refresh(hass, entry)

    (warning,) = records(caplog, logging.WARNING)
    assert "unexpected data" in warning
    assert "unavailable" in warning


async def test_no_warning_without_a_success_to_hold(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The inverter is asleep when the sun comes up: that is no grace used up."""
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    bridge.read.side_effect = InverterUnreachableError("asleep")
    await setup_entry(hass, entry)

    for _ in range(MAX_MISSED_POLLS + 3):
        await refresh(hass, entry)

    assert records(caplog, logging.WARNING) == []
    assert missed(records(caplog, logging.INFO)) == []  # quiet at dawn
    assert any(m.startswith("Missed poll") for m in records(caplog, logging.DEBUG))

    bridge.read.side_effect = None
    caplog.clear()
    await refresh(hass, entry)
    assert [
        m for m in records(caplog, logging.INFO) if m.startswith("Poll succeeded")
    ] == []


async def test_every_poll_is_logged_at_debug_with_duration_and_attempts(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()

    await refresh(hass, entry)  # one attempt
    bridge.read.side_effect = [InverterUnreachableError("x"), make_inverter_data()]
    await refresh(hass, entry)  # the retry works
    bridge.read.side_effect = InverterUnreachableError("x")
    await refresh(hass, entry)  # two attempts fail

    polls = [m for m in records(caplog, logging.DEBUG) if m.startswith("Poll ended")]
    assert [re.sub(r"\d+\.\d s", "N s", m) for m in polls] == [
        "Poll ended: success after N s, 1 attempt(s)",
        "Poll ended: success after N s, 2 attempt(s)",
        "Poll ended: missed after N s, 2 attempt(s)",
    ]


# ----------------------------------------------------------------- diagnostics


async def test_diagnostics_contain_the_poll_statistics(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-06-21 12:00:00+00:00")
    await setup_entry(hass, entry)  # poll 1: success
    bridge.read.side_effect = [InverterUnreachableError("x"), make_inverter_data()]
    await refresh(hass, entry)  # poll 2: success after the retry
    freezer.tick(timedelta(seconds=30))
    bridge.read.side_effect = InverterUnreachableError("asleep")
    await refresh(hass, entry)  # misses 1 and 2
    await refresh(hass, entry)
    bridge.read.side_effect = SmaTimeoutError("no answer")
    await refresh(hass, entry)  # misses 3 and 4 (the grace is used up)
    await refresh(hass, entry)

    result = await async_get_config_entry_diagnostics(hass, entry)

    stats = result["coordinator"]["poll_statistics"]
    assert stats["polls"] == 6
    assert stats["successes"] == 2
    assert stats["successes_after_retry"] == 1
    assert stats["misses_by_exception"] == {
        "InverterUnreachableError": 2,
        "SmaTimeoutError": 2,
    }
    assert stats["grace_exhaustions"] == 1
    assert stats["skipped_reentrant_refreshes"] == 0
    assert stats["last_success"] == "2026-06-21T12:00:00+00:00"
    assert (
        0 <= stats["last_poll_duration_seconds"] <= stats["max_poll_duration_seconds"]
    )
    assert stats["missed_polls_in_a_row"] == 4
    assert stats["poll_budget_seconds"] == 28
    json.dumps(result)  # plain JSON


async def test_the_statistics_start_empty_and_skip_the_night(
    hass: HomeAssistant,
    bridge: Bridge,
    entry: MockConfigEntry,
    sun_is_up,
) -> None:
    sun_is_up.return_value = False
    await setup_entry(hass, entry)

    stats = (await async_get_config_entry_diagnostics(hass, entry))["coordinator"][
        "poll_statistics"
    ]

    assert stats["polls"] == 0  # the night skip is no poll
    assert stats["successes"] == 0
    assert stats["misses_by_exception"] == {}
    assert stats["last_success"] is None
    assert stats["last_poll_duration_seconds"] is None
    assert stats["max_poll_duration_seconds"] == 0
