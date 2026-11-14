"""ESP setup wizard: build an ESPHome YAML for the librescoot_ble_client component from the
example config on GitHub, and append the secrets it needs.

Pure functions plus small file helpers; the config-flow steps live in config_flow.py. Everything
is text substitution on known lines of the example (comments survive), never a YAML round-trip.
"""
from __future__ import annotations

import base64
import os
import re
import secrets as pysecrets
import tempfile
import time

EXAMPLE_REPO = "dtill/librescoot-esphome"
EXAMPLE_FILE = "librescoot-ble-client-example.yaml"
ESPHOME_DIR = "esphome"                 # under /config: where the ESPHome add-on reads its configs
SECRETS_FILE = "secrets.yaml"
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")  # ESPHome node name, ≤ 31 chars
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
NO_MAC = "00:00:00:00:00:00"            # "find the scooter later": the ESP scans, the user fills it in

# Board blocks replacing the example's "pick ONE of the two blocks" section.
CHIPS: dict[str, dict] = {
    "esp32": {
        "label": "ESP32 classic (no PSRAM)",
        "block": [
            "esp32:",
            "  board: esp32dev",
            "  framework:",
            "    type: esp-idf",
        ],
    },
    "s3_n16r8": {
        "label": "ESP32-S3 N16R8 (16 MB flash, 8 MB octal PSRAM)",
        "block": [
            "esp32:",
            "  board: esp32-s3-devkitc-1",
            "  flash_size: 16MB",
            "  framework:",
            "    type: esp-idf",
            "    advanced:",
            "      flash_chip: generic",
            "psram:",
            "  mode: octal",
            "  speed: 80MHz",
        ],
    },
    "s3_n8r8": {
        "label": "ESP32-S3 N8R8 (8 MB flash, 8 MB octal PSRAM)",
        "block": [
            "esp32:",
            "  board: esp32-s3-devkitc-1",
            "  flash_size: 8MB",
            "  framework:",
            "    type: esp-idf",
            "    advanced:",
            "      flash_chip: generic",
            "psram:",
            "  mode: octal",
            "  speed: 80MHz",
        ],
    },
    "s3_fh4r2": {
        "label": "ESP32-S3FH4R2 / S3 Super Mini (4 MB flash, 2 MB quad PSRAM)",
        "block": [
            "esp32:",
            "  board: esp32-s3-devkitc-1",
            "  flash_size: 4MB",
            "  framework:",
            "    type: esp-idf",
            "    advanced:",
            "      flash_chip: generic",
            "psram:",
            "  mode: quad",
            "  speed: 80MHz",
        ],
    },
    "s3_nopsram": {
        "label": "ESP32-S3 without PSRAM",
        "block": [
            "esp32:",
            "  board: esp32-s3-devkitc-1",
            "  framework:",
            "    type: esp-idf",
            "    advanced:",
            "      flash_chip: generic",
        ],
    },
}
PSRAM_CHIPS = {"s3_n16r8", "s3_n8r8", "s3_fh4r2"}


class WizardError(Exception):
    """A step cannot continue; the message is shown to the user."""


def example_url(ref: str) -> str:
    return f"https://raw.githubusercontent.com/{EXAMPLE_REPO}/{ref}/{EXAMPLE_FILE}"


def secret_names(node: str) -> dict[str, str]:
    """Per-node secret keys, so several ESPs never share or overwrite each other's values."""
    stem = re.sub(r"[^a-z0-9]+", "_", node.lower()).strip("_")
    base = stem if stem.startswith("librescoot_") else f"librescoot_{stem}"
    return {
        "api_key": f"{base}_api_key",
        "mac": f"{base}_scooter_mac",
        "ap_pw": f"{base}_fallback_ap_pw",
        "wifi_ssid": f"{base}_wifi_ssid",
        "wifi_pw": f"{base}_wifi_pw",
    }


def new_api_key() -> str:
    return base64.b64encode(pysecrets.token_bytes(32)).decode()


def new_password() -> str:
    return pysecrets.token_urlsafe(12)


def validate_node(node: str, esphome_dir: str, taken: set[str]) -> str | None:
    """Error key, or None when the node name is usable."""
    if not NODE_RE.match(node):
        return "invalid_node"
    if node in taken or os.path.exists(os.path.join(esphome_dir, f"{node}.yaml")):
        return "node_exists"
    return None


