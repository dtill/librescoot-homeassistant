"""Which scooters exist, where they are, and setting an ESP's scooter.

A scooter connected to an ESP stops advertising, so no scan can hear it. The picker therefore
merges three sources: this ESP's own scan, Home Assistant's Bluetooth (adapter or proxies), and
what the other ESP entries report about their scooter (connected or not, and its signal there).
"""
from __future__ import annotations

import os
import re
import time

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from . import adopt, wizard as wz
from .const import CONF_MAC, CONF_NODE
from .helpers import esp_entries, resolve_entities

NOT_SET = "not set"   # what the component's "BLE Scooter nRF MAC" shows for 00:00:00:00:00:00
IN_RANGE_S = 120      # the ESP publishes the advert RSSI every few seconds while it hears the scooter
WEAK_DBM = -85        # below this, pairing and BLE firmware transfers fail or crawl
SILENT_S = 30 * 60    # heard within this long → "silent" (busy or gone), not "not in range"
NAME_HINTS = ("scooter", "unu")


def _state(hass: HomeAssistant, eid: str | None):
    st = hass.states.get(eid) if eid else None
    return st if st is not None and st.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN) else None


def _rssi(st) -> int | None:
    try:
        return int(float(st.state)) if st is not None else None
    except ValueError:
        return None


def scooter_mac(hass: HomeAssistant, ents: dict[str, str]) -> str | None:
    """The ESP's configured scooter, None while it is not set (or not known yet)."""
    st = _state(hass, ents.get("scooter_mac"))
    if st is None or st.state.lower() in (NOT_SET, wz.NO_MAC.lower()):
        return None
    return st.state.upper()


def _ha_last_heard(hass: HomeAssistant, mac: str) -> tuple[float, int] | None:
    """(age in s, rssi) of the last advert Home Assistant's own Bluetooth heard from this MAC."""
    try:
        from homeassistant.components.bluetooth import async_last_service_info
        info = async_last_service_info(hass, mac, connectable=False)
    except Exception:  # noqa: BLE001 - no Bluetooth on this host
        return None
    if info is None:
        return None
    try:  # the clock the advert time was taken with
        from bluetooth_data_tools import monotonic_time_coarse as now
    except ImportError:
        now = time.monotonic
    return max(0.0, now() - info.time), info.rssi


def scooter_link(hass: HomeAssistant, ents: dict[str, str], entry_id: str | None = None) -> dict:
    """state: unset / connected / needs_pairing / busy / in_range (paired, not connected) / silent /
    away / unknown, plus mac, rssi, and for
    busy the other ESP's title, for silent the age of the last advert.

    A scooter connected to anything (phone, another ESP) stops advertising, so "busy with a phone"
    and "gone" look the same from here: it falls silent. Busy is only certain when another ESP of
    this integration holds it (entry_id given → the other entries are checked)."""
    mac_st = _state(hass, ents.get("scooter_mac"))
    if mac_st is None:
        return {"state": "unknown", "mac": None, "rssi": None}
    mac = scooter_mac(hass, ents)
    if mac is None:
        return {"state": "unset", "mac": None, "rssi": None}
    conn = _state(hass, ents.get("connection"))
    rssi_st = _state(hass, ents.get("rssi"))
    rssi = _rssi(rssi_st)
    if conn is not None and conn.state == "on":
        return {"state": "connected", "mac": mac, "rssi": rssi}
    pair = _state(hass, ents.get("pairing_required"))
    passkey = _state(hass, ents.get("passkey_required"))
    if (pair is not None and pair.state == "on") or (passkey is not None and passkey.state == "on"):
        # The component turns this on only while the scooter is in range and not bonded.
        fresh = rssi_st is not None and time.time() - rssi_st.last_reported.timestamp() < IN_RANGE_S
        return {"state": "needs_pairing", "mac": mac, "rssi": rssi if fresh else None}
    if entry_id is not None:
        other = _known_elsewhere(hass, entry_id).get(mac)
        if other and other["link"]["state"] == "connected":
            return {"state": "busy", "mac": mac, "rssi": other["link"].get("rssi"), "esp": other["esp"]}
    ages = []
    if rssi_st is not None and rssi is not None and rssi > -127:
        ages.append((time.time() - rssi_st.last_reported.timestamp(), rssi))
    if (ha := _ha_last_heard(hass, mac)) is not None:
        ages.append(ha)
    if not ages:
        return {"state": "away", "mac": mac, "rssi": None, "never": True}
    age, last = min(ages)
    if age < IN_RANGE_S:
        return {"state": "in_range", "mac": mac, "rssi": last}
    if age < SILENT_S:
        return {"state": "silent", "mac": mac, "rssi": last, "age": age}
    return {"state": "away", "mac": mac, "rssi": None}


