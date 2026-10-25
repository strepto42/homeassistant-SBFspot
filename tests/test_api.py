"""The public API contract of ``smabt`` (what the Home Assistant layer relies on)."""

from __future__ import annotations

import dataclasses
import inspect

import pytest
import smabt


def test_exports() -> None:
    expected = {
        "SmaError",
        "SmaConnectionError",
        "BridgeUnreachableError",
        "BridgeBusyError",
        "InverterUnreachableError",
        "SmaTimeoutError",
        "SmaAuthError",
        "SmaProtocolError",
        "DeviceInfo",
        "SpotData",
        "InverterData",
        "Transport",
        "BridgeTransport",
        "async_read_inverter",
        "SmaBluetoothClient",
    }
    assert expected <= set(smabt.__all__)
    for name in expected:
        assert hasattr(smabt, name), name


def test_error_hierarchy() -> None:
    assert issubclass(smabt.SmaConnectionError, smabt.SmaError)
    assert issubclass(smabt.SmaAuthError, smabt.SmaError)
    assert issubclass(smabt.SmaProtocolError, smabt.SmaError)
    for name in (
        "BridgeUnreachableError",
        "BridgeBusyError",
        "InverterUnreachableError",
        "SmaTimeoutError",
    ):
        assert issubclass(getattr(smabt, name), smabt.SmaConnectionError), name
    assert not issubclass(smabt.SmaAuthError, smabt.SmaConnectionError)
    assert not issubclass(smabt.SmaProtocolError, smabt.SmaConnectionError)


def test_dataclass_fields_and_immutability() -> None:
    def fields(cls: type) -> list[str]:
        return [f.name for f in dataclasses.fields(cls)]

    assert fields(smabt.DeviceInfo) == [
        "serial",
        "susy_id",
        "bt_address",
        "name",
        "device_type",
        "device_class",
        "sw_version",
    ]
    assert fields(smabt.SpotData) == [
        "pac_total",
        "pac1",
        "pac2",
        "pac3",
        "uac1",
        "uac2",
        "uac3",
        "iac1",
        "iac2",
        "iac3",
        "pdc1",
        "pdc2",
        "udc1",
        "udc2",
        "idc1",
        "idc2",
        "grid_frequency",
        "e_today",
        "e_total",
        "operating_time",
        "feed_in_time",
        "temperature",
        "bt_signal",
        "status",
        "grid_relay",
    ]
    assert fields(smabt.InverterData) == ["info", "spot"]

    info = smabt.DeviceInfo(serial=1, susy_id=2, bt_address="00:80:25:00:00:01")
    spot = smabt.SpotData()
    for instance in (info, spot, smabt.InverterData(info, spot)):
        assert not hasattr(instance, "__dict__")  # slots=True
        field = dataclasses.fields(instance)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, field, 1)
    assert all(getattr(spot, f.name) is None for f in dataclasses.fields(spot))


def test_async_read_inverter_signature() -> None:
    assert inspect.iscoroutinefunction(smabt.async_read_inverter)
    params = inspect.signature(smabt.async_read_inverter).parameters
    assert list(params) == ["transport", "password", "timeout"]
    assert params["password"].default == "0000"
    assert params["timeout"].default == 10.0
    assert params["timeout"].kind is inspect.Parameter.KEYWORD_ONLY


def test_bridge_transport_signature() -> None:
    params = inspect.signature(smabt.BridgeTransport).parameters
    assert list(params) == [
        "host",
        "port",
        "bt_address",
        "connect_timeout",
        "bt_timeout",
        "busy_timeout",
        "busy_backoff",
    ]
    assert params["port"].default == 9900
    assert params["busy_timeout"].default == 6.0
    assert params["busy_backoff"].default == 0.25
    assert params["connect_timeout"].default == 10.0
    assert params["bt_timeout"].default == 30.0
    assert params["connect_timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["bt_timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    for method in ("open", "read", "write", "close"):
        assert inspect.iscoroutinefunction(getattr(smabt.BridgeTransport, method)), (
            method
        )
