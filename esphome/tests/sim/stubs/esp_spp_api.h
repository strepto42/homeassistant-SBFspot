#pragma once
// Minimal subset of ESP-IDF 5.5 esp_spp_api.h: only the names, enum values and
// struct fields that sma_bt_bridge.cpp and sim_main.cpp use.
#include <esp_bt_defs.h>
#include <esp_err.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ESP_SPP_MAX_MTU (3 * 330)
#define ESP_SPP_SEC_NONE 0x0000
typedef uint16_t esp_spp_sec_t;

typedef enum {
  ESP_SPP_SUCCESS = 0,
  ESP_SPP_FAILURE,
  ESP_SPP_BUSY,
  ESP_SPP_NO_DATA,
  ESP_SPP_NO_RESOURCE,
  ESP_SPP_NEED_INIT,
  ESP_SPP_NEED_DEINIT,
  ESP_SPP_NO_CONNECTION,
  ESP_SPP_NO_SERVER,
} esp_spp_status_t;

typedef enum { ESP_SPP_ROLE_MASTER = 0, ESP_SPP_ROLE_SLAVE = 1 } esp_spp_role_t;
typedef enum { ESP_SPP_MODE_CB = 0, ESP_SPP_MODE_VFS = 1 } esp_spp_mode_t;

typedef struct {
  esp_spp_mode_t mode;
  bool enable_l2cap_ertm;
  uint16_t tx_buffer_size;
} esp_spp_cfg_t;

typedef enum {
  ESP_SPP_INIT_EVT = 0,
  ESP_SPP_UNINIT_EVT = 1,
  ESP_SPP_DISCOVERY_COMP_EVT = 8,
  ESP_SPP_OPEN_EVT = 26,
  ESP_SPP_CLOSE_EVT = 27,
  ESP_SPP_START_EVT = 28,
  ESP_SPP_CL_INIT_EVT = 29,
  ESP_SPP_DATA_IND_EVT = 30,
  ESP_SPP_CONG_EVT = 31,
  ESP_SPP_WRITE_EVT = 33,
  ESP_SPP_SRV_OPEN_EVT = 34,
  ESP_SPP_SRV_STOP_EVT = 35,
  ESP_SPP_VFS_REGISTER_EVT = 36,
  ESP_SPP_VFS_UNREGISTER_EVT = 37,
} esp_spp_cb_event_t;

typedef union {
  struct spp_init_evt_param {
    esp_spp_status_t status;
  } init;
  struct spp_open_evt_param {
    esp_spp_status_t status;
    uint32_t handle;
    int fd;
    esp_bd_addr_t rem_bda;
  } open;
  struct spp_close_evt_param {
    esp_spp_status_t status;
    uint32_t port_status;
    uint32_t handle;
    bool async;
  } close;
  struct spp_cl_init_evt_param {
    esp_spp_status_t status;
    uint32_t handle;
    uint8_t sec_id;
    bool use_co;
  } cl_init;
  struct spp_write_evt_param {
    esp_spp_status_t status;
    uint32_t handle;
    int len;
    bool cong;
  } write;
  struct spp_data_ind_evt_param {
    esp_spp_status_t status;
    uint32_t handle;
    uint16_t len;
    uint8_t *data;
  } data_ind;
  struct spp_cong_evt_param {
    esp_spp_status_t status;
    uint32_t handle;
    bool cong;
  } cong;
} esp_spp_cb_param_t;

typedef void (*esp_spp_cb_t)(esp_spp_cb_event_t event, esp_spp_cb_param_t *param);

esp_err_t esp_spp_register_callback(esp_spp_cb_t callback);
esp_err_t esp_spp_enhanced_init(const esp_spp_cfg_t *cfg);
esp_err_t esp_spp_connect(esp_spp_sec_t sec_mask, esp_spp_role_t role, uint8_t remote_scn,
                          esp_bd_addr_t peer_bd_addr);
esp_err_t esp_spp_disconnect(uint32_t handle);
esp_err_t esp_spp_write(uint32_t handle, int len, uint8_t *p_data);

#ifdef __cplusplus
}
#endif
