"""Client and routing-planner tests against a mock console (no Home Assistant needed)."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.unifi_eav_bridge import api
from tests.helpers import (
    IPS, MockConsole, RX1, RX2, RX3, TX1, TX2, default_groups, matrix_json,
)


@pytest.fixture(autouse=True)
def _allow_local_sockets(socket_enabled):
    """These tests talk to a local mock server; the HA plugin blocks sockets by default."""
    yield


@pytest.fixture
async def console():
    mock = MockConsole()
    server = TestServer(mock.app)
    await server.start_server()
    mock.url = f"http://{server.host}:{server.port}"
    yield mock
    await server.close()


@pytest.fixture
async def client(console):
    # DummyCookieJar discards every cookie, like Home Assistant's sessions do for
    # consoles addressed by IP. The client must forward cookies itself.
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
        yield api.EavConsoleClient(s, console.url, "ha", "good-password")


def matrix(groups=None) -> api.Matrix:
    return api.parse_matrix(matrix_json(groups if groups is not None else default_groups()))


# ---- reads --------------------------------------------------------------------

async def test_reads_with_a_cookie_jar_that_forgets_cookies(client) -> None:
    data = await client.async_get_data()
    assert set(data.bridges) == {TX1, TX2, RX1, RX2, RX3}  # "Some Switch" ignored
    assert data.bridges[TX1].role == "host" and data.bridges[RX1].role == "client"
    assert data.bridges[TX1].ip == IPS[TX1]
    assert data.matrix.hosts == [TX1, TX2]
    assert data.matrix.group_of_client(RX2)["host"]["mac"] == TX1


async def test_status_fields(client, console) -> None:
    data = await client.async_get_data()
    assert data.bridges[TX1].source_signal is False
    assert data.bridges[RX1].waiting_for_source is True
    assert data.bridges[TX1].waiting_for_source is None


async def test_session_expiry_relogs_in_once(client, console) -> None:
    await client.async_get_data()
    before = console.logins
    console.token = "rotated-by-console"
    await client.async_get_data()
    assert console.logins == before + 1


async def test_bad_password(console) -> None:
    async with aiohttp.ClientSession() as s:
        bad = api.EavConsoleClient(s, console.url, "ha", "nope")
        with pytest.raises(api.EavAuthError):
            await bad.async_get_data()


async def test_unreachable() -> None:
    async with aiohttp.ClientSession() as s:
        dead = api.EavConsoleClient(s, "http://127.0.0.1:9", "ha", "x")
        with pytest.raises(api.EavConnectionError):
            await dead.async_get_data()


async def test_snapshot_fallback_endpoint(client, console) -> None:
    assert await client.async_get_snapshot(TX1) == console.JPEG


# ---- planner (pure) ---------------------------------------------------------------

def test_noop_when_already_routed() -> None:
    assert api.plan_route(matrix(), RX1, TX1) == []


def test_noop_unrouting_an_unrouted_receiver() -> None:
    assert api.plan_route(matrix(), RX3, None) == []


def test_unrouted_receiver_joins_existing_group_and_modes_update() -> None:
    ops = api.plan_route(matrix(), RX3, TX1)
    assert [o.method for o in ops] == ["PUT"]
    clients = ops[0].body["clients"]
    assert [c["mac"] for c in clients] == [RX1, RX2, RX3]
    assert {c["mode"] for c in clients} == {"MULTICAST"}
    assert ops[0].body["id"] == "g-tv"  # full object is sent back


def test_unrouted_receiver_gets_a_new_group_for_a_source_without_one() -> None:
    ops = api.plan_route(matrix(), RX3, TX2, tx_name="Game Console")
    assert [o.method for o in ops] == ["POST"]
    body = ops[0].body
    assert body["name"] == "Game Console" and body["host"]["mac"] == TX2
    assert [c["mac"] for c in body["clients"]] == [RX3]
    assert body["clients"][0]["mode"] == "UNICAST"
    assert "id" not in body


def test_move_between_groups_shrinks_old_and_recomputes_modes() -> None:
    # RX2 leaves the Apple TV group (2 -> 1 receiver, so unicast) and gets a new group.
    ops = api.plan_route(matrix(), RX2, TX2, tx_name="Game Console")
    assert [o.method for o in ops] == ["PUT", "POST"]
    old = ops[0].body
    assert [c["mac"] for c in old["clients"]] == [RX1]
    assert old["clients"][0]["mode"] == "UNICAST"
    assert ops[1].body["host"]["mac"] == TX2


def test_leaving_receiver_keeps_its_settings() -> None:
    groups = default_groups()
    groups[0]["clients"][1].update(resolution="1080p", audio_enabled=False)
    ops = api.plan_route(matrix(groups), RX2, TX2)
    joined = ops[-1].body["clients"][0]
    assert joined["resolution"] == "1080p" and joined["audio_enabled"] is False


def test_last_receiver_leaving_deletes_the_group() -> None:
    groups = default_groups()
    groups[0]["clients"] = groups[0]["clients"][:1]  # only the projector
    ops = api.plan_route(matrix(groups), RX1, TX2)
    assert [o.method for o in ops] == ["DELETE", "POST"]
    assert ops[0].group_id == "g-tv"


def test_disconnect_receiver() -> None:
    ops = api.plan_route(matrix(), RX2, None)
    assert [o.method for o in ops] == ["PUT"]
    assert [c["mac"] for c in ops[0].body["clients"]] == [RX1]


def test_planner_does_not_mutate_input() -> None:
    m = matrix()
    before = repr(m.groups)
    api.plan_route(m, RX2, TX2)
    api.plan_route(m, RX3, TX1)
    assert repr(m.groups) == before


def test_group_and_client_enable_planners() -> None:
    m = matrix()
    ops = api.plan_group_enabled(m, "g-tv", False)
    assert ops[0].body["enabled"] is False and ops[0].method == "PUT"
    assert api.plan_group_enabled(m, "g-tv", True) == []
    ops = api.plan_client_enabled(m, RX2, False)
    assert [c["enabled"] for c in ops[0].body["clients"]] == [True, False]
    with pytest.raises(api.EavApiError):
        api.plan_client_enabled(m, RX3, False)  # not routed
    with pytest.raises(api.EavApiError):
        api.plan_group_enabled(m, "nope", True)


# ---- applying plans to the mock console ---------------------------------------------

async def apply(client, console, rx, tx, name="Source"):
    data = await client.async_get_data()
    ops = api.plan_route(data.matrix, rx, tx, tx_name=name)
    await client.async_apply(ops)
    return ops


def final_routes(console) -> dict[str, list[str]]:
    return {g["host"]["mac"]: [c["mac"] for c in g["clients"]] for g in console.groups}


async def test_apply_join_existing(client, console) -> None:
    await apply(client, console, RX3, TX1)
    assert final_routes(console) == {TX1: [RX1, RX2, RX3]}


async def test_apply_move_to_new_group(client, console) -> None:
    await apply(client, console, RX2, TX2, "Game Console")
    assert final_routes(console) == {TX1: [RX1], TX2: [RX2]}
    assert all(c["mode"] == "UNICAST" for g in console.groups for c in g["clients"])


async def test_apply_move_then_move_back(client, console) -> None:
    await apply(client, console, RX2, TX2, "Game Console")
    await apply(client, console, RX2, TX1)
    assert final_routes(console) == {TX1: [RX1, RX2]}  # empty TX2 group was removed


async def test_apply_disconnect(client, console) -> None:
    await apply(client, console, RX2, None)
    assert final_routes(console) == {TX1: [RX1]}


async def test_writes_carry_csrf_and_cookie(client, console) -> None:
    await apply(client, console, RX3, TX1)  # would 403/401 without them
    assert console.writes and console.writes[0][0] == "PUT"


async def test_write_failure_is_reported_with_status(client, console) -> None:
    console.fail_next_write = 500
    with pytest.raises(api.EavConnectionError) as err:
        await apply(client, console, RX3, TX1)
    assert err.value.status == 500


async def test_group_toggle_applies(client, console) -> None:
    data = await client.async_get_data()
    await client.async_apply(api.plan_group_enabled(data.matrix, "g-tv", False))
    assert console.groups[0]["enabled"] is False
