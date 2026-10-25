#include "sma_bt_bridge.h"

#ifdef USE_ESP32

#include <algorithm>
#include <cerrno>
#include <cinttypes>
#include <cstring>

#include <esp_bt.h>
#include <esp_bt_device.h>
#include <esp_bt_main.h>
#include <esp_gap_bt_api.h>
#include <esp_spp_api.h>
#include <esp_system.h>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

// Waking the main loop from the Bluetooth task keeps the added latency low;
// older ESPHome releases without wake.h fall back to the regular loop interval.
#if __has_include("esphome/core/wake.h")
#include "esphome/core/wake.h"
#define SMA_BT_BRIDGE_CAN_WAKE_LOOP
#endif

namespace esphome::sma_bt_bridge {

static const char *const TAG = "sma_bt_bridge";

namespace {

/// SBFspot (and therefore every SMA inverter) uses RFCOMM server channel 1.
constexpr uint8_t SPP_SERVER_CHANNEL = 1;

/// Size of the Bluetooth task -> loop() message buffer.  SMA packets are a few
/// hundred bytes; this leaves room for bursts while loop() is busy.
constexpr size_t MESSAGE_BUFFER_SIZE = 8192;
/// Largest payload per message (ESP_SPP_MAX_MTU is 990, longer data is split).
constexpr size_t MAX_DATA_CHUNK = 1024;
/// Bytes handed to one esp_spp_write().
constexpr size_t TX_CHUNK_SIZE = 512;

/// Stop pulling SPP data out of the message buffer while this much is queued
/// for a slow TCP client; the message buffer then provides the back-pressure.
constexpr size_t TO_CLIENT_HIGH_WATER = 4096;
/// Stop reading the TCP client while this much is waiting for SPP.
constexpr size_t TO_BT_CAPACITY = 2048;

/// How long a closing socket may keep draining queued bytes to the client.
constexpr uint32_t CLOSE_DRAIN_TIMEOUT_MS = 2000;
/// Pause after an SPP write that was accepted only partially without congestion.
constexpr uint32_t WRITE_RETRY_DELAY_MS = 10;
/// Probe interval while SPP reports congestion but never says it cleared
/// (a rejected probe is harmless: the stack reports it and nothing is lost).
constexpr uint32_t CONGESTION_PROBE_MS = 500;
/// Retry period for creating the listen socket.
constexpr uint32_t LISTEN_RETRY_MS = 5000;
constexpr int MAX_ACCEPTS_PER_LOOP = 4;

/// The single component instance; the Bluedroid callbacks have no user pointer.
SmaBtBridge *g_bridge = nullptr;

const char *spp_status_name(int status) {
  switch (status) {
    case ESP_SPP_SUCCESS:
      return "SUCCESS";
    case ESP_SPP_FAILURE:
      return "FAILURE";
    case ESP_SPP_BUSY:
      return "BUSY";
    case ESP_SPP_NO_DATA:
      return "NO_DATA";
    case ESP_SPP_NO_RESOURCE:
      return "NO_RESOURCE";
    case ESP_SPP_NEED_INIT:
      return "NEED_INIT";
    case ESP_SPP_NEED_DEINIT:
      return "NEED_DEINIT";
    case ESP_SPP_NO_CONNECTION:
      return "NO_CONNECTION";
    case ESP_SPP_NO_SERVER:
      return "NO_SERVER";
    default:
      return "UNKNOWN";
  }
}

bool would_block(int err) { return err == EWOULDBLOCK || err == EAGAIN; }

/// Read and throw away whatever the peer has already sent (bounded).  Closing
/// a socket that still holds unread input makes the stack reset the
/// connection, which can destroy the last bytes we wrote (e.g. an ERR line).
void discard_pending_input(socket::Socket &sock) {
  uint8_t discard[128];
  for (int i = 0; i < 32 && sock.read(discard, sizeof(discard)) > 0; i++) {
  }
}

// ---------------------------------------------------------------------------
// Bluedroid callbacks.  They run on the Bluetooth (BTC) task and must not
// touch component state other than through post_spp_event().
// ---------------------------------------------------------------------------

void spp_callback(esp_spp_cb_event_t event, esp_spp_cb_param_t *param) {
  if (g_bridge != nullptr)
    g_bridge->post_spp_event(event, param);
}

void gap_callback(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param) {
  switch (event) {
    case ESP_BT_GAP_AUTH_CMPL_EVT:
      if (param->auth_cmpl.stat == ESP_BT_STATUS_SUCCESS) {
        ESP_LOGI(TAG, "Bluetooth authentication with %s succeeded",
                 protocol::format_mac(param->auth_cmpl.bda).c_str());
      } else {
        ESP_LOGW(TAG, "Bluetooth authentication with %s failed (status %d); check the pin",
                 protocol::format_mac(param->auth_cmpl.bda).c_str(), static_cast<int>(param->auth_cmpl.stat));
      }
      break;

    case ESP_BT_GAP_PIN_REQ_EVT: {
      // Legacy pairing PIN request.  With the fixed PIN registered in
      // init_bluetooth_() the stack answers by itself and this event does not
      // normally fire; answer it anyway so that pairing cannot stall.
      esp_bt_pin_code_t pin_code{};
      const std::string &pin = g_bridge->pin();
      const size_t len = std::min(pin.size(), sizeof(pin_code));
      std::memcpy(pin_code, pin.data(), len);
      esp_bt_gap_pin_reply(param->pin_req.bda, true, static_cast<uint8_t>(len), pin_code);
      break;
    }

    case ESP_BT_GAP_CFM_REQ_EVT:
      // Secure Simple Pairing numeric comparison.  IO capability is "none" so
      // this is "just works"; accept.
      esp_bt_gap_ssp_confirm_reply(param->cfm_req.bda, true);
      break;

    case ESP_BT_GAP_KEY_REQ_EVT:
      ESP_LOGW(TAG, "Peer asked for a passkey, which is not supported");
      break;

    default:
      break;
  }
}

}  // namespace

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void SmaBtBridge::setup() {
  g_bridge = this;
  this->session_ = protocol::Session(this->bt_connect_timeout_ms_, this->idle_timeout_ms_);

  this->messages_ = xMessageBufferCreate(MESSAGE_BUFFER_SIZE);
  if (this->messages_ == nullptr) {
    ESP_LOGE(TAG, "Out of memory creating the message buffer");
    this->mark_failed();
    return;
  }
  this->bt_scratch_.resize(sizeof(BtMsg) + MAX_DATA_CHUNK);
  this->rx_scratch_.resize(sizeof(BtMsg) + MAX_DATA_CHUNK);

  if (!this->init_bluetooth_()) {
    this->bt_failed_ = true;
    this->mark_failed();
    return;
  }
  this->session_.set_local_mac(this->local_mac_);

  // A failure here is retried from loop().
  this->last_listen_attempt_ = millis();
  this->start_listening_();
}

bool SmaBtBridge::init_bluetooth_() {
  esp_err_t err;

  // Classic Bluetooth only: hand the BLE controller's memory back to the heap
  // (must happen before the controller is initialised, cannot be undone).
  err = esp_bt_controller_mem_release(ESP_BT_MODE_BLE);
  if (err != ESP_OK)
    ESP_LOGW(TAG, "Releasing BLE controller memory failed: %s", esp_err_to_name(err));

  esp_bt_controller_config_t controller_config = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
  err = esp_bt_controller_init(&controller_config);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Bluetooth controller init failed: %s", esp_err_to_name(err));
    return false;
  }
  err = esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Bluetooth controller enable failed: %s (is this an original ESP32?)", esp_err_to_name(err));
    return false;
  }
  err = esp_bluedroid_init();
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Bluedroid init failed: %s", esp_err_to_name(err));
    return false;
  }
  err = esp_bluedroid_enable();
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Bluedroid enable failed: %s", esp_err_to_name(err));
    return false;
  }

  err = esp_bt_gap_register_callback(gap_callback);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "GAP callback registration failed: %s", esp_err_to_name(err));
    return false;
  }
  err = esp_spp_register_callback(spp_callback);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "SPP callback registration failed: %s", esp_err_to_name(err));
    return false;
  }

  // Callback mode: data and events are delivered to spp_callback().  The
  // result arrives asynchronously as ESP_SPP_INIT_EVT.
  esp_spp_cfg_t spp_config{};
  spp_config.mode = ESP_SPP_MODE_CB;
  // Plain L2CAP basic mode, which is also what SBFspot/BlueZ talks to the
  // inverter with.  ERTM would only add a negotiation step that legacy
  // (Bluetooth 2.x) devices may not support.
  spp_config.enable_l2cap_ertm = false;
  spp_config.tx_buffer_size = 0;  // only used by the VFS mode
  err = esp_spp_enhanced_init(&spp_config);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "SPP init failed: %s", esp_err_to_name(err));
    return false;
  }

  // No input/output capability: Secure Simple Pairing degrades to "just works".
  esp_bt_io_cap_t io_capability = ESP_BT_IO_CAP_NONE;
  err = esp_bt_gap_set_security_param(ESP_BT_SP_IOCAP_MODE, &io_capability, sizeof(io_capability));
  if (err != ESP_OK)
    ESP_LOGW(TAG, "Setting the IO capability failed: %s", esp_err_to_name(err));

  // SMA inverters pair with legacy PIN codes: answer with the configured one.
  esp_bt_pin_code_t pin_code{};
  const size_t pin_len = std::min(this->pin_.size(), sizeof(pin_code));
  std::memcpy(pin_code, this->pin_.data(), pin_len);
  err = esp_bt_gap_set_pin(ESP_BT_PIN_TYPE_FIXED, static_cast<uint8_t>(pin_len), pin_code);
  if (err != ESP_OK)
    ESP_LOGW(TAG, "Setting the pairing PIN failed: %s", esp_err_to_name(err));

  // This device only ever initiates connections: stay invisible and closed.
  err = esp_bt_gap_set_scan_mode(ESP_BT_NON_CONNECTABLE, ESP_BT_NON_DISCOVERABLE);
  if (err != ESP_OK)
    ESP_LOGW(TAG, "Disabling inquiry/page scan failed: %s", esp_err_to_name(err));

  const uint8_t *address = esp_bt_dev_get_address();
  if (address == nullptr) {
    ESP_LOGE(TAG, "Could not read the local Bluetooth address");
    return false;
  }
  std::memcpy(this->local_mac_.data(), address, this->local_mac_.size());

  ESP_LOGI(TAG, "Bluetooth Classic stack up, local address %s, free heap %" PRIu32 " bytes",
           protocol::format_mac(this->local_mac_).c_str(), static_cast<uint32_t>(esp_get_free_heap_size()));
  return true;
}

