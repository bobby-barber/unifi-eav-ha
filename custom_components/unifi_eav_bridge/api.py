"""Async client for a UniFi OS console's Network app: EAV bridge status and routing.

No Home Assistant imports, so it can be tested on its own.

What the console exposes (all discovered by observation; none of it is documented):

* ``GET  /proxy/network/api/s/<site>/stat/device``      bridge status (hdmi_ports, ...)
* ``GET  /proxy/network/v2/api/site/<site>/proav/video/matrix``   routing groups
* ``PUT  .../proav/video/group/<id>``                    update a group (full object)
* ``POST .../proav/video/group``                         create a group
* ``DELETE .../proav/video/group/<id>``                  delete a group
* ``GET  .../proav/snapshot/<mac>``                      JPEG of a bridge's input

A "group" is one source (host) plus the receivers (clients) watching it. A receiver
can be in only one group at a time.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import ROLE_CLIENT, ROLE_HOST, STATE_CONNECTED, STATE_WAITING_FOR_SOURCE

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class EavApiError(Exception):
    """Base error."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class EavAuthError(EavApiError):
    """Login failed (bad credentials, or the account can't use the console locally)."""


class EavPermissionError(EavApiError):
    """The console refused a change: the account can't manage the Network app."""


class EavConnectionError(EavApiError):
    """Console unreachable or returned something unexpected."""


# --------------------------------------------------------------------------- models


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
    ip: str | None = None
    ports: list[HdmiPort] = field(default_factory=list)
    uptime: int | None = None
    raw_video_info: dict[str, Any] = field(default_factory=dict)
    device_id: str | None = None  # the console's own id for the device (needed to change its role)
    configured_role: str | None = None  # "eav_bridge_role": what the console is set to
    state: int | None = None  # console device state; 1 = connected, other = busy/offline

    @property
    def switching(self) -> bool:
        """True while the bridge is changing role (or otherwise not idle and connected).

        A role change reboots the bridge (several minutes, with repeated busy states);
        the console shows the new role at once but the bridge reports it only when done.
        """
        live = self.raw_video_info.get("role")
        if self.state is not None and self.state != STATE_CONNECTED:
            return True
        return self.configured_role is not None and live is not None and live != self.configured_role

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


@dataclass
class Matrix:
    """The video routing table."""

    groups: list[dict[str, Any]] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)  # MACs that act as sources
    clients: list[str] = field(default_factory=list)  # MACs that act as receivers

    def group_of_client(self, mac: str) -> dict[str, Any] | None:
        for g in self.groups:
            if any(c.get("mac", "").lower() == mac for c in g.get("clients", [])):
                return g
        return None

    def group_of_host(self, mac: str) -> dict[str, Any] | None:
        for g in self.groups:
            if (g.get("host") or {}).get("mac", "").lower() == mac:
                return g
        return None


@dataclass
class ConsoleData:
    """Everything one poll returns."""

    bridges: dict[str, Bridge]
    matrix: Matrix


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
        configured = dev.get("eav_bridge_role")
        if configured not in (ROLE_HOST, ROLE_CLIENT):
            configured = None
        role = video_info.get("role")
        if role not in (ROLE_HOST, ROLE_CLIENT):
            role = configured  # mid-switch the bridge may not report video_info
        bridges[mac.lower()] = Bridge(
            mac=mac.lower(),
            name=dev.get("name") or dev.get("hostname") or mac,
            role=role,
            channel=video_info.get("channel"),
            online=dev.get("state") == 1,
            ip=dev.get("ip"),
            ports=[_port_from_json(p) for p in ports],
            uptime=dev.get("uptime"),
            raw_video_info=video_info,
            device_id=dev.get("_id"),
            configured_role=configured,
            state=dev.get("state"),
        )
    return bridges


def parse_matrix(raw: Any) -> Matrix:
    if not isinstance(raw, dict):
        raise EavConnectionError("unexpected response shape from proav/video/matrix")
    return Matrix(
        groups=[g for g in raw.get("groups", []) if isinstance(g, dict)],
        hosts=[m.lower() for m in raw.get("hosts", [])],
        clients=[m.lower() for m in raw.get("clients", [])],
    )


