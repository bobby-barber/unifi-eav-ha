"""CEC forwarding: planning, result parsing, the manager's timing rules, the SSH transport,
and the bridge script's syntax.

    pytest tests/test_cec.py

The bridge script itself was verified on real bridges (docs/cec.md, "Verified"); here it is
only syntax-checked, and the transport is exercised against a real local SSH server.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from custom_components.unifi_eav_bridge.api import parse_bridges, parse_matrix
from custom_components.unifi_eav_bridge.cec import (
    CecManager,
    CecTarget,
    SshRunner,
    channel_label,
    parse_result,
    plan_cec,
    status_from,
)
from custom_components.unifi_eav_bridge.const import (
    CEC_DRY_RUN,
    CEC_ERROR,
    CEC_LINKED,
    CEC_OFF,
    CEC_PENDING,
    CEC_RECHECK_SECONDS,
    CEC_RETRY_SECONDS,
    CEC_SETTLE_SECONDS,
    CEC_WAITING,
)
from tests.helpers import IPS, RX1, RX2, RX3, TX1, TX2, client, devices_json, group, matrix_json

SCRIPT = Path(__file__).parents[1] / "custom_components/unifi_eav_bridge/cec_bridge.sh"


def system(groups=None, devices=None):
    bridges = parse_bridges(devices or devices_json())
    matrix = parse_matrix(matrix_json(groups if groups is not None else default()))
    return bridges, matrix


def default():
    return [group("g-tv", TX1, [client(RX1, mode="MULTICAST"), client(RX2, mode="MULTICAST")])]


# --------------------------------------------------------------------------- planning


def test_channel_label_matches_the_sdk_format() -> None:
    assert channel_label(2) == "0002"
    assert channel_label("13") == "0013"
    assert channel_label(9999) == "9999"
    assert channel_label(None) is None
    assert channel_label(10000) is None
    assert channel_label("x") is None


def test_plan_links_each_receiver_to_its_source() -> None:
    bridges, matrix = system()
    plan = plan_cec(bridges, matrix)
    assert plan == {
        TX1: CecTarget("host", "0002"),
        RX1: CecTarget("client", IPS[TX1]),
        RX2: CecTarget("client", IPS[TX1]),
    }
    assert RX3 not in plan and TX2 not in plan  # unrouted bridges are left alone


def test_plan_uses_the_groups_channel() -> None:
    bridges, matrix = system([group("g", TX1, [client(RX1)], channel=17)])
    assert plan_cec(bridges, matrix)[TX1] == CecTarget("host", "0017")


def test_plan_skips_disabled_routes_and_receivers() -> None:
    bridges, matrix = system([group("g", TX1, [client(RX1)], enabled=False)])
    assert plan_cec(bridges, matrix) == {}
    bridges, matrix = system([group("g", TX1, [client(RX1, enabled=False), client(RX2)])])
    assert set(plan_cec(bridges, matrix)) == {TX1, RX2}


def test_plan_skips_offline_and_switching_bridges() -> None:
    devices = devices_json()
    for d in devices:
        if d.get("mac") == RX1:
            d["state"] = 0
        if d.get("mac") == RX2:
            d["state"] = 5  # busy (changing role)
    bridges, matrix = system(devices=devices)
    assert plan_cec(bridges, matrix) == {}  # no usable receiver, so the source isn't set up


def test_plan_needs_a_source_address() -> None:
    devices = devices_json()
    for d in devices:
        if d.get("mac") == TX1:
            d["ip"] = None
    bridges, matrix = system(devices=devices)
    assert plan_cec(bridges, matrix) == {}


def test_target_command() -> None:
    assert CecTarget("host", "0002").command == "bash -s -- host 0002"
    assert CecTarget("off").command == "bash -s -- off"


# --------------------------------------------------------------------------- results


def test_parse_result_reads_the_last_summary_line() -> None:
    out = ("noise\nHA_CEC result=started mode=host state=s_srv_on target=0002 "
           "detail=channel_from_bridge\nstderr junk\n")
    r = parse_result(out)
    assert (r.result, r.state, r.target, r.detail) == (
        "started", "s_srv_on", "0002", "channel_from_bridge")
    assert r.success


def test_parse_result_without_summary_is_an_error() -> None:
    r = parse_result("bash: ipc: command not found\n")
    assert r.result == "error" and not r.success
    assert "command not found" in r.detail


@pytest.mark.parametrize(
    ("target", "line", "status"),
    [
        (CecTarget("host", "0002"), "result=ok state=s_srv_on", CEC_LINKED),
        (CecTarget("host", "0002"), "result=started state=s_pre_start_srv", CEC_WAITING),
        (CecTarget("client", "1.2.3.4"), "result=started state=s_srv_on", CEC_LINKED),
        (CecTarget("client", "1.2.3.4"), "result=error state=s_search", CEC_ERROR),
        (CecTarget("off"), "result=stopped state=none", CEC_OFF),
    ],
)
def test_status_from_result(target, line, status) -> None:
    assert status_from(target, parse_result(f"HA_CEC {line} target=x detail=y")) == status


# --------------------------------------------------------------------------- manager


class FakeBridges:
    """Stands in for SSH: records calls and answers like the real script would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (mac, command)
        self.fail: set[str] = set()
        self.raise_for: set[str] = set()
        self.state: dict[str, str] = {}  # mac -> link state to report instead of the usual

    async def __call__(self, mac, ip, command, script):
        assert script.startswith("#!/bin/bash")
        self.calls.append((mac, command))
        if mac in self.raise_for:
            raise RuntimeError("SSH to x failed: timed out")
        mode = command.split()[3]
        if mac in self.fail:
            return f"HA_CEC result=error mode={mode} state=s_search target=none detail=boom"
        state = {"host": "s_pre_start_srv", "client": "s_srv_on", "off": "none"}[mode]
        state = self.state.get(mac, state)
        result = "stopped" if mode == "off" else "started"
        return f"HA_CEC result={result} mode={mode} state={state} target=x detail=ok"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def manager(dry_run=False):
    fake, clock = FakeBridges(), Clock()
    mgr = CecManager(fake, SCRIPT.read_text(), dry_run=dry_run, clock=clock)
    return mgr, fake, clock


