"""Sensor platform for the SBFspot (SMA Bluetooth) integration.

Every description is spelled out (no helper that builds them) so that
``tests/test_sensor_consistency.py`` can read keys, translation keys and the
``SpotData`` fields from the source with ``ast``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import SbfspotConfigEntry, SbfspotCoordinator, SbfspotData
from .entity import SbfspotEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SbfspotSensorDescription(SensorEntityDescription):
    """Describes an SBFspot sensor."""

    value_fn: Callable[[SbfspotData], StateType]


SENSORS: tuple[SbfspotSensorDescription, ...] = (
    # ------------------------------------------------------------ AC side
    SbfspotSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda d: d.spot.pac_total,
    ),
    SbfspotSensorDescription(
        key="ac_power_l1",
        translation_key="ac_power_l1",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda d: d.spot.pac1,
    ),
    SbfspotSensorDescription(
        key="ac_power_l2",
        translation_key="ac_power_l2",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.spot.pac2,
    ),
    SbfspotSensorDescription(
        key="ac_power_l3",
        translation_key="ac_power_l3",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.spot.pac3,
    ),
    SbfspotSensorDescription(
        key="ac_voltage_l1",
        translation_key="ac_voltage_l1",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda d: d.spot.uac1,
    ),
    SbfspotSensorDescription(
        key="ac_voltage_l2",
        translation_key="ac_voltage_l2",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.spot.uac2,
    ),
    SbfspotSensorDescription(
        key="ac_voltage_l3",
        translation_key="ac_voltage_l3",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.spot.uac3,
    ),
    SbfspotSensorDescription(
        key="ac_current_l1",
        translation_key="ac_current_l1",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value_fn=lambda d: d.spot.iac1,
    ),
    SbfspotSensorDescription(
        key="ac_current_l2",
        translation_key="ac_current_l2",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.spot.iac2,
    ),
    SbfspotSensorDescription(
        key="ac_current_l3",
        translation_key="ac_current_l3",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.spot.iac3,
    ),
    SbfspotSensorDescription(
        key="grid_frequency",
        translation_key="grid_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        value_fn=lambda d: d.spot.grid_frequency,
    ),
    # ------------------------------------------------------------ DC side
    SbfspotSensorDescription(
        key="dc_power_a",
        translation_key="dc_power_a",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda d: d.spot.pdc1,
    ),
    SbfspotSensorDescription(
        key="dc_power_b",
        translation_key="dc_power_b",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.spot.pdc2,
    ),
    SbfspotSensorDescription(
        key="dc_voltage_a",
        translation_key="dc_voltage_a",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda d: d.spot.udc1,
    ),
    SbfspotSensorDescription(
        key="dc_voltage_b",
        translation_key="dc_voltage_b",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.spot.udc2,
    ),
    SbfspotSensorDescription(
        key="dc_current_a",
        translation_key="dc_current_a",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        value_fn=lambda d: d.spot.idc1,
    ),
    SbfspotSensorDescription(
        key="dc_current_b",
        translation_key="dc_current_b",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.spot.idc2,
    ),
    # ------------------------------------------------- energy and run time
    SbfspotSensorDescription(
        key="energy_today",
        translation_key="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d: d.spot.e_today,
    ),
    SbfspotSensorDescription(
        key="energy_total",
        translation_key="energy_total",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d: d.spot.e_total,
    ),
    SbfspotSensorDescription(
        key="operating_time",
        translation_key="operating_time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=lambda d: d.spot.operating_time,
    ),
    SbfspotSensorDescription(
        key="feed_in_time",
        translation_key="feed_in_time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=lambda d: d.spot.feed_in_time,
    ),
    # ----------------------------------------------------------- state
    SbfspotSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda d: d.spot.temperature,
    ),
    SbfspotSensorDescription(
        key="status",
        translation_key="status",
        value_fn=lambda d: d.spot.status,
    ),
    SbfspotSensorDescription(
        key="grid_relay",
        translation_key="grid_relay",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.spot.grid_relay,
    ),
    SbfspotSensorDescription(
        key="bluetooth_signal",
        translation_key="bluetooth_signal",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda d: d.spot.bt_signal,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SbfspotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the SBFspot sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        SbfspotSensor(coordinator, description) for description in SENSORS
    )


class SbfspotSensor(SbfspotEntity, SensorEntity):
    """A read-only value of the inverter."""

    entity_description: SbfspotSensorDescription

    def __init__(
        self,
        coordinator: SbfspotCoordinator,
        description: SbfspotSensorDescription,
    ) -> None:
        """Create the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        """The value from the coordinator data (offline rules already applied)."""
        return self.entity_description.value_fn(self.coordinator.data)
