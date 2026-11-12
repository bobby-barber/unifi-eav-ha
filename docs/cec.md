# HDMI-CEC forwarding

Plugged straight into a TV, an Apple TV (or any HDMI-CEC device) turns the TV on when it wakes and
puts it in standby when it sleeps. Through EAV-Bridges that stops working: the bridges don't pass
CEC out of the box. This page explains why, how the integration turns it on anyway, and everything
found along the way.

Legend: **Verified** / **Observed** / **Unverified** (see [README](README.md)).

## Summary

* The bridges **can** forward CEC. They are built on ASPEED's video-over-IP SDK, which carries CEC
  between a source and its receivers over the network. **Verified**: with it running, an Apple TV
  on a source bridge turned an LG TV on a receiver bridge on and off (2026-11-11).
* UniFi's firmware **switches it off** at every boot, and UniFi's own CEC switch does not turn it
  back on (details below).
* The bridge's root filesystem is a **RAM disk** reloaded from flash at every boot, so no fix made on
  a bridge survives a restart.
* So the integration does it from the outside: with **CEC forwarding** turned on in its options, it
  logs in to the bridges over SSH and starts the SDK's CEC service, and does that again after every
  bridge restart and route change.

## Turning it on

1. In UniFi Network, set a device SSH password: **Settings > System > Device SSH Authentication**
   (the username and password the bridges accept for SSH). It's one login for all adopted devices.
2. In Home Assistant, the integration's **Configure** (options):
   * **Control**: **Live**. In **Dry run** nothing is sent; the log shows what would run on each bridge.
   * **CEC forwarding**: on.
   * **Bridge SSH username** and **Bridge SSH password**: from step 1. Leaving the password empty
     keeps the saved one.
3. Home Assistant needs to reach each bridge on **TCP 22**.
4. Make sure the source device has CEC turned on (Apple TV: **Settings > Remotes and Devices >
   Control TVs and Receivers**), and so does the TV (LG calls it SIMPLINK, Samsung Anynet+, Sony
   BRAVIA Sync).

Each bridge then gets a **CEC** diagnostic sensor:

| State | Meaning |
|---|---|
| `linked` | Source with a receiver attached, or a receiver linked to its source |
| `waiting` | Source is ready, no receiver has attached yet |
| `off` | Not in a route, or forwarding was stopped |
| `pending` | Wanted but not set up yet (first round, bridge restarting or offline) |
| `error` | The last attempt failed; see the `detail` attribute. Retried after 5 minutes |
| `dry_run` | Would be set up; control is in Dry run |

