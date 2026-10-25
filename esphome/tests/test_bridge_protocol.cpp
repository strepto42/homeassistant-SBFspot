// Host unit tests for esphome/components/sma_bt_bridge/bridge_protocol.h.
//
// No test framework: plain checks, a failure report per check and a non-zero
// exit code if anything failed.  Build and run:
//
//   g++ -std=c++17 -Wall -Wextra -Werror -o test_bridge_protocol esphome/tests/test_bridge_protocol.cpp
//   ./test_bridge_protocol

#include "../components/sma_bt_bridge/bridge_protocol.h"

#include <cstdint>
#include <cstdio>
#include <string>
#include <string_view>

using namespace esphome::sma_bt_bridge::protocol;

static int g_checks = 0;
static int g_failures = 0;

#define CHECK(cond) \
  do { \
    g_checks++; \
    if (!(cond)) { \
      g_failures++; \
      std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
    } \
  } while (0)

#define CHECK_EQ(actual, expected) \
  do { \
    g_checks++; \
    const auto a_ = (actual); \
    const auto e_ = (expected); \
    if (!(a_ == e_)) { \
      g_failures++; \
      std::printf("FAIL %s:%d: %s == %s\n", __FILE__, __LINE__, #actual, #expected); \
    } \
  } while (0)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static const uint8_t *bytes(const std::string &s) { return reinterpret_cast<const uint8_t *>(s.data()); }

static Actions feed(Session &s, const std::string &text, uint32_t now) {
  return s.on_client_data(bytes(text), text.size(), now);
}

static const MacAddress LOCAL_MAC{0x11, 0x22, 0x33, 0x44, 0x55, 0x66};
static const MacAddress INVERTER_MAC{0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF};
static const std::string CONNECT_LINE = "CONNECT AA:BB:CC:DD:EE:FF\n";

static constexpr uint32_t BT_TIMEOUT = 15000;
static constexpr uint32_t IDLE_TIMEOUT = 60000;

static Session make_session() {
  Session s(BT_TIMEOUT, IDLE_TIMEOUT);
  s.set_local_mac(LOCAL_MAC);
  return s;
}

/// Drive a fresh session up to State::PIPING and return it.
static Session make_piping_session(uint32_t t0 = 1000) {
  Session s = make_session();
  CHECK(s.on_client_accepted(t0).accepted);
  Actions a = feed(s, CONNECT_LINE, t0 + 10);
  CHECK(a.bt_connect);
  Actions ok = s.on_bt_connected(t0 + 200);
  CHECK_EQ(ok.reply, std::string("OK 11:22:33:44:55:66\n"));
  CHECK(s.state() == State::PIPING);
  return s;
}

// ---------------------------------------------------------------------------
// MAC parsing / formatting
// ---------------------------------------------------------------------------

static void test_mac() {
  MacAddress mac{};
  CHECK(parse_mac("AA:BB:CC:DD:EE:FF", mac));
  CHECK(mac == INVERTER_MAC);
  CHECK(parse_mac("aa:bb:cc:dd:ee:ff", mac));
  CHECK(mac == INVERTER_MAC);
  CHECK(parse_mac("aA:Bb:cC:Dd:eE:Ff", mac));
  CHECK(mac == INVERTER_MAC);
  CHECK(parse_mac("00:01:02:03:04:05", mac));
  CHECK((mac == MacAddress{0, 1, 2, 3, 4, 5}));

  // A failed parse must leave the output untouched.
  const MacAddress before = mac;
  CHECK(!parse_mac("AA:BB:CC:DD:EE:GG", mac));
  CHECK(mac == before);

  CHECK(!parse_mac("", mac));
  CHECK(!parse_mac("AA:BB:CC:DD:EE", mac));        // too short
  CHECK(!parse_mac("AA:BB:CC:DD:EE:FF:00", mac));  // too long
  CHECK(!parse_mac("AA:BB:CC:DD:EE:F", mac));      // 16 chars
  CHECK(!parse_mac("AA:BB:CC:DD:EE:FFF", mac));    // 18 chars
  CHECK(!parse_mac("AABBCCDDEEFF", mac));          // no separators
  CHECK(!parse_mac("AA-BB-CC-DD-EE-FF", mac));     // wrong separator
  CHECK(!parse_mac("AA:BB:CC:DD:EE-FF", mac));     // one wrong separator
  CHECK(!parse_mac("AA:BB:CC:DD:EE:F ", mac));     // space
  CHECK(!parse_mac("AA BB CC DD EE FF", mac));
  CHECK(!parse_mac("0x:BB:CC:DD:EE:FF", mac));
  CHECK(!parse_mac("AA:BB:CC:DD:EE:F\xC3", mac));  // non-ASCII
  CHECK(!parse_mac(std::string_view("AA:BB:CC:DD:EE:F\0", 17), mac));
}

static void test_format_mac() {
  CHECK_EQ(format_mac(INVERTER_MAC), std::string("AA:BB:CC:DD:EE:FF"));
  CHECK_EQ(format_mac(LOCAL_MAC), std::string("11:22:33:44:55:66"));
  CHECK_EQ(format_mac(MacAddress{0x0A, 0xB0, 0x00, 0x01, 0xFE, 0x9F}), std::string("0A:B0:00:01:FE:9F"));
  const uint8_t raw[6] = {0xde, 0xad, 0xbe, 0xef, 0x00, 0x7f};
  CHECK_EQ(format_mac(raw), std::string("DE:AD:BE:EF:00:7F"));

  // Round trip
  MacAddress parsed{};
  CHECK(parse_mac(format_mac(raw), parsed));
  CHECK(parsed == MacAddress({0xde, 0xad, 0xbe, 0xef, 0x00, 0x7f}));
}

// ---------------------------------------------------------------------------
// Line formatting
// ---------------------------------------------------------------------------

static void test_line_formatting() {
  CHECK_EQ(banner_line(), std::string("SMABT 1\n"));
  CHECK_EQ(ok_line(LOCAL_MAC), std::string("OK 11:22:33:44:55:66\n"));
  CHECK_EQ(error_line(ErrorCode::BUSY, "bridge in use"), std::string("ERR BUSY bridge in use\n"));
  CHECK_EQ(error_line(ErrorCode::BAD_REQUEST, "line too long"), std::string("ERR BAD_REQUEST line too long\n"));
  CHECK_EQ(error_line(ErrorCode::BT_CONNECT_FAILED, "status 1"), std::string("ERR BT_CONNECT_FAILED status 1\n"));
  CHECK_EQ(error_line(ErrorCode::BT_TIMEOUT, "slow"), std::string("ERR BT_TIMEOUT slow\n"));
  CHECK_EQ(error_line(ErrorCode::BUSY, ""), std::string("ERR BUSY\n"));

  // Free text can never break line framing.
  CHECK_EQ(error_line(ErrorCode::BAD_REQUEST, "a\nb\rc\x01" "d\xFF"), std::string("ERR BAD_REQUEST a?b?c?d?\n"));
  const std::string long_line = error_line(ErrorCode::BAD_REQUEST, std::string(500, 'x'));
  CHECK_EQ(long_line.size(), std::string("ERR BAD_REQUEST ").size() + 100 + 1);
  CHECK_EQ(long_line.find('\n'), long_line.size() - 1);
}

// ---------------------------------------------------------------------------
// CONNECT line parsing
// ---------------------------------------------------------------------------

static void test_parse_connect_line() {
  ConnectRequest r = parse_connect_line("CONNECT AA:BB:CC:DD:EE:FF");
  CHECK(r.ok);
  CHECK(r.mac == INVERTER_MAC);

  r = parse_connect_line("CONNECT aa:bb:cc:dd:ee:ff");  // MAC is case-insensitive
  CHECK(r.ok);
  CHECK(r.mac == INVERTER_MAC);

  r = parse_connect_line("CONNECT aa:BB:cc:DD:ee:FF\r");  // CRLF terminated
  CHECK(r.ok);
  CHECK(r.mac == INVERTER_MAC);

  // Invalid inputs; each must be rejected with a non-empty reason.
  const char *bad[] = {
      "",                                    // empty
      "\r",                                  // just CR
      "CONNECT",                             // no MAC
      "CONNECT ",                            // empty MAC
      "CONNECT AA:BB:CC:DD:EE",              // too short
      "CONNECT AA:BB:CC:DD:EE:FF:00",        // too long
      "CONNECT AA:BB:CC:DD:EE:GG",           // bad hex
      "CONNECT AABBCCDDEEFF",                // no separators
      "CONNECT AA-BB-CC-DD-EE-FF",           // wrong separators
      "AA:BB:CC:DD:EE:FF",                   // missing prefix
      "HELLO AA:BB:CC:DD:EE:FF",             // wrong command
      "connect AA:BB:CC:DD:EE:FF",           // command is case-sensitive
      "CONNECT  AA:BB:CC:DD:EE:FF",          // two spaces
      " CONNECT AA:BB:CC:DD:EE:FF",          // leading space
      "CONNECT AA:BB:CC:DD:EE:FF ",          // trailing space
      "CONNECT AA:BB:CC:DD:EE:FF\r\r",       // only one CR is tolerated
      "CONNECT\tAA:BB:CC:DD:EE:FF",          // tab instead of space
      "CONNECT AA:BB:CC:DD:EE:FF extra",     // trailing junk
      "CONNECT AA:BB:CC:DD:EE:F\xC3\xA9",    // non-ASCII
  };
  for (const char *line : bad) {
    r = parse_connect_line(line);
    CHECK(!r.ok);
    CHECK(r.error != nullptr && r.error[0] != '\0');
  }

  // Embedded NUL must not truncate the check.
  r = parse_connect_line(std::string_view("CONNECT AA:BB:CC:DD:EE:FF\0x", 27));
  CHECK(!r.ok);

  // Length limit: > 64 bytes is "too long", exactly 64 gets as far as MAC parsing.
  const std::string exactly_64 = "CONNECT AA:BB:CC:DD:EE:FF" + std::string(64 - 25, ' ');
  CHECK_EQ(exactly_64.size(), MAX_LINE_LENGTH);
  r = parse_connect_line(exactly_64);
  CHECK(!r.ok);
  CHECK(std::string(r.error) != "line too long");

  const std::string sixty_five = exactly_64 + " ";
  r = parse_connect_line(sixty_five);
  CHECK(!r.ok);
  CHECK_EQ(std::string(r.error), std::string("line too long"));

  r = parse_connect_line(std::string(1000, 'A'));
  CHECK(!r.ok);
  CHECK_EQ(std::string(r.error), std::string("line too long"));
}

// ---------------------------------------------------------------------------
// LineBuffer
// ---------------------------------------------------------------------------

static void test_line_buffer() {
  LineBuffer lb;
  size_t used = 0;

  // One byte at a time.
  const std::string text = "CONNECT AA:BB:CC:DD:EE:FF\n";
  for (size_t i = 0; i + 1 < text.size(); i++) {
    CHECK(lb.feed(bytes(text) + i, 1, used) == LineBuffer::Status::INCOMPLETE);
    CHECK_EQ(used, static_cast<size_t>(1));
  }
  CHECK(lb.feed(bytes(text) + text.size() - 1, 1, used) == LineBuffer::Status::LINE);
  CHECK_EQ(used, static_cast<size_t>(1));
  CHECK_EQ(std::string(lb.line()), std::string("CONNECT AA:BB:CC:DD:EE:FF"));

  // Bytes after the newline are left for the caller.
  lb.reset();
  const std::string two = "CONNECT AA:BB:CC:DD:EE:FF\r\nPAYLOAD";
  CHECK(lb.feed(bytes(two), two.size(), used) == LineBuffer::Status::LINE);
  CHECK_EQ(used, static_cast<size_t>(27));
  CHECK_EQ(std::string(lb.line()), std::string("CONNECT AA:BB:CC:DD:EE:FF\r"));

  // The buffer restarts automatically after a completed line.
  const std::string next = "X\n";
  CHECK(lb.feed(bytes(next), next.size(), used) == LineBuffer::Status::LINE);
  CHECK_EQ(std::string(lb.line()), std::string("X"));

  // Empty line
  CHECK(lb.feed(bytes(std::string("\n")), 1, used) == LineBuffer::Status::LINE);
  CHECK_EQ(lb.line().size(), static_cast<size_t>(0));

  // Exactly 64 bytes + newline is accepted, 65 bytes is not.
  lb.reset();
  const std::string l64 = std::string(64, 'A') + "\n";
  CHECK(lb.feed(bytes(l64), l64.size(), used) == LineBuffer::Status::LINE);
  CHECK_EQ(lb.line().size(), MAX_LINE_LENGTH);

  lb.reset();
  const std::string l65 = std::string(65, 'A') + "\n";
  CHECK(lb.feed(bytes(l65), l65.size(), used) == LineBuffer::Status::TOO_LONG);
  CHECK_EQ(used, static_cast<size_t>(64));

  // Overflow across several chunks without any newline.
  lb.reset();
  const std::string chunk(40, 'B');
  CHECK(lb.feed(bytes(chunk), chunk.size(), used) == LineBuffer::Status::INCOMPLETE);
  CHECK(lb.feed(bytes(chunk), chunk.size(), used) == LineBuffer::Status::TOO_LONG);
  CHECK_EQ(used, static_cast<size_t>(24));
}

// ---------------------------------------------------------------------------
// ByteFifo
// ---------------------------------------------------------------------------

static void test_byte_fifo() {
  ByteFifo f;
  CHECK(f.empty());
  f.append("hello");
  f.append(" world");
  CHECK_EQ(f.size(), static_cast<size_t>(11));
  CHECK_EQ(std::string(reinterpret_cast<const char *>(f.data()), f.size()), std::string("hello world"));
  f.consume(6);
  CHECK_EQ(std::string(reinterpret_cast<const char *>(f.data()), f.size()), std::string("world"));
  f.consume(100);  // over-consume is clamped
  CHECK(f.empty());

  // Order is preserved across many append/consume cycles (exercises compaction).
  ByteFifo g;
  uint8_t next_in = 0;
  uint8_t next_out = 0;
  bool in_order = true;
  for (int round = 0; round < 2000; round++) {
    uint8_t chunk[37];
    for (auto &b : chunk)
      b = next_in++;
    g.append(chunk, sizeof(chunk));
    const size_t take = (round % 3 == 0) ? 20 : 30;
    const size_t n = take < g.size() ? take : g.size();
    for (size_t i = 0; i < n; i++)
      in_order = in_order && (g.data()[i] == next_out++);
    g.consume(n);
  }
  CHECK(in_order);
  CHECK(g.size() < 100000);
  g.clear();
  CHECK(g.empty());
}

// ---------------------------------------------------------------------------
// Session state machine
// ---------------------------------------------------------------------------

static void test_session_happy_path() {
  Session s = make_session();
  CHECK(s.state() == State::IDLE);

  AcceptResult acc = s.on_client_accepted(1000);
  CHECK(acc.accepted);
  CHECK_EQ(acc.reply, std::string("SMABT 1\n"));
  CHECK(s.state() == State::AWAIT_CONNECT);

  Actions a = feed(s, CONNECT_LINE, 1100);
  CHECK(a.bt_connect);
  CHECK(a.mac == INVERTER_MAC);
  CHECK(a.reply.empty());
  CHECK(!a.close_client);
  CHECK(!a.bt_disconnect);
  CHECK_EQ(a.payload_offset, CONNECT_LINE.size());
  CHECK(s.state() == State::BT_CONNECTING);

  a = s.on_bt_connected(1500);
  CHECK_EQ(a.reply, std::string("OK 11:22:33:44:55:66\n"));
  CHECK(!a.close_client && !a.bt_disconnect && !a.bt_connect);
  CHECK(s.state() == State::PIPING);

  // Payload flows in both directions, nothing is interpreted.
  a = feed(s, std::string("\x7e\xff\x03CONNECT AA:BB:CC:DD:EE:FF\n", 28), 2000);
  CHECK_EQ(a.payload_offset, static_cast<size_t>(0));
  CHECK(a.reply.empty() && !a.close_client && !a.bt_connect);
  CHECK(s.state() == State::PIPING);

  // BT side closes -> client closes, new client can connect straight away.
  a = s.on_bt_closed();
  CHECK(a.close_client);
  CHECK(!a.bt_disconnect);  // already closed
  CHECK(s.state() == State::IDLE);
  CHECK(s.on_client_accepted(3000).accepted);
}

static void test_session_split_connect_line() {
  Session s = make_session();
  CHECK(s.on_client_accepted(0).accepted);
  Actions a = feed(s, "CONN", 10);
  CHECK(!a.bt_connect && a.reply.empty() && !a.close_client);
  CHECK_EQ(a.payload_offset, static_cast<size_t>(4));
  a = feed(s, "ECT aa:bb:cc:dd:ee", 20);
  CHECK(!a.bt_connect && a.reply.empty() && !a.close_client);
  a = feed(s, ":ff\r\n", 30);
  CHECK(a.bt_connect);
  CHECK(a.mac == INVERTER_MAC);
  CHECK(s.state() == State::BT_CONNECTING);
}

static void test_session_pipelined_payload() {
  // Bytes behind the CONNECT line in the same chunk are payload, not handshake.
  Session s = make_session();
  CHECK(s.on_client_accepted(0).accepted);
  const std::string chunk = CONNECT_LINE + "EARLY";
  Actions a = feed(s, chunk, 10);
  CHECK(a.bt_connect);
  CHECK_EQ(a.payload_offset, CONNECT_LINE.size());
  CHECK_EQ(chunk.size() - a.payload_offset, static_cast<size_t>(5));

  // More client data while the BT connect is in flight: held by the caller.
  a = feed(s, "MORE", 20);
  CHECK_EQ(a.payload_offset, static_cast<size_t>(0));
  CHECK(!a.close_client && a.reply.empty());
  CHECK(s.state() == State::BT_CONNECTING);
}

static void test_session_busy() {
  Session s = make_session();
  CHECK(s.on_client_accepted(0).accepted);

  // Busy in every non-idle state, and the rejection never disturbs the session.
  AcceptResult r = s.on_client_accepted(10);
  CHECK(!r.accepted);
  CHECK_EQ(r.reply, std::string("ERR BUSY bridge in use\n"));
  CHECK(s.state() == State::AWAIT_CONNECT);

  feed(s, CONNECT_LINE, 20);
  CHECK(s.state() == State::BT_CONNECTING);
  CHECK(!s.on_client_accepted(30).accepted);
  CHECK(s.state() == State::BT_CONNECTING);

  s.on_bt_connected(40);
  CHECK(!s.on_client_accepted(50).accepted);
  CHECK(s.state() == State::PIPING);

  // Also busy while the previous SPP link is being released.
  s.on_client_closed(60);
  CHECK(s.state() == State::TEARDOWN);
  CHECK(!s.on_client_accepted(70).accepted);
  CHECK(s.state() == State::TEARDOWN);

  s.on_bt_closed();
  CHECK(s.state() == State::IDLE);
  CHECK(s.on_client_accepted(80).accepted);
}

static void test_session_bad_requests() {
  struct Case {
    const char *name;
    std::string input;
    const char *expected_reason;
  };
  const Case cases[] = {
      {"malformed MAC", "CONNECT AA:BB:CC:DD:EE:GG\n", "malformed MAC address"},
      {"short MAC", "CONNECT AA:BB:CC\n", "MAC address must be 17 characters"},
      {"missing prefix", "AA:BB:CC:DD:EE:FF\n", "expected CONNECT AA:BB:CC:DD:EE:FF"},
      {"empty line", "\n", "expected CONNECT AA:BB:CC:DD:EE:FF"},
      {"too long with newline", std::string(70, 'X') + "\n", "line too long"},
      {"too long without newline", std::string(200, 'X'), "line too long"},
  };
  for (const Case &c : cases) {
    Session s = make_session();
    CHECK(s.on_client_accepted(0).accepted);
    Actions a = feed(s, c.input, 100);
    const std::string expected = std::string("ERR BAD_REQUEST ") + c.expected_reason + "\n";
    if (a.reply != expected)
      std::printf("  case '%s': got '%s'\n", c.name, a.reply.c_str());
    CHECK_EQ(a.reply, expected);
    CHECK(a.close_client);
    CHECK(!a.bt_connect);
    CHECK(!a.bt_disconnect);  // there is no SPP link yet
    CHECK(s.state() == State::IDLE);
    CHECK(s.on_client_accepted(200).accepted);  // ready for the next client
  }

  // Over-long line arriving in several chunks.
  Session s = make_session();
  CHECK(s.on_client_accepted(0).accepted);
  CHECK(feed(s, std::string(40, 'A'), 10).reply.empty());
  Actions a = feed(s, std::string(40, 'A'), 20);
  CHECK_EQ(a.reply, std::string("ERR BAD_REQUEST line too long\n"));
  CHECK(a.close_client);
  CHECK(s.state() == State::IDLE);
}

static void test_session_connect_line_timeout() {
  Session s = make_session();
  CHECK(s.on_client_accepted(1000).accepted);

  Actions a = s.on_tick(1000 + CONNECT_LINE_TIMEOUT_MS - 1);
  CHECK(!a.close_client);
  CHECK(s.state() == State::AWAIT_CONNECT);

  a = s.on_tick(1000 + CONNECT_LINE_TIMEOUT_MS);
  CHECK(a.close_client);
  CHECK(a.reply.empty());  // timeout -> plain close
  CHECK(!a.bt_disconnect);
  CHECK(s.state() == State::IDLE);

  // A partially received line does not extend the deadline.
  CHECK(s.on_client_accepted(50000).accepted);
  feed(s, "CONNECT AA:BB", 53000);
  a = s.on_tick(50000 + CONNECT_LINE_TIMEOUT_MS);
  CHECK(a.close_client);
  CHECK(s.state() == State::IDLE);

  // A fresh client after the timeout starts with an empty line buffer.
  CHECK(s.on_client_accepted(60000).accepted);
  a = feed(s, CONNECT_LINE, 60010);
  CHECK(a.bt_connect);
}

static void test_session_client_gone_before_connect() {
  Session s = make_session();
  CHECK(s.on_client_accepted(0).accepted);
  Actions a = s.on_client_closed(10);
  CHECK(!a.bt_disconnect && !a.close_client);
  CHECK(s.state() == State::IDLE);
}

static void test_session_bt_failure() {
  Session s = make_session();
  CHECK(s.on_client_accepted(0).accepted);
  feed(s, CONNECT_LINE, 10);

  Actions a = s.on_bt_connect_failed("page timeout\n(status 4)");
  CHECK_EQ(a.reply, std::string("ERR BT_CONNECT_FAILED page timeout?(status 4)\n"));
  CHECK(a.close_client);
  CHECK(!a.bt_disconnect);
  CHECK(s.state() == State::IDLE);

  // Link closing during setup (before OK) is also a connect failure.
  CHECK(s.on_client_accepted(100).accepted);
  feed(s, CONNECT_LINE, 110);
  a = s.on_bt_closed();
  CHECK_EQ(a.reply, std::string("ERR BT_CONNECT_FAILED connection closed during setup\n"));
  CHECK(a.close_client);
  CHECK(s.state() == State::IDLE);

  // Empty detail text.
  CHECK(s.on_client_accepted(200).accepted);
  feed(s, CONNECT_LINE, 210);
  a = s.on_bt_connect_failed("");
  CHECK_EQ(a.reply, std::string("ERR BT_CONNECT_FAILED connection closed during setup\n"));
}

static void test_session_bt_timeout() {
  Session s = make_session();
  CHECK(s.on_client_accepted(1000).accepted);
  feed(s, CONNECT_LINE, 1100);  // BT connect starts at t=1100

  Actions a = s.on_tick(1100 + BT_TIMEOUT - 1);
  CHECK(a.reply.empty() && !a.close_client && !a.bt_disconnect);
  CHECK(s.state() == State::BT_CONNECTING);

  a = s.on_tick(1100 + BT_TIMEOUT);
  CHECK_EQ(a.reply, std::string("ERR BT_TIMEOUT bluetooth connect timed out\n"));
  CHECK(a.close_client);
  CHECK(a.bt_disconnect);  // cancel the pending SPP connect
  CHECK(s.state() == State::TEARDOWN);

  // The stack reports the cancelled connect as closed -> bridge is free again.
  a = s.on_bt_closed();
  CHECK(a.reply.empty() && !a.close_client);
  CHECK(s.state() == State::IDLE);
}

static void test_session_late_bt_connect_after_cancel() {
  Session s = make_session();
  CHECK(s.on_client_accepted(0).accepted);
  feed(s, CONNECT_LINE, 10);
  s.on_tick(10 + BT_TIMEOUT);
  CHECK(s.state() == State::TEARDOWN);

  // The cancelled connect completes anyway: the link must be dropped again and
  // the session must not claim it is piping.
  Actions a = s.on_bt_connected(20000);
  CHECK(a.bt_disconnect);
  CHECK(a.reply.empty());
  CHECK(s.state() == State::TEARDOWN);
  s.on_bt_closed();
  CHECK(s.state() == State::IDLE);

  // Unsolicited link while idle is dropped as well.
  a = s.on_bt_connected(30000);
  CHECK(a.bt_disconnect);
  CHECK(s.state() == State::IDLE);
}

static void test_session_client_close_while_connecting() {
  Session s = make_session();
  CHECK(s.on_client_accepted(0).accepted);
  feed(s, CONNECT_LINE, 10);

  Actions a = s.on_client_closed(500);
  CHECK(a.bt_disconnect);  // cancel the connect
  CHECK(!a.close_client);  // the client already closed
  CHECK(s.state() == State::TEARDOWN);
  s.on_bt_closed();
  CHECK(s.state() == State::IDLE);
}

static void test_session_client_close_while_piping() {
  Session s = make_piping_session(0);
  Actions a = s.on_client_closed(5000);
  CHECK(a.bt_disconnect);
  CHECK(!a.close_client);
  CHECK(s.state() == State::TEARDOWN);

  // Wait for the SPP close event; a new client is rejected until then.
  CHECK(!s.on_client_accepted(5100).accepted);
  a = s.on_bt_closed();
  CHECK(!a.close_client && !a.bt_disconnect && a.reply.empty());
  CHECK(s.state() == State::IDLE);
  CHECK(s.on_client_accepted(5200).accepted);
}

static void test_session_teardown_timeout() {
  Session s = make_piping_session(0);
  s.on_client_closed(1000);
  CHECK(s.state() == State::TEARDOWN);

  s.on_tick(1000 + TEARDOWN_TIMEOUT_MS - 1);
  CHECK(s.state() == State::TEARDOWN);
  s.on_tick(1000 + TEARDOWN_TIMEOUT_MS);
  CHECK(s.state() == State::IDLE);  // never stuck even if the close event is lost
}

static void test_session_idle_timeout() {
  Session s = make_piping_session(0);  // PIPING since t=200

  Actions a = s.on_tick(200 + IDLE_TIMEOUT - 1);
  CHECK(!a.close_client);

  // Traffic from the client resets the idle timer...
  feed(s, "x", 30000);
  a = s.on_tick(30000 + IDLE_TIMEOUT - 1);
  CHECK(!a.close_client && !a.bt_disconnect);
  // ...and so does traffic from the Bluetooth side.
  s.on_bt_data(60000);
  a = s.on_tick(60000 + IDLE_TIMEOUT - 1);
  CHECK(!a.close_client && !a.bt_disconnect);
  CHECK(s.state() == State::PIPING);

  a = s.on_tick(60000 + IDLE_TIMEOUT);
  CHECK(a.close_client);
  CHECK(a.bt_disconnect);  // "close both"
  CHECK(a.reply.empty());
  CHECK(s.state() == State::TEARDOWN);

  s.on_bt_closed();
  CHECK(s.state() == State::IDLE);
}

static void test_session_idle_timeout_without_traffic() {
  Session s = make_piping_session(0);  // PIPING since t=200
  Actions a = s.on_tick(200 + IDLE_TIMEOUT);
  CHECK(a.close_client && a.bt_disconnect);
  CHECK(s.state() == State::TEARDOWN);
}

static void test_session_abort() {
  Session s = make_piping_session(0);
  Actions a = s.on_abort(100);
  CHECK(a.close_client && a.bt_disconnect);
  CHECK(s.state() == State::TEARDOWN);
  a = s.on_abort(200);  // idempotent
  CHECK(!a.close_client && !a.bt_disconnect);

  Session t = make_session();
  CHECK(t.on_client_accepted(0).accepted);
  a = t.on_abort(5);
  CHECK(a.close_client && !a.bt_disconnect);
  CHECK(t.state() == State::IDLE);
}

static void test_session_clock_wraparound() {
  // millis() wraps every 49.7 days; timeouts must survive the wrap.
  const uint32_t near_wrap = 0xFFFFFFFFu - 2000;

  Session s = make_session();
  CHECK(s.on_client_accepted(near_wrap).accepted);
  Actions a = s.on_tick(near_wrap + 4999);  // wraps past zero
  CHECK(!a.close_client);
  a = s.on_tick(near_wrap + 5000);
  CHECK(a.close_client);

  Session p = make_piping_session(near_wrap);
  p.on_bt_data(near_wrap + 3000);  // after the wrap
  a = p.on_tick(near_wrap + 3000 + IDLE_TIMEOUT - 1);
  CHECK(!a.close_client);
  a = p.on_tick(near_wrap + 3000 + IDLE_TIMEOUT);
  CHECK(a.close_client);
}

static void test_session_stray_events_are_harmless() {
  Session s = make_session();
  // Events that make no sense in IDLE must not change anything.
  Actions a = s.on_bt_closed();
  CHECK(a.reply.empty() && !a.close_client && !a.bt_disconnect);
  a = s.on_bt_connect_failed("x");
  CHECK(a.reply.empty() && !a.close_client);
  a = s.on_client_closed(0);
  CHECK(!a.bt_disconnect);
  a = feed(s, "garbage", 0);
  CHECK(!a.bt_connect && a.reply.empty());
  s.on_bt_data(0);
  a = s.on_tick(1000000);
  CHECK(!a.close_client && !a.bt_disconnect);
  CHECK(s.state() == State::IDLE);

  // BT closing while the handshake is still pending does not end the session.
  CHECK(s.on_client_accepted(0).accepted);
  a = s.on_bt_closed();
  CHECK(!a.close_client);
  CHECK(s.state() == State::AWAIT_CONNECT);
}

int main() {
  test_mac();
  test_format_mac();
  test_line_formatting();
  test_parse_connect_line();
  test_line_buffer();
  test_byte_fifo();
  test_session_happy_path();
  test_session_split_connect_line();
  test_session_pipelined_payload();
  test_session_busy();
  test_session_bad_requests();
  test_session_connect_line_timeout();
  test_session_client_gone_before_connect();
  test_session_bt_failure();
  test_session_bt_timeout();
  test_session_late_bt_connect_after_cancel();
  test_session_client_close_while_connecting();
  test_session_client_close_while_piping();
  test_session_teardown_timeout();
  test_session_idle_timeout();
  test_session_idle_timeout_without_traffic();
  test_session_abort();
  test_session_clock_wraparound();
  test_session_stray_events_are_harmless();

  if (g_failures != 0) {
    std::printf("%d of %d checks FAILED\n", g_failures, g_checks);
    return 1;
  }
  std::printf("all %d checks passed\n", g_checks);
  return 0;
}