bool SmaBtBridge::start_listening_() {
  this->listen_ = socket::socket_ip_loop_monitored(SOCK_STREAM, IPPROTO_TCP);
  if (this->listen_ == nullptr) {
    ESP_LOGE(TAG, "Creating the listen socket failed: errno %d", errno);
    return false;
  }
  int enable = 1;
  this->listen_->setsockopt(SOL_SOCKET, SO_REUSEADDR, &enable, sizeof(enable));

  struct sockaddr_storage address {};
  const socklen_t address_len =
      socket::set_sockaddr_any(reinterpret_cast<struct sockaddr *>(&address), sizeof(address), this->port_);
  // Non-blocking is part of the success condition: a blocking listener would
  // stall loop() inside accept().
  if (address_len == 0 || this->listen_->setblocking(false) != 0 ||
      this->listen_->bind(reinterpret_cast<struct sockaddr *>(&address), address_len) != 0 ||
      this->listen_->listen(2) != 0) {
    const int err = errno;  // read before reset(), whose close() may change it
    this->listen_.reset();
    ESP_LOGW(TAG, "Listening on port %u failed: errno %d (retrying)", this->port_, err);
    return false;
  }
  ESP_LOGI(TAG, "Listening on TCP port %u", this->port_);
  return true;
}

void SmaBtBridge::dump_config() {
  ESP_LOGCONFIG(TAG, "SMA Bluetooth Bridge:");
  ESP_LOGCONFIG(TAG, "  TCP port: %u", this->port_);
  ESP_LOGCONFIG(TAG, "  Bluetooth connect timeout: %" PRIu32 " ms", this->bt_connect_timeout_ms_);
  ESP_LOGCONFIG(TAG, "  Idle timeout: %" PRIu32 " ms", this->idle_timeout_ms_);
  ESP_LOGCONFIG(TAG, "  Pairing PIN: configured (%u characters)", static_cast<unsigned>(this->pin_.size()));
  if (this->bt_failed_ || this->is_failed()) {
    ESP_LOGCONFIG(TAG, "  Local Bluetooth address: unavailable (Bluetooth setup failed)");
  } else {
    ESP_LOGCONFIG(TAG, "  Local Bluetooth address: %s", protocol::format_mac(this->local_mac_).c_str());
  }
}

