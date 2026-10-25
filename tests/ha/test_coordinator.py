"""The coordinator rules.

* Night is normal: no connection outside the sun window, connection errors are
  *offline* data (never ``UpdateFailed``), setup at night succeeds.
* TOTAL_INCREASING sensors never report 0 and never go down: ``energy_total``,
  ``operating_time`` and ``feed_in_time`` keep the last valid value (persisted
  in a ``Store``); ``energy_today`` keeps its value until local midnight.
* A wrong password starts reauth, unexpected data makes entities unavailable.

A wrong 0 in a TOTAL_INCREASING sensor permanently corrupts the long-term
statistics: extend these tests, never relax them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sbfspot.const import DOMAIN
from custom_components.sbfspot.coordinator import storage_key
from custom_components.sbfspot.smabt import (
    BridgeBusyError,
    BridgeUnreachableError,
    InverterUnreachableError,
    SmaAuthError,
    SmaConnectionError,
    SmaProtocolError,
    SmaTimeoutError,
    SpotData,
)

from .conftest import (
    OFFLINE_ERRORS,
    REAL_SPOT,
    Bridge,
    make_inverter_data,
    number,
    refresh,
    setup_entry,
    state_of,
)

# (sensor key, SpotData field): the counters that must never be 0 or go down.
CUMULATIVE = [
    pytest.param("energy_total", "e_total", id="energy_total"),
    pytest.param("operating_time", "operating_time", id="operating_time"),
    pytest.param("feed_in_time", "feed_in_time", id="feed_in_time"),
]
CUMULATIVE_KEYS = ["energy_total", "operating_time", "feed_in_time"]
COORDINATOR_LOGGER = "custom_components.sbfspot.coordinator"


def _data(entry: MockConfigEntry) -> Any:
    return entry.runtime_data.data


# ------------------------------------------------------------------ the sun


SUNRISE = datetime(2026, 6, 21, 6, 0, tzinfo=UTC)
SUNSET = datetime(2026, 6, 21, 18, 0, tzinfo=UTC)


def _fake_sun(hass: HomeAssistant, utc_point_in_time: datetime | None = None) -> bool:
    assert utc_point_in_time is not None
    return SUNRISE <= utc_point_in_time < SUNSET


@pytest.mark.parametrize(
    ("utc_time", "polls"),
    [
        ("2026-06-21 05:40:00", False),  # more than 15 min before sunrise
        ("2026-06-21 05:46:00", True),  # 14 min before sunrise
        ("2026-06-21 06:00:00", True),  # sunrise
        ("2026-06-21 12:00:00", True),  # noon
        ("2026-06-21 18:00:00", True),  # sunset
        ("2026-06-21 18:14:00", True),  # 14 min after sunset
        ("2026-06-21 18:16:00", False),  # more than 15 min after sunset
        ("2026-06-21 23:30:00", False),  # night
    ],
)
async def test_the_inverter_is_polled_from_15_minutes_before_sunrise_to_15_after_sunset(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    utc_time: str,
    polls: bool,
) -> None:
    sun_is_up.side_effect = _fake_sun
    freezer.move_to("2026-06-21 12:00:00+00:00")
    await setup_entry(hass, mock_config_entry)
    assert bridge.reads == 1

    freezer.move_to(f"{utc_time}+00:00")
    await refresh(hass, mock_config_entry)

    assert bridge.reads == (2 if polls else 1)
    assert _data(mock_config_entry).online is polls


async def test_no_connection_is_opened_while_the_sun_is_down(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    sun_is_up.return_value = False
    bridge.read.reset_mock()
    bridge.transport.reset_mock()

    for _ in range(3):
        await refresh(hass, mock_config_entry)

    bridge.read.assert_not_awaited()
    bridge.transport.assert_not_called()
    assert mock_config_entry.runtime_data.last_update_success
    assert not _data(mock_config_entry).online


async def test_polling_resumes_when_the_sun_is_up_again(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
) -> None:
    sun_is_up.return_value = False
    await setup_entry(hass, mock_config_entry)
    bridge.read.assert_not_awaited()

    sun_is_up.return_value = True
    await refresh(hass, mock_config_entry)

    assert bridge.reads == 1
    assert _data(mock_config_entry).online
    assert state_of(hass, "energy_total") == "60854.831"


async def test_a_polar_sun_that_cannot_be_computed_still_polls(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
) -> None:
    sun_is_up.side_effect = ValueError("Unable to find event after one year")

    await setup_entry(hass, mock_config_entry)

    assert bridge.reads == 1
    assert _data(mock_config_entry).online


async def test_the_inverter_is_read_on_every_update_interval(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    assert bridge.reads == 1

    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert bridge.reads == 2

    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert bridge.reads == 3


# ----------------------------------------------------------- online values


async def test_online_poll_reads_the_inverter_through_the_bridge(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    bridge.transport.assert_called_once_with("192.168.1.50", 9900, "00:80:25:00:00:01")
    bridge.read.assert_awaited_once_with(bridge.transport.return_value, "0000")
    data = _data(mock_config_entry)
    assert data.online
    assert data.spot == make_inverter_data().spot
    assert data.info == make_inverter_data().info


# ------------------------------------------------------------- offline rules


@pytest.mark.parametrize("error", OFFLINE_ERRORS)
async def test_connection_errors_are_offline_data_not_failures(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.side_effect = error

    await refresh(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.last_update_success is True
    assert not _data(mock_config_entry).online
    # The entities stay available.
    assert state_of(hass, "power") != STATE_UNAVAILABLE
    assert state_of(hass, "inverter_online", "binary_sensor") == "off"


async def test_the_offline_values_follow_the_rules(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.side_effect = InverterUnreachableError("asleep")

    await refresh(hass, mock_config_entry)

    assert _data(mock_config_entry).spot == SpotData(
        # instantaneous power and current: 0
        pac_total=0.0,
        pac1=0.0,
        pac2=0.0,
        pac3=0.0,
        pdc1=0.0,
        pdc2=0.0,
        iac1=0.0,
        iac2=0.0,
        iac3=0.0,
        idc1=0.0,
        idc2=0.0,
        # voltages, frequency, temperature, signal, status: unknown
        uac1=None,
        uac2=None,
        uac3=None,
        udc1=None,
        udc2=None,
        grid_frequency=None,
        temperature=None,
        bt_signal=None,
        status=None,
        grid_relay=None,
        # counters: the last valid value; e_today: still today's
        e_total=REAL_SPOT["e_total"],
        operating_time=REAL_SPOT["operating_time"],
        feed_in_time=REAL_SPOT["feed_in_time"],
        e_today=REAL_SPOT["e_today"],
    )


async def test_offline_entity_states(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    entity_registry_enabled_by_default: None,
) -> None:
    await setup_entry(hass, mock_config_entry)
    assert number(hass, "power") == 622
    bridge.read.side_effect = InverterUnreachableError("asleep")

    await refresh(hass, mock_config_entry)

    for key in ("power", "ac_power_l1", "ac_power_l2", "ac_power_l3"):
        assert number(hass, key) == 0, key
    for key in ("dc_power_a", "dc_power_b", "ac_current_l1", "dc_current_a"):
        assert number(hass, key) == 0, key
    for key in (
        "ac_voltage_l1",
        "ac_voltage_l2",
        "ac_voltage_l3",
        "dc_voltage_a",
        "dc_voltage_b",
        "grid_frequency",
        "temperature",
        "bluetooth_signal",
        "status",
        "grid_relay",
    ):
        assert state_of(hass, key) == STATE_UNKNOWN, key
    assert number(hass, "energy_total") == pytest.approx(60854.831)
    assert number(hass, "operating_time") == pytest.approx(58826.405)
    assert number(hass, "feed_in_time") == pytest.approx(57238.146)
    assert number(hass, "energy_today") == pytest.approx(4.883)


# ------------------------------------------------- cumulative counters (never 0)


@pytest.mark.parametrize(("key", "field"), CUMULATIVE)
@pytest.mark.parametrize("error", OFFLINE_ERRORS)
async def test_offline_keeps_the_last_counter_value(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    key: str,
    field: str,
    error: Exception,
) -> None:
    await setup_entry(hass, mock_config_entry)
    before = state_of(hass, key)
    assert float(before) == pytest.approx(REAL_SPOT[field])

    bridge.read.side_effect = error
    for _ in range(3):
        await refresh(hass, mock_config_entry)

    assert state_of(hass, key) == before


@pytest.mark.parametrize(("key", "field"), CUMULATIVE)
@pytest.mark.parametrize(
    "bad_value",
    [None, 0.0, 0, -1.0, float("nan"), float("inf"), 1.0],
    ids=["none", "zero", "zero_int", "negative", "nan", "inf", "lower"],
)
async def test_online_none_zero_or_lower_counter_keeps_the_preserved_value(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    key: str,
    field: str,
    bad_value: float | None,
) -> None:
    await setup_entry(hass, mock_config_entry)
    before = state_of(hass, key)

    bridge.read.return_value = make_inverter_data(**{field: bad_value})
    await refresh(hass, mock_config_entry)

    assert _data(mock_config_entry).online
    assert state_of(hass, key) == before


@pytest.mark.parametrize(("key", "field"), CUMULATIVE)
@pytest.mark.parametrize(
    "bad_value",
    [None, 0.0, 0, -1.0, float("nan")],
    ids=["none", "zero", "zero_int", "negative", "nan"],
)
async def test_a_first_poll_without_a_valid_counter_is_unknown_never_zero(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    key: str,
    field: str,
    bad_value: float | None,
) -> None:
    """Nothing is preserved yet: unknown beats 0 (a 0 corrupts the statistics)."""
    bridge.read.return_value = make_inverter_data(**{field: bad_value})

    await setup_entry(hass, mock_config_entry)

    assert _data(mock_config_entry).online
    assert state_of(hass, key) == STATE_UNKNOWN
    # The other counters are not affected.
    others = [k for k in CUMULATIVE_KEYS if k != key]
    assert all(float(state_of(hass, k)) > 0 for k in others)

    # Once the inverter reports a real value, it is taken over.
    bridge.read.return_value = make_inverter_data()
    await refresh(hass, mock_config_entry)
    assert float(state_of(hass, key)) == pytest.approx(REAL_SPOT[field])


@pytest.mark.parametrize(("key", "field"), CUMULATIVE)
async def test_online_higher_counter_is_taken_over(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    key: str,
    field: str,
) -> None:
    await setup_entry(hass, mock_config_entry)

    bridge.read.return_value = make_inverter_data(**{field: REAL_SPOT[field] + 1.5})
    await refresh(hass, mock_config_entry)
    assert float(state_of(hass, key)) == pytest.approx(REAL_SPOT[field] + 1.5)

    # An equal value is fine, too (the counter did not move).
    await refresh(hass, mock_config_entry)
    assert float(state_of(hass, key)) == pytest.approx(REAL_SPOT[field] + 1.5)


async def test_counters_never_report_zero_or_go_down_over_a_whole_day(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Night, glitches and recoveries in one go: every state is a valid counter."""
    sun_is_up.return_value = False  # Home Assistant starts at night
    await setup_entry(hass, mock_config_entry)
    seen: dict[str, list[str]] = {key: [] for key in CUMULATIVE_KEYS}

    def record() -> None:
        for key in CUMULATIVE_KEYS:
            seen[key].append(state_of(hass, key))

    record()  # nothing known yet
    sun_is_up.return_value = True
    steps: list[Any] = [
        make_inverter_data(e_total=100.0, operating_time=10.0, feed_in_time=9.0),
        InverterUnreachableError("asleep"),
        make_inverter_data(e_total=0.0, operating_time=0.0, feed_in_time=0.0),
        make_inverter_data(e_total=None, operating_time=None, feed_in_time=None),
        make_inverter_data(e_total=101.0, operating_time=10.5, feed_in_time=9.4),
        BridgeUnreachableError("refused"),
        make_inverter_data(e_total=50.0, operating_time=1.0, feed_in_time=1.0),
        make_inverter_data(e_total=102.0, operating_time=11.0, feed_in_time=9.9),
    ]
    for step in steps:
        if isinstance(step, Exception):
            bridge.read.side_effect, bridge.read.return_value = step, None
        else:
            bridge.read.side_effect, bridge.read.return_value = None, step
        await refresh(hass, mock_config_entry)
        record()

    for key, states in seen.items():
        assert states[0] == STATE_UNKNOWN, key
        numbers = [float(state) for state in states[1:]]
        assert all(value > 0 for value in numbers), (key, states)
        assert numbers == sorted(numbers), (key, states)
    assert [float(s) for s in seen["energy_total"][1:]] == [
        100.0,
        100.0,
        100.0,
        100.0,
        101.0,
        101.0,
        101.0,
        102.0,
    ]


