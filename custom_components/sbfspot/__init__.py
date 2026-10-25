"""The SBFspot (SMA Bluetooth) integration.

Reads an SMA inverter over Bluetooth Classic through an ESP32 bridge (see
``esphome/``) that Home Assistant reaches over TCP.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import (
    SbfspotConfigEntry,
    SbfspotCoordinator,
    async_remove_stored_data,
)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SbfspotConfigEntry) -> bool:
    """Set up an inverter from a config entry.

    Night is normal: when the sun is down, or the inverter's Bluetooth sleeps,
    the first refresh returns offline data and setup still succeeds.
    """
    coordinator = SbfspotCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SbfspotConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: SbfspotConfigEntry) -> None:
    """Delete the preserved values of a removed config entry."""
    await async_remove_stored_data(hass, entry.entry_id)
