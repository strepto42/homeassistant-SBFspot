"""Diagnostics for the SBFspot (SMA Bluetooth) integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_BT_ADDRESS, CONF_PASSWORD
from .coordinator import SbfspotConfigEntry

# The password and the inverter's Bluetooth address are secrets; the serial
# number (also part of the title, the unique id and the SMA device name)
# identifies the installation.
TO_REDACT = {
    CONF_PASSWORD,
    CONF_BT_ADDRESS,
    "serial",
    "name",
    "title",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SbfspotConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    update_interval = coordinator.update_interval
    return async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "unique_id": entry.unique_id,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "coordinator": {
                "last_update_success": coordinator.last_update_success,
                "update_interval_seconds": (
                    update_interval.total_seconds() if update_interval else None
                ),
                "data": asdict(coordinator.data) if coordinator.data else None,
            },
        },
        TO_REDACT,
    )