// ---------------------------------------------------------------------------
// Bluetooth task -> loop() hand-over
// ---------------------------------------------------------------------------

bool SmaBtBridge::post_message_(const BtMsg &msg, const uint8_t *payload, size_t payload_len) {
  // bt_scratch_ is only ever touched here, and this only runs on the BTC task.
  uint8_t *buffer = this->bt_scratch_.data();
  std::memcpy(buffer, &msg, sizeof(msg));
  if (payload_len > 0)
    std::memcpy(buffer + sizeof(msg), payload, payload_len);
  const size_t total = sizeof(msg) + payload_len;
  if (xMessageBufferSend(this->messages_, buffer, total, 0) != total) {
    // Never drop silently: loop() aborts the session when it sees this flag.
    this->overflow_.store(1);
    return false;
  }
#ifdef SMA_BT_BRIDGE_CAN_WAKE_LOOP
  esphome::wake_loop_threadsafe();
#endif
  return true;
}

void SmaBtBridge::post_spp_event(int event, void *param_ptr) {
  const auto *param = static_cast<const esp_spp_cb_param_t *>(param_ptr);
  BtMsg msg{};
  switch (event) {
    case ESP_SPP_INIT_EVT:
      msg.type = BtMsgType::SPP_INIT;
      msg.status = param->init.status;
      break;
    case ESP_SPP_CL_INIT_EVT:
      msg.type = BtMsgType::CL_INIT;
      msg.status = param->cl_init.status;
      msg.handle = param->cl_init.handle;
      break;
    case ESP_SPP_OPEN_EVT:
      msg.type = BtMsgType::OPEN;
      msg.status = param->open.status;
      msg.handle = param->open.handle;
      break;
    case ESP_SPP_CLOSE_EVT:
      msg.type = BtMsgType::CLOSE;
      msg.status = param->close.status;
      msg.handle = param->close.handle;
      msg.value = static_cast<int32_t>(param->close.port_status);
      break;
    case ESP_SPP_WRITE_EVT:
      msg.type = BtMsgType::WRITE;
      msg.status = param->write.status;
      msg.handle = param->write.handle;
      msg.value = param->write.len;
      msg.flag = param->write.cong ? 1 : 0;
      break;
    case ESP_SPP_CONG_EVT:
      msg.type = BtMsgType::CONG;
      msg.status = param->cong.status;
      msg.handle = param->cong.handle;
      msg.flag = param->cong.cong ? 1 : 0;
      break;
    case ESP_SPP_DATA_IND_EVT: {
      msg.type = BtMsgType::DATA;
      msg.status = param->data_ind.status;
      msg.handle = param->data_ind.handle;
      // The data pointer is only valid during this callback: copy it out.
      // Split oversized indications so that every message fits the scratch.
      const uint8_t *data = param->data_ind.data;
      size_t remaining = param->data_ind.len;
      while (remaining > 0) {
        const size_t chunk = std::min(remaining, MAX_DATA_CHUNK);
        this->post_message_(msg, data, chunk);
        data += chunk;
        remaining -= chunk;
      }
      return;
    }
    default:
      return;  // events this bridge does not care about
  }
  this->post_message_(msg, nullptr, 0);
}

