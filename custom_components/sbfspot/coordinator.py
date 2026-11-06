"""DataUpdateCoordinator for the SBFspot (SMA Bluetooth) integration.

Night is normal: the inverter's Bluetooth radio sleeps without sunlight.  The
coordinator therefore

* does not open a connection outside sunrise - 15 min .. sunset + 15 min, and
  reports *offline* data then (power and current 0: a real 0 W - this is the only
  place that reports 0),
* keeps the cumulative counters (``e_total``, ``operating_time``,
  ``feed_in_time``) at their last valid value, persisted with ``Store``, so a
  ``TOTAL_INCREASING`` sensor never reports 0 or a lower value (that would
  corrupt the long-term statistics), and
* resets ``e_today`` to 0 at local midnight.

A daytime poll that fails with a connection error or a protocol error is a *missed
poll*.  A missed sample is never shown as 0:

* The first ``MAX_MISSED_POLLS`` misses in a row repeat the last online data, as long
  as that success is recent (within ``(MAX_MISSED_POLLS + 1)`` scan intervals).
  Nothing is stored and no counter changes.  The night skip drops the held data, so
  a morning never shows yesterday's values.
* After that a connection error gives *unknown* data: every instantaneous value is
  ``None``, ``inverter_online`` is off, the counters keep their preserved values.  A
  protocol error raises ``UpdateFailed`` (the entities are unavailable).
* One success resets the count.

One poll is one ``asyncio.timeout`` budget (scan interval - 2 s, at least 20 s) that
covers the wait for the bridge, the attempts and the pause between them.  A failed
attempt is retried once, after ``RETRY_DELAY``, when at least
``RETRY_MIN_BUDGET_LEFT`` seconds of the budget are left.  A poll that uses up its
budget is cancelled (the client logs off and closes the transport) and is a miss;
if the budget ends during the retry, the miss is the first attempt's error.
Every read holds the lock of its bridge (see ``bridge_lock``), so reads never
overlap.

A *refused logon* (``SmaLogonRefusedError``, e.g. code 1 when the inverter holds
too many open sessions) and a *wrong password* (``SmaAuthError``) are not misses.
A refusal makes the entities unavailable (``UpdateFailed``, never offline zeros,
the preserved values are not touched), starts no re-authentication, and keeps the
integration away for ``LOGON_REFUSED_BACKOFF`` (the 900 s session timeout) so the
old sessions can expire.  The night skip keeps priority over the back-off.  The
deadline lives in ``hass.data``, per config entry: Home Assistant builds a new
coordinator when it retries a failed setup and when an entry reloads.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import sun
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import UNDEFINED
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .bridge_lock import hold_bridge
from .const import (
    CONF_BT_ADDRESS,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CUMULATIVE_FIELDS,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_PASSWORD,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EXHAUSTION_NEAR_SUN,
    LOGON_REFUSED_BACKOFF,
    MAX_MISSED_POLLS,
    MIN_POLL_BUDGET,
    POLL_BUDGET_MARGIN,
    RETRY_DELAY,
    RETRY_MIN_BUDGET_LEFT,
    STORAGE_VERSION,
    SUN_MARGIN,
    ZERO_WHEN_OFFLINE_FIELDS,
)
from .smabt import (
    BridgeBusyError,
    BridgeTransport,
    BridgeUnreachableError,
    DeviceInfo,
    InverterData,
    SmaAuthError,
    SmaConnectionError,
    SmaError,
    SmaLogonRefusedError,
    SmaProtocolError,
    SmaTimeoutError,
    SpotData,
    async_read_inverter,
)

_LOGGER = logging.getLogger(__name__)

type SbfspotConfigEntry = ConfigEntry[SbfspotCoordinator]

_LOGON_REFUSALS: HassKey[dict[str, datetime]] = HassKey(DOMAIN)
"""``hass.data[DOMAIN]``: entry id -> UTC time the back-off after a refused logon
ends.  An entry stays in it until a logon works again (the refusal *episode*), so
the WARNING and the INFO are logged once per episode, also across coordinators."""


def _logon_refusals(hass: HomeAssistant) -> dict[str, datetime]:
    return hass.data.setdefault(_LOGON_REFUSALS, {})


def clear_logon_refusal(hass: HomeAssistant, entry_id: str) -> None:
    """Forget the refusal episode of a removed config entry."""
    _logon_refusals(hass).pop(entry_id, None)


def storage_key(entry_id: str) -> str:
    """The ``Store`` key that holds the preserved values of one config entry."""
    return f"{DOMAIN}.{entry_id}"


async def async_remove_stored_data(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the preserved values of a removed config entry."""
    await Store(hass, STORAGE_VERSION, storage_key(entry_id)).async_remove()


