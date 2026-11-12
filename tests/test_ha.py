"""Home Assistant-level tests: config flow, status entities, control, and previews.

    pytest tests

Runs the integration inside Home Assistant against a real local HTTP mock of the console
(tests/helpers.py), so cookie handling, CSRF and the actual request sequences are exercised.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiohttp.test_utils import TestServer
from homeassistant import config_entries
from homeassistant.components import camera as camera_component
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.unifi_eav_bridge import camera as camera_module
from custom_components.unifi_eav_bridge.const import (
    CONF_CONTROL_MODE,
    CONF_SITE,
    CONF_SNAPSHOT_INTERVAL,
    CONF_VERIFY_SSL,
    CONTROL_DRY_RUN,
    CONTROL_LIVE,
    DOMAIN,
    EVENT_DRY_RUN,
    NO_SOURCE,
)
from tests.helpers import (
    MockConsole, RX1, RX2, RX3, TX1, TX2, devices_json, group, client as grp_client,
)


@pytest.fixture(autouse=True)
def _env(enable_custom_integrations, socket_enabled):
    yield


@pytest.fixture
async def console(monkeypatch):
    mock = MockConsole()
    server = TestServer(mock.app)
    await server.start_server()
    mock.url = f"http://{server.host}:{server.port}"
    # Bridges "live" on the mock server too, so previews have somewhere to come from.
    for d in mock.devices:
        if "ip" in d:
            d["ip"] = server.host
    monkeypatch.setattr(camera_module, "BRIDGE_PREVIEW_PORT", server.port)
    yield mock
    await server.close()


def entry_data(console, password="good-password"):
    return {
        CONF_HOST: console.url, CONF_USERNAME: "ha", CONF_PASSWORD: password,
        CONF_SITE: "default", CONF_VERIFY_SSL: False,
    }


async def setup_entry(hass, console, options=None, password="good-password") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data(console, password), options=options or {},
        unique_id=console.url,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def ent(hass, platform, mac, key):
    reg = er.async_get(hass)
    return reg.async_get_entity_id(platform, DOMAIN, f"{mac}_{key}")


def state_of(hass, platform, mac, key):
    eid = ent(hass, platform, mac, key)
    assert eid is not None, f"no {platform} entity for {mac}_{key}"
    return hass.states.get(eid)


async def poll(hass, seconds=20):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    # the coordinator refreshes in a background task, over a real socket
    await hass.async_block_till_done(wait_background_tasks=True)


def routes(console):
    return {g["host"]["mac"]: [c["mac"] for c in g["clients"]] for g in console.groups}


# ================================================================== config flow

async def test_flow_success(hass: HomeAssistant, console) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], entry_data(console))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def _flow_error(hass, data) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.FORM
    return result["errors"]["base"]


async def test_flow_invalid_auth(hass, console) -> None:
    assert await _flow_error(hass, entry_data(console, password="wrong")) == "invalid_auth"


async def test_flow_cannot_connect(hass, console) -> None:
    data = {**entry_data(console), CONF_HOST: "http://127.0.0.1:9"}
    assert await _flow_error(hass, data) == "cannot_connect"


async def test_flow_no_bridges(hass, console) -> None:
    console.devices = [{"mac": "aa", "name": "Switch"}]
    assert await _flow_error(hass, entry_data(console)) == "no_bridges"


# ================================================================== status (read-only)

async def test_status_entities(hass, console) -> None:
    entry = await setup_entry(hass, console)
    assert entry.state is ConfigEntryState.LOADED

    waiting = state_of(hass, "binary_sensor", RX1, "waiting_for_source")
    assert waiting.state == "on"
    assert waiting.attributes["source_device"] == "Apple TV"  # by routing group
    assert waiting.attributes["source_signal"] is False
    assert state_of(hass, "binary_sensor", TX1, "source_signal").state == "off"
    assert state_of(hass, "binary_sensor", TX1, "online").state == "on"
    assert state_of(hass, "sensor", RX1, "output_state").state == "WAITING_HOST_MODE"


async def test_role_specific_entities(hass, console) -> None:
    await setup_entry(hass, console)
    assert ent(hass, "binary_sensor", TX1, "waiting_for_source") is None
    assert ent(hass, "binary_sensor", RX1, "source_signal") is None


async def test_status_changes_on_next_poll(hass, console) -> None:
    await setup_entry(hass, console)
    console.devices = devices_json(source_connected=True, waiting=False)
    for d in console.devices:
        if "ip" in d:
            d["ip"] = "127.0.0.1"
    await poll(hass)
    assert state_of(hass, "binary_sensor", RX1, "waiting_for_source").state == "off"
    assert state_of(hass, "binary_sensor", TX1, "source_signal").state == "on"
    assert state_of(hass, "sensor", RX1, "output_state").state == "OPERATING"


async def test_unavailable_not_false_when_console_drops(hass, console) -> None:
    await setup_entry(hass, console)
    console.fail_reads = True
    await poll(hass)
    # An automation must never read a console outage as "source is off".
    assert state_of(hass, "binary_sensor", RX1, "waiting_for_source").state == "unavailable"


async def test_bad_login_fails_setup_cleanly(hass, console) -> None:
    entry = await setup_entry(hass, console, password="wrong")
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_unload(hass, console) -> None:
    entry = await setup_entry(hass, console)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


# ================================================================== control

async def test_control_off_creates_no_control_entities(hass, console) -> None:
    await setup_entry(hass, console)
    assert ent(hass, "select", RX1, "source") is None
    assert ent(hass, "switch", RX1, "output_enabled") is None
    assert ent(hass, "switch", TX1, "route_enabled") is None


async def test_control_off_blocks_direct_writes(hass, console) -> None:
    entry = await setup_entry(hass, console)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    with pytest.raises(HomeAssistantError):
        await coordinator.async_route(RX3, TX1)
    assert console.writes == []


async def test_source_select_lists_sources_and_current(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    s = state_of(hass, "select", RX1, "source")
    assert s.state == "Apple TV"
    assert set(s.attributes["options"]) == {NO_SOURCE, "Apple TV", "Game Console"}
    assert state_of(hass, "select", RX3, "source").state == NO_SOURCE  # unrouted


async def select(hass, mac, option):
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": ent(hass, "select", mac, "source"), "option": option}, blocking=True,
    )
    await hass.async_block_till_done()


async def test_dry_run_sends_nothing_but_reports_the_plan(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_DRY_RUN})
    events = []
    hass.bus.async_listen(EVENT_DRY_RUN, events.append)
    before = routes(console)

    await select(hass, RX2, "Game Console")

    assert console.writes == [] and routes(console) == before
    assert len(events) == 1
    assert [p.split()[0] for p in events[0].data["plan"]] == ["PUT", "POST"]
    attrs = state_of(hass, "select", RX2, "source").attributes
    assert attrs["control_mode"] == "dry_run" and len(attrs["last_plan"]) == 2


async def test_live_move_receiver_to_other_source(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    await select(hass, RX2, "Game Console")
    assert routes(console) == {TX1: [RX1], TX2: [RX2]}
    assert [w[0] for w in console.writes] == ["PUT", "POST"]
    # entity state follows the console after the refresh
    assert state_of(hass, "select", RX2, "source").state == "Game Console"
    assert state_of(hass, "select", RX1, "source").state == "Apple TV"


async def test_live_join_unrouted_receiver_and_disconnect(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    await select(hass, RX3, "Apple TV")
    assert routes(console) == {TX1: [RX1, RX2, RX3]}
    await select(hass, RX3, NO_SOURCE)
    assert routes(console) == {TX1: [RX1, RX2]}
    assert state_of(hass, "select", RX3, "source").state == NO_SOURCE


async def test_live_choosing_current_source_sends_nothing(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    await select(hass, RX1, "Apple TV")
    assert console.writes == []


async def test_live_write_failure_raises_and_leaves_state(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    console.fail_next_write = 500
    with pytest.raises(HomeAssistantError):
        await select(hass, RX3, "Apple TV")
    assert routes(console) == {TX1: [RX1, RX2]}


async def test_permission_error_explains_what_to_fix(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    console.fail_next_write = 403
    with pytest.raises(HomeAssistantError, match="not allowed to change routes"):
        await select(hass, RX3, "Apple TV")
    assert routes(console) == {TX1: [RX1, RX2]}


async def test_plans_from_fresh_data_not_cached(hass, console) -> None:
    """Someone changes routes in the UniFi app; HA's cache is stale; we must not undo it."""
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    # Out-of-band: bar TV was moved to the game console in the UniFi app.
    console.groups = [
        group("g-tv", TX1, [grp_client(RX1)]),
        group("g-game", TX2, [grp_client(RX2)]),
    ]
    await select(hass, RX3, "Game Console")  # HA still thinks RX2 is on Apple TV
    assert routes(console) == {TX1: [RX1], TX2: [RX2, RX3]}


