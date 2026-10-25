"""SMA Bluetooth framing: L1 frames, byte escaping, FCS-16 and L2 packets.

Ported from SBFspot (https://github.com/SBFspot/SBFspot, CC BY-NC-SA 3.0):
``SBFNet.cpp`` (``writePacketHeader``, ``writePacketLength``, ``writePacket``,
``writePacketTrailer``, ``writeByte``, ``validateChecksum``) and ``getPacket()``
in ``SBFspot.cpp``.

Layers
------
**L1** is the Bluetooth link frame (``pkHeader``)::

    7E | len lo | len hi | 7E^lo^hi | source[6] | destination[6] | command[2] | payload

``len`` is the total frame length *on the wire* including the 18 header bytes.
Addresses are little endian (``AA:BB:CC:DD:EE:FF`` is sent as ``FF EE DD CC BB AA``).

**L2** (SMA-net-2) rides inside the payload of ``cmd=1`` frames (and of ``cmd=8``
fragments followed by a final ``cmd=1``).  On the wire::

    7E | escape( FF 03 60 65 | words | ctrl | dstSUSyID[2] | dstSerial[4] | ctrl2[2]
              | srcSUSyID[2] | srcSerial[4] | ctrl2[2] | error[2] | fragment[2]
              | packetID[2] | payload ) | FCS[2] | 7E

The FCS (CRC-16/X-25) covers the unescaped bytes between the two ``7E`` markers.
SBFspot sends the FCS unescaped and re-rolls the packet id until neither FCS byte
is ``0x7D``/``0x7E``; a receiver unescapes everything between the markers, so this
module decodes both an escaped and a raw FCS.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import (
    ESCAPE_BYTE,
    ESCAPE_XOR,
    ESCAPED_BYTES,
    L1_HEADER_LEN,
    L1_START,
    L2_HEADER_LEN,
    L2_SIGNATURE,
)
from .errors import SmaProtocolError

MAX_L1_LENGTH = 2048
"""SBFspot's ``COMMBUFSIZE``; no legitimate Bluetooth frame is larger."""

# ------------------------------ FCS-16


def _build_fcs_table() -> tuple[int, ...]:
    """Reflected CRC-16/X-25 table; identical to SBFspot's ``fcstab``."""
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_FCS_TABLE = _build_fcs_table()


def fcs16(data: bytes) -> int:
    """Return the FCS-16 of *data* (init 0xFFFF, final XOR 0xFFFF)."""
    fcs = 0xFFFF
    for byte in data:
        fcs = (fcs >> 8) ^ _FCS_TABLE[(fcs ^ byte) & 0xFF]
    return fcs ^ 0xFFFF


# ------------------------------ escaping


def escape(data: bytes) -> bytes:
    """Escape ``0x7D 0x7E 0x11 0x12 0x13`` as ``0x7D, byte ^ 0x20`` (``writeByte``)."""
    if not any(byte in ESCAPED_BYTES for byte in data):
        return data
    out = bytearray()
    for byte in data:
        if byte in ESCAPED_BYTES:
            out.append(ESCAPE_BYTE)
            out.append(byte ^ ESCAPE_XOR)
        else:
            out.append(byte)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    """Inverse of :func:`escape` (``getPacket()``: drop 0x7D, XOR the next byte)."""
    if ESCAPE_BYTE not in data:
        return data
    out = bytearray()
    escaped = False
    for byte in data:
        if escaped:
            out.append(byte ^ ESCAPE_XOR)
            escaped = False
        elif byte == ESCAPE_BYTE:
            escaped = True
        else:
            out.append(byte)
    if escaped:
        raise SmaProtocolError("packet ends inside an escape sequence")
    return bytes(out)


# ------------------------------ addresses


def address_to_bytes(address: str) -> bytes:
    """``'00:80:25:00:00:01'`` -> little-endian wire bytes."""
    parts = address.replace("-", ":").split(":")
    if len(parts) != 6:
        raise ValueError(f"not a Bluetooth address: {address!r}")
    try:
        return bytes(int(part, 16) for part in reversed(parts))
    except ValueError:
        raise ValueError(f"not a Bluetooth address: {address!r}") from None


def address_to_str(address: bytes) -> str:
    """Little-endian wire bytes -> ``'AA:BB:CC:DD:EE:FF'`` (upper case)."""
    return ":".join(f"{byte:02X}" for byte in reversed(address))


# ------------------------------ L1


@dataclass(frozen=True, slots=True)
class L1Frame:
    """A Bluetooth link-layer frame.

    ``payload`` holds the bytes exactly as they appear on the wire (still escaped
    for L2 content); the L1 layer itself neither escapes nor unescapes.
    """

    src: bytes
    dst: bytes
    cmd: int
    payload: bytes = b""

    def encode(self) -> bytes:
        """Serialise like ``writePacketHeader`` + ``writePacketLength``."""
        length = L1_HEADER_LEN + len(self.payload)
        if length > MAX_L1_LENGTH:
            raise ValueError(f"L1 frame too long: {length} bytes")
        head = bytes((L1_START, length & 0xFF, length >> 8))
        checksum = head[0] ^ head[1] ^ head[2]
        return (
            head
            + bytes((checksum,))
            + self.src
            + self.dst
            + self.cmd.to_bytes(2, "little")
            + self.payload
        )

    @classmethod
    def decode(cls, data: bytes) -> L1Frame:
        """Parse one complete frame (``frame_length()`` bytes read from the stream)."""
        if frame_length(data[:4]) != len(data):
            raise SmaProtocolError("L1 frame length does not match its header")
        return cls(
            src=data[4:10],
            dst=data[10:16],
            cmd=int.from_bytes(data[16:18], "little"),
            payload=data[L1_HEADER_LEN:],
        )