// ---------------------------------------------------------------------------
// loop()
// ---------------------------------------------------------------------------

void SmaBtBridge::loop() {
  const uint32_t now = millis();

  if (this->listen_ == nullptr && now - this->last_listen_attempt_ >= LISTEN_RETRY_MS) {
    this->last_listen_attempt_ = now;
    this->start_listening_();
  }

  this->process_bt_messages_(now);
  this->accept_clients_(now);
  this->read_client_(now);
  this->pump_to_bt_(now);
  this->flush_to_client_();
  this->apply_(this->session_.on_tick(now), now);
}

void SmaBtBridge::process_bt_messages_(uint32_t now) {
  if (this->overflow_.exchange(0) != 0) {
    ESP_LOGE(TAG, "Bluetooth message buffer overflowed (client too slow?); closing the session to avoid data loss");
    this->apply_(this->session_.on_abort(now), now);
  }

  // Leave messages in the buffer while the TCP client is not keeping up; the
  // buffer then absorbs the burst (and flags an overflow if it cannot).
  while (this->to_client_.size() < TO_CLIENT_HIGH_WATER) {
    const size_t size = xMessageBufferReceive(this->messages_, this->rx_scratch_.data(), this->rx_scratch_.size(), 0);
    if (size < sizeof(BtMsg))
      break;
    BtMsg msg;
    std::memcpy(&msg, this->rx_scratch_.data(), sizeof(msg));
    this->handle_bt_message_(msg, this->rx_scratch_.data() + sizeof(BtMsg), size - sizeof(BtMsg), now);
  }
}

