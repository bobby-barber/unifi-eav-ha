"""Shared base entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Bridge
from .const import DOMAIN


class EavEntity(CoordinatorEntity):
    """Base class: one entity tied to one bridge (by MAC)."""

    _attr_has_entity_name = True
    _required_role: str | None = None  # set on entities that only make sense for one role

    def __init__(self, coordinator, mac: str, key: str) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._attr_unique_id = f"{mac}_{key}"

    @property
    def bridge(self) -> Bridge | None:
        return self.coordinator.bridges.get(self._mac)

    @property
    def available(self) -> bool:
        # Stay "available" while the console is up even if the bridge is
        # offline: the Online sensor itself reports that, and automations get
        # a real False instead of "unavailable".
        b = self.bridge
        if not super().available or b is None:
            return False
        # An entity made for a source/receiver goes unavailable if the bridge changes role.
        return self._required_role is None or b.role == self._required_role

    @property
    def device_info(self) -> DeviceInfo:
        b = self.bridge
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            name=b.name if b else self._mac,
            manufacturer="Ubiquiti",
            model="EAV-Bridge",
            connections={("mac", self._mac)},
        )
