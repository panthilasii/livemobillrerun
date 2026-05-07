"""Fake-phone TCP client used to smoke-test the PC streamer.

Usage:

    python -m tools.fake_phone --host 127.0.0.1 --port 8888 \
        --duration 10 --out /tmp/vcam_capture.h264

It connects to the streamer, reads the H.264 byte stream for `--duration`
seconds, counts NAL units (Annex-B start codes), and optionally writes the
raw bytes to a file you can inspect with:

    ffprobe /tmp/vcam_capture.h264
    ffplay  /tmp/vcam_capture.h264

Exit status:
    0 — at least one NAL unit was received
    1 — connection failed
    2 — connected but received zero bytes (FFmpeg likely failed to spawn)
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

START_CODE = b"\x00\x00\x00\x01"
SHORT_START = b"\x00\x00\x01"


def _count_nal_units(buf: bytes) -> int:
    """Annex-B NAL boundary count (cheap, may double-count emulation bytes)."""
    return buf.count(START_CODE) + buf.count(SHORT_START)


def run(host: str, port: int, duration: float, out_path: Path | None) -> int:
    print(f"[fake_phone] connecting to {host}:{port} …")
    try:
        sock = socket.create_connection((host, port), timeout=5.0)
    except OSError as e:
        print(f"[fake_phone] connect failed: {e}", file=sys.stderr)
        return 1

    sock.settimeout(2.0)
    print(f"[fake_phone] connected, capturing for {duration:.1f}s")

    out_fp = open(out_path, "wb") if out_path else None
    total_bytes = 0
    total_nals = 0
    deadline = time.monotonic() + duration
    last_log = time.monotonic()

    try:
        while time.monotonic() < deadline:
            try:
                buf = sock.recv(64 * 1024)
            except socket.timeout:
                continue
            if not buf:
                print("[fake_phone] server closed connection")
                break
            total_bytes += len(buf)
            total_nals += _count_nal_units(buf)
            if out_fp:
                out_fp.write(buf)
            now = time.monotonic()
            if now - last_log >= 1.0:
                kbps = (total_bytes * 8 / 1024) / max(1.0, now - (deadline - duration))
                print(
                    f"[fake_phone] {total_bytes/1024:7.1f} KiB  "
                    f"NALs={total_nals:5d}  ~{kbps:6.0f} kbps"
                )
                last_log = now
    finally:
        try:
            sock.close()
        except OSError:
            pass
        if out_fp:
            out_fp.close()

    print(f"[fake_phone] done: {total_bytes} bytes, {total_nals} NAL units")
    if out_path:
        print(f"[fake_phone] saved → {out_path}")
        print(f"[fake_phone] verify with: ffprobe {out_path}")

    if total_bytes == 0:
        return 2
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="fake_phone")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8888)
    p.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="seconds to capture (default 10)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="optional file path to save raw H.264 bytes",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return run(args.host, args.port, args.duration, args.out)


if __name__ == "__main__":
    sys.exit(main())
