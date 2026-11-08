"""Sensors: raw HDMI output state on receivers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ROLE_CLIENT
from .entity import EavEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new: list[EavEntity] = []
        for mac, bridge in (coordinator.data or {}).items():
            if mac in known:
                continue
            known.add(mac)
            if bridge.role == ROLE_CLIENT:
                new.append(EavOutputState(coordinator, mac))
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
