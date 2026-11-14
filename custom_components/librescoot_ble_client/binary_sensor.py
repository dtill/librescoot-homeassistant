"""Binary sensors.

ESP entry: on the network (ping), linked to its ESPHome device, relay URL mismatch.
Relay entry: the byte listener is not running.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EspEntity, EspRelayEntity, RelayEntity
from .helpers import is_relay


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    if is_relay(entry):
        async_add_entities([RelayListenerDown(entry)])
        return
    monitor = hass.data[DOMAIN][entry.entry_id]["monitor"]
    async_add_entities([EspOnline(entry, monitor), EspNotLinked(entry, monitor), RelayUrlMismatch(entry, monitor)])


class EspOnline(EspEntity, BinarySensorEntity):
    """Ping to the ESP's IP — independent of the ESPHome API connection."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry, monitor) -> None:
        super().__init__(entry, monitor, "esp_online", "ESP Online")

    @property
    def is_on(self) -> bool | None:
        return self._monitor.online

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._monitor
        return {
            "host": m.host,
            "method": m.method,
            "round_trip_ms": round(m.rtt_ms, 1) if m.rtt_ms is not None else None,
        }


class EspNotLinked(EspEntity, BinarySensorEntity):
    """Problem while this entry cannot find its ESPHome device / entities — no Repair, no relay URL,
    no update steering until it is fixed."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = None  # a problem sensor belongs in the main view

    def __init__(self, entry, monitor) -> None:
        super().__init__(entry, monitor, "esp_linked", "ESP Linked")
        self._entry = entry

    @property
    def is_on(self) -> bool | None:
        return None if self._monitor.linked is None else not self._monitor.linked

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"node": self._entry.data.get("node_name"), "mac": self._entry.data.get("mac")}


class RelayUrlMismatch(EspRelayEntity, BinarySensorEntity):
    """Relay mode, but the ESP uses another URL than the one HA hands out (stale/wrong URL)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = None

    def __init__(self, entry, monitor) -> None:
        super().__init__(entry, monitor, "relay_url_mismatch", "Relay URL Mismatch")

    @property
    def is_on(self) -> bool | None:
        return self._monitor.url_mismatch

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"esp_url": self._monitor.esp_url, "relay_url": self._monitor.want_url(),
                "byte_source": self._monitor.byte_source}


class RelayListenerDown(RelayEntity, BinarySensorEntity):
    """Problem while the byte listener is not running (e.g. its port is taken)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = None

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "listener", "Listener")

    @property
    def is_on(self) -> bool:
        proxy = self._proxy
        return proxy is None or proxy.port is None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        proxy = self._proxy
        foreign = proxy.last_foreign() if proxy is not None else None
        return {
            "port": proxy.port if proxy is not None else None,
            "last_unknown_secret_request": (
                {"at": datetime.fromtimestamp(foreign[0], timezone.utc).isoformat(),
                 "endpoint": foreign[1], "http_status": foreign[2]} if foreign else None
            ),
        }