def read_secret_keys(path: str) -> set[str]:
    """Top-level keys of secrets.yaml — names only, values are never read into the flow."""
    keys: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if (m := re.match(r"^([A-Za-z0-9_\-]+)\s*:", line)):
                    keys.add(m.group(1))
    except FileNotFoundError:
        pass
    return keys


def wifi_secret_candidates(keys: set[str]) -> list[tuple[str, str]]:
    """Existing (ssid, password) secret pairs worth offering for reuse."""
    pairs = []
    for ssid in sorted(k for k in keys if k.endswith("ssid")):
        stem = ssid[: -len("ssid")]
        for pw in (f"{stem}pw", f"{stem}password", f"{stem}pass"):
            if pw in keys:
                pairs.append((ssid, pw))
                break
    return pairs


def _sub_line(text: str, pattern: str, repl: str, what: str) -> str:
    new, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    if n != 1:
        raise WizardError(f"the example config changed ({what} not found) — update the integration")
    return new


def usb_preset(raw: dict) -> dict:
    """Board preset from what the USB detection page read out of the ROM bootloader.

    raw: chip_name, description, features, mac, flash_size, rev_major, rev_minor, and psram (the
    user's answer — external octal PSRAM, e.g. N16R8, is not visible to the ROM).
    Returns chip / flash_size / min_rev / label, or ask_psram, or error.
    """
    name = raw.get("chip_name") or ""
    features = " ".join(raw.get("features") or [])
    flash = raw.get("flash_size") if raw.get("flash_size") in ("2MB", "4MB", "8MB", "16MB", "32MB") else None
    out: dict = {"flash_size": None, "min_rev": None, "mac": (raw.get("mac") or "").upper() or None,
                 "description": raw.get("description") or name}
    if name == "ESP32":
        out["chip"] = "esp32"
        out["flash_size"] = flash if flash and flash != "4MB" else None  # esp32dev assumes 4 MB
        rev = (raw.get("rev_major"), raw.get("rev_minor"))
        if rev in ((3, 0), (3, 1)):
            out["min_rev"] = f"{rev[0]}.{rev[1]}"
    elif name == "ESP32-S3":
        if "Embedded PSRAM 2MB" in features:
            out["chip"] = "s3_fh4r2"
            out["flash_size"] = flash if flash and flash != "4MB" else None
        else:
            psram = True if "Embedded PSRAM 8MB" in features else raw.get("psram")
            if psram is None:
                return {**out, "ask_psram": True}
            if psram:
                out["chip"] = "s3_n16r8" if flash == "16MB" else "s3_n8r8"
                out["flash_size"] = flash if flash not in (None, "16MB", "8MB") else None
            else:
                out["chip"] = "s3_nopsram"
                out["flash_size"] = flash
    else:
        return {**out, "error": f"{name or 'This chip'} is not supported (ESP32 classic and ESP32-S3 only)"}
    label = CHIPS[out["chip"]]["label"]
    extra = [x for x in (out["flash_size"] and f"flash {out['flash_size']}",
                         out["min_rev"] and f"revision {out['min_rev']}") if x]
    out["label"] = label + (f" — {', '.join(extra)}" if extra else "")
    return out


def board_block(chip: str, flash_size: str | None = None, min_rev: str | None = None) -> list[str]:
    """The chip's esp32:/psram: lines; a USB-detected flash size replaces the preset's, a detected
    classic chip revision adds minimum_chip_revision."""
    lines = [l for l in CHIPS[chip]["block"] if not (flash_size and l.startswith("  flash_size:"))]
    if flash_size:
        lines.insert(lines.index("  framework:"), f"  flash_size: {flash_size}")
    if min_rev and chip == "esp32":
        lines += ["    advanced:", f'      minimum_chip_revision: "{min_rev}"']
    return lines


