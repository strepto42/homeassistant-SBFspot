"""Binary sensor platform for the SBFspot (SMA Bluetooth) integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SbfspotConfigEntry, SbfspotCoordinator, SbfspotData
from .entity import SbfspotEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SbfspotBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an SBFspot binary sensor."""

    value_fn: Callable[[SbfspotData], bool]


BINARY_SENSORS: tuple[SbfspotBinarySensorDescription, ...] = (
    SbfspotBinarySensorDescription(
        key="inverter_online",
        translation_key="inverter_online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        # On when the last poll reached the inverter; off at night or when the
        # bridge / inverter cannot be reached.
        value_fn=lambda d: d.online,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SbfspotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the SBFspot binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        SbfspotBinarySensor(coordinator, description) for description in BINARY_SENSORS
    )


class SbfspotBinarySensor(SbfspotEntity, BinarySensorEntity):
    """A read-only state of the inverter."""

    entity_description: SbfspotBinarySensorDescription

    def __init__(
        self,
        coordinator: SbfspotCoordinator,
        description: SbfspotBinarySensorDescription,
    ) -> None:
        """Create the binary sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Whether the last poll reached the inverter."""
        return self.entity_description.value_fn(self.coordinator.data)
