# Preview quality

How a bridge makes its preview picture, what can be changed, and what we measured. Confidence
labels follow the [README](README.md). Numbers are from one source and one receiver; see
[measurements.md](measurements.md) for the full set.

## How it works (Verified)

Each bridge runs **MJPG-Streamer** on port 8080 with two separate inputs fed by the hardware
scaler, a "sub-stream", independent of the real video path:

```
mjpg_streamer -i "input_substream.so -d /dev/stream  -o <stream option>"
              -i "input_substream.so -d /dev/videoip -o <snapshot option>"
              -o "output_http.so -p 8080"
```

| URL | Source | Default option | Meaning |
|---|---|---|---|
| `/?action=snapshot` | `/dev/videoip` | `1024,576,60,0` | width, height, JPEG quality, AS |
| `/?action=stream`   | `/dev/stream`  | `640,360,30,8000,0,60` | width, height, fps, bandwidth cap (kbps), AS, MQ |

The defaults apply when the `SS_SNAPSHOT_OPTION` / `SS_STREAM_OPTION` settings are undefined,
as they are on a fresh bridge. (`AS` is not understood; leave it `0`. `MQ` appears to be motion
quality; 60 to 80 was fine.) The preview needs no login, which is why Home Assistant can show it
without credentials.

## Changing it while running (Verified)

Send an event to the link manager. It applies at once and is **not saved**:

```sh
export PATH=$PATH:/usr/local/bin          # needed in a non-interactive SSH session
e e_ss::config::snapshot::1280,720,90,0
e e_ss::config::stream::1280,720,15,16000,0,70
```

To go back: send the default option strings above, or reboot.

## Making it permanent (Verified)

```sh
astparam s SS_SNAPSHOT_OPTION 1280,720,90,0
astparam s SS_STREAM_OPTION   1280,720,15,16000,0,70
astparam save                 # do not pipe its output into tail; busybox tail rejected the flag
```

Read back from the flash parameter partition to confirm
(`strings /dev/mtdblkparam | grep ^SS_`). **Survived a reboot** on the source bridge: the preview
came back at 1280×720. The receiver was set the same way and checked from the partition, but
not rebooted. To undo, clear both settings and save (or factory reset). Whether a firmware update
clears them is **Unverified**.

## What the hardware allows

### Snapshot (Verified)

| Requested | Delivered | Size | Latency |
|---|---|---|---|
| 1024×576 q60 (default) | 1024×576 | 54 KB | 0.05–0.09 s |
| 1280×720 q85 | 1280×720 | 116 KB | 0.06–0.09 s |
| 1280×720 q90 | 1280×720 | 166 KB | about 0.08 s |
| 1280×720 q95 | 1280×720 | 231 KB | 0.07–0.10 s |
| 1280×720 q100 | 1280×720 | 488 KB | 0.11 s |
| 1600×900 q90 | **1280×720** | 166 KB | 0.08 s |
| 1920×1080 q85–95 | **1280×720** | 116–231 KB | 0.06–0.10 s |

**The ceiling is 1280×720.** Larger requests are silently clamped to it. Quality 90 to 95 is the
sweet spot; 100 roughly doubles the size for little visible gain. With nothing on the input the
snapshot is a tiny blank frame (about 650 bytes).

### Live stream (Verified)

| Configured (w,h,fps,bw,as,mq) | Delivered | Frame rate | Bandwidth | Conditions |
|---|---|---|---|---|
| 640,360,30,8000,0,60 (default) | 640×360 | 29.4 fps | 7.1 Mbit/s | still picture |
| 960,540,30,8000,0,60 | 960×540 | about 15 fps | 6.0 Mbit/s | still picture |
| 960,540,30,16000,0,70 | 960×540 | **29.9 fps** | 13.9 Mbit/s | still picture |
| 800,450,30,12000,0,70 | 800×450 | 30.0 fps | 10.8 Mbit/s | still picture |
| 1280,720,30,12000,0,60 | 1280×720 | about 15 fps | 9.7 Mbit/s | still picture |
| 1280,720,30,24000,0,80 | 1280×720 | 14.9 fps | 13.8 Mbit/s | still picture |
| 1280,720,24,16000,0,70 | 1280×720 | 15.0 fps | 11.3 Mbit/s | still picture |
| 1280,720,15,16000,0,70, set live | 1280×720 | about 29 fps | about 27 Mbit/s | still picture |
| same, **saved and rebooted** | 1280×720 | 15.3 fps | 13.8 Mbit/s | still picture |
| same, video playing (source) | 1280×720 | about 13 fps | 12 Mbit/s | video playing |
| same, video playing (receiver, set live then saved) | 1280×720 | about 23 fps | 24 Mbit/s | video playing |

What this shows:

* **960×540 holds 30 fps** if its bandwidth cap is raised to about 16 Mbit/s. The default 8 Mbit/s
  cap is what held it to 15 fps.
* **1280×720 reached about 15 fps** in most runs whatever fps or bandwidth was requested, but
  some runs (set live, or the receiver) gave 23 to 29 fps. **We do not understand why.** The stream
  rate may depend on the picture content, on whether the setting was applied live or at boot, or
  on both. See [open-questions.md](open-questions.md).
* The picture is clearly sharper at 720p than at the default 640×360, which is the 1/3-scale block
  the firmware's own comment describes.

## Suggested settings

| Goal | Snapshot | Stream |
|---|---|---|
| Better thumbnails, same load | `1280,720,90,0` | leave the default |
| Smooth live view | `1280,720,90,0` | `960,540,30,16000,0,70` |
| Sharpest live view | `1280,720,90,0` | `1280,720,15,16000,0,70` |

Keep the number of simultaneous live viewers modest; see [measurements.md](measurements.md#preview-viewers-and-load).

## Home Assistant side

The integration's camera entity reads these URLs directly, reuses a picture for the configured
refresh time (default 3 s), refreshes immediately when a bridge's signal or routing changes, passes
the live stream through, and falls back to the console's snapshot if the bridge is unreachable.
Quality is set **on the bridge**; the integration does not change it.

You can check what a bridge currently serves with:

```sh
python3 tools/measure_preview.py 192.0.2.21 192.0.2.31
```
