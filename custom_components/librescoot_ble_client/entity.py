"""Base entity: every entity of an ESP entry hangs on that entry's own device."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, RELAY_UNIQUE_ID
from .listener import SIGNAL_RELAY_STATS
from .monitor import EspMonitor, entry_device_identifier, signal_esp


def esp_device_info(entry: ConfigEntry) -> DeviceInfo:
    # Not the ESPHome device itself (that one belongs to the ESPHome integration): a device of this
    # entry, linked to it via via_device by the monitor once the ESPHome device is found.
    return DeviceInfo(
        identifiers={entry_device_identifier(entry)},
        name=f"{entry.title} (HA)",
        manufacturer="Librescoot",
        model="Home Assistant link",
    )


class EspEntity(Entity):
    """Reads EspMonitor; refreshed by the monitor's dispatcher signal."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, monitor: EspMonitor, key: str, name: str) -> None:
        self._monitor = monitor
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = esp_device_info(entry)
        self._signal = signal_esp(entry.entry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self._signal, self._refresh))

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()


class EspRelayEntity(EspEntity):
    """An ESP-entry entity whose value comes from the relay's statistics for this ESP's secret."""

    def __init__(self, entry: ConfigEntry, monitor: EspMonitor, key: str, name: str) -> None:
        super().__init__(entry, monitor, key, name)
        self._secret = entry.data.get("secret")

    @property
    def _proxy(self):
        return self.hass.data.get(DOMAIN, {}).get("_listener")

    @property
    def available(self) -> bool:
        return self._proxy is not None  # no relay entry / listener stopped → unavailable

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_RELAY_STATS, self._refresh))


def relay_device_info() -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, RELAY_UNIQUE_ID)})


class RelayEntity(Entity):
    """An entity of the HA-wide Librescoot Relay entry."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, key: str, name: str) -> None:
        self._attr_unique_id = f"{RELAY_UNIQUE_ID}_{key}"
        self._attr_name = name
        self._attr_device_info = relay_device_info()

    @property
    def _proxy(self):
        return self.hass.data.get(DOMAIN, {}).get("_listener")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_RELAY_STATS, self._refresh))

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()
