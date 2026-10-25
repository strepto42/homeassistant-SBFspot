#pragma once

#include "esphome/core/defines.h"

#ifdef USE_ESP32

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <freertos/FreeRTOS.h>
#include <freertos/message_buffer.h>

#include "esphome/components/socket/socket.h"
#include "esphome/core/component.h"

#include "bridge_protocol.h"

namespace esphome::sma_bt_bridge {

/// Kinds of messages the Bluetooth task hands over to loop().
enum class BtMsgType : uint8_t {
  SPP_INIT,  ///< ESP_SPP_INIT_EVT: SPP is ready (status)
  CL_INIT,   ///< ESP_SPP_CL_INIT_EVT: connect started (status, handle)
  OPEN,      ///< ESP_SPP_OPEN_EVT: link up or connect failed (status, handle)
  CLOSE,     ///< ESP_SPP_CLOSE_EVT: link gone (status, handle, value=port status)
  WRITE,     ///< ESP_SPP_WRITE_EVT: write done (status, handle, value=len, flag=cong)
  CONG,      ///< ESP_SPP_CONG_EVT: congestion changed (handle, flag=cong)
  DATA,      ///< ESP_SPP_DATA_IND_EVT: payload follows the header
};

/// Fixed header of every message in the Bluetooth -> loop() message buffer.
/// Trivially copyable on purpose; a DATA message carries its payload bytes
/// directly behind this header.
struct BtMsg {
  BtMsgType type;
  uint8_t status;  ///< esp_spp_status_t
  uint8_t flag;    ///< congestion flag
  uint8_t reserved;
  uint32_t handle;  ///< SPP connection handle
  int32_t value;    ///< write length / port status
};

/**
 * Bridges one TCP client at a time to a Bluetooth Classic SPP (RFCOMM channel
 * 1) link.  See bridge_protocol.h for the wire protocol and the pure session
 * state machine; this class does the I/O around it:
 *
 *  - loop() (ESPHome main task): TCP listen/accept/read/write, drives the
 *    protocol::Session and issues esp_spp_* calls (which are thread safe).
 *  - Bluedroid callbacks (BTC task): never touch component state; they only
 *    copy events and received data into a FreeRTOS message buffer which
 *    loop() drains in order.
 */
class SmaBtBridge : public Component {
 public:
  void set_port(uint16_t port) { this->port_ = port; }
  void set_bt_connect_timeout(uint32_t ms) { this->bt_connect_timeout_ms_ = ms; }
  void set_idle_timeout(uint32_t ms) { this->idle_timeout_ms_ = ms; }
  void set_pin(const std::string &pin) { this->pin_ = pin; }

  void setup() override;
  void loop() override;
  void dump_config() override;
  /// After WiFi: the TCP server needs the network, the BT stack is independent.
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  // --- called from the Bluedroid (BTC) task; see sma_bt_bridge.cpp ---
  /// Translate an esp_spp_cb_event_t into messages for loop().  @p param is
  /// the esp_spp_cb_param_t of the callback.
  void post_spp_event(int event, void *param);
  /// The configured pairing PIN (constant after setup, safe to read there).
  const std::string &pin() const { return this->pin_; }

 protected:
  // --- setup helpers ---
  bool init_bluetooth_();
  bool start_listening_();
  /// BTC task only: queue a header (+ optional payload) for loop().
  bool post_message_(const BtMsg &msg, const uint8_t *payload, size_t payload_len);

  // --- loop() stages ---
  void process_bt_messages_(uint32_t now);
  void handle_bt_message_(const BtMsg &msg, const uint8_t *payload, size_t payload_len, uint32_t now);
  void accept_clients_(uint32_t now);
  void read_client_(uint32_t now);
  void pump_to_bt_(uint32_t now);
  void flush_to_client_();

  // --- session glue ---
  /// Carry out what the protocol::Session asked for.
  void apply_(const protocol::Actions &actions, uint32_t now);
  void start_bt_connect_(const protocol::MacAddress &mac, uint32_t now);
  void disconnect_bt_();
  /// The client socket failed or was closed by the peer.
  void client_gone_(const char *why, uint32_t now);
  /// Close the client socket; with @p graceful, first let queued bytes drain.
  void close_client_(bool graceful, uint32_t now);
  void release_client_();
  void log_session_end_();
  /// Send a one-line reply to a connection that is not served, then close it.
  void reject_connection_(std::unique_ptr<socket::Socket> sock, const std::string &reply);

  // --- configuration ---
  uint16_t port_{9900};
  uint32_t bt_connect_timeout_ms_{15000};
  uint32_t idle_timeout_ms_{60000};
  std::string pin_{"0000"};

  // --- TCP side (loop() only) ---
  std::unique_ptr<socket::ListenSocket> listen_;
  std::unique_ptr<socket::Socket> client_;
  /// Bytes waiting to be written to the client socket (control lines + SPP data).
  protocol::ByteFifo to_client_;
  /// Bytes read from the client that SPP has not accepted yet.
  protocol::ByteFifo to_bt_;
  bool closing_{false};  ///< client socket closes once to_client_ has drained
  uint32_t closing_since_{0};
  uint32_t last_listen_attempt_{0};

  // --- session / SPP state (loop() only) ---
  protocol::Session session_{15000, 60000};
  protocol::MacAddress local_mac_{};
  protocol::MacAddress peer_mac_{};
  bool bt_ready_{false};         ///< SPP initialised (ESP_SPP_INIT_EVT ok)
  bool bt_failed_{false};        ///< Bluetooth bring-up failed; component unusable
  uint32_t bt_handle_{0};        ///< SPP handle of the current/pending link
  bool bt_handle_valid_{false};  ///< bt_handle_ is set (CL_INIT or OPEN seen)
  bool bt_cancel_pending_{false};  ///< disconnect as soon as the handle is known
  bool write_in_flight_{false};  ///< esp_spp_write issued, WRITE_EVT not yet seen
  size_t write_len_{0};          ///< bytes handed to the write in flight
  bool congested_{false};        ///< SPP asked us to hold back (CONG_EVT / cong flag)
  uint32_t congested_since_{0};  ///< when congestion was last reported (for probing)
  uint32_t write_retry_at_{0};   ///< earliest retry after a partial, uncongested write
  bool write_retry_wait_{false};

  // --- statistics of the current session (for the logs) ---
  uint32_t session_start_{0};
  uint32_t bytes_to_bt_{0};
  uint32_t bytes_to_client_{0};

  // --- Bluetooth task -> loop() hand-over ---
  MessageBufferHandle_t messages_{nullptr};
  std::atomic<uint8_t> overflow_{0};  ///< set by the BT task when a message did not fit
  std::vector<uint8_t> bt_scratch_;   ///< message assembly buffer, BT task only
  std::vector<uint8_t> rx_scratch_;   ///< message receive buffer, loop() only
};

}  // namespace esphome::sma_bt_bridge

#endif  // USE_ESP32