def build_yaml(example: str, *, ref: str, node: str, friendly: str, chip: str,
               wifi: tuple[str, str], github_token: bool, byte_source: str | None,
               flash_size: str | None = None, min_rev: str | None = None) -> str:
    """The example with this ESP's names, board block, secret names and options filled in."""
    if chip not in CHIPS:
        raise WizardError(f"unknown chip {chip}")
    names = secret_names(node)
    out = example
    out = _sub_line(out, r'^(\s*device_name:\s*)"[^"]*"', rf'\g<1>"{node}"', "device_name")
    out = _sub_line(out, r'^(\s*device_friendly_name:\s*)"[^"]*"', rf'\g<1>"{friendly}"', "device_friendly_name")
    out = _sub_line(out, r"^(\s*ble_mac:\s*)!secret\s+\S+", rf"\g<1>!secret {names['mac']}", "ble_mac")

    # Board section: from the "pick ONE" header to the next dashed rule.
    start = re.search(r"^# -+ Board:.*$", out, flags=re.M)
    if start is None:
        raise WizardError("the example config changed (board section not found) — update the integration")
    end = re.search(r"^# -{20,}\s*$", out[start.end():], flags=re.M)
    if end is None:
        raise WizardError("the example config changed (end of board section not found) — update the integration")
    block = "\n".join([f"# ----- Board: {CHIPS[chip]['label']} (set by the Home Assistant setup wizard)",
                       *board_block(chip, flash_size, min_rev)])
    out = out[: start.start()] + block + "\n" + out[start.end() + end.start():]

    if ref != "main":
        out = _sub_line(out, rf"^(\s*)url: https://github.com/{re.escape(EXAMPLE_REPO)}\s*$",
                        rf"\g<0>\n\g<1>ref: {ref}", "external_components url")
    out = _sub_line(out, r"^(\s*key:\s*)!secret\s+librescoot_api_key", rf"\g<1>!secret {names['api_key']}", "api key")
    out = _sub_line(out, r"^(\s*ssid:\s*)!secret\s+librescoot_wifi_ssid", rf"\g<1>!secret {wifi[0]}", "wifi ssid")
    out = _sub_line(out, r"^(\s*password:\s*)!secret\s+librescoot_wifi_pw", rf"\g<1>!secret {wifi[1]}", "wifi password")
    out = _sub_line(out, r"^(\s*password:\s*)!secret\s+librescoot_fallback_ap_pw",
                    rf"\g<1>!secret {names['ap_pw']}", "fallback AP password")
    if github_token:
        out = _sub_line(out, r"^(\s*)# (github_token:\s*!secret\s+librescoot_github_token)", r"\g<1>\g<2>",
                        "github_token")
    if byte_source in ("relay", "github"):
        out = _sub_line(out, r"^(\s*)mac_address:\s*\$\{ble_mac\}\s*$",
                        rf"\g<0>\n\g<1>ota_source_default: {byte_source}", "mac_address")
    header = (f"# Created by the Librescoot-BLE-Client setup wizard in Home Assistant on "
              f"{time.strftime('%Y-%m-%d')} from {EXAMPLE_REPO}@{ref}/{EXAMPLE_FILE}.\n")
    return header + out


def _yaml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def secrets_lines(values: dict[str, str], node: str) -> str:
    """The block appended to secrets.yaml (also shown for copying when the wizard cannot write)."""
    head = (f"# Librescoot-BLE-Client ESP '{node}' — added by the Home Assistant setup wizard "
            f"{time.strftime('%Y-%m-%d %H:%M')}")
    return "\n".join([head, *(f"{k}: {_yaml_str(v)}" for k, v in values.items())]) + "\n"


def append_secrets(path: str, values: dict[str, str], node: str) -> list[str]:
    """Append keys that are not there yet; never touch existing ones. Atomic, keeps a .bak.
    Returns the keys actually added."""
    existing = read_secret_keys(path)
    add = {k: v for k, v in values.items() if k not in existing}
    if not add:
        return []
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
        with open(path + ".bak", "w", encoding="utf-8") as fh:
            fh.write(old)
    text = old + ("" if old.endswith("\n") or not old else "\n") + "\n" + secrets_lines(add, node)
    _atomic_write(path, text)
    return list(add)


def write_new_file(path: str, text: str) -> None:
    """Write a file that must not exist yet (temp file + rename)."""
    if os.path.exists(path):
        raise WizardError(f"{os.path.basename(path)} already exists")
    _atomic_write(path, text)


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".librescoot-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
