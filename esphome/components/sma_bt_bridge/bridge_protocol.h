#pragma once

/**
 * @file bridge_protocol.h
 * @brief Bridge protocol v1: handshake parsing, line formatting and the
 *        session state machine of the SMA Bluetooth bridge.
 *
 * This header is deliberately free of ESP-IDF and ESPHome includes (plain
 * C++17 standard library only) so that it can be compiled and unit tested on a
 * host machine (see esphome/tests/test_bridge_protocol.cpp).  The firmware
 * component (sma_bt_bridge.cpp) owns all I/O; it feeds events into
 * protocol::Session and carries out the protocol::Actions that come back.
 *
 * Wire protocol (TCP, default port 9900), all control lines ASCII + '\n'
 * (a preceding '\r' is ignored):
 *
 *   bridge -> client   SMABT 1                          banner on accept
 *   client -> bridge   CONNECT AA:BB:CC:DD:EE:FF        within 5 s
 *   bridge -> client   OK 11:22:33:44:55:66             ESP32's own BT address
 *                      ERR <CODE> <text>                BUSY, BAD_REQUEST,
 *                                                       BT_CONNECT_FAILED,
 *                                                       BT_TIMEOUT
 *   after OK: transparent bidirectional byte pipe (TCP <-> RFCOMM channel 1)
 */

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace esphome::sma_bt_bridge::protocol {

inline constexpr int PROTOCOL_VERSION = 1;
/// Longest accepted control line, excluding the terminating '\n'.
inline constexpr size_t MAX_LINE_LENGTH = 64;
/// The client must send its CONNECT line within this time after the banner.
inline constexpr uint32_t CONNECT_LINE_TIMEOUT_MS = 5000;
/// Upper bound for waiting on the Bluetooth stack to confirm a disconnect.
inline constexpr uint32_t TEARDOWN_TIMEOUT_MS = 5000;

inline constexpr size_t MAC_SIZE = 6;
inline constexpr size_t MAC_TEXT_LENGTH = 17;  // "AA:BB:CC:DD:EE:FF"
using MacAddress = std::array<uint8_t, MAC_SIZE>;

// ---------------------------------------------------------------------------
// MAC address helpers
// ---------------------------------------------------------------------------

/// Value of a hex digit, or -1 if @p c is not one.
inline int hex_value(char c) {
  if (c >= '0' && c <= '9')
    return c - '0';
  if (c >= 'a' && c <= 'f')
    return c - 'a' + 10;
  if (c >= 'A' && c <= 'F')
    return c - 'A' + 10;
  return -1;
}

/// Parse "AA:BB:CC:DD:EE:FF" (either case, colon separated, exactly 17 chars).
/// @p out is only modified on success.
inline bool parse_mac(std::string_view text, MacAddress &out) {
  if (text.size() != MAC_TEXT_LENGTH)
    return false;
  MacAddress parsed{};
  for (size_t i = 0; i < MAC_SIZE; i++) {
    const size_t pos = i * 3;
    const int hi = hex_value(text[pos]);
    const int lo = hex_value(text[pos + 1]);
    if (hi < 0 || lo < 0)
      return false;
    if (i + 1 < MAC_SIZE && text[pos + 2] != ':')
      return false;
    parsed[i] = static_cast<uint8_t>((hi << 4) | lo);
  }
  out = parsed;
  return true;
}

/// Format 6 bytes as upper case "AA:BB:CC:DD:EE:FF".
inline std::string format_mac(const uint8_t *mac) {
  static constexpr char DIGITS[] = "0123456789ABCDEF";
  std::string text;
  text.reserve(MAC_TEXT_LENGTH);
  for (size_t i = 0; i < MAC_SIZE; i++) {
    if (i != 0)
      text.push_back(':');
    text.push_back(DIGITS[mac[i] >> 4]);
    text.push_back(DIGITS[mac[i] & 0x0F]);
  }
  return text;
}

inline std::string format_mac(const MacAddress &mac) { return format_mac(mac.data()); }

// ---------------------------------------------------------------------------
// Line formatting
// ---------------------------------------------------------------------------

enum class ErrorCode : uint8_t {
  BUSY,               ///< another client session is active
  BAD_REQUEST,        ///< over-long or malformed handshake line
  BT_CONNECT_FAILED,  ///< SPP connect to the inverter failed
  BT_TIMEOUT,         ///< SPP connect did not complete in bt_connect_timeout
};

inline const char *error_code_name(ErrorCode code) {
  switch (code) {
    case ErrorCode::BUSY:
      return "BUSY";
    case ErrorCode::BAD_REQUEST:
      return "BAD_REQUEST";
    case ErrorCode::BT_CONNECT_FAILED:
      return "BT_CONNECT_FAILED";
    case ErrorCode::BT_TIMEOUT:
      return "BT_TIMEOUT";
  }
  return "UNKNOWN";
}

/// Make free text safe for a single protocol line: anything outside printable
/// ASCII (notably CR/LF) becomes '?', and the result is capped in length so
/// that a reply never exceeds MAX_LINE_LENGTH-ish sizes by accident.
inline std::string sanitize_text(std::string_view text, size_t max_length = 100) {
  std::string out;
  out.reserve(text.size() < max_length ? text.size() : max_length);
  for (char c : text) {
    if (out.size() >= max_length)
      break;
    const auto uc = static_cast<unsigned char>(c);
    out.push_back((uc >= 0x20 && uc <= 0x7E) ? c : '?');
  }
  return out;
}

/// "SMABT 1\n"
inline std::string banner_line() { return "SMABT " + std::to_string(PROTOCOL_VERSION) + "\n"; }

/// "OK 11:22:33:44:55:66\n" (the ESP32's own Bluetooth address).
inline std::string ok_line(const MacAddress &local_mac) { return "OK " + format_mac(local_mac) + "\n"; }

/// "ERR <CODE> <text>\n"; the text is sanitised, and omitted (with its space)
/// when empty.
inline std::string error_line(ErrorCode code, std::string_view text) {
  std::string line = "ERR ";
  line += error_code_name(code);
  const std::string clean = sanitize_text(text);
  if (!clean.empty()) {
    line.push_back(' ');
    line += clean;
  }
  line.push_back('\n');
  return line;
}

// ---------------------------------------------------------------------------
// Handshake line parsing
// ---------------------------------------------------------------------------

/// Result of parsing the client's CONNECT line.
struct ConnectRequest {
  bool ok{false};
  MacAddress mac{};
  /// Static, human readable reason when !ok (goes into ERR BAD_REQUEST).
  const char *error{""};
};

/// Parse one handshake line (without its '\n').  A single trailing '\r' is
/// ignored.  The command word is case-sensitive, the MAC is not.
inline ConnectRequest parse_connect_line(std::string_view line) {
  ConnectRequest request;
  if (line.size() > MAX_LINE_LENGTH) {
    request.error = "line too long";
    return request;
  }
  if (!line.empty() && line.back() == '\r')
    line.remove_suffix(1);

  constexpr std::string_view PREFIX = "CONNECT ";
  if (line.substr(0, PREFIX.size()) != PREFIX) {
    request.error = "expected CONNECT AA:BB:CC:DD:EE:FF";
    return request;
  }
  const std::string_view mac_text = line.substr(PREFIX.size());
  if (mac_text.size() != MAC_TEXT_LENGTH) {
    request.error = "MAC address must be 17 characters";
    return request;
  }
  if (!parse_mac(mac_text, request.mac)) {
    request.error = "malformed MAC address";
    return request;
  }
  request.ok = true;
  return request;
}

/// Accumulates bytes up to the first '\n', refusing lines longer than
/// MAX_LINE_LENGTH.  Bytes after the '\n' are *not* consumed so the caller can
/// treat them as payload.
class LineBuffer {
 public:
  enum class Status : uint8_t {
    INCOMPLETE,  ///< all bytes consumed, no complete line yet
    LINE,        ///< a full line is available via line()
    TOO_LONG,    ///< line exceeded MAX_LINE_LENGTH without a newline
  };

  /// Feed bytes; @p consumed is set to how many of them were used.
  Status feed(const uint8_t *data, size_t len, size_t &consumed) {
    consumed = 0;
    if (this->complete_)
      this->reset();
    for (size_t i = 0; i < len; i++) {
      if (data[i] == '\n') {
        this->complete_ = true;
        consumed = i + 1;
        return Status::LINE;
      }
      if (this->buf_.size() >= MAX_LINE_LENGTH) {
        consumed = i;
        return Status::TOO_LONG;
      }
      this->buf_.push_back(static_cast<char>(data[i]));
    }
    consumed = len;
    return Status::INCOMPLETE;
  }

  /// The completed line without '\n'; valid until reset() or the next feed().
  std::string_view line() const { return this->buf_; }

  void reset() {
    this->buf_.clear();
    this->complete_ = false;
  }

 protected:
  std::string buf_;
  bool complete_{false};
};

// ---------------------------------------------------------------------------
// Byte FIFO (used for the two data directions in the component)
// ---------------------------------------------------------------------------

/// Simple growable byte queue with amortised O(1) append and consume.
class ByteFifo {
 public:
  size_t size() const { return this->data_.size() - this->head_; }
  bool empty() const { return this->size() == 0; }
  /// Contiguous view of the queued bytes (valid until the next append/consume).
  const uint8_t *data() const { return this->data_.data() + this->head_; }

  void append(const uint8_t *bytes, size_t len) {
    // Reclaim the consumed prefix once it dominates, so memory stays bounded.
    if (this->head_ >= COMPACT_THRESHOLD && this->head_ * 2 >= this->data_.size()) {
      this->data_.erase(this->data_.begin(), this->data_.begin() + static_cast<std::ptrdiff_t>(this->head_));
      this->head_ = 0;
    }
    this->data_.insert(this->data_.end(), bytes, bytes + len);
  }
  void append(std::string_view text) { this->append(reinterpret_cast<const uint8_t *>(text.data()), text.size()); }

  void consume(size_t len) {
    this->head_ += len < this->size() ? len : this->size();
    if (this->head_ == this->data_.size())
      this->clear();
  }
  void clear() {
    this->data_.clear();
    this->head_ = 0;
  }

 protected:
  static constexpr size_t COMPACT_THRESHOLD = 256;
  std::vector<uint8_t> data_;
  size_t head_{0};
};

// ---------------------------------------------------------------------------
// Session state machine
// ---------------------------------------------------------------------------

enum class State : uint8_t {
  IDLE,           ///< no client; a new one may connect
  AWAIT_CONNECT,  ///< banner sent, waiting for the CONNECT line
  BT_CONNECTING,  ///< SPP connect to the inverter in progress
  PIPING,         ///< transparent byte pipe
  TEARDOWN,       ///< client is gone; waiting for the SPP link to be released
};

inline const char *state_name(State state) {
  switch (state) {
    case State::IDLE:
      return "IDLE";
    case State::AWAIT_CONNECT:
      return "AWAIT_CONNECT";
    case State::BT_CONNECTING:
      return "BT_CONNECTING";
    case State::PIPING:
      return "PIPING";
    case State::TEARDOWN:
      return "TEARDOWN";
  }
  return "UNKNOWN";
}

/// What the I/O layer has to do after feeding an event to the Session.  All
/// fields default to "nothing".
struct Actions {
  /// Control line(s) to write to the client socket (before closing it).
  std::string reply;
  /// Close the client socket (after flushing `reply`).
  bool close_client{false};
  /// Start an SPP connection to `mac` (RFCOMM channel 1).
  bool bt_connect{false};
  MacAddress mac{};
  /// Disconnect the SPP link, or cancel the connect in progress.
  bool bt_disconnect{false};
  /// on_client_data() only: bytes data[payload_offset..len) are not part of
  /// the handshake and belong to the data pipe.  The caller must hold them
  /// until the pipe is open (State::PIPING) or drop them if the session ended.
  size_t payload_offset{0};
};

/// Result of a newly accepted TCP connection.
struct AcceptResult {
  bool accepted{false};
  /// Banner when accepted, "ERR BUSY ..." when rejected.  Rejected sockets are
  /// closed by the caller and never become the session's client.
  std::string reply;
};

/**
 * Pure state machine for one bridge session at a time.  Time is passed in as
 * a free-running millisecond counter (uint32_t, wrap-around safe), there is no
 * I/O and no clock access, so every transition can be unit tested.
 *
 * States: IDLE -> AWAIT_CONNECT -> BT_CONNECTING -> PIPING -> TEARDOWN -> IDLE.
 * TEARDOWN exists so that a new client is not served while the previous SPP
 * link is still being torn down (the inverter accepts one link at a time).
 */
class Session {
 public:
  Session(uint32_t bt_connect_timeout_ms, uint32_t idle_timeout_ms)
      : bt_connect_timeout_ms_(bt_connect_timeout_ms), idle_timeout_ms_(idle_timeout_ms) {}

  void set_local_mac(const MacAddress &mac) { this->local_mac_ = mac; }

  State state() const { return this->state_; }
  bool is_idle() const { return this->state_ == State::IDLE; }

  /// A new TCP client connected.
  AcceptResult on_client_accepted(uint32_t now) {
    if (this->state_ != State::IDLE)
      return {false, error_line(ErrorCode::BUSY, "bridge in use")};
    this->enter_(State::AWAIT_CONNECT, now);
    this->line_.reset();
    return {true, banner_line()};
  }

  /// Bytes received from the client socket.
  Actions on_client_data(const uint8_t *data, size_t len, uint32_t now) {
    Actions actions;
    actions.payload_offset = len;  // default: everything was consumed
    switch (this->state_) {
      case State::AWAIT_CONNECT: {
        size_t consumed = 0;
        const LineBuffer::Status status = this->line_.feed(data, len, consumed);
        if (status == LineBuffer::Status::INCOMPLETE)
          break;
        if (status == LineBuffer::Status::TOO_LONG) {
          this->reject_(actions, "line too long");
          break;
        }
        const ConnectRequest request = parse_connect_line(this->line_.line());
        this->line_.reset();
        if (!request.ok) {
          this->reject_(actions, request.error);
          break;
        }
        actions.bt_connect = true;
        actions.mac = request.mac;
        actions.payload_offset = consumed;
        this->enter_(State::BT_CONNECTING, now);
        break;
      }
      case State::BT_CONNECTING:
        // Client wrote before OK (it should not).  Not an error: the caller
        // holds the bytes and forwards them once the pipe is open.
        actions.payload_offset = 0;
        break;
      case State::PIPING:
        actions.payload_offset = 0;
        this->last_activity_ = now;
        break;
      case State::IDLE:
      case State::TEARDOWN:
        break;  // the client is already gone; drop
    }
    return actions;
  }

  /// The client closed its end (EOF) or its socket failed.
  Actions on_client_closed(uint32_t now) {
    Actions actions;
    switch (this->state_) {
      case State::AWAIT_CONNECT:
        this->state_ = State::IDLE;
        break;
      case State::BT_CONNECTING:
      case State::PIPING:
        actions.bt_disconnect = true;
        this->enter_(State::TEARDOWN, now);
        break;
      case State::IDLE:
      case State::TEARDOWN:
        break;
    }
    return actions;
  }

  /// The bridge itself gives up on the session (e.g. internal buffer
  /// overflow, SPP write failure): close the client and drop the SPP link.
  Actions on_abort(uint32_t now) {
    Actions actions;
    switch (this->state_) {
      case State::AWAIT_CONNECT:
        actions.close_client = true;
        this->state_ = State::IDLE;
        break;
      case State::BT_CONNECTING:
      case State::PIPING:
        actions.close_client = true;
        actions.bt_disconnect = true;
        this->enter_(State::TEARDOWN, now);
        break;
      case State::IDLE:
      case State::TEARDOWN:
        break;
    }
    return actions;
  }

  /// The SPP link came up.
  Actions on_bt_connected(uint32_t now) {
    Actions actions;
    if (this->state_ == State::BT_CONNECTING) {
      this->enter_(State::PIPING, now);
      this->last_activity_ = now;
      actions.reply = ok_line(this->local_mac_);
    } else {
      // Late result of a cancelled connect (or unsolicited): nobody wants it.
      actions.bt_disconnect = true;
    }
    return actions;
  }

  /// The SPP connect failed (no link exists afterwards).
  Actions on_bt_connect_failed(std::string_view detail) { return this->bt_gone_(detail); }

  /// The SPP link closed (no link exists afterwards).
  Actions on_bt_closed() { return this->bt_gone_({}); }

  /// Payload moved from the SPP link towards the client (idle tracking).
  void on_bt_data(uint32_t now) {
    if (this->state_ == State::PIPING)
      this->last_activity_ = now;
  }

  /// Time based transitions; call regularly (every loop iteration).
  Actions on_tick(uint32_t now) {
    Actions actions;
    switch (this->state_) {
      case State::AWAIT_CONNECT:
        if (elapsed_(now, this->entered_) >= CONNECT_LINE_TIMEOUT_MS) {
          actions.close_client = true;  // no message, per protocol
          this->state_ = State::IDLE;
        }
        break;
      case State::BT_CONNECTING:
        if (elapsed_(now, this->entered_) >= this->bt_connect_timeout_ms_) {
          actions.reply = error_line(ErrorCode::BT_TIMEOUT, "bluetooth connect timed out");
          actions.close_client = true;
          actions.bt_disconnect = true;
          this->enter_(State::TEARDOWN, now);
        }
        break;
      case State::PIPING:
        if (elapsed_(now, this->last_activity_) >= this->idle_timeout_ms_) {
          actions.close_client = true;
          actions.bt_disconnect = true;
          this->enter_(State::TEARDOWN, now);
        }
        break;
      case State::TEARDOWN:
        if (elapsed_(now, this->entered_) >= TEARDOWN_TIMEOUT_MS)
          this->state_ = State::IDLE;
        break;
      case State::IDLE:
        break;
    }
    return actions;
  }

 protected:
  /// Wrap-around safe elapsed time.
  static uint32_t elapsed_(uint32_t now, uint32_t since) { return now - since; }

  void enter_(State state, uint32_t now) {
    this->state_ = state;
    this->entered_ = now;
  }

  /// Reject the current client with ERR BAD_REQUEST and end the session.
  void reject_(Actions &actions, std::string_view text) {
    actions.reply = error_line(ErrorCode::BAD_REQUEST, text);
    actions.close_client = true;
    actions.payload_offset = 0;
    this->line_.reset();
    this->state_ = State::IDLE;
  }

  /// Common handling of "the SPP link is gone" (connect failed or closed).
  Actions bt_gone_(std::string_view detail) {
    Actions actions;
    switch (this->state_) {
      case State::BT_CONNECTING:
        actions.reply = error_line(ErrorCode::BT_CONNECT_FAILED,
                                   detail.empty() ? std::string_view("connection closed during setup") : detail);
        actions.close_client = true;
        this->state_ = State::IDLE;
        break;
      case State::PIPING:
        actions.close_client = true;
        this->state_ = State::IDLE;
        break;
      case State::TEARDOWN:
        this->state_ = State::IDLE;  // release confirmed
        break;
      case State::IDLE:
      case State::AWAIT_CONNECT:
        break;
    }
    return actions;
  }

  uint32_t bt_connect_timeout_ms_;
  uint32_t idle_timeout_ms_;
  MacAddress local_mac_{};
  State state_{State::IDLE};
  uint32_t entered_{0};        ///< when the current state was entered
  uint32_t last_activity_{0};  ///< last payload byte in either direction
  LineBuffer line_;
};

}  // namespace esphome::sma_bt_bridge::protocol
