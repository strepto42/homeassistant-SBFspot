// Host simulation of the sma_bt_bridge firmware.  Run it with
// `bash esphome/tests/sim/run.sh` (builds this file and runs sim_test.py).
//
// The real sma_bt_bridge.cpp / bridge_protocol.h are compiled against stubbed
// ESPHome/ESP-IDF headers (stubs/) and linked with this file, which provides
//  - a fake Bluedroid whose callbacks run on their own thread (like the BTC task)
//  - a model of the RFCOMM TX queue (partial writes, congestion, write rejected
//    while congested) and a scriptable "inverter" (selected by the last byte of
//    the MAC address) that echoes everything back in small pieces.
//
// Scenarios (last MAC byte):
//   01 echo, normal            02 connect fails (OPEN status failure)
//   03 connect never completes 04 remote closes after the first write
//   06 slow connect (800 ms)   08 CL_INIT reports failure
//   09 disconnect is ignored   0A cancelled connect completes anyway (late OPEN)
//   0B esp_spp_connect() returns an error   0C CONG(false) event is lost
//   0D inverter floods 200 KB unsolicited after open
#include <esp_bt_device.h>
#include <esp_gap_bt_api.h>
#include <esp_spp_api.h>

#include <algorithm>
#include <atomic>
#include <cstdarg>
#include <cstring>
#include <chrono>
#include <csignal>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <functional>
#include <map>
#include <mutex>
#include <thread>
#include <vector>

#include "esphome/core/wake.h"
#include "sma_bt_bridge.h"

using namespace esphome::sma_bt_bridge;
using Clock = std::chrono::steady_clock;

namespace {

esp_spp_cb_t g_spp_cb = nullptr;
esp_bt_gap_cb_t g_gap_cb = nullptr;
const uint8_t LOCAL_ADDR[6] = {0x11, 0x22, 0x33, 0x44, 0x55, 0x66};

constexpr size_t TXQ_HIGH_WM = 768;
constexpr size_t TXQ_LOW_WM = 256;
constexpr size_t DRAIN_PER_TICK = 300;

void flog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void flog(const char *fmt, ...) {
  static std::mutex m;
  std::lock_guard<std::mutex> lock(m);
  std::printf("FAKE: ");
  va_list ap;
  va_start(ap, fmt);
  std::vprintf(fmt, ap);
  va_end(ap);
  std::printf("\n");
  std::fflush(stdout);
}

struct Link {
  uint32_t handle{0};
  uint8_t scenario{0};
  uint8_t bda[6]{};
  bool open{false};
  bool closed{false};
  bool cancelled{false};
  bool cong{false};
  bool remote_close_done{false};
  std::deque<uint8_t> outq;
  uint32_t rng{12345};
  Clock::time_point echo_at{};
};

class FakeBt {
 public:
  void start() {
    this->thread_ = std::thread([this] { this->run_(); });
  }
  void stop() {
    {
      std::lock_guard<std::mutex> lock(this->m_);
      this->stop_ = true;
    }
    this->cv_.notify_all();
    if (this->thread_.joinable())
      this->thread_.join();
  }
  void post(int delay_ms, std::function<void()> fn) {
    this->post_at(Clock::now() + std::chrono::milliseconds(delay_ms), std::move(fn));
  }
  void post_at(Clock::time_point at, std::function<void()> fn) {
    {
      std::lock_guard<std::mutex> lock(this->m_);
      this->tasks_.emplace(at, std::move(fn));
    }
    this->cv_.notify_all();
  }
  std::map<uint32_t, Link> links;  // BT thread only
  std::atomic<uint32_t> next_handle{1};

