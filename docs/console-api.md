# Console API

The EAV-Bridges have no API of their own that the console exposes. Everything goes through the
UniFi OS console's Network app. These endpoints are **undocumented**; a UniFi update can change
them. Confidence labels follow the [README](README.md).

Base: `https://<console>` (self-signed certificate). `<site>` is `default` unless you renamed it.

## Authentication

`POST /api/auth/login` with JSON `{"username": "...", "password": "...", "rememberMe": false}`.

* **Success (200)**: sets a `TOKEN` cookie and returns an `X-CSRF-Token` response header.
  **Verified.**
* **Wrong credentials**: HTTP 401. Use a **local** UniFi OS account; cloud/SSO logins don't work.
* **Later requests**: send the `TOKEN` cookie. **Writes also need** `X-CSRF-Token: <token>`; reads
  worked without it. The console rotates the token via an `X-Updated-CSRF-Token` header; use the
  latest one.
* **Session expiry**: a request returns 401. Log in again once and retry. **Verified.**

> Home Assistant's HTTP sessions **drop cookies for hosts addressed by bare IP**, so send the
> `Cookie` header yourself. Not doing so makes every call after login fail with 401. See
> [gotchas.md](gotchas.md#home-assistant-drops-cookies-for-ip-addresses).

## Reading

### Device status

`GET /proxy/network/api/s/<site>/stat/device` returns `{"meta": {...}, "data": [ ... ]}`. An EAV
bridge is any entry carrying `hdmi_ports`. Fields seen (**Verified**):

```json
{
  "name": "Source", "mac": "02:00:00:00:00:01", "state": 1, "ip": "192.0.2.21", "uptime": 13621,
  "hdmi_ports": [
    {"port_index": "1", "type": "in",  "up": true,  "connected": false,
     "resolution": "auto", "refresh_rate": "30", "color_format": "rgb"},
    {"port_index": "2", "type": "out", "up": false, "connected": false,
     "resolution": "auto", "refresh_rate": "30", "color_format": "rgb"}
  ],
  "video_info": {"channel": 2, "enabled": true, "role": "host", "refresh_rate": "60"}
}
```

A receiver's `video_info` additionally carried `role: "client"`, `color_format`, `resolution`,
`mode` (`unicast`), `audio_enabled`, `audio_mode`, and `tvwall_*` placement fields; its output
port carries the extra **`state`** string (see
[bridge-internals](bridge-internals.md#receiver-output-states)).

| Field | Meaning |
|---|---|
| `state` | `1` = connected to the console |
| `hdmi_ports[].type` | `in` or `out` |
| `hdmi_ports[].connected` | a device is plugged in and signalling on that port |
| `hdmi_ports[].up` | port is active. Seen `true` on a source's input and `false` on a receiver's output while it was waiting; not checked in other states |
| `hdmi_ports[].resolution`, `refresh_rate`, `color_format` | **configured settings**, not live measurements |
| `ip` | the bridge's own address (used for previews) |
| `video_info.role` | `host` = source, `client` = receiver |
| `video_info.channel` | route channel |

The bridge's own report also contains `system-stats` (`cpu`, `mem`, `uptime`) and `sys_stats`
(`loadavg_*`, memory). Whether the console passes these through its API was **not checked**.

### Routing matrix

`GET /proxy/network/v2/api/site/<site>/proav/video/matrix` (**Verified**):

```json
{
  "bridges": ["<mac>", "<mac>"],
  "hosts":   ["<source mac>"],
  "clients": ["<receiver mac>"],
  "host_candidates": [], "client_candidates": [],
  "groups": [{
    "id": "<uuid>", "name": "Projector", "type": "VIDEO", "channel": 2, "enabled": true,
    "host": {"mac": "<source mac>", "host_type": "VIDEO", "refresh_rate": "FPS60", "enabled": true},
    "clients": [{
      "mac": "<receiver mac>", "client_type": "VIDEO", "mode": "UNICAST",
      "resolution": "AUTO", "color_format": "YCBCR444",
      "audio_enabled": true, "audio_mode": "AUTO", "enabled": true
    }]
  }]
}
```

A **group** is one source plus the receivers watching it. A receiver is in at most one group, and a
source has at most one group. There is also an audio matrix at `proav/audio/matrix` (reported by
other projects; **Unverified** here).

## Writing

Base for all: `/proxy/network/v2/api/site/<site>/proav/video/group`. Requires the CSRF header and
an account that can manage the Network app (otherwise **403 `{"errorCode":403,"message":"No permission"}`**).

| Action | Request | Confidence |
|---|---|---|
| Update a group | `PUT .../group/<id>` with the **full group object** as read, with changes applied | **Verified** (route on/off) |
| Create a group | `POST .../group` with `name`, `type: "VIDEO"`, `host`, `clients` (no `id`/`channel`; the console assigns them) | **Verified** (one receiver, one source) |
| Delete a group | `DELETE .../group/<id>` | **Verified** (one receiver) |

Rules this integration follows (design notes from other projects, matched by behavior here):

1. Re-read the matrix right before planning a change, so outside changes are respected.
2. A receiver leaves its old group first (it cannot be in two), then joins or creates the target group.
3. If the old group has no receivers left, delete it.
4. `mode` is `MULTICAST` when a group has 2 or more receivers, `UNICAST` for one. Recompute on every group touched. The multi-receiver case is **Unverified**.
5. A recreated group gets a new `id` and `channel`, and the integration names it after the source,
   so a group you named by hand (e.g. "Projector") is renamed by a disconnect-then-reconnect cycle.

## Snapshot (fallback)

`GET .../proav/snapshot/<mac>` is reported to return a JPEG of a source bridge's input. The
integration uses it only if a bridge's own preview port can't be reached. **Unverified** on this
hardware.

## What the console logs

The Network app's `server.log` showed adoption and PoE-validation events for the bridges
(for example `UPLINK_NOT_SUPPLYING_POWER` warnings) but **nothing about HDMI or signal state**.

## Not found

* An endpoint to change a bridge's role (source/receiver).
* Live source resolution or audio format through the console.
* Any official or documented API for these devices.