void SmaBtBridge::handle_bt_message_(const BtMsg &msg, const uint8_t *payload, size_t payload_len, uint32_t now) {
  switch (msg.type) {
    case BtMsgType::SPP_INIT:
      if (msg.status == ESP_SPP_SUCCESS) {
        this->bt_ready_ = true;
        ESP_LOGI(TAG, "Bluetooth SPP ready");
      } else {
        this->bt_failed_ = true;
        ESP_LOGE(TAG, "Bluetooth SPP initialisation failed: %s (%d)", spp_status_name(msg.status), msg.status);
        this->mark_failed();
      }
      break;

    case BtMsgType::CL_INIT:
      if (msg.status != ESP_SPP_SUCCESS) {
        ESP_LOGW(TAG, "Bluetooth connect could not be started: %s (%d)", spp_status_name(msg.status), msg.status);
        this->bt_handle_valid_ = false;
        this->bt_cancel_pending_ = false;
        if (this->session_.state() == protocol::State::BT_CONNECTING) {
          this->apply_(this->session_.on_bt_connect_failed(std::string("SPP connect not started: ") +
                                                           spp_status_name(msg.status)),
                       now);
        } else {
          this->apply_(this->session_.on_bt_closed(), now);  // releases a TEARDOWN
        }
        break;
      }
      this->bt_handle_ = msg.handle;
      this->bt_handle_valid_ = true;
      ESP_LOGD(TAG, "Bluetooth connect started (handle %" PRIu32 ")", msg.handle);
      if (this->bt_cancel_pending_) {
        // The session ended before the handle was known: cancel now.
        this->bt_cancel_pending_ = false;
        this->disconnect_bt_();
      }
      break;

    case BtMsgType::OPEN:
      if (this->bt_handle_valid_ && msg.handle != this->bt_handle_) {
        // Result of an earlier, abandoned attempt.
        ESP_LOGD(TAG, "Ignoring SPP open for stale handle %" PRIu32, msg.handle);
        if (msg.status == ESP_SPP_SUCCESS)
          esp_spp_disconnect(msg.handle);
        break;
      }
      if (msg.status == ESP_SPP_SUCCESS) {
        this->bt_handle_ = msg.handle;
        this->bt_handle_valid_ = true;
        this->bt_cancel_pending_ = false;
        this->write_in_flight_ = false;
        this->congested_ = false;
        this->write_retry_wait_ = false;
        ESP_LOGI(TAG, "Bluetooth link to %s established (handle %" PRIu32 ")",
                 protocol::format_mac(this->peer_mac_).c_str(), msg.handle);
        this->apply_(this->session_.on_bt_connected(now), now);
      } else {
        ESP_LOGW(TAG, "Bluetooth connect to %s failed: %s (%d)", protocol::format_mac(this->peer_mac_).c_str(),
                 spp_status_name(msg.status), msg.status);
        this->bt_handle_valid_ = false;
        this->apply_(this->session_.on_bt_connect_failed(std::string("SPP open failed: ") +
                                                         spp_status_name(msg.status)),
                     now);
      }
      break;

    case BtMsgType::CLOSE: {
      if (!this->bt_handle_valid_ || msg.handle != this->bt_handle_) {
        ESP_LOGD(TAG, "Ignoring SPP close for unknown handle %" PRIu32, msg.handle);
        break;
      }
      this->bt_handle_valid_ = false;
      this->bt_cancel_pending_ = false;
      this->write_in_flight_ = false;
      this->congested_ = false;
      this->write_retry_wait_ = false;
      ESP_LOGI(TAG, "Bluetooth link closed (%s, port status %" PRId32 ")", spp_status_name(msg.status), msg.value);
      if (this->session_.state() == protocol::State::BT_CONNECTING) {
        this->apply_(this->session_.on_bt_connect_failed("SPP link closed during connect (port status " +
                                                         std::to_string(msg.value) + ")"),
                     now);
      } else {
        this->apply_(this->session_.on_bt_closed(), now);
      }
      break;
    }

    case BtMsgType::DATA:
      if (this->client_ != nullptr && !this->closing_ && this->session_.state() == protocol::State::PIPING &&
          this->bt_handle_valid_ && msg.handle == this->bt_handle_) {
        this->to_client_.append(payload, payload_len);
        this->bytes_to_client_ += static_cast<uint32_t>(payload_len);
        this->session_.on_bt_data(now);
        ESP_LOGD(TAG, "SPP -> TCP: %u bytes", static_cast<unsigned>(payload_len));
      } else {
        ESP_LOGD(TAG, "Dropping %u bytes of SPP data (no active session)", static_cast<unsigned>(payload_len));
      }
      break;

    case BtMsgType::WRITE:
      if (!this->bt_handle_valid_ || msg.handle != this->bt_handle_)
        break;
      this->write_in_flight_ = false;
      this->congested_ = msg.flag != 0;
      if (this->congested_)
        this->congested_since_ = now;
      if (msg.status == ESP_SPP_SUCCESS) {
        // The stack may take only part of a write when its queue is full.
        const size_t written = msg.value > 0 ? std::min(static_cast<size_t>(msg.value), this->write_len_) : 0;
        this->to_bt_.consume(written);
        this->bytes_to_bt_ += static_cast<uint32_t>(written);
        ESP_LOGD(TAG, "TCP -> SPP: %u of %u bytes accepted%s", static_cast<unsigned>(written),
                 static_cast<unsigned>(this->write_len_), this->congested_ ? " (congested)" : "");
        if (written < this->write_len_ && !this->congested_) {
          this->write_retry_wait_ = true;
          this->write_retry_at_ = now + WRITE_RETRY_DELAY_MS;
        }
      } else if (this->congested_) {
        // Rejected because the link was congested: nothing was consumed, the
        // bytes stay queued and go out again once CONG_EVT clears.
        ESP_LOGD(TAG, "SPP write deferred (congested)");
      } else {
        ESP_LOGW(TAG, "SPP write failed: %s (%d); closing the session", spp_status_name(msg.status), msg.status);
        this->apply_(this->session_.on_abort(now), now);
      }
      break;

    case BtMsgType::CONG:
      if (!this->bt_handle_valid_ || msg.handle != this->bt_handle_)
        break;
      this->congested_ = msg.flag != 0;
      if (this->congested_)
        this->congested_since_ = now;
      ESP_LOGD(TAG, "SPP %s", this->congested_ ? "congested" : "congestion cleared");
      break;
  }
}

