"""Record decoding: the real captured responses and the SMA NaN rules."""

from __future__ import annotations

import struct

import pytest
import smabt
from smabt import parse
from smabt.client import encode_password
from smabt.const import (
    CMD_SIGNAL_RESPONSE,
    DEVICE_INFO_QUERIES,
    SPOT_QUERIES,
    Lri,
)
from smabt.tag_names import TAG_NAMES

from .capture import RX, Capture


@pytest.fixture(scope="module")
def capture() -> Capture:
    return Capture.load()


# ------------------------------ helpers


def query_responses(
    capture: Capture,
) -> list[tuple[tuple[int, int, int], parse.QueryResponse | int]]:
    """``[(request (command, first, last), response or packet status)]`` in order."""
    result = []
    for request, replies in capture.exchanges():
        if not request.is_l2:
            continue
        payload = request.l2.payload
        if len(payload) != 12 or payload[:4] == bytes.fromhex("00020000"):
            continue  # identify / logoff
        key = struct.unpack("<III", payload)
        assert len(replies) == 1
        packet = replies[0].l2
        result.append(
            (key, parse.parse_response(packet) if packet.error == 0 else packet.error)
        )
    return result


def decode_session(capture: Capture) -> parse.DecodedValues:
    values = parse.DecodedValues()
    for _, response in query_responses(capture):
        if isinstance(response, parse.QueryResponse):
            for record in response.records:
                values.add(record)
    return values


def captured_signal(capture: Capture) -> float | None:
    for message in capture.messages:
        if message.direction == RX and message.frames[0].l1.cmd == CMD_SIGNAL_RESPONSE:
            return parse.signal_percent(message.frames[0].l1.payload)
    raise AssertionError("no signal response in the fixture")


def record(code: int, *values: int, size: int = 40, fmt: str = "<i") -> parse.Record:
    """A record with *values* packed (little endian) from offset 8."""
    raw = bytearray(size)
    raw[0:4] = code.to_bytes(4, "little")
    raw[4:8] = (1_700_000_000).to_bytes(4, "little")
    offset = 8
    for value in values:
        struct.pack_into(fmt, raw, offset, value)
        offset += struct.calcsize(fmt)
    return parse.Record(bytes(raw))


def dword_record(lri: Lri, value: int, cls: int = 1) -> parse.Record:
    """28-byte record (four identical values), as the inverter sends for spot values."""
    return record(0x40000000 | lri | cls, value, value, value, value, size=28)


# ------------------------------ real session


def test_the_client_issues_the_captured_queries_in_order(capture: Capture) -> None:
    captured = [key for key, _ in query_responses(capture)]
    expected = [
        (q.command, q.first, q.last) for q in (*DEVICE_INFO_QUERIES, *SPOT_QUERIES)
    ]
    assert captured == expected


def test_spot_values_of_the_real_session(capture: Capture) -> None:
    spot = decode_session(capture).spot_data(captured_signal(capture))

    # What the inverter really reported.
    assert spot.pac_total == 622
    assert spot.pac1 == 622
    assert spot.uac1 == pytest.approx(228.94)
    assert spot.iac1 == pytest.approx(2.720)
    assert spot.pdc1 == 643
    assert spot.udc1 == pytest.approx(336.83)
    assert spot.idc1 == pytest.approx(1.906)
    assert spot.grid_frequency == pytest.approx(49.97)
    assert spot.e_today == pytest.approx(4.883)
    assert spot.e_total == pytest.approx(60854.831)
    assert spot.operating_time == pytest.approx(58826.405, abs=1e-3)
    assert spot.feed_in_time == pytest.approx(57238.146, abs=1e-3)
    assert spot.temperature == pytest.approx(56.96)  # SBFspot prints it as 57.0
    assert spot.status == "Ok"
    assert spot.grid_relay == "Closed"
    assert spot.bt_signal == pytest.approx(72.5, abs=0.1)  # 185 * 100 / 255

    # SBFspot prints 0 for these because it maps NaN to 0.  The inverter sent the
    # NaN sentinel (single-phase unit) or no record at all (second MPPT).
    for name in ("pac2", "pac3", "uac2", "uac3", "iac2", "iac3"):
        assert getattr(spot, name) is None, name
    for name in ("pdc2", "udc2", "idc2"):
        assert getattr(spot, name) is None, name


