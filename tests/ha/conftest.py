"""Fixtures for the Home Assistant integration tests.

Loaded only when running under pytest-homeassistant-custom-component
(Linux / WSL / CI).  Everything is mocked at the boundary of the integration:

* ``async_read_inverter`` (patched where the coordinator and the config flow
  import it) returns a ``InverterData`` built from the real SB 3000TL-20
  capture of ``tests/test_parse.py``, or raises ``smabt`` errors;
* ``BridgeTransport`` is replaced by a recording mock, so no socket is opened;
* the sun is a patched ``homeassistant.helpers.sun.is_up`` (up by default);
* storage is the ``hass_storage`` mock, so tests never touch the disk.

Note: the integration imports ``custom_components.sbfspot.smabt``; tests must
import the error classes from there too (``tests/conftest.py`` loads another
copy as the top-level package ``smabt`` for the protocol tests, whose classes
are *different* objects).
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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
    InverterData,
    InverterUnreachableError,
    SmaConnectionError,
    SmaTimeoutError,
    SpotData,
)

# The SB 3000TL-20 of the real capture (see tests/test_parse.py).
SERIAL = 2100000042
BT_ADDRESS = "00:80:25:00:00:01"
TITLE = f"SB 3000TL-20 {SERIAL}"
BRIDGE_HOST = "192.168.1.50"
PASSWORD = "0000"

DEVICE_INFO = DeviceInfo(
    serial=SERIAL,
    susy_id=113,
    bt_address=BT_ADDRESS,
    name=f"SN: {SERIAL}",
    device_type="SB 3000TL-20",
    device_class="Solar Inverters",
    sw_version="03.20.01.R",
)

# Values of the real session. Phases 2/3 and string B are NaN on the wire
# (single-phase unit with one string), hence ``None``.
REAL_SPOT: dict[str, Any] = {
    "pac_total": 622.0,
    "pac1": 622.0,
    "uac1": 228.94,
    "iac1": 2.72,
    "pdc1": 643.0,
    "udc1": 336.83,
    "idc1": 1.906,
    "grid_frequency": 49.97,
    "e_today": 4.883,
    "e_total": 60854.831,
    "operating_time": 58826.405,
    "feed_in_time": 57238.146,
    "temperature": 56.96,
    "status": "Ok",
    "grid_relay": "Closed",
    "bt_signal": 185 * 100 / 255,
}


def make_inverter_data(info: DeviceInfo = DEVICE_INFO, **spot: Any) -> InverterData:
    """The captured poll, with *spot* fields overridden."""
    return InverterData(info=info, spot=SpotData(**{**REAL_SPOT, **spot}))


# Every way a poll can fail to reach the inverter: all of them are "offline".
OFFLINE_ERRORS = [
    pytest.param(InverterUnreachableError("asleep"), id="inverter_asleep"),
    pytest.param(SmaTimeoutError("no answer"), id="timeout"),
    pytest.param(BridgeBusyError("busy"), id="bridge_busy"),
    pytest.param(BridgeUnreachableError("refused"), id="bridge_down"),
    pytest.param(SmaConnectionError("dropped"), id="connection_dropped"),
]


@dataclass
class Bridge:
    """The mocked boundary: what the integration calls to reach the inverter."""

    read: AsyncMock
    transport: MagicMock

    @property
    def reads(self) -> int:
        """How many times the inverter was read."""
        return self.read.await_count


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Allow Home Assistant to load this custom integration in tests."""
    yield


@pytest.fixture(autouse=True)
def mock_storage(hass_storage: dict[str, Any]) -> dict[str, Any]:
    """Never touch the disk: the preserved values live in ``hass_storage``."""
    return hass_storage


@pytest.fixture(autouse=True)
def sun_is_up() -> Generator[MagicMock]:
    """The sun helper the coordinator calls; up by default.

    Set ``sun_is_up.return_value = False`` for night, or a ``side_effect``
    taking ``(hass, utc_point_in_time)`` for a real sunrise and sunset.
    """
    with patch("homeassistant.helpers.sun.is_up", return_value=True) as mocked:
        yield mocked


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Enable every entity, including the ones that are disabled by default."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        new_callable=PropertyMock,
        return_value=True,
    ):
        yield


@pytest.fixture
def bridge() -> Generator[Bridge]:
    """Patch ``async_read_inverter`` and ``BridgeTransport`` where they are used."""
    read = AsyncMock(return_value=make_inverter_data())
    transport = MagicMock(name="BridgeTransport")
    with (
        patch("custom_components.sbfspot.coordinator.async_read_inverter", new=read),
        patch("custom_components.sbfspot.config_flow.async_read_inverter", new=read),
        patch("custom_components.sbfspot.coordinator.BridgeTransport", new=transport),
        patch("custom_components.sbfspot.config_flow.BridgeTransport", new=transport),
    ):
        yield Bridge(read=read, transport=transport)


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A configured SB 3000TL-20."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=str(SERIAL),
        data={
            CONF_HOST: BRIDGE_HOST,
            CONF_PORT: 9900,
            CONF_BT_ADDRESS: BT_ADDRESS,
            CONF_PASSWORD: PASSWORD,
        },
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add *entry* to Home Assistant and set it up."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    """The entity id of ``<serial>_<key>`` (registered even when disabled)."""
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{SERIAL}_{key}")
    assert found is not None, f"no {platform} entity {key!r} registered"
    return found


def state_of(hass: HomeAssistant, key: str, platform: str = "sensor") -> str:
    """The state string of an entity, e.g. ``unknown`` or ``unavailable``."""
    state = hass.states.get(entity_id(hass, platform, key))
    assert state is not None, f"{key} has no state (disabled?)"
    return state.state


def number(hass: HomeAssistant, key: str) -> float:
    """The numeric state of a sensor (fails on ``unknown`` / ``unavailable``)."""
    return float(state_of(hass, key))


async def refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Poll once, now (the coordinator's own timer is not involved)."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
