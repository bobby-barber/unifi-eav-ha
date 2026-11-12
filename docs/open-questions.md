# Open questions and ideas

What we don't know yet, in rough priority order, and things worth trying.

## Unknowns

1. **Several receivers on one source (multicast).** The planner handles it (2 or more receivers in a
   group use mode `MULTICAST`, one uses `UNICAST`), and it passes against a mock console, but it has
   not been tried on real hardware because the test system has one receiver.
2. **Moving a receiver between two sources.** Needs a second source bridge.
3. **Why the live stream's frame rate varies.** At 1280×720 we saw about 15 fps in most runs, but
   about 29 fps right after a live change, about 13 fps on the source with video playing, and
   about 23 fps on the receiver with video playing. Suspects: picture content (still vs motion),
   whether the setting was applied live or at boot, and the bandwidth cap. Needs a controlled
   test with a fixed source on both bridges.
4. **Source bridge CPU/load under preview viewers.** Not captured; see
   [measurements.md](measurements.md).
5. **Does a firmware update reset saved preview settings?** Unknown. Re-check `astparam g SS_STREAM_OPTION`
   after updating.
6. **Does the console ever overwrite `SS_*` settings?** It did not during our reboot test.
7. **Why a role change rearranges other bridges and routes.** The call works (see
   [console-api.md](console-api.md#changing-a-bridges-role-source-or-receiver)), but flipping one bridge
   also flipped its paired bridge and rebuilt routes. Is that the console's pairing logic, or a side
   effect of provisioning? Would changing a bridge that is in **no** route (and has no paired unit)
   behave? Does the firmware actually get swapped, or only the boot mode?
8. **Audio routing.** Audio has its own matrix (`proav/audio/matrix`, reported by other projects).
   Not tried.
9. **Console snapshot endpoint** (`proav/snapshot/<mac>`). Used as a fallback, never exercised on
   real hardware.
10. **Receiver `up` flag on the output port.** Seen `false` while waiting; not checked in other states.
11. **The "USB" and "IR" ports.** The store page says configuration is "coming in a future update".

## Ideas worth trying

* **Bridge CPU, memory and uptime sensors in Home Assistant.** The bridge's own status report
  includes `system-stats` and `sys_stats`. If the console passes them through `stat/device`, they
  could become sensors with no SSH at all. Check that field first.
* **Live resolution and audio format of a source.** `/tmp/video_status` on a source has them. They
  are not in the console's data, so surfacing them would need device access, which the integration
  deliberately avoids. A separate, optional helper could do it.
* **A "preview quality" option in the integration.** Today quality is set on the bridge by hand.
  An optional one-time setup action could write the saved settings, with clear warnings.
* **Wake-aware automations.** The receiver states give a clean "source went away" signal; a matching
  "source came back" trigger (Source signal on) could turn the projector on.
* **Scene support.** The select entity can already be driven from scenes; a ready-made "movie mode"
  example would help.
* **Capturing the real stream.** About 146 Mbit/s of proprietary video-over-IP per receiver. A
  mirror port plus reverse-engineering the format would be the route; the practical alternative is
  an HDMI splitter into a capture device.
