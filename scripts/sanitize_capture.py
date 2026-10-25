#!/usr/bin/env python3
"""Turn a raw ``SBFspot -d5 -v5`` Bluetooth session log into a sanitised fixture.

The raw log contains the inverter's Bluetooth MAC, the local adapter's MAC, the
inverter serial number and the (XOR-"encrypted") logon password.  None of that
may be committed.  This script reads the log, *detects* those values from the
structure of the log itself (nothing is hard-coded), replaces them with
documentation-grade placeholders and writes ``tests/fixtures/sb3000tl20_session.json``.

Every frame is decoded, substituted, and re-encoded with correct lengths,
escaping and FCS, so the fixture is a self-consistent wire capture::

    python scripts/sanitize_capture.py path/to/raw_d5.log [output.json]

Placeholders
------------
* inverter Bluetooth MAC -> ``00:80:25:00:00:01``
* local adapter MAC      -> ``02:00:00:00:00:02``
* serial number          -> ``2100000042`` (binary little-endian and ASCII)
* logon password         -> the encoding of the default user password ``0000``

What SBFspot's debug log does (and does not) contain
----------------------------------------------------
Sent packets (``-d5``) are printed as the *escaped wire bytes* including the
18-byte Bluetooth L1 header.  Received packets are printed from SBFspot's
``pcktBuf``: for the L1-only commands (2, 4, 5) that is the raw frame, but for
SMA-net-2 (L2) packets it is the *unescaped* L2 packet with the L1 header
stripped and multi-part (cmd 8 ... cmd 1) responses already concatenated.  For
those the script rebuilds the L1 frames (source = inverter, destination = local
adapter, command 8/1) and verifies the rebuilt lengths against the
``PacketLength=`` lines in the log.  Such frames are marked
``"origin": "reconstructed"`` in the fixture.  Frames that the log mentions but
does not dump (cmd 10 and 12 between the hello and the topology packet) are only
listed under ``meta.uncaptured``.

Protocol background: SBFspot (https://github.com/SBFspot/SBFspot, CC BY-NC-SA 3.0).
This script is deliberately self-contained (standard library only, own FCS/escape
implementation) so that the fixture is produced independently of the code under
test.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

L1_HEADER = 18
L2_SIGNATURE = bytes.fromhex("ff036065")
ESCAPE_BYTES = frozenset({0x7D, 0x7E, 0x11, 0x12, 0x13})

# Placeholders written into the fixture.
NEW_INVERTER_MAC = "00:80:25:00:00:01"
NEW_LOCAL_MAC = "02:00:00:00:00:02"
NEW_SERIAL = 2100000042
NEW_PASSWORD = "0000"
USER_ENC_CHAR = 0x88  # SBFspot logonSMAInverter(): encChar for the "user" group
PW_FIELD_LEN = 12

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1] / "tests/fixtures/sb3000tl20_session.json"
)

_ROW = re.compile(r"^\d{8}: ((?:[0-9A-F]{2} ?)+)\s*$")
_DUMP_HEADER = "--------: 00 01 02 03 04 05 06 07 08 09"
_RECEIVED_MARKER = "<<<====== Content of pcktBuf =======>>>"


# ------------------------------ codec


def _fcs_table() -> list[int]:
    """CRC-16/X-25 (PPP FCS-16) lookup table, identical to SBFspot's ``fcstab``."""
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_FCS_TABLE = _fcs_table()


def fcs16(data: bytes) -> int:
    """FCS-16 over *data* (initial 0xFFFF, final XOR 0xFFFF)."""
    fcs = 0xFFFF
    for byte in data:
        fcs = (fcs >> 8) ^ _FCS_TABLE[(fcs ^ byte) & 0xFF]
    return fcs ^ 0xFFFF


def escape(data: bytes) -> bytes:
    """SBFspot ``writeByte()`` escaping: 0x7D/0x7E/0x11/0x12/0x13 -> 0x7D, b ^ 0x20."""
    out = bytearray()
    for byte in data:
        if byte in ESCAPE_BYTES:
            out += bytes((0x7D, byte ^ 0x20))
        else:
            out.append(byte)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    """Inverse of :func:`escape`."""
    out = bytearray()
    it = iter(data)
    for byte in it:
        if byte == 0x7D:
            out.append(next(it) ^ 0x20)
        else:
            out.append(byte)
    return bytes(out)


