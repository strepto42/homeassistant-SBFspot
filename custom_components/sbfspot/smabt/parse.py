"""Decoding of SMA data records into inverter values.

Ported from ``getInverterData()`` and its helpers in SBFspot's ``SBFspot.cpp``
(https://github.com/SBFspot/SBFspot, CC BY-NC-SA 3.0): record layout and size,
the per-LRI ``switch``, ``getattribute()``, ``version_tostring()`` and the NaN
rules of ``nan.h``.

A *query response* is an L2 packet whose payload is::

    command[4] | first[4] | last[4] | record * (last - first + 1)

All records of one response have the same size (``recordsize`` in SBFspot) and
start with ``code[4] timestamp[4]``; ``code & 0xFF`` is the record class (MPPT or
phase number), ``code & 0x00FFFF00`` the LRI and ``code >> 24`` the data type.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

from .const import (
    DT_STATUS,
    DT_STRING,
    NAN_S32,
    NAN_S64,
    NAN_U32,
    NAN_U64,
    TAG_END_OF_LIST,
    Lri,
)
from .errors import SmaProtocolError
from .frame import L2Packet
from .models import SpotData
from .tag_names import TAG_NAMES

_LOGGER = logging.getLogger(__name__)

_RESPONSE_PREFIX = 12  # command, first, last
_ATTRIBUTE_AREA = 40  # getattribute(): `const int recordsize = 40`
_VERSION_TYPES = "NEABRS"  # NOREV-EXPERIMENTAL-ALPHA-BETA-RELEASE-SPECIAL


@dataclass(frozen=True, slots=True)
class Record:
    """One record of a query response."""

    raw: bytes

    @property
    def code(self) -> int:
        return int.from_bytes(self.raw[0:4], "little")

    @property
    def lri(self) -> int:
        """Logical record index (``code & 0x00FFFF00``)."""
        return self.code & 0x00FFFF00

    @property
    def cls(self) -> int:
        """Record class: MPPT number for DC values, phase for AC, ``code & 0xFF``."""
        return self.code & 0xFF

    @property
    def data_type(self) -> int:
        return self.code >> 24

    @property
    def timestamp(self) -> int:
        return int.from_bytes(self.raw[4:8], "little")

    @property
    def size(self) -> int:
        return len(self.raw)


@dataclass(frozen=True, slots=True)
class QueryResponse:
    """A decoded response to ``getInverterData(command, first, last)``."""

    command: int
    first: int
    last: int
    records: tuple[Record, ...]


def parse_response(packet: L2Packet) -> QueryResponse:
    """Split the payload of a query response into records.

    ``recordsize = 4 * (words - 9) / (last - first + 1)`` as in SBFspot, which
    equals ``(len(payload) - 12) / count`` for a well-formed packet.
    """
    payload = packet.payload
    if len(payload) < _RESPONSE_PREFIX:
        raise SmaProtocolError("query response too short")
    command, first, last = struct.unpack_from("<III", payload)
    count = (last - first + 1) & 0xFFFFFFFF
    body = payload[_RESPONSE_PREFIX:]
    if not body:
        return QueryResponse(command, first, last, ())
    if count == 0 or len(body) % count:
        raise SmaProtocolError(
            f"cannot split {len(body)} record bytes into {count} records"
        )
    size = len(body) // count
    if size < 8:
        raise SmaProtocolError(f"implausible record size {size}")
    records = tuple(Record(body[i : i + size]) for i in range(0, len(body), size))
    return QueryResponse(command, first, last, records)


# ------------------------------ values


def value32(record: Record) -> int | None:
    """Signed 32-bit value at offset 16, ``None`` for the NaN sentinels.

    SBFspot (``get_long(recptr + 16)``) treats both ``0x80000000`` and
    ``0xFFFFFFFF`` as NaN for 32-bit records.
    """
    if record.size < 20:
        return None
    (value,) = struct.unpack_from("<i", record.raw, 16)
    if value in (NAN_S32, NAN_U32 - (1 << 32)):
        return None
    return value


def value64(record: Record) -> int | None:
    """Signed 64-bit value at offset 8 of 16-byte records, ``None`` for NaN.

    SBFspot only reads a 64-bit value when ``recordsize == 16``.
    """
    if record.size != 16:
        return None
    (value,) = struct.unpack_from("<q", record.raw, 8)
    if value in (NAN_S64, NAN_U64 - (1 << 64)):
        return None
    return value


def attributes(record: Record) -> list[int]:
    """Selected tags of a status record (``getattribute()``).

    The record carries up to eight 32-bit attributes from offset 8: the low 24 bits
    are a tag id, the top byte is 1 if the tag is currently *set*.  ``0xFFFFFE``
    ends the list.
    """
    tags = []
    for offset in range(8, min(_ATTRIBUTE_AREA, record.size - 3), 4):
        (attribute,) = struct.unpack_from("<I", record.raw, offset)
        tag = attribute & 0x00FFFFFF
        if tag == TAG_END_OF_LIST:
            break
        if attribute >> 24 == 1:
            tags.append(tag)
    return tags


def tag_name(tag: int) -> str | None:
    """English description of a tag id, or ``None`` if unknown/empty."""
    return TAG_NAMES.get(tag) or None


def version_string(version: int) -> str:
    """``version_tostring()``: 0x03200104 -> ``'03.20.01.R'`` (BCD major/minor)."""
    vtype = version & 0xFF
    kind = _VERSION_TYPES[vtype] if vtype < len(_VERSION_TYPES) else "?"
    build = (version >> 8) & 0xFF
    minor = (version >> 16) & 0xFF
    major = (version >> 24) & 0xFF
    return f"{major >> 4}{major & 0x0F}.{minor >> 4}{minor & 0x0F}.{build:02d}.{kind}"


def record_text(record: Record) -> str:
    """NUL-terminated string at offset 8 (``strnlen(recptr + 8, recordsize - 8)``)."""
    return record.raw[8:].split(b"\x00", 1)[0].decode("ascii", errors="replace")


# ------------------------------ accumulator


@dataclass(slots=True)
class DecodedValues:
    """Raw (unscaled) values collected from the records of all queries of a poll.

    ``None`` means: not reported, NaN, or the carrying query failed.
    """

    name: str | None = None
    device_type_tag: int | None = None
    device_class_tag: int | None = None
    sw_version: str | None = None
    status_tag: int | None = None
    grid_relay_tag: int | None = None
    temperature: int | None = None
    pac_total: int | None = None
    pac: dict[int, int | None] = field(default_factory=dict)
    uac: dict[int, int | None] = field(default_factory=dict)
    iac: dict[int, int | None] = field(default_factory=dict)
    pdc: dict[int, int | None] = field(default_factory=dict)
    udc: dict[int, int | None] = field(default_factory=dict)
    idc: dict[int, int | None] = field(default_factory=dict)
    grid_frequency: int | None = None
    e_today: int | None = None
    e_total: int | None = None
    operating_time: int | None = None
    feed_in_time: int | None = None

    def add(self, record: Record) -> None:
        """Apply one record (the ``switch (lri)`` in ``getInverterData``)."""
        lri = record.lri
        cls = record.cls
        is_text = record.data_type in (DT_STRING, DT_STATUS)
        value = None if is_text else value32(record)

        match lri:
            case Lri.GRID_MS_TOT_W:
                self.pac_total = value
            case Lri.GRID_MS_W_PHS_A:
                self.pac[1] = value
            case Lri.GRID_MS_W_PHS_B:
                self.pac[2] = value
            case Lri.GRID_MS_W_PHS_C:
                self.pac[3] = value
            case Lri.GRID_MS_PH_V_PHS_A:
                self.uac[1] = value
            case Lri.GRID_MS_PH_V_PHS_B:
                self.uac[2] = value
            case Lri.GRID_MS_PH_V_PHS_C:
                self.uac[3] = value
            case Lri.GRID_MS_A_PHS_A_1 | Lri.GRID_MS_A_PHS_A:
                self.iac[1] = value
            case Lri.GRID_MS_A_PHS_B_1 | Lri.GRID_MS_A_PHS_B:
                self.iac[2] = value
            case Lri.GRID_MS_A_PHS_C_1 | Lri.GRID_MS_A_PHS_C:
                self.iac[3] = value
            case Lri.GRID_MS_HZ:
                self.grid_frequency = value
            case Lri.DC_MS_WATT:
                self.pdc[cls] = value
            case Lri.DC_MS_VOL:
                self.udc[cls] = value
            case Lri.DC_MS_AMP:
                self.idc[cls] = value
            case Lri.METERING_TOT_WH_OUT:
                self.e_total = value64(record)
            case Lri.METERING_DY_WH_OUT:
                self.e_today = value64(record)
            case Lri.METERING_TOT_OP_TMS:
                self.operating_time = value64(record)
            case Lri.METERING_TOT_FEED_TMS:
                self.feed_in_time = value64(record)
            case Lri.COOLSYS_TMP_NOM:
                self.temperature = value
            case Lri.NAMEPLATE_LOCATION:
                self.name = record_text(record)
            case Lri.NAMEPLATE_PKG_REV:
                if record.size >= 28:
                    (version,) = struct.unpack_from("<I", record.raw, 24)
                    self.sw_version = version_string(version)
            case Lri.NAMEPLATE_MODEL:
                if tags := attributes(record):
                    self.device_type_tag = tags[0]
            case Lri.NAMEPLATE_MAIN_MODEL:
                if tags := attributes(record):
                    self.device_class_tag = tags[0]
            case Lri.OPERATION_HEALTH:
                if tags := attributes(record):
                    self.status_tag = tags[0]
            case Lri.OPERATION_GRI_SW_STT:
                if tags := attributes(record):
                    self.grid_relay_tag = tags[0]
            case _:
                _LOGGER.debug("ignoring record %08X (LRI %08X)", record.code, lri)

    @property
    def device_type(self) -> str | None:
        return _tag_text(self.device_type_tag, "type")

    @property
    def device_class(self) -> str | None:
        return _tag_text(self.device_class_tag, "class")

    @property
    def status(self) -> str | None:
        return _tag_text(self.status_tag, "status")

    @property
    def grid_relay(self) -> str | None:
        return _tag_text(self.grid_relay_tag, "relay state")

    def spot_data(self, bt_signal: float | None) -> SpotData:
        """Scale to SI-ish units exactly like SBFspot's ``toVolt()``, ``toAmp()`` ..."""
        return SpotData(
            pac_total=_scale(self.pac_total, 1),
            pac1=_scale(self.pac.get(1), 1),
            pac2=_scale(self.pac.get(2), 1),
            pac3=_scale(self.pac.get(3), 1),
            uac1=_scale(self.uac.get(1), 100),
            uac2=_scale(self.uac.get(2), 100),
            uac3=_scale(self.uac.get(3), 100),
            iac1=_scale(self.iac.get(1), 1000),
            iac2=_scale(self.iac.get(2), 1000),
            iac3=_scale(self.iac.get(3), 1000),
            pdc1=_scale(self.pdc.get(1), 1),
            pdc2=_scale(self.pdc.get(2), 1),
            udc1=_scale(self.udc.get(1), 100),
            udc2=_scale(self.udc.get(2), 100),
            idc1=_scale(self.idc.get(1), 1000),
            idc2=_scale(self.idc.get(2), 1000),
            grid_frequency=_scale(self.grid_frequency, 100),
            e_today=_scale(self.e_today, 1000),  # Wh -> kWh
            e_total=_scale(self.e_total, 1000),
            operating_time=_scale(self.operating_time, 3600),  # s -> h
            feed_in_time=_scale(self.feed_in_time, 3600),
            temperature=_scale(self.temperature, 100),
            bt_signal=bt_signal,
            status=self.status,
            grid_relay=self.grid_relay,
        )


def _scale(raw: int | None, divisor: int) -> float | None:
    return None if raw is None else raw / divisor


def _tag_text(tag: int | None, what: str) -> str | None:
    """Description of *tag*; unknown ids are reported rather than hidden."""
    if tag is None:
        return None
    return tag_name(tag) or f"Unknown {what} ({tag})"


def signal_percent(payload: bytes) -> float | None:
    """BT signal strength from a ``cmd=4`` payload (``pcktBuf[22] * 100 / 255``)."""
    if len(payload) < 5:
        return None
    return payload[4] * 100.0 / 255.0
