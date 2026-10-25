"""Framing: L1 frames, escaping, FCS-16 and L2 packets, checked against the real
(sanitised) capture."""

from __future__ import annotations

import dataclasses
import random

import pytest
import smabt
from smabt import frame as frame_mod
from smabt.const import (
    ADDR_UNKNOWN,
    ADDR_ZERO,
    CMD_HELLO,
    CMD_L2_FRAGMENT,
    CMD_L2_LAST,
)

from .capture import RX, TX, Capture, fragment_stream


@pytest.fixture(scope="module")
def capture() -> Capture:
    return Capture.load()


# ------------------------------ FCS


def test_fcs16_matches_the_crc16_x25_check_value() -> None:
    assert frame_mod.fcs16(b"123456789") == 0x906E


def test_fcs16_of_nothing_is_zero() -> None:
    assert frame_mod.fcs16(b"") == 0x0000


# ------------------------------ escaping


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        (b"\x7d", b"\x7d\x5d"),
        (b"\x7e", b"\x7d\x5e"),
        (b"\x11", b"\x7d\x31"),
        (b"\x12", b"\x7d\x32"),
        (b"\x13", b"\x7d\x33"),
        (b"\x14\x7d\x00", b"\x14\x7d\x5d\x00"),
        (b"abc", b"abc"),
    ],
)
def test_escape_known_values(raw: bytes, escaped: bytes) -> None:
    assert frame_mod.escape(raw) == escaped
    assert frame_mod.unescape(escaped) == raw


def test_escape_round_trips_every_byte_value() -> None:
    every_byte = bytes(range(256))
    escaped = frame_mod.escape(every_byte)
    assert frame_mod.unescape(escaped) == every_byte
    # Only the escape byte itself may remain among the special characters.
    body = escaped.replace(b"\x7d\x5d", b"").replace(b"\x7d\x5e", b"")
    assert not set(body) & {0x7E, 0x11, 0x12, 0x13}


def test_escape_round_trips_random_data() -> None:
    rng = random.Random(1234)
    for _ in range(200):
        data = bytes(
            rng.choice([0x7D, 0x7E, 0x11, 0x13, 0x00, 0xFF, 0x5D]) for _ in range(40)
        )
        assert frame_mod.unescape(frame_mod.escape(data)) == data


def test_unescape_rejects_a_dangling_escape() -> None:
    with pytest.raises(smabt.SmaProtocolError):
        frame_mod.unescape(b"\x01\x7d")


# ------------------------------ addresses


def test_address_helpers_are_little_endian() -> None:
    wire = frame_mod.address_to_bytes("00:80:25:00:00:01")
    assert wire == bytes.fromhex("010000258000")
    assert frame_mod.address_to_str(wire) == "00:80:25:00:00:01"


@pytest.mark.parametrize("bad", ["", "00:80:25:00:00", "zz:80:25:00:00:01"])
def test_address_helpers_reject_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match="Bluetooth address"):
        frame_mod.address_to_bytes(bad)


# ------------------------------ L1


def test_l1_encode_matches_sbfspots_layout() -> None:
    frame = frame_mod.L1Frame(
        ADDR_ZERO, bytes.fromhex("010000258000"), CMD_HELLO, b"\xaa\xbb"
    )
    wire = frame.encode()
    assert wire[0] == 0x7E
    assert int.from_bytes(wire[1:3], "little") == len(wire) == 20
    assert wire[3] == 0x7E ^ wire[1] ^ wire[2]
    assert wire[4:10] == ADDR_ZERO
    assert wire[10:16] == bytes.fromhex("010000258000")
    assert wire[16:18] == b"\x02\x00"
    assert wire[18:] == b"\xaa\xbb"
    assert frame_mod.L1Frame.decode(wire) == frame


@pytest.mark.parametrize(
    ("head", "message"),
    [
        (b"\x00\x12\x00\x6c", "0x7E"),
        (b"\x7e\x12\x00\x00", "checksum"),
        (b"\x7e\x11\x00\x6f", "length"),  # shorter than the header
        (b"\x7e\x00\x10\x6e", "length"),  # 4096 > COMMBUFSIZE
        (b"\x7e\x12", "truncated"),
    ],
)
def test_l1_header_validation(head: bytes, message: str) -> None:
    with pytest.raises(smabt.SmaProtocolError, match=message):
        frame_mod.frame_length(head)