async def test_route_and_output_switches(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    assert state_of(hass, "switch", TX1, "route_enabled").state == "on"
    assert state_of(hass, "switch", TX2, "route_enabled").state == "unknown"  # no group

    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": ent(hass, "switch", TX1, "route_enabled")}, blocking=True)
    await hass.async_block_till_done()
    assert console.groups[0]["enabled"] is False
    assert state_of(hass, "switch", TX1, "route_enabled").state == "off"

    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": ent(hass, "switch", RX2, "output_enabled")}, blocking=True)
    await hass.async_block_till_done()
    assert [c["enabled"] for c in console.groups[0]["clients"]] == [True, False]
    assert state_of(hass, "switch", RX2, "output_enabled").state == "off"
    assert state_of(hass, "switch", RX3, "output_enabled").state == "unknown"  # unrouted


async def test_options_reload_adds_control_entities(hass, console) -> None:
    entry = await setup_entry(hass, console)
    assert ent(hass, "select", RX1, "source") is None
    hass.config_entries.async_update_entry(entry, options={CONF_CONTROL_MODE: CONTROL_DRY_RUN})
    await hass.async_block_till_done()
    assert ent(hass, "select", RX1, "source") is not None


# ================================================================== previews

def cam(hass, mac):
    eid = ent(hass, "camera", mac, "preview")
    assert eid is not None
    return eid


async def test_every_bridge_gets_a_preview_camera(hass, console) -> None:
    await setup_entry(hass, console)
    for mac in (TX1, TX2, RX1, RX2, RX3):
        assert hass.states.get(cam(hass, mac)) is not None


async def test_preview_comes_from_the_bridge_itself(hass, console) -> None:
    await setup_entry(hass, console)
    img = await camera_component.async_get_image(hass, cam(hass, TX1))
    assert img.content == MockConsole.BRIDGE_JPEG
    assert console.preview_hits == 1 and console.snapshot_hits == 0
    assert hass.states.get(cam(hass, TX1)).attributes["image_source"] == "bridge"


async def test_preview_is_cached_then_refreshed(hass, console, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(camera_module, "_now", lambda: clock[0])
    await setup_entry(hass, console, {CONF_SNAPSHOT_INTERVAL: 5})
    for _ in range(3):
        await camera_component.async_get_image(hass, cam(hass, TX1))
    assert console.preview_hits == 1  # reused

    clock[0] += 6  # past the refresh interval
    await camera_component.async_get_image(hass, cam(hass, TX1))
    assert console.preview_hits == 2


async def test_preview_refetched_immediately_when_state_changes(hass, console, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(camera_module, "_now", lambda: clock[0])
    await setup_entry(hass, console, {CONF_SNAPSHOT_INTERVAL: 300})
    await camera_component.async_get_image(hass, cam(hass, TX1))
    assert console.preview_hits == 1

    console.devices = devices_json(source_connected=True, waiting=False)  # Apple TV came on
    for d in console.devices:
        if "ip" in d:
            d["ip"] = "127.0.0.1"
    await poll(hass)
    await camera_component.async_get_image(hass, cam(hass, TX1))
    assert console.preview_hits == 2  # no waiting out the 300 s


async def test_preview_falls_back_to_console_snapshot(hass, console) -> None:
    await setup_entry(hass, console)
    console.preview_ok = False
    img = await camera_component.async_get_image(hass, cam(hass, TX1))
    assert img.content == MockConsole.JPEG
    assert hass.states.get(cam(hass, TX1)).attributes["image_source"] == "console"


async def test_preview_keeps_last_image_if_everything_is_down(hass, console, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(camera_module, "_now", lambda: clock[0])
    await setup_entry(hass, console, {CONF_SNAPSHOT_INTERVAL: 1})
    first = await camera_component.async_get_image(hass, cam(hass, TX1))
    console.preview_ok = False
    console.token = "gone"  # console snapshot fails too (login would still work, so break it)
    console.groups = console.groups  # no-op; keeps intent explicit
    clock[0] += 10
    again = await camera_component.async_get_image(hass, cam(hass, TX1))
    assert again.content in (first.content, MockConsole.JPEG)  # never an exception, never empty


async def test_live_stream_passes_through(hass, console, hass_client) -> None:
    await setup_entry(hass, console)
    eid = cam(hass, TX1)
    token = hass.states.get(eid).attributes["access_token"]
    http = await hass_client()
    resp = await http.get(f"/api/camera_proxy_stream/{eid}?token={token}")
    assert resp.status == 200
    assert "multipart/x-mixed-replace" in resp.headers["Content-Type"]
    body = await resp.read()
    assert MockConsole.BRIDGE_JPEG in body


# ================================================================== bridge role (source / receiver)

async def set_role(hass, mac, option):
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": ent(hass, "select", mac, "role"), "option": option}, blocking=True,
    )
    await hass.async_block_till_done()


def device_puts(console):
    return [w for w in console.writes if w[1].startswith("device:")]


async def test_role_select_only_with_control_on(hass, console) -> None:
    await setup_entry(hass, console)
    assert ent(hass, "select", TX1, "role") is None


async def test_role_select_shows_current_role_for_every_bridge(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    assert state_of(hass, "select", TX1, "role").state == "Source"
    assert state_of(hass, "select", RX1, "role").state == "Receiver"
    assert set(state_of(hass, "select", RX1, "role").attributes["options"]) == {"Source", "Receiver"}
    assert state_of(hass, "select", RX1, "role").attributes["switching"] is False


async def test_role_dry_run_sends_nothing(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_DRY_RUN})
    events = []
    hass.bus.async_listen(EVENT_DRY_RUN, events.append)
    await set_role(hass, TX2, "Receiver")
    assert console.writes == []
    assert events[0].data["bodies"] == [{"eav_bridge_role": "client"}]
    assert "reboots" in state_of(hass, "select", TX2, "role").attributes["last_plan"][0]


async def test_live_role_change_sends_one_put_and_shows_switching(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    await set_role(hass, TX2, "Receiver")
    assert device_puts(console) == [("PUT", f"device:id-{TX2}", {"eav_bridge_role": "client"})]
    s = state_of(hass, "select", TX2, "role")
    assert s.state == "Receiver"  # the console already shows the new role
    assert s.attributes["switching"] is True  # but the bridge has not finished
    assert s.attributes["reported_role"] == "Source"


async def test_entities_follow_the_bridge_after_it_changes_role(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    assert state_of(hass, "binary_sensor", TX2, "source_signal").state != "unavailable"
    await set_role(hass, TX2, "Receiver")
    console.finish_switch(TX2)
    await poll(hass)
    # source-only entities are now unavailable; receiver ones have appeared and work
    assert state_of(hass, "binary_sensor", TX2, "source_signal").state == "unavailable"
    assert state_of(hass, "select", TX2, "role").attributes["switching"] is False
    assert ent(hass, "select", TX2, "source") is not None
    assert state_of(hass, "select", TX2, "source").state == NO_SOURCE
    assert ent(hass, "binary_sensor", TX2, "waiting_for_source") is not None
    # and back again: the original entities come back instead of duplicating
    await set_role(hass, TX2, "Source")
    console.finish_switch(TX2)
    await poll(hass)
    assert state_of(hass, "binary_sensor", TX2, "source_signal").state != "unavailable"
    assert state_of(hass, "select", TX2, "source").state == "unavailable"


async def test_role_change_refused_while_any_bridge_is_busy(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    await set_role(hass, TX2, "Receiver")  # starts a switch that has not finished
    with pytest.raises(HomeAssistantError, match="wait for"):
        await set_role(hass, RX3, "Source")
    assert len(device_puts(console)) == 1  # the second one never reached the console


async def test_choosing_the_current_role_sends_nothing(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    await set_role(hass, TX1, "Source")
    assert console.writes == []


async def test_role_change_permission_error_is_explained(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    console.csrf = "rotated"  # the mock answers writes with 403 when the CSRF header is wrong
    with pytest.raises(HomeAssistantError, match="not allowed to change"):
        await set_role(hass, TX2, "Receiver")


async def test_role_change_rejected_by_console_raises(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    console.device_put_rc = "error"
    with pytest.raises(HomeAssistantError, match="did not accept"):
        await set_role(hass, TX2, "Receiver")


async def test_role_control_off_blocks_direct_writes(hass, console) -> None:
    entry = await setup_entry(hass, console)
    with pytest.raises(HomeAssistantError):
        await hass.data[DOMAIN][entry.entry_id].async_set_role(TX2, "client")
    assert console.writes == []


# ================================================================== receiver audio mode

async def set_audio(hass, mac, option):
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": ent(hass, "select", mac, "audio_mode"), "option": option}, blocking=True,
    )
    await hass.async_block_till_done()


async def test_audio_mode_select_on_receivers_only(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    assert state_of(hass, "select", RX1, "audio_mode").state == "Auto"
    assert ent(hass, "select", TX1, "audio_mode") is None
    assert state_of(hass, "select", RX3, "audio_mode").state == "unavailable"  # not routed


async def test_audio_mode_change_writes_only_that_receiver(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    await set_audio(hass, RX2, "PCM")
    assert [c["audio_mode"] for c in console.groups[0]["clients"]] == ["AUTO", "PCM"]
    assert routes(console) == {TX1: [RX1, RX2]}  # route untouched
    assert state_of(hass, "select", RX2, "audio_mode").state == "PCM"
    writes = len(console.writes)
    await set_audio(hass, RX2, "PCM")  # already PCM: nothing sent
    assert len(console.writes) == writes


async def test_audio_mode_dry_run_sends_nothing(hass, console) -> None:
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_DRY_RUN})
    await set_audio(hass, RX1, "PCM")
    assert console.writes == []
    assert "audio mode -> PCM" in state_of(hass, "select", RX1, "audio_mode").attributes[
        "last_plan"][0]


# ================================================================== CEC forwarding

from custom_components.unifi_eav_bridge.const import (  # noqa: E402
    CONF_CEC, CONF_SSH_PASSWORD, CONF_SSH_USERNAME,
)

CEC_OPTS = {
    CONF_CONTROL_MODE: CONTROL_LIVE, CONF_CEC: True,
    CONF_SSH_USERNAME: "admin", CONF_SSH_PASSWORD: "bridge-pass",
}


@pytest.fixture
def fake_ssh(monkeypatch):
    """Replaces SSH: records (ip, command) and answers like the bridge script."""
    calls: list[tuple[str, str, str]] = []
    made: list[tuple[str, str]] = []

    class Fake:
        def __init__(self, username, password):
            made.append((username, password))

        async def __call__(self, mac, ip, command, script):
            assert "HA_CEC result=" in script  # the real script file is sent
            calls.append((mac, ip, command))
            mode = command.split()[3]
            state = "s_pre_start_srv" if mode == "host" else "s_srv_on"
            return f"HA_CEC result=started mode={mode} state={state} target=x detail=ok\n"

    monkeypatch.setattr("custom_components.unifi_eav_bridge.SshRunner", Fake)
    return calls, made


async def test_cec_off_by_default_no_sensor_no_ssh(hass, console, fake_ssh) -> None:
    calls, made = fake_ssh
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE})
    await poll(hass)
    assert ent(hass, "sensor", TX1, "cec") is None
    assert calls == [] and made == []


async def test_cec_sets_up_routed_bridges_after_poll(hass, console, fake_ssh) -> None:
    calls, made = fake_ssh
    await setup_entry(hass, console, CEC_OPTS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert made == [("admin", "bridge-pass")]
    assert [(c[0], c[2]) for c in calls] == [
        (TX1, "bash -s -- host 0002"),
        (RX1, f"bash -s -- client {console.devices[1]['ip']}"),
        (RX2, f"bash -s -- client {console.devices[1]['ip']}"),
    ]
    cec = state_of(hass, "sensor", RX1, "cec")
    assert cec.state == "linked"
    assert cec.attributes["link_state"] == "s_srv_on"
    assert state_of(hass, "sensor", TX1, "cec").state == "linked"
    assert state_of(hass, "sensor", RX3, "cec").state == "off"  # not routed
    calls.clear()
    await poll(hass)
    assert calls == []  # nothing changed: no new SSH logins


async def test_cec_follows_a_route_change(hass, console, fake_ssh) -> None:
    calls, _ = fake_ssh
    await setup_entry(hass, console, CEC_OPTS)
    await hass.async_block_till_done(wait_background_tasks=True)
    calls.clear()
    await select(hass, RX3, "Apple TV")
    await poll(hass)
    assert [(c[0], c[2].split()[3]) for c in calls] == [(RX3, "client")]


async def test_cec_dry_run_logs_only(hass, console, fake_ssh) -> None:
    calls, _ = fake_ssh
    await setup_entry(hass, console, {**CEC_OPTS, CONF_CONTROL_MODE: CONTROL_DRY_RUN})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == []
    assert state_of(hass, "sensor", RX1, "cec").state == "dry_run"


async def test_cec_without_ssh_login_explains(hass, console, fake_ssh) -> None:
    calls, _ = fake_ssh
    await setup_entry(hass, console, {CONF_CONTROL_MODE: CONTROL_LIVE, CONF_CEC: True})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == []
    s = state_of(hass, "sensor", TX1, "cec")
    assert s.state == "off"
    assert "password missing" in s.attributes["problem"]


async def _options(hass, entry, data):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(result["flow_id"], data)


OPT_BASE = {"scan_interval": 15, "snapshot_interval": 3}


async def test_options_cec_needs_control_and_ssh(hass, console, fake_ssh) -> None:
    entry = await setup_entry(hass, console)
    r = await _options(hass, entry, {**OPT_BASE, CONF_CONTROL_MODE: "off", CONF_CEC: True,
                                     CONF_SSH_USERNAME: "a", CONF_SSH_PASSWORD: "b"})
    assert r["errors"] == {"base": "cec_needs_control"}
    r = await _options(hass, entry, {**OPT_BASE, CONF_CONTROL_MODE: CONTROL_LIVE,
                                     CONF_CEC: True, CONF_SSH_USERNAME: "a"})
    assert r["errors"] == {"base": "cec_needs_ssh"}


async def test_options_empty_password_keeps_the_saved_one(hass, console, fake_ssh) -> None:
    entry = await setup_entry(hass, console, CEC_OPTS)
    r = await _options(hass, entry, {**OPT_BASE, CONF_CONTROL_MODE: CONTROL_LIVE,
                                     CONF_CEC: True, CONF_SSH_USERNAME: "admin",
                                     CONF_SSH_PASSWORD: ""})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SSH_PASSWORD] == "bridge-pass"
