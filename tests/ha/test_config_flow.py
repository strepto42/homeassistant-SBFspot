"""Config flow: user step, reconfigure and reauth."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import call

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sbfspot.const import (
    CONF_BT_ADDRESS,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    DOMAIN,
)
from custom_components.sbfspot.smabt import (
    BridgeBusyError,
    BridgeUnreachableError,
    DeviceInfo,
    InverterUnreachableError,
    SmaAuthError,
    SmaConnectionError,
    SmaProtocolError,
    SmaTimeoutError,
)

from .conftest import (
    BRIDGE_HOST,
    BT_ADDRESS,
    PASSWORD,
    SERIAL,
    TITLE,
    Bridge,
    make_inverter_data,
    setup_entry,
    state_of,
)

USER_INPUT = {
    CONF_HOST: BRIDGE_HOST,
    CONF_PORT: 9900,
    CONF_BT_ADDRESS: BT_ADDRESS,
    CONF_PASSWORD: PASSWORD,
}

ERROR_CASES = [
    (BridgeUnreachableError("refused"), "bridge_unreachable"),
    (BridgeBusyError("busy"), "bridge_busy"),
    (InverterUnreachableError("asleep"), "inverter_unreachable"),
    (SmaTimeoutError("no answer"), "inverter_unreachable"),
    (SmaConnectionError("dropped"), "cannot_connect"),
    (SmaAuthError("bad password"), "invalid_auth"),
    (SmaProtocolError("garbage"), "protocol_error"),
    (RuntimeError("boom"), "unknown"),
]
ERROR_IDS = [key for _, key in ERROR_CASES]


async def _start(hass: HomeAssistant):
    """Start the user flow."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


def _defaults(schema: vol.Schema) -> dict[str, Any]:
    return {
        str(key): key.default()
        for key in schema.schema
        if key.default is not vol.UNDEFINED
    }


def _suggested(schema: vol.Schema) -> dict[str, Any]:
    return {
        str(key): key.description["suggested_value"]
        for key in schema.schema
        if key.description and "suggested_value" in key.description
    }


# ---------------------------------------------------------------- user step


async def test_user_flow_success(hass: HomeAssistant, bridge: Bridge) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TITLE
    assert result["data"] == USER_INPUT
    assert isinstance(result["data"][CONF_PORT], int)
    assert result["result"].unique_id == str(SERIAL)
    # The settings were proved by reading the inverter with the password (the
    # second read is the first refresh of the entry that was just created).
    assert bridge.transport.call_args_list[0] == call(BRIDGE_HOST, 9900, BT_ADDRESS)
    assert bridge.read.await_args_list[0] == call(
        bridge.transport.return_value, PASSWORD
    )
    assert bridge.reads == 2


async def test_user_form_defaults(hass: HomeAssistant, bridge: Bridge) -> None:
    result = await _start(hass)

    assert _defaults(result["data_schema"]) == {CONF_PORT: 9900, CONF_PASSWORD: "0000"}


async def test_user_flow_normalises_the_bluetooth_address(
    hass: HomeAssistant, bridge: Bridge
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**USER_INPUT, CONF_BT_ADDRESS: " 00-80-25-ab-cd-ef "},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BT_ADDRESS] == "00:80:25:AB:CD:EF"
    assert bridge.transport.call_args_list[0] == call(
        BRIDGE_HOST, 9900, "00:80:25:AB:CD:EF"
    )


async def test_user_flow_strips_the_host_and_coerces_the_port(
    hass: HomeAssistant, bridge: Bridge
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**USER_INPUT, CONF_HOST: "  sma-bridge.local ", CONF_PORT: 9901.0},
    )

    assert result["data"][CONF_HOST] == "sma-bridge.local"
    assert result["data"][CONF_PORT] == 9901
    assert type(result["data"][CONF_PORT]) is int


@pytest.mark.parametrize(
    "bad_address",
    ["", "not a mac", "00:80:25:00:00", "00:80:25:00:00:01:02", "GG:80:25:00:00:01"],
)
async def test_user_flow_rejects_a_bad_bluetooth_address(
    hass: HomeAssistant, bridge: Bridge, bad_address: str
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_BT_ADDRESS: bad_address}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_BT_ADDRESS: "invalid_bt_address"}
    bridge.read.assert_not_awaited()
    # What the user typed is still in the form.
    assert _suggested(result["data_schema"])[CONF_HOST] == BRIDGE_HOST


