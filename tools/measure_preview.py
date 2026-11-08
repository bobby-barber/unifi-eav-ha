#!/usr/bin/env python3
"""Measure an EAV-Bridge's preview: snapshot size/latency and live-stream rate.

Read-only: it only issues HTTP GETs to the bridge's unauthenticated preview port (8080).
It does not log in, change settings, or touch the console.

    python3 tools/measure_preview.py 192.0.2.21            # one bridge, 5 s of stream
    python3 tools/measure_preview.py 192.0.2.21 192.0.2.31 --seconds 10

Standard library only. Reports, per bridge:
  * snapshot: HTTP status, bytes, time to first byte and total, JPEG dimensions
  * stream:   frames per second, Mbit/s, and the dimensions of the first frame
"""

from __future__ import annotations

import argparse
import socket
import struct
import time
import urllib.request

SOI = b"\xff\xd8\xff"
EOI = b"\xff\xd9"


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a JPEG's start-of-frame marker."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + seg
    return None


def snapshot(host: str, port: int) -> str:
    url = f"http://{host}:{port}/?action=snapshot"
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            first = r.read(1)
            ttfb = time.monotonic() - t0
            body = first + r.read()
            status = r.status
    except OSError as err:
        return f"  snapshot: FAILED ({err})"
    total = time.monotonic() - t0
    dims = jpeg_size(body)
    shape = f"{dims[0]}x{dims[1]}" if dims else "unknown size"
    return (f"  snapshot: HTTP {status}, {shape}, {len(body) / 1024:.0f} KiB, "
            f"first byte {ttfb * 1000:.0f} ms, total {total * 1000:.0f} ms")


def stream(host: str, port: int, seconds: float) -> str:
    url = f"http://{host}:{port}/?action=stream"
    buf = bytearray()
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            r.fp.raw._sock.settimeout(3)  # type: ignore[attr-defined]
            while time.monotonic() - t0 < seconds:
                try:
                    chunk = r.read1(65536)
                except (TimeoutError, socket.timeout):
                    break
                if not chunk:
                    break
                buf += chunk
    except OSError as err:
        return f"  stream:   FAILED ({err})"
    elapsed = max(time.monotonic() - t0, 0.001)
    frames = buf.count(SOI)
    first = buf.find(SOI)
    end = buf.find(EOI, first + 500) if first >= 0 else -1
    dims = jpeg_size(bytes(buf[first : end + 2])) if end > 0 else None
    shape = f"{dims[0]}x{dims[1]}" if dims else "unknown size"
    return (f"  stream:   {shape}, ~{frames / elapsed:.1f} fps, "
            f"{len(buf) * 8 / elapsed / 1e6:.1f} Mbit/s over {elapsed:.0f} s")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("hosts", nargs="+", help="bridge IP address(es)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--seconds", type=float, default=5.0, help="stream sample length")
    args = ap.parse_args()
    for host in args.hosts:
        print(host)
        print(snapshot(host, args.port))
        print(stream(host, args.port, args.seconds))


if __name__ == "__main__":
    main()