// ---------------------------------------------------------------------------
// TCP side
// ---------------------------------------------------------------------------

void SmaBtBridge::accept_clients_(uint32_t now) {
  if (this->listen_ == nullptr)
    return;
  for (int i = 0; i < MAX_ACCEPTS_PER_LOOP; i++) {
    struct sockaddr_storage address {};
    socklen_t address_len = sizeof(address);
    auto sock = this->listen_->accept_loop_monitored(reinterpret_cast<struct sockaddr *>(&address), &address_len);
    if (sock == nullptr) {
      const int err = errno;
      if (!would_block(err) && err != ECONNABORTED && err != EINTR) {
        ESP_LOGW(TAG, "Accept failed: errno %d; re-creating the listen socket", err);
        this->listen_.reset();
        this->last_listen_attempt_ = now;
      }
      return;
    }
    sock->setblocking(false);
    int enable = 1;
    // Small request/response exchanges: do not let Nagle delay them.
    sock->setsockopt(IPPROTO_TCP, TCP_NODELAY, &enable, sizeof(enable));

    char peer[socket::SOCKADDR_STR_LEN];
    sock->getpeername_to(std::span<char, socket::SOCKADDR_STR_LEN>(peer));

    protocol::AcceptResult result;
    if (this->client_ != nullptr) {
      // The previous client socket is still draining its last bytes, although
      // the session itself is already idle.
      result.reply = protocol::error_line(protocol::ErrorCode::BUSY, "bridge in use");
    } else {
      result = this->session_.on_client_accepted(now);
    }
    if (!result.accepted) {
      ESP_LOGI(TAG, "Rejecting client %s: bridge in use", peer);
      this->reject_connection_(std::move(sock), result.reply);
      continue;
    }

    this->client_ = std::move(sock);
    this->closing_ = false;
    this->to_client_.clear();
    this->to_bt_.clear();
    this->to_client_.append(result.reply);
    this->session_start_ = now;
    this->bytes_to_bt_ = 0;
    this->bytes_to_client_ = 0;
    ESP_LOGI(TAG, "Client connected from %s", peer);
  }
}

