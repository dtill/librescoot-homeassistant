"""Config + options flow for Librescoot-BLE-Client.

Discovery: an ESP running the librescoot_ble_client component advertises the mDNS project
name (see manifest zeroconf). HA then offers one config entry per device — near-zero input;
everything is derived from the device and the rest lives in the options flow.
"""
from __future__ import annotations

import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CHANNELS,
    CONF_CHANNEL,
    CONF_DISCOVERED_MAC,
    CONF_GITHUB_REPO,
    CONF_GITHUB_TOKEN,
    CONF_HA_HOST,
    CONF_HA_PORT,
    CONF_MAC,
    CONF_NODE,
    CONF_SECRET,
    DEFAULT_CHANNEL,
    DEFAULT_HA_PORT,
    DEFAULT_REPO,
    DOMAIN,
)
from .helpers import default_host, parse_pin, resolve_entities, scan_for_scooters, slug

_RESCAN = "__rescan__"
_UNBIND = "__unbind__"


class LibrescootConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow (zeroconf discovery + manual)."""

    VERSION = 1

    def __init__(self) -> None:
        self._node: str | None = None
        self._mac: str | None = None
        self._friendly: str | None = None

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        props = discovery_info.properties or {}
        mac = props.get("mac")
        hostname = (discovery_info.hostname or "").rstrip(".")
        node = hostname[:-6] if hostname.endswith(".local") else hostname
        if not node:
            node = props.get("friendly_name") or discovery_info.name.split(".")[0]
        self._node = node
        self._mac = mac
        self._friendly = props.get("friendly_name") or node

        await self.async_set_unique_id(mac or slug(node))
        self._abort_if_unique_id_configured(updates={CONF_NODE: node})
        self.context["title_placeholders"] = {"name": self._friendly}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=self._friendly or self._node,
                data={CONF_NODE: self._node, CONF_MAC: self._mac},
            )
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._friendly or self._node},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            node = user_input[CONF_NODE].strip()
            await self.async_set_unique_id(slug(node))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=node, data={CONF_NODE: node, CONF_MAC: None})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NODE): str}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return LibrescootOptionsFlow()


class LibrescootOptionsFlow(OptionsFlow):
    """One flow: Home-Assistant-side settings + a scooter picker that branches on demand.

    The `init` form holds the OTA/relay settings and a single **Scooter** select whose default is
    this ESP's already-configured scooter (read from the ESP's `BLE Scooter nRF MAC` sensor). Choosing it
    opens a manage step (pair only if the scooter is actually asking for a passkey; unbind); other
    choices scan for a new scooter or let you type a MAC. No sub-step is shown unless picked here.
    """

    def __init__(self) -> None:
        self._settings: dict[str, Any] = {}
        self._found: dict[str, str] = {}
        self._own_mac: str | None = None
        self._scooter_mac: str | None = None
        self._scan_task: Any = None
        self._picked: str | None = None

    # ---- entity-state helpers -------------------------------------------------
    def _ents(self) -> dict[str, str]:
        return resolve_entities(
            self.hass, self.config_entry.data[CONF_NODE], self.config_entry.data.get(CONF_MAC)
        )

    def _read_ota_source(self) -> str:
        eid = self._ents().get("ota_source")
        st = self.hass.states.get(eid) if eid else None
        if st and st.state in ("HA relay", "direct GitHub"):
            return st.state
        return "HA relay"

    def _connection_status(self) -> str:
        """Markdown block for the options dialog: did HA find the ESP, which relay URL does HA hand
        out, what does the ESP actually use, and has the ESP reached the relay."""
        entry = self.config_entry
        ents = self._ents()
        lines: list[str] = []
        if ents:
            lines.append(f"✅ ESPHome device found ({len(ents)} entities of this component)")
        else:
            lines.append(
                f"❌ ESPHome device **not found** — node `{entry.data[CONF_NODE]}`, "
                f"MAC `{entry.data.get(CONF_MAC) or '—'}`. Add the ESP in the ESPHome integration, or "
                "re-add this entry with the ESP's node name."
            )

        ctx = self.hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
        proxy = self.hass.data.get(DOMAIN, {}).get("_listener")
        want = (
            f"http://{ctx['host']}:{proxy.port if proxy and proxy.port else ctx['port']}/ota/{ctx['secret']}"
            if ctx.get("host") and ctx.get("secret")
            else "—"
        )
        lines.append(f"Relay URL from HA: `{want}`")

        def state(key: str) -> str | None:
            eid = ents.get(key)
            st = self.hass.states.get(eid) if eid else None
            return st.state if st else None

        mode = state("ota_source") or "?"
        have = state("source_url") or "?"
        if mode == "direct GitHub":
            lines.append(f"ESP byte source: **direct GitHub** — the ESP ignores the relay URL (`{have}`)")
        else:
            mark = "✅" if have == want else "❌"
            lines.append(f"ESP byte source: **{mode}**, URL on the ESP: {mark} `{have}`")
            reach = state("relay_reachable")
            if reach is not None:
                lines.append(f"ESP reaches the relay: {'✅ yes' if reach == 'on' else '❌ no (' + reach + ')'}")

        if proxy is None:
            lines.append("❌ Relay listener is not running")
        else:
            seen = proxy.last_seen(ctx.get("secret", ""))
            if seen:
                ago = int(time.time() - seen[0])
                lines.append(f"Last request from this ESP: {ago} s ago — `{seen[1]}` → HTTP {seen[2]}")
            else:
                lines.append("Last request from this ESP: none since Home Assistant started")
            foreign = proxy.last_foreign()
            if foreign:
                ago = int(time.time() - foreign[0])
                lines.append(
                    f"⚠️ A request with an unknown or missing secret came in {ago} s ago "
                    f"(`{foreign[1]}` → HTTP {foreign[2]}) — an ESP uses a stale or wrong URL"
                )
        return "\n\n".join(lines)

    def _read_scooter_mac(self) -> str | None:
        eid = self._ents().get("scooter_mac")
        st = self.hass.states.get(eid) if eid else None
        if st and st.state and st.state not in ("unknown", "unavailable", "not set"):
            return st.state.upper()
        return None

    def _is_on(self, key: str) -> bool:
        eid = self._ents().get(key)
        return bool(eid and self.hass.states.is_state(eid, "on"))

    # ---- main form ------------------------------------------------------------
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        opts = self.config_entry.options
        self._scooter_mac = self._read_scooter_mac()
        if user_input is not None:
            action = user_input.pop("scooter_action", "keep")
            ota_source = user_input.pop("ota_source", None)
            self._settings = user_input  # OTA / relay / channel settings
            # OTA byte source lives on the ESP (its NVS-persisted select), not in the config entry —
            # set it there if the user changed it. The integration then stops/starts overriding the URL.
            if ota_source and ota_source != self._read_ota_source():
                eid = self._ents().get("ota_source")
                if eid:
                    await self.hass.services.async_call(
                        "select", "select_option", {"entity_id": eid, "option": ota_source}, blocking=True
                    )
            if action == "manage":
                return await self.async_step_scooter()
            if action == "scan":
                return await self.async_step_scan()
            if action == "manual":
                return await self.async_step_manual()
            return self._save()  # "keep" — just save the settings

        host = opts.get(CONF_HA_HOST) or await default_host(self.hass)
        actions = [{"value": "keep", "label": "Keep current scooter — just save settings"}]
        if self._scooter_mac:
            actions.append({"value": "manage", "label": f"{self._scooter_mac} — this ESP’s scooter (pair / unbind)"})
        actions += [
            {"value": "scan", "label": "🔍 Scan for a new scooter"},
            {"value": "manual", "label": "⌨️ Enter a scooter MAC manually"},
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_GITHUB_REPO, default=opts.get(CONF_GITHUB_REPO, DEFAULT_REPO)): str,
                vol.Optional(CONF_GITHUB_TOKEN, default=opts.get(CONF_GITHUB_TOKEN, "")): str,
                vol.Required(CONF_HA_HOST, default=host): str,
                vol.Required(CONF_HA_PORT, default=opts.get(CONF_HA_PORT, DEFAULT_HA_PORT)): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Required(CONF_CHANNEL, default=opts.get(CONF_CHANNEL, DEFAULT_CHANNEL)): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=CHANNELS)
                ),
                vol.Required("ota_source", default=self._read_ota_source()): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["HA relay", "direct GitHub"], mode=selector.SelectSelectorMode.LIST
                    )
                ),
                vol.Required("scooter_action", default="keep"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=actions, mode=selector.SelectSelectorMode.LIST)
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={"mac": self._scooter_mac or "not set", "status": self._connection_status()},
        )

    # ---- manage the already-configured scooter --------------------------------
    async def async_step_scooter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            act = user_input["action"]
            if act == "pair":
                self._picked = self._scooter_mac
                # The component never pairs unprompted. If the scooter isn't already asking for a
                # passkey, arm a deliberate (re)pairing first (presses "BLE Pair Scooter": clears any
                # stale bond + reconnects), then collect the passkey it will then display.
                if not self._is_on("passkey_required"):
                    btn = self._ents().get("pair_scooter")
                    if btn:
                        await self.hass.services.async_call(
                            "button", "press", {"entity_id": btn}, blocking=True
                        )
                return await self.async_step_pair()
            if act == "unbind":
                return await self.async_step_unbind()
            return self._save()  # done

        needs_pass = self._is_on("passkey_required")
        connected = self._is_on("connection")
        status = "asking for a passkey — pair it" if needs_pass else ("connected" if connected else "not connected right now")
        actions = [{"value": "done", "label": "Done"}]
        pair_label = "🔑 Enter passkey (pair now)" if needs_pass else "🔑 Pair / re-pair this scooter"
        actions.append({"value": "pair", "label": pair_label})
        actions.append({"value": "unbind", "label": "🔓 Remove bond (unbind) — re-pair on next connect"})
        return self.async_show_form(
            step_id="scooter",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="pair" if needs_pass else "done"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=actions, mode=selector.SelectSelectorMode.LIST)
                    )
                }
            ),
            description_placeholders={"mac": self._scooter_mac or "—", "status": status},
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._handle_picked((user_input.get("mac") or "").strip())
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required("mac", default=self._scooter_mac or ""): str}),
        )

    # ---- scan for a new scooter ----------------------------------------------
    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._scan_task is None:
            node = self.config_entry.data[CONF_NODE]
            mac = self.config_entry.data.get(CONF_MAC)
            secret = self.config_entry.data[CONF_SECRET]
            self._scan_task = self.hass.async_create_task(scan_for_scooters(self.hass, node, mac, secret))
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan", progress_action="scanning", progress_task=self._scan_task
            )
        try:
            self._found, self._own_mac = self._scan_task.result()
        except Exception:  # noqa: BLE001
            self._found, self._own_mac = {}, None
        self._scan_task = None
        return self.async_show_progress_done(next_step_id="scan_result")

    async def async_step_scan_result(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            picked = user_input["scooter"]
            if picked == _RESCAN:
                self._found = {}
                return await self.async_step_scan()
            if picked == _UNBIND:
                return await self.async_step_unbind()
            return await self._handle_picked(picked)

        options = [{"value": addr, "label": label} for addr, label in self._found.items()]
        options.append({"value": _RESCAN, "label": "🔄 Scan again"})
        if self._own_mac:
            options.append({"value": _UNBIND, "label": "🔓 Remove bond (unbind this ESP’s scooter)"})
        return self.async_show_form(
            step_id="scan_result",
            data_schema=vol.Schema(
                {
                    vol.Required("scooter"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST)
                    )
                }
            ),
            description_placeholders={"count": str(len(self._found))},
            errors={"base": "no_scooter_found"} if not self._found else None,
        )

    # ---- shared: what to do with a chosen MAC --------------------------------
    async def _handle_picked(self, mac: str) -> ConfigFlowResult:
        """This ESP's own scooter → pair only if it's actually asking for a passkey (never for an
        already-bonded/connected one); a different MAC needs a YAML change (noted to the user)."""
        self._picked = mac
        own = (self._own_mac or self._scooter_mac or "").lower()
        if own and mac.lower() == own:
            if self._is_on("passkey_required"):
                return await self.async_step_pair()
            return self._save(mac)  # already this ESP's bonded scooter — no passkey needed
        return await self.async_step_mac_note()

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # The passkey is a TEXT field: it is displayed with leading zeros and a numeric input field
        # discards those as you type. Parsed and validated here instead.
        schema = vol.Schema({vol.Required("pin"): str})
        if user_input is not None:
            pin = parse_pin(user_input["pin"])
            if pin is None:
                return self.async_show_form(
                    step_id="pair", data_schema=schema, errors={"pin": "invalid_pin"}
                )
            node = slug(self.config_entry.data[CONF_NODE])
            await self.hass.services.async_call(
                "esphome", f"{node}_passkey_reply", {"pin": pin}, blocking=True
            )
            return self._save(self._picked)
        return self.async_show_form(step_id="pair", data_schema=schema)

    async def async_step_unbind(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            btn = self._ents().get("remove_bond")
            if btn:
                await self.hass.services.async_call("button", "press", {"entity_id": btn}, blocking=True)
            return self._save()
        return self.async_show_form(
            step_id="unbind",
            data_schema=vol.Schema({}),
            description_placeholders={"mac": self._scooter_mac or "—"},
        )

    async def async_step_mac_note(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(self._picked)
        return self.async_show_form(
            step_id="mac_note",
            data_schema=vol.Schema({}),
            description_placeholders={"mac": self._picked or "—"},
        )

    def _save(self, mac: str | None = None) -> ConfigFlowResult:
        data = {**self.config_entry.options, **self._settings}
        if mac:
            data[CONF_DISCOVERED_MAC] = mac
        return self.async_create_entry(title="", data=data)
