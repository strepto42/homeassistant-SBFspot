"""``scripts/sanitize_capture.py``, tested without any real inverter data.

The committed fixture is turned back into a *raw-looking* SBFspot ``-d5`` log that
carries different, made-up identifiers (other MACs, serial and password).  Running
the sanitiser over that log must reproduce the committed fixture exactly - which
shows that the script detects the values from the log structure alone, replaces
them everywhere (binary, ASCII, escaped, inside multi-part packets) and re-encodes
lengths, escaping and FCS correctly.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from smabt import frame as frame_mod
from smabt.const import CMD_L2_FRAGMENT

from .capture import Capture
from .fake_bridge import synthetic_frame

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sanitize_capture.py"

ALT_INVERTER = "00:80:25:AB:CD:EF"
ALT_LOCAL = "20:AA:BB:CC:DD:EE"
ALT_SERIAL = 2100123456
ALT_PASSWORD = "hunter2"


@pytest.fixture(scope="module")
def sc() -> Iterator[ModuleType]:
    name = "sanitize_capture_script"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve string annotations through it
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[name]


@pytest.fixture(scope="module")
def capture() -> Capture:
    return Capture.load()


def alternative_substitutions(sc: ModuleType, capture: Capture) -> object:
    """placeholder -> alternative identifiers (what the sanitiser undoes)."""
    ids = capture.identifiers
    return sc.Substitutions(
        [
            (sc.mac_to_le(ids["inverter_bt_address"]), sc.mac_to_le(ALT_INVERTER)),
            (sc.mac_to_le(ids["local_bt_address"]), sc.mac_to_le(ALT_LOCAL)),
            (
                ids["serial"].to_bytes(4, "little"),
                ALT_SERIAL.to_bytes(4, "little"),
            ),
            (str(ids["serial"]).encode(), str(ALT_SERIAL).encode()),
            (sc.encode_password(ids["password"]), sc.encode_password(ALT_PASSWORD)),
        ]
    )


def hexdump(data: bytes) -> list[str]:
    """SBFspot's ``HexDump(buf, count, 10)`` layout."""
    lines = ["--------: 00 01 02 03 04 05 06 07 08 09"]
    for offset in range(0, len(data), 10):
        row = "".join(f"{b:02X} " for b in data[offset : offset + 10])
        lines.append(f"{offset:08d}: {row}")
    return lines


def render_raw_log(sc: ModuleType, capture: Capture) -> str:
    """What SBFspot would have logged had the identifiers been the alternative ones."""
    subs = alternative_substitutions(sc, capture)
    ids = capture.identifiers
    out = [
        "SBFspot V3.9.12",
        f"Connecting to {ALT_INVERTER} (1/10)",
        "Initialising...",
        f"SUSyID: {ids['app_susy_id']} - SessionID: {ids['session_id']}",
        f"Local BT address: {ALT_LOCAL}",
        f"Serial Nr: {ALT_SERIAL:08X} ({ALT_SERIAL})",
        f"SUSyID: {ids['inverter_susy_id']} - SN: {ALT_SERIAL}",
    ]
    uncaptured = {u["before_frame"]: u for u in capture.meta["uncaptured"]}
    for message in capture.messages:
        first = message.frames[0]
        l1 = first.l1
        src, dst = subs.apply(l1.src), subs.apply(l1.dst)
        if message.direction == "tx":
            payload = l1.payload
            if message.is_l2:
                packet = message.l2
                body = frame_mod.L2Packet.decode(message.stream)
                altered = subs.apply(packet_body(sc, body))
                fcs = sc.fcs16(altered).to_bytes(2, "little")
                assert not {0x7D, 0x7E} & set(fcs), "choose other alternative values"
                payload = b"\x7e" + sc.escape(altered) + fcs + b"\x7e"
                assert packet
            else:
                payload = subs.apply(sc.unescape(payload))
                payload = sc.escape(payload)
            wire = frame_mod.L1Frame(src, dst, l1.cmd, payload).encode()
            out += [*hexdump(wire), f"{len(wire)} Bytes sent"]
            continue

        out.append(f"getPacket({l1.cmd})")
        if first.index in uncaptured:
            for cmd, length in [(10, 31), (12, 20)]:
                out += [
                    "Received 18 bytes",
                    f"Received {length - 18} bytes",
                    f"cmd={cmd}",
                ]
        if not message.is_l2:
            wire = frame_mod.L1Frame(src, dst, l1.cmd, subs.apply(l1.payload)).encode()
            out += [
                "Received 18 bytes",
                f"Received {len(wire) - 18} bytes",
                f"cmd={l1.cmd}",
            ]
            out += ["<<<====== Content of pcktBuf =======>>>", *hexdump(wire)]
            out.append("<<<=================================>>>")
            continue
        packet = message.l2
        altered = subs.apply(packet_body(sc, packet))
        fcs = sc.fcs16(altered).to_bytes(2, "little")
        logical_orig = frame_mod.unescape(message.stream[1:-1])
        sizes = [len(f.l1.payload) for f in message.frames]
        cuts = sc._logical_cuts(logical_orig, sizes)
        logical_alt = altered + fcs
        bounds = [0, *cuts, len(logical_alt)]
        for n, frame in enumerate(message.frames):
            chunk = sc.escape(logical_alt[bounds[n] : bounds[n + 1]])
            if n == 0:
                chunk = b"\x7e" + chunk
            if n == len(message.frames) - 1:
                chunk += b"\x7e"
            wire = frame_mod.L1Frame(src, dst, frame.l1.cmd, chunk).encode()
            out += ["Received 18 bytes", f"Received {len(wire) - 18} bytes"]
            out += [f"cmd={frame.l1.cmd}", f"PacketLength={len(wire)}"]
        out += ["<<<====== Content of pcktBuf =======>>>"]
        out += hexdump(b"\x7e" + altered + fcs + b"\x7e")
        out.append("<<<=================================>>>")
    return "\n".join(out) + "\n"