void SmaBtBridge::reject_connection_(std::unique_ptr<socket::Socket> sock, const std::string &reply) {
  // A fresh socket has an empty send buffer, so one short write always fits.
  sock->write(reply.data(), reply.size());
  discard_pending_input(*sock);
  // `sock` closes on destruction
}

void SmaBtBridge::read_client_(uint32_t now) {
  uint8_t buffer[256];
  while (this->client_ != nullptr && !this->closing_ && this->to_bt_.size() < TO_BT_CAPACITY) {
    const ssize_t n = this->client_->read(buffer, sizeof(buffer));
    if (n < 0) {
      const int err = errno;
      if (would_block(err))
        return;
      if (err == EINTR)
        continue;
      ESP_LOGD(TAG, "Client read failed: errno %d", err);
      this->client_gone_("read error", now);
      return;
    }
    if (n == 0) {
      this->client_gone_("closed by peer", now);
      return;
    }

    const protocol::Actions actions = this->session_.on_client_data(buffer, static_cast<size_t>(n), now);
    // Whatever follows the CONNECT line (or arrives while the Bluetooth link is
    // still coming up) is payload: queue it for SPP.
    const protocol::State state = this->session_.state();
    if (actions.payload_offset < static_cast<size_t>(n) &&
        (state == protocol::State::BT_CONNECTING || state == protocol::State::PIPING)) {
      this->to_bt_.append(buffer + actions.payload_offset, static_cast<size_t>(n) - actions.payload_offset);
    }
    this->apply_(actions, now);
  }
}

void SmaBtBridge::flush_to_client_() {
  if (this->client_ == nullptr)
    return;
  const uint32_t now = millis();

  // A few attempts: stop as soon as the socket takes less than offered.
  for (int attempt = 0; attempt < 4 && !this->to_client_.empty(); attempt++) {
    const size_t offered = this->to_client_.size();
    const ssize_t n = this->client_->write(this->to_client_.data(), offered);
    if (n > 0) {
      this->to_client_.consume(static_cast<size_t>(n));
      if (static_cast<size_t>(n) < offered)
        break;
      continue;
    }
    const int err = n < 0 ? errno : EAGAIN;
    if (would_block(err))
      break;
    if (err == EINTR)
      continue;
    ESP_LOGD(TAG, "Client write failed: errno %d", err);
    this->client_gone_("write error", now);
    return;
  }

  if (this->closing_ && (this->to_client_.empty() || now - this->closing_since_ >= CLOSE_DRAIN_TIMEOUT_MS)) {
    if (!this->to_client_.empty())
      ESP_LOGW(TAG, "Dropping %u bytes the client did not accept before close",
               static_cast<unsigned>(this->to_client_.size()));
    this->release_client_();
  }
}

void SmaBtBridge::client_gone_(const char *why, uint32_t now) {
  ESP_LOGI(TAG, "Client disconnected (%s)", why);
  const protocol::Actions actions = this->session_.on_client_closed(now);
  this->release_client_();
  this->apply_(actions, now);
}

void SmaBtBridge::close_client_(bool graceful, uint32_t now) {
  if (this->client_ == nullptr)
    return;
  this->flush_to_client_();  // may release the socket itself on a write error
  if (this->client_ == nullptr)
    return;
  if (graceful && !this->to_client_.empty()) {
    // Queued bytes (e.g. the last SPP data or an ERR line) are still on their way.
    this->closing_ = true;
    this->closing_since_ = now;
    return;
  }
  this->release_client_();
}