@dataclass(frozen=True, slots=True)
class SbfspotData:
    """What the entities show.

    ``spot`` already follows the offline rules (preserved cumulative values,
    zero power at night, unknown values after missed polls, ...); ``info`` is the
    last known device info (from the inverter or the store), ``None`` only if it
    was never read.
    """

    online: bool
    info: DeviceInfo | None
    spot: SpotData


@dataclass(slots=True)
class PollStatistics:
    """What the polls did since the coordinator was created (in memory only).

    ``polls`` counts the polls that tried to read the inverter: the night skip, the
    logon back-off and a skipped re-entrant refresh are not polls.  A poll ends as a
    success, a miss, or a failure that is neither (wrong password, refused logon,
    unexpected exception), so ``polls`` can be larger than the sum of the first two.
    """

    polls: int = 0
    successes: int = 0
    successes_after_retry: int = 0
    misses_by_exception: dict[str, int] = field(default_factory=dict)
    grace_exhaustions: int = 0
    skipped_reentrant_refreshes: int = 0
    max_poll_duration: float = 0.0
    last_poll_duration: float | None = None
    last_success: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """The statistics as JSON-compatible data."""
        return {
            "polls": self.polls,
            "successes": self.successes,
            "successes_after_retry": self.successes_after_retry,
            "misses": sum(self.misses_by_exception.values()),
            "misses_by_exception": dict(sorted(self.misses_by_exception.items())),
            "grace_exhaustions": self.grace_exhaustions,
            "skipped_reentrant_refreshes": self.skipped_reentrant_refreshes,
            "max_poll_duration_seconds": round(self.max_poll_duration, 3),
            "last_poll_duration_seconds": (
                None
                if self.last_poll_duration is None
                else round(self.last_poll_duration, 3)
            ),
            "last_success": (
                self.last_success.isoformat() if self.last_success else None
            ),
        }


class _Attempts:
    """What one poll did so far (the budget can end the poll at any point).

    ``first_error`` is the error of the first attempt once a retry was started;
    ``retry_cut`` says that the budget ended the poll during that retry.
    """

    __slots__ = ("count", "first_error", "retry_cut")

    def __init__(self) -> None:
        self.count = 0
        self.first_error: SmaConnectionError | None = None
        self.retry_cut = False