async def test_first_round_sets_up_sources_before_receivers() -> None:
    mgr, fake, _ = manager()
    bridges, matrix = system()
    assert await mgr.async_sync(bridges, matrix)
    assert [c[0] for c in fake.calls] == [TX1, RX1, RX2]
    assert fake.calls[0][1] == "bash -s -- host 0002"
    assert fake.calls[1][1] == f"bash -s -- client {IPS[TX1]}"
    assert mgr.bridges[RX1].status == CEC_LINKED
    # The source reported "waiting", but a receiver linked to it afterwards.
    assert mgr.bridges[TX1].status == CEC_LINKED


async def test_nothing_runs_again_until_something_changes() -> None:
    mgr, fake, clock = manager()
    bridges, matrix = system()
    await mgr.async_sync(bridges, matrix)
    fake.calls.clear()
    clock.now += 60
    assert not await mgr.async_sync(bridges, matrix)
    assert fake.calls == []


async def test_periodic_recheck() -> None:
    mgr, fake, clock = manager()
    bridges, matrix = system()
    await mgr.async_sync(bridges, matrix)
    fake.calls.clear()
    clock.now += CEC_RECHECK_SECONDS + 1
    await mgr.async_sync(bridges, matrix)
    assert [c[0] for c in fake.calls] == [TX1, RX1, RX2]


async def test_bridge_restart_is_detected_by_uptime() -> None:
    mgr, fake, clock = manager()
    devices = devices_json()
    bridges, matrix = system(devices=devices)
    await mgr.async_sync(bridges, matrix)
    fake.calls.clear()
    for d in devices:
        if d.get("mac") == RX1:
            d["uptime"] = 5  # was 100: it restarted between polls
    clock.now += 30
    bridges, matrix = system(devices=devices)
    await mgr.async_sync(bridges, matrix)
    assert fake.calls == [(RX1, f"bash -s -- client {IPS[TX1]}")]


async def test_bridge_offline_then_back_is_set_up_again() -> None:
    mgr, fake, clock = manager()
    devices = devices_json()
    await mgr.async_sync(*system(devices=devices))
    fake.calls.clear()
    for d in devices:
        if d.get("mac") == RX2:
            d["state"] = 0
    clock.now += 30
    await mgr.async_sync(*system(devices=devices))
    assert fake.calls == []  # nothing to do while it is away; the others keep running
    assert mgr.bridges[RX2].status == CEC_PENDING  # still in a route: set up when back
    for d in devices:
        if d.get("mac") == RX2:
            d["state"] = 1
    clock.now += 30
    await mgr.async_sync(*system(devices=devices))
    assert fake.calls == [(RX2, f"bash -s -- client {IPS[TX1]}")]


async def test_route_change_relinks_the_receiver() -> None:
    mgr, fake, clock = manager()
    await mgr.async_sync(*system())
    fake.calls.clear()
    moved = [group("g-tv", TX1, [client(RX1)]), group("g-game", TX2, [client(RX2)], channel=5)]
    clock.now += 30
    await mgr.async_sync(*system(moved))
    assert fake.calls == [
        (TX2, "bash -s -- host 0005"),
        (RX2, f"bash -s -- client {IPS[TX2]}"),
    ]


