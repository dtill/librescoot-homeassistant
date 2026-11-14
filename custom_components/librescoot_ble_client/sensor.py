"""Sensors.

ESP entry: the ESP's BLE link mode (read-only mirror of its select) and what the relay knows about
this ESP (URL, last request, bytes served, transfer speed, chain build).
Relay entry: Home-Assistant-wide relay state (GitHub API budget, registered ESPs, total bytes,
merged-bundle cache, xdelta3 availability).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfDataRate, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import chain as chain_mod
from .const import DOMAIN
from .entity import EspEntity, EspRelayEntity, RelayEntity
from .helpers import esp_entries, is_relay

LINK_MODES = ["always", "auto", "interval", "scan", "disconnect"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    if is_relay(entry):
        async_add_entities([
            GithubRemaining(entry), RegisteredEsps(entry), BytesServedTotal(entry),
            ChainCacheSize(entry), Xdelta3Status(entry),
        ])
        return
    monitor = hass.data[DOMAIN][entry.entry_id]["monitor"]
    async_add_entities([
        BleLinkMode(entry, monitor), RelayUrl(entry, monitor), RelayLastRequest(entry, monitor),
        RelayBytesServed(entry, monitor), RelayTransferSpeed(entry, monitor), ChainBuild(entry, monitor),
    ])


# ---- ESP entry ------------------------------------------------------------------------

class BleLinkMode(EspEntity, SensorEntity):
    """Changing the mode stays on the ESP's own select; this mirrors it for history/automations."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = LINK_MODES
    _attr_icon = "mdi:bluetooth-settings"

    def __init__(self, entry, monitor) -> None:
        super().__init__(entry, monitor, "ble_link_mode", "BLE Link Mode")

    @property
    def native_value(self) -> str | None:
        mode = self._monitor.link_mode
        return mode if mode in LINK_MODES else None


class RelayUrl(EspRelayEntity, SensorEntity):
    _attr_icon = "mdi:link-variant"

    def __init__(self, entry, monitor) -> None:
        super().__init__(entry, monitor, "relay_url", "Relay URL")

    @property
    def native_value(self) -> str | None:
        return self._monitor.want_url()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"esp_url": self._monitor.esp_url, "byte_source": self._monitor.byte_source}


class RelayLastRequest(EspRelayEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:swap-horizontal"

    def __init__(self, entry, monitor) -> None:
        super().__init__(entry, monitor, "relay_last_request", "Relay Last Request")

    def _seen(self):
        return self._proxy.last_seen(self._secret) if self._proxy is not None else None

    @property
    def native_value(self) -> datetime | None:
        seen = self._seen()
        return datetime.fromtimestamp(seen[0], timezone.utc) if seen else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        seen = self._seen()
        return {"endpoint": seen[1] if seen else None, "http_status": seen[2] if seen else None}


class RelayBytesServed(EspRelayEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_suggested_display_precision = 2

    def __init__(self, entry, monitor) -> None:
        super().__init__(entry, monitor, "relay_bytes_served", "Relay Bytes Served")

    @property
    def native_value(self) -> int:
        return self._proxy.served(self._secret)


class RelayTransferSpeed(EspRelayEntity, SensorEntity):
    """Relay → ESP. The relay streams GitHub's bytes straight through, so this is also the rate it
    pulls from GitHub — and in practice the ESP's BLE upload rate."""

    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfDataRate.BYTES_PER_SECOND
    _attr_suggested_unit_of_measurement = UnitOfDataRate.KILOBYTES_PER_SECOND
    _attr_suggested_display_precision = 1

    def __init__(self, entry, monitor) -> None:
        super().__init__(entry, monitor, "relay_transfer_speed", "Relay Transfer Speed")

    @property
    def native_value(self) -> float:
        return round(self._proxy.rate(self._secret), 1)


class ChainBuild(EspRelayEntity, SensorEntity):
    _attr_icon = "mdi:link-box-variant"

    def __init__(self, entry, monitor) -> None:
        super().__init__(entry, monitor, "chain_build", "Chain Build")

    @property
    def native_value(self) -> str:
        return self._proxy.chain_status(self._secret)


# ---- relay entry ----------------------------------------------------------------------

class GithubRemaining(RelayEntity, SensorEntity):
    _attr_icon = "mdi:github"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "requests"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "github_remaining", "GitHub API Remaining")
        self._entry = entry

    @property
    def available(self) -> bool:
        return self._proxy is not None

    @property
    def native_value(self) -> int | None:
        return self._proxy.github.get("remaining")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        gh = self._proxy.github if self._proxy is not None else {}
        reset = gh.get("reset")
        return {
            "limit": gh.get("limit"),
            "resets_at": datetime.fromtimestamp(reset, timezone.utc).isoformat() if reset else None,
            "token": bool(self._entry.options.get("github_token")),
        }


class RegisteredEsps(RelayEntity, SensorEntity):
    _attr_icon = "mdi:chip"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "registered_esps", "Registered ESPs")

    @property
    def available(self) -> bool:
        return self._proxy is not None

    @property
    def native_value(self) -> int:
        return self._proxy.registered

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"entries": [e.title for e in esp_entries(self.hass)]}


class BytesServedTotal(RelayEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_suggested_display_precision = 2

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "bytes_served_total", "Bytes Served Total")

    @property
    def available(self) -> bool:
        return self._proxy is not None

    @property
    def native_value(self) -> int:
        return self._proxy.served_total()


class ChainCacheSize(RelayEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_suggested_display_precision = 1

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "chain_cache_size", "Chain Cache Size")

    @property
    def available(self) -> bool:
        return self._proxy is not None

    @property
    def native_value(self) -> int | None:
        return self._proxy.cache_bytes


class Xdelta3Status(RelayEntity, SensorEntity):
    """Whether delta chains can be merged on this host (bundled binary for this architecture)."""

    _attr_icon = "mdi:file-compare"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "xdelta3", "xdelta3")

    @property
    def native_value(self) -> str:
        return chain_mod.status()[:255]
