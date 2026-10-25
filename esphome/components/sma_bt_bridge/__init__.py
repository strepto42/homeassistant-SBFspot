"""ESPHome external component: TCP <-> Bluetooth Classic SPP (RFCOMM) bridge.

The ESP32 acts as a dumb byte pipe between a TCP client (Home Assistant) and
an RFCOMM channel 1 link to a Bluetooth Classic device (an SMA inverter).  All
protocol logic lives on the client side, the firmware only implements the
small handshake documented in bridge_protocol.h.

Only the original ESP32 (ESP32-WROOM / ESP32-WROVER) has the BR/EDR radio
needed for this; the S2/S3/C3/C6/H2 variants cannot do Bluetooth Classic.
"""

import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.components.esp32 import (
    add_idf_sdkconfig_option,
    get_esp32_variant,
    request_bluetooth,
    request_software_coexistence,
)
from esphome.components.esp32.const import VARIANT_ESP32
from esphome.components.socket import SocketType, consume_sockets
from esphome.const import CONF_ID, CONF_PIN, CONF_PORT, CONF_WIFI
from esphome.core import CORE

DOMAIN = "sma_bt_bridge"

DEPENDENCIES = ["esp32", "network"]
AUTO_LOAD = ["socket"]

CONF_BT_CONNECT_TIMEOUT = "bt_connect_timeout"
CONF_IDLE_TIMEOUT = "idle_timeout"

DEFAULT_PORT = 9900
DEFAULT_PIN = "0000"

# Components that start the BLE side of the Bluetooth stack.  This component
# runs the controller in Classic-only mode with BLE compiled out, so none of
# these can work next to it.
BLE_COMPONENTS = (
    "esp32_ble",
    "esp32_ble_tracker",
    "esp32_ble_server",
    "esp32_ble_beacon",
    "esp32_ble_client",
    "bluetooth_proxy",
    "ble_client",
    "improv_ble",
    "esp32_improv",
)

sma_bt_bridge_ns = cg.esphome_ns.namespace(DOMAIN)
SmaBtBridge = sma_bt_bridge_ns.class_("SmaBtBridge", cg.Component)


def _validate_pin(value):
    """Legacy pairing PIN: 4-16 printable ASCII characters, quoted in YAML."""
    # string_strict, not string: an unquoted `pin: 0000` is the YAML integer 0
    # and would silently lose its leading zeros.
    pin = cv.string_strict(value)
    if not 4 <= len(pin) <= 16:
        raise cv.Invalid(f"pin must be 4 to 16 characters long, got {len(pin)}")
    if any(not 0x20 <= ord(char) <= 0x7E for char in pin):
        raise cv.Invalid("pin may only contain printable ASCII characters")
    return pin


_TIMEOUT = cv.All(
    cv.positive_time_period_milliseconds,
    cv.Range(min=cv.TimePeriod(seconds=1), max=cv.TimePeriod(minutes=10)),
)

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(SmaBtBridge),
            cv.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
            cv.Optional(CONF_BT_CONNECT_TIMEOUT, default="15s"): _TIMEOUT,
            cv.Optional(CONF_IDLE_TIMEOUT, default="60s"): _TIMEOUT,
            cv.Optional(CONF_PIN, default=DEFAULT_PIN): _validate_pin,
        }
    ).extend(cv.COMPONENT_SCHEMA),
    # One listening socket, plus the active client and the short-lived socket
    # used to turn away a second client with "ERR BUSY".
    consume_sockets(1, DOMAIN, SocketType.TCP_LISTEN),
    consume_sockets(2, DOMAIN),
)


def _final_validate(config):
    """Checks that need the complete configuration (chip, framework, BLE)."""
    variant = get_esp32_variant()
    if variant != VARIANT_ESP32:
        raise cv.Invalid(
            f"sma_bt_bridge needs an original ESP32 (ESP32-WROOM / ESP32-WROVER, "
            f"e.g. board: esp32dev) because only that chip has Bluetooth Classic "
            f"(BR/EDR). The configured chip is {variant}, which cannot do it."
        )

    if CORE.using_arduino:
        raise cv.Invalid(
            "sma_bt_bridge requires the ESP-IDF framework (it uses the Bluedroid "
            "SPP API directly). Set 'esp32: framework: type: esp-idf'."
        )

    full_config = fv.full_config.get()
    present = [name for name in BLE_COMPONENTS if name in full_config]
    if present:
        names = ", ".join(f"'{name}'" for name in present)
        raise cv.Invalid(
            f"sma_bt_bridge cannot be combined with {names}. This component runs "
            f"the Bluetooth controller in Classic-only (BR/EDR) mode with BLE "
            f"compiled out, while those components need the BLE stack. Remove "
            f"them from this device's configuration; use a second ESP32 if BLE "
            f"is needed as well."
        )
    return config


FINAL_VALIDATE_SCHEMA = _final_validate


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_bt_connect_timeout(config[CONF_BT_CONNECT_TIMEOUT]))
    cg.add(var.set_idle_timeout(config[CONF_IDLE_TIMEOUT]))
    cg.add(var.set_pin(config[CONF_PIN]))

    # Pull the IDF "bt" component into the build and enable the Bluetooth
    # stack, then narrow it down to Classic-only Bluedroid with SPP.
    request_bluetooth()
    if CONF_WIFI in CORE.config:
        # WiFi and Bluetooth share one radio on the ESP32; without the
        # coexistence arbiter, SPP traffic and WiFi starve each other.
        request_software_coexistence()

    add_idf_sdkconfig_option("CONFIG_BT_ENABLED", True)
    add_idf_sdkconfig_option("CONFIG_BT_BLUEDROID_ENABLED", True)
    add_idf_sdkconfig_option("CONFIG_BT_CLASSIC_ENABLED", True)
    add_idf_sdkconfig_option("CONFIG_BT_SPP_ENABLED", True)
    # Controller in BR/EDR-only mode; this also keeps the BLE controller's RAM
    # out of the image.  (The three modes form a Kconfig choice, so the other
    # two are set to false explicitly for clarity.)
    add_idf_sdkconfig_option("CONFIG_BTDM_CTRL_MODE_BR_EDR_ONLY", True)
    add_idf_sdkconfig_option("CONFIG_BTDM_CTRL_MODE_BLE_ONLY", False)
    add_idf_sdkconfig_option("CONFIG_BTDM_CTRL_MODE_BTDM", False)
    # No BLE host stack.  request_bluetooth() defaults the BLE 4.2
    # feature set on; it depends on BT_BLE_ENABLED, so turn it off as well.
    add_idf_sdkconfig_option("CONFIG_BT_BLE_ENABLED", False)
    add_idf_sdkconfig_option("CONFIG_BT_BLE_42_FEATURES_SUPPORTED", False)
    # Headroom for the application SPP/GAP callbacks, which run on the BTC task.
    add_idf_sdkconfig_option("CONFIG_BT_BTC_TASK_STACK_SIZE", 4096)