def test_l1_decode_requires_the_exact_length() -> None:
    wire = frame_mod.L1Frame(ADDR_ZERO, ADDR_ZERO, 1, b"abcd").encode()
    with pytest.raises(smabt.SmaProtocolError):
        frame_mod.L1Frame.decode(wire + b"x")


# ------------------------------ captured frames


def test_every_captured_frame_is_a_valid_l1_frame(capture: Capture) -> None:
    assert len(capture.frames) > 40
    for frame in capture.frames:
        assert frame_mod.frame_length(frame.wire[:4]) == len(frame.wire), frame.index
        decoded = frame.l1
        assert decoded.encode() == frame.wire, frame.index


def test_captured_l1_addresses(capture: Capture) -> None:
    inverter, local = capture.inverter_address, capture.local_address
    for frame in capture.frames:
        l1 = frame.l1
        if frame.direction == RX:
            assert l1.src == inverter, frame.index
        elif frame.index == 1:
            # SBFspot does not know its own address yet when it echoes the hello.
            assert (l1.src, l1.dst) == (ADDR_ZERO, inverter)
        else:
            assert l1.src == local, frame.index
            assert l1.dst in (inverter, ADDR_UNKNOWN), frame.index


def test_every_captured_l2_packet_decodes_with_a_valid_fcs(capture: Capture) -> None:
    l2_messages = [m for m in capture.messages if m.is_l2]
    assert len(l2_messages) == 34  # 18 requests + 16 responses
    for message in l2_messages:
        packet = message.l2  # raises on a bad FCS or length field
        assert packet.packet_id & 0x8000
        assert packet.fcs_bytes() == frame_mod.fcs16(
            frame_mod.unescape(message.stream[1:-1])[:-2]
        ).to_bytes(2, "little")


def test_re_encoding_reproduces_the_captured_wire_bytes(capture: Capture) -> None:
    for message in capture.messages:
        if not message.is_l2:
            continue
        packet = message.l2
        if message.direction == TX:
            # SBFspot: escaped body, raw FCS, never a 0x7D/0x7E in the FCS.
            assert packet.fcs_is_wire_safe()
            assert packet.encode() == message.stream
            first = message.frames[0].l1
            rebuilt = frame_mod.L1Frame(
                first.src, first.dst, first.cmd, packet.encode()
            )
            assert rebuilt.encode() == message.frames[0].wire
        else:
            first = message.frames[0].l1
            assert packet.encode(escape_fcs=True) == message.stream
            rebuilt = fragment_stream(message.stream, first.src, first.dst)
            assert rebuilt == [f.wire for f in message.frames]


def test_multi_part_responses_use_fixed_size_fragments(capture: Capture) -> None:
    multi = [m for m in capture.messages if len(m.frames) > 1]
    assert len(multi) == 4
    for message in multi:
        *head, last = message.frames
        assert all(f.l1.cmd == CMD_L2_FRAGMENT for f in head)
        assert all(len(f.wire) == 109 for f in head)
        assert last.l1.cmd == CMD_L2_LAST
        assert len(last.wire) <= 109


def test_a_corrupted_byte_fails_the_fcs(capture: Capture) -> None:
    checked = 0
    for message in capture.messages:
        if not message.is_l2:
            continue
        stream = message.stream
        for position in range(1, len(stream) - 1):
            corrupted = bytearray(stream)
            corrupted[position] ^= 0x01
            with pytest.raises(smabt.SmaProtocolError):
                frame_mod.L2Packet.decode(bytes(corrupted))
            checked += 1
    assert checked > 2000


def test_l2_decode_needs_both_markers(capture: Capture) -> None:
    stream = capture.messages[3].stream
    with pytest.raises(smabt.SmaProtocolError, match="complete"):
        frame_mod.L2Packet.decode(stream[:-1])
    with pytest.raises(smabt.SmaProtocolError, match="complete"):
        frame_mod.L2Packet.decode(stream[1:])