def l1_frame(src: bytes, dst: bytes, cmd: int, payload_wire: bytes) -> bytes:
    """Build an L1 frame; *payload_wire* is already escaped where needed."""
    length = L1_HEADER + len(payload_wire)
    head = bytes((0x7E, length & 0xFF, length >> 8))
    chk = head[0] ^ head[1] ^ head[2]
    return head + bytes((chk,)) + src + dst + cmd.to_bytes(2, "little") + payload_wire


def mac_to_le(mac: str) -> bytes:
    return bytes(int(part, 16) for part in reversed(mac.split(":")))


def encode_password(password: str) -> bytes:
    raw = bytearray(PW_FIELD_LEN * [USER_ENC_CHAR])
    for idx, char in enumerate(password.encode("ascii")):
        raw[idx] = (char + USER_ENC_CHAR) & 0xFF
    return bytes(raw)


# ------------------------------ L2 view

# Offsets inside the L2 *body*: the bytes between the leading 0x7E and the FCS.
BODY_SIGNATURE = slice(0, 4)
BODY_PACKET_ID = slice(26, 28)
BODY_PAYLOAD = 28


@dataclass
class L2:
    """An SMA-net-2 packet; ``body`` is unescaped, without 0x7E markers and FCS."""

    body: bytes

    @property
    def packet_id(self) -> int:
        return int.from_bytes(self.body[BODY_PACKET_ID], "little") & 0x7FFF

    def with_packet_id(self, packet_id: int) -> L2:
        raw = bytearray(self.body)
        raw[BODY_PACKET_ID] = (packet_id | 0x8000).to_bytes(2, "little")
        return L2(bytes(raw))

    def fcs_bytes(self) -> bytes:
        return fcs16(self.body).to_bytes(2, "little")


def parse_l2_unescaped(buf: bytes) -> L2:
    """Parse SBFspot's ``pcktBuf`` view: 7E <body> <fcs> 7E, already unescaped."""
    if buf[0] != 0x7E or buf[-1] != 0x7E or buf[1:5] != L2_SIGNATURE:
        raise ValueError("not an L2 packet")
    body, fcs = buf[1:-3], buf[-3:-1]
    if fcs16(body).to_bytes(2, "little") != fcs:
        raise ValueError("FCS mismatch in captured L2 packet")
    return L2(body)


# ---------------------------------------- log parsing


@dataclass
class Message:
    """One logical message of the session, in log order."""

    direction: str  # "tx" (client -> inverter) or "rx"
    hdr: bytes = b""  # 18-byte L1 header (tx and L1-only rx)
    payload: bytes = b""  # unescaped L1 payload (L1-only messages)
    l2: L2 | None = None
    # rx L2 only: unescaped length of every L1 fragment's payload and its L1 cmd
    fragments: list[tuple[int, int]] = field(default_factory=list)
    origin: str = "wire"


@dataclass
class Log:
    inverter_mac: str
    local_mac: str
    serial: int
    messages: list[Message]
    uncaptured: list[dict[str, int]]


def _read_dump(lines: list[str], idx: int) -> tuple[bytes, int]:
    """Read a hex dump starting at the header line *idx*; return (bytes, next idx)."""
    assert lines[idx].strip() == _DUMP_HEADER
    idx += 1
    out = bytearray()
    while idx < len(lines):
        match = _ROW.match(lines[idx])
        if not match:
            break
        out += bytes.fromhex(match.group(1))
        idx += 1
    return bytes(out), idx


def _detect(text: str, pattern: str, what: str) -> re.Match[str]:
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise SystemExit(f"cannot detect {what} in the log (pattern not found)")
    return match


def parse_log(text: str) -> Log:
    inverter_mac = _detect(
        text, r"^Connecting to ([0-9A-Fa-f:]{17})", "inverter MAC"
    ).group(1)
    local_mac = _detect(
        text, r"^Local BT address: ([0-9A-Fa-f:]{17})", "local MAC"
    ).group(1)
    serial = int(_detect(text, r"^Serial Nr: [0-9A-F]{8} \((\d+)\)", "serial").group(1))

    lines = text.splitlines()
    messages: list[Message] = []
    uncaptured: list[dict[str, int]] = []
    pending: list[dict[str, int]] = []  # L1 frames accepted by the current getPacket()
    received: list[int] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("getPacket("):
            pending, received = [], []
        elif match := re.match(r"Received (\d+) bytes", line):
            received.append(int(match.group(1)))
        elif match := re.match(r"cmd=(\d+)$", line):
            pending.append({"cmd": int(match.group(1)), "length": sum(received)})
            received = []
        elif match := re.match(r"PacketLength=(\d+)$", line):
            pending[-1]["pkt_len"] = int(match.group(1))
        elif line == _RECEIVED_MARKER:
            dump, idx = _read_dump(lines, idx + 1)
            messages.append(_received_message(dump, pending, uncaptured, len(messages)))
            idx -= 1  # the loop increments again
        elif line == _DUMP_HEADER:
            dump, idx = _read_dump(lines, idx)
            sent = re.match(r"(\d+) Bytes sent$", lines[idx])
            if not sent or int(sent.group(1)) != len(dump):
                raise SystemExit(
                    "sent dump is not followed by a matching 'Bytes sent' line"
                )
            messages.append(_sent_message(dump))
            idx -= 1
        idx += 1
    return Log(inverter_mac, local_mac, serial, messages, uncaptured)


