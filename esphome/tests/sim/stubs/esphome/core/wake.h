#pragma once
#include <atomic>
namespace esphome {
inline std::atomic<unsigned> g_wake_count{0};
inline void wake_loop_threadsafe() { g_wake_count++; }
}  // namespace esphome
