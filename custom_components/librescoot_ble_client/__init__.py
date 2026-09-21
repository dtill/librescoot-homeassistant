"""The Librescoot-BLE-Client integration.

Adds two helpers on top of the `librescoot_ble_client` ESPHome component:
  * a pairing helper (forwards a passkey to the component's passkey_reply action), and
  * an OTA byte listener that streams GitHub firmware assets to the ESP over plain HTTP with
    Range support (listener.py) — so updates work even on an HTTPS-only HA and on the classic
    ESP that cannot do the GitHub-CDN TLS handshake.

The single update entity lives on the ESP (surfaced by the ESPHome integration); this
integration never creates a second one — it only feeds and configures it.
"""
from __future__ import annotations

import logging
import secrets

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import (
    CONF_CHANNEL,
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
    ENTITY_PAIRING_REQUIRED,
    ENTITY_PASSKEY_REQUIRED,
    ENTITY_REBOOT_REQUIRED,
    ENTITY_REMOVE_BOND,
    LISTENER_ROUTE,
    SERVICE_PASSKEY_REPLY,
    SERVICE_REBOOT,
    SERVICE_UPDATE,
)
from .helpers import default_host, find_esphome_device, parse_pin, resolve_entities, slug
from .listener import OtaProxy

_LOGGER = logging.getLogger(__name__)