void SmaBtBridge::release_client_() {
  if (this->client_ == nullptr)
    return;
  this->log_session_end_();
  discard_pending_input(*this->client_);
  this->client_.reset();
  this->closing_ = false;
  this->to_client_.clear();
  this->to_bt_.clear();
}

void SmaBtBridge::log_session_end_() {
  ESP_LOGI(TAG, "Session closed after %" PRIu32 " ms: %" PRIu32 " bytes to the inverter, %" PRIu32 " bytes from it",
           static_cast<uint32_t>(millis() - this->session_start_), this->bytes_to_bt_, this->bytes_to_client_);
}

// ---------------------------------------------------------------------------
// Session glue
// ---------------------------------------------------------------------------

void SmaBtBridge::apply_(const protocol::Actions &actions, uint32_t now) {
  if (!actions.reply.empty() && this->client_ != nullptr)
    this->to_client_.append(actions.reply);
  if (actions.bt_connect)
    this->start_bt_connect_(actions.mac, now);
  if (actions.bt_disconnect)
    this->disconnect_bt_();
  if (actions.close_client)
    this->close_client_(true, now);
}

void SmaBtBridge::start_bt_connect_(const protocol::MacAddress &mac, uint32_t now) {
  this->peer_mac_ = mac;
  this->bt_handle_valid_ = false;
  this->bt_cancel_pending_ = false;
  this->write_in_flight_ = false;
  this->congested_ = false;
  this->write_retry_wait_ = false;

  const std::string text = protocol::format_mac(mac);
  if (!this->bt_ready_) {
    ESP_LOGW(TAG, "Cannot connect to %s: Bluetooth SPP is not ready", text.c_str());
    this->apply_(this->session_.on_bt_connect_failed("bluetooth stack not ready"), now);
    return;
  }

  ESP_LOGI(TAG, "Connecting to %s (RFCOMM channel %u)", text.c_str(), static_cast<unsigned>(SPP_SERVER_CHANNEL));
  esp_bd_addr_t address;
  std::memcpy(address, mac.data(), sizeof(address));
  const esp_err_t err = esp_spp_connect(ESP_SPP_SEC_NONE, ESP_SPP_ROLE_MASTER, SPP_SERVER_CHANNEL, address);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "esp_spp_connect to %s failed: %s", text.c_str(), esp_err_to_name(err));
    this->apply_(this->session_.on_bt_connect_failed(std::string("esp_spp_connect: ") + esp_err_to_name(err)), now);
  }
}

void SmaBtBridge::disconnect_bt_() {
  if (!this->bt_handle_valid_) {
    // The connect is under way but its handle has not been reported yet.
    this->bt_cancel_pending_ = true;
    return;
  }
  ESP_LOGD(TAG, "Disconnecting Bluetooth link (handle %" PRIu32 ")", this->bt_handle_);
  const esp_err_t err = esp_spp_disconnect(this->bt_handle_);
  if (err != ESP_OK)
    ESP_LOGW(TAG, "esp_spp_disconnect failed: %s", esp_err_to_name(err));
}

void SmaBtBridge::pump_to_bt_(uint32_t now) {
  if (this->session_.state() != protocol::State::PIPING || !this->bt_handle_valid_)
    return;
  // One write at a time; wait for its WRITE_EVT before sending more.
  if (this->write_in_flight_ || this->to_bt_.empty())
    return;
  if (this->congested_ && now - this->congested_since_ < CONGESTION_PROBE_MS)
    return;
  if (this->write_retry_wait_ && static_cast<int32_t>(now - this->write_retry_at_) < 0)
    return;
  this->write_retry_wait_ = false;

  const size_t len = std::min(this->to_bt_.size(), TX_CHUNK_SIZE);
  // esp_spp_write() takes a non-const pointer but only reads (and copies) the
  // data, so to_bt_ keeps ownership until the WRITE_EVT confirms the length.
  const esp_err_t err =
      esp_spp_write(this->bt_handle_, static_cast<int>(len), const_cast<uint8_t *>(this->to_bt_.data()));
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "esp_spp_write failed: %s; closing the session", esp_err_to_name(err));
    this->apply_(this->session_.on_abort(now), now);
    return;
  }
  this->write_in_flight_ = true;
  this->write_len_ = len;
}

}  // namespace esphome::sma_bt_bridge

#endif  // USE_ESP32