def test_the_unused_phases_are_nan_on_the_wire(capture: Capture) -> None:
    """Document *why* phases 2 and 3 are None."""
    by_lri = {}
    for _, response in query_responses(capture):
        if isinstance(response, parse.QueryResponse):
            for rec in response.records:
                by_lri[rec.lri] = rec
    pac2 = by_lri[Lri.GRID_MS_W_PHS_B]
    assert struct.unpack_from("<I", pac2.raw, 16)[0] == 0x80000000  # NaN_S32
    uac2 = by_lri[Lri.GRID_MS_PH_V_PHS_B]
    assert struct.unpack_from("<I", uac2.raw, 16)[0] == 0xFFFFFFFF  # NaN_U32
    assert Lri.DC_MS_WATT in by_lri
    assert [r.cls for r in query_responses(capture)[8][1].records] == [1]  # one MPPT


def test_device_info_of_the_real_session(capture: Capture) -> None:
    values = decode_session(capture)
    assert values.name == "SN: 2100000042"
    assert values.device_type == "SB 3000TL-20"
    assert values.device_class == "Solar Inverters"
    assert values.sw_version == "03.20.01.R"


def test_real_status_21_answer_is_reported_as_a_status(capture: Capture) -> None:
    statuses = [r for _, r in query_responses(capture) if isinstance(r, int)]
    assert statuses == [21]


def test_raw_integer_values_of_the_real_session(capture: Capture) -> None:
    values = decode_session(capture)
    assert values.uac == {1: 22894, 2: None, 3: None}
    assert values.iac == {1: 2720, 2: None, 3: None}
    assert values.e_total == 60_854_831  # Wh
    assert values.e_today == 4_883
    assert values.grid_frequency == 4997
    assert values.temperature == 5696


def test_signal_strength_is_a_percentage_of_255(capture: Capture) -> None:
    assert captured_signal(capture) == pytest.approx(185 * 100 / 255)


# ------------------------------ NaN sentinels


@pytest.mark.parametrize("raw", [0x80000000, 0xFFFFFFFF])
def test_32_bit_nan_sentinels_are_none(raw: int) -> None:
    data = bytearray(28)
    data[16:20] = struct.pack("<I", raw)
    assert parse.value32(parse.Record(bytes(data))) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0),
        (1, 1),
        (0xFFFFFFFE, -2),
        (0x7FFFFFFF, 2147483647),
        (0x80000001, -2147483647),
    ],
)
def test_32_bit_values_are_signed_and_only_nan_is_none(raw: int, expected: int) -> None:
    data = bytearray(28)
    data[16:20] = struct.pack("<I", raw)
    assert parse.value32(parse.Record(bytes(data))) == expected


@pytest.mark.parametrize("raw", [0x8000000000000000, 0xFFFFFFFFFFFFFFFF])
def test_64_bit_nan_sentinels_are_none(raw: int) -> None:
    data = bytearray(16)
    data[8:16] = struct.pack("<Q", raw)
    assert parse.value64(parse.Record(bytes(data))) is None


def test_64_bit_values() -> None:
    data = bytearray(16)
    data[8:16] = struct.pack("<Q", 60_854_831)
    assert parse.value64(parse.Record(bytes(data))) == 60_854_831
    data[8:16] = struct.pack("<Q", 0)
    assert parse.value64(parse.Record(bytes(data))) == 0


def test_64_bit_values_need_a_16_byte_record() -> None:
    assert parse.value64(parse.Record(bytes(28))) is None  # SBFspot: recordsize == 16
    assert parse.value32(parse.Record(bytes(16))) is None  # too short for offset 16


@pytest.mark.parametrize("sentinel", [-0x80000000, -1], ids=["NaN_S32", "NaN_U32"])
def test_nan_records_become_none_not_zero(sentinel: int) -> None:
    values = parse.DecodedValues()
    for lri in (Lri.GRID_MS_TOT_W, Lri.GRID_MS_HZ, Lri.COOLSYS_TMP_NOM, Lri.DC_MS_WATT):
        values.add(dword_record(lri, sentinel))
    spot = values.spot_data(None)
    assert spot.pac_total is None
    assert spot.grid_frequency is None
    assert spot.temperature is None
    assert spot.pdc1 is None