# --------------------------------------------------------------------------- routing


@dataclass
class Op:
    """One HTTP write the console needs, in order."""

    method: str  # POST | PUT | DELETE
    group_id: str | None  # None for POST
    body: dict[str, Any] | None
    note: str = ""

    def describe(self) -> str:
        tail = f"/{self.group_id}" if self.group_id else ""
        return f"{self.method} proav/video/group{tail}  ({self.note})"


# Settings used when a receiver joins a group and we have nothing better to copy.
# Values are the ones observed on a real EAV-Bridge setup.
DEFAULT_CLIENT = {
    "client_type": "VIDEO",
    "mode": "UNICAST",
    "resolution": "AUTO",
    "color_format": "YCBCR444",
    "audio_enabled": True,
    "audio_mode": "AUTO",
    "enabled": True,
}
DEFAULT_HOST = {"host_type": "VIDEO", "refresh_rate": "FPS60", "enabled": True}


def _with_modes(clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A group with 2+ receivers streams multicast; a single receiver is unicast."""
    mode = "MULTICAST" if len(clients) >= 2 else "UNICAST"
    return [{**c, "mode": mode} for c in clients]


def plan_route(
    matrix: Matrix, rx_mac: str, tx_mac: str | None, tx_name: str = "Source"
) -> list[Op]:
    """Work out the writes that make receiver ``rx_mac`` watch source ``tx_mac``.

    ``tx_mac=None`` disconnects the receiver. Pure function: nothing is sent.
    The receiver leaves its current group first (the console allows one group per
    receiver), then joins, or creates, the source's group. Returns [] if already so.
    """
    rx_mac = rx_mac.lower()
    tx_mac = tx_mac.lower() if tx_mac else None

    current = matrix.group_of_client(rx_mac)
    if current is not None and (current.get("host") or {}).get("mac", "").lower() == tx_mac:
        return []
    if current is None and tx_mac is None:
        return []

    ops: list[Op] = []
    template: dict[str, Any] | None = None

    if current is not None:
        remaining = [c for c in current["clients"] if c.get("mac", "").lower() != rx_mac]
        template = next(
            (c for c in current["clients"] if c.get("mac", "").lower() == rx_mac), None
        )
        if remaining:
            updated = {**copy.deepcopy(current), "clients": _with_modes(copy.deepcopy(remaining))}
            ops.append(Op("PUT", current["id"], updated, "receiver leaves its old source"))
        else:
            ops.append(Op("DELETE", current["id"], None, "last receiver left; remove empty group"))

    if tx_mac is not None:
        new_client = {**DEFAULT_CLIENT, **(copy.deepcopy(template) if template else {})}
        new_client["mac"] = rx_mac
        target = matrix.group_of_host(tx_mac)
        if target is not None:
            clients = copy.deepcopy(target["clients"]) + [new_client]
            updated = {**copy.deepcopy(target), "clients": _with_modes(clients)}
            ops.append(Op("PUT", target["id"], updated, "receiver joins the source's group"))
        else:
            body = {
                "name": tx_name,
                "type": "VIDEO",
                "host": {**DEFAULT_HOST, "mac": tx_mac},
                "clients": _with_modes([new_client]),
            }
            ops.append(Op("POST", None, body, "no group for this source yet; create one"))
    return ops


def plan_group_enabled(matrix: Matrix, group_id: str, enabled: bool) -> list[Op]:
    """Turn a whole route (one source's group) on or off."""
    for g in matrix.groups:
        if g.get("id") == group_id:
            if bool(g.get("enabled")) == enabled:
                return []
            return [Op("PUT", group_id, {**copy.deepcopy(g), "enabled": enabled},
                       "enable route" if enabled else "disable route")]
    raise EavApiError(f"unknown group {group_id}")


def plan_client_enabled(matrix: Matrix, rx_mac: str, enabled: bool) -> list[Op]:
    """Turn one receiver's output on or off inside its group."""
    rx_mac = rx_mac.lower()
    g = matrix.group_of_client(rx_mac)
    if g is None:
        raise EavApiError("receiver is not routed to any source")
    clients = copy.deepcopy(g["clients"])
    for c in clients:
        if c.get("mac", "").lower() == rx_mac:
            if bool(c.get("enabled", True)) == enabled:
                return []
            c["enabled"] = enabled
    return [Op("PUT", g["id"], {**copy.deepcopy(g), "clients": clients},
               "enable receiver" if enabled else "disable receiver")]


AUDIO_MODES = ("AUTO", "PCM")


def plan_client_audio_mode(matrix: Matrix, rx_mac: str, mode: str) -> list[Op]:
    """Set one receiver's audio mode (``AUTO`` or ``PCM``) inside its group.

    The setting lives on the receiver's entry in its source's group, so the receiver must
    be routed. Verified on hardware 2026-11-11 (VDS-04): the console accepts both values and
    the receiver keeps its route. PCM is not a stereo downmix (see docs/gotchas.md).
    """
    rx_mac = rx_mac.lower()
    if mode not in AUDIO_MODES:
        raise EavApiError(f"unknown audio mode {mode!r}")
    g = matrix.group_of_client(rx_mac)
    if g is None:
        raise EavApiError("receiver is not routed to any source; route it first")
    clients = copy.deepcopy(g["clients"])
    for c in clients:
        if c.get("mac", "").lower() == rx_mac:
            if c.get("audio_mode", "AUTO") == mode:
                return []
            c["audio_mode"] = mode
    return [Op("PUT", g["id"], {**copy.deepcopy(g), "clients": clients},
               f"receiver audio mode -> {mode}")]


# --------------------------------------------------------------------------- role


@dataclass
class RoleOp:
    """Change one bridge between source (host) and receiver (client)."""

    device_id: str
    mac: str
    role: str
    note: str = ""

    @property
    def body(self) -> dict[str, Any]:
        return {"eav_bridge_role": self.role}

    def describe(self) -> str:
        return f"PUT rest/device/{self.device_id}  ({self.note})"


def plan_role(bridges: dict[str, Bridge], mac: str, role: str) -> list[RoleOp]:
    """Work out the write that makes bridge ``mac`` a source or a receiver.

    Pure function. Refuses while ANY bridge is still mid-change: on real hardware, changing
    one bridge made the console reassign its paired bridge and routes too, so changes must
    not overlap. Returns [] if the bridge already has that role.
    """
    mac = mac.lower()
    if role not in (ROLE_HOST, ROLE_CLIENT):
        raise EavApiError(f"unknown role {role!r}")
    bridge = bridges.get(mac)
    if bridge is None:
        raise EavApiError(f"unknown bridge {mac}")
    if not bridge.device_id:
        raise EavApiError("the console did not report an id for this bridge")
    busy = [b.name for b in bridges.values() if b.switching or not b.online]
    if busy:
        raise EavApiError(
            "wait for these bridges to finish starting up or changing role: " + ", ".join(busy)
        )
    if (bridge.configured_role or bridge.role) == role:
        return []
    return [RoleOp(bridge.device_id, mac, role,
                   f"{bridge.name}: {bridge.configured_role or bridge.role} -> {role}; "
                   "reboots the bridge, takes several minutes")]


# --------------------------------------------------------------------------- client


class EavConsoleClient:
    """Talks to the console. Owns login state; pass in a shared aiohttp session.

    Cookies are forwarded by hand: Home Assistant's HTTP sessions don't keep
    cookies for consoles addressed by bare IP, which would break every call after
    login.
    """

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
        self._base = host.rstrip("/") if "://" in host else f"https://{host}"
        self._username = username
        self._password = password
        self._site = site
        self._ssl = None if verify_ssl else False
        self._cookies: dict[str, str] = {}
        self._csrf: str | None = None

    # ---- auth ---------------------------------------------------------------

    @property
    def logged_in(self) -> bool:
        return bool(self._cookies)

    async def async_login(self) -> None:
        """Log in. The console sets a TOKEN cookie and returns a CSRF token."""
        self._cookies, self._csrf = {}, None
        try:
            async with self._session.post(
                f"{self._base}/api/auth/login",
                json={"username": self._username, "password": self._password,
                      "rememberMe": False},
                ssl=self._ssl,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status in (400, 401, 403):
                    raise EavAuthError(f"login rejected ({resp.status})", resp.status)
                if resp.status != 200:
                    raise EavConnectionError(f"login returned HTTP {resp.status}", resp.status)
                self._cookies = {k: m.value for k, m in resp.cookies.items()}
                self._csrf = resp.headers.get("X-CSRF-Token") or resp.headers.get(
                    "X-Updated-CSRF-Token"
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EavConnectionError(f"cannot reach console: {err}") from err
        if not self._cookies:
            raise EavAuthError("login returned no session cookie")

    # ---- plumbing -------------------------------------------------------------

    def _site_api(self, path: str) -> str:
        return f"{self._base}/proxy/network/api/s/{self._site}/{path}"

    def _proav(self, path: str) -> str:
        return f"{self._base}/proxy/network/v2/api/site/{self._site}/proav/{path}"

    async def _request(
        self, method: str, url: str, *, body: Any = None, raw: bool = False, retry: bool = True
    ) -> Any:
        """Authenticated request; re-logs in once if the session has expired."""
        if not self._cookies:
            await self.async_login()
        headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in self._cookies.items())}
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        try:
            async with self._session.request(
                method, url, json=body, headers=headers, ssl=self._ssl,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if updated := resp.headers.get("X-Updated-CSRF-Token"):
                    self._csrf = updated
                if resp.status == 401:
                    self._cookies = {}
                    if not retry:
                        raise EavAuthError("session rejected after re-login", 401)
                    _LOGGER.debug("session expired, logging in again")
                    return await self._request(method, url, body=body, raw=raw, retry=False)
                if resp.status == 403 and method != "GET":
                    raise EavPermissionError(
                        "the console refused the change (HTTP 403). This account can read "
                        "status but is not allowed to change routes: give it a role that can "
                        "manage the Network app in UniFi OS (Admins & Users)",
                        403,
                    )
                if resp.status >= 400:
                    text = (await resp.text())[:200]
                    raise EavConnectionError(f"{method} {url} -> HTTP {resp.status}: {text}",
                                             resp.status)
                if raw:
                    return await resp.read()
                text = await resp.text()
                if not text:
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EavConnectionError(f"{method} {url} failed: {err}") from err

    # ---- reads ----------------------------------------------------------------

    async def async_get_bridges(self) -> dict[str, Bridge]:
        payload = await self._request("GET", self._site_api("stat/device"))
        devices = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(devices, list):
            raise EavConnectionError("unexpected response shape from stat/device")
        return parse_bridges(devices)

    async def async_get_matrix(self) -> Matrix:
        return parse_matrix(await self._request("GET", self._proav("video/matrix")))

    async def async_get_data(self) -> ConsoleData:
        """Bridge status and routing in one go (two requests)."""
        return ConsoleData(await self.async_get_bridges(), await self.async_get_matrix())

    async def async_get_snapshot(self, mac: str) -> bytes:
        """JPEG the console keeps for a bridge (fallback when the bridge itself is unreachable)."""
        return await self._request("GET", self._proav(f"snapshot/{mac}"), raw=True)

    # ---- writes ---------------------------------------------------------------

    async def async_set_role(self, op: RoleOp) -> None:
        """Change a bridge's role. The bridge then reboots; this takes several minutes."""
        result = await self._request(
            "PUT", self._site_api(f"rest/device/{op.device_id}"), body=op.body
        )
        rc = ((result or {}).get("meta") or {}).get("rc") if isinstance(result, dict) else None
        if rc != "ok":
            raise EavApiError(f"console did not accept the role change: {result!r}"[:200])

    async def async_apply(self, ops: list[Op]) -> None:
        """Send planned writes in order. Stops at the first failure."""
        for op in ops:
            if op.method == "POST":
                await self._request("POST", self._proav("video/group"), body=op.body)
            elif op.method == "PUT":
                await self._request("PUT", self._proav(f"video/group/{op.group_id}"), body=op.body)
            elif op.method == "DELETE":
                await self._request("DELETE", self._proav(f"video/group/{op.group_id}"))
            else:  # pragma: no cover - planner never produces others
                raise EavApiError(f"unsupported op {op.method}")
