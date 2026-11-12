"""UniFi EAV Bridge: HDMI status, routing control and previews for Ubiquiti EAV-Bridges."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import importlib
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    Bridge,
    ConsoleData,
    EavApiError,
    EavAuthError,
    EavConnectionError,
    EavConsoleClient,
    Op,
    plan_client_audio_mode,
    plan_client_enabled,
    plan_group_enabled,
    plan_role,
    plan_route,
)
from .cec import CecManager, SshRunner
from .const import (
    CONF_CEC,
    CONF_CONTROL_MODE,
    CONF_SCAN_INTERVAL,
    CONF_SITE,
    CONF_SNAPSHOT_INTERVAL,
    CONF_SSH_PASSWORD,
    CONF_SSH_USERNAME,
    CONF_VERIFY_SSL,
    CONTROL_DRY_RUN,
    CONTROL_LIVE,
    CONTROL_OFF,
    DEFAULT_CEC,
    DEFAULT_CONTROL_MODE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SITE,
    DEFAULT_SNAPSHOT_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    EVENT_DRY_RUN,
    MIN_SCAN_INTERVAL,
    MIN_SNAPSHOT_INTERVAL,
    ROLE_HOST,
)

_LOGGER = logging.getLogger(__name__)

CEC_SCRIPT = Path(__file__).with_name("cec_bridge.sh")

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


class EavCoordinator(DataUpdateCoordinator[ConsoleData]):
    """Polls the console for bridge status and routing, and performs route changes."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: EavConsoleClient
    ) -> None:
        interval = max(
            MIN_SCAN_INTERVAL,
            entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
            config_entry=entry,
        )
        self.client = client
        self._write_lock = asyncio.Lock()
        self.last_plan: list[str] = []
        self.cec: CecManager | None = None  # set up by async_setup_entry when CEC is enabled
        self.cec_problem: str | None = None  # why CEC forwarding can't run, if it can't

    # ---- options ---------------------------------------------------------------

    @property
    def control_mode(self) -> str:
        return self.config_entry.options.get(CONF_CONTROL_MODE, DEFAULT_CONTROL_MODE)

    @property
    def snapshot_interval(self) -> int:
        return max(
            MIN_SNAPSHOT_INTERVAL,
            self.config_entry.options.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL),
        )

    # ---- reads -----------------------------------------------------------------

    async def _async_update_data(self) -> ConsoleData:
        try:
            data = await self.client.async_get_data()
        except EavAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (EavConnectionError, EavApiError) as err:
            raise UpdateFailed(str(err)) from err
        self._schedule_cec(data)
        return data

    # ---- CEC forwarding ----------------------------------------------------------

    def _schedule_cec(self, data: ConsoleData) -> None:
        """After each poll, let the CEC manager catch up in the background.

        Polls never wait for SSH. A round already in progress makes this a no-op.
        """
        if self.cec is None or self.cec.busy:
            return
        self.config_entry.async_create_background_task(
            self.hass, self._async_cec_round(data), name=f"{DOMAIN} CEC forwarding"
        )

    async def _async_cec_round(self, data: ConsoleData) -> None:
        if self.cec is None:
            return
        try:
            changed = await self.cec.async_sync(data.bridges, data.matrix)
        except Exception:  # noqa: BLE001 - never let a CEC problem break polling
            _LOGGER.exception("CEC forwarding round failed")
            return
        if changed:
            self.async_update_listeners()

    @property
    def bridges(self) -> dict[str, Bridge]:
        return self.data.bridges if self.data else {}

    def source_for(self, bridge: Bridge) -> Bridge | None:
        """For a receiver, find the transmitter it is routed to (by group), else by channel."""
        if self.data:
            group = self.data.matrix.group_of_client(bridge.mac)
            if group is not None:
                host = self.bridges.get((group.get("host") or {}).get("mac", "").lower())
                if host is not None:
                    return host
        if bridge.channel is None:
            return None
        for other in self.bridges.values():
            if other.role == ROLE_HOST and other.channel == bridge.channel:
                return other
        return None

    # ---- writes ----------------------------------------------------------------

    async def _run_plan(self, build) -> None:
        """Re-read the routing table, build a plan from it, then dry-run or send it."""
        if self.control_mode == CONTROL_OFF:
            raise HomeAssistantError("Control is turned off for this integration")
        async with self._write_lock:
            try:
                matrix = await self.client.async_get_matrix()  # never plan from stale data
                ops: list[Op] = build(matrix)
                self.last_plan = [op.describe() for op in ops]
                if self.control_mode == CONTROL_DRY_RUN:
                    _LOGGER.warning(
                        "DRY RUN, nothing sent. Would send: %s", self.last_plan or "(nothing to do)"
                    )
                    self.hass.bus.async_fire(
                        EVENT_DRY_RUN,
                        {"plan": self.last_plan, "bodies": [op.body for op in ops]},
                    )
                    self.async_update_listeners()  # show last_plan on the entities
                    return
                if not ops:
                    return
                _LOGGER.info("Sending %d change(s) to the console: %s", len(ops), self.last_plan)
                await self.client.async_apply(ops)
            except EavAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except EavApiError as err:
                raise HomeAssistantError(f"Console change failed: {err}") from err
        if self.control_mode == CONTROL_LIVE:
            await self.async_refresh()

    async def async_route(self, rx_mac: str, tx_mac: str | None) -> None:
        """Make a receiver watch a source (``None`` = nothing)."""
        tx_name = self.bridges[tx_mac].name if tx_mac and tx_mac in self.bridges else "Source"
        await self._run_plan(lambda m: plan_route(m, rx_mac, tx_mac, tx_name=tx_name))

    async def async_set_role(self, mac: str, role: str) -> None:
        """Make a bridge a source (``host``) or a receiver (``client``).

        Slow and disruptive: the bridge reboots (about 3 to 5 minutes or more), and the
        console may change routes. Plans from freshly read device data.
        """
        if self.control_mode == CONTROL_OFF:
            raise HomeAssistantError("Control is turned off for this integration")
        async with self._write_lock:
            try:
                bridges = await self.client.async_get_bridges()  # never plan from stale data
                ops = plan_role(bridges, mac, role)
                self.last_plan = [op.describe() for op in ops]
                if self.control_mode == CONTROL_DRY_RUN:
                    _LOGGER.warning(
                        "DRY RUN, nothing sent. Would send: %s", self.last_plan or "(nothing to do)"
                    )
                    self.hass.bus.async_fire(
                        EVENT_DRY_RUN,
                        {"plan": self.last_plan, "bodies": [op.body for op in ops]},
                    )
                    self.async_update_listeners()
                    return
                for op in ops:
                    _LOGGER.warning("Changing bridge role: %s", op.describe())
                    await self.client.async_set_role(op)
            except EavAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except EavApiError as err:
                raise HomeAssistantError(f"Role change failed: {err}") from err
        if self.control_mode == CONTROL_LIVE:
            await self.async_refresh()

    async def async_set_group_enabled(self, group_id: str, enabled: bool) -> None:
        await self._run_plan(lambda m: plan_group_enabled(m, group_id, enabled))

    async def async_set_receiver_enabled(self, rx_mac: str, enabled: bool) -> None:
        await self._run_plan(lambda m: plan_client_enabled(m, rx_mac, enabled))

    async def async_set_audio_mode(self, rx_mac: str, mode: str) -> None:
        """Set a receiver's audio mode: ``AUTO`` (pass through) or ``PCM``."""
        await self._run_plan(lambda m: plan_client_audio_mode(m, rx_mac, mode))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    session = async_create_clientsession(
        hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
    )
    client = EavConsoleClient(
        session,
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        site=entry.data.get(CONF_SITE, DEFAULT_SITE),
        verify_ssl=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
    )
    coordinator = EavCoordinator(hass, entry, client)
    await _async_setup_cec(hass, entry, coordinator)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    return True


