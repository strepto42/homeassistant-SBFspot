"""Options flow: the polling interval."""

from __future__ import annotations

from datetime import timedelta

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sbfspot.const import CONF_SCAN_INTERVAL

from .conftest import Bridge, setup_entry


def _suggested(schema: vol.Schema) -> dict:
    return {
        str(key): key.description["suggested_value"]
        for key in schema.schema
        if key.description and "suggested_value" in key.description
    }


async def test_the_form_defaults_to_60_seconds(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    (key,) = result["data_schema"].schema
    assert str(key) == CONF_SCAN_INTERVAL
    assert key.default() == 60


async def test_the_form_shows_the_current_interval(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_SCAN_INTERVAL: 90}
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    assert _suggested(result["data_schema"]) == {CONF_SCAN_INTERVAL: 90}


async def test_changing_the_interval_reloads_the_entry(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    old_coordinator = mock_config_entry.runtime_data
    assert old_coordinator.update_interval == timedelta(seconds=60)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SCAN_INTERVAL: 120}
    assert mock_config_entry.options == {CONF_SCAN_INTERVAL: 120}
    assert isinstance(mock_config_entry.options[CONF_SCAN_INTERVAL], int)
    # The entry was reloaded: a new coordinator polls at the new interval.
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data is not old_coordinator
    assert mock_config_entry.runtime_data.update_interval == timedelta(seconds=120)


@pytest.mark.parametrize("seconds", [30, 31, 3599, 3600, 45.0])
async def test_intervals_in_range_are_accepted(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    seconds: float,
) -> None:
    await setup_entry(hass, mock_config_entry)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: seconds}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_SCAN_INTERVAL] == seconds
    assert type(mock_config_entry.options[CONF_SCAN_INTERVAL]) is int
    assert mock_config_entry.runtime_data.update_interval == timedelta(
        seconds=int(seconds)
    )


@pytest.mark.parametrize("seconds", [0, 10, 29, 29.9, 3601, 100000, -60])
async def test_intervals_out_of_range_are_rejected(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    seconds: float,
) -> None:
    await setup_entry(hass, mock_config_entry)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: seconds}
        )

    assert mock_config_entry.options == {}
