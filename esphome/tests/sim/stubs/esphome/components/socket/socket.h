#pragma once
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <memory>
#include <span>

namespace esphome::socket {
static constexpr size_t SOCKADDR_STR_LEN = 16;

class Socket {
 public:
  explicit Socket(int fd) : fd_(fd) {}
  ~Socket() { this->close(); }
  Socket(const Socket &) = delete;
  Socket &operator=(const Socket &) = delete;
  int close() {
    if (this->fd_ < 0)
      return 0;
    int r = ::close(this->fd_);
    this->fd_ = -1;
    return r;
  }
  int setsockopt(int level, int name, const void *v, socklen_t l) { return ::setsockopt(this->fd_, level, name, v, l); }
  int setblocking(bool blocking) {
    int fl = ::fcntl(this->fd_, F_GETFL, 0);
    if (fl < 0)
      return fl;
    fl = blocking ? (fl & ~O_NONBLOCK) : (fl | O_NONBLOCK);
    return ::fcntl(this->fd_, F_SETFL, fl);
  }
  int bind(const struct sockaddr *a, socklen_t l) { return ::bind(this->fd_, a, l); }
  int listen(int backlog) { return ::listen(this->fd_, backlog); }
  std::unique_ptr<Socket> accept_loop_monitored(struct sockaddr *a, socklen_t *l) {
    int fd = ::accept(this->fd_, a, l);
    if (fd == -1)
      return {};
    // lwIP on the ESP32 has a TCP send buffer of a few KB; mimic that so that
    // back-pressure towards slow clients shows up as it would on the device.
    int sndbuf = 4096;
    ::setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));
    return std::make_unique<Socket>(fd);
  }
  ssize_t read(void *buf, size_t len) { return ::read(this->fd_, buf, len); }
  ssize_t write(const void *buf, size_t len) { return ::send(this->fd_, buf, len, MSG_NOSIGNAL); }
  size_t getpeername_to(std::span<char, SOCKADDR_STR_LEN> buf) {
    struct sockaddr_in a {};
    socklen_t l = sizeof(a);
    if (::getpeername(this->fd_, reinterpret_cast<struct sockaddr *>(&a), &l) != 0) {
      buf[0] = 0;
      return 0;
    }
    inet_ntop(AF_INET, &a.sin_addr, buf.data(), SOCKADDR_STR_LEN);
    return std::strlen(buf.data());
  }

 protected:
  int fd_;
};
using ListenSocket = Socket;

inline std::unique_ptr<Socket> socket_ip_loop_monitored(int type, int protocol) {
  int fd = ::socket(AF_INET, type, protocol);
  if (fd < 0)
    return nullptr;
  return std::make_unique<Socket>(fd);
}
inline socklen_t set_sockaddr_any(struct sockaddr *addr, socklen_t addrlen, uint16_t port) {
  if (addrlen < sizeof(sockaddr_in))
    return 0;
  auto *in = reinterpret_cast<struct sockaddr_in *>(addr);
  std::memset(in, 0, sizeof(*in));
  in->sin_family = AF_INET;
  in->sin_addr.s_addr = htonl(INADDR_ANY);
  in->sin_port = htons(port);
  return sizeof(*in);
}
}  // namespace esphome::socket
