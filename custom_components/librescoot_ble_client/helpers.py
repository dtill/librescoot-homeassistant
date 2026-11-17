"""Shared helpers: slugs and auto-resolution of the ESP's entities."""
from __future__ import annotations

import asyncio
import logging
import re

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

from .const import (
    CONF_GITHUB_REPO,
    CONF_GITHUB_TOKEN,
    CONF_HA_HOST,
    CONF_HA_PORT,
    CONF_KIND,
    DEFAULT_HA_PORT,
    DEFAULT_REPO,
    DOMAIN,
    KIND_RELAY,
    RELAY_STORE_KEY,
    ENTITY_CHANNEL,
    ENTITY_CONNECTION,
    ENTITY_LINK_MODE,
    ENTITY_METHOD,
    ENTITY_OTA_SOURCE,
    ENTITY_PAIR_BUTTON,
    ENTITY_PASSKEY_SEND,
    ENTITY_PASSKEY_TEXT,
    ENTITY_PAIRING_REQUIRED,
    ENTITY_PASSKEY_REQUIRED,
    ENTITY_REBOOT_BUTTON,
    ENTITY_REBOOT_REQUIRED,
    ENTITY_REMOVE_BOND,
    ENTITY_RSSI,
    ENTITY_SCOOTER_MAC,
    ENTITY_SOURCE_URL,
    ENTITY_RELAY_REACHABLE,
    ENTITY_ALARM_ARM,
    ENTITY_ALARM_ARMED,
    ENTITY_ALARM_DISARM,
    ENTITY_ALARM_ENABLED,
    ENTITY_ALARM_LAST_TRIGGER,
    ENTITY_ALARM_LAST_TRIGGER_TIME,
    ENTITY_ALARM_STATUS,
    ENTITY_INSTALL_DBC,
    ENTITY_INSTALL_MDB,
    ENTITY_UPDATE_DBC,
    ENTITY_UPDATE_MDB,
)


RELAY_KEYS = (CONF_HA_HOST, CONF_HA_PORT, CONF_GITHUB_REPO, CONF_GITHUB_TOKEN)


def is_relay(entry: ConfigEntry) -> bool:
    return entry.data.get(CONF_KIND) == KIND_RELAY


def relay_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """The HA-wide "Librescoot Relay" entry, loaded or not."""
    return next((e for e in hass.config_entries.async_entries(DOMAIN) if is_relay(e)), None)


def esp_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [e for e in hass.config_entries.async_entries(DOMAIN) if not is_relay(e)]


async def relay_settings(hass: HomeAssistant) -> dict:
    """Host, port, repo and token the relay serves with. They live on the relay entry; before one
    exists (first start after the upgrade) the first ESP entry's options still apply."""
    entry = relay_entry(hass)
    src = entry.options if entry is not None else next(
        (e.options for e in esp_entries(hass) if any(k in e.options for k in RELAY_KEYS)), {}
    )
    return {
        CONF_HA_HOST: src.get(CONF_HA_HOST) or await default_host(hass),
        CONF_HA_PORT: src.get(CONF_HA_PORT, DEFAULT_HA_PORT),
        CONF_GITHUB_REPO: src.get(CONF_GITHUB_REPO, DEFAULT_REPO),
        CONF_GITHUB_TOKEN: src.get(CONF_GITHUB_TOKEN) or None,
    }


def _relay_store(hass: HomeAssistant) -> Store:
    return Store(hass, 1, RELAY_STORE_KEY)


async def relay_was_created(hass: HomeAssistant) -> bool:
    return bool((await _relay_store(hass).async_load() or {}).get("created"))


async def mark_relay_created(hass: HomeAssistant) -> None:
    await _relay_store(hass).async_save({"created": True})


def slug(value: str) -> str:
    """ESPHome exposes user actions as service esphome.<node-slug>_<action>."""
    return re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")


async def default_host(hass: HomeAssistant) -> str:
    """The LAN IP the ESP should use to reach HA's byte listener.

    Prefer the source IP of HA's primary LAN interface (an address the ESP can always reach)
    over HA's configured URL, whose hostname may resolve to a public/reverse-proxy address.
    """
    try:
        from homeassistant.components import network

        return await network.async_get_source_ip(hass, network.const.MDNS_TARGET_IP)
    except Exception:  # noqa: BLE001 - fall back to the configured internal URL host
        from urllib.parse import urlparse

        from homeassistant.helpers.network import NoURLAvailableError, get_url

        try:
            return urlparse(get_url(hass, prefer_external=False, allow_internal=True)).hostname or ""
        except NoURLAvailableError:
            return ""


def _norm_mac(value: str) -> str:
    return re.sub(r"[^0-9a-f]", "", (value or "").lower())


