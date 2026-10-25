#pragma once
#include <chrono>
#include <cstdint>
namespace esphome {
inline uint32_t millis() {
  using namespace std::chrono;
  return static_cast<uint32_t>(duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count());
}
}  // namespace esphome
