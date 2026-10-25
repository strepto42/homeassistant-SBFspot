"""DataUpdateCoordinator for the SBFspot (SMA Bluetooth) integration.

Night is normal: the inverter's Bluetooth radio sleeps without sunlight.  The
coordinator therefore

* does not open a connection outside sunrise - 15 min .. sunset + 15 min,
* turns every connection failure into *offline* data instead of ``UpdateFailed``,
* keeps the cumulative counters (``e_total``, ``operating_time``,
  ``feed_in_time``) at their last valid value, persisted with ``Store``, so a
  ``TOTAL_INCREASING`` sensor never reports 0 or a lower value (that would
  corrupt the long-term statistics), and
* resets ``e_today`` to 0 at local midnight.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, replace
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
    SpotData,
    async_read_inverter,
)

_LOGGER = logging.getLogger(__name__)

type SbfspotConfigEntry = ConfigEntry[SbfspotCoordinator]


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
    zero power while offline, ...); ``info`` is the last known device info (from
    the inverter or the store), ``None`` only if it was never read.
    """

    online: bool
    info: DeviceInfo | None
    spot: SpotData


def _finite(value: object) -> float | None:
    """*value* as a float if it is a finite number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


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
        """Read the inverter, or report it offline."""
        if not self._sun_is_near(dt_util.utcnow()):
            _LOGGER.debug("The sun is down: not polling the inverter")
            return self._offline_data()

        data = self.config_entry.data
        transport = BridgeTransport(
            data[CONF_HOST],
            data.get(CONF_PORT, DEFAULT_BRIDGE_PORT),
            data[CONF_BT_ADDRESS],
        )
        try:
            result = await async_read_inverter(
                transport, data.get(CONF_PASSWORD, DEFAULT_PASSWORD)
            )
        except SmaAuthError as err:
            raise ConfigEntryAuthFailed("The inverter rejected the password") from err
        except SmaConnectionError as err:
            self._log_connection_error(err)
            return self._offline_data()
        except SmaError as err:
            raise UpdateFailed(f"Unexpected data from the inverter: {err}") from err

        self._note_bridge_reachable()
        return await self._online_data(result)

    def _log_connection_error(self, err: SmaConnectionError) -> None:
        """Log an offline poll without spamming the log."""
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
            _LOGGER.debug("The inverter is offline: %s", err)

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

        values: dict[str, Any] = {
            name: self._counters.get(name) for name in CUMULATIVE_FIELDS
        }
        values["e_today"] = self._e_today_value(today)
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

    def _offline_data(self) -> SbfspotData:
        """The data while the inverter cannot be reached."""
        today = dt_util.now().date()
        values: dict[str, Any] = dict.fromkeys(ZERO_WHEN_OFFLINE_FIELDS, 0.0)
        for name in CUMULATIVE_FIELDS:
            values[name] = self._counters.get(name)
        values["e_today"] = self._e_today_value(today)
        return SbfspotData(online=False, info=self._info, spot=SpotData(**values))

    def _e_today_value(self, today: date) -> float | None:
        """Today's energy: preserved during the day, 0 after local midnight."""
        if self._e_today is None or self._e_today_date is None:
            return None
        return self._e_today if self._e_today_date == today else 0.0