@pytest.mark.parametrize(("error", "key"), ERROR_CASES, ids=ERROR_IDS)
async def test_user_flow_error_mapping(
    hass: HomeAssistant,
    bridge: Bridge,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    key: str,
) -> None:
    bridge.read.side_effect = error
    result = await _start(hass)

    with caplog.at_level(logging.ERROR):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": key}
    assert hass.config_entries.async_entries(DOMAIN) == []
    # Only the unexpected error is logged (with its traceback).
    assert ("Unexpected error" in caplog.text) is (key == "unknown")

    # The form can be submitted again once the problem is gone.
    bridge.read.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_duplicate_aborts(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "192.168.1.99"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert mock_config_entry.data[CONF_HOST] == BRIDGE_HOST


async def test_user_flow_uses_a_generic_title_without_a_device_type(
    hass: HomeAssistant, bridge: Bridge
) -> None:
    bridge.read.return_value = make_inverter_data(
        info=DeviceInfo(serial=SERIAL, susy_id=113, bt_address=BT_ADDRESS)
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["title"] == f"SMA inverter {SERIAL}"


# -------------------------------------------------------------- reconfigure

NEW_HOST = "192.168.1.60"
NEW_PORT = 9901
NEW_BT_ADDRESS = "00:80:25:00:00:02"
RECONFIGURE_INPUT = {
    CONF_HOST: NEW_HOST,
    CONF_PORT: NEW_PORT,
    CONF_BT_ADDRESS: NEW_BT_ADDRESS,
}


async def test_reconfigure_form_is_prefilled(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)

    result = await mock_config_entry.start_reconfigure_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert _suggested(result["data_schema"]) == {
        CONF_HOST: BRIDGE_HOST,
        CONF_PORT: 9900,
        CONF_BT_ADDRESS: BT_ADDRESS,
    }


async def test_reconfigure_success_updates_the_entry_in_place(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    entry_id = mock_config_entry.entry_id
    old_coordinator = mock_config_entry.runtime_data
    bridge.read.reset_mock()
    bridge.transport.reset_mock()

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], RECONFIGURE_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert entry.entry_id == entry_id
    assert entry.unique_id == str(SERIAL)
    assert entry.data == {**RECONFIGURE_INPUT, CONF_PASSWORD: PASSWORD}
    # Validated with the stored password against the new address ...
    assert bridge.read.await_args_list[0].args[1] == PASSWORD
    # ... and reloaded, so the new coordinator talks to the new bridge.
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not old_coordinator
    assert bridge.transport.call_args == ((NEW_HOST, NEW_PORT, NEW_BT_ADDRESS),)


async def test_reconfigure_refuses_a_different_inverter(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.return_value = make_inverter_data(
        info=DeviceInfo(serial=2100000099, susy_id=113, bt_address=NEW_BT_ADDRESS)
    )

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], RECONFIGURE_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert mock_config_entry.data[CONF_HOST] == BRIDGE_HOST
    assert mock_config_entry.data[CONF_BT_ADDRESS] == BT_ADDRESS
    assert mock_config_entry.unique_id == str(SERIAL)


async def test_reconfigure_rejects_a_bad_bluetooth_address(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.reset_mock()

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**RECONFIGURE_INPUT, CONF_BT_ADDRESS: "nope"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BT_ADDRESS: "invalid_bt_address"}
    bridge.read.assert_not_awaited()


@pytest.mark.parametrize(("error", "key"), ERROR_CASES, ids=ERROR_IDS)
async def test_reconfigure_error_mapping(
    hass: HomeAssistant,
    bridge: Bridge,
    mock_config_entry: MockConfigEntry,
    error: Exception,
    key: str,
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.side_effect = error

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], RECONFIGURE_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": key}
    assert mock_config_entry.data[CONF_HOST] == BRIDGE_HOST


# -------------------------------------------------------------------- reauth


async def test_reauth_success_changes_the_password(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.reset_mock()

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"] == {"name": TITLE}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-secret"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data == {**USER_INPUT, CONF_PASSWORD: "new-secret"}
    # The new password was validated against the stored bridge and inverter.
    assert bridge.read.await_args_list[0].args[1] == "new-secret"
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_reauth_wrong_password_shows_an_error(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.side_effect = SmaAuthError("rejected")

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "still-wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    assert mock_config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_reauth_refuses_a_different_inverter(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    bridge.read.return_value = make_inverter_data(
        info=DeviceInfo(serial=2100000099, susy_id=113, bt_address=BT_ADDRESS)
    )

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-secret"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert mock_config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_auth_failure_in_the_coordinator_starts_reauth(
    hass: HomeAssistant, bridge: Bridge, mock_config_entry: MockConfigEntry
) -> None:
    """Wrong password -> ``ConfigEntryAuthFailed`` -> a reauth flow for the entry."""
    bridge.read.side_effect = SmaAuthError("rejected")

    await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [(f["context"]["source"], f["step_id"]) for f in flows] == [
        (SOURCE_REAUTH, "reauth_confirm")
    ]
    assert flows[0]["context"]["entry_id"] == mock_config_entry.entry_id

    # Fix the password through that flow: the entry loads again.
    bridge.read.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], {CONF_PASSWORD: "fixed"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state_of(hass, "energy_total") == "60854.831"