def _finite(value: object) -> float | None:
    """*value* as a float if it is a finite number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _polls(count: int) -> str:
    return f"{count} poll{'' if count == 1 else 's'}"


class SbfspotCoordinator(DataUpdateCoordinator[SbfspotData]):
    """Polls one SMA inverter through its ESP32 Bluetooth bridge."""

    config_entry: SbfspotConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: SbfspotConfigEntry) -> None:
        """Create the coordinator (the store is loaded on the first refresh)."""
        interval = int(
            config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(config_entry.entry_id)
        )
        self._info: DeviceInfo | None = None
        self._counters: dict[str, float] = {}
        self._e_today: float | None = None
        self._e_today_date: date | None = None
        self._bridge_unreachable_logged = False

        self._poll_budget = max(MIN_POLL_BUDGET, interval - POLL_BUDGET_MARGIN)
        self._grace_window = timedelta(seconds=(MAX_MISSED_POLLS + 1) * interval)
        self._polling = False
        self._missed = 0
        self._held: SbfspotData | None = None
        self._held_at: datetime | None = None
        self._exhaustion_logged = False
        self.statistics = PollStatistics()

    @property
    def poll_budget(self) -> float:
        """Seconds one whole poll may take: scan interval - 2 s, at least 20 s."""
        return self._poll_budget

    @property
    def missed_polls(self) -> int:
        """How many daytime polls in a row have failed (0 after a success)."""
        return self._missed

    # ------------------------------------------------------------ persistence

    async def _async_setup(self) -> None:
        """Load the preserved values before the first refresh."""
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return
        info = stored.get("info")
        if isinstance(info, dict):
            try:
                self._info = DeviceInfo(**info)
            except TypeError:
                _LOGGER.debug("Ignoring unreadable stored device info: %s", info)
        for name in CUMULATIVE_FIELDS:
            value = _finite(stored.get(name))
            if value is not None and value > 0:
                self._counters[name] = value
        e_today = _finite(stored.get("e_today"))
        if e_today is not None:
            try:
                self._e_today_date = date.fromisoformat(str(stored.get("e_today_date")))
            except ValueError:
                pass
            else:
                self._e_today = e_today
        _LOGGER.debug("Loaded preserved values: %s", self._snapshot())

    def _snapshot(self) -> dict[str, Any]:
        """The data that is persisted."""
        return {
            "info": asdict(self._info) if self._info is not None else None,
            **self._counters,
            "e_today": self._e_today,
            "e_today_date": (
                self._e_today_date.isoformat() if self._e_today_date else None
            ),
        }

    # ------------------------------------------------------------------ update

    def _sun_is_near(self, now: datetime) -> bool:
        """Is it between sunrise - margin and sunset + margin?"""
        try:
            return sun.is_up(self.hass, now - SUN_MARGIN) or sun.is_up(
                self.hass, now + SUN_MARGIN
            )
        except ValueError:
            # No sunrise or sunset within a year (polar regions): just poll.
            return True

    async def _async_update_data(self) -> SbfspotData:
        """Read the inverter, or report it offline (night) or unknown (a miss)."""
        now = dt_util.utcnow()
        if not self._sun_is_near(now):
            _LOGGER.debug("The sun is down: not polling the inverter")
            self._end_daylight_run()
            return self._offline_data()
        self._check_logon_backoff(now)

        if self._polling:
            # Home Assistant's own refresh lock keeps ``async_refresh`` and the
            # timer apart; this guards the contract if an update starts twice.
            self.statistics.skipped_reentrant_refreshes += 1
            _LOGGER.debug("A poll is already running: not reading the inverter again")
            return self.data if self.data is not None else self._unknown_data()

        self._polling = True
        try:
            return await self._poll()
        finally:
            self._polling = False

    def _end_daylight_run(self) -> None:
        """The night starts: the held data is stale and the misses are history."""
        self._held = None
        self._held_at = None
        self._missed = 0
        self._exhaustion_logged = False

    async def _poll(self) -> SbfspotData:
        """One poll: the whole of it runs under the time budget."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        attempts = _Attempts()
        statistics = self.statistics
        statistics.polls += 1
        outcome = "failed"
        try:
            try:
                result = await self._read_within_budget(attempts)
            except SmaAuthError as err:
                outcome = "wrong password"
                raise ConfigEntryAuthFailed(
                    "The inverter rejected the password"
                ) from err
            except SmaLogonRefusedError as err:
                outcome = "logon refused"
                raise self._logon_refused(err) from err
            except (SmaConnectionError, SmaProtocolError) as err:
                outcome = "missed"
                return self._missed_poll(
                    err, loop.time() - started, retry_cut=attempts.retry_cut
                )
            except SmaError as err:
                raise UpdateFailed(f"Unexpected data from the inverter: {err}") from err
            outcome = "success"
            return await self._poll_succeeded(result, attempts.count)
        finally:
            duration = loop.time() - started
            statistics.last_poll_duration = duration
            statistics.max_poll_duration = max(statistics.max_poll_duration, duration)
            _LOGGER.debug(
                "Poll ended: %s after %.1f s, %d attempt(s)",
                outcome,
                duration,
                attempts.count,
            )

    async def _read_within_budget(self, attempts: _Attempts) -> InverterData:
        """Hold the bridge and read the inverter, all within the poll budget.

        The wait for the bridge counts against the budget.  When the budget ends,
        the read is cancelled (the client logs off and closes the transport) and
        ``SmaTimeoutError`` is raised.  If it ended during the retry of a failed
        first attempt, the error of that first attempt is raised instead: it is why
        the poll failed (``attempts.retry_cut`` tells that the retry was cut).
        """
        data = self.config_entry.data
        try:
            async with asyncio.timeout(self._poll_budget) as budget:
                async with hold_bridge(
                    self.hass, data[CONF_HOST], data.get(CONF_PORT, DEFAULT_BRIDGE_PORT)
                ):
                    return await self._read_with_retry(budget, attempts)
        except TimeoutError as err:
            if not budget.expired():
                raise
            if attempts.first_error is not None:
                attempts.retry_cut = True
                raise attempts.first_error from err
            doing = (
                "reading the inverter" if attempts.count else "waiting for the bridge"
            )
            raise SmaTimeoutError(
                f"the poll used up its budget of {self._poll_budget:g} s while {doing}"
            ) from err

    async def _read_with_retry(
        self, budget: asyncio.Timeout, attempts: _Attempts
    ) -> InverterData:
        """Read the inverter; after a connection error, try once more.

        The retry waits ``RETRY_DELAY`` and needs ``RETRY_MIN_BUDGET_LEFT`` seconds
        of the poll budget; otherwise the error ends the poll.
        """
        data = self.config_entry.data
        loop = asyncio.get_running_loop()
        while True:
            attempts.count += 1
            transport = BridgeTransport(
                data[CONF_HOST],
                data.get(CONF_PORT, DEFAULT_BRIDGE_PORT),
                data[CONF_BT_ADDRESS],
            )
            try:
                return await async_read_inverter(
                    transport, data.get(CONF_PASSWORD, DEFAULT_PASSWORD)
                )
            except SmaConnectionError as err:
                deadline = budget.when()
                left = math.inf if deadline is None else deadline - loop.time()
                if attempts.count > 1 or left < RETRY_MIN_BUDGET_LEFT:
                    raise
                attempts.first_error = err
                _LOGGER.debug(
                    "Attempt %d failed (%s: %s); trying again in %g s "
                    "(%.0f s of the poll budget left)",
                    attempts.count,
                    type(err).__name__,
                    err,
                    RETRY_DELAY,
                    left,
                )
                await asyncio.sleep(RETRY_DELAY)

    async def _poll_succeeded(self, result: InverterData, attempts: int) -> SbfspotData:
        """Build the data of a good poll; the misses are over."""
        self._note_bridge_reachable()
        self._note_logon_accepted()
        data = await self._online_data(result)

        if self._missed and self._held is not None:
            _LOGGER.info("Poll succeeded again after %d missed polls", self._missed)
        self._missed = 0
        self._exhaustion_logged = False
        self._held = data
        self._held_at = dt_util.utcnow()

        statistics = self.statistics
        statistics.successes += 1
        if attempts > 1:
            statistics.successes_after_retry += 1
        statistics.last_success = self._held_at
        return data

    # ------------------------------------------------------------- missed polls

    def _can_hold(self, now: datetime) -> bool:
        """Is the last online data still good for this miss?"""
        return (
            self._held is not None
            and self._held_at is not None
            and self._missed <= MAX_MISSED_POLLS
            and now - self._held_at <= self._grace_window
        )

    def _missed_poll(
        self,
        err: SmaConnectionError | SmaProtocolError,
        duration: float,
        *,
        retry_cut: bool = False,
    ) -> SbfspotData:
        """A daytime poll failed: hold the last online data, or report the miss.

        *err* is the reason of the miss; with *retry_cut* the budget ended the poll
        during the retry of a failed first attempt, and *err* is that first error.
        """
        self._missed += 1
        name = type(err).__name__
        misses = self.statistics.misses_by_exception
        misses[name] = misses.get(name, 0) + 1

        # A miss after a success of this daylight run is news; the inverter that
        # is still asleep when the sun comes up is not.
        held_run = self._held is not None
        level = (
            logging.INFO
            if held_run and self._missed <= MAX_MISSED_POLLS + 1
            else logging.DEBUG
        )
        detail = f"{err} (retry cut by the poll budget)" if retry_cut else str(err)
        _LOGGER.log(
            level,
            "Missed poll %d/%d: %s: %s (after %.1f s)",
            self._missed,
            MAX_MISSED_POLLS,
            name,
            detail,
            duration,
        )

        if self._can_hold(dt_util.utcnow()):
            if not isinstance(err, BridgeUnreachableError):
                self._note_bridge_reachable()
            assert self._held is not None
            return self._held

        if held_run and not self._exhaustion_logged:
            self._exhaustion_logged = True
            self.statistics.grace_exhaustions += 1
            self._warn_grace_used_up(err, detail)
        if isinstance(err, SmaConnectionError):
            self._log_connection_error(err)
            return self._unknown_data()
        raise UpdateFailed(f"Unexpected data from the inverter: {err}") from err

    def _sun_is_changing(self, now: datetime) -> bool:
        """Is a sunrise or sunset within ``EXHAUSTION_NEAR_SUN`` of *now*?

        The inverter wakes up and goes to sleep then.  ``False`` if the sun cannot
        be computed (polar regions), like ``_sun_is_near`` but the other way round:
        without a sunrise or sunset the log keeps the louder level.
        """
        try:
            return sun.is_up(self.hass, now - EXHAUSTION_NEAR_SUN) != sun.is_up(
                self.hass, now + EXHAUSTION_NEAR_SUN
            )
        except ValueError:
            return False

    def _warn_grace_used_up(
        self, err: SmaConnectionError | SmaProtocolError, detail: str
    ) -> None:
        """The message of a miss episode: the entities no longer show the old data.

        It is a WARNING, but INFO within an hour of sunrise or sunset: the inverter
        waking up or going to sleep is expected then.  An unreachable bridge has its
        own warning (host and port) at any time of day; that one is the warning of
        this episode, so there are never two for the same failure.
        """
        if (
            isinstance(err, BridgeUnreachableError)
            and not self._bridge_unreachable_logged
        ):
            return
        level = (
            logging.INFO if self._sun_is_changing(dt_util.utcnow()) else logging.WARNING
        )
        if isinstance(err, SmaConnectionError):
            _LOGGER.log(
                level,
                "No answer from the inverter for %s in a row (last error: %s: %s). "
                "Its values are unknown until it answers again",
                _polls(self._missed),
                type(err).__name__,
                detail,
            )
        else:
            _LOGGER.log(
                level,
                "The inverter sent unexpected data for %s in a row (last error: "
                "%s: %s). Its entities are unavailable until it answers correctly "
                "again",
                _polls(self._missed),
                type(err).__name__,
                detail,
            )

    def _check_logon_backoff(self, now: datetime) -> None:
        """Stay away from the inverter while the back-off after a refusal runs.

        Raises ``UpdateFailed`` without a connection attempt.  Once the deadline has
        passed the next attempt decides.
        """
        until = _logon_refusals(self.hass).get(self.config_entry.entry_id)
        if until is not None and now < until:
            _LOGGER.debug(
                "The inverter refused the logon: not connecting until %s", until
            )
            raise UpdateFailed(
                "The inverter refused the logon; not connecting again before "
                f"{dt_util.as_local(until):%H:%M:%S}"
            )

    def _logon_refused(self, err: SmaLogonRefusedError) -> UpdateFailed:
        """Start the back-off after a refusal; returns the error to raise."""
        refusals = _logon_refusals(self.hass)
        new_episode = self.config_entry.entry_id not in refusals
        refusals[self.config_entry.entry_id] = dt_util.utcnow() + LOGON_REFUSED_BACKOFF
        minutes = int(LOGON_REFUSED_BACKOFF.total_seconds() // 60)
        if new_episode:
            _LOGGER.warning(
                "The inverter refused the logon (code %s). It probably has too many "
                "open sessions. Waiting %d minutes (the session timeout) before "
                "trying again",
                err.code,
                minutes,
            )
        else:
            _LOGGER.debug("The inverter still refuses the logon (code %s)", err.code)
        return UpdateFailed(
            f"The inverter refused the logon (code {err.code}); "
            f"waiting {minutes} minutes before trying again"
        )

    def _note_logon_accepted(self) -> None:
        if _logon_refusals(self.hass).pop(self.config_entry.entry_id, None) is not None:
            _LOGGER.info("The inverter accepts the logon again")

    def _log_connection_error(self, err: SmaConnectionError) -> None:
        """Log a poll that is reported as unknown without spamming the log."""
        if isinstance(err, BridgeUnreachableError):
            if not self._bridge_unreachable_logged:
                self._bridge_unreachable_logged = True
                _LOGGER.warning(
                    "Cannot reach the SMA Bluetooth bridge at %s:%s: %s",
                    self.config_entry.data[CONF_HOST],
                    self.config_entry.data.get(CONF_PORT, DEFAULT_BRIDGE_PORT),
                    err,
                )
            else:
                _LOGGER.debug("The SMA Bluetooth bridge is still unreachable: %s", err)
            return
        # The bridge answered: it is the inverter that is not reachable (normal
        # at night) or the bridge that is busy.
        self._note_bridge_reachable()
        if isinstance(err, BridgeBusyError):
            _LOGGER.debug("The SMA Bluetooth bridge is busy: %s", err)
        else:
            _LOGGER.debug("The inverter is not reachable: %s", err)

    def _note_bridge_reachable(self) -> None:
        if self._bridge_unreachable_logged:
            self._bridge_unreachable_logged = False
            _LOGGER.info("The SMA Bluetooth bridge is reachable again")

    # ------------------------------------------------------------- data rules

    async def _online_data(self, result: InverterData) -> SbfspotData:
        """Build the data of a successful poll and persist what changed."""
        spot = result.spot
        today = dt_util.now().date()
        before = self._snapshot()

        if result.info != self._info:
            self._info = result.info
            self._async_update_device(result.info)
        for name in CUMULATIVE_FIELDS:
            value = _finite(getattr(spot, name))
            known = self._counters.get(name)
            # None, 0 and anything below the preserved value are not valid for a
            # cumulative counter: keep the preserved value instead.
            if value is not None and value > 0 and (known is None or value >= known):
                self._counters[name] = value
        e_today = _finite(spot.e_today)
        if e_today is not None:
            self._e_today, self._e_today_date = e_today, today

        if self._snapshot() != before:
            await self._store.async_save(self._snapshot())

        values = self._preserved_values(today)
        return SbfspotData(online=True, info=self._info, spot=replace(spot, **values))

    @callback
    def _async_update_device(self, info: DeviceInfo) -> None:
        """Bring the device registry up to date (e.g. after a firmware update, or
        when the device was created at night, before the inverter was ever read)."""
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, str(info.serial))})
        if device is not None:
            registry.async_update_device(
                device.id,
                model=info.device_type or UNDEFINED,
                sw_version=info.sw_version or UNDEFINED,
            )

    def _preserved_values(self, today: date) -> dict[str, Any]:
        """The values that survive a gap: the counters and today's energy."""
        values: dict[str, Any] = {
            name: self._counters.get(name) for name in CUMULATIVE_FIELDS
        }
        values["e_today"] = self._e_today_value(today)
        return values

    def _offline_data(self) -> SbfspotData:
        """The data at night: the inverter sleeps, so power and current are 0 W."""
        values = dict.fromkeys(ZERO_WHEN_OFFLINE_FIELDS, 0.0)
        values.update(self._preserved_values(dt_util.now().date()))
        return SbfspotData(online=False, info=self._info, spot=SpotData(**values))

    def _unknown_data(self) -> SbfspotData:
        """The data after missed polls: every instantaneous value is unknown.

        A sample that is missing is not 0 W.  The counters and today's energy keep
        their preserved values; the entities stay available.
        """
        values = self._preserved_values(dt_util.now().date())
        return SbfspotData(online=False, info=self._info, spot=SpotData(**values))

    def _e_today_value(self, today: date) -> float | None:
        """Today's energy: preserved during the day, 0 after local midnight."""
        if self._e_today is None or self._e_today_date is None:
            return None
        return self._e_today if self._e_today_date == today else 0.0
