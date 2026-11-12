# Gotchas

Things that went wrong while building and testing this, with the cause and the fix. All
**Verified** unless marked.

## Home Assistant drops cookies for IP addresses

**Symptom:** the config flow says "rejected that username or password" although the credentials are
right. **Cause:** login succeeds, but Home Assistant's HTTP session does not keep cookies for a
host addressed by bare IP, so the next request has no session and returns 401; a naive client
reports that as bad credentials. **Fix:** read the `TOKEN` cookie from the login response and send
it in a `Cookie` header yourself. The test suite uses a cookie jar that discards cookies so this
cannot regress.

## A read-only UniFi account cannot change routes

**Symptom:** `HTTP 403 {"errorCode":403,"message":"No permission"}` when toggling a route.
**Cause:** the account can read the Network app but not manage it. **Fix:** give a **local-only**
account a role that can manage the Network app (Admins & Users). Reading status and previews works
without it.

## The gateway's intrusion prevention can block SSH to the bridges

**Symptom:** SSH to a bridge (or the console) works a few times, then every connection times out
silently, while HTTP to the same device still works, and another machine can still connect.
**Cause:** the UniFi gateway's IPS flagged many quick SSH logins as an "SSH scan"
(Emerging Threats signature 2003068, "ET SCAN Potential SSH Scan OUTBOUND") and blocked the source.
**Fix:** add a detection exclusion for the machine that does the probing, and use **one SSH
connection** (`ControlMaster`/`ControlPersist`) instead of one per command. Home Assistant only
SSHes into the bridges with **CEC forwarding** on, and then sparingly: one bridge at a time, only
when something changed, otherwise once per bridge every 30 minutes ([cec.md](cec.md#when-the-integration-logs-in)).

## SSH keys added to a bridge vanish on reboot

The bridge keeps its SSH home in memory, so an `authorized_keys` file added by hand is gone after a
restart. Settings saved with `astparam save` do survive. Plan for this if you rely on a key.

## dropbear refuses key login for fixable reasons

* The home directory must not be group- or world-writable. `chmod 755 /root` fixed a rejected key
  here.
* The login account needs a real shell. The default `root` account had an invalid shell entry on
  the units we saw, so the account configured through the UniFi device-authentication settings
  (another user with UID 0) was the one that worked. Check `/etc/passwd` (names and shells only;
  don't paste password hashes).

## `e` and `astparam` are not on PATH in a non-interactive SSH session

Running `ssh bridge e e_ss::...` printed `ipc: command not found` and changed nothing. Add
`/usr/local/bin` to `PATH` first: `ssh bridge 'export PATH=$PATH:/usr/local/bin; e ...'`.

## BusyBox quirks

* `tail -2` is rejected; use `tail -n 2`. Piping a command into a failing `tail` can cut the
  command short, so run `astparam save` **unpiped**.
* `timeout 15 cmd` works for real programs (`timeout 2 sleep 5` is killed after 2 s) but not for
  shell builtins: `timeout 5 echo hi` fails with "can't execute 'echo'". There is no `uptime`,
  `pidof`, `nohup`, `setsid` or `id`.
* `df -h` is rejected; use plain `df`.

## Changing a role can rearrange your routes

Changing a bridge between source and receiver is one API call, but the console did more than we asked.
Turning a receiver (paired with a source) into a source also turned that source into a receiver and
put both into the first source's route, which then fed three receivers. Turning the receiver back left
the other bridge as a receiver. We rebuilt the routes by hand afterwards. Also expect: the bridge is
busy for about 3 to 5 minutes, its SSH key (kept in memory) disappears, and the console record and the
bridge's own report disagree until it finishes. Do it deliberately, on an idle system, one bridge at a
time, and check your routes afterwards. The integration refuses to start one while any bridge is busy.

## Lost dialogue on 5.1 sources

**Symptom:** some programs play with music and crowd noise but no commentary or dialogue, on some
rooms only, while the same program is fine on a TV connected straight to the source.
**Cause:** the program's audio is 5.1 with the voices only in the **center channel**, and the room's
TV or soundbar can only take stereo. A Sonos Playbar (1st gen) connected by **optical** can't take
multichannel PCM at all, and a small HDMI monitor with its own speakers can't either; both
downmixed the 5.1 by dropping the center channel. Programs broadcast in stereo were fine, which is
why it looked random. **Verified** (2026-11-11): forcing the Apple TV to stereo brought the voices
back in both problem rooms, and the Sonos app showed a different audio format in the problem room
than in a working 7.1 room on the same source.
**Why the source sends 5.1 anyway:** a source bridge shows the Apple TV its own fixed EDID (named
`EAVBRIDGE`: PCM up to 8 channels, Dolby Digital, Dolby Digital Plus, DTS, DTS-HD, TrueHD),
whatever is connected to any receiver, so the source picks surround. The bridge's setting to copy a
receiver's EDID instead (`ENABLE_REMOTE_EDID`) is off, and it could only follow one receiver anyway.
**What did not help:** a receiver's **audio mode** set to **PCM** (the `Audio mode` select). It
does not downmix to stereo.
**What helps:** set the source device to stereo (Apple TV: Settings > Video and Audio > Audio
Format), or put a downmixing HDMI audio extractor in front of the stereo-only TV or soundbar, or
connect the soundbar by HDMI (ARC/eARC) instead of optical.

## Use a source bridge as a source only

A source bridge has an HDMI output too, and it's tempting to use it as a pass-through to a nearby
TV. Don't if you can avoid it: in the setup above, audio and CEC problems were much harder to
pin down with a pass-through in the chain, and removing it fixed the rest. Let a source bridge only
take a source in, and use a receiver bridge for every display. (The source's EDID comes from the
bridge itself either way; unplugging the pass-through changed nothing there. **Verified**.)

## UniFi's own CEC switch doesn't start CEC

`ubus call hal cec '{"enable":true}'` on a bridge, and setting `NO_CEC=n` in flash, both leave CEC
forwarding off after a restart: `/etc/init.d/rcS` forces `NO_CEC=y` at every boot, and UniFi's CEC
startup fails because the driver isn't loaded yet. See [cec.md](cec.md#why-unifis-firmware-doesnt-do-this-itself)
for the full chain and how the integration works around it.

## The SDK's CEC service ignores `kill`

The bridge's CEC link manager (`link_mgrh_cec.sh` / `link_mgrc_cec.sh`) traps `SIGTERM`, so `kill`
leaves it running. A second copy started next to it then shares the same message queue, and
sessions fail with "Ignore. session ID doesn't match". Use `kill -9`, and run one copy only.

## Newer asyncssh needs a newer `cryptography` than Home Assistant ships

CEC forwarding uses `asyncssh`. asyncssh 2.24 requires `cryptography` 48 or later, while Home
Assistant 2026.2 pins `cryptography` 46.0.5. Installing the newest asyncssh in a Home Assistant test
environment upgraded `cryptography` and broke the pin. Home Assistant installs integration
requirements under its own pins, so it picks a compatible asyncssh (2.23.1 here) by itself; for a
test environment, install `asyncssh` together with `cryptography==<Home Assistant's version>`.

## Domain name clash

Two other EAV projects use the Home Assistant domain `unifi_eav`. This one uses
`unifi_eav_bridge` so they can coexist. If you installed an early version, delete the old
`custom_components/unifi_eav` folder, or two entries with similar names appear.

## Platform differences we hit

* On macOS with zsh, `read -p "prompt" VAR` does not work; use `read -s "VAR?prompt"`.
* `pytest-homeassistant-custom-component` blocks real sockets by default; tests that start a mock
  server need the `socket_enabled` fixture, and the camera tests need `PyTurboJPEG` installed.
* Home Assistant refreshes coordinators in a **background task**, so tests must wait with
  `async_block_till_done(wait_background_tasks=True)`.

## Preview caveats

* A preview snapshot of an empty input is a tiny blank frame (about 650 bytes), so a very small
  file means "no signal", not "broken".
* The first measurement after a live setting change can differ from steady state (see
  [open-questions.md](open-questions.md)).
* Higher-quality live views cost network bandwidth per viewer (10–25 Mbit/s); don't leave many open.
