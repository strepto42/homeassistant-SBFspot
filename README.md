# SBFspot (SMA Bluetooth) for Home Assistant

A Home Assistant custom integration for SMA solar inverters with **SMA
Bluetooth**. The protocol code is a Python port of the Bluetooth code of
[SBFspot](https://github.com/SBFspot/SBFspot).

Home Assistant does not need Bluetooth. A small ESP32 bridge connects it to the
inverter:

```
Home Assistant --TCP:9900--> ESP32 (sma_bt_bridge) --Bluetooth Classic--> SMA inverter
```

ESPHome Bluetooth proxies cannot do this. They use BLE only, and SMA inverters
use Bluetooth Classic (RFCOMM).

**Tested on:** SMA Sunny Boy SB 3000TL-20, firmware 03.20.01.R. Other SMA
inverters with SMA Bluetooth will probably work. One integration entry reads
one inverter. Multi-inverter Bluetooth networks (NetID 2 and higher) are not
supported.

## What you need

- An **original ESP32** board (ESP32-WROOM or ESP32-WROVER, for example an
  "ESP32 DevKit V1"). ESP32-S2, S3, C3, C6 and H2 boards have no Bluetooth
  Classic. Use a dedicated board: the bridge cannot run together with
  `bluetooth_proxy` or other BLE components.
- The ESP32 within Bluetooth range of the inverter, and on your Wi-Fi.
- The inverter's **Bluetooth address**. SMA addresses start with `00:80:25`.
  Find it with `SBFspot -scan`, in Sunny Explorer, or in an old `SBFspot.cfg`
  (`BTAddress=`).
- The inverter's **user password** (SMA default `0000`).
- Home Assistant 2026.3 or later, and ESPHome 2026.10 or later.

## 1. Flash the bridge

1. In the ESPHome dashboard, make a new device from
   [`esphome/sma-bt-bridge.yaml`](esphome/sma-bt-bridge.yaml).
2. Replace its `external_components:` block with this one. ESPHome then gets
   the component from GitHub. Set `ref` to the release tag of the integration
   that you install, so that the firmware and the integration match:

   ```yaml
   external_components:
     - source:
         type: git
         url: https://github.com/strepto42/homeassistant-SBFspot
         ref: v0.1.2
         path: esphome/components
       components: [sma_bt_bridge]
   ```

3. Add `wifi_ssid`, `wifi_password` and `api_encryption_key` to your ESPHome
   `secrets.yaml`.
4. Install the firmware on the ESP32. The first install must be over USB.
   After that, ESPHome can update it over Wi-Fi.

Bridge options (all optional):

| Option | Default | Meaning |
| --- | --- | --- |
| `port` | `9900` | TCP port that Home Assistant connects to. |
| `bt_connect_timeout` | `15s` | Maximum time to set up the Bluetooth link. |
| `idle_timeout` | `60s` | Close a session with no data in either direction. |
| `pin` | `"0000"` | Bluetooth pairing PIN. Keep the quotes. |

You can also flash from a clone of this repository:
`cp esphome/secrets.example.yaml esphome/secrets.yaml`, set the values, then
run `esphome run esphome/sma-bt-bridge.yaml`. On Windows, use PowerShell, not
Git Bash.

Optional test without Home Assistant (in daylight):

```bash
python scripts/read_inverter.py --bridge <esp32-ip> --bt 00:80:25:AA:BB:CC -v
```

## 2. Install the integration

- **HACS:** open **HACS → ⋮ → Custom repositories**. Add
  `https://github.com/strepto42/homeassistant-SBFspot` with the type
  **Integration**. Download **SBFspot (SMA Bluetooth)**, then restart Home
  Assistant.
- **Manual:** copy `custom_components/sbfspot/` to `config/custom_components/`
  and restart Home Assistant.

This integration is a HACS *custom repository*. The HACS default list accepts
only OSI-approved licences, and this project must keep the CC BY-NC-SA 3.0
licence of SBFspot (see [Licence and credits](#licence-and-credits)).

## 3. Set up

Go to **Settings → Devices & services → Add integration → SBFspot (SMA
Bluetooth)**. Enter:

- the bridge host (IP address or host name of the ESP32) and port (`9900`);
- the inverter's Bluetooth address;
- the inverter's user password.

Do this in daylight. The inverter's Bluetooth sleeps when there is no sun.
Give the ESP32 a fixed IP address (a DHCP reservation in your router).

After setup:

- **Configure** sets the poll interval: 30 to 3600 s, default 60 s.
- **Reconfigure** changes the bridge address or the Bluetooth address. It
  refuses a different inverter.
- If the password changes, Home Assistant asks for the new one.

## Entities

| Entity | Unit | Enabled | Category |
| --- | --- | --- | --- |
| AC power | W | yes | |
| AC power / voltage / current L1 | W / V / A | yes | |
| AC power / voltage / current L2, L3 | W / V / A | no | |
| DC power / voltage / current string A | W / V / A | yes | |
| DC power / voltage / current string B | W / V / A | no | |
| Grid frequency | Hz | yes | |
| Energy today, Energy total | kWh | yes | |
| Temperature | °C | yes | |
| Status | text | yes | |
| Grid relay | text | yes | diagnostic |
| Operating time, Feed-in time | h | yes | diagnostic |
| Bluetooth signal | % | yes | diagnostic |
| Inverter online | connectivity | yes | diagnostic |

Use **Energy total** as *Solar production* in the Energy dashboard.

## How polling works

- Each poll is one complete Bluetooth session: connect, log on, read, log off,
  disconnect. A poll takes about 6 seconds. No connection stays open between
  polls.
- The integration polls only from 15 minutes before sunrise to 15 minutes
  after sunset.
- Polls never overlap. One lock per bridge covers the polls and the checks in
  the setup, reconfigure and password dialogs.
- A poll must end within the poll interval minus 2 seconds (at least 20
  seconds). A poll that takes longer is stopped and counts as missed.
- If the first attempt of a poll fails, the poll tries once more after 2
  seconds, if at least 15 seconds of its time are left.

### At night

The inverter sleeps, and the integration does not poll:

- power and current show 0. This is the only time that they show 0;
- voltage, frequency, temperature and status show unknown;
- *Energy total*, *Operating time* and *Feed-in time* keep their last value.
  They never drop to 0 and never go down, also after a restart;
- *Energy today* keeps its value until midnight, then shows 0.

### Missed polls in daylight

A missed daytime sample is never 0:

- For up to 3 missed polls in a row, the sensors keep the last values. This
  applies only if the last good poll is recent, and never across the night.
- After that, the live values show unknown and *Inverter online* is off. The
  totals and *Energy today* keep their values. If the inverter sends bad data,
  the entities become unavailable.
- The next good poll shows fresh values again.

## Troubleshooting

### Setup messages

| Message | What to do |
| --- | --- |
| Cannot connect to the bridge | Check the host, the port and that the ESP32 is on the network. |
| The bridge is busy | Another connection uses the bridge. Try again in a minute. |
| The bridge cannot reach the inverter | Try in daylight. Check the Bluetooth address and the distance. |
| The inverter rejected the password | Use the inverter's *user* password. |
| The inverter refused the logon | The inverter still has open sessions. Wait 15 minutes. |
| Different inverter | Reconfigure and re-authentication must use the same inverter. Add another inverter as a new device. |

### Poll statistics

Go to **Settings → Devices & services → SBFspot (SMA Bluetooth) → ⋮ → Download
diagnostics**. The `poll_statistics` block counts since the last restart:
polls, successes, successes after the retry, misses by cause, and the poll
durations. The password, the Bluetooth address and the serial number are
removed from the file.

### Log

By default, Home Assistant logs only warnings and errors. The integration logs
each missed poll at **INFO**, with its cause and duration, for example:

```
Missed poll 1/3: InverterUnreachableError: bridge error BT_CONNECT_FAILED: ... (after 5.5 s)
```

To see these lines until the next restart, run the action `logger.set_level` in
**Developer tools → Actions** with:

```yaml
custom_components.sbfspot: info
```

To keep the setting, add this to `configuration.yaml`. Keep
`default: warning`: without it, a new `logger:` section sets everything to
debug.

```yaml
logger:
  default: warning
  logs:
    custom_components.sbfspot: info  # or debug: also every Bluetooth frame
```

At `debug`, the log shows every Bluetooth frame. The password is masked.

## Updates

- Update the integration in HACS, then restart Home Assistant.
- Flash the bridge again only when the changelog says so. Change `ref` in the
  bridge YAML to the new release tag first.

## Licence and credits

Licensed under [CC BY-NC-SA 3.0](LICENSE), the licence of SBFspot, from which
the protocol code is derived. SBFspot © 2012-2025 SBF. Non-commercial use only.
Not affiliated with SMA Solar Technology AG.

## Changelog

- **0.1.2** — Missed polls no longer show as glitches.
  - A missed daytime sample is **unknown, not 0**. Only the night shows 0 W.
  - After a missed poll, the sensors keep the last values for up to 3 polls in
    a row, if the last good poll is recent. Never yesterday's values.
  - A failed attempt is tried once more after 2 seconds, if at least 15
    seconds of the poll time are left.
  - A poll must end within the poll interval minus 2 seconds (at least 20
    seconds).
  - Reads no longer overlap: one lock per bridge covers the polls and the setup,
    reconfigure and password dialogs.
  - The log shows each missed poll (INFO) and one warning when the sensors go
    unknown (INFO within an hour of sunrise or sunset). The diagnostics show
    poll statistics.
  - No bridge update is needed.
- **0.1.1** — Fix: after about 15 polls the inverter refused every logon
  (error code 1), and the integration asked for the password again.
  - Cause: the bridge dropped the final logoff of each poll, so each poll left
    a session open on the inverter for 15 minutes.
  - The client now waits until the logoff is sent before it closes the
    connection. The bridge now sends all queued data before it disconnects.
  - A refused logon is not a wrong password: the entities become unavailable,
    no re-authentication starts, and the integration waits 15 minutes.
  - **Flash the new bridge firmware.** The integration update alone already
    stops the problem.
- **0.1.0** — First release: SMA Bluetooth protocol client, ESP32 bridge
  firmware, Home Assistant integration.