 protected:
  void run_() {
    std::unique_lock<std::mutex> lock(this->m_);
    while (!this->stop_) {
      if (this->tasks_.empty()) {
        this->cv_.wait(lock);
        continue;
      }
      auto it = this->tasks_.begin();
      if (it->first > Clock::now()) {
        this->cv_.wait_until(lock, it->first);
        continue;
      }
      auto fn = std::move(it->second);
      this->tasks_.erase(it);
      lock.unlock();
      fn();
      lock.lock();
    }
  }
  std::mutex m_;
  std::condition_variable cv_;
  std::multimap<Clock::time_point, std::function<void()>> tasks_;
  bool stop_{false};
  std::thread thread_;
};

FakeBt g_bt;

void emit(esp_spp_cb_event_t ev, esp_spp_cb_param_t &p) {
  if (g_spp_cb != nullptr)
    g_spp_cb(ev, &p);
}

void emit_close(uint32_t handle, bool async) {
  esp_spp_cb_param_t p{};
  p.close.status = ESP_SPP_SUCCESS;
  p.close.port_status = 0;
  p.close.handle = handle;
  p.close.async = async;
  flog("close event h=%u async=%d", handle, async ? 1 : 0);
  emit(ESP_SPP_CLOSE_EVT, p);
}

void emit_data(uint32_t handle, const uint8_t *data, size_t len) {
  std::vector<uint8_t> copy(data, data + len);  // the callback may only use it during the call
  esp_spp_cb_param_t p{};
  p.data_ind.status = ESP_SPP_SUCCESS;
  p.data_ind.handle = handle;
  p.data_ind.len = static_cast<uint16_t>(len);
  p.data_ind.data = copy.data();
  emit(ESP_SPP_DATA_IND_EVT, p);
}

void do_open(uint32_t h) {
  Link &l = g_bt.links[h];
  l.open = true;
  esp_spp_cb_param_t p{};
  p.open.status = ESP_SPP_SUCCESS;
  p.open.handle = h;
  std::memcpy(p.open.rem_bda, l.bda, 6);
  flog("open h=%u", h);
  emit(ESP_SPP_OPEN_EVT, p);

  // Drain tick: delivers queued TX bytes to the "inverter", which echoes them.
  struct Ticker {
    static void run(uint32_t handle) {
      auto it = g_bt.links.find(handle);
      if (it == g_bt.links.end() || it->second.closed)
        return;
      Link &lk = it->second;
      size_t n = std::min(lk.outq.size(), DRAIN_PER_TICK);
      std::vector<uint8_t> chunk(lk.outq.begin(), lk.outq.begin() + static_cast<std::ptrdiff_t>(n));
      lk.outq.erase(lk.outq.begin(), lk.outq.begin() + static_cast<std::ptrdiff_t>(n));
      // echo back in pieces of 1..40 bytes, several data events per response
      size_t off = 0;
      // Keep the echo strictly ordered across ticks: schedule on a per-link
      // monotonic timeline instead of "now + delay".
      auto at = std::max(Clock::now(), lk.echo_at);
      while (off < chunk.size()) {
        lk.rng = lk.rng * 1103515245u + 12345u;
        size_t piece = 1 + (lk.rng >> 16) % 40;
        piece = std::min(piece, chunk.size() - off);
        std::vector<uint8_t> part(chunk.begin() + static_cast<std::ptrdiff_t>(off),
                                  chunk.begin() + static_cast<std::ptrdiff_t>(off + piece));
        g_bt.post_at(at, [handle, part]() { emit_data(handle, part.data(), part.size()); });
        off += piece;
        at += std::chrono::milliseconds((lk.rng >> 8) % 2);
      }
      lk.echo_at = at;
      if (lk.cong && lk.outq.size() < TXQ_LOW_WM) {
        lk.cong = false;
        if (lk.scenario != 0x0C) {
          esp_spp_cb_param_t cp{};
          cp.cong.status = ESP_SPP_SUCCESS;
          cp.cong.handle = handle;
          cp.cong.cong = false;
          emit(ESP_SPP_CONG_EVT, cp);
        } else {
          flog("(CONG false event lost on purpose)");
        }
      }
      g_bt.post(5, [handle]() { run(handle); });
    }
  };
  g_bt.post(5, [h]() { Ticker::run(h); });

  if (l.scenario == 0x0D) {
    // Unsolicited flood: 200 KB in 500 byte indications, no pacing.
    for (int i = 0; i < 400; i++) {
      g_bt.post(10, [h]() {
        std::vector<uint8_t> blob(500, 0x5A);
        emit_data(h, blob.data(), blob.size());
      });
    }
  }
}

void do_write(uint32_t h, std::vector<uint8_t> data) {
  auto it = g_bt.links.find(h);
  esp_spp_cb_param_t p{};
  p.write.handle = h;
  if (it == g_bt.links.end() || !it->second.open || it->second.closed) {
    p.write.status = ESP_SPP_NO_CONNECTION;
    p.write.len = -1;
    p.write.cong = false;
    emit(ESP_SPP_WRITE_EVT, p);
    return;
  }
  Link &l = it->second;
  const bool old_cong = l.cong;  // like bta_jv_rfcomm_write: a write while congested is refused
  size_t accepted = 0;
  if (!old_cong) {
    while (accepted < data.size() && l.outq.size() <= TXQ_HIGH_WM) {
      size_t n = std::min<size_t>(300, data.size() - accepted);
      l.outq.insert(l.outq.end(), data.begin() + static_cast<std::ptrdiff_t>(accepted),
                    data.begin() + static_cast<std::ptrdiff_t>(accepted + n));
      accepted += n;
    }
    if (l.outq.size() > TXQ_HIGH_WM)
      l.cong = true;
    p.write.status = ESP_SPP_SUCCESS;
    p.write.len = static_cast<int>(accepted);
  } else {
    p.write.status = ESP_SPP_FAILURE;
    p.write.len = -1;
  }
  p.write.cong = l.cong;
  emit(ESP_SPP_WRITE_EVT, p);

  if (l.scenario == 0x04 && !l.remote_close_done) {
    l.remote_close_done = true;
    g_bt.post(20, [h]() {
      auto i2 = g_bt.links.find(h);
      if (i2 != g_bt.links.end() && !i2->second.closed) {
        i2->second.closed = true;
        emit_close(h, true);
      }
    });
  }
}

}  // namespace

