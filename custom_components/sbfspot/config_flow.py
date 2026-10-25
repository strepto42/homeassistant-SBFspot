"""Config flow for the SBFspot (SMA Bluetooth) integration.

Every step proves that the settings work by reading the inverter once, through
the ESP32 bridge: ``async_read_inverter(BridgeTransport(...), password)``.
That needs the inverter's Bluetooth radio, which sleeps without sunlight, so
set up, reconfigure and re-authenticate in daylight.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_BT_ADDRESS,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_PASSWORD,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .smabt import (
    BridgeBusyError,
    BridgeTransport,
    BridgeUnreachableError,
    DeviceInfo,
    InverterUnreachableError,
    SmaAuthError,
    SmaConnectionError,
    SmaProtocolError,
    SmaTimeoutError,
    async_read_inverter,
)
from .smabt.transport import normalise_address

_LOGGER = logging.getLogger(__name__)

_HOST_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
_PORT_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
)
_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_SCAN_INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL,
        max=MAX_SCAN_INTERVAL,
        step=1,
        unit_of_measurement="s",
        mode=NumberSelectorMode.BOX,
    )
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): _HOST_SELECTOR,
        vol.Required(CONF_PORT, default=DEFAULT_BRIDGE_PORT): _PORT_SELECTOR,
        vol.Required(CONF_BT_ADDRESS): str,
        vol.Required(CONF_PASSWORD, default=DEFAULT_PASSWORD): _PASSWORD_SELECTOR,
    }
)
STEP_RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): _HOST_SELECTOR,
        vol.Required(CONF_PORT): _PORT_SELECTOR,
        vol.Required(CONF_BT_ADDRESS): str,
    }
)
STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR})
STEP_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
        ): _SCAN_INTERVAL_SELECTOR,
    }
)


class ValidationError(HomeAssistantError):
    """Reading the inverter failed; ``error`` is the key of the error string."""

    def __init__(self, error: str) -> None:
        """Remember the error key."""
        super().__init__(error)
        self.error = error


async def async_validate(
    host: str, port: int, bt_address: str, password: str
) -> DeviceInfo:
    """Read the inverter once and return its device info.

    Raises:
        ValidationError: with the key of the error string to show.
    """
    transport = BridgeTransport(host, port, bt_address)
    try:
        result = await async_read_inverter(transport, password)
    except BridgeUnreachableError as err:
        raise ValidationError("bridge_unreachable") from err
    except BridgeBusyError as err:
        raise ValidationError("bridge_busy") from err
    except (InverterUnreachableError, SmaTimeoutError) as err:
        raise ValidationError("inverter_unreachable") from err
    except SmaConnectionError as err:
        raise ValidationError("cannot_connect") from err
    except SmaAuthError as err:
        raise ValidationError("invalid_auth") from err
    except SmaProtocolError as err:
        raise ValidationError("protocol_error") from err
    except Exception as err:
        _LOGGER.exception("Unexpected error while reading the inverter")
        raise ValidationError("unknown") from err
    return result.info


class SbfspotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for an SMA Bluetooth inverter."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return SbfspotOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the bridge, the inverter and its password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            password = user_input[CONF_PASSWORD]
            try:
                bt_address = normalise_address(user_input[CONF_BT_ADDRESS])
            except ValueError:
                errors[CONF_BT_ADDRESS] = "invalid_bt_address"
            else:
                try:
                    info = await async_validate(host, port, bt_address, password)
                except ValidationError as err:
                    errors["base"] = err.error
                else:
                    await self.async_set_unique_id(str(info.serial))
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"{info.device_type or 'SMA inverter'} {info.serial}",
                        data={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_BT_ADDRESS: bt_address,
                            CONF_PASSWORD: password,
                        },
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the bridge or the Bluetooth address of the same inverter."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            try:
                bt_address = normalise_address(user_input[CONF_BT_ADDRESS])
            except ValueError:
                errors[CONF_BT_ADDRESS] = "invalid_bt_address"
            else:
                try:
                    info = await async_validate(
                        host, port, bt_address, entry.data[CONF_PASSWORD]
                    )
                except ValidationError as err:
                    errors["base"] = err.error
                else:
                    # The entry keeps its history and entities: refuse another
                    # inverter.
                    await self.async_set_unique_id(str(info.serial))
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_BT_ADDRESS: bt_address,
                        },
                        reason="reconfigure_successful",
                    )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_RECONFIGURE_SCHEMA, user_input or entry.data
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start re-authentication after the inverter rejected the password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new inverter password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            try:
                info = await async_validate(
                    entry.data[CONF_HOST],
                    entry.data[CONF_PORT],
                    entry.data[CONF_BT_ADDRESS],
                    password,
                )
            except ValidationError as err:
                errors["base"] = err.error
            else:
                await self.async_set_unique_id(str(info.serial))
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_PASSWORD: password},
                    reason="reauth_successful",
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"name": entry.title},
            errors=errors,
        )


class SbfspotOptionsFlow(OptionsFlowWithReload):
    """Options: how often the inverter is polled (the entry reloads on change)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the polling interval."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                STEP_OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
