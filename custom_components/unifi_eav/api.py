"""Minimal async client for a UniFi OS console's Network app (read-only).

Only what this integration needs: log in with a local account and read
``stat/device`` for the EAV bridges. It deliberately has no Home Assistant
imports so it can be tested on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import ROLE_CLIENT, ROLE_HOST, STATE_WAITING_FOR_SOURCE

_LOGGER = logging.getLogger(__name__)


class EavApiError(Exception):
    """Base error."""


class EavAuthError(EavApiError):
    """Login failed (bad credentials, or the account can't use the console locally)."""


class EavConnectionError(EavApiError):
    """Console unreachable or returned something unexpected."""


@dataclass
class HdmiPort:
    """One HDMI port as reported by the bridge."""

    index: str
    direction: str  # "in" or "out"
    up: bool
    connected: bool
    resolution: str | None
    refresh_rate: str | None
    color_format: str | None
    state: str | None  # e.g. WAITING_HOST_MODE on a receiver's output


@dataclass
class Bridge:
    """A single EAV-Bridge."""

    mac: str
    name: str
    role: str | None  # "host" (source) or "client" (display)
    channel: int | None
    online: bool
    ports: list[HdmiPort] = field(default_factory=list)
    uptime: int | None = None
    raw_video_info: dict[str, Any] = field(default_factory=dict)

    def port(self, direction: str) -> HdmiPort | None:
        for p in self.ports:
            if p.direction == direction:
                return p
        return None

    @property
    def source_signal(self) -> bool | None:
        """True if a source device is feeding this bridge's HDMI input."""
        p = self.port("in")
        return None if p is None else p.connected

    @property
    def waiting_for_source(self) -> bool | None:
        """True if this receiver is showing "Waiting for video input"."""
        p = self.port("out")
        if p is None or p.state is None:
            return None
        return p.state == STATE_WAITING_FOR_SOURCE


def _port_from_json(raw: dict[str, Any]) -> HdmiPort:
    return HdmiPort(
        index=str(raw.get("port_index", "")),
        direction=str(raw.get("type", "")),
        up=bool(raw.get("up", False)),
        connected=bool(raw.get("connected", False)),
        resolution=raw.get("resolution"),
        refresh_rate=None if raw.get("refresh_rate") is None else str(raw["refresh_rate"]),
        color_format=raw.get("color_format"),
        state=raw.get("state"),
    )


def parse_bridges(devices: list[dict[str, Any]]) -> dict[str, Bridge]:
    """Turn the console's device list into Bridge objects keyed by MAC.

    A device counts as an EAV bridge if it reports ``hdmi_ports``; this avoids
    depending on the exact model string, which differs between firmware.
    """
    bridges: dict[str, Bridge] = {}
    for dev in devices:
        ports = dev.get("hdmi_ports")
        mac = dev.get("mac")
        if not ports or not mac:
            continue
        video_info = dev.get("video_info") or {}
        role = video_info.get("role")
        if role not in (ROLE_HOST, ROLE_CLIENT):
            role = None
        bridges[mac.lower()] = Bridge(
            mac=mac.lower(),
            name=dev.get("name") or dev.get("hostname") or mac,
            role=role,
            channel=video_info.get("channel"),
            online=dev.get("state") == 1,
            ports=[_port_from_json(p) for p in ports],
            uptime=dev.get("uptime"),
            raw_video_info=video_info,
        )
    return bridges


class EavConsoleClient:
    """Talks to the console. Owns login state; pass in a shared aiohttp session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        site: str = "default",
        verify_ssl: bool = False,
    ) -> None:
        self._session = session
        self._base = f"https://{host}"
        self._username = username
        self._password = password
        self._site = site
        self._ssl = None if verify_ssl else False
        self._csrf: str | None = None
        self._logged_in = False

    async def async_login(self) -> None:
        """Log in. The console sets a TOKEN cookie; it also returns a CSRF token."""
        try:
            async with self._session.post(
                f"{self._base}/api/auth/login",
                json={
                    "username": self._username,
                    "password": self._password,
                    "rememberMe": False,
                },
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (400, 401, 403):
                    raise EavAuthError(f"login rejected ({resp.status})")
                if resp.status != 200:
                    raise EavConnectionError(f"login returned HTTP {resp.status}")
                self._csrf = resp.headers.get("X-CSRF-Token") or resp.headers.get(
                    "X-Updated-CSRF-Token"
                )
        except aiohttp.ClientError as err:
            raise EavConnectionError(str(err)) from err
        self._logged_in = True

    async def _get_json(self, path: str) -> Any:
        headers = {"X-CSRF-Token": self._csrf} if self._csrf else {}
        try:
            async with self._session.get(
                f"{self._base}{path}",
                headers=headers,
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    self._logged_in = False
                    raise EavAuthError("session expired")
                if resp.status != 200:
                    raise EavConnectionError(f"{path} returned HTTP {resp.status}")
                new_csrf = resp.headers.get("X-Updated-CSRF-Token")
                if new_csrf:
                    self._csrf = new_csrf
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise EavConnectionError(str(err)) from err

    async def async_get_bridges(self) -> dict[str, Bridge]:
        """Return all EAV bridges, logging in again once if the session expired."""
        if not self._logged_in:
            await self.async_login()
        path = f"/proxy/network/api/s/{self._site}/stat/device"
        try:
            payload = await self._get_json(path)
        except EavAuthError:
            _LOGGER.debug("Session expired, logging in again")
            await self.async_login()
            payload = await self._get_json(path)
        devices = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(devices, list):
            raise EavConnectionError("unexpected response shape from stat/device")
        return parse_bridges(devices)
