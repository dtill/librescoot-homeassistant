"""Config-flow steps of the "Set up a new ESP" wizard.

chip → scooter → name → WiFi → options → write. Every form has a "back" checkbox; the answers live
in self._wiz and are saved as a draft after each step, so a closed dialog or an HA restart can
resume. Files are written only in the last step; that step is idempotent (secrets that exist are
skipped, an existing YAML of a resumed run is kept, an existing entry for the node is reused).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from . import adopt, wizard as wz, wizard_web as wzweb
from .const import CONF_MAC, CONF_NODE, DOMAIN
from .helpers import slug

_LOGGER = logging.getLogger(__name__)

STEPS = ["wiz_chip", "wiz_scooter", "wiz_name", "wiz_wifi", "wiz_options", "wiz_write"]
DRAFT_KEY = f"{DOMAIN}.wizard_draft"
DRAFT_MAX_AGE_S = 7 * 24 * 3600
BACK = "back"
SCOOTER_MANUAL = "__manual__"
SCOOTER_LATER = "__later__"
WIFI_NEW = "__new__"
CHIP_USB = "__usb__"


class WizardFlowMixin:
    """Mixed into the config flow; uses self.hass and the flow's form helpers."""

    _wiz: dict[str, Any]

    # ---- draft ------------------------------------------------------------------------
    def _draft_store(self) -> Store:
        return Store(self.hass, 1, DRAFT_KEY)

    async def _save_draft(self, step: str) -> None:
        data = {k: v for k, v in self._wiz.items() if k not in ("wifi_pw", "api_key")}  # never stored
        await self._draft_store().async_save({**data, "step": step, "saved": time.time()})

    async def _drop_draft(self) -> None:
        await self._draft_store().async_remove()

    def _esphome_dir(self) -> str:
        return self.hass.config.path(wz.ESPHOME_DIR)

    # ---- entry + resume ---------------------------------------------------------------
    async def async_step_new_esp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        draft = await self._draft_store().async_load()
        if draft and time.time() - draft.get("saved", 0) < DRAFT_MAX_AGE_S:
            self._draft = draft
            return self.async_show_menu(
                step_id="wiz_resume",
                menu_options=["wiz_continue", "wiz_restart"],
                description_placeholders={
                    "node": draft.get("node") or "—",
                    "step": str(STEPS.index(draft["step"]) + 1 if draft.get("step") in STEPS else 1),
                    "count": str(len(STEPS)),
                    "age": f"{int((time.time() - draft.get('saved', 0)) // 60)} min",
                },
            )
        self._wiz = {}
        return await self.async_step_wiz_chip()

    async def async_step_wiz_continue(self, user_input=None) -> ConfigFlowResult:
        self._wiz = {k: v for k, v in self._draft.items() if k not in ("step", "saved")}
        step = self._draft.get("step") if self._draft.get("step") in STEPS else STEPS[0]
        return await getattr(self, f"async_step_{step}")()

    async def async_step_wiz_restart(self, user_input=None) -> ConfigFlowResult:
        await self._drop_draft()
        self._wiz = {}
        return await self.async_step_wiz_chip()

    async def _back(self, step: str) -> ConfigFlowResult:
        prev = STEPS[max(STEPS.index(step) - 1, 0)]
        return await getattr(self, f"async_step_{prev}")()

    @staticmethod
    def _with_back(fields: dict) -> vol.Schema:
        return vol.Schema({**fields, vol.Optional(BACK, default=False): bool})

    # ---- 1 chip -----------------------------------------------------------------------
    async def async_step_wiz_chip(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        usb = wzweb.usb_detection(self.hass)
        if user_input is not None:
            pick = user_input["chip"]
            if pick == CHIP_USB and usb:
                self._wiz.update(chip=usb["chip"], flash_size=usb["flash_size"], min_rev=usb["min_rev"],
                                 esp_mac=usb["mac"], usb_label=usb["label"])
            elif pick != CHIP_USB:
                self._wiz["chip"] = pick
                for k in ("flash_size", "min_rev", "esp_mac", "usb_label"):
                    self._wiz.pop(k, None)
            else:
                # "fetch the USB result" without one (yet / expired): show the step again
                return await self.async_step_wiz_chip()
            await self._save_draft("wiz_scooter")
            return await self.async_step_wiz_scooter()
        options = [{"value": k, "label": v["label"]} for k, v in wz.CHIPS.items()]
        default = self._wiz.get("chip", "esp32")
        if usb:
            options.insert(0, {"value": CHIP_USB, "label": f"Use the board detected via USB: {usb['label']}"})
        else:
            options.append({"value": CHIP_USB, "label": "I ran the USB detection page — fetch its result"})
            default = CHIP_USB if not self._wiz.get("chip") or self._wiz.get("usb_label") else default
        schema = vol.Schema({
            vol.Required("chip", default=default): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST)
            )
        })
        return self.async_show_form(step_id="wiz_chip", data_schema=schema,
                                    description_placeholders={"usb_link": wzweb.new_tab_link(
                                        f"/{wzweb.USB_PANEL}", "the USB detection page")})

    # ---- 2 scooter --------------------------------------------------------------------
    def _nearby_scooters(self) -> list[dict[str, str]]:
        """Scooters Home Assistant's own Bluetooth (adapter or proxies) currently hears."""
        try:
            from homeassistant.components.bluetooth import async_discovered_service_info
        except ImportError:
            return []
        found: dict[str, dict[str, str]] = {}
        try:
            infos = list(async_discovered_service_info(self.hass, connectable=False))
        except Exception:  # noqa: BLE001 - bluetooth not set up on this host
            return []
        for info in infos:
            name = info.name or ""
            if "scooter" in name.lower() or "unu" in name.lower():
                found[info.address.upper()] = {"value": info.address.upper(),
                                               "label": f"{name}  {info.address.upper()}  ({info.rssi} dBm)"}
        return sorted(found.values(), key=lambda o: o["label"])

    async def async_step_wiz_scooter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(BACK):
                return await self._back("wiz_scooter")
            pick = user_input["scooter"]
            if pick == SCOOTER_MANUAL:
                mac = (user_input.get("mac") or "").strip().upper()
                if not wz.MAC_RE.match(mac):
                    errors["mac"] = "invalid_mac"
                else:
                    self._wiz["scooter_mac"] = mac
            elif pick == SCOOTER_LATER:
                self._wiz["scooter_mac"] = None
            else:
                self._wiz["scooter_mac"] = pick
            if not errors:
                await self._save_draft("wiz_name")
                return await self.async_step_wiz_name()
        nearby = self._nearby_scooters()
        options = nearby + [
            {"value": SCOOTER_MANUAL, "label": "Enter the scooter's Bluetooth address"},
            {"value": SCOOTER_LATER, "label": "Later — the ESP finds it after the first install"},
        ]
        default = self._wiz.get("scooter_mac") or (nearby[0]["value"] if nearby else SCOOTER_LATER)
        if default not in {o["value"] for o in options}:
            default = SCOOTER_MANUAL
        schema = self._with_back({
            vol.Required("scooter", default=default): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST)
            ),
            vol.Optional("mac", default=self._wiz.get("scooter_mac") or ""): str,
        })
        return self.async_show_form(
            step_id="wiz_scooter", data_schema=schema, errors=errors,
            description_placeholders={"count": str(len(nearby))},
        )

    # ---- 3 name -----------------------------------------------------------------------
    def _taken_nodes(self) -> set[str]:
        taken = {e.data.get("device_name") for e in self.hass.config_entries.async_entries("esphome")}
        taken |= {e.data.get(CONF_NODE) for e in self.hass.config_entries.async_entries(DOMAIN)}
        return {t for t in taken if t}

    async def async_step_wiz_name(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(BACK):
                return await self._back("wiz_name")
            node = user_input["node"].strip().lower()
            if (err := await self.hass.async_add_executor_job(
                    wz.validate_node, node, self._esphome_dir(), self._taken_nodes())):
                errors["node"] = err
            else:
                self._wiz["node"] = node
                self._wiz["friendly"] = user_input["friendly"].strip() or node
                await self._save_draft("wiz_wifi")
                return await self.async_step_wiz_wifi()
        mac = (self._wiz.get("scooter_mac") or "").replace(":", "").lower()
        node = self._wiz.get("node") or (f"librescoot-{mac[-6:]}" if mac else "librescoot-ble-client")
        schema = self._with_back({
            vol.Required("node", default=node): str,
            vol.Required("friendly", default=self._wiz.get("friendly") or node.replace("-", " ").title()): str,
        })
        return self.async_show_form(step_id="wiz_name", data_schema=schema, errors=errors)

    # ---- 4 WiFi -----------------------------------------------------------------------
    async def async_step_wiz_wifi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        secrets_path = os.path.join(self._esphome_dir(), wz.SECRETS_FILE)
        keys = await self.hass.async_add_executor_job(wz.read_secret_keys, secrets_path)
        pairs = wz.wifi_secret_candidates(keys)
        if user_input is not None:
            if user_input.get(BACK):
                return await self._back("wiz_wifi")
            pick = user_input["wifi"]
            if pick == WIFI_NEW:
                if not user_input.get("ssid") or not user_input.get("password"):
                    errors["base"] = "wifi_missing"
                else:
                    names = wz.secret_names(self._wiz["node"])
                    self._wiz["wifi_keys"] = [names["wifi_ssid"], names["wifi_pw"]]
                    self._wiz["wifi_ssid"] = user_input["ssid"]
                    self._wiz["wifi_pw"] = user_input["password"]  # not saved in the draft
            else:
                self._wiz["wifi_keys"] = pick.split("|")
                self._wiz.pop("wifi_ssid", None)
                self._wiz.pop("wifi_pw", None)
            if not errors:
                await self._save_draft("wiz_options")
                return await self.async_step_wiz_options()
        options = [{"value": f"{s}|{p}", "label": f"Use existing secrets {s} / {p}"} for s, p in pairs]
        options.append({"value": WIFI_NEW, "label": "Enter network name and password"})
        default = "|".join(self._wiz["wifi_keys"]) if self._wiz.get("wifi_keys") and not self._wiz.get(
            "wifi_ssid") else (options[0]["value"])
        schema = self._with_back({
            vol.Required("wifi", default=default): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST)
            ),
            vol.Optional("ssid", default=self._wiz.get("wifi_ssid", "")): str,
            vol.Optional("password", default=""): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        })
        return self.async_show_form(step_id="wiz_wifi", data_schema=schema, errors=errors)

    # ---- 5 options --------------------------------------------------------------------
    async def async_step_wiz_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        secrets_path = os.path.join(self._esphome_dir(), wz.SECRETS_FILE)
        keys = await self.hass.async_add_executor_job(wz.read_secret_keys, secrets_path)
        has_token = "librescoot_github_token" in keys
        psram = self._wiz.get("chip") in wz.PSRAM_CHIPS
        if user_input is not None:
            if user_input.get(BACK):
                return await self._back("wiz_options")
            self._wiz["byte_source"] = user_input.get("byte_source", "relay")
            token = (user_input.get("github_token") or "").strip()
            self._wiz["use_token"] = bool(token) or (has_token and user_input.get("use_token", False))
            if token:
                self._wiz["new_token"] = token
            await self._save_draft("wiz_write")
            return await self.async_step_wiz_write()
        fields: dict = {}
        if psram:
            fields[vol.Required("byte_source", default=self._wiz.get("byte_source", "github"))] = (
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[{"value": "github", "label": "Direct from GitHub (this ESP downloads itself)"},
                             {"value": "relay", "label": "Through Home Assistant (Librescoot – Relay)"}],
                    mode=selector.SelectSelectorMode.LIST)))
        if has_token:
            fields[vol.Required("use_token", default=self._wiz.get("use_token", True))] = bool
        else:
            fields[vol.Optional("github_token", default="")] = selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
        return self.async_show_form(
            step_id="wiz_options", data_schema=self._with_back(fields),
            description_placeholders={"psram_note": "This board has PSRAM; with 2 MB the direct download is untested — choose Home Assistant if unsure." if self._wiz.get("chip") == "s3_fh4r2" else ""},
        )

    # ---- 6 write ----------------------------------------------------------------------
    async def _fetch_example(self) -> tuple[str, str]:
        """(ref, text): the latest release tag of the component repo, else main."""
        session = async_get_clientsession(self.hass)
        ref = "main"
        try:
            async with session.get(f"https://api.github.com/repos/{wz.EXAMPLE_REPO}/releases/latest",
                                   headers={"Accept": "application/vnd.github+json"}, timeout=20) as r:
                if r.status == 200:
                    ref = (await r.json()).get("tag_name") or "main"
        except Exception:  # noqa: BLE001 - fall back to main
            pass
        async with session.get(wz.example_url(ref), timeout=30) as r:
            if r.status != 200:
                raise wz.WizardError(f"GitHub answered HTTP {r.status} for the example config")
            return ref, await r.text()

    def _target(self) -> dict:
        return wzweb.esphome_target(self.hass, self._esphome_dir())

    async def async_step_wiz_write(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        w = self._wiz
        errors: dict[str, str] = {}
        target = await self.hass.async_add_executor_job(self._target)
        placeholders = {
            "node": w.get("node", ""), "file": f"esphome/{w.get('node', '')}.yaml",
            "chip": wz.CHIPS.get(w.get("chip"), {}).get("label", ""),
            "scooter": w.get("scooter_mac") or "later", "error": "",
            "where": self._where_note(target),
        }
        if user_input is not None:
            if user_input.get(BACK):
                return await self._back("wiz_write")
            if w.get("wifi_ssid") and not w.get("wifi_pw"):
                # Resumed after a restart: the WiFi password is never stored in the draft.
                return await self.async_step_wiz_wifi()
            how = user_input.get("how", "write" if target["writable"] else "download")
            try:
                ref, example = await self._fetch_example()
                if how == "write":
                    result = await self.hass.async_add_executor_job(self._write_files, ref, example)
                else:
                    result = self._build_files(ref, example)
            except wz.WizardError as err:
                errors["base"] = "write_failed"
                placeholders["error"] = str(err)
            except Exception as err:  # noqa: BLE001 - network / filesystem
                errors["base"] = "github_unreachable"
                placeholders["error"] = str(err)
            else:
                await self._drop_draft()
                if how == "download":
                    self._download = result
                    return await self.async_step_wiz_download()
                return await self._finish(result)
        fields: dict = {}
        if target["writable"] or target["dir"]:
            fields[vol.Required("how", default="write" if target["writable"] else "download")] = (
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[{"value": "write", "label": "Write it into the ESPHome folder"},
                             {"value": "download", "label": "Show it for download / copying"}],
                    mode=selector.SelectSelectorMode.LIST)))
        return self.async_show_form(
            step_id="wiz_write", data_schema=self._with_back(fields), errors=errors,
            description_placeholders=placeholders,
        )

    @staticmethod
    def _where_note(target: dict) -> str:
        if target["hassio"] and not target["addon"]:
            return ("The ESPHome Device Builder add-on is not installed, so the files are offered for "
                    f"download. [Install the add-on]({wzweb.ADDON_LINK}) to let the wizard write them directly.")
        if target["hassio"] and target["addon_state"] not in (None, "started"):
            return "The ESPHome Device Builder add-on is installed but not running — start it before installing."
        if not target["hassio"] and not target["dir"]:
            return ("This Home Assistant has no ESPHome folder (Container / Core installation), so the files "
                    "are offered for download; put them where your ESPHome Device Builder keeps its configs.")
        return ""

    def _secret_values(self, api_key: str) -> dict[str, str]:
        w = self._wiz
        names = wz.secret_names(w["node"])
        values = {
            names["api_key"]: api_key,
            names["mac"]: w.get("scooter_mac") or wz.NO_MAC,
            names["ap_pw"]: wz.new_password(),
        }
        if w.get("wifi_ssid"):
            values[w["wifi_keys"][0]] = w["wifi_ssid"]
            values[w["wifi_keys"][1]] = w["wifi_pw"]
        if w.get("new_token"):
            values["librescoot_github_token"] = w["new_token"]
        return values

    def _yaml_text(self, ref: str, example: str) -> str:
        w = self._wiz
        return wz.build_yaml(
            example, ref=ref, node=w["node"], friendly=w["friendly"], chip=w["chip"],
            wifi=tuple(w["wifi_keys"]), github_token=w.get("use_token", False),
            byte_source=w.get("byte_source") if w.get("chip") in wz.PSRAM_CHIPS else None,
            flash_size=w.get("flash_size"), min_rev=w.get("min_rev"),
        )

    def _write_files(self, ref: str, example: str) -> dict[str, Any]:
        """Executor: secrets first (append-only), then the YAML (new file). Idempotent on resume."""
        w = self._wiz
        esphome_dir = self._esphome_dir()
        names = wz.secret_names(w["node"])
        yaml_path = os.path.join(esphome_dir, f"{w['node']}.yaml")
        api_key = wz.new_api_key()
        added = wz.append_secrets(os.path.join(esphome_dir, wz.SECRETS_FILE), self._secret_values(api_key), w["node"])
        if not os.path.exists(yaml_path):
            wz.write_new_file(yaml_path, self._yaml_text(ref, example))
        return {"api_key": api_key if names["api_key"] in added else None,
                "api_key_name": names["api_key"], "ref": ref}

    def _build_files(self, ref: str, example: str) -> dict[str, Any]:
        """Nothing is written: the YAML and the secret lines are offered for download instead."""
        w = self._wiz
        api_key = wz.new_api_key()
        secrets_text = wz.secrets_lines(self._secret_values(api_key), w["node"])
        token = wzweb.offer_download(self.hass, {
            f"{w['node']}.yaml": self._yaml_text(ref, example),
            f"{w['node']}-secrets.yaml": secrets_text,
        })
        return {"api_key": api_key, "api_key_name": wz.secret_names(w["node"])["api_key"], "ref": ref,
                "token": token, "secrets": secrets_text}

    async def async_step_wiz_download(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        d = self._download
        node = self._wiz["node"]
        if user_input is not None:
            return await self._finish(d)
        return self.async_show_form(
            step_id="wiz_download", data_schema=vol.Schema({}),
            description_placeholders={
                "node": node,
                "yaml_link": wzweb.new_tab_link(wzweb.download_url(d["token"], f"{node}.yaml"), f"{node}.yaml"),
                "secrets_link": wzweb.new_tab_link(
                    wzweb.download_url(d["token"], f"{node}-secrets.yaml"), f"{node}-secrets.yaml"),
                "secrets": d["secrets"].rstrip(),
            },
        )

    async def _finish(self, result: dict[str, Any]) -> ConfigFlowResult:
        w = self._wiz
        esp_mac = (w.get("esp_mac") or "").replace(":", "").lower() or None
        # Same unique id as an ESPHome mDNS discovery of this ESP (its MAC), so it is not offered twice.
        await self.async_set_unique_id(esp_mac or slug(w["node"]))
        if self._async_current_entries() and any(
                e.data.get(CONF_NODE) == w["node"] for e in self._async_current_entries()):
            return self.async_abort(reason="wizard_done_existing")
        key = result["api_key"] or f"(already in secrets.yaml as {result['api_key_name']})"
        return self.async_create_entry(
            title=w["friendly"],
            data={CONF_NODE: w["node"], CONF_MAC: esp_mac},
            description="wizard_done",
            description_placeholders={"node": w["node"], "key": key,
                                      "mac_note": "" if w.get("scooter_mac") else
                                      " The scooter address is still empty: after the first install open this "
                                      "entry's options → Scan for a new scooter, then put the address into "
                                      f"secrets.yaml ({wz.secret_names(w['node'])['mac']}) and install again."},
        )

    # ---- import mode: adopt an existing YAML ----------------------------------------------
    async def _release_ref(self) -> str | None:
        """Latest release tag of the component repo, None while there is none (then no pinning)."""
        try:
            async with async_get_clientsession(self.hass).get(
                    f"https://api.github.com/repos/{wz.EXAMPLE_REPO}/releases/latest",
                    headers={"Accept": "application/vnd.github+json"}, timeout=20) as r:
                return (await r.json()).get("tag_name") if r.status == 200 else None
        except Exception:  # noqa: BLE001 - offline: the pin fix is simply not offered
            return None

    async def async_step_adopt(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        esphome_dir = self._esphome_dir()
        files = await self.hass.async_add_executor_job(adopt.list_configs, esphome_dir)
        if not files:
            return self.async_abort(reason="adopt_none")
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input["file"]
            text = await self.hass.async_add_executor_job(self._read_config, name)
            info = adopt.inspect(text)
            node = info["node"]
            if not node or "$" in node:
                errors["file"] = "adopt_no_name"
            elif node in self._linked_nodes():
                errors["file"] = "adopt_linked"
            else:
                ref = await self._release_ref()
                self._adopt = {"file": name, "text": text, "info": info, "ref": ref,
                               "fixes": adopt.offer_fixes(info, ref)}
                return await self.async_step_adopt_fix()
        linked = self._linked_nodes()
        options = []
        for name in files:
            node = adopt.inspect(await self.hass.async_add_executor_job(self._read_config, name))["node"]
            note = " — already added" if node in linked else ""
            options.append({"value": name, "label": f"{name}  ({node or 'no name'}){note}"})
        schema = vol.Schema({
            vol.Required("file", default=getattr(self, "_adopt", {}).get("file", files[0])):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=options, mode=selector.SelectSelectorMode.LIST)),
        })
        return self.async_show_form(step_id="adopt", data_schema=schema, errors=errors)

    def _read_config(self, name: str) -> str:
        with open(os.path.join(self._esphome_dir(), os.path.basename(name)), encoding="utf-8") as fh:
            return fh.read()

    def _linked_nodes(self) -> set[str]:
        return {e.data.get(CONF_NODE) for e in self.hass.config_entries.async_entries(DOMAIN) if e.data.get(CONF_NODE)}

    async def async_step_adopt_fix(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        a = self._adopt
        info, node = a["info"], a["info"]["node"]
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(BACK):
                return await self.async_step_adopt()
            chosen = [f for f, _ in a["fixes"] if f in user_input.get("fixes", [])]
            keys = await self.hass.async_add_executor_job(
                wz.read_secret_keys, os.path.join(self._esphome_dir(), wz.SECRETS_FILE))
            if {"psram_octal", "psram_quad"} <= set(chosen):
                errors["fixes"] = "adopt_psram_both"
            elif "api_key" in chosen and wz.secret_names(node)["api_key"] in keys:
                # The YAML would point at a secret with another value — ESP and HA would disagree.
                errors["fixes"] = "adopt_key_exists"
            else:
                await self.async_set_unique_id(slug(node))
                self._abort_if_unique_id_configured()
                if chosen:
                    await self.hass.async_add_executor_job(self._write_adopted, chosen)
                return self.async_create_entry(
                    title=info["friendly"] or node, data={CONF_NODE: node, CONF_MAC: None},
                    description="adopt_done",
                    description_placeholders={"node": node, "file": a["file"],
                                              "changed": f"{len(chosen)} change(s) written, the previous file is "
                                                         f"{a['file']}.bak." if chosen else "The file was not changed."},
                )
        fields: dict = {}
        previews = ""
        if a["fixes"]:
            fields[vol.Optional("fixes", default=[])] = selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": f, "label": label} for f, label in a["fixes"]], multiple=True,
                mode=selector.SelectSelectorMode.LIST))
            previews = "\n\n".join(
                f"**{label}**\n```diff\n{adopt.preview(a['text'], f, node=node, release_ref=a['ref'], name=a['file'])}\n```"
                for f, label in a["fixes"])
        board = info["board"] or "unknown board"
        if info["psram"]:
            board += f", PSRAM {info['psram_mode'] or 'yes'}"
        if info["flash_size"]:
            board += f", {info['flash_size']} flash"
        return self.async_show_form(
            step_id="adopt_fix", data_schema=self._with_back(fields), errors=errors,
            description_placeholders={
                "file": a["file"], "node": node, "board": board,
                "summary": (f"{len(a['fixes'])} suggested change(s) — tick the ones to apply, nothing else is touched:"
                            if a["fixes"] else "Nothing to change — this only links the file's ESP to the integration."),
                "previews": previews,
            },
        )

    def _write_adopted(self, chosen: list[str]) -> None:
        a = self._adopt
        node = a["info"]["node"]
        new = adopt.apply_fixes(a["text"], chosen, node=node, release_ref=a["ref"])
        if "api_key" in chosen:
            key = adopt.api_key_value(a["text"])
            wz.append_secrets(os.path.join(self._esphome_dir(), wz.SECRETS_FILE),
                              {wz.secret_names(node)["api_key"]: key}, node)
        adopt.write_adopted(os.path.join(self._esphome_dir(), a["file"]), new)
