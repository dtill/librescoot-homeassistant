# Librescoot-BLE-Component — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dtill&repository=librescoot-homeassistant&category=integration)

![Overview](images/librescoot-ble-component-ha-helper.png)

Home Assistant companion for the
[Librescoot-BLE-Client ESPHome component](https://github.com/dtill/librescoot-esphome#mdb-nrf-ble-client):
an ESP32 next to your scooter that talks to it over Bluetooth, so lock/unlock, blinkers,
seatbox, batteries, status, alarm and firmware updates show up in Home Assistant.

It is made for **offline scooters** — scooters without an LTE contract — but works for every
Librescoot scooter. The big plus over the phone app: **sensor readings and the alarm keep working
while the scooter hibernates**, and you get a **Bluetooth presence sensor** that tells you whether
the scooter is at home.

## What you need

- an **unu Scooter Pro** running **Librescoot 1.4.0 or newer** with **nRF firmware v2.12 or newer**
- an **ESP32** (classic) or **ESP32-S3** board flashed with the
  [Librescoot-BLE-Client ESPHome component](https://github.com/dtill/librescoot-esphome#mdb-nrf-ble-client)
  and added to Home Assistant through the ESPHome integration — on a classic, firmware updates
  need this integration; an ESP32-S3 can also download them from GitHub itself
- Home Assistant with HACS

## What this integration adds

The ESP does the everyday work on its own. This integration handles the moments where Home
Assistant can help:

- **Pairing made easy** — when the scooter is nearby but not paired, a Repair walks you through it:
  press *Start pairing*, type the code from the scooter's dashboard, done.
- **Firmware updates** — Home Assistant fetches the release and hands it to the ESP over your home
  network; the scooter's updates appear like any other update in Home Assistant. Each update uses
  the smallest file that fits — the HDiffPatch `.hdelta` where the scooter supports it, else the
  regular `.delta`. Before a delta update starts, it checks that the update fits the version the
  scooter runs, so a mismatching one is refused before any transfer.
- **Faster updates for scooters that are several releases behind** — several update steps are
  combined into one install and one reboot. Newer scooters (nightly `20261109T030336` or later)
  do this themselves: the ESP uploads each step and the scooter installs them together — no Home
  Assistant needed. For older scooters Home Assistant merges the steps into one file; this needs a
  Home Assistant host on x86_64, on other hosts the steps are installed one after another.
- **Stuck after an update?** — if the scooter does not reboot on its own, a Repair offers to restart it.
- **Find a new scooter** — a scan in the integration's options lists nearby scooters with their
  Bluetooth address for the ESPHome configuration.

## Install

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dtill&repository=librescoot-homeassistant&category=integration)

1. Click the button above, or add **https://github.com/dtill/librescoot-homeassistant** in HACS as a
   custom repository of type *Integration*.
2. Install **Librescoot-BLE-Component** and restart Home Assistant.
3. Your ESP is discovered automatically once it is added through the ESPHome integration — confirm
   the notification, or go to *Settings → Devices & Services → Add Integration →
   Librescoot-BLE-Client* and enter the ESP's node name.

Manual install: copy `custom_components/librescoot_ble_client/` into your Home Assistant
`config/custom_components/` folder, restart, then add the integration as in step 3.

## Where to find things

| What | Where in Home Assistant |
| :--- | :--- |
| Pairing and "scooter stuck" prompts | *Settings → System → Repairs* |
| Update channel, GitHub token, find a scooter | *Settings → Devices & Services → Librescoot-BLE-Client → Configure* |
| Firmware updates | the scooter's **OTA MDB Update** / **OTA DBC Update** entities |
| Everything the ESP can do | the ESP's ESPHome device page |

A GitHub token is optional; it only raises GitHub's request limit if you check for updates very often.

## Need more?

The full list of entities, how pairing and updates work in detail, and troubleshooting are in the
[ESPHome component's README](https://github.com/dtill/librescoot-esphome/blob/main/components/librescoot_ble_client/README.md).