async def _async_setup_cec(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: EavCoordinator
) -> None:
    """Create the CEC manager if CEC forwarding is turned on in the options.

    Needs control set to Live (Dry run only logs what it would do) and the bridges' SSH
    login (UniFi Network: Settings > System > Device SSH Authentication).
    """
    opts = entry.options
    if not opts.get(CONF_CEC, DEFAULT_CEC):
        return
    if coordinator.control_mode == CONTROL_OFF:
        coordinator.cec_problem = "control is off"
        return
    username = opts.get(CONF_SSH_USERNAME) or ""
    password = opts.get(CONF_SSH_PASSWORD) or ""
    if not username or not password:
        coordinator.cec_problem = "bridge SSH username or password missing"
        _LOGGER.warning("CEC forwarding is on but the bridge SSH login is not set in the options")
        return
    try:
        # Import once off the event loop (a first import reads files from disk).
        await hass.async_add_executor_job(importlib.import_module, "asyncssh")
    except ImportError:
        coordinator.cec_problem = "the asyncssh package is not installed"
        _LOGGER.error("CEC forwarding needs the asyncssh package, which is not installed")
        return
    script = await hass.async_add_executor_job(CEC_SCRIPT.read_text)
    coordinator.cec = CecManager(
        SshRunner(username, password),
        script,
        dry_run=coordinator.control_mode == CONTROL_DRY_RUN,
    )


async def _async_reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
