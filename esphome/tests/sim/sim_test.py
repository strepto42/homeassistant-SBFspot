"""End-to-end tests of the bridge against the host simulation (sim_main.cpp).

usage: python3 sim_test.py "<sim-binary> [wrapper args]" [only-test-substring]

The first argument is the simulation command line (it is split with shlex).
The TCP port comes from $SIM_PORT (default 19900); $SIM_VERBOSE=1 echoes the
simulation output.
"""

import os
import random
import re
import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback

PORT = int(os.environ.get("SIM_PORT", "19900"))
BT_TIMEOUT_MS = 1500
IDLE_MS = 1500
LOCAL = "11:22:33:44:55:66"
# Longest time the bridge keeps the SPP link open for bytes the client sent
# before it closed (BT_DRAIN_TIMEOUT_MS in bridge_protocol.h).
DRAIN_TIMEOUT_S = 2.0


class Sim:
    def __init__(self, binary):
        self.binary = binary
        self.lines = []
        self.lock = threading.Lock()

    def start(self):
        self.proc = subprocess.Popen(
            [*shlex.split(self.binary), str(PORT), str(BT_TIMEOUT_MS), str(IDLE_MS)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.ready = threading.Event()
        threading.Thread(target=self._reader, daemon=True).start()
        assert self.ready.wait(10), "simulation did not become ready"

    def _reader(self):
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            with self.lock:
                self.lines.append(line)
            if line == "READY":
                self.ready.set()
            if os.environ.get("SIM_VERBOSE"):
                print("   |", line)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def mark(self):
        with self.lock:
            return len(self.lines)

    def since(self, mark):
        with self.lock:
            return list(self.lines[mark:])

    def count(self, mark, text):
        return sum(1 for line in self.since(mark) if text in line)

    def wait_for(self, mark, text, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.count(mark, text):
                return True
            time.sleep(0.02)
        return False

    def crashed(self):
        return self.proc.poll() is not None


def dial(rcvbuf=None):
    s = socket.socket()
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.settimeout(10)
    s.connect(("127.0.0.1", PORT))
    return s


class LineReader:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.eof = False

    def line(self, timeout=5.0):
        """Next line without newline, None on EOF/timeout (timeout raises)."""
        end = time.time() + timeout
        while b"\n" not in self.buf:
            if self.eof:
                return None
            left = end - time.time()
            if left <= 0:
                raise TimeoutError(f"no line within {timeout:.1f}s (have {self.buf!r})")
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(4096)
            except TimeoutError:
                raise TimeoutError(
                    f"no line within {timeout:.1f}s (have {self.buf!r})"
                ) from None
            if not chunk:
                self.eof = True
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("ascii")

    def read_exact(self, n, timeout=10.0):
        end = time.time() + timeout
        while len(self.buf) < n:
            if self.eof:
                break
            left = end - time.time()
            if left <= 0:
                raise TimeoutError(f"got {len(self.buf)} of {n} bytes")
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                raise TimeoutError(f"got {len(self.buf)} of {n} bytes") from None
            if not chunk:
                self.eof = True
                break
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def wait_eof(self, timeout=5.0):
        """Wait for the peer to close; returns (seconds waited, bytes seen)."""
        start = time.time()
        seen = self.buf
        self.buf = b""
        while not self.eof:
            left = timeout - (time.time() - start)
            if left <= 0:
                raise TimeoutError(f"connection still open after {timeout:.1f}s")
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                raise TimeoutError(
                    f"connection still open after {timeout:.1f}s"
                ) from None
            if not chunk:
                self.eof = True
            else:
                seen += chunk
        return time.time() - start, seen


def mac(last):
    return f"AA:BB:CC:DD:EE:{last:02X}"


def open_session(last, wait=6.0, line_end="\n", mac_text=None):
    """Connect (retry while BUSY), send CONNECT; returns (sock, reader, reply)."""
    end = time.time() + wait
    while True:
        s = dial()
        r = LineReader(s)
        first = r.line(5)
        if first == "SMABT 1":
            break
        s.close()
        assert first is not None and first.startswith("ERR BUSY"), (
            f"unexpected first line {first!r}"
        )
        if time.time() > end:
            raise AssertionError(f"bridge stayed busy for {wait:.1f}s")
        time.sleep(0.05)
    s.sendall((f"CONNECT {mac_text or mac(last)}{line_end}").encode())
    reply = r.line(10)
    return s, r, reply


def echo_roundtrip(sock, reader, payload, timeout=10.0):
    sock.sendall(payload)
    return reader.read_exact(len(payload), timeout)


def fnv1a64(data):
    """Same hash as the fake inverter (sim_main.cpp) logs for what it saw."""
    h = 0xCBF29CE484222325
    for b in data:
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def random_bytes(seed, n):
    rng = random.Random(seed)
    return bytes(rng.getrandbits(8) for _ in range(n))


# What the fake inverter side logs (sim_main.cpp): the bytes the SPP stack had
# accepted when the disconnect was handled, and the bytes the inverter had
# received when the link was closed.
STACK_ACCEPTED = re.compile(
    r"link h=\d+ disconnect handled: stack_accepted=(\d+) fnv=([0-9a-f]{16})"
)
INVERTER_RECEIVED = re.compile(
    r"link h=\d+ closed: inverter_received=(\d+) fnv=([0-9a-f]{16})"
)


def wait_link_line(sim, mark, pattern, timeout=10.0):
    """Wait for a fake-inverter line; returns (length, hash, seconds waited)."""
    start = time.time()
    while time.time() - start < timeout:
        for line in sim.since(mark):
            m = pattern.search(line)
            if m:
                return int(m.group(1)), m.group(2), time.time() - start
        time.sleep(0.01)
    raise AssertionError(f"no {pattern.pattern!r} line within {timeout:.0f}s")


def bridge_warnings(sim, mark):
    return [line for line in sim.since(mark) if line.startswith("[W]")]


def assert_inverter_got(sim, mark, data):
    """The SPP stack had every byte before the disconnect; the inverter got all."""
    length, digest, _ = wait_link_line(sim, mark, STACK_ACCEPTED)
    assert (length, digest) == (len(data), fnv1a64(data)), (
        f"SPP disconnect came with {length} of {len(data)} bytes accepted"
    )
    length, digest, _ = wait_link_line(sim, mark, INVERTER_RECEIVED)
    assert (length, digest) == (len(data), fnv1a64(data)), (
        f"inverter received {length} of {len(data)} bytes"
    )


def send_and_close(sock, data, how):
    """Send the last bytes and end the connection with no delay."""
    sock.sendall(data)
    if how == "shutdown":
        sock.shutdown(socket.SHUT_WR)
    elif how == "reset":
        # SO_LINGER 0: close() sends RST instead of FIN ("read error" on the bridge)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


# --------------------------------------------------------------------------- tests

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@test
def happy_path_and_banner(sim):
    s = dial()
    r = LineReader(s)
    assert r.line() == "SMABT 1"
    s.sendall(b"CONNECT AA:BB:CC:DD:EE:01\n")
    assert r.line() == "OK " + LOCAL
    assert echo_roundtrip(s, r, b"hello inverter") == b"hello inverter"
    mark = sim.mark()
    s.close()
    assert sim.wait_for(mark, "disconnect h="), (
        "bridge did not disconnect SPP after TCP close"
    )
    assert sim.wait_for(mark, "close event"), "no close event"


@test
def crlf_and_lowercase_mac(sim):
    s, r, reply = open_session(1, line_end="\r\n", mac_text="aa:bb:cc:dd:ee:01")
    assert reply == "OK " + LOCAL, reply
    assert echo_roundtrip(s, r, b"abc") == b"abc"
    s.close()


@test
def pipelined_payload_after_connect(sim):
    wait_idle(sim)
    s = dial()
    r = LineReader(s)
    assert r.line() == "SMABT 1"
    s.sendall(b"CONNECT AA:BB:CC:DD:EE:01\nEARLY-BYTES")
    assert r.line() == "OK " + LOCAL
    assert r.read_exact(11) == b"EARLY-BYTES"
    s.close()


def wait_idle(sim, timeout=8.0):
    """Wait until the bridge accepts a client again (previous teardown done)."""
    end = time.time() + timeout
    while time.time() < end:
        s = dial()
        r = LineReader(s)
        first = r.line(5)
        s.close()
        if first == "SMABT 1":
            time.sleep(0.05)
            return
        time.sleep(0.05)
    raise AssertionError("bridge did not become idle")


@test
def large_transfer_both_directions(sim):
    s, r, reply = open_session(1)
    assert reply == "OK " + LOCAL
    rng = random.Random(1)
    total = 100_000
    data = bytes(rng.getrandbits(8) for _ in range(total))
    received = bytearray()
    err = []

    def reader():
        try:
            while len(received) < total:
                chunk = r.sock.recv(65536) if not r.buf else None
                if r.buf:
                    received.extend(r.buf)
                    r.buf = b""
                    continue
                if not chunk:
                    err.append(f"EOF after {len(received)} bytes")
                    return
                received.extend(chunk)
        except Exception as exc:
            err.append(repr(exc))

    s.settimeout(60)
    t = threading.Thread(target=reader)
    t.start()
    pos = 0
    start = time.time()
    while pos < total:
        n = rng.randint(1, 1500)
        s.sendall(data[pos : pos + n])
        pos += n
        if rng.random() < 0.05:
            time.sleep(0.01)
    t.join(90)
    assert not t.is_alive(), f"receiver stuck at {len(received)}/{total}"
    assert not err, err
    assert bytes(received) == data, f"payload corrupted (len {len(received)})"
    print(f"      {total} bytes echoed in {time.time() - start:.1f}s")
    s.close()


@test
def second_client_gets_busy_without_disturbing_first(sim):
    s, r, reply = open_session(1)
    assert reply == "OK " + LOCAL
    s2 = dial()
    r2 = LineReader(s2)
    assert r2.line() == "ERR BUSY bridge in use"
    _waited, extra = r2.wait_eof(3)
    assert extra == b"", extra
    assert echo_roundtrip(s, r, b"still alive") == b"still alive"
    # busy also while connecting / handshaking: covered by the unit tests
    s.close()


@test
def bad_requests(sim):
    cases = [
        (
            b"CONNECT AA:BB:CC:DD:EE\n",
            "ERR BAD_REQUEST MAC address must be 17 characters",
        ),
        (b"HELLO\n", "ERR BAD_REQUEST expected CONNECT AA:BB:CC:DD:EE:FF"),
        (b"CONNECT GG:BB:CC:DD:EE:01\n", "ERR BAD_REQUEST malformed MAC address"),
        (b"X" * 100, "ERR BAD_REQUEST line too long"),
        (b"X" * 3000 + b"\n", "ERR BAD_REQUEST line too long"),
    ]
    for payload, expected in cases:
        s = dial()
        r = LineReader(s)
        assert r.line() == "SMABT 1"
        s.sendall(payload)
        got = r.line()
        assert got == expected, f"{payload[:20]!r} -> {got!r} (expected {expected!r})"
        try:
            r.wait_eof(3)
        except ConnectionResetError:
            # acceptable: the client kept unread bytes in flight; the ERR line
            # itself was delivered above.
            assert len(payload) > 1000, "unexpected reset"
        s.close()
        # no Bluetooth was involved, so the bridge must be immediately free again
        s = dial()
        r = LineReader(s)
        assert r.line() == "SMABT 1", "bridge not free right after a bad request"
        s.close()
        time.sleep(0.05)


@test
def connect_line_timeout(sim):
    wait_idle(sim)
    s = dial()
    r = LineReader(s)
    assert r.line() == "SMABT 1"
    waited, extra = r.wait_eof(8)
    assert extra == b"", extra
    assert 4.5 <= waited <= 6.5, waited
    print(f"      closed after {waited:.2f}s")


@test
def bt_connect_failure(sim):
    s, r, reply = open_session(0x02)
    assert reply == "ERR BT_CONNECT_FAILED SPP open failed: FAILURE", reply
    _waited, extra = r.wait_eof(3)
    assert extra == b""
    s.close()
    # link is gone, bridge usable at once
    s, r, reply = open_session(1, wait=2)
    assert reply == "OK " + LOCAL
    s.close()


@test
def bt_cl_init_failure(sim):
    s, r, reply = open_session(0x08)
    assert reply.startswith("ERR BT_CONNECT_FAILED SPP connect not started"), reply
    r.wait_eof(3)
    s.close()


@test
def esp_spp_connect_error(sim):
    s, r, reply = open_session(0x0B)
    assert reply == "ERR BT_CONNECT_FAILED esp_spp_connect: ESP_FAIL", reply
    r.wait_eof(3)
    s.close()


@test
def bt_timeout_cancels_connect(sim):
    mark = sim.mark()
    t0 = time.time()
    s, r, reply = open_session(0x03)
    assert reply == "ERR BT_TIMEOUT bluetooth connect timed out", reply
    elapsed = time.time() - t0
    assert 1.3 <= elapsed <= 2.6, elapsed
    r.wait_eof(3)
    s.close()
    assert sim.wait_for(mark, "disconnect h="), "pending connect was not cancelled"
    s, r, reply = open_session(1, wait=3)
    assert reply == "OK " + LOCAL
    s.close()


@test
def remote_close_closes_tcp(sim):
    s, r, reply = open_session(0x04)
    assert reply == "OK " + LOCAL
    s.sendall(b"trigger")
    _waited, seen = r.wait_eof(3)
    assert seen in (b"", b"trigger") or b"trigger".startswith(seen), seen
    s.close()
    s, r, reply = open_session(1, wait=3)
    assert reply == "OK " + LOCAL
    s.close()


@test
def idle_timeout_and_keepalive(sim):
    s, r, reply = open_session(1)
    assert reply == "OK " + LOCAL
    # traffic every second keeps a 1.5 s idle timeout from firing
    for _ in range(4):
        time.sleep(1.0)
        assert echo_roundtrip(s, r, b"k") == b"k"
    mark = sim.mark()
    waited, extra = r.wait_eof(5)
    assert extra == b""
    assert 1.2 <= waited <= 3.0, waited
    print(f"      idle close after {waited:.2f}s")
    assert sim.wait_for(mark, "disconnect h="), "SPP not disconnected on idle timeout"
    s.close()


@test
def client_close_while_bt_connecting(sim):
    mark = sim.mark()
    wait_idle(sim)
    s = dial()
    r = LineReader(s)
    assert r.line() == "SMABT 1"
    s.sendall(b"CONNECT AA:BB:CC:DD:EE:06\n")  # takes 800 ms to open
    time.sleep(0.2)
    s.close()
    assert sim.wait_for(mark, "disconnect h=", 3), "connect was not cancelled"
    s, r, reply = open_session(1, wait=4)
    assert reply == "OK " + LOCAL
    assert sim.count(mark, "open h=") >= 1
    s.close()


@test
def late_open_after_lost_cancel_is_dropped(sim):
    wait_idle(sim)
    mark = sim.mark()
    s = dial()
    r = LineReader(s)
    assert r.line() == "SMABT 1"
    s.sendall(b"CONNECT AA:BB:CC:DD:EE:0A\n")  # cancel is ignored, OPEN comes at 600 ms
    time.sleep(0.2)
    s.close()
    assert sim.wait_for(mark, "open h=", 3), "late open did not happen"
    # the bridge must notice the unexpected link and drop it again
    assert sim.wait_for(mark, "close event", 3), "late link was not closed again"
    assert sim.count(mark, "disconnect h=") == 2, sim.since(mark)
    s, r, reply = open_session(1, wait=4)
    assert reply == "OK " + LOCAL
    s.close()


@test
def teardown_timeout_when_stack_never_answers(sim):
    s, _r, reply = open_session(0x09)
    assert reply == "OK " + LOCAL
    s.close()
    t0 = time.time()
    s2 = dial()
    r2 = LineReader(s2)
    assert r2.line() == "ERR BUSY bridge in use"  # still tearing down
    s2.close()
    s3, _r3, reply = open_session(1, wait=8)
    assert reply == "OK " + LOCAL
    elapsed = time.time() - t0
    assert 4.0 <= elapsed <= 7.5, elapsed
    print(f"      recovered after {elapsed:.2f}s")
    s3.close()


@test
def congestion_lost_event_is_probed(sim):
    s, _r, reply = open_session(0x0C)
    assert reply == "OK " + LOCAL
    rng = random.Random(7)
    total = 12_000
    data = bytes(rng.getrandbits(8) for _ in range(total))
    got = bytearray()
    done = threading.Event()

    def reader():
        s.settimeout(30)
        try:
            while len(got) < total:
                chunk = s.recv(65536)
                if not chunk:
                    break
                got.extend(chunk)
        except Exception:
            pass
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    s.sendall(data)
    assert done.wait(40), f"transfer stuck at {len(got)}/{total}"
    assert bytes(got) == data
    s.close()


@test
def flood_with_stalled_reader_aborts_cleanly(sim):
    wait_idle(sim)
    mark = sim.mark()
    s = dial(rcvbuf=2048)
    r = LineReader(s)
    assert r.line() == "SMABT 1"
    s.sendall(b"CONNECT AA:BB:CC:DD:EE:0D\n")
    time.sleep(3.0)  # do not read: the inverter floods 200 KB
    # draining now must end in EOF (session aborted), never in endless data
    total = 0
    s.settimeout(5)
    t_end = time.time() + 15
    eof = False
    while time.time() < t_end:
        try:
            chunk = s.recv(65536)
        except TimeoutError:
            break
        if not chunk:
            eof = True
            break
        total += len(chunk)
    assert eof, "bridge kept the session open after losing data"
    assert total < 200_000, total  # data was cut, not silently completed
    assert sim.count(mark, "overflowed") >= 1, sim.since(mark)[-10:]
    s.close()


@test
def many_quick_sessions(sim):
    for i in range(25):
        s, r, reply = open_session(1, wait=4)
        assert reply == "OK " + LOCAL, (i, reply)
        msg = bytes([i]) * (1 + i * 7)
        assert echo_roundtrip(s, r, msg) == msg
        s.close()
    assert not sim.crashed()


# --- the client sends its last bytes (SMA logoff) and closes at once -----------


@test
def last_bytes_before_close_reach_the_inverter(sim):
    # Data and FIN arrive within one loop() pass, so the bridge reads the bytes
    # and the EOF together.  Several rounds: this is a race, not a single shot.
    for how in ("close", "shutdown", "reset"):
        for round_no, size in enumerate((40, 120, 600, 1500)):
            data = random_bytes(round_no, size)
            wait_idle(sim)
            mark = sim.mark()
            s, _r, reply = open_session(1)
            assert reply == "OK " + LOCAL
            send_and_close(s, data, how)
            try:
                assert_inverter_got(sim, mark, data)
            except AssertionError as exc:
                raise AssertionError(f"{how}, {size} bytes: {exc}") from None
            assert not bridge_warnings(sim, mark), bridge_warnings(sim, mark)


@test
def last_bytes_wait_for_congestion_to_clear(sim):
    # The SPP link is congested (nothing drains for 600 ms) when the client
    # closes: the bytes must go out when the congestion clears, and only then
    # may the bridge disconnect.
    data = random_bytes(21, 1600)
    wait_idle(sim)
    mark = sim.mark()
    s, _r, reply = open_session(0x0E)
    assert reply == "OK " + LOCAL
    send_and_close(s, data, "close")
    t_close = time.time()
    wait_link_line(sim, mark, STACK_ACCEPTED)
    waited = time.time() - t_close
    assert 0.4 <= waited < DRAIN_TIMEOUT_S - 0.1, waited
    print(f"      disconnect {waited:.2f}s after the client closed")
    assert_inverter_got(sim, mark, data)
    assert not bridge_warnings(sim, mark), bridge_warnings(sim, mark)
    # What the inverter answered while the bridge was still draining is dropped.
    assert sim.count(mark, "Discarding") >= 1, sim.since(mark)[-15:]
    s, r, reply = open_session(1, wait=4)
    assert reply == "OK " + LOCAL
    assert echo_roundtrip(s, r, b"after the drain") == b"after the drain"
    s.close()


@test
def drain_timeout_drops_bytes_and_frees_the_bridge(sim):
    # Congestion that lasts 4 s outlasts the drain timeout: warn, disconnect.
    data = random_bytes(22, 1600)
    wait_idle(sim)
    mark = sim.mark()
    s, _r, reply = open_session(0x0F)
    assert reply == "OK " + LOCAL
    send_and_close(s, data, "close")
    t_close = time.time()
    accepted, _digest, _ = wait_link_line(sim, mark, STACK_ACCEPTED)
    waited = time.time() - t_close
    assert DRAIN_TIMEOUT_S - 0.3 <= waited <= DRAIN_TIMEOUT_S + 1.2, waited
    print(f"      disconnect {waited:.2f}s after the client closed")
    assert 0 < accepted < len(data), accepted
    dropped = [
        int(m.group(1))
        for line in bridge_warnings(sim, mark)
        if (m := re.search(r"dropping (\d+) bytes", line))
    ]
    assert dropped == [len(data) - accepted], (
        dropped,
        accepted,
        bridge_warnings(sim, mark),
    )
    assert not sim.crashed()
    s, r, reply = open_session(1, wait=6)
    assert reply == "OK " + LOCAL
    assert echo_roundtrip(s, r, b"still works") == b"still works"
    s.close()


@test
def write_in_flight_is_confirmed_before_disconnect(sim):
    # The stack confirms the write 200 ms late.  The client closes meanwhile:
    # the bridge must wait for the confirmation (partial writes and congestion
    # are reported through it) instead of disconnecting at once.
    data = random_bytes(25, 100)
    wait_idle(sim)
    mark = sim.mark()
    s, _r, reply = open_session(0x10)
    assert reply == "OK " + LOCAL
    s.sendall(data)
    time.sleep(0.08)  # the bridge has issued the write; its confirmation is due
    s.close()
    t_close = time.time()
    wait_link_line(sim, mark, STACK_ACCEPTED)
    waited = time.time() - t_close
    assert 0.06 <= waited < DRAIN_TIMEOUT_S - 0.1, waited
    assert_inverter_got(sim, mark, data)
    assert not bridge_warnings(sim, mark), bridge_warnings(sim, mark)


@test
def new_client_gets_busy_while_draining(sim):
    data = random_bytes(23, 1600)
    wait_idle(sim)
    mark = sim.mark()
    s, _r, reply = open_session(0x0E)
    assert reply == "OK " + LOCAL
    send_and_close(s, data, "close")
    time.sleep(0.25)  # the link is still stalled: the drain is under way
    assert sim.count(mark, "disconnect h=") == 0, "SPP disconnected too early"
    s2 = dial()
    r2 = LineReader(s2)
    assert r2.line() == "ERR BUSY bridge in use"
    _waited, extra = r2.wait_eof(3)
    assert extra == b"", extra
    s2.close()
    # The rejected client did not disturb the drain.
    assert_inverter_got(sim, mark, data)
    s, _r, reply = open_session(1, wait=6)
    assert reply == "OK " + LOCAL
    s.close()


@test
def link_closed_by_inverter_during_drain(sim):
    # Scenario 04: the inverter drops the link 20 ms after the first write, so
    # most of the 1800 bytes cannot be delivered.  The bridge must say so, go
    # back to idle and serve the next client.
    data = random_bytes(24, 1800)
    wait_idle(sim)
    mark = sim.mark()
    s, _r, reply = open_session(0x04)
    assert reply == "OK " + LOCAL
    send_and_close(s, data, "close")
    assert sim.wait_for(mark, "close event", 5), sim.since(mark)[-15:]
    s, _r, reply = open_session(1, wait=6)
    assert reply == "OK " + LOCAL
    s.close()
    dropped = [line for line in bridge_warnings(sim, mark) if "dropping" in line]
    assert len(dropped) == 1, bridge_warnings(sim, mark)
    assert not sim.crashed()


@test
def early_bytes_dropped_when_client_closes_while_connecting(sim):
    # Nothing can be delivered before the link is up: cancel and drop (the
    # inverter must never see the link, let alone the bytes).
    wait_idle(sim)
    mark = sim.mark()
    s = dial()
    r = LineReader(s)
    assert r.line() == "SMABT 1"
    s.sendall(b"CONNECT AA:BB:CC:DD:EE:06\nEARLY-BYTES")  # opens only after 800 ms
    s.close()
    assert sim.wait_for(mark, "disconnect h=", 3), "connect was not cancelled"
    time.sleep(1.2)
    assert sim.count(mark, "open h=") == 0, sim.since(mark)
    assert sim.count(mark, "stack_accepted") == 0
    s, r, reply = open_session(1, wait=4)
    assert reply == "OK " + LOCAL
    s.close()


def main():
    binary = sys.argv[1]
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    sim = Sim(binary)
    sim.start()
    failed = 0
    for fn in TESTS:
        if only and only not in fn.__name__:
            continue
        t0 = time.time()
        try:
            fn(sim)
            print(f"PASS {fn.__name__:<55} {time.time() - t0:.1f}s")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__:<55} {time.time() - t0:.1f}s")
            traceback.print_exc(limit=6)
            for line in sim.since(max(0, sim.mark() - 25)):
                print("   |", line)
        if sim.crashed():
            print("SIMULATION CRASHED")
            failed += 1
            break
        time.sleep(0.2)
    sim.stop()
    time.sleep(0.5)
    bad = [
        line
        for line in sim.since(0)
        if any(k in line for k in ("Sanitizer", "runtime error", "ERROR: ", "==ERROR"))
        or line.startswith("FAKE: ERROR")
    ]
    # The bridge logs its own '[E]' lines in the overflow test; only sanitizer
    # output counts here.
    bad = [line for line in bad if not line.startswith("[E]")]
    if bad:
        failed += 1
        print("SANITIZER / HARNESS ERRORS:")
        for line in bad[:20]:
            print("   !", line)
    print(f"{failed} test(s) failed" if failed else "all tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
