"""HDMI-CEC forwarding between a source bridge and its receivers.

No Home Assistant imports, so it can be tested on its own. Full background: docs/cec.md.

The short version:

* EAV-Bridges run ASPEED's video-over-IP SDK, which can carry CEC (the HDMI "turn on / go to
  standby" messages) from the device on a source bridge to the TVs on its receivers.
* UniFi's firmware forces that off at every boot, and the bridge's root filesystem is a RAM
  disk, so nothing changed on a bridge survives a restart. UniFi's own on/off switch
  (``ubus call hal cec``) does not start the network part either.
* So this module logs in to each bridge over SSH and runs ``cec_bridge.sh``, which starts the
  SDK's CEC service from a temporary copy with CEC switched on. It does that when a route
  appears or changes, when a bridge restarts, and every ``CEC_RECHECK_SECONDS`` as a check.

Planning (which bridge needs what) is a pure function, ``plan_cec``. ``CecManager`` decides
when to run the script and remembers results; the SSH transport is passed in, so tests use a
fake one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
import time
from typing import Any

from .api import Bridge, Matrix
from .const import (
    CEC_DRY_RUN,
    CEC_ERROR,
    CEC_LINKED,
    CEC_OFF,
    CEC_PENDING,
    CEC_RECHECK_SECONDS,
    CEC_RETRY_SECONDS,
    CEC_SCRIPT_TIMEOUT,
    CEC_WAITING,
    ROLE_CLIENT,
    ROLE_HOST,
    SSH_PORT,
    SSH_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

MODE_HOST = "host"
MODE_CLIENT = "client"
MODE_OFF = "off"


@dataclass(frozen=True)
class CecTarget:
    """What one bridge should be doing: ``host <channel>``, ``client <source ip>`` or ``off``."""

    mode: str
    arg: str = ""

    @property
    def command(self) -> str:
        """The command run on the bridge; the script itself arrives on stdin."""
        return f"bash -s -- {self.mode} {self.arg}".rstrip()


def channel_label(channel: Any) -> str | None:
    """Route channel as the bridge's SDK writes it: 2 -> "0002". None if unusable."""
    try:
        value = int(channel)
    except (TypeError, ValueError):
        return None
    return f"{value:04d}" if 0 <= value <= 9999 else None


def _usable(bridge: Bridge | None, role: str) -> bool:
    return (
        bridge is not None
        and bridge.online
        and not bridge.switching
        and bridge.role == role
        and bool(bridge.ip)
    )


def plan_cec(bridges: dict[str, Bridge], matrix: Matrix) -> dict[str, CecTarget]:
    """Work out which bridges need CEC forwarding, from the routing table. Pure function.

    For every enabled route group: each enabled, online receiver links to the source's
    address, and the source serves the group's channel. A source with no usable receiver
    gets nothing. Bridges in no route are left out (the manager stops forwarding on bridges
    it set up earlier).
    """
    targets: dict[str, CecTarget] = {}
    for group in matrix.groups:
        if not group.get("enabled", True):
            continue
        host_mac = (group.get("host") or {}).get("mac", "").lower()
        host = bridges.get(host_mac)
        if not _usable(host, ROLE_HOST):
            continue
        channel = channel_label(group.get("channel"))
        if channel is None:
            channel = channel_label(host.channel)
        if channel is None:
            continue
        receivers = [
            c.get("mac", "").lower()
            for c in group.get("clients", [])
            if c.get("enabled", True)
        ]
        receivers = [m for m in receivers if _usable(bridges.get(m), ROLE_CLIENT)]
        if not receivers:
            continue
        targets[host_mac] = CecTarget(MODE_HOST, channel)
        for mac in receivers:
            targets[mac] = CecTarget(MODE_CLIENT, host.ip)
    return targets


def routed_macs(matrix: Matrix) -> set[str]:
    """Every bridge that is part of an enabled route, whatever its current health."""
    macs: set[str] = set()
    for group in matrix.groups:
        if not group.get("enabled", True):
            continue
        macs.add((group.get("host") or {}).get("mac", "").lower())
        macs.update(
            c.get("mac", "").lower() for c in group.get("clients", []) if c.get("enabled", True)
        )
    macs.discard("")
    return macs


@dataclass
class CecResult:
    """The script's last line: ``HA_CEC result=... mode=... state=... target=... detail=...``."""

    result: str  # ok | started | stopped | off | running | error
    state: str = "none"  # the SDK link manager's state, e.g. s_srv_on
    target: str = "none"
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.result in ("ok", "started", "stopped")