async def test_removed_route_stops_forwarding_on_those_bridges() -> None:
    mgr, fake, clock = manager()
    await mgr.async_sync(*system())
    fake.calls.clear()
    clock.now += 30
    await mgr.async_sync(*system([group("g-tv", TX1, [client(RX1)])]))
    assert fake.calls == [(RX2, "bash -s -- off")]
    assert mgr.bridges[RX2].status == CEC_OFF
    fake.calls.clear()
    clock.now += CEC_RECHECK_SECONDS + 1
    await mgr.async_sync(*system([group("g-tv", TX1, [client(RX1)])]))
    assert (RX2, "bash -s -- off") not in fake.calls  # an applied "off" isn't repeated


async def test_errors_wait_before_retrying() -> None:
    mgr, fake, clock = manager()
    fake.fail.add(RX1)
    fake.raise_for.add(RX2)
    await mgr.async_sync(*system())
    assert mgr.bridges[RX1].status == CEC_ERROR
    assert mgr.bridges[RX1].result.detail == "boom"
    assert mgr.bridges[RX2].status == CEC_ERROR
    assert "timed out" in mgr.bridges[RX2].result.detail
    assert mgr.bridges[TX1].status == CEC_WAITING  # no receiver linked
    fake.calls.clear()
    clock.now += 60
    await mgr.async_sync(*system())
    assert fake.calls == []  # too soon to retry
    fake.fail.clear()
    fake.raise_for.clear()
    clock.now += CEC_RETRY_SECONDS
    await mgr.async_sync(*system())
    # The source was left waiting, so it is looked at again too (sources first).
    assert [c[0] for c in fake.calls] == [TX1, RX1, RX2]
    assert mgr.bridges[RX1].status == CEC_LINKED


async def test_unlinked_bridges_are_checked_again_soon() -> None:
    # Tonight's case: the receiver was still searching when its run ended, so both showed
    # "Waiting"/"Pending" for half an hour after the link had come up on its own.
    mgr, fake, clock = manager()
    fake.state[RX1] = fake.state[RX2] = "s_search"
    await mgr.async_sync(*system())
    assert mgr.bridges[TX1].status == CEC_WAITING
    assert mgr.bridges[RX1].status == CEC_PENDING
    fake.calls.clear()
    clock.now += CEC_SETTLE_SECONDS - 1
    await mgr.async_sync(*system())
    assert fake.calls == []  # not yet
    fake.state.clear()  # the receivers linked meanwhile
    clock.now += 1
    await mgr.async_sync(*system())
    assert [c[0] for c in fake.calls] == [TX1, RX1, RX2]
    assert {mgr.bridges[m].status for m in (TX1, RX1, RX2)} == {CEC_LINKED}
    fake.calls.clear()
    clock.now += CEC_SETTLE_SECONDS * 4
    await mgr.async_sync(*system())
    assert fake.calls == []  # linked: back to the half-hourly check


async def test_unlinked_checks_back_off() -> None:
    mgr, fake, clock = manager()
    fake.state[RX1] = fake.state[RX2] = "s_search"  # never links
    await mgr.async_sync(*system())
    times = []
    start = clock.now
    for _ in range(int(3 * CEC_RECHECK_SECONDS / 60)):
        clock.now += 60
        fake.calls.clear()
        await mgr.async_sync(*system())
        if (RX1, f"bash -s -- client {IPS[TX1]}") in fake.calls:
            times.append(int(clock.now - start))
    gaps = [b - a for a, b in zip([0, *times], times)]
    s = CEC_SETTLE_SECONDS
    assert gaps[:4] == [s, 2 * s, 4 * s, 8 * s]
    assert max(gaps) == CEC_RECHECK_SECONDS  # capped at the normal recheck


async def test_dry_run_runs_nothing() -> None:
    mgr, fake, _ = manager(dry_run=True)
    await mgr.async_sync(*system())
    assert fake.calls == []
    assert mgr.bridges[TX1].status == CEC_DRY_RUN
    assert mgr.bridges[RX1].attributes()["wanted"] == f"client {IPS[TX1]}"