def link_text(link: dict) -> tuple[str, str]:
    """(symbol, details) for the status chain."""
    s = link["state"]
    dbm = f", {link['rssi']} dBm" if link.get("rssi") is not None else ""
    if s == "busy":
        there = f", {link['rssi']} dBm there" if link.get("rssi") is not None else ""
        return "🔵", f"busy: connected to {link['esp']}{there}"
    if s == "away" and link.get("never"):
        return "⚪", "not in range, no signal from it heard yet"
    if s == "silent":
        mins = max(1, int(link["age"] // 60))
        return "🟠", (f"silent for {mins} min, last {link['rssi']} dBm: connected to a phone or another "
                      "device, or out of range")
    if s in ("connected", "in_range", "needs_pairing") and link.get("rssi") is not None \
            and link["rssi"] < WEAK_DBM:
        sym, text = _plain_link_text(s, dbm)
        return sym, (f"{text} — ⚠️ weak signal: below {WEAK_DBM} dBm pairing and firmware updates are "
                     "unreliable, move the ESP closer to the scooter")
    return _plain_link_text(s, dbm)


def _plain_link_text(s: str, dbm: str) -> tuple[str, str]:
    return {
        "needs_pairing": ("🔴", f"in range{dbm}, needs pairing"),
        "unset": ("🔴", "no scooter set"),
        "connected": ("🟢", f"connected{dbm}"),
        "in_range": ("🟡", f"in range{dbm}, paired, not connected"),
        "away": ("⚪", "not in range, not heard for over 30 min"),
    }.get(s, ("⚪", "checking…"))


def _known_elsewhere(hass: HomeAssistant, exclude_entry_id: str | None) -> dict[str, dict]:
    """Scooters configured on the other ESP entries: {MAC: {esp, link}}."""
    out: dict[str, dict] = {}
    for entry in esp_entries(hass):
        if entry.entry_id == exclude_entry_id:
            continue
        ents = resolve_entities(hass, entry.data.get(CONF_NODE, ""), entry.data.get(CONF_MAC))
        link = scooter_link(hass, ents)
        if link["mac"]:
            out[link["mac"]] = {"esp": entry.title, "link": link}
    return out


def _heard_by_ha(hass: HomeAssistant) -> dict[str, dict]:
    try:
        from homeassistant.components.bluetooth import async_discovered_service_info
        infos = list(async_discovered_service_info(hass, connectable=False))
    except Exception:  # noqa: BLE001 - no Bluetooth on this host
        return {}
    out = {}
    for info in infos:
        name = info.name or ""
        if any(h in name.lower() for h in NAME_HINTS):
            out[info.address.upper()] = {"name": name, "rssi": info.rssi}
    return out


def candidates(hass: HomeAssistant, entry_id: str | None, scan: dict[str, int | None] | None = None,
               own: str | None = None) -> list[dict[str, str]]:
    """Select options, best first: what this ESP hears (strongest first), then what Home Assistant
    hears, then scooters that only the other ESPs know (connected to them, so silent)."""
    scan = {k.upper(): v for k, v in (scan or {}).items()}
    own = own.upper() if own else None
    known = _known_elsewhere(hass, entry_id)
    heard = _heard_by_ha(hass)
    rows = []
    for mac in set(scan) | set(heard) | set(known) | ({own} if own else set()):
        here = scan.get(mac)
        here = None if here is not None and here <= -127 else here
        parts = [(f"{here} dBm here" + (" (weak)" if here < WEAK_DBM else "")) if here is not None else None]
        if here is None and mac in heard:
            parts[0] = f"{heard[mac]['rssi']} dBm via Home Assistant Bluetooth"
        if mac == own:
            parts.append("this ESP's scooter")
        elif mac in known:
            k = known[mac]
            if k["link"]["state"] == "connected":
                there = f", {k['link']['rssi']} dBm there" if k["link"].get("rssi") is not None else ""
                parts.append(f"connected to {k['esp']}{there}")
            else:
                parts.append(f"paired with {k['esp']} (not connected)")
        else:
            parts.append("new")
        name = heard.get(mac, {}).get("name")
        label = f"{mac}{'  ' + name if name else ''}  —  " + " · ".join(p for p in parts if p)
        best = here if here is not None else (heard[mac]["rssi"] if mac in heard else None)
        rows.append((0 if here is not None else 1 if mac in heard else 2, -(best or -200), mac, label))
    rows.sort()
    return [{"value": mac, "label": label} for _, _, mac, label in rows]


# ---- writing the MAC into the ESP's configuration -----------------------------------------

def _config_for_node(esphome_dir: str, node: str) -> tuple[str | None, str | None]:
    for name in adopt.list_configs(esphome_dir):
        path = os.path.join(esphome_dir, name)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if adopt.inspect(text)["node"] == node:
            return path, text
    return None, None


def write_mac(esphome_dir: str, node: str, mac: str) -> str:
    """Put the scooter MAC where the ESP's YAML takes it from. Returns what was changed, or raises
    WizardError with what the user has to do by hand. Executor only (file IO)."""
    path, text = _config_for_node(esphome_dir, node)
    if path is None:
        raise wz.WizardError(f"no configuration for node {node} in the ESPHome folder")
    info = adopt.inspect(text)
    src = info["subs"].get("ble_mac")
    if src is None:
        m = re.search(r"^\s+mac_address:\s*(.+?)\s*$", text, flags=re.M)
        src = m.group(1) if m else None
    if src is None:
        raise wz.WizardError(f"{os.path.basename(path)} has no mac_address: to set")
    m = re.match(r"!secret\s+(\S+)", src)
    if m:
        key = m.group(1)
        spath = os.path.join(esphome_dir, wz.SECRETS_FILE)
        with open(spath, encoding="utf-8") as fh:
            stext = fh.read()
        new, n = re.subn(rf'^({re.escape(key)}:\s*).*$', rf'\g<1>"{mac}"', stext, count=1, flags=re.M)
        if n != 1:
            raise wz.WizardError(f"secrets.yaml has no {key}")
        with open(spath + ".bak", "w", encoding="utf-8") as fh:
            fh.write(stext)
        wz._atomic_write(spath, new)
        return f"secrets.yaml: {key} = {mac}"
    if src.startswith("$"):
        raise wz.WizardError(f"mac_address: in {os.path.basename(path)} uses {src}; set it there")
    new, n = re.subn(r'^(\s+(?:ble_mac|mac_address):\s*)["\']?[0-9A-Fa-f:]{17}["\']?', rf'\g<1>"{mac}"',
                     text, count=1, flags=re.M)
    if n != 1:
        raise wz.WizardError(f"could not find the MAC in {os.path.basename(path)}")
    adopt.write_adopted(path, new)
    return f"{os.path.basename(path)}: mac = {mac}"
