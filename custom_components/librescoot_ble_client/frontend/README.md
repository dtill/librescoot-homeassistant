# USB detection page

`usb-panel.js` is the admin-only page at `/librescoot-usb` that the setup wizard links to. It reads
the ESP's chip through Web Serial in the browser and sends the result to Home Assistant over the
websocket command `librescoot_ble_client/usb_detected`.

`esptool-js-0.7.0.js` is the unmodified `bundle.js` of the npm package
[esptool-js 0.7.0](https://www.npmjs.com/package/esptool-js/v/0.7.0) by Espressif
(https://github.com/espressif/esptool-js), checked against the registry's integrity hash
(`sha512-9wHuW/s9t4Vs6HKcr3pra3gbRVWwaN0eabMWodWH4KEJHEYZxgmWwZ3s0F4xcWVCZOc/L98bGZ4uWoZvR8KBKA==`).
SHA-256 of the file: `00ebe6fb0b202976d3e2a3c6b9a6bd438c26a81d2af6739c95ac276a64c1a84e`.
License: Apache-2.0, see `esptool-js-LICENSE`.
