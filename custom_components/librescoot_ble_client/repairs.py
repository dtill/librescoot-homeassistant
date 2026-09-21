"""Repair flows.

Two fixable Repair issues, both driven by the ESP's problem sensors:

  * pairing — the "BLE Pairing Required" sensor turns on when the configured scooter is in range but
    not bonded; fixing runs the pairing flow (arms 'BLE Pair Scooter', then forwards the passkey the
    scooter then displays). This is the same flow the options-flow pair step uses.
  * reboot — the "OTA Reboot Required" sensor turns on when the scooter has been stuck in
    pending-reboot for >20 min; fixing restarts the scooter via the reboot_scooter service.
"""
from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_MAC, CONF_NODE, DOMAIN, SERVICE_PASSKEY_REPLY, SERVICE_REBOOT
from .helpers import parse_pin, resolve_entities

# A TEXT field, not a number: the passkey is shown on the dashboard with leading zeros, and a
# numeric input field discards them as you type ("012345" collapses to "12345" or refuses the
# keystroke). The value is validated and converted here instead.
_PIN_SCHEMA = vol.Schema({vol.Required("pin"): str})


class PairingRepairFlow(RepairsFlow):
    """Fix "scooter detected but not paired".

    Two steps, never a bare passkey field first: a "start" confirmation that explains the situation,
    then — only after the user submits it — pairing is armed on the ESP (press 'BLE Pair Scooter', which
    clears any stale bond and connects; that is what makes the scooter display the passkey) and the
    code is collected. There is deliberately no "remove bond" here: in the not-paired state there is
    no live bond to forget, and the pair action already clears any stale one. Unbinding a working
    scooter lives in the integration's options (the gear), not in this Repair.
    """

    def __init__(self, data: dict | None) -> None:
        self._data = data or {}
        self._verify_task: asyncio.Task | None = None

    def _ents(self) -> dict:
        # Resolve WITH the ESP's MAC (from the issue data): the ESPHome node name and the HA device
        # name don't always slug-match, so a name-only lookup can miss every entity — which silently
        # skipped the 'BLE Pair Scooter' press and was why no code appeared on the scooter.
        return resolve_entities(self.hass, self._data.get("node"), self._data.get("mac"))

    def _state(self, ents: dict, key: str) -> str | None:
        eid = ents.get(key)
        st = self.hass.states.get(eid) if eid else None
        if st is None or st.state in ("unknown", "unavailable"):
            return None
        return st.state

    def _placeholders(self, ents: dict | None = None) -> dict:
        ents = ents if ents is not None else self._ents()
        rssi = self._state(ents, "rssi")
        rssi_txt = f"{rssi} dBm" if rssi is not None else "unknown (no advertisement heard — the scooter may be out of range or asleep)"
        in_range = (
            self._state(ents, "pairing_required") == "on"
            or self._state(ents, "connection") == "on"
        )
        return {
            "node": self._data.get("node", ""),
            "mac": self._state(ents, "scooter_mac") or "?",
            "rssi": rssi_txt,
            "range": "yes" if in_range else "not right now",
        }

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_start()

    async def async_step_start(self, user_input: dict | None = None) -> FlowResult:
        # A single, explicit action button — picking it is what triggers pairing (below). No passkey
        # field here: at this point the scooter has not been asked to pair and shows no code yet.
        return self.async_show_menu(
            step_id="start",
            menu_options=["pair"],
            description_placeholders=self._placeholders(),
        )

    async def async_step_pair(self, user_input: dict | None = None) -> FlowResult:
        node = self._data.get("node")
        ents = self._ents()
        if user_input is None:
            # This is the trigger: arm pairing on the ESP (press 'BLE Pair Scooter' → clears any stale
            # bond, connects, and makes the scooter display the passkey), unless it's already asking.
            already_asking = self._state(ents, "passkey_required") == "on"
            if not already_asking and ents.get("pair_scooter"):
                await self.hass.services.async_call(
                    "button", "press", {"entity_id": ents["pair_scooter"]}, blocking=True
                )
            return self.async_show_form(
                step_id="pair",
                data_schema=_PIN_SCHEMA,
                description_placeholders=self._placeholders(ents),
            )
        pin = parse_pin(user_input["pin"])
        if pin is None:
            return self.async_show_form(
                step_id="pair",
                data_schema=_PIN_SCHEMA,
                errors={"pin": "invalid_pin"},
                description_placeholders=self._placeholders(ents),
            )
        # Prefer the ESP's own entities — the same pair a phone uses directly: fill "BLE Pairing
        # Passkey", then press "BLE Pairing Send Passkey". Falls back to the service for firmware
        # that doesn't expose them.
        txt = ents.get("passkey_text")
        btn = ents.get("passkey_send")
        if txt and btn:
            await self.hass.services.async_call(
                "text", "set_value", {"entity_id": txt, "value": f"{pin:06d}"}, blocking=True
            )
            await self.hass.services.async_call(
                "button", "press", {"entity_id": btn}, blocking=True
            )
        else:
            call_data: dict = {"pin": pin}
            if node:
                call_data["node"] = node
            await self.hass.services.async_call(
                DOMAIN, SERVICE_PASSKEY_REPLY, call_data, blocking=True
            )
        # Do NOT report success yet — the code may be wrong or bonding may not complete.
        return await self.async_step_verify()

    def _bonded(self) -> bool:
        """Is the scooter actually paired right now?

        A connection is NOT proof: the GATT link is established before authentication, so it comes up
        even when pairing then fails — reporting that as success is exactly what made this dialog
        claim "paired" when nothing was. The ESP's "BLE Pairing Required" sensor answers honestly
        (it checks the controller's bond list), so success needs a live connection AND that sensor
        explicitly off. If the sensor is missing (older firmware) fall back to the connection.
        """
        ents = self._ents()
        eid = ents.get("connection")
        if not eid or not self.hass.states.is_state(eid, "on"):
            return False
        pid = ents.get("pairing_required")
        if not pid:
            return True
        st = self.hass.states.get(pid)
        return st is not None and st.state == "off"

    async def _wait_connected(self, timeout: float = 45.0) -> bool:
        if not self._ents().get("connection"):
            return False
        deadline = self.hass.loop.time() + timeout
        while self.hass.loop.time() < deadline:
            if self._bonded():
                return True
            await asyncio.sleep(1)
        return self._bonded()

    async def async_step_verify(self, user_input: dict | None = None) -> FlowResult:
        if self._verify_task is None:
            self._verify_task = self.hass.async_create_task(self._wait_connected())
        if not self._verify_task.done():
            return self.async_show_progress(
                step_id="verify",
                progress_action="verifying",
                progress_task=self._verify_task,
            )
        try:
            ok = bool(self._verify_task.result())
        except Exception:  # noqa: BLE001
            ok = False
        self._verify_task = None
        return self.async_show_progress_done(next_step_id="verified" if ok else "failed")

    async def async_step_verified(self, user_input: dict | None = None) -> FlowResult:
        return self.async_create_entry(title="", data={})

    async def async_step_failed(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await self.async_step_start()  # try again from the top
        return self.async_show_form(
            step_id="failed",
            data_schema=vol.Schema({}),
            description_placeholders=self._placeholders(),
        )


class RebootRepairFlow(RepairsFlow):
    """Confirm, then restart the scooter to clear a stuck pending-reboot."""

    def __init__(self, data: dict | None) -> None:
        self._data = data or {}

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            call_data: dict = {}
            if self._data.get("node"):
                call_data["node"] = self._data["node"]
            await self.hass.services.async_call(
                DOMAIN, SERVICE_REBOOT, call_data, blocking=True
            )
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"node": self._data.get("node", "")},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    # Home Assistant does not persist an issue's `data` to disk (it's re-supplied by the integration
    # on each setup), so recover node/mac from the entry id embedded in the issue_id
    # (pairing_<entry_id> / reboot_<entry_id>) whenever it's missing — without a mac the entity
    # lookup can fail and the 'BLE Pair Scooter' press would be silently skipped.
    data = dict(data or {})
    if not data.get("node") and "_" in issue_id:
        entry = hass.config_entries.async_get_entry(issue_id.split("_", 1)[1])
        if entry is not None:
            data.setdefault("node", entry.data.get(CONF_NODE))
            data.setdefault("mac", entry.data.get(CONF_MAC))
    if issue_id.startswith("reboot_"):
        return RebootRepairFlow(data)
    return PairingRepairFlow(data)
