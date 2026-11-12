# Bridge internals

What runs on an EAV-Bridge, found by reading the device over SSH (read-only unless a section
says otherwise). Confidence labels follow the [README](README.md).

## Hardware and system (Verified)

| | |
|---|---|
| CPU / RAM | one ARMv7 core, about 480 MB (about 275 MB free in normal use) |
| OS | Linux 4.9.0 (`armv7l`), BusyBox userland |
| Firmware | version 1.0.11.1297; system image A9.19.5 build 5341 |
| SSH | dropbear (`-R -B`: creates host keys, allows blank passwords) on port 22 |
| Storage | flash with an A/B image layout (`kernel_a/b`, `rootfs_patch_a/b`) and a small parameter partition |
| Idle load average | typically 3.4–4.4 (a single core already busy with scripts, **not** a sign of trouble) |
| Boot time | about 59 s from `reboot` to answering on the network |
| Role at boot | kernel command line carries `uav_mode=host` (source) or client (receiver) |

The ASPEED video-over-IP SDK is the base: shell "link manager" scripts in `/usr/local/bin`, an HDMI
chip handler for a GSV2K11 chip, a Qt touchscreen UI, and UniFi's management agent (`mcad`) on top.

## Services (Observed)

Main processes on both roles: `ubusd` (message bus), `ui-hal` (hardware abstraction, HDMI link
events), `ui-mainctld`, `mcad` (talks to the console; standard UniFi inform), `ulcmd`,
`fwupdate`, `msgd`, `GUI` (framebuffer UI), `heartbeat`, `name_service`, `node_responser`,
`astclient`, `irsd`, `cec`, `gsv2k11_app_arm_c`, several `link_mgrc_*` or `link_mgrh_*` scripts
(`c` = client/receiver, `h` = host/source), and **`mjpg_streamer`** (the preview).

## Listening ports (Observed)

| Port | Service | Notes |
|---|---|---|
| TCP 22 | dropbear SSH | needs a key or the configured device password |
| TCP 8080 | MJPG-Streamer | the preview; **no login** |
| TCP 6850 | `msgd` | internal messaging |
| TCP 59401 | `irsd` | IR service |
| UDP 3333 | `name_service` | discovery |
| UDP 10001 | `mcad` | UniFi discovery |
| UDP 5353 | avahi | mDNS |
| UDP 225.x.0.2:590xx | `heartbeat` | multicast heartbeats between bridges |

The bridge does **not** serve HTTPS or a web UI.

## Receiver output states

The receiver's output port carries a `state` string in the console's device record
(**Verified**, three values seen, all picked up within one 15 s poll):

| `state` | Meaning | On the projector |
|---|---|---|
| `OPERATING` | video is flowing | picture |
| `WAITING_HOST_MODE` | its source is off or not sending | "Waiting for video input..." splash |
| `CLIENT_STATE_SUSPENDING` | its route was disabled on purpose | no picture |

`WAITING_HOST_MODE` is the one that means "the source went away". On the device it comes from the
video link manager's power-save handler (event `e_video_enter_pwr_save`), which also shows the
on-screen text. `CLIENT_STATE_SUSPENDING` appeared about one poll after the route was turned off
and cleared about one poll after it was turned on.

## Status files on a source bridge (Observed)

* **`/tmp/video_status`**: JSON describing the input and output sides, including the things the
  console does not expose:

  ```json
  {"result":"ok","mode":"host",
   "source":{"type":"hdmi_in","connected":false,
             "video":{"resolution":"unknown","width":0,"height":0,"refreshRateHz":0,
                      "colorFormat":"unknown","aspectRatio":"unknown","interlaced":false},
             "audio":{"present":false,"audioFormat":"unknown","audioSampleRateHz":0,
                      "channels":0,"sampleSize":0}},
   "output":{"type":"videoip_out","connected":false, "video":{...}, "audio":{...}}}
  ```

  This was only read while the source was off. It likely carries the **live resolution, refresh
  rate, color format and audio format** when a signal is present (not checked with a signal).