Attributes: `wanted` and `applied` (what the bridge should run / last confirmed), `link_state`
(the SDK's own state, `s_srv_on` when linked), `detail`, `last_run`. If CEC is on but can't run
(control off, no SSH login), the sensor stays `off` with a `problem` attribute.

## When the integration logs in

After each console poll (every 15 s by default) the integration works out which bridges need CEC,
from the routing table: every enabled route's source, and each of its enabled, online receivers.
It then logs in only where something is due:

* a route appeared, changed source, or got a new channel;
* a bridge restarted (its uptime went down) or came back online;
* an error is 5 minutes old (retry);
* 30 minutes passed since the last check of a working bridge.

A bridge that leaves every route gets `off` once. One bridge is handled at a time, sources before
their receivers, and a round never overlaps another. Polling never waits for SSH.

Why so careful with logins: UniFi gateways with intrusion prevention can block a host that opens
many SSH sessions (see [gotchas](gotchas.md#the-gateways-intrusion-prevention-can-block-ssh-to-the-bridges)).
A steady system costs one login per bridge every 30 minutes.

When a source restarts, its receivers are left running: they keep looking for it, and reattach on
their own once it is set up again (**Observed**).

## What runs on the bridge

The integration sends [`cec_bridge.sh`](../custom_components/unifi_eav_bridge/cec_bridge.sh) over
SSH on standard input and runs it with `bash -s -- <mode> [arg]`. Nothing is stored on the bridge
except a temporary copy of an SDK script in `/tmp` (RAM). The script never writes to flash.

| Mode | What it does |
|---|---|
| `host <channel>` | Source. Starts the CEC service with CEC on, sets the forward rule to pass everything, advertises CEC as on, starts a session on the route's channel |
| `client <source ip>` | Receiver. Same start, then links to the source by address |
| `off` | Stops the service and sets the forward rule to block |
| `status` | Reports, changes nothing |

When the bridge is already running with the wanted target, the script answers `ok` and changes
nothing. Its last line is always a summary the integration reads:

```text
HA_CEC result=started mode=client state=s_srv_on target=192.0.2.21 detail=linked
```

`result` is `ok`, `started`, `stopped`, `off`, `running` or `error`; on error `detail` names the step
(`not_a_source`, `cec_driver_missing`, `manager_did_not_start`, `source_did_not_answer`, ...).

The steps, and why each one is needed:

1. **Stop every copy of the SDK's CEC link manager** (`link_mgrh_cec.sh` on a source,
   `link_mgrc_cec.sh` on a receiver), its IPC server and the `cec` app, with `kill -9`. The link
   manager traps `SIGTERM`, so a normal kill leaves it running, and two copies fight over the same
   IPC queue (sessions then get "Ignore. session ID doesn't match").
2. **Start the link manager from a copy with CEC on.** At start it reads `/var/lm_var`, which holds
   `NO_CEC=y`; with that it never opens the network side (the kernel log then shows
   `CEC:sk_net_tx_frame is NULL` for every message). The copy sets `NO_CEC=n` right after reading
   that file. Starting it also loads the `cec.ko` driver.
3. **`echo 0xff > /sys/devices/platform/cec/forward_rule`**: pass every CEC message. The driver
   loads with `forward_rule=0` (block all). `0xff` is also what UniFi's own startup code tries to set.
   The narrower bits are `0x01` system audio, `0x02` remote control pass-through, `0x04` standby,
   `0x08` one touch play.
4. **Source only:** `node_query --if essential --of essential --set_key NO_CEC=n`, so receivers that
   ask see CEC as on, then `ipc @c_lm_set s ce_start:<channel>`.
5. **Receiver only:** `ipc @c_lm_set s ce_start:<source ip>`. By address, because a channel name
   finds nothing: UniFi doesn't run the SDK's name lookup (`MY_IP=Unknown`, "Unknown host ()").

### Channels

The SDK names a route by a four-digit channel, and builds multicast addresses from it:
`225.<service>.<first two digits>.<last two digits>`, with service 2 for video and 8 for CEC.
UniFi's route channel (the group's `channel` in the console, e.g. `2`) is that number: channel 2
is `0002`, video on `225.2.0.2`, CEC on `225.8.0.2`. **Verified**.

`/var/lm_var` on a source said `CH_SELECT=0000`, which is **wrong** for UniFi; starting on `0000`
puts the source on `225.8.0.0` where no receiver looks. The script therefore reads the channel from
the bridge itself (the address its video service listens on, UDP 59002) and falls back to the
console's number. `detail=channel_from_bridge` or `channel_from_console` says which it used.

A route that is deleted and recreated gets a new channel; the integration follows that.

## Why UniFi's firmware doesn't do this itself

Found on firmware 1.0.11.1297 (**Observed** on all bridges checked):

* `/etc/init.d/rcS` runs `astparam s NO_CEC y` on every boot, in both the normal and manufacturing
  paths. The SDK's link manager reads that and never starts CEC.
* UniFi added its own switch: `ubus call hal cec '{"enable":true}'` (in `ui-hal`). It saves a
  lowercase `no_cec=n` to flash and tries to set the forward rule. With `no_cec=n`, a block later in
  `rcS` drives GPIO 3 high (it appears to power or enable the CEC line) and writes `0xff` to the
  forward rule, then logs `Failed to set CEC forward_rule` and `CEC initialization failed`, because
  the driver isn't loaded at that point and nothing loads it afterwards.
* Setting the uppercase `NO_CEC=n` in flash does nothing either: `rcS` overwrites it at the next boot.
* The console has no CEC setting and sends none (`mcad`, the console agent, never mentions CEC).

So UniFi's CEC feature looks unfinished: it powers the hardware but never starts the forwarding.
A future firmware may finish it; if so, check the sensor and turn this option off if you see
conflicts.

Whether GPIO 3 needs to be high for forwarding is **Unverified**: it was high on the bridges where
forwarding worked, because `no_cec=n` had been set with UniFi's switch during testing. If
forwarding runs but nothing reaches the TV on a fresh bridge, run
`ubus call hal cec '{"enable":true}'` on it once and restart it.

## Why a fix can't live on the bridge

* Root is `/dev/ram`, loaded from flash at boot (kernel command line `root=/dev/ram`). Every file
  change, including SSH `authorized_keys`, is lost on restart. **Verified**.
* What persists: the `astparam` settings in flash, and `/data` (ext4, used for Dante data, dropbear
  host keys, UniFi settings). **Observed**.
* Boot never runs a user script from either. It copies Dante configuration and checks a few flag
  files (`/data/mfg_mode`, `/data/no_IR`, ...). Getting code to run at boot would mean abusing those
  copies or sourced settings: fragile, and likely broken by a firmware update. Not done.

## Verified on hardware (2026-11-11)

One source (VDS-01, Apple TV on its input) and one receiver (VDS-04, LG TV on its output), console
Network app 11.1.47, bridge firmware 1.0.11.1297:

* Before: Apple TV on and off did nothing to the TV. The source bridge received the Apple TV's
  `Standby` (`4F 36`) and `Report Power Status` but couldn't send them (`sk_net_tx_frame is NULL`).
* After the steps above: the TV followed the Apple TV on and off. The receiver's kernel log showed
  messages arriving over the network (`CEC:NET Tx ...`) and the TV's replies going back.
* The source sees the Apple TV as logical address 4 (Player 1, physical address 1.0.0.0); the
  receiver sees the TV at 0 (TV) and reports itself at physical address 3.0.0.0.
* `cec_bridge.sh` itself: first run on each bridge answered `started`, a second run `ok` with no
  change; the integration's manager code, run against both bridges, reported both `linked`.

Not tried: several receivers on one source (multicast is how the SDK does it, so it should work);
a second source; TV remote buttons controlling the Apple TV through the bridges (the forward rule
allows it); other TV brands; a firmware update.

Not verified: SSH **password** login from Home Assistant to a real bridge (tests used a local SSH
server; the hardware run used a key). The bridge runs dropbear, which accepts passwords.

## Security notes

* The SSH password is stored in Home Assistant's config entry, like the console password.
* The bridge's SSH host key is pinned the first time Home Assistant connects (per bridge, for as
  long as Home Assistant runs). A different key later is refused with an error on the sensor. The
  bridges keep their host keys in `/data`, so a key only changes after a reset; reload the
  integration then.