def _norm_name(value: str) -> str:
    """Names compared without separators: node `librescoot-ble-apple-s3mini` and friendly name
    `Librescoot-BLE-Apple-S3-Mini` are the same device."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def all_devices(dev_reg: dr.DeviceRegistry) -> list[dr.DeviceEntry]:
    """Every device entry. Since HA 2026.11 iterating `devices` yields the entries and using it as a
    mapping is deprecated; older versions only have the mapping."""
    devices = dev_reg.devices
    return list(devices.values()) if hasattr(type(devices), "values") else list(devices)


def device_entry_ids(device: dr.DeviceEntry) -> list[str]:
    """The config entries a device belongs to (one since HA 2026.11, a set before)."""
    entry_id = getattr(device, "config_entry_id", None)
    return [entry_id] if entry_id else list(device.config_entries)


def entry_device(dev_reg: dr.DeviceRegistry, identifier: tuple[str, str], entry_id: str):
    """A config entry's own device by identifier (identifiers are unique per entry since 2026.11)."""
    if hasattr(dev_reg, "async_get_device_by_identifier"):
        return dev_reg.async_get_device_by_identifier(identifier, entry_id)
    return dev_reg.async_get_device(identifiers={identifier})


# Entities only the librescoot_ble_client component creates: a device carrying one of them is one of ours.
COMPONENT_MARKERS = (ENTITY_SOURCE_URL, ENTITY_UPDATE_MDB, ENTITY_LINK_MODE)


def _esphome_entries(hass: HomeAssistant) -> dict:
    return {e.entry_id: e for e in hass.config_entries.async_entries("esphome")}


def _esphome_entry_of(device, esp_entries: dict):
    return next((esp_entries[i] for i in device_entry_ids(device) if i in esp_entries), None)


def _device_names(device, esp_entry) -> set[str]:
    """Every name a renamed ESP might still be known by: the node name the ESPHome entry stored, the
    entry title (the name it was added with) and the device's own and user-given name."""
    names = {device.name or "", device.name_by_user or ""}
    if esp_entry is not None:
        names |= {esp_entry.data.get("device_name", ""), esp_entry.title or ""}
    return {_norm_name(n) for n in names if n}


def device_mac(device) -> str | None:
    return next((conn[1] for conn in device.connections if conn[0] == dr.CONNECTION_NETWORK_MAC), None)


def has_component(hass: HomeAssistant, device) -> bool:
    ent_reg = er.async_get(hass)
    return any(
        (e.original_name or e.name) in COMPONENT_MARKERS
        for e in er.async_entries_for_device(ent_reg, device.id, include_disabled_entities=True)
    )


def find_esphome_device(hass: HomeAssistant, node_name: str, mac: str | None):
    """Locate the ESPHome device for this node: by MAC, then by name. Only devices of an ESPHome entry
    count, so a router or another integration that knows the same MAC cannot capture the link."""
    return _find_esphome_device(hass, node_name, mac)[0]


def explain_link(hass: HomeAssistant, node_name: str, mac: str | None) -> str:
    """Why the lookup ended as it did, for the log."""
    return _find_esphome_device(hass, node_name, mac)[1]


def _find_esphome_device(hass: HomeAssistant, node_name: str, mac: str | None):
    dev_reg = dr.async_get(hass)
    esp_entries = _esphome_entries(hass)
    esp_devices = [(d, _esphome_entry_of(d, esp_entries)) for d in all_devices(dev_reg)]
    esp_devices = [(d, e) for d, e in esp_devices if e is not None]
    if mac:
        want = _norm_mac(mac)
        for device, _e in esp_devices:
            if any(_norm_mac(conn[1]) == want for conn in device.connections):
                return device, f"found by MAC {mac} ({device.name})"
    target = _norm_name(node_name)
    for device, esp_entry in esp_devices:
        if target in _device_names(device, esp_entry):
            return device, f"found by name {node_name} ({device.name})"
    seen = ", ".join(
        f"{esp_entry.data.get('device_name') or esp_entry.title} [{device_mac(device) or 'no MAC'}]"
        for device, esp_entry in esp_devices
    ) or "none"
    return None, (f"no ESPHome device with MAC {mac or '—'} or name {node_name}; "
                  f"ESPHome devices in Home Assistant: {seen}")


def component_devices(hass: HomeAssistant) -> list[dict]:
    """ESPHome devices that run the librescoot_ble_client component, for linking an entry by hand."""
    dev_reg = dr.async_get(hass)
    esp_entries = _esphome_entries(hass)
    out = []
    for device in all_devices(dev_reg):
        esp_entry = _esphome_entry_of(device, esp_entries)
        if esp_entry is None or not has_component(hass, device):
            continue
        out.append({
            "device": device,
            "node": esp_entry.data.get("device_name") or device.name or esp_entry.title,
            "mac": device_mac(device),
            "name": device.name_by_user or device.name or esp_entry.title,
        })
    return out


