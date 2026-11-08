"""Preview camera for every bridge: what its HDMI input (source) or screen (receiver) shows.

Images come straight from the bridge, which serves an unauthenticated MJPEG preview on
port 8080. If the bridge can't be reached from Home Assistant (different VLAN, firewall),
the console's own snapshot is used instead.

To stay light on these small devices, a fetched image is reused for a few seconds
(Options: "Preview refresh"), and it is thrown away at once when the bridge's signal
or routing changes, so a freshly switched source shows up right away.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from aiohttp import web

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import (
    async_aiohttp_proxy_web,
    async_get_clientsession,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import EavApiError
from .const import BRIDGE_PREVIEW_PORT, DOMAIN
from .entity import EavEntity

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_TIMEOUT = aiohttp.ClientTimeout(total=5)
_now = time.monotonic  # indirection so tests can move the clock


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = []
        for mac in coordinator.bridges:
            if mac not in known:
                known.add(mac)
                new.append(EavPreviewCamera(coordinator, mac))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class EavPreviewCamera(EavEntity, Camera):
    """Live-ish still image, and a pass-through MJPEG stream, from one bridge."""

    _attr_translation_key = "preview"
    _attr_brand = "Ubiquiti"
    _attr_model = "EAV-Bridge"

    def __init__(self, coordinator, mac: str) -> None:
        EavEntity.__init__(self, coordinator, mac, "preview")
        Camera.__init__(self)
        self._image: bytes | None = None
        self._image_time = 0.0
        self._image_source: str | None = None
        self._lock = asyncio.Lock()
        self._state_key: tuple | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._state_key = self._current_key()  # baseline, so the first real change is noticed

    # ---- freshness ----------------------------------------------------------------

    def _current_key(self) -> tuple:
        b = self.bridge
        if b is None:
            return ()
        group = self.coordinator.data.matrix.group_of_client(b.mac) if self.coordinator.data else None
        host = (group.get("host") or {}).get("mac") if group else None
        return (b.source_signal, b.waiting_for_source, host, b.online)

    @callback
    def _handle_coordinator_update(self) -> None:
        key = self._current_key()
        if self._state_key is not None and key != self._state_key:
            self._image_time = 0.0  # state changed: next request fetches a new image
        self._state_key = key
        super()._handle_coordinator_update()

    def _fresh(self) -> bool:
        return (
            self._image is not None
            and _now() - self._image_time < self.coordinator.snapshot_interval
        )

    # ---- images -------------------------------------------------------------------

    def _preview_base(self) -> str | None:
        b = self.bridge
        return f"http://{b.ip}:{BRIDGE_PREVIEW_PORT}" if b and b.ip else None

    async def _fetch_from_bridge(self) -> bytes | None:
        base = self._preview_base()
        if base is None:
            return None
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                f"{base}/?action=snapshot", timeout=SNAPSHOT_TIMEOUT
            ) as resp:
                if resp.status == 200 and resp.content_type.startswith("image/"):
                    return await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Preview from bridge %s failed: %s", self._mac, err)
        return None

    async def _fetch_from_console(self) -> bytes | None:
        try:
            return await self.coordinator.client.async_get_snapshot(self._mac)
        except EavApiError as err:
            _LOGGER.debug("Console snapshot for %s failed: %s", self._mac, err)
            return None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        if self._fresh():
            return self._image
        async with self._lock:  # many viewers, one fetch
            if self._fresh():
                return self._image
            for source, fetch in (("bridge", self._fetch_from_bridge),
                                  ("console", self._fetch_from_console)):
                image = await fetch()
                if image:
                    changed = source != self._image_source
                    self._image, self._image_time, self._image_source = (
                        image, _now(), source)
                    if changed and self.hass is not None:
                        self.async_write_ha_state()  # keep the image_source attribute honest
                    break
            return self._image  # last good image if both failed (may be None)

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        """Pass the bridge's own MJPEG stream through; else fall back to repeated stills."""
        base = self._preview_base()
        if base is None:
            return await super().handle_async_mjpeg_stream(request)
        session = async_get_clientsession(self.hass)
        stream = session.get(
            f"{base}/?action=stream",
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=5),
        )
        return await async_aiohttp_proxy_web(self.hass, request, stream)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "image_source": self._image_source,
            "preview_url": self._preview_base(),
            "refresh_seconds": self.coordinator.snapshot_interval,
        }
