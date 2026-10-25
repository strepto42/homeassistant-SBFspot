#pragma once
// Host model of a FreeRTOS message buffer: ordered, bounded (payload + 4 bytes
// of length overhead per message), thread safe, one message per receive.
#include <freertos/FreeRTOS.h>

#include <cstring>
#include <deque>
#include <mutex>
#include <vector>

struct SimMessageBuffer {
  std::mutex m;
  std::deque<std::vector<uint8_t>> q;
  size_t capacity{0};
  size_t used{0};
};
typedef SimMessageBuffer *MessageBufferHandle_t;

inline MessageBufferHandle_t xMessageBufferCreate(size_t size) {
  auto *h = new SimMessageBuffer();
  h->capacity = size;
  return h;
}
inline size_t xMessageBufferSend(MessageBufferHandle_t h, const void *data, size_t len, TickType_t) {
  std::lock_guard<std::mutex> lock(h->m);
  if (h->used + len + 4 > h->capacity)
    return 0;
  h->q.emplace_back(static_cast<const uint8_t *>(data), static_cast<const uint8_t *>(data) + len);
  h->used += len + 4;
  return len;
}
inline size_t xMessageBufferReceive(MessageBufferHandle_t h, void *buf, size_t buflen, TickType_t) {
  std::lock_guard<std::mutex> lock(h->m);
  if (h->q.empty() || h->q.front().size() > buflen)
    return 0;
  const size_t n = h->q.front().size();
  std::memcpy(buf, h->q.front().data(), n);
  h->q.pop_front();
  h->used -= n + 4;
  return n;
}
