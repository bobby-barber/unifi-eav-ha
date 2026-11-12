# Documentation

Everything learned about Ubiquiti EAV-Bridge devices while building this integration. The
bridges have no public documentation, so most of this was found by observation on real hardware.

| Page | What's in it |
|---|---|
| [capabilities.md](capabilities.md) | What you can do with the bridges and this integration, and how well each thing is proven |
| [console-api.md](console-api.md) | The UniFi console endpoints used for status, routing, and snapshots, with payloads |
| [bridge-internals.md](bridge-internals.md) | What runs on a bridge: hardware, services, ports, status files, states |
| [preview-quality.md](preview-quality.md) | How the preview video works, how to raise its quality, and the measured results |
| [measurements.md](measurements.md) | Every number we measured: loads, bandwidth, latency, timings |
| [gotchas.md](gotchas.md) | Things that bit us, and how to avoid them |
| [open-questions.md](open-questions.md) | What we don't know yet, and ideas worth trying |

## How to read the claims

Each claim carries a confidence level:

* **Verified**: tried on real hardware, result confirmed.
* **Observed**: seen once or twice, or read from the device without being exercised.
* **Unverified**: reported by someone else, or inferred, and not yet checked on our hardware.

## Test setup behind these notes

* One UniFi Cloud Key Gen2 Plus console running the Network app.
* Four EAV-Bridges: two sources and two receivers. Most measurements were taken with one source
  (an Apple TV) and one receiver (a projector) on one route; the role-change tests used all four.
* Bridge firmware 1.0.11.1297 (system image A9.19.5, build 5341), Linux 4.9.0 on a single-core ARMv7.
* Home Assistant 2026.2.x for the integration tests.

Anything involving **several receivers on one source**, **several sources**, or **audio routing**
has not been tried. Addresses and MAC addresses in these pages are placeholders
(`192.0.2.x`, `02:00:00:00:00:xx`).

## Tools

* [`tools/measure_preview.py`](../tools/measure_preview.py): read-only HTTP check of a bridge's
  preview (snapshot size and latency, stream frame rate and bandwidth). No login, no changes.