* Home Assistant's own SSH keys are never offered; only the password is used.
* The bridges run everything as root; the script stops and starts only the CEC parts of the SDK.

## Undoing it

* Turn **CEC forwarding** off in the options. The integration stops logging in. What's running on
  the bridges keeps running until they restart.
* To stop it at once, restart the bridges (UniFi Network, device, Restart), or run the script's
  `off` mode over SSH:

  ```bash
  ssh <user>@<bridge> 'bash -s -- off' < custom_components/unifi_eav_bridge/cec_bridge.sh
  ```

* To undo UniFi's own switch as well: `ubus call hal cec '{"enable":false}'` on the bridge.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Sensor `error`, detail mentions username or password | The SSH login in UniFi Network (Device SSH Authentication) and in the options |
| Sensor `error`, `SSH to ... failed` | Home Assistant can't reach TCP 22 on the bridge (firewall, VLAN, IPS block) |
| Sensor `error`, `host key ... changed` | The bridge was reset; reload the integration |
| Source stays `waiting` | No receiver attached: check the receivers' sensors |
| Receiver `error`, `source_did_not_answer` | The source isn't running CEC on that channel; check its sensor and `detail` |
| Everything `linked` but the TV ignores the source | CEC off on the TV or source device; GPIO note above; check the receiver's kernel log with CEC debug on |

On a bridge (as root), useful read-only checks:

```bash
cat /sys/devices/platform/cec/topology          # devices seen on this bridge's HDMI port
head -1 /sys/devices/platform/cec/forward_rule  # 0xFF = pass through
netstat -anu | grep -E '5980[01]|59008'         # CEC data (59800/59801) and heartbeat (59008)
cat /var/log/ha_cec_lm.log                      # the link manager started by the script
echo 0 > /sys/devices/platform/cec/debug_level  # trace every message to dmesg (2 = default)
dmesg | grep CEC:
```