* **`/tmp/hdmiIn`**: a one-character flag, stale (not live).
* **`/tmp/gsv.log`**: the HDMI chip's event log (plug detect, TMDS lock, audio lock).
* **`mca-dump`**: prints the bridge's own status report (the same content it sends to the console:
  `hdmi_ports`, `video_info`, `if_table`, `system-stats`, `sys_stats`, `version`, ...). Filter
  secrets before sharing any output.

## Configuration tools (Verified)

| Tool | Use |
|---|---|
| `astparam g <name>` / `s <name> <value>` / `save` / `dump` | read, stage, and persist settings in the parameter partition |
| `e <event>` | send an event to the link manager, for example `e e_ss::config::snapshot::1280,720,90,0` |
| `fw_printenv` | bootloader environment |

`e` calls `ipc @lm_set s "<event>"`, so a non-interactive SSH session must have `/usr/local/bin`
on `PATH`; otherwise it prints `ipc: command not found` and does nothing.

## The preview subsystem

See [preview-quality.md](preview-quality.md).

## How the role (source or receiver) is stored (Observed)

Read from the device; **nothing here was changed by hand**.

* The role is the U-Boot variable **`uav_mode`**, `host` (source) or `client` (receiver). Read it
  with `fw_printenv uav_mode` (`fw_printenv` lives in `/usr/sbin`, which is not on the PATH of a
  non-interactive SSH session). The kernel command line carries it as `uav_mode=host|client`.
* Every boot, `/etc/init.d/rcS` reads it and picks the platform (`ast1530-h-v1` for a source,
  `ast1530-c-v1` for a receiver). A receiver also gets display and input device nodes. The device tree
  names the board "AST1530 Host EVB" or "Client EVB".
* U-Boot has helper commands `force_uav_mode host|client` and `reset_uav_mode` (back to what the
  device tree says). The host and client builds are separate images (`all.bin` and `allc.bin` for
  network boot).
* The bridge's `fwupdate` program contains a **role switch** routine: it handles an upgrade request
  with an "other role" option, sets `uav_mode` and the boot image, reboots, and rolls back if
  something fails. It refuses if another image slot is damaged or an upgrade is running. The
  Ubiquiti agent (`mcad`) and `ui-hal` also read the role. This is how the console's role change
  reaches the bridge.
* **Do not change `uav_mode` by hand.** That would skip the rest of the switch. Use the console.

## HDMI-CEC (Observed, firmware 1.0.11.1297)

* The SDK's CEC support is present on both roles: kernel driver `/usr/local/bin/cec.ko`
  (sysfs `/sys/devices/platform/cec/` once loaded: `topology`, `forward_rule`, `send`, `debug_level`,
  `arc_proxy`, ...), the `cec` app, link managers `link_mgrh_cec.sh` (source) and `link_mgrc_cec.sh`
  (receiver), and helpers `cec_send`, `cec_power_on_tv`, `cec_power_off_tv`, `cec_topology`.
* It is off: `/etc/init.d/rcS` sets `NO_CEC=y` at every boot, so the driver never loads.
  UniFi's `ui-hal` has a `cec` ubus method (`ubus call hal cec '{"enable":true}'`) that saves a
  lowercase `no_cec=n`; at boot that drives GPIO 3 high and fails to set the forward rule.
* Network: CEC data on UDP 59800/59801, heartbeat on 59008, multicast group `225.8.<channel>`.
* Started by hand it works (**Verified**). How, and why it can't be made permanent on the bridge:
  [cec.md](cec.md).

## Root filesystem (Verified)

The kernel boots with `root=/dev/ram`: the root filesystem is a RAM disk loaded from flash, writable
but thrown away at every restart. That is why SSH keys and any edited script vanish. What persists:
the `astparam` settings in flash and the `/data` partition (ext4: Dante data, dropbear host keys,
UniFi settings under `/data/persistent`). Boot runs no user script from either.

## The real video stream

A receiver took in roughly **146 Mbit/s** (about 1,200 packets/s) of actual video while the
preview was about 14 Mbit/s. It is carried as UDP by the ASPEED video-over-IP kernel module
(`videoip`); a single receiver gets it as unicast and multiple receivers share multicast groups
in the `225.x` range. The format is proprietary. We know of no standard tool that decodes it,
and we did not try. Capturing it would need a switch mirror port, and decoding it would be a
reverse-engineering project. For a monitor or thumbnail, raising the preview quality is the
practical route.