@pytest.mark.parametrize("sentinel", [0x8000000000000000, 0xFFFFFFFFFFFFFFFF])
def test_nan_energy_and_time_become_none(sentinel: int) -> None:
    values = parse.DecodedValues()
    for lri in (
        Lri.METERING_TOT_WH_OUT,
        Lri.METERING_DY_WH_OUT,
        Lri.METERING_TOT_OP_TMS,
        Lri.METERING_TOT_FEED_TMS,
    ):
        values.add(record(0x00000001 | lri, sentinel, size=16, fmt="<Q"))
    spot = values.spot_data(None)
    assert (spot.e_total, spot.e_today, spot.operating_time, spot.feed_in_time) == (
        None,
        None,
        None,
        None,
    )


# ------------------------------ units


def test_units_follow_sbfspot() -> None:
    values = parse.DecodedValues()
    values.add(dword_record(Lri.GRID_MS_TOT_W, 1234))
    values.add(dword_record(Lri.GRID_MS_PH_V_PHS_B, 23001))
    values.add(dword_record(Lri.GRID_MS_A_PHS_C_1, 1500))
    values.add(dword_record(Lri.GRID_MS_A_PHS_A, 2720))  # alternate current LRI
    values.add(dword_record(Lri.GRID_MS_HZ, 5001))
    values.add(dword_record(Lri.COOLSYS_TMP_NOM, -1250))
    values.add(dword_record(Lri.DC_MS_WATT, 400, cls=2))
    values.add(dword_record(Lri.DC_MS_VOL, 31000, cls=2))
    values.add(dword_record(Lri.DC_MS_AMP, 1250, cls=2))
    values.add(record(0x1 | Lri.METERING_TOT_WH_OUT, 1500, size=16, fmt="<Q"))
    values.add(record(0x1 | Lri.METERING_TOT_OP_TMS, 7200, size=16, fmt="<Q"))
    spot = values.spot_data(50.0)

    assert spot.pac_total == 1234
    assert spot.uac2 == pytest.approx(230.01)
    assert spot.iac3 == pytest.approx(1.5)
    assert spot.iac1 == pytest.approx(2.72)
    assert spot.grid_frequency == pytest.approx(50.01)
    assert spot.temperature == pytest.approx(-12.5)
    assert (spot.pdc2, spot.udc2, spot.idc2) == (
        400,
        pytest.approx(310.0),
        pytest.approx(1.25),
    )
    assert spot.e_total == pytest.approx(1.5)  # Wh -> kWh
    assert spot.operating_time == pytest.approx(2.0)  # s -> h
    assert spot.bt_signal == 50.0


def test_unknown_lri_is_ignored() -> None:
    values = parse.DecodedValues()
    values.add(record(0x00123401, 5, 6, 7, 8))
    assert values == parse.DecodedValues()


# ------------------------------ strings, tags


def status_record(lri: Lri, *attributes: int) -> parse.Record:
    raw = bytearray(40)
    raw[0:4] = (0x08000001 | lri).to_bytes(4, "little")
    for index, attribute in enumerate(attributes):
        struct.pack_into("<I", raw, 8 + 4 * index, attribute)
    return parse.Record(bytes(raw))


def test_status_record_reports_the_first_selected_tag() -> None:
    rec = status_record(Lri.OPERATION_HEALTH, 35, 0x01000133, 0x01000023, 0x00FFFFFE)
    assert parse.attributes(rec) == [0x133, 0x23]
    values = parse.DecodedValues()
    values.add(rec)
    assert values.status == "Ok"  # tag 307


def test_attribute_list_stops_at_the_end_marker() -> None:
    rec = status_record(Lri.OPERATION_HEALTH, 0x00FFFFFE, 0x01000133)
    assert parse.attributes(rec) == []


def test_status_without_a_selected_tag_is_none() -> None:
    values = parse.DecodedValues()
    values.add(status_record(Lri.OPERATION_GRI_SW_STT, 0x00000033, 0x00FFFFFE))
    assert values.grid_relay is None


