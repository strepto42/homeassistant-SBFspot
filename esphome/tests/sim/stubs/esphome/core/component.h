#pragma once
namespace esphome {
namespace setup_priority {
inline constexpr float AFTER_WIFI = 200.0f;
}
class Component {
 public:
  virtual ~Component() = default;
  virtual void setup() {}
  virtual void loop() {}
  virtual void dump_config() {}
  virtual float get_setup_priority() const { return 0.0f; }
  void mark_failed() { this->failed_ = true; }
  bool is_failed() const { return this->failed_; }

 protected:
  bool failed_{false};
};
}  // namespace esphome
