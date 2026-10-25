#pragma once
#include <esp_bt_defs.h>
#include <esp_err.h>
typedef enum { ESP_BT_STATUS_SUCCESS = 0, ESP_BT_STATUS_FAIL = 1 } esp_bt_status_t;
typedef enum {
  ESP_BT_GAP_AUTH_CMPL_EVT,
  ESP_BT_GAP_PIN_REQ_EVT,
  ESP_BT_GAP_CFM_REQ_EVT,
  ESP_BT_GAP_KEY_NOTIF_EVT,
  ESP_BT_GAP_KEY_REQ_EVT,
} esp_bt_gap_cb_event_t;
typedef union {
  struct {
    esp_bd_addr_t bda;
    esp_bt_status_t stat;
    uint8_t device_name[249];
  } auth_cmpl;
  struct {
    esp_bd_addr_t bda;
    bool min_16_digit;
  } pin_req;
  struct {
    esp_bd_addr_t bda;
    uint32_t num_val;
  } cfm_req;
} esp_bt_gap_cb_param_t;
typedef void (*esp_bt_gap_cb_t)(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param);
typedef uint8_t esp_bt_pin_code_t[16];
typedef enum { ESP_BT_PIN_TYPE_VARIABLE = 0, ESP_BT_PIN_TYPE_FIXED = 1 } esp_bt_pin_type_t;
typedef enum { ESP_BT_SP_IOCAP_MODE = 0 } esp_bt_sp_param_t;
#define ESP_BT_IO_CAP_NONE 3
typedef uint8_t esp_bt_io_cap_t;
typedef enum { ESP_BT_NON_CONNECTABLE, ESP_BT_CONNECTABLE } esp_bt_connection_mode_t;
typedef enum {
  ESP_BT_NON_DISCOVERABLE,
  ESP_BT_LIMITED_DISCOVERABLE,
  ESP_BT_GENERAL_DISCOVERABLE
} esp_bt_discovery_mode_t;
extern "C" {
esp_err_t esp_bt_gap_register_callback(esp_bt_gap_cb_t callback);
esp_err_t esp_bt_gap_set_security_param(esp_bt_sp_param_t param_type, void *value, uint8_t len);
esp_err_t esp_bt_gap_set_pin(esp_bt_pin_type_t pin_type, uint8_t pin_code_len, esp_bt_pin_code_t pin_code);
esp_err_t esp_bt_gap_set_scan_mode(esp_bt_connection_mode_t c_mode, esp_bt_discovery_mode_t d_mode);
esp_err_t esp_bt_gap_pin_reply(esp_bd_addr_t bd_addr, bool accept, uint8_t pin_code_len, esp_bt_pin_code_t pin_code);
esp_err_t esp_bt_gap_ssp_confirm_reply(esp_bd_addr_t bd_addr, bool accept);
}