def test_unknown_tags_are_reported_with_their_id() -> None:
    values = parse.DecodedValues()
    values.add(status_record(Lri.NAMEPLATE_MODEL, 0x01FFFF00))
    values.add(status_record(Lri.NAMEPLATE_MAIN_MODEL, 0x01FFFF01))
    values.add(status_record(Lri.OPERATION_HEALTH, 0x01FFFF02))
    assert "16776960" in (values.device_type or "")
    assert "16776961" in (values.device_class or "")
    assert "16776962" in (values.status or "")


def test_tag_names_cover_the_sbfspot_tag_list() -> None:
    assert len(TAG_NAMES) > 8000
    assert TAG_NAMES[307] == "Ok"
    assert TAG_NAMES[51] == "Closed"
    assert TAG_NAMES[558] == "SB 3000TL-20"
    assert TAG_NAMES[8001] == "Solar Inverters"
    assert TAG_NAMES[2] == "°C"


@pytest.mark.parametrize(
    ("version", "text"),
    [
        (0x03200104, "03.20.01.R"),
        (0x12340000, "12.34.00.N"),  # BCD: 0x12 is printed as "12", not "18"
        (0x01020305, "01.02.03.S"),
        (0x01020301, "01.02.03.E"),
        (0x01020306, "01.02.03.?"),
    ],
)
def test_version_string(version: int, text: str) -> None:
    assert parse.version_string(version) == text


def test_record_text() -> None:
    raw = bytearray(40)
    raw[8:22] = b"SN: 2100000042"
    assert parse.record_text(parse.Record(bytes(raw))) == "SN: 2100000042"
    assert parse.record_text(parse.Record(b"\0" * 8 + b"A" * 32)) == "A" * 32


def test_software_version_record() -> None:
    raw = bytearray(40)
    raw[0:4] = (Lri.NAMEPLATE_PKG_REV | 1).to_bytes(4, "little")
    raw[24:28] = (0x03200104).to_bytes(4, "little")
    values = parse.DecodedValues()
    values.add(parse.Record(bytes(raw)))
    assert values.sw_version == "03.20.01.R"


# ------------------------------ response layout


def test_parse_response_splits_records_by_the_range() -> None:
    packet = smabt.frame.L2Packet(
        1, 2, 3, 4, 0x8001, struct.pack("<III", 0x51000201, 4, 6) + bytes(range(24)) * 3
    )
    # 72 bytes / 3 records = 24 bytes per record
    response = parse.parse_response(packet)
    assert (response.command, response.first, response.last) == (0x51000201, 4, 6)
    assert [len(r.raw) for r in response.records] == [24, 24, 24]


def test_parse_response_without_records() -> None:
    packet = smabt.frame.L2Packet(1, 2, 3, 4, 0x8001, struct.pack("<III", 1, 0, 0))
    assert parse.parse_response(packet).records == ()


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00" * 8,  # shorter than command/first/last
        struct.pack("<III", 1, 0, 2) + bytes(40),  # 40 bytes into 3 records
        struct.pack("<III", 1, 5, 4) + bytes(40),  # last < first
        struct.pack("<III", 1, 0, 9) + bytes(40),  # 4-byte records
    ],
)
def test_parse_response_rejects_inconsistent_packets(payload: bytes) -> None:
    packet = smabt.frame.L2Packet(1, 2, 3, 4, 0x8001, payload)
    with pytest.raises(smabt.SmaProtocolError):
        parse.parse_response(packet)


def test_signal_percent() -> None:
    assert parse.signal_percent(bytes([5, 0, 0, 0, 255, 0])) == 100.0
    assert parse.signal_percent(bytes([5, 0, 0, 0, 0, 0])) == 0.0
    assert parse.signal_percent(b"\x05\x00") is None


# ------------------------------ password


def test_password_encoding_matches_sbfspot() -> None:
    assert encode_password("0000").hex() == "b8b8b8b8" + "88" * 8
    assert encode_password("").hex() == "88" * 12
    assert encode_password("0000", installer=True).hex() == "ebebebeb" + "bb" * 8
    assert len(encode_password("x" * 30)) == 12  # truncated like SBFspot
    assert encode_password("abc")[:3] == bytes((ord(c) + 0x88) & 0xFF for c in "abc")
