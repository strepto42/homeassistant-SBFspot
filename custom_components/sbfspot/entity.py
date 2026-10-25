"""Base entity for the SBFspot (SMA Bluetooth) integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BT_ADDRESS, DOMAIN, MANUFACTURER
from .coordinator import SbfspotCoordinator


class SbfspotEntity(CoordinatorEntity[SbfspotCoordinator]):
    """Common base: one device per inverter, ``<serial>_<key>`` unique ids."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SbfspotCoordinator, key: str) -> None:
        """Wire the entity to the coordinator."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._serial = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{self._serial}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """The inverter, from the last known device info.

        The info is persisted, so the device is complete even when Home Assistant
        was set up (or restarted) at night and has not read the inverter yet.
        """
        entry = self.coordinator.config_entry
        info = self.coordinator.data.info if self.coordinator.data else None
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            connections={(CONNECTION_BLUETOOTH, entry.data[CONF_BT_ADDRESS])},
            manufacturer=MANUFACTURER,
            model=info.device_type if info else None,
            name=entry.title,
            serial_number=self._serial,
            sw_version=info.sw_version if info else None,
        )
