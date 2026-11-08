"""Tests for the console client, run against a mock console (no Home Assistant needed).

    python tests/test_api.py

Uses the real shape of data captured from a UniFi Cloud Key Gen2 Plus with two
EAV bridges: an Apple TV source and a projector receiver waiting for a source.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer
import aiohttp

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "unifi_eav"


def load_api():
    """Import api.py/const.py without importing the package __init__ (needs HA)."""
    pkg = types.ModuleType("unifi_eav_pkg")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["unifi_eav_pkg"] = pkg
    for name in ("const", "api"):
        spec = importlib.util.spec_from_file_location(
            f"unifi_eav_pkg.{name}", PKG_DIR / f"{name}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"unifi_eav_pkg.{name}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["unifi_eav_pkg.api"]


api = load_api()

TV_MAC = "02:00:00:00:00:01"
PROJ_MAC = "02:00:00:00:00:02"


def devices(source_connected: bool, waiting: bool):
    return [
        {  # an unrelated device (no hdmi_ports) must be ignored
            "mac": "aa:bb:cc:dd:ee:ff",
            "name": "Some Switch",
            "state": 1,
        },
        {
            "name": "VDS-01 Apple TV",
            "mac": TV_MAC,
            "state": 1,
            "uptime": 13621,
            "hdmi_ports": [
                {"port_index": "1", "type": "in", "up": True,
                 "connected": source_connected, "resolution": "auto",
                 "refresh_rate": "30", "color_format": "rgb"},
                {"port_index": "2", "type": "out", "up": False, "connected": False,
                 "resolution": "auto", "refresh_rate": "30", "color_format": "rgb"},
            ],
            "video_info": {"channel": 2, "enabled": True, "role": "host",
                           "refresh_rate": "60"},
        },
        {
            "name": "VDS-02 Projector",
            "mac": PROJ_MAC,
            "state": 1,
            "uptime": 13686,
            "hdmi_ports": [
                {"port_index": "1", "type": "in", "up": False, "connected": False,
                 "resolution": "auto", "refresh_rate": "30", "color_format": "rgb"},
                {"port_index": "2", "type": "out", "up": False, "connected": False,
                 "resolution": "1080p", "refresh_rate": "60", "color_format": "rgb",
                 **({"state": "WAITING_HOST_MODE"} if waiting else {"state": "STREAMING"})},
            ],
            "video_info": {"role": "client", "channel": 2, "mode": "unicast"},
        },
    ]


class MockConsole:
    def __init__(self):
        self.source_connected = False
        self.waiting = True
        self.logins = 0
        self.valid_token = "TOKEN-1"
        self.csrf = "csrf-abc"
        app = web.Application()
        app.router.add_post("/api/auth/login", self.login)
        app.router.add_get("/proxy/network/api/s/{site}/stat/device", self.stat_device)
        self.app = app

    async def login(self, req):
        body = await req.json()
        if body.get("password") != "good-password":
            return web.json_response({"error": {"code": 401}}, status=401)
        self.logins += 1
        self.valid_token = f"TOKEN-{self.logins}"
        resp = web.json_response({"ok": True})
        resp.set_cookie("TOKEN", self.valid_token)
        resp.headers["X-CSRF-Token"] = self.csrf
        return resp

    async def stat_device(self, req):
        if req.cookies.get("TOKEN") != self.valid_token:
            return web.json_response({"meta": {"rc": "error"}, "data": []}, status=401)
        if req.headers.get("X-CSRF-Token") != self.csrf:
            return web.json_response({"meta": {"rc": "error"}, "data": []}, status=403)
        return web.json_response(
            {"meta": {"rc": "ok"}, "data": devices(self.source_connected, self.waiting)}
        )


async def main() -> int:
    failures = 0

    def check(label, cond):
        nonlocal failures
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            failures += 1

    console = MockConsole()
    server = TestServer(console.app)
    await server.start_server()
    host = f"{server.host}:{server.port}"

    # The client always uses https; point it at the plain-HTTP mock.
    class HttpClient(api.EavConsoleClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._base = f"http://{host}"

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        client = HttpClient(s, host, "ha", "good-password")

        bridges = await client.async_get_bridges()
        check("only the two EAV bridges are returned", set(bridges) == {TV_MAC, PROJ_MAC})
        tv, proj = bridges[TV_MAC], bridges[PROJ_MAC]
        check("Apple TV bridge is the host", tv.role == "host")
        check("projector bridge is the client", proj.role == "client")
        check("source signal is off when Apple TV is off", tv.source_signal is False)
        check("projector is waiting for source", proj.waiting_for_source is True)
        check("both bridges online", tv.online and proj.online)
        check("channel recorded", tv.channel == proj.channel == 2)
        check("host has no 'waiting' state (not applicable)", tv.waiting_for_source is None)

        console.source_connected = True
        console.waiting = False
        bridges = await client.async_get_bridges()
        check("source signal on when Apple TV is on", bridges[TV_MAC].source_signal is True)
        check("projector no longer waiting", bridges[PROJ_MAC].waiting_for_source is False)

        # Session expiry: the console forgets our token; client must re-login once.
        logins_before = console.logins
        console.valid_token = "something-else"
        bridges = await client.async_get_bridges()
        check("recovers from an expired session", set(bridges) == {TV_MAC, PROJ_MAC})
        check("re-logged in exactly once", console.logins == logins_before + 1)

    # Bad password
    async with aiohttp.ClientSession() as s:
        bad = HttpClient(s, host, "ha", "wrong")
        try:
            await bad.async_get_bridges()
            check("bad password raises EavAuthError", False)
        except api.EavAuthError:
            check("bad password raises EavAuthError", True)

    # Unreachable console
    async with aiohttp.ClientSession() as s:
        dead = api.EavConsoleClient(s, "127.0.0.1:9", "ha", "x")
        dead._base = "http://127.0.0.1:9"
        try:
            await dead.async_get_bridges()
            check("unreachable console raises EavConnectionError", False)
        except api.EavConnectionError:
            check("unreachable console raises EavConnectionError", True)

    await server.close()
    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