async def test_no_counter_is_known_before_the_first_online_poll(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Nothing preserved: unknown, never a made-up 0."""
    sun_is_up.return_value = False

    await setup_entry(hass, mock_config_entry)

    for key in (*CUMULATIVE_KEYS, "energy_today"):
        assert state_of(hass, key) == STATE_UNKNOWN, key


# ------------------------------------------------------------- energy today


@pytest.fixture
async def amsterdam(hass: HomeAssistant) -> None:
    """Local midnight is 22:00 UTC in June: it is not UTC midnight."""
    await hass.config.async_set_time_zone("Europe/Amsterdam")


async def test_energy_today_is_kept_until_local_midnight_then_zero(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    amsterdam: None,
    mock_config_entry: MockConfigEntry,
) -> None:
    freezer.move_to("2026-06-21 10:00:00+00:00")  # 12:00 local
    await setup_entry(hass, mock_config_entry)
    assert number(hass, "energy_today") == pytest.approx(4.883)

    bridge.read.side_effect = InverterUnreachableError("asleep")

    freezer.move_to("2026-06-21 21:59:00+00:00")  # 23:59 local: still the 21st
    await refresh(hass, mock_config_entry)
    assert number(hass, "energy_today") == pytest.approx(4.883)

    freezer.move_to("2026-06-21 22:01:00+00:00")  # 00:01 local on the 22nd
    await refresh(hass, mock_config_entry)
    assert number(hass, "energy_today") == 0

    # Still 0 later that night, and the totals are untouched.
    freezer.move_to("2026-06-22 02:00:00+00:00")
    await refresh(hass, mock_config_entry)
    assert number(hass, "energy_today") == 0
    assert number(hass, "energy_total") == pytest.approx(60854.831)


async def test_energy_today_follows_the_inverter_the_next_morning(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    amsterdam: None,
    mock_config_entry: MockConfigEntry,
) -> None:
    freezer.move_to("2026-06-21 10:00:00+00:00")
    await setup_entry(hass, mock_config_entry)

    freezer.move_to("2026-06-22 04:00:00+00:00")  # 06:00 local, sun is up
    bridge.read.return_value = make_inverter_data(e_today=0.2)
    await refresh(hass, mock_config_entry)

    assert number(hass, "energy_today") == pytest.approx(0.2)


async def test_online_without_energy_today_keeps_todays_value_then_resets(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    amsterdam: None,
    mock_config_entry: MockConfigEntry,
) -> None:
    freezer.move_to("2026-06-21 10:00:00+00:00")
    await setup_entry(hass, mock_config_entry)

    bridge.read.return_value = make_inverter_data(e_today=None)
    await refresh(hass, mock_config_entry)
    assert number(hass, "energy_today") == pytest.approx(4.883)

    freezer.move_to("2026-06-22 04:00:00+00:00")  # next day, inverter still None
    await refresh(hass, mock_config_entry)
    assert number(hass, "energy_today") == 0


# ------------------------------------------------- persistence (restart, Store)


async def _restart(
    hass: HomeAssistant, entry: MockConfigEntry, sun_is_up: Any, *, night: bool
) -> None:
    """Unload and set the entry up again with a fresh coordinator."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    sun_is_up.return_value = not night
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_the_preserved_values_are_stored(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    amsterdam: None,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    freezer.move_to("2026-06-21 10:00:00+00:00")
    await setup_entry(hass, mock_config_entry)

    stored = hass_storage[storage_key(mock_config_entry.entry_id)]["data"]
    assert stored["e_total"] == pytest.approx(60854.831)
    assert stored["operating_time"] == pytest.approx(58826.405)
    assert stored["feed_in_time"] == pytest.approx(57238.146)
    assert stored["e_today"] == pytest.approx(4.883)
    assert stored["e_today_date"] == "2026-06-21"  # the local date
    assert stored["info"]["serial"] == 2100000042
    assert stored["info"]["device_type"] == "SB 3000TL-20"


async def test_preserved_values_survive_a_restart_at_night(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.reset_mock()

    await _restart(hass, mock_config_entry, sun_is_up, night=True)

    bridge.read.assert_not_awaited()
    assert not _data(mock_config_entry).online
    assert number(hass, "energy_total") == pytest.approx(60854.831)
    assert number(hass, "operating_time") == pytest.approx(58826.405)
    assert number(hass, "feed_in_time") == pytest.approx(57238.146)
    assert number(hass, "energy_today") == pytest.approx(4.883)
    assert number(hass, "power") == 0
    # The device is complete even though the inverter was not read.
    (device,) = dr.async_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert device.model == "SB 3000TL-20"
    assert device.sw_version == "03.20.01.R"


@pytest.mark.parametrize("error", OFFLINE_ERRORS)
async def test_preserved_values_survive_a_restart_with_the_inverter_asleep(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.side_effect = error

    await _restart(hass, mock_config_entry, sun_is_up, night=False)

    assert not _data(mock_config_entry).online
    for key in CUMULATIVE_KEYS:
        assert float(state_of(hass, key)) > 0, key
    assert number(hass, "energy_total") == pytest.approx(60854.831)


async def test_a_restart_never_lowers_the_counters(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)

    # After the restart the inverter answers with a lower / empty counter.
    bridge.read.return_value = make_inverter_data(
        e_total=5.0, operating_time=None, feed_in_time=0.0
    )
    await _restart(hass, mock_config_entry, sun_is_up, night=False)

    assert _data(mock_config_entry).online
    assert number(hass, "energy_total") == pytest.approx(60854.831)
    assert number(hass, "operating_time") == pytest.approx(58826.405)
    assert number(hass, "feed_in_time") == pytest.approx(57238.146)


async def test_the_stored_format_is_read_at_night(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    freezer: FrozenDateTimeFactory,
    amsterdam: None,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Pins the on-disk format: what a released version wrote must stay readable."""
    freezer.move_to("2026-06-21 20:00:00+00:00")  # 22:00 local on the 21st
    key = storage_key(mock_config_entry.entry_id)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "info": {
                "serial": 2100000042,
                "susy_id": 113,
                "bt_address": "00:80:25:00:00:01",
                "name": "SN: 2100000042",
                "device_type": "SB 3000TL-20",
                "device_class": "Solar Inverters",
                "sw_version": "03.20.01.R",
            },
            "e_total": 60854.831,
            "operating_time": 58826.405,
            "feed_in_time": 57238.146,
            "e_today": 4.883,
            "e_today_date": "2026-06-21",
        },
    }
    sun_is_up.return_value = False

    await setup_entry(hass, mock_config_entry)

    bridge.read.assert_not_awaited()
    assert number(hass, "energy_total") == pytest.approx(60854.831)
    assert number(hass, "operating_time") == pytest.approx(58826.405)
    assert number(hass, "feed_in_time") == pytest.approx(57238.146)
    assert number(hass, "energy_today") == pytest.approx(4.883)
    (device,) = dr.async_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert device.model == "SB 3000TL-20"


async def test_a_stored_energy_today_of_yesterday_reads_zero_at_startup(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-06-22 12:00:00+00:00")
    key = storage_key(mock_config_entry.entry_id)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "e_total": 60854.831,
            "e_today": 4.883,
            "e_today_date": "2026-06-20",
        },
    }
    sun_is_up.return_value = False

    await setup_entry(hass, mock_config_entry)

    assert number(hass, "energy_today") == 0
    assert number(hass, "energy_total") == pytest.approx(60854.831)
    # No stored device info: the device still exists, from the config entry.
    assert state_of(hass, "inverter_online", "binary_sensor") == "off"


@pytest.mark.parametrize(
    "garbage",
    [
        {"info": "nope", "e_total": "lots", "e_today": [], "e_today_date": 5},
        {"info": {"bogus": 1}, "e_total": -5, "operating_time": 0, "e_today": 1.0},
        {"e_total": float("nan"), "feed_in_time": True, "e_today_date": "x"},
        [],
    ],
    ids=["wrong_types", "bad_values", "nan_and_bool", "not_a_dict"],
)
async def test_unreadable_stored_values_are_ignored(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
    garbage: Any,
) -> None:
    key = storage_key(mock_config_entry.entry_id)
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": garbage}
    sun_is_up.return_value = False

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    for key_ in CUMULATIVE_KEYS:
        assert state_of(hass, key_) == STATE_UNKNOWN, key_


async def test_the_store_is_only_written_when_something_changed(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    await setup_entry(hass, mock_config_entry)
    key = storage_key(mock_config_entry.entry_id)
    first = hass_storage[key]

    await refresh(hass, mock_config_entry)  # identical poll
    assert hass_storage[key] is first

    bridge.read.return_value = make_inverter_data(e_total=60855.0)
    await refresh(hass, mock_config_entry)
    assert hass_storage[key]["data"]["e_total"] == 60855.0


# ------------------------------------------------------ errors that are not night


async def test_a_wrong_password_starts_reauth(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    bridge.read.side_effect = SmaAuthError("rejected")
    await refresh(hass, mock_config_entry)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    assert flows[0]["context"]["entry_id"] == mock_config_entry.entry_id
    assert mock_config_entry.runtime_data.last_update_success is False


async def test_unexpected_data_makes_the_entities_unavailable(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    assert number(hass, "power") == 622

    bridge.read.side_effect = SmaProtocolError("garbage")
    await refresh(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.last_update_success is False
    assert state_of(hass, "power") == STATE_UNAVAILABLE
    assert state_of(hass, "energy_total") == STATE_UNAVAILABLE
    assert state_of(hass, "inverter_online", "binary_sensor") == STATE_UNAVAILABLE
    # A protocol error is not a reauth case.
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    # ... and they come back with the next good read.
    bridge.read.side_effect = None
    await refresh(hass, mock_config_entry)
    assert number(hass, "power") == 622
    assert number(hass, "energy_total") == pytest.approx(60854.831)


async def test_unexpected_exceptions_do_not_look_like_night(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    bridge.read.side_effect = RuntimeError("bug")
    await refresh(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.last_update_success is False
    assert state_of(hass, "power") == STATE_UNAVAILABLE


# -------------------------------------------------------------------- logging


def _coordinator_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == COORDINATOR_LOGGER]


async def test_an_unreachable_bridge_is_warned_about_once_until_it_answers(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, mock_config_entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.side_effect = BridgeUnreachableError("connection refused")

    for _ in range(4):
        await refresh(hass, mock_config_entry)

    warnings = [r for r in _coordinator_records(caplog) if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "192.168.1.50:9900" in warnings[0].getMessage()
    assert "connection refused" in warnings[0].getMessage()
    assert any(
        "still unreachable" in r.getMessage() and r.levelno == logging.DEBUG
        for r in _coordinator_records(caplog)
    )

    # The bridge answers again ...
    caplog.clear()
    bridge.read.side_effect = None
    await refresh(hass, mock_config_entry)
    assert [
        r.getMessage()
        for r in _coordinator_records(caplog)
        if r.levelno == logging.INFO
    ] == ["The SMA Bluetooth bridge is reachable again"]

    # ... so the next outage is reported again.
    caplog.clear()
    bridge.read.side_effect = BridgeUnreachableError("timed out")
    await refresh(hass, mock_config_entry)
    await refresh(hass, mock_config_entry)
    warnings = [r for r in _coordinator_records(caplog) if r.levelno >= logging.WARNING]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(InverterUnreachableError("asleep"), id="inverter_asleep"),
        pytest.param(SmaTimeoutError("no answer"), id="timeout"),
        pytest.param(BridgeBusyError("busy"), id="bridge_busy"),
        pytest.param(SmaConnectionError("dropped"), id="connection_dropped"),
    ],
)
async def test_other_offline_errors_are_never_warnings(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    await setup_entry(hass, mock_config_entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()
    bridge.read.side_effect = error

    for _ in range(3):
        await refresh(hass, mock_config_entry)

    records = _coordinator_records(caplog)
    assert records, "the offline poll should be logged at DEBUG"
    assert all(r.levelno == logging.DEBUG for r in records)


async def test_a_bridge_that_answers_resets_the_unreachable_warning(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bridge answered (the inverter is asleep): a later outage is a new one."""
    await setup_entry(hass, mock_config_entry)
    bridge.read.side_effect = BridgeUnreachableError("refused")
    await refresh(hass, mock_config_entry)
    caplog.clear()

    bridge.read.side_effect = InverterUnreachableError("asleep")
    await refresh(hass, mock_config_entry)
    bridge.read.side_effect = BridgeUnreachableError("refused again")
    await refresh(hass, mock_config_entry)

    warnings = [r for r in _coordinator_records(caplog) if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
