"""The inverter refuses the logon although the password is right.

Real hardware: the bridge dropped the final logoff of every poll, so the inverter
kept ~15 sessions open (each logon asks for a 900 s timeout) and then answered
every logon with L2 error code 1.  That is *not* a wrong password:

* no re-authentication flow, no ``ConfigEntryAuthFailed``;
* the entities are unavailable (``UpdateFailed``) - never offline zeros;
* the preserved ``TOTAL_INCREASING`` values are not touched;
* the integration stays away for 15 minutes (the session timeout): polls inside
  the back-off raise ``UpdateFailed`` again *without* opening a connection;
* one WARNING per refusal episode, one INFO when a logon works again;
* the night skip keeps priority: at night the data is offline data, no connection.
"""

from __future__ import annotations

import copy
import logging
from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sbfspot.const import (
    CONF_BT_ADDRESS,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    DOMAIN,
    LOGON_REFUSED_BACKOFF,
)
from custom_components.sbfspot.coordinator import storage_key
from custom_components.sbfspot.smabt import (
    InverterUnreachableError,
    SmaAuthError,
    SmaLogonRefusedError,
)

from .conftest import (
    BRIDGE_HOST,
    BT_ADDRESS,
    PASSWORD,
    REAL_SPOT,
    SERIAL,
    TITLE,
    Bridge,
    make_inverter_data,
    number,
    refresh,
    setup_entry,
    state_of,
)

COORDINATOR_LOGGER = "custom_components.sbfspot.coordinator"
CUMULATIVE_KEYS = ["energy_total", "operating_time", "feed_in_time"]
EVERY_ENTITY = [
    ("power", "sensor"),
    ("energy_today", "sensor"),
    ("energy_total", "sensor"),
    ("operating_time", "sensor"),
    ("feed_in_time", "sensor"),
    ("inverter_online", "binary_sensor"),
]


def refused(code: int = 1) -> SmaLogonRefusedError:
    return SmaLogonRefusedError(code)


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == COORDINATOR_LOGGER]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    # Exactly WARNING: Home Assistant itself logs one ERROR when an update first
    # fails ("Error fetching ... data"), under the coordinator's logger.
    return [r for r in _records(caplog) if r.levelno == logging.WARNING]