def resolve_entities(hass: HomeAssistant, node_name: str, mac: str | None) -> dict[str, str]:
    """Map the component's known entity names to their current HA entity_ids.

    Robust to entity_id renames: matches on the ESPHome-provided original_name, which is
    exactly the name set in the component YAML.
    """
    device = find_esphome_device(hass, node_name, mac)
    if device is None:
        return {}
    ent_reg = er.async_get(hass)
    by_name = {
        ENTITY_UPDATE_MDB: "update_mdb",
        ENTITY_UPDATE_DBC: "update_dbc",
        ENTITY_INSTALL_MDB: "install_mdb",
        ENTITY_INSTALL_DBC: "install_dbc",
        ENTITY_SOURCE_URL: "source_url",
        ENTITY_RELAY_REACHABLE: "relay_reachable",
        ENTITY_ALARM_STATUS: "alarm_status",
        ENTITY_ALARM_ENABLED: "alarm_enabled",
        ENTITY_ALARM_ARMED: "alarm_armed",
        ENTITY_ALARM_ARM: "alarm_arm",
        ENTITY_ALARM_DISARM: "alarm_disarm",
        ENTITY_ALARM_LAST_TRIGGER: "alarm_last_trigger",
        ENTITY_ALARM_LAST_TRIGGER_TIME: "alarm_last_trigger_time",
        ENTITY_CHANNEL: "channel",
        ENTITY_METHOD: "method",
        ENTITY_PASSKEY_REQUIRED: "passkey_required",
        ENTITY_PAIRING_REQUIRED: "pairing_required",
        ENTITY_REBOOT_REQUIRED: "reboot_required",
        ENTITY_REBOOT_BUTTON: "reboot_button",
        ENTITY_LINK_MODE: "link_mode",
        ENTITY_OTA_SOURCE: "ota_source",
        ENTITY_CONNECTION: "connection",
        ENTITY_REMOVE_BOND: "remove_bond",
        ENTITY_SCOOTER_MAC: "scooter_mac",
        ENTITY_PAIR_BUTTON: "pair_scooter",
        ENTITY_PASSKEY_TEXT: "passkey_text",
        ENTITY_PASSKEY_SEND: "passkey_send",
        ENTITY_RSSI: "rssi",
    }
    out: dict[str, str] = {}
    for entry in er.async_entries_for_device(ent_reg, device.id, include_disabled_entities=True):
        name = entry.original_name or entry.name
        key = by_name.get(name)
        if key:
            out[key] = entry.entity_id
        elif entry.domain == "update" and "update_legacy" not in out:
            # Pre-two-board component build (one combined update entity). Kept only as an explicit
            # fallback — never guess a board here: picking "the first update entity" would install
            # MDB firmware when DBC was meant (or vice versa).
            out["update_legacy"] = entry.entity_id
    return out


def _state(hass: HomeAssistant, entity_id: str) -> str | None:
    st = hass.states.get(entity_id)
    if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    return st.state


def _parse_discovered(raw: str) -> tuple[dict[str, int | None], str | None]:
    """Parse the ESP's scan report ("MAC@rssi,MAC@rssi*,…") into {MAC: rssi} and the ESP's own
    scooter MAC (the one marked with a trailing '*'; rssi -127 = not heard this pass)."""
    out: dict[str, int | None] = {}
    own: str | None = None
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        is_own = tok.endswith("*")
        tok = tok[:-1] if is_own else tok
        addr, _, rssi = tok.partition("@")
        try:
            out[addr.upper()] = int(rssi)
        except ValueError:
            out[addr.upper()] = None
        if is_own:
            own = addr.upper()
    return out, own


async def scan_for_scooters(
    hass: HomeAssistant, node_name: str, mac: str | None, secret: str, dwell: float = 18.0
) -> tuple[dict[str, int | None], str | None]:
    """Find scooters this ESP hears. Returns ({MAC: rssi}, own_scooter_mac); `scooters.candidates`
    adds what Home Assistant and the other ESPs know.

    The ESP is briefly put into `scan` link mode; while scanning it **POSTs** its result to this
    integration's HTTP listener (no HA entity for the raw list). We read it back from the listener
    after the dwell. The ESP always includes its own configured scooter first (marked), so it
    appears even if the scan couldn't re-hear its advert. `mac` is the ESP's own MAC (device lookup
    only) — NOT the scooter's; `secret` keys the per-device scan result on the listener.
    """
    ents = resolve_entities(hass, node_name, mac)
    link = ents.get("link_mode")
    proxy = hass.data.get(DOMAIN, {}).get("_listener")
    if not link or proxy is None:
        _LOGGER.warning("scan: node %s is missing the BLE Link Mode entity or the OTA listener", node_name)
        return {}, None

    prev = _state(hass, link) or "auto"
    restore = prev if prev in ("disconnect", "auto", "always", "interval") else "auto"  # never restore to scan
    proxy.clear_scan(secret)
    try:
        await hass.services.async_call(
            "select", "select_option", {"entity_id": link, "option": "scan"}, blocking=True
        )
        await asyncio.sleep(dwell)
        raw = proxy.get_scan(secret)
    finally:
        await hass.services.async_call(
            "select", "select_option", {"entity_id": link, "option": restore}, blocking=True
        )
    return _parse_discovered(raw)


def parse_pin(raw) -> int | None:
    """Parse a pairing passkey exactly as it appears on the scooter dashboard.

    A text field, not a number: the dashboard shows the code with leading zeros and a numeric input
    discards them as you type. Returns None if it is not 1-6 digits. ("012345" and "12345" are the
    same passkey to the BLE stack, which takes the numeric value.)
    """
    text = str(raw).strip().replace(" ", "")
    if not text.isdigit() or len(text) > 6:
        return None
    return int(text)
