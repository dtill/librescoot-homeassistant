"""Web side of the ESP setup wizard.

- Download of generated files when the wizard cannot write into an ESPHome folder (HA Container /
  Core, or HA OS without the ESPHome add-on): one-time token, 30 min, held in memory only.
- Where the ESPHome Device Builder lives: the add-on on HA OS / Supervised, else unknown.
- The USB chip detection page (admin-only panel at /librescoot-usb, not in the sidebar): reads
  the chip through Web Serial in the browser and hands the result to the wizard over a websocket
  command. The HA server never sees the USB device.
"""
from __future__ import annotations

import logging
import os
import secrets as pysecrets
import time

from aiohttp import web
import voluptuous as vol

from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from . import wizard as wz
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DOWNLOAD_URL = f"/api/{DOMAIN}/wizard"
DOWNLOAD_TTL_S = 30 * 60
ADDON_LINK = "https://my.home-assistant.io/redirect/supervisor_addon/?addon=5c53de3b_esphome"
_DATA = "_wizard_downloads"
_USB = "_usb_detected"
USB_PANEL = "librescoot-usb"
USB_TTL_S = 30 * 60
STATIC_URL = f"/{DOMAIN}_static"


def esphome_target(hass: HomeAssistant, esphome_dir: str) -> dict:
    """Can the wizard write the YAML where the Device Builder reads it?

    hassio: whether this is HA OS / Supervised; addon / addon_state: the ESPHome add-on, if
    installed; dir: the esphome folder exists; writable: writing is the right default.
    """
    info = {"hassio": False, "addon": None, "addon_state": None, "dir": os.path.isdir(esphome_dir)}
    try:
        from homeassistant.helpers.hassio import is_hassio
        info["hassio"] = is_hassio(hass)
    except ImportError:
        pass
    if info["hassio"]:
        try:
            from homeassistant.components.hassio import get_addons_list
            for addon in get_addons_list(hass):
                if "esphome" in (addon.get("slug") or ""):
                    info["addon"] = addon.get("slug")
                    info["addon_state"] = addon.get("state")
                    break
        except Exception:  # noqa: BLE001 - supervisor not ready / API changed: treat as unknown
            _LOGGER.debug("wizard: add-on list not available", exc_info=True)
    # HA OS: the add-on reads /config/esphome. Container/Core: only if the folder already exists
    # (the user mounted it for a Device Builder running elsewhere).
    info["writable"] = bool(info["addon"]) if info["hassio"] else info["dir"]
    return info


def offer_download(hass: HomeAssistant, files: dict[str, str]) -> str:
    """Hold the files for download; returns the token for the URLs below."""
    store: dict = hass.data.setdefault(DOMAIN, {}).setdefault(_DATA, {})
    now = time.time()
    for tok in [t for t, v in store.items() if v["until"] < now]:
        store.pop(tok)
    token = pysecrets.token_urlsafe(24)
    store[token] = {"until": now + DOWNLOAD_TTL_S, "files": files}
    return token


def new_tab_link(url: str, text: str) -> str:
    """An HTML link that opens in a new tab. HA's markdown only does that for other hosts, and a tag
    with attributes inside a translation string fails ICU parsing (INVALID_TAG) — so it goes in as a
    description placeholder, whose value is not parsed."""
    return f'<a href="{url}" target="_blank">{text}</a>'


def download_url(token: str, name: str) -> str:
    return f"{DOWNLOAD_URL}/{token}/{name}"


class WizardDownloadView(HomeAssistantView):
    """GET /api/librescoot_ble_client/wizard/<token>/<file>. No HA auth: a plain browser link carries
    no bearer token, so the random one-time token is the authorisation (it expires after 30 min)."""

    url = DOWNLOAD_URL + "/{token}/{name}"
    name = f"api:{DOMAIN}:wizard_download"
    requires_auth = False

    async def get(self, request: web.Request, token: str, name: str) -> web.Response:
        hass = request.app["hass"]
        entry = hass.data.get(DOMAIN, {}).get(_DATA, {}).get(token)
        if entry is None or entry["until"] < time.time() or name not in entry["files"]:
            return web.Response(status=404, text="expired — run the wizard's last step again")
        return web.Response(
            text=entry["files"][name], content_type="text/yaml", charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
        )


def usb_detection(hass: HomeAssistant) -> dict | None:
    """The last board preset the USB page produced, if it is recent and usable."""
    det = hass.data.get(DOMAIN, {}).get(_USB)
    if not det or time.time() - det["at"] > USB_TTL_S or "chip" not in det["preset"]:
        return None
    return det["preset"]


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/usb_detected",
    vol.Required("chip_name"): str,
    vol.Optional("description", default=""): str,
    vol.Optional("features", default=[]): [str],
    vol.Optional("mac", default=""): str,
    vol.Optional("flash_size"): vol.Any(str, None),
    vol.Optional("rev_major"): vol.Any(int, None),
    vol.Optional("rev_minor"): vol.Any(int, None),
    vol.Optional("psram"): vol.Any(bool, None),
})
@callback
def ws_usb_detected(hass: HomeAssistant, connection, msg: dict) -> None:
    raw = {k: v for k, v in msg.items() if k not in ("id", "type")}
    preset = wz.usb_preset(raw)
    hass.data.setdefault(DOMAIN, {})[_USB] = {"at": time.time(), "preset": preset, "raw": raw}
    _LOGGER.info("USB detection: %s → %s", raw.get("description") or raw.get("chip_name"),
                 preset.get("label") or preset.get("error") or "PSRAM question")
    connection.send_result(msg["id"], preset)


async def async_setup_wizard_web(hass: HomeAssistant) -> None:
    hass.http.register_view(WizardDownloadView())
    websocket_api.async_register_command(hass, ws_usb_detected)
    await hass.http.async_register_static_paths([
        StaticPathConfig(STATIC_URL, os.path.join(os.path.dirname(__file__), "frontend"), cache_headers=False),
    ])
    await panel_custom.async_register_panel(
        hass, frontend_url_path=USB_PANEL, webcomponent_name="librescoot-usb-panel",
        module_url=f"{STATIC_URL}/usb-panel.js?v=1", require_admin=True,
    )  # no sidebar_title: reachable from the wizard's link only
