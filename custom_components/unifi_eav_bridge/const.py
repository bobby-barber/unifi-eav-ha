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

# Each bridge serves a small unauthenticated MJPEG preview on this port.
BRIDGE_PREVIEW_PORT = 8080

NO_SOURCE = "Disconnected"  # option label for "receiver watches nothing"

EVENT_DRY_RUN = f"{DOMAIN}_dry_run"
