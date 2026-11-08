"""Home Assistant-level tests: config flow, entities, and state changes.

    pytest tests/test_ha.py

Needs pytest-homeassistant-custom-component. The console is mocked at the HTTP layer
with the same data shape captured from the real console.
"""

from __future__ import annotations

from datetime import timedelta

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.unifi_eav.const import (
    CONF_SITE,
    CONF_VERIFY_SSL,
    DOMAIN,
)

CONSOLE = "192.168.1.2"
LOGIN_URL = f"https://{CONSOLE}/api/auth/login"
DEVICE_URL = f"https://{CONSOLE}/proxy/network/api/s/default/stat/device"
TV = "02:00:00:00:00:01"
PROJ = "02:00:00:00:00:02"

ENTRY_DATA = {
    CONF_HOST: CONSOLE,
    CONF_USERNAME: "ha",
    CONF_PASSWORD: "pw",
    CONF_SITE: "default",
    CONF_VERIFY_SSL: False,
}


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def devices(source_connected: bool, waiting: bool):
    return [
        {"mac": "aa:bb:cc:dd:ee:ff", "name": "Some Switch", "state": 1},
        {
            "name": "VDS-01 Apple TV", "mac": TV, "state": 1,
            "hdmi_ports": [
                {"port_index": "1", "type": "in", "up": True,
                 "connected": source_connected, "resolution": "auto",
                 "refresh_rate": "30", "color_format": "rgb"},
                {"port_index": "2", "type": "out", "up": False, "connected": False,
                 "resolution": "auto", "refresh_rate": "30", "color_format": "rgb"},
            ],
            "video_info": {"channel": 2, "enabled": True, "role": "host"},
        },
        {
            "name": "VDS-02 Projector", "mac": PROJ, "state": 1,
            "hdmi_ports": [
                {"port_index": "1", "type": "in", "up": False, "connected": False,
                 "resolution": "auto", "refresh_rate": "30", "color_format": "rgb"},
                {"port_index": "2", "type": "out", "up": False, "connected": False,
                 "resolution": "1080p", "refresh_rate": "60", "color_format": "rgb",
                 "state": "WAITING_HOST_MODE" if waiting else "STREAMING"},
            ],
            "video_info": {"role": "client", "channel": 2, "mode": "unicast"},
        },
    ]


def mock_console(aioclient_mock, source_connected=False, waiting=True, login_status=200):
    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, status=login_status, json={},
                        headers={"X-CSRF-Token": "csrf"})
    aioclient_mock.get(DEVICE_URL, json={"data": devices(source_connected, waiting)})


async def setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=CONSOLE)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def state_of(hass: HomeAssistant, platform: str, mac: str, key: str):
    ent = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{mac}_{key}")
    assert ent is not None, f"no {platform} entity for {mac}_{key}"
    return hass.states.get(ent)


# ---- config flow -----------------------------------------------------------

async def test_flow_success(hass: HomeAssistant, aioclient_mock) -> None:
    mock_console(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ENTRY_DATA
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == CONSOLE
    await hass.async_block_till_done()
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def _flow_error(hass, aioclient_mock, setup) -> str:
    setup()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.FORM
    return result["errors"]["base"]


async def test_flow_invalid_auth(hass, aioclient_mock) -> None:
    err = await _flow_error(
        hass, aioclient_mock, lambda: mock_console(aioclient_mock, login_status=401)
    )
    assert err == "invalid_auth"


async def test_flow_cannot_connect(hass, aioclient_mock) -> None:
    def setup():
        aioclient_mock.clear_requests()
        aioclient_mock.post(LOGIN_URL, exc=aiohttp.ClientError("boom"))

    assert await _flow_error(hass, aioclient_mock, setup) == "cannot_connect"


async def test_flow_no_bridges(hass, aioclient_mock) -> None:
    def setup():
        aioclient_mock.clear_requests()
        aioclient_mock.post(LOGIN_URL, json={}, headers={"X-CSRF-Token": "c"})
        aioclient_mock.get(DEVICE_URL, json={"data": [{"mac": "aa", "name": "Switch"}]})

    assert await _flow_error(hass, aioclient_mock, setup) == "no_bridges"


# ---- entities --------------------------------------------------------------

async def test_entities_created_with_expected_states(hass, aioclient_mock) -> None:
    mock_console(aioclient_mock, source_connected=False, waiting=True)
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED

    waiting = state_of(hass, "binary_sensor", PROJ, "waiting_for_source")
    assert waiting.state == "on"
    assert waiting.attributes["source_device"] == "VDS-01 Apple TV"
    assert waiting.attributes["source_signal"] is False
    assert waiting.attributes["output_state"] == "WAITING_HOST_MODE"

    assert state_of(hass, "binary_sensor", TV, "source_signal").state == "off"
    assert state_of(hass, "binary_sensor", TV, "online").state == "on"
    assert state_of(hass, "binary_sensor", PROJ, "online").state == "on"
    assert state_of(hass, "sensor", PROJ, "output_state").state == "WAITING_HOST_MODE"


async def test_role_specific_entities(hass, aioclient_mock) -> None:
    mock_console(aioclient_mock)
    await setup_entry(hass)
    reg = er.async_get(hass)
    # a source has no "waiting" sensor, a receiver has no "source signal" sensor
    assert reg.async_get_entity_id("binary_sensor", DOMAIN, f"{TV}_waiting_for_source") is None
    assert reg.async_get_entity_id("binary_sensor", DOMAIN, f"{PROJ}_source_signal") is None


async def test_state_changes_on_next_poll(hass, aioclient_mock) -> None:
    mock_console(aioclient_mock, source_connected=False, waiting=True)
    await setup_entry(hass)
    assert state_of(hass, "binary_sensor", PROJ, "waiting_for_source").state == "on"

    # Apple TV turns on: console now reports signal and the receiver streaming.
    mock_console(aioclient_mock, source_connected=True, waiting=False)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()

    assert state_of(hass, "binary_sensor", PROJ, "waiting_for_source").state == "off"
    assert state_of(hass, "binary_sensor", TV, "source_signal").state == "on"
    assert state_of(hass, "sensor", PROJ, "output_state").state == "STREAMING"


async def test_entities_unavailable_when_console_drops(hass, aioclient_mock) -> None:
    mock_console(aioclient_mock)
    await setup_entry(hass)

    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, json={}, headers={"X-CSRF-Token": "c"})
    aioclient_mock.get(DEVICE_URL, exc=aiohttp.ClientError("down"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()

    # Must NOT read as "off"/"on": an automation would act on a false reading.
    assert state_of(hass, "binary_sensor", PROJ, "waiting_for_source").state == "unavailable"


async def test_setup_fails_cleanly_on_bad_login(hass, aioclient_mock) -> None:
    mock_console(aioclient_mock, login_status=401)
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_unload(hass, aioclient_mock) -> None:
    mock_console(aioclient_mock)
    entry = await setup_entry(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
