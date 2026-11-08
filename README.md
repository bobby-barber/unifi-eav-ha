# UniFi EAV Bridge Status (Home Assistant)

Reads the live HDMI state of Ubiquiti **EAV-Bridge** devices from your UniFi console, so
automations can react to things like *"the receiver is waiting for a source"*.

Why it exists: the bridges have no public API, but each one reports its HDMI ports to the
UniFi Network app, and the console exposes that data. This integration polls the console
(no SSH to the bridges, nothing installed on them).

## Entities

For each bridge, depending on its role:

| Entity | On a... | Meaning |
|---|---|---|
| `binary_sensor.<bridge>_online` | any bridge | Console sees the bridge as connected |
| `binary_sensor.<source>_source_signal` | source (host) | A device (Apple TV etc.) is feeding the HDMI input |
| `binary_sensor.<receiver>_waiting_for_source` | receiver (client) | Bridge is showing "Waiting for video input..." |
| `sensor.<receiver>_hdmi_output_state` | receiver (client) | The firmware's raw state string (e.g. `WAITING_HOST_MODE`) |

`waiting_for_source` also carries attributes: `source_device`, `source_signal` (looks up the
transmitter on the same channel), `output_state`, `channel`.

## Install

**With HACS (recommended)**

1. In Home Assistant open **HACS**, click the three dots (top right), then **Custom repositories**.
2. Paste this repository's URL, set the category to **Integration**, and click **Add**.
3. Find **UniFi EAV Bridge Status** in HACS, click **Download**, then restart Home Assistant.
4. Go to Settings, Devices & services, Add integration, **UniFi EAV Bridge Status**.
5. Enter the console address (e.g. `192.168.1.2`), a **local** UniFi OS account
   (not a Ubiquiti cloud login), and its password. Leave "Verify HTTPS certificate" off for
   the console's self-signed certificate.

**Manually:** copy `custom_components/unifi_eav` into `<config>/custom_components/`, restart
Home Assistant, then do step 4 and 5 above.

The account only needs to read device status. A read-only role is enough.

If Home Assistant sits on a different VLAN than the console, allow it through the firewall
to the console on 443.

## Example: turn the projector off when the source goes away

```yaml
alias: Projector off when source is off
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.vds_02_projector_waiting_for_source
    to: "on"
    for: "00:02:00"        # ignore short drops, e.g. the Apple TV waking up
actions:
  - action: switch.turn_off      # use whatever turns YOUR projector off
    target:
      entity_id: switch.projector
```

And the reverse, turning it on when the source comes back:

```yaml
alias: Projector on when source is on
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.vds_01_apple_tv_source_signal
    to: "on"
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.projector
```

Entity IDs come from your bridge names in UniFi, so check yours under Developer tools, States.

## Tests

```bash
python tests/test_api.py     # needs aiohttp; runs against a mock console
```

## Notes and limits

- The console API used here is not officially documented, so a UniFi update could change it.
- Polling every 15 seconds by default (adjustable in the integration's options).
- Don't log into the bridges over SSH in a tight loop: the gateway's intrusion prevention can
  block you. This integration never touches the bridges directly.
