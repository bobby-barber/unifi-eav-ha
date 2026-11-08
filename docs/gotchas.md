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
connection** (`ControlMaster`/`ControlPersist`) instead of one per command. Home Assistant itself
never SSHes into a bridge, so the integration does not trigger this.

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
* `timeout 15 cmd` does not work the GNU way; there is no `uptime`, `pidof`, or `id`.
* `df -h` is rejected; use plain `df`.

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
