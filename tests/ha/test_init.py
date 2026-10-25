"""Setup, unload, removal and the night rules of setting up."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sbfspot.const import CONF_SCAN_INTERVAL, DOMAIN
from custom_components.sbfspot.coordinator import storage_key
from custom_components.sbfspot.smabt import SmaProtocolError

from .conftest import (
    DEVICE_INFO,
    OFFLINE_ERRORS,
    Bridge,
    entity_id,
    make_inverter_data,
    number,
    refresh,
    setup_entry,
    state_of,
)


async def test_setup_and_unload(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert bridge.reads == 1
    assert state_of(hass, "energy_total") == "60854.831"

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert state_of(hass, "energy_total") == STATE_UNAVAILABLE
    assert state_of(hass, "inverter_online", "binary_sensor") == STATE_UNAVAILABLE


async def test_removing_the_entry_deletes_the_preserved_values(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    await setup_entry(hass, mock_config_entry)
    key = storage_key(mock_config_entry.entry_id)
    assert key in hass_storage

    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_update_interval_defaults_to_60_seconds(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.update_interval == timedelta(seconds=60)


async def test_update_interval_comes_from_the_options(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_SCAN_INTERVAL: 45}
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.runtime_data.update_interval == timedelta(seconds=45)


async def test_one_device_with_all_the_entities(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    await setup_entry(hass, mock_config_entry)

    devices = dr.async_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert len(devices) == 1
    device = devices[0]
    assert device.identifiers == {(DOMAIN, "2100000042")}
    assert device.connections == {("bluetooth", "00:80:25:00:00:01")}
    assert device.manufacturer == "SMA"
    assert device.model == "SB 3000TL-20"
    assert device.name == "SB 3000TL-20 2100000042"
    assert device.serial_number == "2100000042"
    assert device.sw_version == "03.20.01.R"


# ------------------------------------------------------------ night is normal


async def test_setup_at_night_succeeds_without_a_connection_attempt(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
) -> None:
    sun_is_up.return_value = False

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    bridge.read.assert_not_awaited()
    bridge.transport.assert_not_called()
    # Entities exist and are available, with offline values.
    assert state_of(hass, "inverter_online", "binary_sensor") == "off"
    assert number(hass, "power") == 0
    assert state_of(hass, "ac_voltage_l1") == STATE_UNKNOWN
    assert state_of(hass, "energy_total") == STATE_UNKNOWN  # never read yet
    assert state_of(hass, "status") == STATE_UNKNOWN


@pytest.mark.parametrize("error", OFFLINE_ERRORS)
async def test_setup_with_the_inverter_asleep_succeeds(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    """Daylight, but no answer: offline data is valid data, setup must not fail."""
    bridge.read.side_effect = error

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert bridge.reads == 1
    assert state_of(hass, "inverter_online", "binary_sensor") == "off"
    assert number(hass, "power") == 0
    assert entity_id(hass, "sensor", "energy_total")


async def test_setup_retries_on_unexpected_data(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    """A protocol error is not 'night': Home Assistant retries the setup."""
    bridge.read.side_effect = SmaProtocolError("garbage")

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_the_device_is_completed_when_the_inverter_is_first_read(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Set up at night with nothing stored: the device has no model yet."""
    sun_is_up.return_value = False
    await setup_entry(hass, mock_config_entry)
    (device,) = dr.async_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert device.model is None
    assert device.sw_version is None
    assert device.identifiers == {(DOMAIN, "2100000042")}

    sun_is_up.return_value = True
    await refresh(hass, mock_config_entry)

    (device,) = dr.async_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert device.model == "SB 3000TL-20"
    assert device.sw_version == "03.20.01.R"


async def test_the_device_follows_a_firmware_update(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    await setup_entry(hass, mock_config_entry)

    bridge.read.return_value = make_inverter_data(
        info=replace(DEVICE_INFO, sw_version="03.30.00.R")
    )
    await refresh(hass, mock_config_entry)

    (device,) = dr.async_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert device.sw_version == "03.30.00.R"
    assert device.model == "SB 3000TL-20"
