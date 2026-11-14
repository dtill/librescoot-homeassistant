"""Import mode of the setup wizard: adopt an ESPHome YAML that was written by hand.

Read as text, never through a YAML loader and never resolving `!secret` — secrets.yaml is not
opened. Finds the node name and the board, and offers fixes; each fix is a pure text edit on the
original file, so comments and layout survive and nothing is applied unless the user ticks it.
"""
from __future__ import annotations

import difflib
import os
import re

from . import wizard as wz

COMPONENT_RE = re.compile(r"^librescoot_ble_client:\s*(#.*)?$", re.M)
OBSOLETE_OPTIONS = ("stage_only", "hdelta")  # removed from the component; ESPHome rejects them


def list_configs(esphome_dir: str) -> list[str]:
    """File names in the ESPHome folder that configure the librescoot_ble_client component."""
    out = []
    try:
        names = sorted(os.listdir(esphome_dir))
    except FileNotFoundError:
        return []
    for name in names:
        if not name.endswith((".yaml", ".yml")) or name.startswith(".") or name.startswith("secrets."):
            continue
        try:
            with open(os.path.join(esphome_dir, name), encoding="utf-8") as fh:
                if COMPONENT_RE.search(fh.read()):
                    out.append(name)
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _sections(text: str) -> dict[str, tuple[int, int]]:
    """Top-level keys → (first line, end line exclusive), by line index. Comment lines between two
    sections belong to neither; trailing comments of a section are not trimmed (harmless here)."""
    lines = text.split("\n")
    starts = [(i, m.group(1)) for i, l in enumerate(lines) if (m := re.match(r"^([A-Za-z0-9_]+):", l))]
    out = {}
    for n, (i, key) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        while end > i + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        out.setdefault(key, (i, end))
    return out


def _value(raw: str) -> str:
    raw = re.sub(r"\s+#.*$", "", raw.strip())
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return raw


def _subst(value: str, subs: dict[str, str]) -> str:
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: subs.get(m.group(1) or m.group(2), m.group(0)), value)


def _key_in(lines: list[str], span: tuple[int, int] | None, key: str, indent: int | None = 2) -> tuple[int, str] | None:
    if span is None:
        return None
    pat = re.compile(rf"^(\s{{{indent}}}){key}:\s*(.*)$" if indent is not None else rf"^(\s+){key}:\s*(.*)$")
    for i in range(span[0] + 1, span[1]):
        if (m := pat.match(lines[i])):
            return i, m.group(2)
    return None


def inspect(text: str) -> dict:
    """What the file says (no secrets resolved): node, friendly name, board, flash, PSRAM, …"""
    lines = text.split("\n")
    sec = _sections(text)
    subs: dict[str, str] = {}
    if (span := sec.get("substitutions")):
        for i in range(span[0] + 1, span[1]):
            if (m := re.match(r"^\s{2}(\w+):\s*(.*)$", lines[i])):
                subs[m.group(1)] = _value(m.group(2))
    info: dict = {"subs": subs}
    name = _key_in(lines, sec.get("esphome"), "name")
    info["node"] = _subst(_value(name[1]), subs) if name else None
    friendly = _key_in(lines, sec.get("esphome"), "friendly_name")
    info["friendly"] = _subst(_value(friendly[1]), subs) if friendly else None
    board = _key_in(lines, sec.get("esp32"), "board")
    info["board"] = _value(board[1]) if board else None
    info["s3"] = bool(info["board"] and "s3" in info["board"].lower())
    flash = _key_in(lines, sec.get("esp32"), "flash_size")
    info["flash_size"] = _value(flash[1]) if flash else None
    info["psram"] = "psram" in sec
    mode = _key_in(lines, sec.get("psram"), "mode")
    info["psram_mode"] = _value(mode[1]) if mode else None
    info["web_server"] = "web_server" in sec
    key = _key_in(lines, sec.get("api"), "key", indent=None)
    info["api_key_inline"] = bool(key and not key[1].strip().startswith("!secret") and _value(key[1]))
    info["ext_ref"] = None
    info["ext_ours"] = False
    if (span := sec.get("external_components")):
        for i in range(span[0] + 1, span[1]):
            if re.search(rf"url:\s*\S*{re.escape(wz.EXAMPLE_REPO)}", lines[i]):
                info["ext_ours"] = True
            if (m := re.match(r"^\s+ref:\s*(\S+)", lines[i])):
                info["ext_ref"] = _value(m.group(1))
    comp = sec.get("librescoot_ble_client")
    info["obsolete"] = [k for k in OBSOLETE_OPTIONS if _key_in(lines, comp, k)]
    return info


# ---- fixes ------------------------------------------------------------------------------

def _insert_in_esp32(lines: list[str], sec: dict, new: str) -> list[str]:
    """A 2-space key right after `board:` in the esp32 block."""
    board = _key_in(lines, sec.get("esp32"), "board")
    at = board[0] + 1 if board else sec["esp32"][0] + 1
    return lines[:at] + [new] + lines[at:]