def _is_l2_payload(payload_wire: bytes) -> bool:
    return payload_wire[:1] == b"\x7e" and payload_wire[1:5] == L2_SIGNATURE


def _sent_message(wire: bytes) -> Message:
    if wire[0] != 0x7E or int.from_bytes(wire[1:3], "little") != len(wire):
        raise SystemExit("sent frame length does not match its L1 header")
    hdr, payload_wire = wire[:L1_HEADER], wire[L1_HEADER:]
    if _is_l2_payload(payload_wire):
        # 7E <escaped body> <raw FCS> 7E  (SBFspot writePacketTrailer())
        body = unescape(payload_wire[1:-3])
        l2 = L2(body)
        if payload_wire[-3:-1] != l2.fcs_bytes() or payload_wire[-1] != 0x7E:
            raise SystemExit("captured sent L2 packet has an invalid FCS")
        return Message("tx", hdr=hdr, l2=l2)
    return Message("tx", hdr=hdr, payload=unescape(payload_wire))


def _received_message(
    dump: bytes,
    pending: list[dict[str, int]],
    uncaptured: list[dict[str, int]],
    index: int,
) -> Message:
    last = pending[-1]
    l2_frames = [frame for frame in pending if "pkt_len" in frame]
    if not l2_frames:
        # L1-only packet: the dump is the raw frame.
        if len(dump) != last["length"]:
            raise SystemExit("received L1 dump does not match the logged length")
        for frame in pending[:-1]:
            uncaptured.append({"before_message": index, **frame})
        return Message("rx", hdr=dump[:L1_HEADER], payload=dump[L1_HEADER:])
    l2 = parse_l2_unescaped(dump)
    fragments = [(frame["cmd"], frame["pkt_len"] - L1_HEADER) for frame in l2_frames]
    return Message(
        "rx", l2=l2, fragments=[(c, n) for c, n in fragments], origin="reconstructed"
    )


# ------------------------------ rewriting


@dataclass
class Substitutions:
    pairs: list[tuple[bytes, bytes]]

    def apply(self, data: bytes) -> bytes:
        for old, new in self.pairs:
            data = data.replace(old, new)
        return data


def find_password_field(messages: list[Message]) -> bytes:
    """The 12 encoded password bytes of the first logon request (cmd 0xFFFD040C)."""
    for msg in messages:
        if msg.direction == "tx" and msg.l2 is not None:
            payload = msg.l2.body[BODY_PAYLOAD:]
            if payload[:4] == bytes.fromhex("0c04fdff") and len(payload) == 32:
                return payload[-PW_FIELD_LEN:]
    raise SystemExit("no logon request found in the log")


def build_wire(
    messages: list[Message], subs: Substitutions, src_local: bytes, src_inverter: bytes
) -> tuple[list[dict[str, str]], list[int], int]:
    """Substitute and re-encode every message.

    Returns ``(frames, first_frame_of_message, id_shift)``.  ``id_shift`` is how
    many packet ids SBFspot would have skipped because a re-computed FCS
    contained 0x7D/0x7E (``isCrcValid()``); responses are renumbered to match.
    """
    frames: list[dict[str, str]] = []
    first_frame: list[int] = []
    id_shift = 0
    for msg in messages:
        first_frame.append(len(frames))
        if msg.direction == "tx":
            hdr = subs.apply(msg.hdr)
            if msg.l2 is None:
                payload = subs.apply(msg.payload)
                wire = l1_frame_from_header(hdr, escape(payload))
            else:
                body = L2(subs.apply(msg.l2.body))
                while True:
                    shifted = body.with_packet_id(msg.l2.packet_id + id_shift)
                    fcs = shifted.fcs_bytes()
                    if 0x7D in fcs or 0x7E in fcs:  # SBFspot would re-roll the id
                        id_shift += 1
                        continue
                    break
                payload_wire = b"\x7e" + escape(shifted.body) + fcs + b"\x7e"
                wire = l1_frame_from_header(hdr, payload_wire)
            frames.append({"dir": "tx", "origin": "wire", "hex": wire.hex()})
        elif msg.l2 is None:
            hdr = subs.apply(msg.hdr)
            wire = l1_frame_from_header(hdr, subs.apply(msg.payload))
            frames.append({"dir": "rx", "origin": "wire", "hex": wire.hex()})
        else:
            body = L2(subs.apply(msg.l2.body))
            if id_shift:
                body = body.with_packet_id(msg.l2.packet_id + id_shift)
            frames.extend(_rx_fragments(msg, body, src_inverter, src_local))
    return frames, first_frame, id_shift