extern "C" {

const uint8_t *esp_bt_dev_get_address(void) { return LOCAL_ADDR; }
esp_err_t esp_bt_gap_register_callback(esp_bt_gap_cb_t cb) {
  g_gap_cb = cb;
  return ESP_OK;
}
esp_err_t esp_bt_gap_set_security_param(esp_bt_sp_param_t, void *, uint8_t) { return ESP_OK; }
esp_err_t esp_bt_gap_set_pin(esp_bt_pin_type_t type, uint8_t len, esp_bt_pin_code_t pin) {
  flog("set_pin type=%d len=%u pin=%.*s", static_cast<int>(type), len, len, reinterpret_cast<const char *>(pin));
  return ESP_OK;
}
esp_err_t esp_bt_gap_set_scan_mode(esp_bt_connection_mode_t c, esp_bt_discovery_mode_t d) {
  flog("scan mode connectable=%d discoverable=%d", static_cast<int>(c), static_cast<int>(d));
  return ESP_OK;
}
esp_err_t esp_bt_gap_pin_reply(esp_bd_addr_t, bool, uint8_t, esp_bt_pin_code_t) { return ESP_OK; }
esp_err_t esp_bt_gap_ssp_confirm_reply(esp_bd_addr_t, bool) { return ESP_OK; }

esp_err_t esp_spp_register_callback(esp_spp_cb_t cb) {
  g_spp_cb = cb;
  return ESP_OK;
}

esp_err_t esp_spp_enhanced_init(const esp_spp_cfg_t *cfg) {
  if (cfg->mode != ESP_SPP_MODE_CB)
    flog("ERROR: SPP not initialised in callback mode");
  g_bt.post(10, []() {
    esp_spp_cb_param_t p{};
    p.init.status = ESP_SPP_SUCCESS;
    emit(ESP_SPP_INIT_EVT, p);
  });
  return ESP_OK;
}

esp_err_t esp_spp_connect(esp_spp_sec_t sec, esp_spp_role_t role, uint8_t scn, esp_bd_addr_t bda) {
  const uint8_t scenario = bda[5];
  flog("connect mac=%02X:%02X:%02X:%02X:%02X:%02X scn=%u sec=%u role=%d", bda[0], bda[1], bda[2], bda[3], bda[4],
       bda[5], scn, sec, static_cast<int>(role));
  if (scn != 1 || sec != ESP_SPP_SEC_NONE || role != ESP_SPP_ROLE_MASTER)
    flog("ERROR: unexpected connect parameters");
  if (scenario == 0x0B)
    return ESP_FAIL;
  const uint32_t h = g_bt.next_handle.fetch_add(1);
  uint8_t copy[6];
  std::memcpy(copy, bda, 6);
  g_bt.post(0, [h, scenario, copy]() {
    Link &l = g_bt.links[h];
    l.handle = h;
    l.scenario = scenario;
    std::memcpy(l.bda, copy, 6);
    esp_spp_cb_param_t p{};
    p.cl_init.status = scenario == 0x08 ? ESP_SPP_FAILURE : ESP_SPP_SUCCESS;
    p.cl_init.handle = h;
    emit(ESP_SPP_CL_INIT_EVT, p);
    if (scenario == 0x08)
      return;
    switch (scenario) {
      case 0x02:
        g_bt.post(100, [h]() {
          esp_spp_cb_param_t op{};
          op.open.status = ESP_SPP_FAILURE;
          op.open.handle = h;
          emit(ESP_SPP_OPEN_EVT, op);
        });
        break;
      case 0x03:
        break;  // never completes
      case 0x06:
        g_bt.post(800, [h]() {
          if (!g_bt.links[h].cancelled)
            do_open(h);
        });
        break;
      case 0x0A:
        g_bt.post(600, [h]() { do_open(h); });  // opens even if cancelled meanwhile
        break;
      default:
        g_bt.post(50, [h]() { do_open(h); });
        break;
    }
  });
  return ESP_OK;
}

esp_err_t esp_spp_disconnect(uint32_t h) {
  flog("disconnect h=%u", h);
  g_bt.post(0, [h]() {
    auto it = g_bt.links.find(h);
    if (it == g_bt.links.end() || it->second.closed)
      return;
    Link &l = it->second;
    if (l.scenario == 0x09)
      return;  // stack never answers
    if (!l.open) {
      l.cancelled = true;
      if (l.scenario == 0x0A)
        return;  // cancel gets lost, the connect completes later
    }
    g_bt.post(30, [h]() {
      auto i2 = g_bt.links.find(h);
      if (i2 != g_bt.links.end() && !i2->second.closed) {
        i2->second.closed = true;
        emit_close(h, false);
      }
    });
  });
  return ESP_OK;
}

esp_err_t esp_spp_write(uint32_t h, int len, uint8_t *p) {
  std::vector<uint8_t> data(p, p + len);  // the real stack copies, too
  g_bt.post(0, [h, data]() { do_write(h, data); });
  return ESP_OK;
}

}  // extern "C"