def _fix_psram(text: str, mode: str, flash: str | None) -> str:
    lines = text.split("\n")
    sec = _sections(text)
    if flash and not _key_in(lines, sec.get("esp32"), "flash_size"):
        lines = _insert_in_esp32(lines, sec, f"  flash_size: {flash}")
        sec = _sections("\n".join(lines))
    end = sec["esp32"][1]
    return "\n".join(lines[:end] + ["psram:", f"  mode: {mode}", "  speed: 80MHz"] + lines[end:])


def _fix_flash_4mb(text: str) -> str:
    lines = text.split("\n")
    return "\n".join(_insert_in_esp32(lines, _sections(text), "  flash_size: 4MB"))


def _fix_pin(text: str, ref: str) -> str:
    return re.sub(rf"^(\s*)(url:\s*\S*{re.escape(wz.EXAMPLE_REPO)}\S*)\s*$",
                  rf"\g<1>\g<2>\n\g<1>ref: {ref}", text, count=1, flags=re.M)


def _fix_web_server(text: str) -> str:
    lines = text.split("\n")
    a, b = _sections(text)["web_server"]
    body = [("# " + l) if l.strip() else l for l in lines[a:b]]
    note = "# web_server commented out by the Home Assistant setup wizard: it costs internal RAM a board without PSRAM needs for BLE + TLS."
    return "\n".join(lines[:a] + [note] + body + lines[b:])


def _fix_api_key(text: str, secret: str) -> str:
    lines = text.split("\n")
    i, _ = _key_in(lines, _sections(text)["api"], "key", indent=None)
    lines[i] = re.sub(r"^(\s+key:\s*).*$", rf"\g<1>!secret {secret}", lines[i])
    return "\n".join(lines)


def _fix_obsolete(text: str) -> str:
    lines = text.split("\n")
    a, b = _sections(text)["librescoot_ble_client"]
    drop = {i for i in range(a + 1, b) if re.match(rf"^\s{{2}}({'|'.join(OBSOLETE_OPTIONS)}):", lines[i])}
    return "\n".join(l for i, l in enumerate(lines) if i not in drop)


def api_key_value(text: str) -> str | None:
    lines = text.split("\n")
    key = _key_in(lines, _sections(text).get("api"), "key", indent=None)
    return _value(key[1]) if key else None


def offer_fixes(info: dict, release_ref: str | None) -> list[tuple[str, str]]:
    """(fix id, label) for everything worth changing in this file."""
    fixes = []
    if info["s3"] and not info["psram"]:
        fixes.append(("psram_octal", "Add `psram: mode: octal` — the board has 8 MB PSRAM (R8 on the module, e.g. N16R8 / N8R8)"))
        fixes.append(("psram_quad", "Add `psram: mode: quad` and `flash_size: 4MB` — ESP32-S3FH4R2 / S3 Super Mini (2 MB PSRAM)"))
    elif info["s3"] and info["psram_mode"] == "quad" and not info["flash_size"]:
        fixes.append(("flash_4mb", "Add `flash_size: 4MB` (quad-PSRAM S3 modules like the S3FH4R2 have 4 MB flash)"))
    if info["ext_ours"] and not info["ext_ref"] and release_ref:
        fixes.append(("pin_ref", f"Pin `external_components` to the release `{release_ref}` instead of the newest commit"))
    if info["web_server"] and not info["psram"]:
        fixes.append(("web_server", "Comment out `web_server:` — without PSRAM its RAM is needed for BLE and TLS"))
    if info["api_key_inline"]:
        fixes.append(("api_key", "Move the API encryption key from the file into secrets.yaml"))
    if info["obsolete"]:
        fixes.append(("obsolete", "Remove options the component no longer has: " + ", ".join(f"`{k}`" for k in info["obsolete"])))
    return fixes


def apply_fixes(text: str, chosen: list[str], *, node: str, release_ref: str | None) -> str:
    out = text
    for fix in chosen:
        if fix == "psram_octal":
            out = _fix_psram(out, "octal", None)
        elif fix == "psram_quad":
            out = _fix_psram(out, "quad", "4MB")
        elif fix == "flash_4mb":
            out = _fix_flash_4mb(out)
        elif fix == "pin_ref" and release_ref:
            out = _fix_pin(out, release_ref)
        elif fix == "web_server":
            out = _fix_web_server(out)
        elif fix == "api_key":
            out = _fix_api_key(out, wz.secret_names(node)["api_key"])
        elif fix == "obsolete":
            out = _fix_obsolete(out)
    return out


def preview(text: str, fix: str, *, node: str, release_ref: str | None, name: str) -> str:
    """Unified diff of one fix alone, with an inline API key masked."""
    new = apply_fixes(text, [fix], node=node, release_ref=release_ref)
    key = api_key_value(text)
    diff = "\n".join(difflib.unified_diff(text.split("\n"), new.split("\n"), name, name, n=1, lineterm=""))
    if key and not key.startswith("!secret"):
        diff = diff.replace(key, "•••")
    return diff


def write_adopted(path: str, text: str) -> None:
    """Replace the file atomically, keeping the previous version as <file>.bak."""
    with open(path, encoding="utf-8") as fh:
        old = fh.read()
    with open(path + ".bak", "w", encoding="utf-8") as fh:
        fh.write(old)
    wz._atomic_write(path, text)
