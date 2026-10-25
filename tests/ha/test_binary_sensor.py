"""Binary sensor platform: ``inverter_online``."""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sbfspot.smabt import SmaProtocolError

from .conftest import (
    OFFLINE_ERRORS,
    Bridge,
    entity_id,
    refresh,
    setup_entry,
    state_of,
)


def _online(hass: HomeAssistant) -> str:
    return state_of(hass, "inverter_online", "binary_sensor")


async def test_online_when_the_last_poll_reached_the_inverter(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    state = hass.states.get(entity_id(hass, "binary_sensor", "inverter_online"))
    assert state is not None
    assert state.state == "on"
    assert state.attributes["device_class"] == "connectivity"
    entry = er.async_get(hass).async_get(state.entity_id)
    assert entry is not None
    assert entry.entity_category is er.EntityCategory.DIAGNOSTIC
    assert entry.disabled_by is None


async def test_offline_at_night(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
) -> None:
    sun_is_up.return_value = False

    await setup_entry(hass, mock_config_entry)

    assert _online(hass) == "off"


@pytest.mark.parametrize("error", OFFLINE_ERRORS)
async def test_offline_when_the_inverter_cannot_be_reached(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    await setup_entry(hass, mock_config_entry)
    assert _online(hass) == "on"

    bridge.read.side_effect = error
    await refresh(hass, mock_config_entry)
    assert _online(hass) == "off"

    bridge.read.side_effect = None
    await refresh(hass, mock_config_entry)
    assert _online(hass) == "on"


async def test_follows_the_sun(
    hass: HomeAssistant,
    bridge: Bridge,
    sun_is_up,
    mock_config_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, mock_config_entry)
    assert _online(hass) == "on"

    sun_is_up.return_value = False
    await refresh(hass, mock_config_entry)
    assert _online(hass) == "off"

    sun_is_up.return_value = True
    await refresh(hass, mock_config_entry)
    assert _online(hass) == "on"


async def test_unavailable_only_on_an_update_failure(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    bridge.read.side_effect = SmaProtocolError("garbage")
    await refresh(hass, mock_config_entry)

    assert _online(hass) == STATE_UNAVAILABLE
