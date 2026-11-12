"""Sensors: raw HDMI output state on receivers, and CEC forwarding status per bridge."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CEC_OFF, CEC_STATES, DOMAIN, ROLE_CLIENT
from .entity import EavEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[tuple[str, str]] = set()
    cec_on = coordinator.cec is not None or coordinator.cec_problem is not None

    @callback
    def _add_new() -> None:
        new: list[EavEntity] = []
        for mac, bridge in coordinator.bridges.items():
            if (mac, "output_state") not in known and bridge.role == ROLE_CLIENT:
                known.add((mac, "output_state"))
                new.append(EavOutputState(coordinator, mac))
            if cec_on and (mac, "cec") not in known:
                known.add((mac, "cec"))
                new.append(EavCecStatus(coordinator, mac))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class EavOutputState(EavEntity, SensorEntity):
    """The receiver's own state string for its HDMI output (e.g. WAITING_HOST_MODE).

    Useful for discovering other states the firmware reports; the "waiting for
    source" binary sensor is the one to automate on.
    """

    _attr_translation_key = "output_state"
    _required_role = ROLE_CLIENT
    _attr_icon = "mdi:video-wireless"

    def __init__(self, coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "output_state")

    @property
    def native_value(self) -> str | None:
        b = self.bridge
        out = b.port("out") if b else None
        return None if out is None else out.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        b = self.bridge
        out = b.port("out") if b else None
        if out is None:
            return {}
        return {
            "connected": out.connected,
            "up": out.up,
            "resolution_setting": out.resolution,
            "refresh_rate_setting": out.refresh_rate,
            "color_format": out.color_format,
        }


class EavCecStatus(EavEntity, SensorEntity):
    """Whether HDMI-CEC forwarding is running on this bridge (only when CEC is turned on).

    linked   source with a receiver attached, or receiver linked to its source
    waiting  source ready, no receiver attached yet
    off      not in use (no route), or stopped
    pending  wanted but not set up yet (first round, bridge restarting)
    error    the last attempt failed; see the "detail" attribute
    dry_run  would be set up; control is in dry run
    """

    _attr_translation_key = "cec"
    _attr_icon = "mdi:television-shimmer"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CEC_STATES

    def __init__(self, coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "cec")

    @property
    def native_value(self) -> str | None:
        cec = self.coordinator.cec
        if cec is None:
            return CEC_OFF
        st = cec.bridges.get(self._mac)
        return st.status if st else CEC_OFF

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cec = self.coordinator.cec
        if cec is None:
            return {"problem": self.coordinator.cec_problem}
        st = cec.bridges.get(self._mac)
        return st.attributes() if st else {}