def l1_frame_from_header(hdr: bytes, payload_wire: bytes) -> bytes:
    return l1_frame(
        hdr[4:10], hdr[10:16], int.from_bytes(hdr[16:18], "little"), payload_wire
    )


def _rx_fragments(
    msg: Message, body: L2, inverter: bytes, local: bytes
) -> list[dict[str, str]]:
    """Re-split a received L2 packet into L1 fragments like the inverter sent it.

    The logged ``PacketLength`` of each original fragment tells how many *wire*
    bytes it carried.  That gives the logical (unescaped) cut points of the
    original; the same cuts are then applied to the substituted packet, whose
    escaped fragment lengths may differ.
    """
    original = msg.l2
    assert original is not None
    sizes = [size for _, size in msg.fragments]
    logical_orig = original.body + original.fcs_bytes()
    if 2 + len(escape(logical_orig)) != sum(sizes):
        raise SystemExit(
            "re-escaped received packet does not match the logged PacketLength; "
            "the SBFspot log layout differs from what this script expects"
        )
    cuts = _logical_cuts(logical_orig, sizes)

    logical_new = body.body + body.fcs_bytes()
    frames = []
    bounds = [0, *cuts, len(logical_new)]
    last = len(msg.fragments) - 1
    for n, (cmd, _) in enumerate(msg.fragments):
        payload = escape(logical_new[bounds[n] : bounds[n + 1]])
        if n == 0:
            payload = b"~" + payload
        if n == last:
            payload += b"~"
        wire = l1_frame(inverter, local, cmd, payload)
        frames.append({"dir": "rx", "origin": "reconstructed", "hex": wire.hex()})
    return frames


def _logical_cuts(logical: bytes, sizes: list[int]) -> list[int]:
    """Logical offsets at which the escaped stream was cut into fragments *sizes*.

    The first fragment also carries the leading 0x7E marker.  Raises if a cut
    would fall inside an escape pair (SBFspot could not reassemble that either).
    """
    cuts: list[int] = []
    wire = 1  # leading 0x7E
    target = sizes[0]
    for count, byte in enumerate(logical, start=1):
        wire += 2 if byte in ESCAPE_BYTES else 1
        if wire == target and len(cuts) < len(sizes) - 1:
            cuts.append(count)
            target += sizes[len(cuts)]
        elif wire > target:
            raise SystemExit("fragment boundary falls inside an escape sequence")
    return cuts


def _leak_patterns(log: Log, password_field: bytes) -> dict[str, list[bytes]]:
    """Byte/text patterns of every sensitive value, in all notations we know of."""
    serial = log.serial
    inv, loc = mac_to_le(log.inverter_mac), mac_to_le(log.local_mac)
    return {
        "inverter MAC": [
            inv,
            inv[::-1],
            log.inverter_mac.encode(),
            log.inverter_mac.lower().encode(),
        ],
        "local MAC": [
            loc,
            loc[::-1],
            log.local_mac.encode(),
            log.local_mac.lower().encode(),
        ],
        "serial": [
            serial.to_bytes(4, "little"),
            serial.to_bytes(4, "big"),
            str(serial).encode(),
            f"{serial:08x}".encode(),
            f"{serial:08X}".encode(),
        ],
        "password": [password_field],
    }


def _scan(label: str, haystack: bytes, patterns: dict[str, list[bytes]]) -> list[str]:
    return [
        f"{what} found in {label}"
        for what, needles in patterns.items()
        for needle in needles
        if needle in haystack
    ]


