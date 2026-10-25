Hand-written host stand-ins that mirror the ESPHome 2026.10 and ESP-IDF 5.5 APIs
used by `sma_bt_bridge.cpp`. The real `esphome` compile job in CI is the source
of truth for API drift: if a stub and that job disagree, fix the stub.