def frame_length(head: bytes) -> int:
    """Validate the first four bytes of a frame and return the total frame length.

    Raises :class:`SmaProtocolError` if the start byte, the header checksum or the
    length is wrong.
    """
    if len(head) < 4:
        raise SmaProtocolError("truncated L1 header")
    if head[0] != L1_START:
        raise SmaProtocolError(f"L1 frame does not start with 0x7E: {head.hex()}")
    if head[0] ^ head[1] ^ head[2] != head[3]:
        raise SmaProtocolError(f"bad L1 header checksum: {head.hex()}")
    length = head[1] | (head[2] << 8)
    if not L1_HEADER_LEN <= length <= MAX_L1_LENGTH:
        raise SmaProtocolError(f"implausible L1 frame length {length}")
    return length


def is_l2_start(payload: bytes) -> bool:
    """True if an L1 payload begins an L2 packet (``7E FF 03 60 65``)."""
    return payload[:1] == bytes((L1_START,)) and payload[1:5] == L2_SIGNATURE


# ------------------------------ L2

_MIN_L2_INNER = len(L2_SIGNATURE) + L2_HEADER_LEN + 2  # signature + header + FCS


@dataclass(frozen=True, slots=True)
class L2Packet:
    """An SMA-net-2 packet (the content between the ``7E`` markers, unescaped)."""

    dst_susy_id: int
    dst_serial: int
    src_susy_id: int
    src_serial: int
    packet_id: int
    """Raw 16-bit field; SBFspot sets bit 15 (``pcktID | 0x8000``)."""
    payload: bytes = b""
    ctrl: int = 0xA0
    dst_ctrl2: int = 0
    src_ctrl2: int = 0
    error: int = 0
    """Packet status; 0 = OK, 21 = LRI not available, 0x0100 on logon = bad password."""
    fragment: int = 0
    """Count-down of follow-up packets in a multi-packet response (0 = last)."""

    @property
    def sequence(self) -> int:
        """Packet id without the direction flag (``get_short(...) & 0x7FFF``)."""
        return self.packet_id & 0x7FFF

    def _body(self) -> bytes:
        """Unescaped bytes covered by the FCS."""
        if len(self.payload) % 4:
            raise ValueError("L2 payload must be a multiple of four bytes")
        words = (L2_HEADER_LEN + len(self.payload)) // 4
        return (
            L2_SIGNATURE
            + bytes((words, self.ctrl))
            + self.dst_susy_id.to_bytes(2, "little")
            + self.dst_serial.to_bytes(4, "little")
            + self.dst_ctrl2.to_bytes(2, "little")
            + self.src_susy_id.to_bytes(2, "little")
            + self.src_serial.to_bytes(4, "little")
            + self.src_ctrl2.to_bytes(2, "little")
            + self.error.to_bytes(2, "little")
            + self.fragment.to_bytes(2, "little")
            + self.packet_id.to_bytes(2, "little")
            + self.payload
        )

    def fcs_bytes(self) -> bytes:
        """The two FCS bytes, little endian."""
        return fcs16(self._body()).to_bytes(2, "little")

    def encode(self, *, escape_fcs: bool = False) -> bytes:
        """Serialise as ``writePacket`` + ``writePacketTrailer`` do.

        The body is escaped; the FCS is written *raw* like SBFspot does, so the
        caller must check :meth:`fcs_is_wire_safe` and bump the packet id
        otherwise (``isCrcValid()`` loop).  With ``escape_fcs=True`` the FCS is
        escaped as well, which is always safe; receivers accept both forms.
        """
        fcs = self.fcs_bytes()
        if escape_fcs:
            fcs = escape(fcs)
        return bytes((L1_START,)) + escape(self._body()) + fcs + bytes((L1_START,))

    def fcs_is_wire_safe(self) -> bool:
        """False if a raw FCS byte would be mistaken for a marker or escape."""
        return not any(b in (ESCAPE_BYTE, L1_START) for b in self.fcs_bytes())

    @classmethod
    def decode(cls, stream: bytes) -> L2Packet:
        """Parse a reassembled L2 stream (``7E ... 7E``, still escaped).

        Everything between the markers is unescaped first (as SBFspot's
        ``getPacket`` does), so an escaped or raw FCS both work.
        """
        if not is_l2_start(stream) or stream[-1:] != bytes((L1_START,)):
            raise SmaProtocolError("not a complete L2 packet")
        inner = unescape(stream[1:-1])
        if len(inner) < _MIN_L2_INNER:
            raise SmaProtocolError("L2 packet too short")
        body, fcs = inner[:-2], inner[-2:]
        if fcs16(body).to_bytes(2, "little") != fcs:
            raise SmaProtocolError("L2 packet FCS mismatch")
        words = body[4]
        if words * 4 != len(body) - len(L2_SIGNATURE):
            raise SmaProtocolError(
                f"L2 length field ({words} words) disagrees with packet size"
            )
        return cls(
            ctrl=body[5],
            dst_susy_id=int.from_bytes(body[6:8], "little"),
            dst_serial=int.from_bytes(body[8:12], "little"),
            dst_ctrl2=int.from_bytes(body[12:14], "little"),
            src_susy_id=int.from_bytes(body[14:16], "little"),
            src_serial=int.from_bytes(body[16:20], "little"),
            src_ctrl2=int.from_bytes(body[20:22], "little"),
            error=int.from_bytes(body[22:24], "little"),
            fragment=int.from_bytes(body[24:26], "little"),
            packet_id=int.from_bytes(body[26:28], "little"),
            payload=body[28:],
        )