def self_check(
    messages_out: list[Message],
    frames: list[dict[str, str]],
    text: str,
    patterns: dict[str, list[bytes]],
) -> None:
    """Fail loudly if any real value survived (wire, unescaped, or as text)."""
    problems = _scan("fixture text", text.encode(), patterns)
    for n, frame in enumerate(frames):
        raw = bytes.fromhex(frame["hex"])
        problems += _scan(f"frame {n} (wire)", raw, patterns)
        problems += _scan(
            f"frame {n} (unescaped)",
            raw[:L1_HEADER] + unescape(raw[L1_HEADER:]),
            patterns,
        )
    for n, msg in enumerate(messages_out):
        body = msg.l2.body if msg.l2 else msg.payload
        problems += _scan(f"message {n}", msg.hdr + body, patterns)
    if problems:
        raise SystemExit("sanitisation failed: " + "; ".join(sorted(set(problems))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("log", type=Path, help="raw SBFspot -d5 -v5 log file")
    parser.add_argument("output", type=Path, nargs="?", default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    text = args.log.read_text(encoding="utf-8", errors="replace")
    log = parse_log(text)
    password_field = find_password_field(log.messages)
    inverter_susy = int(
        _detect(text, r"^SUSyID: (\d+) - SN: \d+", "inverter SUSyID").group(1)
    )
    app = _detect(text, r"^SUSyID: (\d+) - SessionID: (\d+)", "application session")
    logon_payload = next(
        m.l2.body[BODY_PAYLOAD:]
        for m in log.messages
        if m.direction == "tx"
        and m.l2
        and m.l2.body[BODY_PAYLOAD : BODY_PAYLOAD + 4] == bytes.fromhex("0c04fdff")
    )
    logon_time = int.from_bytes(logon_payload[12:16], "little")

    new_inverter = mac_to_le(NEW_INVERTER_MAC)
    new_local = mac_to_le(NEW_LOCAL_MAC)
    subs = Substitutions(
        [
            (mac_to_le(log.inverter_mac), new_inverter),
            (mac_to_le(log.local_mac), new_local),
            (log.serial.to_bytes(4, "little"), NEW_SERIAL.to_bytes(4, "little")),
            (str(log.serial).encode(), str(NEW_SERIAL).encode()),
            (password_field, encode_password(NEW_PASSWORD)),
        ]
    )
    frames, first_frame, id_shift = build_wire(
        log.messages, subs, new_local, new_inverter
    )

    # Substituted view of the messages, for the leak scan.
    cleaned = [
        Message(
            m.direction,
            hdr=subs.apply(m.hdr),
            payload=subs.apply(m.payload),
            l2=L2(subs.apply(m.l2.body)) if m.l2 else None,
        )
        for m in log.messages
    ]

    uncaptured = [
        {
            "before_frame": first_frame[u["before_message"]],
            "cmd": u["cmd"],
            "l1_length": u["length"],
        }
        for u in log.uncaptured
    ]
    fixture = {
        "meta": {
            "description": (
                "Sanitised SBFspot Bluetooth session against an SMA Sunny Boy "
                "SB 3000TL-20 (user login, one full poll). Generated by "
                "scripts/sanitize_capture.py."
            ),
            "source": "SBFspot V3.9.12, -d5 -v5, Windows RFCOMM channel 1",
            "frame_origin": {
                "wire": (
                    "bytes exactly as SBFspot logged them (sent frames and "
                    "received L1-only frames)"
                ),
                "reconstructed": (
                    "received SMA-net-2 frames: SBFspot only logs the unescaped, "
                    "concatenated L2 packet, so the L1 header (inverter -> local "
                    "adapter) and the fragmentation are rebuilt; lengths were "
                    "verified against the logged PacketLength"
                ),
            },
            "uncaptured": uncaptured,
            "packet_id_shift": id_shift,
        },
        "identifiers": {
            "inverter_bt_address": NEW_INVERTER_MAC,
            "local_bt_address": NEW_LOCAL_MAC,
            "serial": NEW_SERIAL,
            "inverter_susy_id": inverter_susy,
            "app_susy_id": int(app.group(1)),
            "session_id": int(app.group(2)),
            "logon_time": logon_time,
            "password": NEW_PASSWORD,
        },
        "frames": frames,
    }
    out_text = json.dumps(fixture, indent=1) + "\n"
    self_check(cleaned, frames, out_text, _leak_patterns(log, password_field))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(out_text, encoding="utf-8", newline="\n")
    print(
        f"wrote {args.output} ({len(log.messages)} messages, {len(frames)} frames, "
        f"packet-id shift {id_shift}); leak scan clean",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