def _infos(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    # Home Assistant logs its own INFO ("Fetching ... data recovered") under the
    # coordinator's logger; only the integration's logon message counts here.
    return [
        r
        for r in _records(caplog)
        if r.levelno == logging.INFO and "logon" in r.getMessage()
    ]


def _assert_unavailable(hass: HomeAssistant) -> None:
    for key, platform in EVERY_ENTITY:
        assert state_of(hass, key, platform) == STATE_UNAVAILABLE, key


async def _connects(bridge: Bridge, hass: HomeAssistant, entry: Any) -> bool:
    """Poll once; was the inverter read (a connection attempt made)?"""
    before = bridge.read.await_count
    transports = bridge.transport.call_count
    await refresh(hass, entry)
    attempted = bridge.read.await_count > before
    assert (bridge.transport.call_count > transports) is attempted
    return attempted


@pytest.fixture
async def refusing(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> MockConfigEntry:
    """A set up entry whose inverter has just refused a logon."""
    await setup_entry(hass, mock_config_entry)
    assert number(hass, "power") == 622
    bridge.read.side_effect = refused()
    await refresh(hass, mock_config_entry)
    assert mock_config_entry.runtime_data.last_update_success is False
    return mock_config_entry


# ------------------------------------------------------------------ the poll


def test_the_backoff_is_the_session_timeout() -> None:
    assert LOGON_REFUSED_BACKOFF == timedelta(seconds=900)


async def test_a_refused_logon_does_not_start_reauth(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    bridge.read.side_effect = refused(1)
    await refresh(hass, mock_config_entry)

    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    coordinator = mock_config_entry.runtime_data
    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert "code 1" in str(coordinator.last_exception)


async def test_a_refused_logon_makes_every_entity_unavailable_not_offline(
    hass: HomeAssistant, refusing: MockConfigEntry
) -> None:
    """Offline would show 0 W; unavailable says "we do not know"."""
    _assert_unavailable(hass)


async def test_a_refusal_does_not_touch_the_preserved_values(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    await setup_entry(hass, mock_config_entry)
    key = storage_key(mock_config_entry.entry_id)
    stored = copy.deepcopy(hass_storage[key])
    stored_object = hass_storage[key]

    bridge.read.side_effect = refused()
    for _ in range(3):
        await refresh(hass, mock_config_entry)
    assert hass_storage[key] is stored_object  # not even written
    assert hass_storage[key] == stored

    # The inverter takes the logon again, but reports bad counters: the preserved
    # values stay, as they do after any other gap.
    freezer.tick(LOGON_REFUSED_BACKOFF)
    bridge.read.side_effect = None
    bridge.read.return_value = make_inverter_data(
        e_total=0.0, operating_time=None, feed_in_time=5.0
    )
    await refresh(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.last_update_success is True
    assert number(hass, "energy_total") == pytest.approx(REAL_SPOT["e_total"])
    assert number(hass, "operating_time") == pytest.approx(REAL_SPOT["operating_time"])
    assert number(hass, "feed_in_time") == pytest.approx(REAL_SPOT["feed_in_time"])
    assert hass_storage[key] == stored


# ------------------------------------------------------------------ back-off


async def test_polls_inside_the_backoff_make_no_connection_attempt(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    refusing: MockConfigEntry,
) -> None:
    bridge.read.reset_mock()
    bridge.transport.reset_mock()

    for _ in range(10):  # ten minutes of 60 s polls
        freezer.tick(timedelta(seconds=60))
        assert not await _connects(bridge, hass, refusing)

        coordinator = refusing.runtime_data
        assert coordinator.last_update_success is False
        assert isinstance(coordinator.last_exception, UpdateFailed)

    bridge.read.assert_not_awaited()
    bridge.transport.assert_not_called()
    _assert_unavailable(hass)
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_the_backoff_ends_exactly_after_15_minutes(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    refusing: MockConfigEntry,
) -> None:
    freezer.tick(timedelta(seconds=899))
    assert not await _connects(bridge, hass, refusing)

    freezer.tick(timedelta(seconds=1))
    bridge.read.side_effect = None  # the old sessions have timed out
    assert await _connects(bridge, hass, refusing)

    assert refusing.runtime_data.last_update_success is True
    assert refusing.runtime_data.data.online
    assert number(hass, "power") == 622
    assert number(hass, "energy_total") == pytest.approx(REAL_SPOT["e_total"])
    assert state_of(hass, "inverter_online", "binary_sensor") == "on"


async def test_a_refusal_after_the_backoff_starts_a_new_backoff(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    refusing: MockConfigEntry,
) -> None:
    freezer.tick(LOGON_REFUSED_BACKOFF)
    assert await _connects(bridge, hass, refusing)  # refused again (still set)
    assert refusing.runtime_data.last_update_success is False

    freezer.tick(timedelta(seconds=899))
    assert not await _connects(bridge, hass, refusing)
    freezer.tick(timedelta(seconds=1))
    assert await _connects(bridge, hass, refusing)


async def test_an_attempt_that_is_not_refused_ends_the_backoff(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    refusing: MockConfigEntry,
) -> None:
    """After the back-off the inverter may just be asleep: that is offline data and
    the next poll connects again without another wait."""
    freezer.tick(LOGON_REFUSED_BACKOFF)
    bridge.read.side_effect = InverterUnreachableError("asleep")
    assert await _connects(bridge, hass, refusing)
    assert refusing.runtime_data.last_update_success is True
    assert not refusing.runtime_data.data.online

    freezer.tick(timedelta(seconds=60))
    bridge.read.side_effect = None
    assert await _connects(bridge, hass, refusing)
    assert refusing.runtime_data.data.online


async def test_a_wrong_password_after_a_refusal_still_starts_reauth(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    refusing: MockConfigEntry,
) -> None:
    freezer.tick(LOGON_REFUSED_BACKOFF)
    bridge.read.side_effect = SmaAuthError("rejected")

    await refresh(hass, refusing)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_a_refusal_during_setup_retries_setup_without_reauth(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge.read.side_effect = refused()

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(_warnings(caplog)) == 1


# --------------------------------------- the back-off survives a new coordinator
#
# Home Assistant builds a new coordinator when it retries a setup that raised
# ``ConfigEntryNotReady`` and when an entry reloads.  The deadline therefore lives
# in ``hass.data`` (per entry id), not on the coordinator.


async def _retry_setup(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float = 90
) -> None:
    """Let the time pass and fire the setup retry that Home Assistant scheduled.

    Home Assistant runs the retried setup as a *background* task, which
    ``async_block_till_done()`` does not wait for by default: the test would race
    the retry (platform forwarding included) and see ``SETUP_IN_PROGRESS``.
    """
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_retries_stay_away_from_the_inverter_for_15_minutes(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge.read.side_effect = refused()
    await setup_entry(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert bridge.read.await_count == 1
    bridge.read.reset_mock()
    bridge.transport.reset_mock()

    # 12 minutes of setup retries.  Home Assistant retries after 5, 10, 20, ... 160,
    # 320 s: each one is turned away without a connection.
    for _ in range(8):
        await _retry_setup(hass, freezer)
        assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    bridge.read.assert_not_awaited()
    bridge.transport.assert_not_called()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    # The first retry after the 15 minutes connects, the logon works, it loads.
    bridge.read.side_effect = None
    await _retry_setup(hass, freezer, seconds=600)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert bridge.read.await_count == 1
    assert number(hass, "power") == 622
    assert len(_warnings(caplog)) == 1  # one episode: one WARNING
    assert len(_infos(caplog)) == 1


async def test_the_backoff_survives_a_reload(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    refusing: MockConfigEntry,
) -> None:
    bridge.read.reset_mock()
    bridge.transport.reset_mock()
    freezer.tick(timedelta(seconds=60))

    await hass.config_entries.async_reload(refusing.entry_id)
    await hass.async_block_till_done()

    assert refusing.state is ConfigEntryState.SETUP_RETRY
    bridge.read.assert_not_awaited()
    bridge.transport.assert_not_called()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    # Reloading again, or retrying, changes nothing until the 15 minutes are over.
    await hass.config_entries.async_reload(refusing.entry_id)
    await _retry_setup(hass, freezer)
    bridge.read.assert_not_awaited()

    bridge.read.side_effect = None
    await _retry_setup(hass, freezer, seconds=900)
    assert refusing.state is ConfigEntryState.LOADED
    assert number(hass, "power") == 622
    assert number(hass, "energy_total") == pytest.approx(REAL_SPOT["e_total"])


async def test_the_backoff_is_kept_per_entry(
    hass: HomeAssistant, bridge: Bridge, refusing: MockConfigEntry
) -> None:
    """Another inverter behind another entry has its own sessions and its own wait."""
    other = MockConfigEntry(
        domain=DOMAIN,
        title="SB 3000TL-20 2100000043",
        unique_id="2100000043",
        data={**refusing.data, CONF_BT_ADDRESS: "00:80:25:00:00:02"},
    )
    bridge.read.side_effect = None
    bridge.read.reset_mock()

    await setup_entry(hass, other)

    assert other.state is ConfigEntryState.LOADED
    assert bridge.read.await_count == 1


async def test_removing_the_entry_forgets_the_backoff(
    hass: HomeAssistant,
    bridge: Bridge,
    refusing: MockConfigEntry,
) -> None:
    entry_id = refusing.entry_id
    assert entry_id in hass.data[DOMAIN]

    await hass.config_entries.async_remove(entry_id)
    await hass.async_block_till_done()

    assert entry_id not in hass.data.get(DOMAIN, {})
    # Behaviour, not just bookkeeping: an entry with the same id connects at once.
    again = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=str(SERIAL),
        entry_id=entry_id,
        data=dict(refusing.data),
    )
    bridge.read.side_effect = None
    bridge.read.reset_mock()
    await setup_entry(hass, again)
    assert again.state is ConfigEntryState.LOADED
    assert bridge.read.await_count == 1


async def test_the_night_skip_wins_when_setup_is_retried_inside_the_backoff(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    freezer: FrozenDateTimeFactory,
    refusing: MockConfigEntry,
) -> None:
    bridge.read.reset_mock()
    sun_is_up.return_value = False
    freezer.tick(timedelta(seconds=60))

    await hass.config_entries.async_reload(refusing.entry_id)
    await hass.async_block_till_done()

    # Night: offline data and a loaded entry, as every night - no connection.
    assert refusing.state is ConfigEntryState.LOADED
    assert not refusing.runtime_data.data.online
    assert number(hass, "power") == 0
    bridge.read.assert_not_awaited()


# --------------------------------------------------------------------- night


async def test_the_night_skip_wins_over_the_backoff(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    freezer: FrozenDateTimeFactory,
    refusing: MockConfigEntry,
) -> None:
    """Sun down during the back-off: offline data and no connection, as every night
    - the entities come back from unavailable to the preserved night values."""
    bridge.read.reset_mock()
    bridge.transport.reset_mock()
    sun_is_up.return_value = False
    freezer.tick(timedelta(seconds=60))

    await refresh(hass, refusing)

    coordinator = refusing.runtime_data
    assert coordinator.last_update_success is True
    assert not coordinator.data.online
    assert number(hass, "power") == 0
    assert number(hass, "energy_total") == pytest.approx(REAL_SPOT["e_total"])
    assert state_of(hass, "inverter_online", "binary_sensor") == "off"
    bridge.read.assert_not_awaited()
    bridge.transport.assert_not_called()

    # The sun is up again, 5 minutes into the back-off: still no connection ...
    sun_is_up.return_value = True
    freezer.tick(timedelta(seconds=240))
    assert not await _connects(bridge, hass, refusing)
    assert coordinator.last_update_success is False
    _assert_unavailable(hass)

    # ... until the 15 minutes are over.
    freezer.tick(timedelta(seconds=600))
    bridge.read.side_effect = None
    assert await _connects(bridge, hass, refusing)
    assert number(hass, "power") == 622


async def test_a_night_poll_without_a_refusal_is_unchanged(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    sun_is_up.return_value = False
    bridge.read.reset_mock()

    await refresh(hass, mock_config_entry)

    bridge.read.assert_not_awaited()
    assert mock_config_entry.runtime_data.last_update_success is True
    assert number(hass, "power") == 0


# ------------------------------------------------------------------- logging


async def test_one_warning_per_refusal_episode_and_one_info_when_it_ends(
    hass: HomeAssistant,
    bridge: Bridge,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, mock_config_entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()

    # Episode 1: refused, polls inside the back-off, refused again after it.
    bridge.read.side_effect = refused(1)
    await refresh(hass, mock_config_entry)
    for _ in range(5):
        freezer.tick(timedelta(seconds=60))
        await refresh(hass, mock_config_entry)
    freezer.tick(LOGON_REFUSED_BACKOFF)
    await refresh(hass, mock_config_entry)  # refused again: same episode

    (warning,) = _warnings(caplog)
    text = warning.getMessage()
    assert warning.levelno == logging.WARNING
    assert "refused the logon" in text
    assert "code 1" in text
    assert "sessions" in text
    assert "15 minutes" in text
    assert _infos(caplog) == []

    # The logon works again: one INFO.
    caplog.clear()
    freezer.tick(LOGON_REFUSED_BACKOFF)
    bridge.read.side_effect = None
    await refresh(hass, mock_config_entry)
    await refresh(hass, mock_config_entry)
    (info,) = _infos(caplog)
    assert "logon" in info.getMessage()
    assert _warnings(caplog) == []

    # Episode 2 is reported again, with its own code.
    caplog.clear()
    bridge.read.side_effect = refused(7)
    await refresh(hass, mock_config_entry)
    (warning,) = _warnings(caplog)
    assert "code 7" in warning.getMessage()


async def test_other_polls_never_log_the_refusal_messages(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, mock_config_entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.sbfspot")
    caplog.clear()

    for _ in range(3):
        await refresh(hass, mock_config_entry)

    assert _warnings(caplog) == []
    assert _infos(caplog) == []


# --------------------------------------------------------------- config flow

USER_INPUT = {
    CONF_HOST: BRIDGE_HOST,
    CONF_PORT: 9900,
    CONF_BT_ADDRESS: BT_ADDRESS,
    CONF_PASSWORD: PASSWORD,
}
RECONFIGURE_INPUT = {
    CONF_HOST: "192.168.1.60",
    CONF_PORT: 9901,
    CONF_BT_ADDRESS: "00:80:25:00:00:02",
}


async def test_user_flow_reports_a_refused_logon(
    hass: HomeAssistant, bridge: Bridge
) -> None:
    bridge.read.side_effect = refused()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "logon_refused"}
    assert hass.config_entries.async_entries(DOMAIN) == []

    # Once the inverter takes the logon, the same form goes through.
    bridge.read.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_reconfigure_flow_reports_a_refused_logon(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.side_effect = refused()

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], RECONFIGURE_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "logon_refused"}
    assert mock_config_entry.data[CONF_HOST] == BRIDGE_HOST


async def test_reauth_flow_reports_a_refused_logon_and_keeps_the_password(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.side_effect = refused()

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "maybe-right"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "logon_refused"}
    assert mock_config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_a_wrong_password_in_the_flow_is_still_invalid_auth(
    hass: HomeAssistant, bridge: Bridge
) -> None:
    bridge.read.side_effect = SmaAuthError("rejected")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["errors"] == {"base": "invalid_auth"}
