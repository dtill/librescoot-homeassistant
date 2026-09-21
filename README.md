# Librescoot-BLE-Client — Home Assistant integration

Companion integration for the **Librescoot-BLE-Client** ESPHome component
([`librescoot_ble_client`](../esphome-test-env/esphome_configs/my_components/librescoot_ble_client/)) —
which connects to the LibreScoot / unu Scooter Pro firmware through the nRF BLE chip and
controls the most common phone-app functions and a little more.

> **Do you need it?** For **firmware updates from an ESP32 classic: yes** — that board has no PSRAM
> and cannot hold the GitHub-CDN TLS session next to the Bluetooth stack, so this integration
> downloads the release and serves the bytes to the ESP over your local network. On an **ESP32-S3 it
> is optional**: the S3 downloads straight from GitHub, but can be pointed at this relay instead at
> any time (the ESP's **OTA Source** select). **Everything else — control, all sensors, pairing —
> works without this integration on both boards.**

This integration adds several helpers on top of what the ESP already exposes:

1. **Pairing helper** — a `librescoot_ble_client.passkey_reply` service that forwards the 6-digit
   passkey (shown on the scooter dashboard) to the component during bonding, so you don't
   have to hunt for the raw `esphome.<node>_passkey_reply` action. Codes are taken as text, so a
   leading zero survives.
2. **Pairing Repair** — the component never pairs unprompted, so when the configured scooter is
   **in range but not bonded** (its **BLE Pairing Required** problem sensor turns on — e.g. a firmware
   update reset the bond), this integration raises a fixable Repair **"Scooter detected but not
   paired"**. It shows the scooter MAC, whether it's in range and the **signal (RSSI)**, then an
   explicit **Start pairing** button that arms the ESP (the scooter then displays the passkey — this
   can take ~10–15 s), collects the code, and **only reports success once the encrypted
   `BLE Connection` is actually established** (otherwise it offers to try again).
3. **OTA helper (the "golden path")** — Home Assistant downloads the firmware **delta** assets
   from GitHub (HA does TLS effortlessly), caches them, and **serves them to the ESP over
   plain HTTP** with Range support. The ESP then only does the BLE transfer. This makes
   firmware updates work on the **ESP32-classic**, where a direct GitHub download fails (the
   `objects.githubusercontent.com` RSA-4096 TLS handshake doesn't fit the classic's heap while
   BLE is active). No manual `range_server.py` mirror needed. The ESP's **OTA Source HA Relay** sensor
   turns on while this relay is reachable, so the component knows OTA downloads are possible.
4. **Stuck-reboot helper** — when the scooter has been stuck in *pending reboot* for over 20
   minutes (an install finished but the reboot that applies it hasn't happened, or the BLE
   status latched stale), the ESP's **OTA Reboot Required** problem sensor turns on and this
   integration raises a fixable Repair — just like the passkey prompt — offering to **restart
   the scooter** (it presses the ESP's Reboot button). Also available as the
   `librescoot_ble_client.reboot_scooter` service.
5. **Find-scooter scan** — an options-flow step that briefly switches the ESP to **`scan` link
   mode**, collects nearby **"scooter"** BLE advertisements (the ESP POSTs them to this
   integration's listener), and offers their **BLE MAC** in a dropdown (strongest signal first),
   then restores the link mode. Use it to find a new scooter's MAC for the ESPHome YAML
   (`mac_address:`).

> **Status:** loaded on a live HA instance — the config entry sets up, the pairing helper, the
> plain-HTTP OTA relay and the stuck-reboot Repair are wired. Only the two-entity OTA *install*
> trigger is still pending (see below). The end-to-end Repair fire (needs a real >20-min stuck
> reboot) has not been exercised yet.

> ℹ️ **Two boards, two update entities.** The scooter is a main board (**MDB**) and a dashboard
> (**DBC**), each with its own firmware, and it **reboots after installing either** — so one call
> installs exactly one board. The integration resolves both `OTA MDB Update` and `OTA DBC Update`,
> and `librescoot_ble_client.update` takes a `component:` field (`auto` / `mdb` / `dbc`); `auto`
> picks whichever board currently reports an update and refuses if both do.

## Prerequisites

- The `librescoot_ble_client` ESPHome device is added to HA via the **ESPHome integration**, and
  it declares these entities: the **OTA MDB Update** and **OTA DBC Update** entities and the
  **OTA Source URL** text (all present in the example
  `Librescoot-BLE-Client-component-485934.yaml`).
- The ESP can reach Home Assistant over **plain HTTP** on your LAN (e.g. `http://<ha-ip>:8123`).
  If HA is HTTPS-only, the ESP would need TLS again — expose an http URL on the LAN, or use the
  local `range_server.py` mirror instead.

## Install

**Manual:** copy `custom_components/librescoot_ble_client/` into your HA `config/custom_components/`,
restart HA, then add the integration under *Settings → Devices & Services → Add Integration →
Librescoot-BLE-Client*.

**HACS (custom repository):** push the contents of this `homeassistant-integration/` folder to
a GitHub repo (so `custom_components/librescoot_ble_client/` sits at the repo root, alongside `hacs.json`).
In HACS → *Integrations → ⋮ → Custom repositories*, add that repo URL with category
**Integration**, install it, restart HA, then add the integration.

## Configure

The config flow asks for:

| Field | What |
| :--- | :--- |
| ESPHome node name | e.g. `librescoot-component-485934` (used for the pairing action `esphome.<node>_passkey_reply`). |
| Update entities | the **OTA MDB Update** and **OTA DBC Update** entities (resolved automatically). |
| OTA Source URL text entity | the component's **OTA Source URL** text. |
| GitHub repo | `librescoot/librescoot`. |
| GitHub token | optional, raises the 60/h rate limit. |
| HA base URL | the http URL the ESP uses to reach HA, e.g. `http://192.168.1.10:8123`. |
| Update channel | `nightly` / `testing` / `stable`. |

On setup the integration points the ESP's **OTA Source URL** at its plain-HTTP byte listener,
`http://<HA host>:<port>/ota/<secret>` (default port `8127`), so the ESP fetches firmware bytes
from HA. The ESP persists this URL in NVS and its **OTA Source HA Relay** sensor turns on while the
listener is reachable.

The same listener answers `GET /base/<secret>/<tag>/<file>` with the release a `.delta` was
generated against, read from the archive's own metadata. A delta only applies to the exact release
it was built from, and the scooter does not check that — it assumes the running version is the base,
applies the patch and only fails at the very end. The ESP refuses a mismatching delta up front;
letting Home Assistant read the base means boards without the memory to decompress the archive get
that protection too. It replies `204` when it cannot determine the base, which the ESP treats as
"unknown" rather than "mismatch".

Two further endpoints let the **update check itself** run over the relay, so a board that cannot
hold a TLS session next to the Bluetooth stack still gets one:

| Endpoint | Answers |
| :--- | :--- |
| `GET /check/<secret>/<channel>` | `latest <tag>`, then one tag per line. Cached for 5 minutes. |
| `GET /notes/<secret>/<tag>` | That release's notes. |

The ESP uses these only while its byte source is the relay; in direct-GitHub mode it queries the API
itself. **The SHA-256 is never relayed** — the ESP always fetches it from `api.github.com` over TLS.
That is what keeps a transferred bundle verifiable even though the bytes travel over plain HTTP: the
scooter checks the staged file against a digest that came from GitHub directly.

### Delta chaining

`GET /chain/<secret>/<component>/<from_tag>/<to_tag>` bridges two releases with one `.delta` and
answers with `name`, `size`, `sha256`, `result` and `steps`, one per line;
`GET /chainbytes/<secret>/<component>/<from_tag>/<to_tag>/<file>` serves it with Range support.
The bundle is built in the background: while that runs the endpoint answers `503 building <phase>`
and the ESP polls (showing the phase in **OTA Status**); `422` carries a one-line reason when the
span cannot be bridged, and the ESP then installs the steps one at a time.

Two ways to build, chosen by span:

- **Up to 15 releases:** the official per-release patches are composed with `xdelta3 merge`. This
  holds every input and the composed patch in memory at once — about 45× the patch bytes — so the
  run is checked against the host's free memory (from the asset sizes, before anything is
  downloaded) and refused with that reason when it would not fit.
- **Longer spans:** a fresh delta is encoded from the two full images (`xdelta3 -e`, same settings
  as the official artifacts). Memory stays around 300 MB whatever the span, the result is smaller
  than a composed patch (23 nightlies: 10.8 MB vs 14.5 MB), and the cost is downloading both
  images (~330 MB) and ~1.4 GB of temporary disk under `librescoot_ota_chain/`. The images are
  deleted after the build.

`xdelta3` is not shipped by the Home Assistant container, so a musl build for x86_64 is bundled at
`bin/xdelta3`; anything on `PATH` is used in preference. One build runs at a time. Bundles are
packed deterministically (a rebuild after a restart is byte-identical, so a staged transfer still
resumes) and cached under `librescoot_ota_chain/`, oldest evicted first.

A merged artifact is not an official release asset, so its SHA-256 is a transport check only. The
`result` field is the applied-image hash taken verbatim from the official last release of the run —
the value the scooter verifies after applying the patch — and it is what authenticity rests on. An
ESP with PSRAM fetches that official release from GitHub over TLS itself and refuses the merged
artifact if the two hashes disagree, so this endpoint does not have to be trusted. A board without
PSRAM cannot reach the release CDN and takes the value from here; it logs that it is doing so.

Verified on the vehicle: four chained installs across both scooter boards, two of them with the ESP
cross-checking the anchor. The scooter's updater logs `Delta chain applies to base (1 deltas)` — it
never learns a chain was merged, it just sees one patch whose base is the installed version.

## Use

### Pairing
The component **never pairs unprompted** — a lost/stale bond is refused, not retried, so the scooter
isn't spammed when it returns to range. Pairing is only started deliberately.

The easy path is the **Repair**: when the configured scooter is in range but unbonded, a fixable
Repair **"Scooter detected but not paired"** appears under *Settings → System → Repairs*. It shows
the scooter MAC, in-range state and **signal (RSSI)**, then:

1. **Start pairing** — arms the ESP (clears any stale bond and connects). The scooter dashboard
   then shows a 6-digit passkey; **this can take ~10–15 s** after the scooter wakes. The scooter
   must be **unlocked** and you need **physical access to its dashboard**.
2. **Enter the passkey** shown on the dashboard. It is a text field, so codes with leading zeros
   (`012345`) can be typed as displayed — a numeric field discards those. The dialog fills the ESP's
   **BLE Pairing Passkey** and presses **BLE Pairing Send Passkey**, the same two entities you can
   use by hand.
3. The Repair then **verifies the bond** — a live connection *and* the ESP reporting
   **BLE Pairing Required** as off — and only reports success once both hold (otherwise it offers to
   try again). A connection on its own proves nothing: the link is established before
   authentication and stays up even when pairing fails.

The pairing session expires ~30 s after the scooter shows the code, so enter it promptly; if it
expires the scooter simply shows a new one for the next attempt.

You can do the whole thing without this dialog, directly on the ESP: press **BLE Pairing Start**,
type the code into **BLE Pairing Passkey**, press **BLE Pairing Send Passkey**. The
`librescoot_ble_client.passkey_reply` service still works too:

```yaml
service: librescoot_ble_client.passkey_reply
data:
  pin: "012345"
```

### OTA byte source (relay vs direct GitHub)
The **Configure** form also has an **OTA byte source** select: **`HA relay`** (this integration
downloads from GitHub and serves the bytes to the ESP over plain HTTP) or **`direct GitHub`** (the
ESP — an S3+PSRAM — fetches from GitHub itself; no relay). Changing it sets the ESP's own **OTA
Source** select. While the ESP is in `direct GitHub` mode the integration **does not** touch its
OTA Source URL. The ESP's compiled default is chip-based (S3 → direct GitHub, classic → relay), so
you usually don't need to change this.

### Scooter (find / manage / pair / unbind)
*Settings → Devices & Services → Librescoot-BLE-Client → **Configure***. **One** form: the
HA-side settings plus a single **Scooter** select. The ESP's already-configured scooter (read
from its `BLE Scooter nRF MAC` sensor — a real `AA:BB:CC:DD:EE:FF`) is the default choice:

- **`<MAC>` — this ESP's scooter** → a manage step: **🔓 Remove bond (unbind)** so it re-pairs;
  **🔑 Enter passkey** appears **only** when the scooter is actually asking for one (the
  `BLE Passkey Required` sensor is on) — never for an already-bonded/connected scooter.
- **🔍 Scan for a new scooter** → progress spinner while the ESP is briefly in `scan` mode
  (~18 s), then a dropdown. The ESP's own scooter is **always** listed (even if the scan couldn't
  re-hear it); others show signal strength. A *different* scooter's MAC must go into the ESPHome
  YAML's `mac_address:` and the ESP reflashed.
