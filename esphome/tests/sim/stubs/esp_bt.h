#pragma once
#include <esp_err.h>
typedef struct {
  int dummy;
} esp_bt_controller_config_t;
#define BT_CONTROLLER_INIT_CONFIG_DEFAULT() \
  { 0 }
typedef enum {
  ESP_BT_MODE_IDLE = 0,
  ESP_BT_MODE_BLE = 1,
  ESP_BT_MODE_CLASSIC_BT = 2,
  ESP_BT_MODE_BTDM = 3
} esp_bt_mode_t;
inline esp_err_t esp_bt_controller_mem_release(esp_bt_mode_t) { return ESP_OK; }
inline esp_err_t esp_bt_controller_init(esp_bt_controller_config_t *) { return ESP_OK; }
inline esp_err_t esp_bt_controller_enable(esp_bt_mode_t) { return ESP_OK; }