async def _build_ctx(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    opts = entry.options
    return {
        "entry_id": entry.entry_id,
        "node": entry.data[CONF_NODE],
        "mac": entry.data.get(CONF_MAC),
        "secret": entry.data[CONF_SECRET],
        "repo": opts.get(CONF_GITHUB_REPO, DEFAULT_REPO),
        "token": opts.get(CONF_GITHUB_TOKEN) or None,
        "channel": opts.get(CONF_CHANNEL, DEFAULT_CHANNEL),
        "host": opts.get(CONF_HA_HOST) or await default_host(hass),
        "port": opts.get(CONF_HA_PORT, DEFAULT_HA_PORT),
    }


def _source_url(ctx: dict) -> str:
    return f"http://{ctx['host']}:{ctx['port']}{LISTENER_ROUTE}/{ctx['secret']}"


def _pick_ctx(hass: HomeAssistant, call: ServiceCall) -> dict:
    entries = {k: v for k, v in hass.data.get(DOMAIN, {}).items() if isinstance(v, dict) and "node" in v}
    want = call.data.get("node")
    if want:
        for ctx in entries.values():
            if slug(ctx["node"]) == slug(want):
                return ctx
        raise RuntimeError(f"no configured Librescoot-BLE-Client node '{want}'")
    if len(entries) == 1:
        return next(iter(entries.values()))
    raise RuntimeError("multiple nodes configured — pass 'node' to select one")


def _available(hass: HomeAssistant, entity_id: str) -> bool:
    st = hass.states.get(entity_id)
    return st is not None and st.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _esp_wants_relay(hass: HomeAssistant, ctx: dict) -> bool:
    """The ESP's **OTA Source** select owns the decision. Only override the byte URL in HA-relay
    mode; in "direct GitHub" mode the ESP points itself at GitHub and we must leave it alone."""
    eid = resolve_entities(hass, ctx["node"], ctx.get("mac")).get("ota_source")
    if not eid:
        return True  # select not present (older build) → behave as before (relay)
    st = hass.states.get(eid)
    if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return False  # mode not known yet → do NOT override (avoid clobbering a direct-GitHub URL)
    return st.state == "HA relay"


async def _apply_source_url(hass: HomeAssistant, ctx: dict, entity_id: str) -> None:
    if not _esp_wants_relay(hass, ctx):
        return  # ESP is in direct-GitHub mode — do not touch its URL
    want = _source_url(ctx)
    st = hass.states.get(entity_id)
    if st is not None and st.state == want:
        return  # already correct — don't spam the ESP
    await hass.services.async_call(
        "text", "set_value", {"entity_id": entity_id, "value": want}, blocking=True
    )
    _LOGGER.info("pointed %s OTA source at %s", ctx["node"], want)


def _ensure_source_url(hass: HomeAssistant, entry: ConfigEntry, ctx: dict) -> None:
    """Keep the ESP's OTA Source URL pointed at our listener **while it is in HA-relay mode**.

    Re-applied every time the URL (or the OTA Source select) changes and is available — the ESP
    resets the text to its compiled default on every reboot/reflash. `_apply_source_url` only writes
    in relay mode and only when the value differs, so this is idempotent and won't loop."""
    ents = resolve_entities(hass, ctx["node"], ctx.get("mac"))
    eid = ents.get("source_url")
    if not eid:
        _LOGGER.debug("OTA Source URL entity not found yet for %s", ctx["node"])
        return
    if _available(hass, eid):
        hass.async_create_task(_apply_source_url(hass, ctx, eid))

    @callback
    def _on_state(event) -> None:
        new = event.data.get("new_state")
        if new is not None and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            hass.async_create_task(_apply_source_url(hass, ctx, eid))

    # Watch both the URL entity AND the OTA Source select: flipping the select back to "HA relay"
    # must re-apply the relay URL; flipping to "direct GitHub" makes _apply_source_url a no-op.
    watch = [eid]
    src = ents.get("ota_source")
    if src:
        watch.append(src)

    entry.async_on_unload(async_track_state_change_event(hass, watch, _on_state))


def _enable_disabled_entities(hass: HomeAssistant, ctx: dict, names: list[str]) -> None:
    """Some sensors (BLE Passkey Required, OTA Reboot Required) ship disabled_by_default so they don't
    clutter the device for people who don't run this integration. We need their state to drive
    the Repair issues, so enable any that are still disabled and reload the ESPHome entry once so
    the entities are actually added."""
    device = find_esphome_device(hass, ctx["node"], ctx.get("mac"))
    if device is None:
        return
    ent_reg = er.async_get(hass)
    wanted = set(names)
    changed = False
    for e in er.async_entries_for_device(ent_reg, device.id, include_disabled_entities=True):
        if (e.original_name or e.name) in wanted and e.disabled_by is not None:
            ent_reg.async_update_entity(e.entity_id, disabled_by=None)
            changed = True
    if not changed:
        return  # none present yet, or all already enabled
    for entry_id in device.config_entries:
        esp = hass.config_entries.async_get_entry(entry_id)
        if esp is not None and esp.domain == "esphome":
            hass.async_create_task(hass.config_entries.async_reload(entry_id))
            _LOGGER.info("enabled %s and reloaded ESPHome entry for %s", ", ".join(names), ctx["node"])
            break




def _pairing_issue_id(entry: ConfigEntry) -> str:
    return f"pairing_{entry.entry_id}"


def _watch_pairing(hass: HomeAssistant, entry: ConfigEntry, ctx: dict) -> None:
    """Raise a fixable Repair issue while the configured scooter is in range but not paired.

    Driven by the ESP's "BLE Pairing Required" problem sensor, which is on ONLY when the configured
    scooter is actually advertising nearby yet unbonded — never merely because it's away or briefly
    disconnected. Fixing it runs the pairing flow (arms 'BLE Pair Scooter', then forwards the passkey)."""
    eid = resolve_entities(hass, ctx["node"], ctx.get("mac")).get("pairing_required")
    if not eid:
        return
    issue_id = _pairing_issue_id(entry)

    @callback
    def _sync(state) -> None:
        if state is None:
            return
        # Only "on" is a confirmed, actionable problem (scooter in range + unbonded). "off" (a live
        # bonded connection) and "unknown" (not connected / can't tell) both mean "no Repair now".
        if state.state == "on":
            ir.async_create_issue(
                hass, DOMAIN, issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="pairing_required",
                translation_placeholders={"node": ctx["node"]},
                data={"node": ctx["node"], "mac": ctx.get("mac"), "entry_id": entry.entry_id},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    _sync(hass.states.get(eid))

    @callback
    def _on_state(event) -> None:
        _sync(event.data.get("new_state"))

    entry.async_on_unload(async_track_state_change_event(hass, [eid], _on_state))


def _reboot_issue_id(entry: ConfigEntry) -> str:
    return f"reboot_{entry.entry_id}"


def _watch_reboot(hass: HomeAssistant, entry: ConfigEntry, ctx: dict) -> None:
    """Raise a fixable Repair while the scooter is stuck in pending-reboot (>20 min).

    The ESP's "OTA Reboot Required" problem sensor turns on after 20 min of continuous
    pending-reboot; fixing the issue restarts the scooter via its Reboot button."""
    eid = resolve_entities(hass, ctx["node"], ctx.get("mac")).get("reboot_required")
    if not eid:
        return
    issue_id = _reboot_issue_id(entry)

    @callback
    def _sync(state) -> None:
        if state is None:
            return
        if state.state == "on":
            ir.async_create_issue(
                hass, DOMAIN, issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="reboot_required",
                translation_placeholders={"node": ctx["node"]},
                data={"node": ctx["node"], "mac": ctx.get("mac"), "entry_id": entry.entry_id},
            )
        elif state.state == "off":
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    _sync(hass.states.get(eid))

    @callback
    def _on_state(event) -> None:
        _sync(event.data.get("new_state"))

    entry.async_on_unload(async_track_state_change_event(hass, [eid], _on_state))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    # One-time per-device bearer secret, persisted in the entry.
    if CONF_SECRET not in entry.data:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_SECRET: secrets.token_urlsafe(18)}
        )

    ctx = await _build_ctx(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = ctx

    # Shared plain-HTTP byte listener; each device registers its secret.
    # One listener is shared by every configured device. Only publish it in hass.data once it really
    # listens, and never remove an already-working one on our own failure: with two entries (one per
    # board) a transient bind failure would otherwise drop the sibling's working proxy and leave this
    # entry rebuilding a proxy that can never bind — an endless retry loop.
    proxy: OtaProxy | None = hass.data[DOMAIN].get("_listener")
    created = proxy is None
    if proxy is None:
        proxy = OtaProxy(hass)
    if not await proxy.start(ctx["port"]):
        # Without the listener the ESP can never fetch bytes in HA-relay mode — fail the setup so
        # Home Assistant retries (e.g. once whatever holds the port is released) instead of running
        # silently broken.
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise ConfigEntryNotReady(f"OTA byte listener could not bind port {ctx['port']}")
    if created:
        hass.data[DOMAIN]["_listener"] = proxy
    proxy.register(ctx["secret"], ctx["repo"], ctx["token"])
    if proxy.port is not None and proxy.port != ctx["port"]:
        _LOGGER.warning(
            "listener already runs on port %d; entry wants %d — using %d "
            "(restart HA after removing the other entry to change it)",
            proxy.port, ctx["port"], proxy.port,
        )

    _ensure_source_url(hass, entry, ctx)

    @callback
    def _registry_fixups(_now=None) -> None:
        # Enable the disabled-by-default problem sensors. Run deferred + retried: at our setup time
        # the ESPHome device/entities may not be registered yet (setup order isn't guaranteed).
        _enable_disabled_entities(
            hass, ctx,
            [ENTITY_PAIRING_REQUIRED, ENTITY_PASSKEY_REQUIRED, ENTITY_REBOOT_REQUIRED, ENTITY_REMOVE_BOND],
        )

    _registry_fixups()  # immediate, in case they're already there
    for _delay in (5, 20, 60):
        entry.async_on_unload(async_call_later(hass, _delay, _registry_fixups))
    _watch_pairing(hass, entry, ctx)
    _watch_reboot(hass, entry, ctx)

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ir.async_delete_issue(hass, DOMAIN, _pairing_issue_id(entry))
    ir.async_delete_issue(hass, DOMAIN, _reboot_issue_id(entry))
    ctx = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    proxy: OtaProxy | None = hass.data.get(DOMAIN, {}).get("_listener")
    if ctx and proxy is not None:
        proxy.unregister(ctx["secret"])
        if proxy.empty:
            await proxy.stop()
            hass.data[DOMAIN].pop("_listener", None)
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _do_update(hass: HomeAssistant, ctx: dict, install: bool, component: str = "auto") -> None:
    """Point the ESP at our byte relay and optionally trigger an install.

    The scooter is two boards (MDB + DBC) with one update entity each, and it reboots after
    installing either — so exactly one board is installed per call. `component` selects it;
    "auto" takes whichever entity currently reports an update (the component only ever offers
    one at a time, in the right order), and refuses if that is ambiguous or unavailable.
    """
    ents = resolve_entities(hass, ctx["node"], ctx.get("mac"))
    if not ents:
        raise RuntimeError(
            f"could not find the ESP's OTA entities for node '{ctx['node']}' — "
            "is the ESPHome device set up in HA?"
        )
    # Only steer the byte source while the ESP is in HA-relay mode; in "direct GitHub" mode the ESP
    # owns its URL and overwriting it here would fight the device's own setting.
    if "source_url" in ents and _esp_wants_relay(hass, ctx):
        await hass.services.async_call(
            "text", "set_value", {"entity_id": ents["source_url"], "value": _source_url(ctx)}, blocking=True
        )
    if not install:
        return

    if component in ("mdb", "dbc"):
        eid = ents.get(f"update_{component}")
        if not eid:
            raise RuntimeError(f"no '{component.upper()}' update entity for node '{ctx['node']}'")
    else:
        offered = [
            ents[k] for k in ("update_mdb", "update_dbc")
            if k in ents and (st := hass.states.get(ents[k])) is not None and st.state == "on"
        ]
        if len(offered) > 1:
            raise RuntimeError(
                "both MDB and DBC report an update — pass 'component: mdb' or 'component: dbc'"
            )
        if not offered:
            # Nothing on offer: fall back to a legacy single-entity build, else there is nothing to do.
            if "update_legacy" not in ents:
                _LOGGER.info("update service: no update is currently offered for %s", ctx["node"])
                return
            offered = [ents["update_legacy"]]
        eid = offered[0]

    await hass.services.async_call("update", "install", {"entity_id": eid}, blocking=True)
    _LOGGER.warning(
        "install triggered for node %s (%s) — the scooter verifies the SHA and installs",
        ctx["node"], eid,
    )


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.data[DOMAIN].get("_services_registered"):
        return
    hass.data[DOMAIN]["_services_registered"] = True

    async def _handle_passkey(call: ServiceCall) -> None:
        ctx = _pick_ctx(hass, call)
        node = slug(ctx["node"])
        pin = parse_pin(call.data["pin"])
        if pin is None:
            raise vol.Invalid("pin must be the 1-6 digit passkey shown on the scooter dashboard")
        await hass.services.async_call(
            "esphome", f"{node}_passkey_reply", {"pin": pin}, blocking=True
        )
        _LOGGER.info("passkey forwarded to esphome.%s_passkey_reply", node)

    async def _handle_update(call: ServiceCall) -> None:
        ctx = _pick_ctx(hass, call)
        await _do_update(
            hass, ctx,
            install=bool(call.data.get("install", False)),
            component=call.data.get("component", "auto"),
        )

    async def _handle_reboot(call: ServiceCall) -> None:
        ctx = _pick_ctx(hass, call)
        eid = resolve_entities(hass, ctx["node"], ctx.get("mac")).get("reboot_button")
        if not eid:
            raise RuntimeError(f"no reboot button entity for node '{ctx['node']}'")
        await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
        _LOGGER.warning("scooter restart requested for node %s (pending-reboot recovery)", ctx["node"])

    hass.services.async_register(
        DOMAIN, SERVICE_PASSKEY_REPLY, _handle_passkey,
        schema=vol.Schema(
            {
                # Accept a string so a dashboard code with leading zeros survives the UI (a
                # number field discards them); parse_pin validates and converts it.
                vol.Required("pin"): vol.Any(str, int),
                vol.Optional("node"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE, _handle_update,
        schema=vol.Schema(
            {
                vol.Optional("install", default=False): cv.boolean,
                vol.Optional("component", default="auto"): vol.In(["auto", "mdb", "dbc"]),
                vol.Optional("node"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REBOOT, _handle_reboot,
        schema=vol.Schema({vol.Optional("node"): cv.string}),
    )
