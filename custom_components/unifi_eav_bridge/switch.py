"""Enable switches: a receiver's output, and a source's whole route (control only)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONTROL_OFF, DOMAIN, ROLE_CLIENT, ROLE_HOST
from .entity import EavEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.control_mode == CONTROL_OFF:
        return
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = []
        for mac, bridge in coordinator.bridges.items():
            if mac in known:
                continue
            if bridge.role == ROLE_CLIENT:
                known.add(mac)
                new.append(EavReceiverOutput(coordinator, mac))
            elif bridge.role == ROLE_HOST:
                known.add(mac)
                new.append(EavSourceRoute(coordinator, mac))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class EavReceiverOutput(EavEntity, SwitchEntity):
    """Turns one receiver's video output on or off without changing its source."""

    _attr_translation_key = "output_enabled"
    _attr_icon = "mdi:television"

    def __init__(self, coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "output_enabled")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        group = data.matrix.group_of_client(self._mac) if data else None
        if group is None:
            return None  # not routed anywhere
        for c in group.get("clients", []):
            if c.get("mac", "").lower() == self._mac:
                return bool(c.get("enabled", True))
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_receiver_enabled(self._mac, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_receiver_enabled(self._mac, False)


class EavSourceRoute(EavEntity, SwitchEntity):
    """Starts or stops a source's whole route (all receivers watching it)."""

    _attr_translation_key = "route_enabled"
    _attr_icon = "mdi:video-switch"

    def __init__(self, coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "route_enabled")

    def _group(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        return data.matrix.group_of_host(self._mac) if data else None

    @property
    def is_on(self) -> bool | None:
        g = self._group()
        return None if g is None else bool(g.get("enabled"))

    async def _set(self, enabled: bool) -> None:
        g = self._group()
        if g is None:
            raise HomeAssistantError("This source has no receivers routed to it")
        await self.coordinator.async_set_group_enabled(g["id"], enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
