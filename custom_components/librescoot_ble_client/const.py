"""Constants for the Librescoot-BLE-Client integration."""

DOMAIN = "librescoot_ble_client"

# The ESPHome mDNS project name advertised by the librescoot_ble_client component
# (esphome: project: name). Used for zeroconf discovery.
ESPHOME_PROJECT_NAME = "librescoot.ble_client"

# Config-entry data keys (set once, identify the device)
CONF_NODE = "node_name"        # ESPHome node name -> esphome.<node-slug>_<action> services
CONF_MAC = "mac"               # ESP MAC, used as the unique_id (one entry per device)
CONF_SECRET = "secret"         # per-device bearer secret embedded in the OTA byte URL path
CONF_KIND = "kind"             # entry kind: "relay" for the HA-wide relay entry; absent = an ESP

# The single HA-wide "Librescoot Relay" entry: owns the byte listener and the HA-wide settings
# (host, port, GitHub repo + token). ESP entries only register their secret with it.
KIND_RELAY = "relay"
RELAY_UNIQUE_ID = "relay"
RELAY_TITLE = "Librescoot Relay"
RELAY_STORE_KEY = f"{DOMAIN}.relay"          # remembers that the relay entry was created once
SIGNAL_RELAY_READY = f"{DOMAIN}_relay_ready"  # dispatcher: listener (re)started, register again
ISSUE_RELAY_MISSING = "relay_missing"

# Config-entry option keys (editable any time via the options flow)
CONF_GITHUB_REPO = "github_repo"
CONF_GITHUB_TOKEN = "github_token"
CONF_HA_HOST = "ha_host"       # host/IP the ESP uses to reach HA's byte listener
CONF_HA_PORT = "ha_port"       # port of the plain-HTTP byte listener
CONF_CHANNEL = "channel"       # default channel (the ESP's own select is authoritative)
CONF_ALARM_CODE = "alarm_code"                        # optional PIN for the alarm panel, checked in HA
CONF_ALARM_DISARM_DISABLES = "alarm_disarm_disables"  # disarm also turns Alarm Enabled off

DEFAULT_REPO = "librescoot/librescoot"
DEFAULT_CHANNEL = "nightly"
CHANNELS = ["nightly", "testing", "stable"]
DEFAULT_HA_PORT = 8127         # plain-HTTP OTA byte listener (separate from HA's SSL port)

# The ESP's OTA Source URL is set to  http://<ha_host>:<ha_port>/ota/<secret>
# and the component appends  /<tag>/<asset-name>.  The listener (listener.py) serves that.
LISTENER_ROUTE = "/ota"

# Original entity names as defined in the component YAML — used to auto-resolve the
# ESP's entities from its device without asking the user to pick them.
# The scooter is two boards, each with its own firmware and its own reboot, so the component
# exposes one update entity (and one manual install button) per board — never a combined one.
ENTITY_UPDATE_MDB = "OTA MDB Update"
ENTITY_UPDATE_DBC = "OTA DBC Update"
ENTITY_INSTALL_MDB = "OTA MDB Install"
ENTITY_INSTALL_DBC = "OTA DBC Install"
ENTITY_SOURCE_URL = "OTA Source URL"
# Alarm entities of the component, used by the integration's alarm control panel.
ENTITY_ALARM_STATUS = "Alarm Status"
ENTITY_ALARM_ENABLED = "Alarm Enabled"
ENTITY_ALARM_ARMED = "Alarm Armed"
ENTITY_ALARM_ARM = "Alarm Arm"
ENTITY_ALARM_DISARM = "Alarm Disarm"
ENTITY_ALARM_LAST_TRIGGER = "Alarm Last Trigger"
ENTITY_ALARM_LAST_TRIGGER_TIME = "Alarm Last Trigger Time"
ENTITY_RELAY_REACHABLE = "OTA Source HA Relay"  # ESP-side check that it reaches this relay
ENTITY_CHANNEL = "OTA channel"
ENTITY_METHOD = "OTA Update Method"
ENTITY_PASSKEY_REQUIRED = "BLE Passkey Required"
# On only when the configured scooter is in range (advertising) but not bonded — drives the
# pairing Repair. Distinct from BLE Passkey Required, which is on only during an armed pairing.
ENTITY_PAIRING_REQUIRED = "BLE Pairing Required"
# On when the scooter has been stuck in "pending reboot" for >20 min (an install finished but
# the reboot that applies it hasn't happened) — drives the reboot Repair.
ENTITY_REBOOT_REQUIRED = "OTA Reboot Required"
# Button the reboot Repair presses to restart the scooter.
ENTITY_REBOOT_BUTTON = "Reboot MDB"
# Used by the "find scooter" scan: the ESP is switched to scan link mode, then the discovered
# adverts are read back.
ENTITY_LINK_MODE = "BLE Link Mode"
# Select on the ESP: "direct GitHub" (autonomous) or "HA relay" (this integration serves the bytes).
# When it reads "direct GitHub", the integration must NOT override the OTA Source URL.
ENTITY_OTA_SOURCE = "OTA Source"
ENTITY_DISCOVERED = "Discovered Scooters"
ENTITY_CONNECTION = "BLE Connection"
ENTITY_REMOVE_BOND = "BLE Pairing Delete"
ENTITY_SCOOTER_MAC = "BLE Scooter nRF MAC"
# Signal to the configured scooter (advertisement RSSI while disconnected, connection RSSI while
# connected) — shown in the pairing Repair so the user can tell whether the scooter is in range.
ENTITY_RSSI = "BLE RSSI"
# Button that arms a deliberate (re)pairing: clears any stale bond and reconnects so the scooter
# issues a fresh passkey request. The component never pairs unprompted; this is the only trigger.
ENTITY_PAIR_BUTTON = "BLE Pairing Start"
# Passkey entry on the ESP itself: the text holds the dashboard code, the button sends it.
ENTITY_PASSKEY_TEXT = "BLE Pairing Passkey"
ENTITY_PASSKEY_SEND = "BLE Pairing Send Passkey"

# Options key: the scooter MAC the user picked from the scan (informational — goes into the YAML).
CONF_DISCOVERED_MAC = "discovered_mac"

SERVICE_PASSKEY_REPLY = "passkey_reply"
SERVICE_UPDATE = "update"
SERVICE_REBOOT = "reboot_scooter"
