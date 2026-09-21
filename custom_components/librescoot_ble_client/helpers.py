"""Shared helpers: slugs and auto-resolution of the ESP's entities."""
from __future__ import annotations

import asyncio
import logging
import re

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

_LOGGER = logging.getLogger(__name__)

from .const import (
    DOMAIN,
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
    ENTITY_INSTALL_DBC,
    ENTITY_INSTALL_MDB,
    ENTITY_UPDATE_DBC,
    ENTITY_UPDATE_MDB,
)


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


def find_esphome_device(hass: HomeAssistant, node_name: str, mac: str | None):
    """Locate the ESPHome device for this node, by MAC connection then by name."""
    dev_reg = dr.async_get(hass)
    if mac:
        want = _norm_mac(mac)
        for device in dev_reg.devices.values():
            if any(_norm_mac(conn[1]) == want for conn in device.connections):
                return device
    target = slug(node_name)
    for device in dev_reg.devices.values():
        if slug(device.name or "") == target or slug(device.name_by_user or "") == target:
            return device
    return None


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


def _parse_discovered(raw: str) -> tuple[dict[str, str], str | None]:
    """Parse the ESP's `Discovered Scooters` state ("MAC@rssi,MAC@rssi*,…") into {MAC: label} and
    the ESP's own scooter MAC (the one marked with a trailing '*', rssi -127 = not heard)."""
    out: dict[str, str] = {}
    own: str | None = None
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        is_own = tok.endswith("*")
        tok = tok[:-1] if is_own else tok
        if "@" in tok:
            addr, rssi = tok.rsplit("@", 1)
        else:
            addr, rssi = tok, ""
        if is_own:
            own = addr
            heard = rssi not in ("", "-127", "-128")
            out[addr] = f"{addr}  ({rssi} dBm — this ESP’s scooter)" if heard else f"{addr}  (this ESP’s scooter — not heard this scan)"
        else:
            out[addr] = f"{addr}  ({rssi} dBm)" if rssi else addr
    return out, own


async def scan_for_scooters(
    hass: HomeAssistant, node_name: str, mac: str | None, secret: str, dwell: float = 18.0
) -> tuple[dict[str, str], str | None]:
    """Find scooters. Returns ({MAC: label}, own_scooter_mac).

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