def test_l2_length_field_must_agree_with_the_packet(capture: Capture) -> None:
    packet = capture.messages[3].l2
    body = bytearray(packet.encode()[1:-1])
    # Declare one word too many and fix up the FCS so only the length is wrong.
    unescaped = bytearray(frame_mod.unescape(bytes(body)))
    body_part = unescaped[:-2]
    body_part[4] += 1
    fcs = frame_mod.fcs16(bytes(body_part)).to_bytes(2, "little")
    stream = (
        b"\x7e" + frame_mod.escape(bytes(body_part)) + frame_mod.escape(fcs) + b"\x7e"
    )
    with pytest.raises(smabt.SmaProtocolError, match="length"):
        frame_mod.L2Packet.decode(stream)


# ------------------------------ FCS corner cases


def find_packet(predicate) -> frame_mod.L2Packet:
    """First L2 request (by packet id) whose FCS satisfies *predicate*."""
    template = frame_mod.L2Packet(
        dst_susy_id=0xFFFF,
        dst_serial=0xFFFFFFFF,
        src_susy_id=125,
        src_serial=974336833,
        packet_id=0x8000,
        payload=b"\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
    )
    for packet_id in range(0x8000, 0x8000 + 20000):
        candidate = dataclasses.replace(template, packet_id=packet_id)
        if predicate(candidate.fcs_bytes()):
            return candidate
    raise AssertionError("no packet id found")


@pytest.mark.parametrize("special", [0x7D, 0x7E])
def test_fcs_that_looks_like_a_marker_is_not_wire_safe(special: int) -> None:
    """SBFspot re-rolls the packet id; we also decode an escaped FCS."""
    packet = find_packet(lambda fcs: special in fcs)
    assert not packet.fcs_is_wire_safe()
    # Escaped FCS: always decodable.
    assert frame_mod.L2Packet.decode(packet.encode(escape_fcs=True)) == packet


def test_a_raw_escape_byte_in_the_fcs_cannot_be_decoded() -> None:
    packet = find_packet(lambda fcs: 0x7D in fcs)
    try:
        decoded = frame_mod.L2Packet.decode(packet.encode())
    except smabt.SmaProtocolError:
        return  # the 0x7D swallowed its neighbour
    assert decoded != packet


@pytest.mark.parametrize("special", [0x11, 0x12, 0x13])
def test_fcs_with_a_flow_control_byte_decodes_raw_and_escaped(special: int) -> None:
    packet = find_packet(
        lambda fcs: special in fcs and 0x7D not in fcs and 0x7E not in fcs
    )
    assert packet.fcs_is_wire_safe()  # SBFspot sends it raw ...
    raw = packet.encode()
    escaped = packet.encode(escape_fcs=True)
    assert raw != escaped  # ... other senders may escape it
    assert frame_mod.L2Packet.decode(raw) == packet
    assert frame_mod.L2Packet.decode(escaped) == packet


def test_l2_encode_round_trips_all_fields() -> None:
    packet = frame_mod.L2Packet(
        dst_susy_id=113,
        dst_serial=2100000042,
        src_susy_id=125,
        src_serial=974336833,
        packet_id=0x8007,
        payload=bytes(range(8, 24)),
        ctrl=0xE0,
        dst_ctrl2=0x0100,
        src_ctrl2=0x0300,
        error=21,
        fragment=2,
    )
    assert frame_mod.L2Packet.decode(packet.encode()) == packet
    assert packet.sequence == 7


def test_l2_payload_must_be_whole_words() -> None:
    packet = frame_mod.L2Packet(1, 2, 3, 4, 0x8001, payload=b"abc")
    with pytest.raises(ValueError, match="multiple of four"):
        packet.encode()


def test_is_l2_start() -> None:
    assert frame_mod.is_l2_start(b"\x7e\xff\x03\x60\x65\x09")
    assert not frame_mod.is_l2_start(b"\x7e\xff\x03\x60")
    assert not frame_mod.is_l2_start(b"\xff\x03\x60\x65")
    assert not frame_mod.is_l2_start(b"")