- **⌨️ Enter a scooter MAC manually**, or **Keep current** to just save settings.

Each sub-step is only shown when picked here (one main flow, branches on demand). The link mode
is restored automatically. The picked MAC is stored in options for reference; it does **not**
reprogram the ESP (the `mac_address:` lives in the ESPHome YAML).

### Stuck reboot
When the ESP's **OTA Reboot Required** problem sensor turns on (the scooter has sat in *pending
reboot* for >20 min — see the component README), a fixable Repair **"Scooter restart needed"**
appears under *Settings → System → Repairs*. Fixing it restarts the scooter. Equivalent service:

```yaml
service: librescoot_ble_client.reboot_scooter
```

The **BLE Pairing Required**, **OTA Reboot Required** and **BLE Passkey Required** sensors are plain `problem`
binary sensors and are **shown by default** (no need for the integration to enable them).
**BLE Pairing Required** is tri-state: `OK` only while connected, `Problem` while in range but unbonded,
`unknown` otherwise — so it is never a false green "OK" before a bond is confirmed.

### Update
```yaml
# Prepare only (point the ESP's byte source at HA, no install):
service: librescoot_ble_client.update
data:
  install: false

# Real install of whichever board currently reports an update:
service: librescoot_ble_client.update
data:
  install: true
  component: auto     # or: mdb / dbc

# Install a specific board:
service: librescoot_ble_client.update
data:
  install: true
  component: dbc
```

