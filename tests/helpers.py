"""Shared fixtures: a small AV system and a mock console that behaves like the real one."""

from __future__ import annotations

import copy
import uuid
from typing import Any

from aiohttp import web

TX1 = "02:00:00:00:00:01"  # "Apple TV"
TX2 = "02:00:00:00:00:02"  # "Game Console" (no group yet in the default setup)
RX1 = "02:00:00:00:00:11"  # "Projector"
RX2 = "02:00:00:00:00:12"  # "Bar TV"
RX3 = "02:00:00:00:00:13"  # "Patio TV"

NAMES = {TX1: "Apple TV", TX2: "Game Console", RX1: "Projector", RX2: "Bar TV", RX3: "Patio TV"}
IPS = {TX1: "192.0.2.21", TX2: "192.0.2.22", RX1: "192.0.2.31", RX2: "192.0.2.32", RX3: "192.0.2.33"}


def client(mac: str, **over: Any) -> dict[str, Any]:
    return {
        "mac": mac, "client_type": "VIDEO", "mode": "UNICAST", "resolution": "AUTO",
        "color_format": "YCBCR444", "audio_enabled": True, "audio_mode": "AUTO",
        "enabled": True, **over,
    }


def group(gid: str, host: str, clients: list[dict[str, Any]], name: str = "", **over) -> dict:
    return {
        "id": gid, "name": name or NAMES[host], "channel": 2, "type": "VIDEO", "enabled": True,
        "host": {"mac": host, "host_type": "VIDEO", "refresh_rate": "FPS60", "enabled": True},
        "clients": clients, **over,
    }


def default_groups() -> list[dict[str, Any]]:
    """Apple TV feeds the projector and the bar TV (multicast); the patio TV is unrouted."""
    return [group("g-tv", TX1, [client(RX1, mode="MULTICAST"), client(RX2, mode="MULTICAST")])]


def matrix_json(groups: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "bridges": [TX1, TX2, RX1, RX2, RX3],
        "hosts": [TX1, TX2],
        "clients": [RX1, RX2, RX3],
        "groups": groups,
        "host_candidates": [], "client_candidates": [],
    }


def devices_json(source_connected: bool = False, waiting: bool = True) -> list[dict[str, Any]]:
    def host(mac, connected):
        return {
            "name": NAMES[mac], "mac": mac, "state": 1, "ip": IPS[mac], "uptime": 100,
            "hdmi_ports": [
                {"port_index": "1", "type": "in", "up": True, "connected": connected,
                 "resolution": "auto", "refresh_rate": "30", "color_format": "rgb"},
                {"port_index": "2", "type": "out", "up": False, "connected": False,
                 "resolution": "auto", "refresh_rate": "30", "color_format": "rgb"},
            ],
            "video_info": {"channel": 2, "enabled": True, "role": "host"},
        }

    def recv(mac, is_waiting):
        return {
            "name": NAMES[mac], "mac": mac, "state": 1, "ip": IPS[mac], "uptime": 100,
            "hdmi_ports": [
                {"port_index": "1", "type": "in", "up": False, "connected": False,
                 "resolution": "auto", "refresh_rate": "30", "color_format": "rgb"},
                {"port_index": "2", "type": "out", "up": False, "connected": False,
                 "resolution": "1080p", "refresh_rate": "60", "color_format": "rgb",
                 "state": "WAITING_HOST_MODE" if is_waiting else "STREAMING"},
            ],
            "video_info": {"role": "client", "channel": 2, "mode": "unicast"},
        }

    return [
        {"mac": "aa:bb:cc:dd:ee:ff", "name": "Some Switch", "state": 1},  # not a bridge
        host(TX1, source_connected), host(TX2, False),
        recv(RX1, waiting), recv(RX2, waiting), recv(RX3, True),
    ]


