"""Entry point for NP Create / vcam-pc.

Usage::

    python -m src.main                        # Studio (customer UI, default)
    python -m src.main --legacy               # legacy diagnostic UI
    python -m src.main --cli --profile NAME   # headless streamer

In CLI mode, Ctrl+C stops cleanly.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from .adb import AdbController
from .config import PROJECT_ROOT, ProfileLibrary, StreamConfig
from .health import HealthMonitor
from .log_setup import configure_logging
from .playlist import list_videos, write_playlist
from .tcp_server import TcpStreamServer


def _setup_logging(verbose: bool) -> None:
    """Configure root logging with a rotating on-disk file handler
    in addition to console output.

    The on-disk log is critical for non-technical customer support:
    when something breaks at 11pm during a TikTok Live, the customer
    can't paste a stack trace into Line OA; they CAN attach
    ``logs/npcreate.log`` (or the diagnostic ZIP we generate from
    Settings → "ส่ง Log ให้แอดมิน"). All log-config knobs --
    rotation size, retention, redaction policy -- live in
    ``log_setup`` so they're testable in isolation.
    """
    configure_logging(verbose=verbose)


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


def _check_writable_install_location() -> bool:
    """Detect a read-only install location (a customer running
    NP-Create.app straight from the mounted .dmg) and refuse to
    boot.

    Why this exists
    ---------------
    The whole app — logs, config.json, device_profiles.json,
    license activation ledger, update cache, sqlite — writes
    relative to ``PROJECT_ROOT``. If the customer double-clicks
    NP-Create.app while it's still inside the .dmg volume (which
    macOS mounts read-only), the first ``logs/`` mkdir explodes
    with ``OSError: [Errno 30] Read-only file system`` and they
    see a stack-trace popup. The fix everyone expects on macOS
    is the same as OBS / Discord / Notion: a clear dialog telling
    them to drag the app to /Applications/ first.

    Returns ``True`` when the install location is writable (boot
    should continue). Returns ``False`` when it isn't (caller
    must exit).

    Skipped entirely in dev mode (``sys.frozen`` is False) so
    running ``python -m src.main`` from the source tree keeps
    working even on a read-only checkout.
    """
    if not getattr(sys, "frozen", False):
        return True

    looks_like_volume = str(PROJECT_ROOT).startswith("/Volumes/")
    probe = PROJECT_ROOT / ".npcreate-writable-probe"
    try:
        probe.write_bytes(b"")
        # Best-effort cleanup; failure here is harmless.
        try:
            probe.unlink()
        except OSError:
            pass
        return True
    except OSError as exc:
        # errno 30 = read-only filesystem, 13 = permission denied
        if exc.errno not in (30, 13):
            # Some other I/O glitch — don't block the launch on it.
            return True

    if looks_like_volume:
        title = "NP Create — กรุณาติดตั้งก่อนเปิดใช้งาน"
        message = (
            "ตอนนี้คุณกำลังเปิด NP Create จากในแผ่นภาพดิสก์ (.dmg)\n"
            "ซึ่งเป็นพื้นที่อ่านอย่างเดียว ทำให้โปรแกรมเขียน\n"
            "ค่าตั้งค่า / log / cache ไม่ได้\n\n"
            "วิธีติดตั้งให้ถูกต้อง:\n"
            "  1. ลาก  NP-Create  ใส่ทางลัด  Applications  ในหน้าต่างเดียวกัน\n"
            "  2. ปิดหน้าต่างนี้ (Eject) แผ่นภาพดิสก์\n"
            "  3. เปิด  NP-Create  จาก  /Applications/  (Launchpad / Finder)\n\n"
            "โปรแกรมจะปิดตัวเองตอนนี้ — เปิดอีกครั้งหลังลากเสร็จได้เลยครับ"
        )
    else:
        title = "NP Create — กรุณาติดตั้งก่อนเปิดใช้งาน"
        message = (
            "NP Create ติดตั้งอยู่ในตำแหน่งที่เขียนไฟล์ไม่ได้:\n  "
            f"{PROJECT_ROOT}"
            "\n\nกรุณาย้ายโปรแกรมไป /Applications/ หรือโฟลเดอร์ที่\n"
            "เขียนได้ก่อนเปิดใช้งานครับ"
        )

    # Build a transient Tk root just for the dialog. Customtkinter is
    # heavier than we need here and slow to import; vanilla tkinter
    # ships with the Python bundle.
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showwarning(title, message)
        root.destroy()
    except Exception:
        # If even tkinter is unavailable, fall back to stderr so the
        # message at least makes it into Console.app / Event Viewer.
        sys.stderr.write(f"{title}\n\n{message}\n")

    if looks_like_volume:
        # Helper: open /Applications/ in Finder so the customer can
        # drag the .app there without hunting for it.
        import subprocess
        try:
            subprocess.run(
                ["open", "/Applications/"],
                check=False,
                timeout=5,
            )
        except Exception:
            pass

    return False


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

    # macOS .dmg footgun guard: refuse to boot when PROJECT_ROOT is
    # on a read-only mount. We do this *before* the diagnostic write
    # because that itself would crash on the .dmg.
    if not _check_writable_install_location():
        return 1

    # Always emit a startup diagnostic. This is the file we ask
    # customers to send when "the wizard never finds my phone" —
    # it captures every path the resolver picked plus a live
    # ``adb version`` / ``adb devices`` probe, so support can
    # tell which layer broke without bouncing screenshots back
    # and forth. See ``_startup_diagnostic.py`` for the rationale.
    try:
        from ._startup_diagnostic import write_diagnostic
        write_diagnostic()
    except Exception:
        # Diagnostic must never block startup.
        pass

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
