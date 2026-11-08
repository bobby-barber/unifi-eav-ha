"""Receiver source selector (only when control is enabled)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONTROL_OFF, DOMAIN, NO_SOURCE, ROLE_CLIENT, ROLE_HOST
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
            if mac not in known and bridge.role == ROLE_CLIENT:
                known.add(mac)
                new.append(EavReceiverSource(coordinator, mac))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class EavReceiverSource(EavEntity, SelectEntity):
    """Which source a receiver watches. Choose "Disconnected" to stop watching."""

    _attr_translation_key = "source"
    _attr_icon = "mdi:video-input-hdmi"

    def __init__(self, coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "source")

    def _label_to_mac(self) -> dict[str, str]:
        labels: dict[str, str] = {}
        for mac, b in self.coordinator.bridges.items():
            if b.role != ROLE_HOST:
                continue
            label = b.name
            if label == NO_SOURCE or label in labels:  # keep every option unique
                label = f"{label} ({mac})"
            labels[label] = mac
        return labels

    @property
    def options(self) -> list[str]:
        return [NO_SOURCE, *self._label_to_mac()]

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data
        if data is None:
            return None
        group = data.matrix.group_of_client(self._mac)
        if group is None:
            return NO_SOURCE
        host = (group.get("host") or {}).get("mac", "").lower()
        for label, mac in self._label_to_mac().items():
            if mac == host:
                return label
        return None  # routed to a source we don't know

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "control_mode": self.coordinator.control_mode,
            "last_plan": self.coordinator.last_plan,
        }

    async def async_select_option(self, option: str) -> None:
        if option == NO_SOURCE:
            await self.coordinator.async_route(self._mac, None)
            return
        mac = self._label_to_mac().get(option)
        if mac is None:
            raise ServiceValidationError(f"Unknown source: {option}")
        await self.coordinator.async_route(self._mac, mac)
