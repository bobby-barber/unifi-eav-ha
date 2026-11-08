# UniFi EAV Bridge for Home Assistant

Status, control and live previews for Ubiquiti **EAV-Bridge** (Pro AV) devices, through your
UniFi console. Nothing is installed on the bridges and Home Assistant never logs into them.

* **Status**: is a source on, is a receiver "Waiting for video input", is a bridge online.
* **Control**: choose which source each receiver watches, enable or disable routes. Off by default.
* **Previews**: a camera per bridge showing its HDMI input (sources) or screen (receivers),
  kept as fresh as the devices comfortably allow.

> The console API used here is undocumented and reverse-engineered. A UniFi update could change
> it. Reading, and every kind of write (update, create, delete a route), has been verified on real
> hardware with one source and one receiver. Routing **several receivers to one source** (multicast)
> is untested. Control starts in a log-only "dry run" mode so you can see what it would do first.

## Install

1. In Home Assistant open **HACS**, three dots (top right), **Custom repositories**.
2. Paste this repository's URL, category **Integration**, **Add**.
3. Find **UniFi EAV Bridge** in HACS, **Download**, then restart Home Assistant.
4. Settings, Devices & services, Add integration, **UniFi EAV Bridge**.
5. Enter your console address, a **local** UniFi OS account (not a Ubiquiti cloud login) and its
   password. Leave "Verify HTTPS certificate" off for the console's self-signed certificate.

Manual install: copy `custom_components/unifi_eav_bridge` into `<config>/custom_components/`.

Network notes: Home Assistant must reach the console on port 443. For previews it should also
reach each bridge on port 8080 (if it can't, previews fall back to the console's snapshot).
Don't SSH into the bridges in a loop; an intrusion-prevention system may block you.

## Entities

| Entity | On | Meaning |
|---|---|---|
| `binary_sensor.<bridge>_online` | every bridge | Console sees the bridge as connected |
| `binary_sensor.<source>_source_signal` | source | A device (Apple TV etc.) is feeding the HDMI input |
| `binary_sensor.<receiver>_waiting_for_source` | receiver | Showing "Waiting for video input..." |
| `sensor.<receiver>_hdmi_output_state` | receiver | Raw firmware state (e.g. `WAITING_HOST_MODE`) |
| `camera.<bridge>_preview` | every bridge | Picture from the bridge, plus a live stream |
| `select.<receiver>_source` | receiver | **Control.** Which source it watches, or `Disconnected` |
| `switch.<receiver>_output` | receiver | **Control.** Receiver's output on/off, keeps its source |
| `switch.<source>_route` | source | **Control.** Starts/stops the whole route from that source |

A receiver's `HDMI output state` has been seen as `OPERATING` (video flowing),
`WAITING_HOST_MODE` (source is off: "Waiting for video input") and `CLIENT_STATE_SUSPENDING`
(its route was disabled). "Waiting for source" is on only for `WAITING_HOST_MODE`, so turning a
route off on purpose does not look like a source going away.

`waiting_for_source` carries `source_device`, `source_signal`, `output_state`, `channel`.

## Control (routing)

Options, **Control**:

* **Off** (default): read-only. No control entities exist.
* **Dry run**: control entities exist, but nothing is sent. What *would* be sent is logged at
  WARNING level, fired as the `unifi_eav_bridge_dry_run` event, and shown in the select's
  `last_plan` attribute. **Start here**, try your automations, and read the plan.
* **Live**: changes are sent to the console.

How a route change works: a source has one "group" listing the receivers that watch it, and a
receiver is in only one group. Switching a receiver removes it from its old group (deleting the
group if it was the last receiver), then adds it to the new source's group (creating it if needed).
Groups with 2+ receivers use multicast, a single receiver uses unicast, and this is recomputed on
both groups. The routing table is re-read from the console right before every change, so changes
made in the UniFi app in the meantime are respected.

**Permissions:** changing routes needs a UniFi OS account that can manage the Network app. A
read-only account works for status and previews but the console answers changes with
`403 No permission`. Use a limited, local-only account with Network admin rights.

Not supported: changing a bridge between source and receiver (the console's way of doing that
isn't known yet).

### Example: switch the projector to the game console

```yaml
action: select.select_option
target:
  entity_id: select.vds_02_projector_source
data:
  option: Game Console
```

### Example: projector off when the source goes away

```yaml
alias: Projector off when source is off
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.vds_02_projector_waiting_for_source
    to: "on"
    for: "00:02:00"        # ignore short drops, e.g. the Apple TV waking up
actions:
  - action: switch.turn_off      # whatever turns YOUR projector off
    target:
      entity_id: switch.projector
```

## Previews

Each bridge serves a small MJPEG preview on port 8080 (no login). The camera:

* fetches a picture on demand and reuses it for **Preview refresh** seconds (default 3), so many
  viewers cost the bridge one request;
* throws the picture away immediately when the bridge's signal or routing changes, so a freshly
  switched source shows right away;
* passes the bridge's own live stream through for the camera's video view;
* falls back to the console's snapshot if the bridge can't be reached, and keeps the last good
  picture if everything is down.

`image_source` (`bridge` or `console`) shows where the last picture came from. Dashboards pull a
new picture about every 10 s with a picture card; use the live view for continuous video. The
bridges are small devices, so keep the refresh interval at a few seconds.

## Documentation

Everything we learned about the bridges, including the API, internals, preview tuning, and
measured numbers, is in [`docs/`](docs/README.md):

* [Capabilities](docs/capabilities.md): what works and how well it is proven
* [Console API](docs/console-api.md) and [bridge internals](docs/bridge-internals.md)
* [Preview quality](docs/preview-quality.md): raising the preview to 720p, with measurements
* [Measurements](docs/measurements.md), [gotchas](docs/gotchas.md), and [open questions](docs/open-questions.md)

## Tests

```bash
pip install pytest-homeassistant-custom-component PyTurboJPEG
pytest tests
```

52 tests run the integration inside Home Assistant against a mock console: login and cookies
(including a cookie jar that forgets cookies like Home Assistant's), routing plans, dry run versus
live writes, switches, previews, caching and fallbacks, and failure handling.
