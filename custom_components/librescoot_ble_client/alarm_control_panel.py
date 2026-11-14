"""The scooter alarm as a Home Assistant alarm control panel.

Built only from the ESP's existing alarm entities — the ESPHome component is used as it is:
- state from `Alarm Status` (the scooter's 9a590221 alarm status, nRF >= v2.11.0-ls),
- arm/disarm through the `Alarm Armed` switch (falls back to the `Alarm Arm` / `Alarm Disarm`
  buttons on builds without it),
- `Alarm Enabled` turned on first when arming: `alarm:arm` is a no-op while the alarm is disabled,
  and `alarm:enable` in stand-by arms by itself after its 5 s delay.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import CONF_ALARM_CODE, CONF_ALARM_DISARM_DISABLES, CONF_MAC, CONF_NODE, DOMAIN
from .entity import EspEntity
from .helpers import is_relay, resolve_entities

_LOGGER = logging.getLogger(__name__)

# Scooter alarm status (text of the ESP's "Alarm Status") → HA alarm state.
STATUS_MAP = {
    "disabled": AlarmControlPanelState.DISARMED,
    "disarmed": AlarmControlPanelState.DISARMED,
    "delay-armed": AlarmControlPanelState.ARMING,
    "armed": AlarmControlPanelState.ARMED_AWAY,
    "level-1-triggered": AlarmControlPanelState.PENDING,   # warning stage
    "level-2-triggered": AlarmControlPanelState.TRIGGERED,
}
ARM_CONFIRM_S = 15  # no armed / delay-armed by then → show the scooter's real state again
_ALARM_KEYS = ("alarm_status", "alarm_enabled", "alarm_armed", "alarm_arm", "alarm_disarm",
               "alarm_last_trigger", "alarm_last_trigger_time")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    if is_relay(entry):
        return
    monitor = hass.data[DOMAIN][entry.entry_id]["monitor"]
    async_add_entities([ScooterAlarm(entry, monitor)])


class ScooterAlarm(EspEntity, AlarmControlPanelEntity):
    _attr_supported_features = AlarmControlPanelEntityFeature.ARM_AWAY
    _attr_entity_category = None  # the panel belongs in the main view, not under diagnostics
    _attr_icon = "mdi:shield-moon"

    def __init__(self, entry: ConfigEntry, monitor) -> None:
        super().__init__(entry, monitor, "alarm", "Alarm")
        self._entry = entry
        self._ents: dict[str, str] = {}
        self._unwatch = None
        self._arming_until_cancel = None
        self._arming = False
        code = entry.options.get(CONF_ALARM_CODE) or ""
        self._code = code.strip()
        self._attr_code_format = CodeFormat.NUMBER if self._code else None
        self._attr_code_arm_required = bool(self._code)

    # ---- wiring to the ESP's entities --------------------------------------------------
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()  # monitor signal (30 s) → _refresh re-resolves
        self.async_on_remove(self._stop_watch)
        self.async_on_remove(self._cancel_arming)
        self._resolve()

    @callback
    def _stop_watch(self) -> None:
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None

    def _resolve(self) -> None:
        ents = resolve_entities(self.hass, self._entry.data[CONF_NODE], self._entry.data.get(CONF_MAC))
        found = {k: ents[k] for k in _ALARM_KEYS if k in ents}
        if found != self._ents:
            self._stop_watch()
            self._ents = found
            if found:
                self._unwatch = async_track_state_change_event(self.hass, list(found.values()), self._on_esp_state)
        self._update_from_esp()

    @callback
    def _refresh(self) -> None:
        self._resolve()
        self.async_write_ha_state()

    @callback
    def _on_esp_state(self, event) -> None:
        self._update_from_esp()
        self.async_write_ha_state()

    def _state(self, key: str) -> str | None:
        eid = self._ents.get(key)
        st = self.hass.states.get(eid) if eid else None
        return st.state if st and st.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN) else None

    def _update_from_esp(self) -> None:
        status = self._state("alarm_status")
        mapped = STATUS_MAP.get(status) if status else None
        if self._arming and mapped in (AlarmControlPanelState.ARMING, AlarmControlPanelState.ARMED_AWAY,
                                       AlarmControlPanelState.PENDING, AlarmControlPanelState.TRIGGERED):
            self._cancel_arming()  # the scooter confirmed (or something more urgent happened)
        if self._arming:
            mapped = AlarmControlPanelState.ARMING
        self._attr_alarm_state = mapped
        if status and status not in STATUS_MAP:
            _LOGGER.debug("alarm: unknown scooter status %r", status)

    @property
    def available(self) -> bool:
        return self._state("alarm_status") is not None and self._monitor.online is not False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "scooter_status": self._state("alarm_status"),
            "enabled": self._state("alarm_enabled") == "on" if self._state("alarm_enabled") else None,
            "last_trigger": self._state("alarm_last_trigger"),
            "last_trigger_time": self._state("alarm_last_trigger_time"),
        }

    # ---- actions -----------------------------------------------------------------------
    def _check_code(self, code: str | None) -> None:
        if self._code and (code or "").strip() != self._code:
            raise ServiceValidationError("wrong alarm code")

    async def _call(self, domain: str, service: str, key: str) -> None:
        eid = self._ents.get(key)
        if eid is None:
            raise HomeAssistantError(f"the ESP has no '{key.replace('_', ' ')}' entity")
        await self.hass.services.async_call(domain, service, {"entity_id": eid}, blocking=True)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        self._check_code(code)
        self._resolve()
        if self._state("alarm_enabled") == "off":
            # Enabling arms by itself in stand-by (waiting_enabled → delay_armed → armed); a separate
            # arm command would be ignored while the alarm is still disabled.
            await self._call("switch", "turn_on", "alarm_enabled")
        elif "alarm_armed" in self._ents:
            await self._call("switch", "turn_on", "alarm_armed")
        else:
            await self._call("button", "press", "alarm_arm")
        self._start_arming()

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        self._check_code(code)
        self._resolve()
        self._cancel_arming()
        if "alarm_armed" in self._ents:
            await self._call("switch", "turn_off", "alarm_armed")
        else:
            await self._call("button", "press", "alarm_disarm")
        if self._entry.options.get(CONF_ALARM_DISARM_DISABLES) and self._state("alarm_enabled") == "on":
            await self._call("switch", "turn_off", "alarm_enabled")

    # ---- "arming" until the scooter confirms --------------------------------------------
    def _start_arming(self) -> None:
        self._cancel_arming()
        self._arming = True
        self._arming_until_cancel = async_call_later(self.hass, ARM_CONFIRM_S, self._arm_timeout)
        self._update_from_esp()
        self.async_write_ha_state()

    @callback
    def _cancel_arming(self) -> None:
        self._arming = False
        if self._arming_until_cancel is not None:
            self._arming_until_cancel()
            self._arming_until_cancel = None

    @callback
    def _arm_timeout(self, _now) -> None:
        self._arming_until_cancel = None
        if self._arming:
            self._arming = False
            _LOGGER.warning(
                "alarm %s: not armed after %d s (scooter status %r) — is the scooter in stand-by and the "
                "BLE link up?", self._entry.title, ARM_CONFIRM_S, self._state("alarm_status"),
            )
        self._update_from_esp()
        self.async_write_ha_state()
