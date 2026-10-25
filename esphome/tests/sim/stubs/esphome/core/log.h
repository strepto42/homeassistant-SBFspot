#pragma once
#include <cstdarg>
#include <cstdio>
#include <mutex>
namespace esphome {
inline void sim_log(char level, const char *tag, const char *fmt, ...) __attribute__((format(printf, 3, 4)));
inline void sim_log(char level, const char *tag, const char *fmt, ...) {
  static std::mutex m;
  std::lock_guard<std::mutex> lock(m);
  std::printf("[%c][%s] ", level, tag);
  va_list ap;
  va_start(ap, fmt);
  std::vprintf(fmt, ap);
  va_end(ap);
  std::printf("\n");
  std::fflush(stdout);
}
}  // namespace esphome
#define ESP_LOGE(tag, ...) esphome::sim_log('E', tag, __VA_ARGS__)
#define ESP_LOGW(tag, ...) esphome::sim_log('W', tag, __VA_ARGS__)
#define ESP_LOGI(tag, ...) esphome::sim_log('I', tag, __VA_ARGS__)
#define ESP_LOGD(tag, ...) esphome::sim_log('D', tag, __VA_ARGS__)
#define ESP_LOGCONFIG(tag, ...) esphome::sim_log('C', tag, __VA_ARGS__)
#define ESP_LOGV(tag, ...) \
  do { \
  } while (0)