def parse_result(output: str) -> CecResult:
    """Read the summary line. Anything unexpected becomes an error result."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if not line.startswith("HA_CEC "):
            continue
        fields: dict[str, str] = {}
        for part in line[len("HA_CEC "):].split(" "):
            key, sep, value = part.partition("=")
            if sep:
                fields[key] = value
        return CecResult(
            result=fields.get("result", "error"),
            state=fields.get("state", "none"),
            target=fields.get("target", "none"),
            detail=fields.get("detail", ""),
        )
    tail = output.strip()[-200:] or "no output"
    return CecResult(result="error", detail=f"unexpected output: {tail}")


def status_from(target: CecTarget, result: CecResult) -> str:
    """Sensor value for one bridge after a run."""
    if not result.success:
        return CEC_ERROR
    if target.mode == MODE_OFF or result.result == "stopped":
        return CEC_OFF
    if result.state == "s_srv_on":
        return CEC_LINKED
    if target.mode == MODE_HOST and result.state == "s_pre_start_srv":
        return CEC_WAITING
    return CEC_PENDING


@dataclass
class BridgeCec:
    """What the manager knows about one bridge."""

    applied: CecTarget | None = None  # last target the bridge confirmed
    wanted: CecTarget | None = None
    status: str = CEC_OFF
    result: CecResult | None = None
    last_run: float | None = None  # time.time() of the last SSH run
    uptime: int | None = None  # console-reported uptime at the last look (restart detection)
    dry_run_logged: CecTarget | None = None

    def attributes(self) -> dict[str, Any]:
        return {
            "wanted": self.wanted.command.removeprefix("bash -s -- ") if self.wanted else None,
            "applied": self.applied.command.removeprefix("bash -s -- ") if self.applied else None,
            "link_state": self.result.state if self.result else None,
            "detail": self.result.detail if self.result else None,
            "last_run": (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.last_run))
                if self.last_run else None
            ),
        }


# (mac, ip, command, script) -> combined output of the command
Runner = Callable[[str, str, str, str], Awaitable[str]]


class CecManager:
    """Decides when to run the bridge script, runs it one bridge at a time, keeps results.

    One SSH login at a time, on purpose: UniFi gateways with IPS may block a host that opens
    many SSH sessions in a short time.
    """

    def __init__(
        self,
        runner: Runner,
        script: str,
        *,
        dry_run: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._runner = runner
        self._script = script
        self.dry_run = dry_run
        self._clock = clock
        self._lock = asyncio.Lock()
        self.bridges: dict[str, BridgeCec] = {}

    def state(self, mac: str) -> BridgeCec:
        return self.bridges.setdefault(mac, BridgeCec())

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def _due(self, bridge: Bridge, st: BridgeCec, want: CecTarget, now: float) -> bool:
        rebooted = (
            st.uptime is not None and bridge.uptime is not None and bridge.uptime < st.uptime
        )
        if rebooted:
            _LOGGER.info("CEC: %s restarted; setting CEC forwarding up again", bridge.name)
            return True
        since = None if st.last_run is None else now - st.last_run
        if st.status == CEC_ERROR and since is not None and since < CEC_RETRY_SECONDS:
            return False  # wait before retrying a failure
        if st.applied != want:
            return True
        if want.mode == MODE_OFF:
            return False
        return since is None or since >= CEC_RECHECK_SECONDS

    async def async_sync(self, bridges: dict[str, Bridge], matrix: Matrix) -> bool:
        """Bring every bridge in line with the routing table. Returns True if anything changed.

        Skips the round if one is already running (the next poll will try again).
        """
        if self._lock.locked():
            return False
        async with self._lock:
            return await self._sync(bridges, matrix)

    async def _sync(self, bridges: dict[str, Bridge], matrix: Matrix) -> bool:
        now = self._clock()
        desired = plan_cec(bridges, matrix)
        work: list[tuple[str, Bridge, CecTarget]] = []
        changed = False

        routed = routed_macs(matrix)
        for mac in sorted(set(desired) | set(self.bridges)):
            st = self.state(mac)
            bridge = bridges.get(mac)
            want = desired.get(mac)
            unusable = bridge is None or not bridge.online or bridge.switching or not bridge.ip
            if unusable:
                # Offline or restarting: whatever ran there is gone or soon will be. Set it up
                # again when it is back (if it is still in a route).
                status = CEC_PENDING if mac in routed else CEC_OFF
                if st.applied is not None or st.status != status:
                    st.applied = None
                    st.status, changed = status, True
                continue
            if want is None and mac in routed:
                # Still in a route, but its partner is away (offline, restarting): leave it
                # running rather than stopping and restarting it around a short outage.
                continue
            if want is None and st.applied is not None and st.applied.mode != MODE_OFF:
                want = CecTarget(MODE_OFF)  # we set it up earlier; the route is gone now
            if st.wanted != want:
                st.wanted, changed = want, True
            if want is None:
                if st.status != CEC_OFF:
                    st.status, changed = CEC_OFF, True
                st.uptime = bridge.uptime
                continue
            due = self._due(bridge, st, want, now)
            st.uptime = bridge.uptime
            if not due:
                continue
            if self.dry_run:
                status = CEC_OFF if want.mode == MODE_OFF else CEC_DRY_RUN
                if st.dry_run_logged != want:
                    _LOGGER.warning(
                        "DRY RUN, nothing sent. Would run on %s (%s): %s",
                        bridge.name, bridge.ip, want.command,
                    )
                    st.dry_run_logged = want
                if st.status != status:
                    st.status, changed = status, True
                continue
            work.append((mac, bridge, want))

        # Sources first, so they are listening when their receivers start; stops last.
        order = {MODE_HOST: 0, MODE_CLIENT: 1, MODE_OFF: 2}
        work.sort(key=lambda item: order[item[2].mode])
        for mac, bridge, want in work:
            await self._run(mac, bridge, want)
            changed = True

        # A source's own check can't see a receiver that attached after it; trust the receivers.
        for mac, want in desired.items():
            if want.mode != MODE_HOST:
                continue
            st = self.state(mac)
            receivers = [
                self.state(m) for m, t in desired.items()
                if t.mode == MODE_CLIENT and t.arg == bridges[mac].ip
            ]
            if st.status == CEC_WAITING and any(r.status == CEC_LINKED for r in receivers):
                st.status, changed = CEC_LINKED, True
        return changed

    async def _run(self, mac: str, bridge: Bridge, want: CecTarget) -> None:
        st = self.state(mac)
        st.last_run = self._clock()
        try:
            output = await self._runner(mac, bridge.ip, want.command, self._script)
            result = parse_result(output)
        except Exception as err:  # noqa: BLE001 - any transport failure is reported the same way
            result = CecResult(result="error", detail=str(err) or type(err).__name__)
        st.result = result
        st.status = status_from(want, result)
        if result.success:
            st.applied = want
            _LOGGER.info("CEC: %s %s -> %s (%s)", bridge.name, want.command, result.result,
                         result.state)
        else:
            st.applied = None
            _LOGGER.warning("CEC: %s %s failed: %s", bridge.name, want.command, result.detail)


class SshRunner:
    """Runs the bridge script over SSH with the bridges' device password.

    The bridge's SSH host key is pinned on first use (per bridge, for as long as Home
    Assistant runs): a different key later is refused. EAV-Bridges keep their host keys in
    persistent storage, so the key only changes after a factory reset.
    """

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._host_keys: dict[str, str] = {}

    async def __call__(self, mac: str, ip: str, command: str, script: str) -> str:
        import asyncssh  # imported here so the rest of the integration works without it

        try:
            async with asyncssh.connect(
                ip,
                port=SSH_PORT,
                username=self._username,
                password=self._password,
                known_hosts=None,  # pinned below instead (bridges have no stable CA)
                client_keys=None,  # password only; don't offer Home Assistant's own keys
                connect_timeout=SSH_TIMEOUT,
            ) as conn:
                key = conn.get_server_host_key()
                fingerprint = key.get_fingerprint() if key is not None else ""
                pinned = self._host_keys.setdefault(mac, fingerprint)
                if pinned != fingerprint:
                    raise RuntimeError(
                        f"SSH host key of {ip} changed ({fingerprint}, expected {pinned}); "
                        "refusing. Reload the integration if the bridge was reset."
                    )
                result = await asyncio.wait_for(
                    conn.run(command, input=script, check=False), CEC_SCRIPT_TIMEOUT
                )
        except asyncssh.PermissionDenied as err:
            raise RuntimeError(
                "the bridge rejected the SSH username or password (UniFi Network: "
                "Settings > System > Device SSH Authentication)"
            ) from err
        except (OSError, asyncssh.Error, TimeoutError) as err:
            raise RuntimeError(f"SSH to {ip} failed: {err}") from err
        return f"{result.stdout or ''}\n{result.stderr or ''}"
