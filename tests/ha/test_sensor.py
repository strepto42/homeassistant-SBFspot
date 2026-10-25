"""Sensor platform: values, units, classes, defaults and the device."""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sbfspot.const import DOMAIN

from .conftest import SERIAL, TITLE, Bridge, entity_id, setup_entry, state_of

# key, state, unit, device class, state class  (the real SB 3000TL-20 capture)
ONLINE_SENSORS = [
    ("power", 622.0, "W", "power", "measurement"),
    ("ac_power_l1", 622.0, "W", "power", "measurement"),
    ("ac_voltage_l1", 228.94, "V", "voltage", "measurement"),
    ("ac_current_l1", 2.72, "A", "current", "measurement"),
    ("dc_power_a", 643.0, "W", "power", "measurement"),
    ("dc_voltage_a", 336.83, "V", "voltage", "measurement"),
    ("dc_current_a", 1.906, "A", "current", "measurement"),
    ("grid_frequency", 49.97, "Hz", "frequency", "measurement"),
    ("energy_today", 4.883, "kWh", "energy", "total_increasing"),
    ("energy_total", 60854.831, "kWh", "energy", "total_increasing"),
    ("operating_time", 58826.405, "h", "duration", "total_increasing"),
    ("feed_in_time", 57238.146, "h", "duration", "total_increasing"),
    ("temperature", 56.96, "°C", "temperature", "measurement"),
    ("bluetooth_signal", 185 * 100 / 255, "%", None, "measurement"),
]
TEXT_SENSORS = [("status", "Ok"), ("grid_relay", "Closed")]

# NaN on the wire (single-phase unit, one string): unknown, not 0.
EMPTY_SENSORS = [
    "ac_power_l2",
    "ac_power_l3",
    "ac_voltage_l2",
    "ac_voltage_l3",
    "ac_current_l2",
    "ac_current_l3",
    "dc_power_b",
    "dc_voltage_b",
    "dc_current_b",
]
DISABLED_BY_DEFAULT = set(EMPTY_SENSORS)
DIAGNOSTIC = {"operating_time", "feed_in_time", "grid_relay", "bluetooth_signal"}


@pytest.mark.parametrize(
    ("key", "value", "unit", "device_class", "state_class"), ONLINE_SENSORS
)
async def test_online_numeric_sensors(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    key: str,
    value: float,
    unit: str,
    device_class: str | None,
    state_class: str,
) -> None:
    await setup_entry(hass, mock_config_entry)

    state = hass.states.get(entity_id(hass, "sensor", key))
    assert state is not None
    assert float(state.state) == pytest.approx(value)
    assert state.attributes["unit_of_measurement"] == unit
    assert state.attributes.get("device_class") == device_class
    assert state.attributes["state_class"] == state_class


@pytest.mark.parametrize(("key", "value"), TEXT_SENSORS)
async def test_online_text_sensors(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    key: str,
    value: str,
) -> None:
    await setup_entry(hass, mock_config_entry)

    state = hass.states.get(entity_id(hass, "sensor", key))
    assert state is not None
    assert state.state == value
    assert "unit_of_measurement" not in state.attributes
    assert "state_class" not in state.attributes
    assert "device_class" not in state.attributes


async def test_values_the_inverter_does_not_report_are_unknown(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    entity_registry_enabled_by_default: None,
) -> None:
    await setup_entry(hass, mock_config_entry)

    for key in EMPTY_SENSORS:
        assert state_of(hass, key) == STATE_UNKNOWN, key


async def test_the_second_phase_and_string_are_disabled_by_default(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    sensors = [e for e in entries if e.domain == "sensor"]
    assert len(sensors) == 25
    disabled = {
        e.unique_id.removeprefix(f"{SERIAL}_")
        for e in sensors
        if e.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    }
    assert disabled == DISABLED_BY_DEFAULT
    for key in DISABLED_BY_DEFAULT:
        assert hass.states.get(entity_id(hass, "sensor", key)) is None
    # Everything else is enabled and has a state.
    for entry in sensors:
        if entry.disabled_by is None:
            assert hass.states.get(entry.entity_id) is not None, entry.entity_id


async def test_diagnostic_entities(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    registry = er.async_get(hass)
    for entry in er.async_entries_for_config_entry(
        registry, mock_config_entry.entry_id
    ):
        key = entry.unique_id.removeprefix(f"{SERIAL}_")
        expected = (
            EntityCategory.DIAGNOSTIC
            if key in DIAGNOSTIC | {"inverter_online"}
            else None
        )
        assert entry.entity_category is expected, key


async def test_unique_ids_names_and_one_device(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    assert len(entries) == 26  # 25 sensors + the binary sensor
    assert {e.unique_id for e in entries} == {
        f"{SERIAL}_{key}"
        for key in (
            *(k for k, *_ in ONLINE_SENSORS),
            *(k for k, _ in TEXT_SENSORS),
            *EMPTY_SENSORS,
            "inverter_online",
        )
    }
    assert len({e.device_id for e in entries}) == 1

    state = hass.states.get(entity_id(hass, "sensor", "power"))
    assert state is not None
    assert state.entity_id == "sensor.sb_3000tl_20_2100000042_ac_power"
    assert state.attributes["friendly_name"] == f"{TITLE} AC power"
    state = hass.states.get(entity_id(hass, "sensor", "dc_power_a"))
    assert state is not None
    assert state.attributes["friendly_name"] == f"{TITLE} DC power string A"


async def test_the_entities_belong_to_the_integration(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    entry = er.async_get(hass).async_get(entity_id(hass, "sensor", "energy_total"))
    assert entry is not None
    assert entry.platform == DOMAIN
    assert entry.config_entry_id == mock_config_entry.entry_id