def packet_body(sc: ModuleType, packet: frame_mod.L2Packet) -> bytes:
    """The FCS-covered bytes of a decoded packet."""
    return packet._body()


def run_sanitizer(sc: ModuleType, log_text: str, tmp_path: Path) -> dict[str, object]:
    raw = tmp_path / "raw_d5.log"
    raw.write_text(log_text, encoding="utf-8")
    out = tmp_path / "fixture.json"
    assert sc.main([str(raw), str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def test_sanitiser_reproduces_the_committed_fixture(
    sc: ModuleType, capture: Capture, tmp_path: Path
) -> None:
    log_text = render_raw_log(sc, capture)
    # The synthetic log really contains the "real" (alternative) values ...
    assert ALT_INVERTER in log_text
    assert str(ALT_SERIAL) in log_text
    assert sc.encode_password(ALT_PASSWORD).hex(" ").upper().split()[0] in log_text

    result = run_sanitizer(sc, log_text, tmp_path)

    # ... and the sanitiser maps it back to exactly the committed fixture.
    committed = json.loads(
        (Path(__file__).parent / "fixtures" / "sb3000tl20_session.json").read_text(
            encoding="utf-8"
        )
    )
    assert result["frames"] == committed["frames"]
    assert result["identifiers"] == committed["identifiers"]
    assert result["meta"] == committed["meta"]
    out_text = json.dumps(result)
    assert ALT_INVERTER.replace(":", "").lower() not in out_text.lower()
    assert str(ALT_SERIAL) not in out_text


def test_sanitiser_refuses_a_log_it_cannot_understand(
    sc: ModuleType, tmp_path: Path
) -> None:
    raw = tmp_path / "raw.log"
    raw.write_text("nothing useful here\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="cannot detect"):
        sc.main([str(raw), str(tmp_path / "out.json")])


def test_self_check_fails_if_a_value_survives(sc: ModuleType, capture: Capture) -> None:
    """The leak scan must catch a substitution that was forgotten."""
    log = sc.Log(ALT_INVERTER, ALT_LOCAL, ALT_SERIAL, [], [])
    patterns = sc._leak_patterns(log, sc.encode_password(ALT_PASSWORD))
    leaked = [{"hex": sc.mac_to_le(ALT_INVERTER).hex() + "aabb"}]
    with pytest.raises(SystemExit, match="sanitisation failed"):
        sc.self_check([], leaked, "", patterns)
    clean = [{"hex": frame.wire.hex()} for frame in capture.frames]
    sc.self_check([], clean, "", patterns)  # does not raise


def test_synthetic_helper_is_consistent(capture: Capture) -> None:
    frame = synthetic_frame(capture, 10, 31)
    assert len(frame) == 31
    assert frame_mod.L1Frame.decode(frame).cmd == 10
    assert CMD_L2_FRAGMENT == 8
