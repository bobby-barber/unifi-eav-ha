"""Binary sensors: bridge online, source signal, waiting-for-source."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ROLE_CLIENT, ROLE_HOST
from .entity import EavEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[tuple[str, str]] = set()

    @callback
    def _add_new() -> None:
        new: list[EavEntity] = []
        for mac, bridge in coordinator.bridges.items():
            if (mac, "online") not in known:
                known.add((mac, "online"))
                new.append(EavOnline(coordinator, mac))
            # Created for the role the bridge has now; kept (unavailable) if it changes role.
            if bridge.role == ROLE_HOST and (mac, "source_signal") not in known:
                known.add((mac, "source_signal"))
                new.append(EavSourceSignal(coordinator, mac))
            elif bridge.role == ROLE_CLIENT and (mac, "waiting_for_source") not in known:
                known.add((mac, "waiting_for_source"))
                new.append(EavWaitingForSource(coordinator, mac))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class EavOnline(EavEntity, BinarySensorEntity):
    """Whether the console currently considers the bridge connected."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "online"

    def __init__(self, coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "online")

    @property
    def is_on(self) -> bool | None:
        b = self.bridge
        return None if b is None else b.online


class EavSourceSignal(EavEntity, BinarySensorEntity):
    """On a source bridge: is a device (Apple TV, etc.) driving its HDMI input?"""

    _attr_translation_key = "source_signal"
    _required_role = ROLE_HOST
    _attr_icon = "mdi:hdmi-port"

    def __init__(self, coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "source_signal")

    @property
    def is_on(self) -> bool | None:
        b = self.bridge
        return None if b is None else b.source_signal

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        b = self.bridge
        p = b.port("in") if b else None
        if p is None:
            return {}
        return {
            "port_up": p.up,
            "resolution_setting": p.resolution,
            "refresh_rate_setting": p.refresh_rate,
            "color_format": p.color_format,
        }


class EavWaitingForSource(EavEntity, BinarySensorEntity):
    """On a receiver: is it showing "Waiting for video input"?

    Turn the projector or TV off when this stays on for a while.
    """

    _attr_translation_key = "waiting_for_source"
    _required_role = ROLE_CLIENT
    _attr_icon = "mdi:video-input-hdmi"

    def __init__(self, coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "waiting_for_source")

    @property
    def is_on(self) -> bool | None:
        b = self.bridge
        return None if b is None else b.waiting_for_source

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        b = self.bridge
        if b is None:
            return {}
        out = b.port("out")
        attrs: dict[str, Any] = {
            "output_state": out.state if out else None,
            "output_connected": out.connected if out else None,
            "channel": b.channel,
        }
        src = self.coordinator.source_for(b)
        if src is not None:
            attrs["source_device"] = src.name
            attrs["source_signal"] = src.source_signal
        return attrs
