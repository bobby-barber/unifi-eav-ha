"""Constants for the UniFi EAV Bridge integration."""

from __future__ import annotations

DOMAIN = "unifi_eav_bridge"

CONF_SITE = "site"
CONF_VERIFY_SSL = "verify_ssl"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_SNAPSHOT_INTERVAL = "snapshot_interval"
CONF_CONTROL_MODE = "control_mode"

DEFAULT_SITE = "default"
DEFAULT_VERIFY_SSL = False  # UniFi consoles ship with a self-signed certificate
DEFAULT_SCAN_INTERVAL = 15  # seconds between status polls
MIN_SCAN_INTERVAL = 5
DEFAULT_SNAPSHOT_INTERVAL = 3  # seconds a fetched thumbnail is reused
MIN_SNAPSHOT_INTERVAL = 1

# Control (writing routes to the console) is opt-in.
#   off      read-only: no control entities are created
#   dry_run  control entities exist but only log and report what they WOULD send
#   live     changes are sent to the console
CONTROL_OFF = "off"
CONTROL_DRY_RUN = "dry_run"
CONTROL_LIVE = "live"
CONTROL_MODES = [CONTROL_OFF, CONTROL_DRY_RUN, CONTROL_LIVE]
DEFAULT_CONTROL_MODE = CONTROL_OFF

# The Network app reports each bridge's HDMI state in the "hdmi_ports" list of
# its stat/device record. On a receiver ("client") the output port carries a
# "state" string; WAITING_HOST_MODE is what the bridge shows on screen as
# "Waiting for video input...".
STATE_WAITING_FOR_SOURCE = "WAITING_HOST_MODE"

ROLE_HOST = "host"  # transmitter / source side
ROLE_CLIENT = "client"  # receiver / display side
ROLE_LABELS = {ROLE_HOST: "Source", ROLE_CLIENT: "Receiver"}  # what the role select shows
STATE_CONNECTED = 1  # stat/device "state" of a bridge that is connected and idle

# Each bridge serves a small unauthenticated MJPEG preview on this port.
BRIDGE_PREVIEW_PORT = 8080

NO_SOURCE = "Disconnected"  # option label for "receiver watches nothing"

EVENT_DRY_RUN = f"{DOMAIN}_dry_run"

# Receiver audio mode, a per-receiver field ("audio_mode") inside its route group.
# AUTO passes the source's audio through; PCM asks for plain PCM. The console accepts both.
# Tested 2026-11-11: PCM did NOT turn a 5.1 broadcast into stereo with the center channel
# kept, so it is not a downmix; see docs/gotchas.md ("Lost dialogue on 5.1").
AUDIO_MODE_AUTO = "AUTO"
AUDIO_MODE_PCM = "PCM"
AUDIO_MODE_LABELS = {AUDIO_MODE_AUTO: "Auto", AUDIO_MODE_PCM: "PCM"}

# ---- CEC forwarding (opt-in; see docs/cec.md) --------------------------------------
# UniFi's firmware ships with the SDK's HDMI-CEC forwarding switched off and undoes any
# change at every bridge restart, so the integration logs in to the bridges over SSH and
# starts it (cec_bridge.sh), again after every restart and route change.
CONF_CEC = "cec_forwarding"
CONF_SSH_USERNAME = "ssh_username"
CONF_SSH_PASSWORD = "ssh_password"
DEFAULT_CEC = False
SSH_PORT = 22
SSH_TIMEOUT = 20  # seconds to connect
CEC_SCRIPT_TIMEOUT = 90  # seconds the bridge script may take (it waits for the link)
# How often an already-linked bridge is checked anyway. Kept long on purpose: each check is
# one SSH login, and UniFi gateways with IPS can block hosts that log in to SSH a lot.
CEC_RECHECK_SECONDS = 30 * 60
CEC_RETRY_SECONDS = 5 * 60  # after an error
# A bridge that was set up but isn't linked yet (source waiting, receiver still searching) is
# checked again after this long, then twice as long each time, up to CEC_RECHECK_SECONDS.
# Without it, a "Waiting" or "Pending" status could sit for half an hour after the link came up
# (or a source that stopped serving went unnoticed for that long).
CEC_SETTLE_SECONDS = 2 * 60

# Values of the per-bridge "CEC" sensor.
CEC_LINKED = "linked"  # source with a receiver attached, or receiver linked to its source
CEC_WAITING = "waiting"  # source ready, no receiver attached yet
CEC_OFF = "off"  # not in use: no route, or forwarding stopped
CEC_PENDING = "pending"  # wanted but not set up yet (first run, bridge restarting, ...)
CEC_ERROR = "error"
CEC_DRY_RUN = "dry_run"  # would be set up; control is in dry run
CEC_STATES = [CEC_LINKED, CEC_WAITING, CEC_OFF, CEC_PENDING, CEC_ERROR, CEC_DRY_RUN]
