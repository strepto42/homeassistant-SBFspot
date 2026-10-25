"""Result types returned by the SMA Bluetooth client."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Identity of the inverter.

    ``serial``/``susy_id`` come from the logon response, ``bt_address`` is the
    inverter's Bluetooth address (``AA:BB:CC:DD:EE:FF``, as seen in the
    announcement packet).  The text fields are ``None`` when the corresponding
    query failed.
    """

    serial: int
    susy_id: int
    bt_address: str
    name: str | None = None
    device_type: str | None = None
    device_class: str | None = None
    sw_version: str | None = None


@dataclass(frozen=True, slots=True)
class SpotData:
    """One snapshot of the inverter's spot values.

    Every value is ``None`` when the inverter reported SMA's NaN sentinel for it
    (e.g. phases 2 and 3 of a single-phase inverter), did not report it at all,
    or the query that carries it failed.

    Units: power W, voltage V, current A, frequency Hz, energy kWh, times h,
    temperature degrees Celsius, Bluetooth signal percent.
    """

    pac_total: float | None = None
    pac1: float | None = None
    pac2: float | None = None
    pac3: float | None = None
    uac1: float | None = None
    uac2: float | None = None
    uac3: float | None = None
    iac1: float | None = None
    iac2: float | None = None
    iac3: float | None = None
    pdc1: float | None = None
    pdc2: float | None = None
    udc1: float | None = None
    udc2: float | None = None
    idc1: float | None = None
    idc2: float | None = None
    grid_frequency: float | None = None
    e_today: float | None = None
    e_total: float | None = None
    operating_time: float | None = None
    feed_in_time: float | None = None
    temperature: float | None = None
    bt_signal: float | None = None
    status: str | None = None
    grid_relay: str | None = None


@dataclass(frozen=True, slots=True)
class InverterData:
    """Everything one poll returns."""

    info: DeviceInfo
    spot: SpotData
