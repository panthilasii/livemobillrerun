"""Entry point for NP Create / vcam-pc.

Usage::

    python -m src.main                        # Studio (customer UI, default)
    python -m src.main --legacy               # legacy diagnostic UI
    python -m src.main --cli --profile NAME   # headless streamer

In CLI mode, Ctrl+C stops cleanly.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

from .adb import AdbController
from .config import PROJECT_ROOT, ProfileLibrary, StreamConfig
from .health import HealthMonitor
from .playlist import list_videos, write_playlist
from .tcp_server import TcpStreamServer


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="npcreate")
    p.add_argument(
        "--studio",
        action="store_true",
        help="launch the NP Create customer UI (default)",
    )
    p.add_argument(
        "--legacy",
        action="store_true",
        help="launch the legacy diagnostic Tkinter panel",
    )
    p.add_argument(
        "--gui",
        action="store_true",
        help="alias for --legacy (kept for back-compat with old scripts)",
    )
    p.add_argument("--cli", action="store_true", help="run headless")
    p.add_argument("--profile", help="device profile name (CLI mode)")
    p.add_argument("--port", type=int, help="override tcp_port from config")
    p.add_argument(
        "--no-adb-reverse",
        action="store_true",
        help="don't run `adb reverse` on start (useful for ffplay testing)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def run_cli(args: argparse.Namespace) -> int:
    cfg = StreamConfig.load()
    if args.port:
        cfg.tcp_port = args.port

    profiles = ProfileLibrary.load()
    name = args.profile or cfg.default_profile
    profile = profiles.get(name)
    if profile is None:
        print(f"unknown profile {name!r}; known: {profiles.names()}")
        return 2

    videos = list_videos(cfg.videos_path)
    if not videos:
        print(f"no videos in {cfg.videos_path}/  — drop some .mp4 files there")
        return 2
    print(f"playlist: {len(videos)} files in {cfg.videos_path}")
    pl = write_playlist(videos, loop=cfg.loop_playlist)

    adb = AdbController(cfg.adb_path)
    if cfg.auto_adb_reverse and not args.no_adb_reverse:
        if adb.is_available() and adb.devices():
            ok = adb.reverse(cfg.tcp_port)
            print(f"adb reverse tcp:{cfg.tcp_port} → {'OK' if ok else 'FAILED'}")
        else:
            print("(adb not available or no device — skipping `adb reverse`)")

    server = TcpStreamServer(
        cfg,
        on_state=lambda s: print(f"[server] {s}"),
    )
    server.start(pl, profile)

    monitor = HealthMonitor(server=server, adb=adb, interval_s=5.0)
    monitor.start()

    stop = {"flag": False}

    def _sig(_signo: int, _frame) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        # The HealthMonitor handles all periodic logging now. We just
        # idle here and watch for shutdown signals or server failures.
        while not stop["flag"] and server.is_running():
            time.sleep(0.5)
    finally:
        monitor.stop()
        server.stop()
        if cfg.auto_adb_reverse and not args.no_adb_reverse and adb.is_available():
            adb.reverse_remove(cfg.tcp_port)
        try:
            Path(pl).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def run_studio(args: argparse.Namespace) -> int:
    # Imported lazily so the heavier customtkinter import doesn't
    # slow down --cli launches.
    from .ui.studio_app import StudioApp

    app = StudioApp(
        port_override=args.port,
        no_adb_reverse=args.no_adb_reverse,
    )
    app.mainloop()
    return 0


def run_legacy(args: argparse.Namespace) -> int:
    from .ui.app import VcamApp

    app = VcamApp(port_override=args.port, no_adb_reverse=args.no_adb_reverse)
    app.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    _setup_logging(args.verbose)

    use_legacy = args.legacy or args.gui

    # Default to the Studio UI. CLI mode is opt-in only (--cli) so
    # double-clicked launchers, IDEs, and non-TTY contexts all land on
    # the customer UI rather than dropping into a headless streamer.
    if not args.studio and not use_legacy and not args.cli:
        args.studio = True

    print(f"vcam-pc — project root: {PROJECT_ROOT}")
    if args.cli:
        return run_cli(args)
    if use_legacy:
        return run_legacy(args)
    return run_studio(args)


if __name__ == "__main__":
    sys.exit(main())