Installing firmware onto a road vehicle is deliberate, so `install` defaults to off. One call
installs **one** board (the scooter reboots afterwards); run it again for the other. `component:
auto` uses the board the component currently offers — it offers only one at a time, in the right
order — and errors out rather than guessing if both are on offer. The native HA update card's
**Install** button on either entity works too. The byte source is only re-pointed at Home
Assistant while the ESP's **OTA Source** select is on `HA relay`; in `direct GitHub` mode the ESP
owns its URL and the integration leaves it alone.

## How the OTA path works

```
GitHub (TLS)            Home Assistant                     ESP32 (BLE)               Scooter
  releases/assets  ─▶  download + cache  ─▶  serve /api/librescoot_ble_client/ota  ─▶  BLE OTA  ─▶  nRF
                       (aiohttp, TLS)        (plain HTTP, Range)            transfer      staging
```

The **bulk bytes** go HA→ESP over plain HTTP, which is fast and reliable on the classic. In relay
mode the release listing and the notes come from HA too, so the classic completes a whole update
check without opening a single TLS session. The **SHA-256 stays on the ESP** — it is always fetched
from `api.github.com` over TLS by the board itself, which is what makes the plain-HTTP byte path
safe. The ESP's transfer engine (windowed, ACKed, adaptively paced, resumable) is unchanged.

## Notes / TODO for first testing

- The OTA file view (`/api/librescoot_ble_client/ota/<tag>/<file>`) is **unauthenticated** (LAN-only,
  serving public firmware assets) so the ESP can fetch without credentials.
- Consider a `DataUpdateCoordinator` to pre-cache new releases on a schedule so the native card
  is always ready, and an integration-owned `update` entity for a fully native UX.
- Multi-device: the services currently act on the first configured entry.
- Update the OTA-**install** trigger to the two-entity model (`OTA MDB Update` / `OTA DBC
  Update`). The reboot Repair, the byte relay, the delta-base lookup and the check/notes endpoints
  are current. Staging without installing is the ESP's **OTA Stage Only** switch, not a config
  option.
