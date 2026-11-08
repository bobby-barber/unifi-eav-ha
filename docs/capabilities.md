# Capabilities

What you can do with EAV-Bridges, through this integration or directly, and how well each is proven.
Legend: **Verified** / **Observed** / **Unverified** (see [README](README.md)).

## Read status

| Capability | How | Confidence |
|---|---|---|
| Is a source device (Apple TV etc.) feeding a bridge's HDMI input | `hdmi_ports[in].connected` in the console's device record | **Verified** |
| Is a receiver "Waiting for video input" | `hdmi_ports[out].state == "WAITING_HOST_MODE"` | **Verified** |
| Is a route suspended on purpose | `hdmi_ports[out].state == "CLIENT_STATE_SUSPENDING"` | **Verified** |
| Is video flowing to a receiver | `hdmi_ports[out].state == "OPERATING"` | **Verified** |
| Is a bridge connected to the console | device `state == 1` | **Verified** |
| Which source a receiver watches | the routing matrix groups | **Verified** |
| Detect changes quickly | polling every 15 s picked up every change within one poll | **Verified** |

The three receiver states are listed with timings in [bridge-internals.md](bridge-internals.md#receiver-output-states).

## Change routing (needs a UniFi account that can manage the Network app)

| Capability | How | Confidence |
|---|---|---|
| Turn a whole route on or off | `PUT` the group with `enabled` flipped | **Verified**: projector dropped to `CLIENT_STATE_SUSPENDING` and recovered to `OPERATING` |
| Turn one receiver's output on or off | `PUT` the group with that client's `enabled` flipped | **Observed**: accepted by the console; no visible effect was checked, since the route was off at the time |
| Disconnect a receiver | `DELETE` its group (when it is the only receiver), else `PUT` the group without it | **Verified** for the `DELETE` case; the multi-receiver `PUT` case is **Unverified** |
| Connect a receiver to a source | `POST` a new group, or `PUT` it into the source's existing group | **Verified** for `POST`; joining an existing multi-receiver group is **Unverified** |
| Move a receiver between sources | leave the old group, then join the new one | **Unverified** (needs two sources) |
| Multicast (one source to several receivers) | 2+ clients in one group, mode `MULTICAST` | **Unverified** (needs two receivers) |
| Change a bridge between source and receiver role | unknown | **Unverified**; no endpoint found |

A read-only account can see everything but gets **HTTP 403 `No permission`** on every write.

## Previews

| Capability | Confidence |
|---|---|
| Snapshot (JPEG) from any bridge with no login, port 8080 | **Verified** |
| Live MJPEG video from any bridge with no login, port 8080 | **Verified** |
| Snapshot up to **1280×720** (larger requests are clamped to this) | **Verified** |
| Live stream at 640×360, 800×450, 960×540, 1280×720 | **Verified** |
| 30 fps at 960×540 and below; about 15 fps at 1280×720 with a still picture (with video playing, 13–29 fps was seen) | **Verified**, frame-rate behavior not fully explained |
| Change quality at run time without a restart | **Verified** (device event system) |
| Make the change permanent across reboots | **Verified** (`astparam save`, tested on a reboot) |
| Console-side snapshot as a fallback | **Unverified** on this hardware (tested only against a mock) |

Details and numbers: [preview-quality.md](preview-quality.md).

## What is not possible (as far as we found)

* **Reading live resolution or audio format of a source through the console.** A bridge knows them
  (`/tmp/video_status` on the device), but the console's device record only carries the port flags.
* **Decoding the real video stream with standard tools.** It is a proprietary ASPEED video-over-IP
  format. See [bridge-internals.md](bridge-internals.md#the-real-video-stream).
* **Preview above 1280×720.** The hardware returned 1280×720 for 1600×900 and 1920×1080 requests.