static volatile std::sig_atomic_t g_stop = 0;

int main(int argc, char **argv) {
  const unsigned port = argc > 1 ? static_cast<unsigned>(std::atoi(argv[1])) : 9900u;
  const unsigned bt_timeout = argc > 2 ? static_cast<unsigned>(std::atoi(argv[2])) : 1500u;
  const unsigned idle_timeout = argc > 3 ? static_cast<unsigned>(std::atoi(argv[3])) : 1500u;
  std::signal(SIGTERM, [](int) { g_stop = 1; });
  std::signal(SIGINT, [](int) { g_stop = 1; });

  SmaBtBridge bridge;
  bridge.set_port(static_cast<uint16_t>(port));
  bridge.set_bt_connect_timeout(bt_timeout);
  bridge.set_idle_timeout(idle_timeout);
  bridge.set_pin("0000");

  g_bt.start();
  bridge.setup();
  bridge.dump_config();
  std::printf("READY\n");
  std::fflush(stdout);

  while (!g_stop) {
    bridge.loop();
    // ESPHome runs loop() every 16 ms unless something wakes the main task.
    const unsigned wake_before = esphome::g_wake_count.load();
    for (int i = 0; i < 16 && esphome::g_wake_count.load() == wake_before && !g_stop; i++)
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  g_bt.stop();
  return 0;
}
