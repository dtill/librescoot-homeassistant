"""Per-ESP status the integration tracks itself: is the ESP on the network (ping), did HA find its
ESPHome device, and which BLE link mode is it in. Entities read it and refresh on a dispatcher
signal; nothing here polls the ESP over the ESPHome API."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from icmplib import NameLookupError, SocketPermissionError, async_ping

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .const import DOMAIN, LISTENER_ROUTE
from . import scooters
from .helpers import find_esphome_device, resolve_entities

_LOGGER = logging.getLogger(__name__)

PING_INTERVAL = timedelta(seconds=30)
PING_COUNT = 3
PING_TIMEOUT = 2
OFFLINE_AFTER = 2  # failed rounds in a row before "offline" — one lost round is not an outage
API_PORT = 6053    # TCP fallback where ICMP is not permitted


def signal_esp(entry_id: str) -> str:
    return f"{DOMAIN}_esp_{entry_id}"


def entry_device_identifier(entry: ConfigEntry) -> tuple[str, str]:
    return (DOMAIN, entry.entry_id)


class EspMonitor:
    """Owned by one ESP config entry (ctx["monitor"])."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, ctx: dict) -> None:
        self.hass = hass
        self.entry = entry
        self.ctx = ctx
        self.online: bool | None = None
        self.rtt_ms: float | None = None
        self.host: str | None = None
        self.method: str | None = None  # "icmp" / "icmp (privileged)" / "tcp 6053"
        self.linked: bool | None = None
        self.link_mode: str | None = None
        self.esp_url: str | None = None      # the ESP's OTA Source URL text
        self.byte_source: str | None = None  # the ESP's OTA Source select ("HA relay" / "direct GitHub")
        self.url_mismatch: bool | None = None
        self.no_scooter = False  # the ESP reports no scooter MAC (00:00:00:00:00:00 in its YAML)
        self._mismatch_rounds = 0
        self._push_warned = False  # one warning per mismatch episode, not one per 30 s round
        self._fails = 0
        self._privileged: bool | None = None
        self._watched: tuple[str | None, ...] = ()  # ESP entity ids followed live
        self._unwatch = None

    # ---- lifecycle ----------------------------------------------------------------
    @callback
    def async_start(self) -> None:
        self.entry.async_on_unload(
            async_track_time_interval(self.hass, self._tick, PING_INTERVAL, name=f"{DOMAIN} ping")
        )
        self.entry.async_on_unload(self._stop_watch)
        self.hass.async_create_task(self._tick())

    @callback
    def _stop_watch(self) -> None:
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_esp(self.entry.entry_id))

    def want_url(self) -> str | None:
        """The relay URL Home Assistant hands this ESP (relay's live port wins over settings)."""
        proxy = self.hass.data.get(DOMAIN, {}).get("_listener")
        port = proxy.port if proxy is not None and proxy.port else self.ctx.get("port")
        if not self.ctx.get("host") or not self.ctx.get("secret"):
            return None
        return f"http://{self.ctx['host']}:{port}{LISTENER_ROUTE}/{self.ctx['secret']}"

    # ---- one round ----------------------------------------------------------------
    async def _tick(self, _now=None) -> None:
        device = find_esphome_device(self.hass, self.ctx["node"], self.ctx.get("mac"))
        ents = resolve_entities(self.hass, self.ctx["node"], self.ctx.get("mac")) if device else {}
        self.linked = bool(ents)
        self.no_scooter = bool(ents) and scooters.scooter_link(self.hass, ents)["state"] == "unset"
        self._link_device(device)
        self._watch(ents)
        self.host = self._esp_host(device)
        await self._ping()
        self._check_mismatch()
        if self._mismatch_rounds:
            # The setup-time watcher misses an ESP whose ESPHome device appeared after this entry
            # (e.g. created by the setup wizard before the first install) — correct it from here.
            await self.async_push_url()
        self._sync_issues()
        self._notify()

    def _link_device(self, esp_device: dr.DeviceEntry | None) -> None:
        """Point our entry device at the ESPHome device (via_device) once it is known. ESPHome
        devices carry no identifiers, so this is set by registry id, not by DeviceInfo."""
        if esp_device is None:
            return
        reg = dr.async_get(self.hass)
        own = reg.async_get_device(identifiers={entry_device_identifier(self.entry)})
        if own is not None and own.via_device_id != esp_device.id:
            reg.async_update_device(own.id, via_device_id=esp_device.id)

    def _esp_host(self, esp_device: dr.DeviceEntry | None) -> str | None:
        """The address the ESPHome integration itself connects to, else the mDNS name."""
        if esp_device is not None:
            for entry_id in esp_device.config_entries:
                esp_entry = self.hass.config_entries.async_get_entry(entry_id)
                if esp_entry is not None and esp_entry.domain == "esphome" and esp_entry.data.get("host"):
                    return esp_entry.data["host"]
        node = self.ctx.get("node")
        return f"{node}.local" if node else None

    def _watch(self, ents: dict[str, str]) -> None:
        """Follow the ESP's link mode, OTA Source URL and OTA Source select live."""
        ids = (ents.get("link_mode"), ents.get("source_url"), ents.get("ota_source"))
        if ids != self._watched:
            self._stop_watch()
            self._watched = ids
            live = [i for i in ids if i]
            if live:
                self._unwatch = async_track_state_change_event(self.hass, live, self._on_esp_state)
        self._read_esp_states()

    def _read_esp_states(self) -> None:
        def value(eid: str | None) -> str | None:
            st = self.hass.states.get(eid) if eid else None
            return st.state if st and st.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN) else None
        link, url, src = self._watched if len(self._watched) == 3 else (None, None, None)
        self.link_mode, self.esp_url, self.byte_source = value(link), value(url), value(src)

    @callback
    def _on_esp_state(self, event) -> None:
        self._read_esp_states()
        self._notify()

    def url_entity(self) -> str | None:
        """The ESP's OTA Source URL text entity, while it is available."""
        eid = self._watched[1] if len(self._watched) == 3 else None
        st = self.hass.states.get(eid) if eid else None
        return eid if st is not None and st.state != STATE_UNAVAILABLE else None

    async def async_push_url(self) -> bool:
        """Write the relay URL into the ESP (relay mode only). True when the ESP took it."""
        want, eid = self.want_url(), self.url_entity()
        if self.byte_source != "HA relay" or not want or not eid:
            return False
        if self.esp_url == want:
            return True
        try:
            await self.hass.services.async_call(
                "text", "set_value", {"entity_id": eid, "value": want}, blocking=True)
        except Exception as err:  # noqa: BLE001 - ESP went away mid-call
            _LOGGER.warning("%s: could not set the relay URL on the ESP: %s", self.entry.title, err)
            return False
        for _ in range(20):  # the ESP echoes the new text back over the API
            await asyncio.sleep(0.5)
            self._read_esp_states()
            if self.esp_url == want:
                _LOGGER.info("%s: ESP now uses the relay URL %s", self.entry.title, want)
                self._push_warned = False
                self._mismatch_rounds = 0
                self.url_mismatch = False
                self._sync_issues()
                self._notify()
                return True
        (_LOGGER.debug if self._push_warned else _LOGGER.warning)(
            "%s: the ESP did not take the relay URL (still %s)", self.entry.title, self.esp_url)
        self._push_warned = True
        return False

    def _check_mismatch(self) -> None:
        """Relay mode and the ESP's URL is not the one HA hands out. Needs two rounds in a row: the
        integration re-pushes the URL itself, so a single round can just be that push in flight."""
        want = self.want_url()
        if self.byte_source != "HA relay" or not self.esp_url or not want:
            self._mismatch_rounds = 0
            self.url_mismatch = None if self.byte_source is None else False
            return
        self._mismatch_rounds = self._mismatch_rounds + 1 if self.esp_url != want else 0
        self.url_mismatch = self._mismatch_rounds >= 2

    def _sync_issues(self) -> None:
        node = self.ctx.get("node", "")
        nl = f"esp_not_linked_{self.entry.entry_id}"
        if self.linked is False:
            ir.async_create_issue(
                self.hass, DOMAIN, nl, is_fixable=False, severity=ir.IssueSeverity.ERROR,
                translation_key="esp_not_linked",
                translation_placeholders={"title": self.entry.title, "node": node,
                                          "mac": self.ctx.get("mac") or "—"},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, nl)
        mm = f"relay_url_mismatch_{self.entry.entry_id}"
        if self.url_mismatch:
            ir.async_create_issue(
                # Fixable while the ESP is linked and its URL entity answers: the fix writes it.
                self.hass, DOMAIN, mm, is_fixable=bool(self.linked and self.url_entity()),
                severity=ir.IssueSeverity.WARNING, translation_key="relay_url_mismatch",
                translation_placeholders={"title": self.entry.title, "esp_url": self.esp_url or "—",
                                          "want_url": self.want_url() or "—"},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, mm)

        ns = f"no_scooter_{self.entry.entry_id}"
        if self.no_scooter:
            ir.async_create_issue(
                self.hass, DOMAIN, ns, is_fixable=bool(self.online), severity=ir.IssueSeverity.WARNING,
                translation_key="no_scooter",
                translation_placeholders={"title": self.entry.title, "node": node},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ns)

    @callback
    def async_clear_issues(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, f"no_scooter_{self.entry.entry_id}")
        ir.async_delete_issue(self.hass, DOMAIN, f"esp_not_linked_{self.entry.entry_id}")
        ir.async_delete_issue(self.hass, DOMAIN, f"relay_url_mismatch_{self.entry.entry_id}")

    # ---- reachability -------------------------------------------------------------
    async def _ping(self) -> None:
        if not self.host:
            self.online, self.rtt_ms, self.method = None, None, None
            return
        alive, rtt = await self._icmp()
        if alive is None:  # ICMP not permitted on this host → TCP to the API port instead
            alive, rtt = await self._tcp()
        if alive:
            self._fails = 0
            self.online, self.rtt_ms = True, rtt
        else:
            self._fails += 1
            self.rtt_ms = None
            if self._fails >= OFFLINE_AFTER or self.online is None:
                self.online = False

    async def _icmp(self) -> tuple[bool | None, float | None]:
        """(alive, rtt) — alive None when ICMP sockets are not permitted at all."""
        for privileged in ([self._privileged] if self._privileged is not None else [False, True]):
            try:
                host = await async_ping(
                    self.host, count=PING_COUNT, interval=0.3, timeout=PING_TIMEOUT, privileged=privileged
                )
            except SocketPermissionError:
                continue
            except NameLookupError:
                return False, None
            except OSError as err:
                _LOGGER.debug("ping %s: %s", self.host, err)
                return False, None
            self._privileged = privileged
            self.method = "icmp (privileged)" if privileged else "icmp"
            return host.is_alive, (host.avg_rtt if host.is_alive else None)
        return None, None

    async def _tcp(self) -> tuple[bool, float | None]:
        self.method = f"tcp {API_PORT}"
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            _r, w = await asyncio.wait_for(asyncio.open_connection(self.host, API_PORT), PING_TIMEOUT * 2)
        except (OSError, asyncio.TimeoutError):
            return False, None
        w.close()
        return True, (loop.time() - start) * 1000
