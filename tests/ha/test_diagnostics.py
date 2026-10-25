"""Diagnostics: coordinator data, without the secrets."""

from __future__ import annotations

import json

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.sbfspot.const import (
    CONF_BT_ADDRESS,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    DOMAIN,
)
from custom_components.sbfspot.smabt import InverterUnreachableError

from .conftest import BT_ADDRESS, SERIAL, Bridge, refresh, setup_entry

REDACTED = "**REDACTED**"
SECRET_PASSWORD = "s3cret-Pa55"


@pytest.fixture
def secret_entry() -> MockConfigEntry:
    """An entry whose password cannot be mistaken for any other value."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"SB 3000TL-20 {SERIAL}",
        unique_id=str(SERIAL),
        data={
            CONF_HOST: "192.168.1.50",
            CONF_PORT: 9900,
            CONF_BT_ADDRESS: BT_ADDRESS,
            CONF_PASSWORD: SECRET_PASSWORD,
        },
    )


async def test_diagnostics_redact_the_password_and_the_bluetooth_address(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    bridge: Bridge,
    secret_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, secret_entry)

    result = await get_diagnostics_for_config_entry(hass, hass_client, secret_entry)

    assert result["entry"]["data"][CONF_PASSWORD] == REDACTED
    assert result["entry"]["data"][CONF_BT_ADDRESS] == REDACTED
    assert result["entry"]["data"][CONF_HOST] == "192.168.1.50"
    assert result["entry"]["data"][CONF_PORT] == 9900
    # The device info inside the coordinator data is redacted too.
    assert result["coordinator"]["data"]["info"]["bt_address"] == REDACTED
    # Nothing leaks anywhere in the document.
    text = json.dumps(result)
    assert SECRET_PASSWORD not in text
    assert BT_ADDRESS not in text
    assert str(SERIAL) not in text


async def test_diagnostics_contain_the_coordinator_data(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    bridge: Bridge,
    secret_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, secret_entry)

    result = await get_diagnostics_for_config_entry(hass, hass_client, secret_entry)

    coordinator = result["coordinator"]
    assert coordinator["last_update_success"] is True
    assert coordinator["update_interval_seconds"] == 60
    assert coordinator["data"]["online"] is True
    assert coordinator["data"]["info"]["device_type"] == "SB 3000TL-20"
    assert coordinator["data"]["info"]["sw_version"] == "03.20.01.R"
    assert coordinator["data"]["spot"]["pac_total"] == 622.0
    assert coordinator["data"]["spot"]["e_total"] == pytest.approx(60854.831)
    assert coordinator["data"]["spot"]["status"] == "Ok"
    assert result["entry"]["options"] == {}


async def test_diagnostics_while_offline(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    bridge: Bridge,
    secret_entry: MockConfigEntry,
) -> None:
    await setup_entry(hass, secret_entry)
    bridge.read.side_effect = InverterUnreachableError("asleep")
    await refresh(hass, secret_entry)

    result = await get_diagnostics_for_config_entry(hass, hass_client, secret_entry)

    data = result["coordinator"]["data"]
    assert data["online"] is False
    assert data["spot"]["pac_total"] == 0.0
    assert data["spot"]["e_total"] == pytest.approx(60854.831)
    assert SECRET_PASSWORD not in json.dumps(result)


async def test_diagnostics_before_the_inverter_was_ever_read(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    bridge: Bridge,
    sun_is_up,
    secret_entry: MockConfigEntry,
) -> None:
    sun_is_up.return_value = False
    await setup_entry(hass, secret_entry)

    result = await get_diagnostics_for_config_entry(hass, hass_client, secret_entry)

    assert result["coordinator"]["data"]["info"] is None
    assert result["coordinator"]["data"]["spot"]["e_total"] is None
    assert SECRET_PASSWORD not in json.dumps(result)