async def test_rounds_never_overlap() -> None:
    gate = asyncio.Event()
    calls = []

    async def slow(mac, ip, command, script):
        calls.append(mac)
        await gate.wait()
        return "HA_CEC result=started mode=client state=s_srv_on target=x detail=ok"

    mgr = CecManager(slow, "#!/bin/bash\n", clock=Clock())
    first = asyncio.create_task(mgr.async_sync(*system()))
    await asyncio.sleep(0)
    assert mgr.busy
    assert not await mgr.async_sync(*system())  # skipped, not queued
    gate.set()
    await first
    assert calls == [TX1, RX1, RX2]


async def test_source_restart_leaves_receivers_alone_then_sets_source_up() -> None:
    mgr, fake, clock = manager()
    devices = devices_json()
    await mgr.async_sync(*system(devices=devices))
    fake.calls.clear()
    for d in devices:
        if d.get("mac") == TX1:
            d["state"] = 5  # restarting
    clock.now += 30
    await mgr.async_sync(*system(devices=devices))
    assert mgr.bridges[TX1].status == CEC_PENDING
    assert mgr.bridges[TX1].applied is None
    assert fake.calls == []  # receivers are not stopped around a short outage
    for d in devices:
        if d.get("mac") == TX1:
            d["state"], d["uptime"] = 1, 3
    clock.now += 30
    await mgr.async_sync(*system(devices=devices))
    assert fake.calls == [(TX1, "bash -s -- host 0002")]


# --------------------------------------------------------------------------- the script


def test_bridge_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_bridge_script_has_the_documented_modes() -> None:
    text = SCRIPT.read_text()
    for mode in ("status)", "off)", "host)", "client)"):
        assert f"\n{mode}" in text
    assert "HA_CEC result=" in text
    assert "astparam s" not in text and "astparam save" not in text  # never writes flash


# --------------------------------------------------------------------------- SSH transport


@pytest.fixture
async def ssh_server(socket_enabled):
    asyncssh = pytest.importorskip("asyncssh")
    received: list[tuple[str, str]] = []

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "admin" and password == "right"

    async def handle(process):
        script = await process.stdin.read()
        received.append((process.command, script))
        process.stdout.write("HA_CEC result=ok mode=host state=s_srv_on target=0002 detail=x\n")
        process.exit(0)

    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        Server, "127.0.0.1", 0, server_host_keys=[key], process_factory=handle
    )
    port = server.sockets[0].getsockname()[1]
    yield port, received, key
    server.close()
    await server.wait_closed()


async def test_ssh_runner_sends_script_on_stdin(ssh_server, monkeypatch) -> None:
    port, received, _ = ssh_server
    monkeypatch.setattr("custom_components.unifi_eav_bridge.cec.SSH_PORT", port)
    runner = SshRunner("admin", "right")
    out = await runner(TX1, "127.0.0.1", "bash -s -- host 0002", "#!/bin/bash\necho hi\n")
    assert parse_result(out).result == "ok"
    assert received == [("bash -s -- host 0002", "#!/bin/bash\necho hi\n")]


async def test_ssh_runner_wrong_password_is_explained(ssh_server, monkeypatch) -> None:
    port, _, _ = ssh_server
    monkeypatch.setattr("custom_components.unifi_eav_bridge.cec.SSH_PORT", port)
    with pytest.raises(RuntimeError, match="rejected the SSH username or password"):
        await SshRunner("admin", "wrong")(TX1, "127.0.0.1", "bash -s -- status", "")


async def test_ssh_runner_refuses_a_changed_host_key(ssh_server, monkeypatch) -> None:
    port, _, _ = ssh_server
    monkeypatch.setattr("custom_components.unifi_eav_bridge.cec.SSH_PORT", port)
    runner = SshRunner("admin", "right")
    runner._host_keys[TX1] = "SHA256:somethingelse"
    with pytest.raises(RuntimeError, match="host key .* changed"):
        await runner(TX1, "127.0.0.1", "bash -s -- status", "")


async def test_ssh_runner_unreachable(monkeypatch, socket_enabled) -> None:
    pytest.importorskip("asyncssh")
    monkeypatch.setattr("custom_components.unifi_eav_bridge.cec.SSH_PORT", 1)
    with pytest.raises(RuntimeError, match="SSH to 127.0.0.1 failed"):
        await SshRunner("admin", "right")(TX1, "127.0.0.1", "bash -s -- status", "")


async def test_source_goes_first_even_when_its_mac_sorts_last() -> None:
    mgr, fake, _ = manager()
    late = "02:00:00:00:00:ff"
    devices = devices_json()
    for d in devices:
        if d.get("mac") == TX1:
            d["mac"] = late
    groups = [group("g", late, [client(RX1)], name="Late source")]
    await mgr.async_sync(*system(groups, devices))
    assert [c[0] for c in fake.calls] == [late, RX1]
