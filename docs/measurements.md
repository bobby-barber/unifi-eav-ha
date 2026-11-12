# Measurements

Every number measured so far. One source (Apple TV) and one receiver (projector) on one route,
unless stated. Confidence labels follow the [README](README.md).

## Timings (Verified)

| Event | Time |
|---|---|
| Route turned off, until the receiver reported `CLIENT_STATE_SUSPENDING` | about 15 s (one poll) |
| Route turned on, until `OPERATING` | about 15 s (one poll) |
| Source turned on, until "Waiting for source" cleared | within one poll |
| Reconnect (create route) in Home Assistant, until Output showed On | same second (the integration refreshes after a write) |
| Bridge `reboot` to answering on the network | about 59 s |
| Snapshot request on a bridge | 45–90 ms |
| Console poll interval used | 15 s (configurable, minimum 5 s) |

## Bridge hardware and load

| | Source (encoder) | Receiver (decoder) |
|---|---|---|
| Load average (range seen over several hours) | 3.4–4.4 | 2.3–4.2 |
| Memory | about 275 MB free of 480 MB | not measured |
| CPU busy, no preview viewers, video playing | not measured | **5%** (max 10%) |

A load average near 4 on a single core is **normal** for these devices, even idle. Changing the
preview quality did **not** change it in any run.

## Preview viewers and load

Video playing on the Apple TV; N simultaneous live preview viewers pulling the stream from each
bridge for 20 s at 1280×720 (receiver measured; see the gap below).

| Viewers | Receiver load | Receiver CPU busy | Data per viewer (receiver) | Data per viewer (source) |
|---|---|---|---|---|
| 0 | 3.94 | 5% (max 10%) | n/a | n/a |
| 1 | 4.16 | 13% (max 18%) | 25.2 Mbit/s | 13.0 Mbit/s |
| 2 | 3.97 | 19% (max 26%) | 24.3 Mbit/s each | 12.6 Mbit/s each |
| 3 | 3.55 | 23% (max 33%) | 22.8 Mbit/s each | 11.0 Mbit/s each |
| 0 again | 3.38 | 5% (max 8%) | n/a | n/a |

* CPU use rises about **6–7% per viewer** and falls straight back when they leave.
* Load average did not respond. Per-viewer data rates dip only slightly as viewers are added.
* **The source bridge's own load and CPU were not captured** (its SSH key had been wiped by an
  earlier reboot). It is the bridge that encodes the real video, so this remains a gap.
* The projector was **not checked for visible glitches** during the test; a check was requested
  and no answer was recorded.

## Network

| | Value |
|---|---|
| Real video arriving at a receiver | about **146 Mbit/s**, about 1,220 packets/s |
| Preview stream per viewer (720p, default-ish cap) | 11–25 Mbit/s depending on bridge and settings |
| Receiver `rx_drop` counter | about **18.6 per second with no viewers**, about 21 per second during the test: **no increase from preview viewers**. For scale, a source's counter read about 7,200 after 3.7 hours of uptime. |
| Errors (`rx_errs`, `tx_errs`) | 0 during the test |

## Preview file sizes

See [preview-quality.md](preview-quality.md#what-the-hardware-allows) for snapshot size by
quality and for the stream frame-rate and bandwidth table.

## Console and integration

| | Value |
|---|---|
| Requests per poll | 2 (`stat/device` and the routing matrix) |
| Integration tests | 52 (client, routing planner, Home Assistant entities, control, previews) |
| Deliberate-breakage checks | 6 breakages, all caught by the tests |

## Not measured

* Source bridge CPU and load under preview viewers.
* Anything with more than one receiver or more than one source.
* Several viewers on **both** bridges for longer than 20 s.
* Behavior of the real video stream while many preview viewers are active (judged by eye only).

## Role change (Verified, four bridges, console Network app 11.1.47)

| Event | Time |
|---|---|
| Console record shows the new `eav_bridge_role` after the `PUT` | immediately |
| Bridge busy (`state` 5) cycles until settled, first change (receiver to source) | about 170 s, two busy cycles |
| Same, changing it back | still cycling at 300 s (settled by about 5 minutes) |
| Same, another bridge (source to receiver) | about 320 s, three busy cycles |
| Firmware version after a role change | unchanged (1.0.17.1715) |