class MockConsole:
    """Behaves like the console where it matters for this client.

    * login sets a TOKEN cookie and returns X-CSRF-Token;
    * every call needs the cookie sent explicitly in the Cookie header;
    * writes need the CSRF header (403 otherwise);
    * a receiver may be in only one group, a source in only one group (400 otherwise);
    * snapshots return a tiny JPEG.
    """

    JPEG = b"\xff\xd8\xff\xe0MOCKJPEG\xff\xd9"

    def __init__(self) -> None:
        self.groups = default_groups()
        self.devices = devices_json()
        self.logins = 0
        self.token = "TOKEN-0"
        self.csrf = "csrf-abc"
        self.writes: list[tuple[str, str, Any]] = []  # (method, path, body)
        self.fail_next_write: int | None = None
        self.fail_reads = False  # make stat/device and matrix return 500
        self.preview_ok = True  # bridge preview port answers
        self.preview_hits = 0
        self.snapshot_hits = 0
        app = web.Application()
        app.router.add_post("/api/auth/login", self.login)
        base = "/proxy/network"
        app.router.add_get(f"{base}/api/s/{{site}}/stat/device", self.stat_device)
        app.router.add_get(f"{base}/v2/api/site/{{site}}/proav/video/matrix", self.matrix)
        app.router.add_post(f"{base}/v2/api/site/{{site}}/proav/video/group", self.create)
        app.router.add_put(f"{base}/v2/api/site/{{site}}/proav/video/group/{{gid}}", self.update)
        app.router.add_delete(f"{base}/v2/api/site/{{site}}/proav/video/group/{{gid}}", self.delete)
        app.router.add_get(f"{base}/v2/api/site/{{site}}/proav/snapshot/{{mac}}", self.snapshot)
        app.router.add_get("/", self.preview)  # what a bridge itself serves on its preview port
        self.app = app

    # -- helpers
    def _authed(self, req) -> bool:
        cookie = req.headers.get("Cookie", "")
        return f"TOKEN={self.token}" in cookie

    def _unauth(self):
        return web.json_response({"meta": {"rc": "error"}, "data": []}, status=401)

    def _check_write(self, req):
        if not self._authed(req):
            return self._unauth()
        if req.headers.get("X-CSRF-Token") != self.csrf:
            return web.json_response({"error": "csrf"}, status=403)
        if self.fail_next_write:
            status, self.fail_next_write = self.fail_next_write, None
            return web.json_response({"error": "forced"}, status=status)
        return None

    def _validate(self, groups: list[dict[str, Any]]) -> str | None:
        seen_rx, seen_tx = set(), set()
        for g in groups:
            h = g["host"]["mac"]
            if h in seen_tx:
                return "source in two groups"
            seen_tx.add(h)
            for c in g["clients"]:
                if c["mac"] in seen_rx:
                    return "receiver in two groups"
                seen_rx.add(c["mac"])
        return None

    # -- handlers
    async def login(self, req):
        body = await req.json()
        if body.get("password") != "good-password":
            return web.json_response({"error": {"code": 401}}, status=401)
        self.logins += 1
        self.token = f"TOKEN-{self.logins}"
        resp = web.json_response({"ok": True})
        resp.set_cookie("TOKEN", self.token)
        resp.headers["X-CSRF-Token"] = self.csrf
        return resp

    async def stat_device(self, req):
        if not self._authed(req):
            return self._unauth()
        if self.fail_reads:
            return web.json_response({"error": "boom"}, status=500)
        return web.json_response({"meta": {"rc": "ok"}, "data": self.devices})

    async def matrix(self, req):
        if not self._authed(req):
            return self._unauth()
        if self.fail_reads:
            return web.json_response({"error": "boom"}, status=500)
        return web.json_response(matrix_json(self.groups))

    async def create(self, req):
        if (bad := self._check_write(req)) is not None:
            return bad
        body = await req.json()
        self.writes.append(("POST", "group", copy.deepcopy(body)))
        new = {**body, "id": str(uuid.uuid4()), "channel": 10 + len(self.groups), "enabled": True}
        err = self._validate([*self.groups, new])
        if err:
            return web.json_response({"error": err}, status=400)
        self.groups.append(new)
        return web.json_response(new, status=201)

    async def update(self, req):
        if (bad := self._check_write(req)) is not None:
            return bad
        gid, body = req.match_info["gid"], await req.json()
        self.writes.append(("PUT", gid, copy.deepcopy(body)))
        idx = next((i for i, g in enumerate(self.groups) if g["id"] == gid), None)
        if idx is None:
            return web.json_response({"error": "not found"}, status=404)
        candidate = [*self.groups[:idx], body, *self.groups[idx + 1:]]
        err = self._validate(candidate)
        if err:
            return web.json_response({"error": err}, status=400)
        self.groups = candidate
        return web.json_response(body)

    async def delete(self, req):
        if (bad := self._check_write(req)) is not None:
            return bad
        gid = req.match_info["gid"]
        self.writes.append(("DELETE", gid, None))
        before = len(self.groups)
        self.groups = [g for g in self.groups if g["id"] != gid]
        if len(self.groups) == before:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({})

    async def snapshot(self, req):
        if not self._authed(req):
            return self._unauth()
        self.snapshot_hits += 1
        return web.Response(body=self.JPEG, content_type="image/jpeg")

    BRIDGE_JPEG = b"\xff\xd8\xff\xe0BRIDGEJPEG\xff\xd9"

    async def preview(self, req):
        """Stands in for a bridge's MJPG-Streamer (no login)."""
        if not self.preview_ok:
            return web.Response(status=500, text="down")
        action = req.query.get("action")
        if action == "snapshot":
            self.preview_hits += 1
            return web.Response(body=self.BRIDGE_JPEG, content_type="image/jpeg")
        if action == "stream":
            resp = web.StreamResponse(headers={
                "Content-Type": "multipart/x-mixed-replace;boundary=boundarydonotcross"})
            await resp.prepare(req)
            for _ in range(2):
                await resp.write(b"--boundarydonotcross\r\nContent-Type: image/jpeg\r\n\r\n"
                                 + self.BRIDGE_JPEG + b"\r\n")
            return resp
        return web.Response(status=400, text="400")
