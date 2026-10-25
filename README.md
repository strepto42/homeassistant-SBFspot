# SBFspot (SMA Bluetooth) for Home Assistant

A Home Assistant custom integration (HACS) for SMA solar inverters with **SMA
Bluetooth** (e.g. Sunny Boy SB 3000TL-20). The protocol is a Python port of
[SBFspot](https://github.com/SBFspot/SBFspot)'s Bluetooth code.

Home Assistant does not need Bluetooth. A small ESP32 bridge connects it to the
inverter:

```
Home Assistant --TCP:9900--> ESP32 (sma_bt_bridge) --Bluetooth Classic--> SMA inverter
```

ESPHome Bluetooth proxies cannot do this: they are BLE-only, and SMA inverters
use Bluetooth Classic (RFCOMM).

## What you need

- An **original ESP32** board (ESP32-WROOM/WROVER, e.g. "ESP32 DevKit V1").
  ESP32-S2/S3/C3/C6/H2 have no Bluetooth Classic. Use a dedicated board: the
  bridge cannot run next to `bluetooth_proxy` or other BLE components.
- The ESP32 within Bluetooth range of the inverter, and on your Wi-Fi.
- The inverter's Bluetooth address and user password (SMA default `0000`).
- Home Assistant 2026.3 or later.

## 1. Flash the bridge

1. In the ESPHome dashboard, make a new device from
   [`esphome/sma-bt-bridge.yaml`](esphome/sma-bt-bridge.yaml). Replace its
   `external_components:` block with this one, so ESPHome fetches the
   component from GitHub:

   ```yaml
   external_components:
     - source:
         type: git
         url: https://github.com/strepto42/homeassistant-SBFspot
         ref: main
         path: esphome/components
       components: [sma_bt_bridge]
   ```

2. Add `wifi_ssid`, `wifi_password` and `api_encryption_key` to your ESPHome
   `secrets.yaml`.
3. Install it to the ESP32. The first install must be over USB. ESPHome
   2026.10 or later is required.

From a clone of this repository you can also flash it directly:
`cp esphome/secrets.example.yaml esphome/secrets.yaml`, edit the values, then
`esphome run esphome/sma-bt-bridge.yaml` (on Windows: from PowerShell).

Optional end-to-end test before Home Assistant (in daylight):

```bash
python scripts/read_inverter.py --bridge <esp32-ip> --bt 00:80:25:AA:BB:CC -v
```

## 2. Install the integration

- **HACS:** add this repository as a custom repository (category
  *Integration*), install **SBFspot (SMA Bluetooth)**, restart.
- **Manual:** copy `custom_components/sbfspot/` to `config/custom_components/`
  and restart.

Then go to **Settings → Devices & services → Add integration → SBFspot (SMA
Bluetooth)**. Enter the bridge host, port (9900), inverter Bluetooth address and
password. Do this in daylight: the inverter's Bluetooth sleeps without sun.

Options: poll interval (default 60 s, 30–3600 s). Reconfigure changes the
bridge address. A wrong password starts a re-authentication flow.

## Entities

| Entity | Unit | Default |
| --- | --- | --- |
| AC power | W | on |
| AC power / voltage / current L1 | W / V / A | on |
| AC power / voltage / current L2, L3 | W / V / A | off |
| DC power / voltage / current string A | W / V / A | on |
| DC power / voltage / current string B | W / V / A | off |
| Grid frequency | Hz | on |
| Energy today, Energy total | kWh | on |
| Operating time, Feed-in time | h | on (diagnostic) |
| Temperature | °C | on |
| Status, Grid relay | text | on |
| Bluetooth signal | % | on (diagnostic) |
| Inverter online | connectivity | on (diagnostic) |

*Energy total* and *Energy today* work with the Energy dashboard.

## Night behaviour

The integration polls only from 15 minutes before sunrise to 15 minutes after
sunset. When the inverter cannot be reached:

- power and current show 0;
- voltage, frequency, temperature and status show unknown;
- *Energy total*, *Operating time* and *Feed-in time* keep their last value.
  They never drop to 0 and never go down, also after a restart;
- *Energy today* keeps its value until midnight, then shows 0.

## Debug logging

```yaml
logger:
  logs:
    custom_components.sbfspot: debug
```

The debug log shows every Bluetooth frame. The password is masked.

## Licence and credits

Licensed under [CC BY-NC-SA 3.0](LICENSE), the licence of SBFspot, from which
the protocol code is derived. SBFspot © 2012-2025 SBF. Non-commercial use only.
Not affiliated with SMA Solar Technology AG.

## Changelog

- **0.1.0** — First release: SMA Bluetooth protocol client, ESP32 bridge
  firmware, Home Assistant integration.
