"""Constants for the UniFi EAV Bridge Status integration."""

from __future__ import annotations

DOMAIN = "unifi_eav"

CONF_SITE = "site"
CONF_VERIFY_SSL = "verify_ssl"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SITE = "default"
DEFAULT_VERIFY_SSL = False  # UniFi consoles ship with a self-signed certificate
DEFAULT_SCAN_INTERVAL = 15  # seconds
MIN_SCAN_INTERVAL = 5

# The Network app reports each bridge's HDMI state in the "hdmi_ports" list of
# its stat/device record. On a receiver ("client") the output port carries a
# "state" string; WAITING_HOST_MODE is what the bridge shows on screen as
# "Waiting for video input...".
STATE_WAITING_FOR_SOURCE = "WAITING_HOST_MODE"

ROLE_HOST = "host"  # transmitter / source side
ROLE_CLIENT = "client"  # receiver / display side
