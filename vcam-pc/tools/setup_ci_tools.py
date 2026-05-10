#!/usr/bin/env python3
"""NP Create — CI helper: download every tool the customer install
needs (platform-tools, JDK 21, lspatch.jar, ffmpeg) into
``.tools/<os>/``.

Why this exists separately from ``setup_windows_tools.py`` and
``setup_macos_tools.py``
========================================================================

The two existing helpers were authored for the **admin's dev box**
where lspatch.jar already lives at ``.tools/lspatch/lspatch.jar``
(populated by ``install_lspatch.sh``). They copy / symlink from that
local cache into the per-OS subdir.

GitHub Actions runners are *fresh* every build — there's no local
``.tools/`` to copy from. So we need a self-contained downloader
that grabs every artifact straight from upstream:

* **platform-tools** — adb / fastboot from
  https://dl.google.com/android/repository/
* **JDK 21**         — Adoptium Temurin from GitHub releases
* **lspatch.jar**    — JingMatrix/LSPatch GitHub releases
* **ffmpeg**         — gyan.dev (Windows) / osxexperts.net (macOS)

scrcpy is handled by the sibling ``setup_scrcpy.py`` (already
wired into release.yml). This script complements it.

Why ffmpeg landed here in v1.7.10
---------------------------------
v1.7.6 portable ZIP shipped with ffmpeg.exe because the admin's
dev workspace had it pre-populated by ``setup_ffmpeg.py``. v1.7.8
+ v1.7.9 builds came out of fresh CI runners that *never* invoked
``setup_ffmpeg.py``, so the customer Setup.exe / portable ZIP were
ffmpeg-less. The Hook → Live pipeline silently degraded to "stream
fails to start" with no popup. Folding ffmpeg into this CI helper
guarantees both distribution channels carry the binary.

Usage::

    python tools/setup_ci_tools.py --os windows
    python tools/setup_ci_tools.py --os macos
    python tools/setup_ci_tools.py             # current OS

Outputs into ``<workspace>/.tools/<os>/{platform-tools,jdk-21,lspatch}/``.
Idempotent — skips already-downloaded artifacts unless ``--force``.

Encoding note (Windows)
-----------------------
All ``print()`` calls use ASCII-only markers (``->``, ``[OK]``,
``[!]``) instead of arrows / checkmarks. The Inno-Setup-bound
``windows-latest`` runner inherits cp1252 stdio and would crash
on emoji even with ``PYTHONUTF8=1`` in some PyInstaller bootstrap
paths — defence in depth.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
WORKSPACE = PROJECT.parent

# Use the existing CA-tolerant downloader so corporate CI mirrors
# with custom certs still work. ``_download_helper`` is a private
# sibling of this script.
sys.path.insert(0, str(HERE))
from _download_helper import download as _safe_download  # noqa: E402

CACHE = WORKSPACE / ".cache" / "ci-tools"


# ── upstream URLs (pinned where possible, dynamic for lspatch) ─────


# Adoptium Temurin 21 LTS. Pinning to a specific build (.5+11) for
# reproducibility — bump via PR after smoke-testing. The naming
# convention is consistent enough that we can templatise per-OS.
JDK_VERSION = "21.0.5+11"
JDK_VER_PATH = "21.0.5%2B11"           # URL-encoded form ('+' -> %2B)
JDK_VER_FILE = "21.0.5_11"             # filename form ('+' -> '_')

JDK_URLS = {
    "windows": (
        f"https://github.com/adoptium/temurin21-binaries/releases/"
        f"download/jdk-{JDK_VER_PATH}/"
        f"OpenJDK21U-jdk_x64_windows_hotspot_{JDK_VER_FILE}.zip"
    ),
    "macos": (
        f"https://github.com/adoptium/temurin21-binaries/releases/"
        f"download/jdk-{JDK_VER_PATH}/"
        f"OpenJDK21U-jdk_aarch64_mac_hotspot_{JDK_VER_FILE}.tar.gz"
    ),
}

PT_URLS = {
    "windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "macos":   "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
}

LSPATCH_RELEASE_API = (
    "https://api.github.com/repos/JingMatrix/LSPatch/releases/latest"
)

# Pinned fallback if the GitHub API is rate-limiting / down. We do
# our own release dance (build + tag + publish on push) entirely in
# this workflow, so the macOS job runs ~1 minute after the Windows
# one and burns the same shared anonymous quota — it eats 403s
# regularly without an Authorization header.
#
# Bumping this URL is a deliberate operation: the patched-TikTok
# class-name fingerprint can shift between LSPatch majors and the
# customer-side ``hook_status.probe()`` would need re-validating.
# The release we tested with the v1.7.10 patched-TikTok signature
# detection logic is v0.8 (2026-03), so that's what we pin.
LSPATCH_PINNED_URL = (
    "https://github.com/JingMatrix/LSPatch/releases/"
    "download/v0.8/lspatch.jar"
)

# ffmpeg "static" / "essentials" builds. Same upstreams as
# ``tools/setup_ffmpeg.py``; we duplicate the constants instead of
# importing because that file lives next door but isn't a package
# and importing across ``tools/`` would force sys.path gymnastics
# in CI for no real win.
FFMPEG_URLS = {
    "windows": (
        "https://www.gyan.dev/ffmpeg/builds/"
        "ffmpeg-release-essentials.zip"
    ),
    "macos": "https://www.osxexperts.net/ffmpeg711arm.zip",
}
# Path inside each archive that maps to the ``ffmpeg`` binary.
FFMPEG_BIN_IN_ARCHIVE = {
    "windows": "bin/ffmpeg.exe",
    "macos":   "ffmpeg",
}

# Google USB Driver — Windows-only ADB driver bundle. Apache-2
# licensed, redistributable, signed by Google, ~8.6 MB. macOS has
# native ADB-over-USB support (no driver needed) so we don't
# bother on that platform.
#
# Why we bundle it (v1.7.11)
# --------------------------
# Customers report "phone shows up in adb on Mac but not in Windows
# NP Create installer". Root cause: Windows ships no Android USB
# driver out of the box. Without one, ``adb devices`` returns an
# empty list, the wizard sits on "🔄 รอเครื่องเชื่อมต่อ…" forever,
# and the customer has no idea what to do. Shipping the official
# Google driver lets the in-app help dialog launch the installer
# without a separate download.
#
# The Google driver is most reliable on Pixel + Nexus, but it also
# WCID-registers as a generic Android ADB driver that satisfies
# many OEMs (including modern Xiaomi/Redmi running HyperOS once
# MIUI's "USB debugging (Security settings)" toggle is on). For
# Xiaomi devices that need the OEM-signed driver, the dialog also
# links to Mi PC Suite.
GOOGLE_USB_DRIVER_URL = (
    "https://dl.google.com/android/repository/usb_driver_r13-windows.zip"
)

# MediaMTX — single-binary RTMP/RTSP/HLS server (Go). Powers
# v1.8.0's "Mode B" no-USB live path: the customer installs a
# Play-Store virtual-cam app (CameraFi/Larix/DU Recorder) on
# the phone, sets its RTMP input to ``rtmp://<PC-IP>:1935/live``
# and picks it as the camera in TikTok. PC pushes the looped
# video file via FFmpeg → MediaMTX → phone over WiFi. No ADB,
# no USB cable, no Windows OEM driver.
#
# Pinned at v1.18.1 (April 2026). MediaMTX maintains backwards-
# compat for the RTMP listener config, but the exec layout and
# CLI args have moved across major versions, so we lock the
# build we tested against.
MEDIAMTX_VERSION = "v1.18.1"
MEDIAMTX_URLS = {
    "windows": (
        "https://github.com/bluenviron/mediamtx/releases/download/"
        f"{MEDIAMTX_VERSION}/mediamtx_{MEDIAMTX_VERSION}_windows_amd64.zip"
    ),
    "macos": (
        "https://github.com/bluenviron/mediamtx/releases/download/"
        f"{MEDIAMTX_VERSION}/mediamtx_{MEDIAMTX_VERSION}_darwin_arm64.tar.gz"
    ),
}


# ── helpers ────────────────────────────────────────────────────────


def _download(url: str, dst: Path, force: bool = False) -> Path:
    if dst.is_file() and not force:
        size_mb = dst.stat().st_size / 1024 / 1024
        print(f"  [OK] cached {dst.name} ({size_mb:,.1f} MB)")
        return dst
    print(f"  -> downloading {url}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    _safe_download(url, dst)
    size_mb = dst.stat().st_size / 1024 / 1024
    print(f"  [OK] wrote {dst.name} ({size_mb:,.1f} MB)")
    return dst


def _extract_zip(zpath: Path, into: Path, strip_first: bool = False) -> None:
    """Unpack ``zpath`` into ``into``. If ``strip_first`` is True,
    drop the single top-level directory inside the archive (matches
    the Adoptium layout: jdk-21.0.5+11/{bin,lib,...})."""
    into.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        if strip_first:
            tops = {n.split("/", 1)[0] for n in zf.namelist() if n.strip()}
            if len(tops) != 1:
                raise SystemExit(
                    f"expected single top dir in {zpath.name}, got {tops}"
                )
            top_name = next(iter(tops))
        else:
            top_name = ""
        for member in zf.infolist():
            name = member.filename
            if strip_first:
                if not name.startswith(top_name + "/"):
                    continue
                name = name[len(top_name) + 1:]
            if not name:
                continue
            target = into / name
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _extract_tar(tpath: Path, into: Path, strip_first: bool = False) -> None:
    """Unpack ``tpath`` (a .tar.gz / .tgz) into ``into``."""
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tpath, "r:gz") as tf:
        members = tf.getmembers()
        if strip_first:
            tops = {m.name.split("/", 1)[0] for m in members if m.name.strip()}
            if len(tops) != 1:
                raise SystemExit(
                    f"expected single top dir in {tpath.name}, got {tops}"
                )
            top_name = next(iter(tops))
        else:
            top_name = ""
        for m in members:
            name = m.name
            if strip_first:
                if not name.startswith(top_name + "/"):
                    continue
                name = name[len(top_name) + 1:]
            if not name or m.isdir():
                # We rebuild dir tree implicitly when extracting files.
                continue
            target = into / name
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(m)
            if extracted is None:
                continue
            with target.open("wb") as dst:
                shutil.copyfileobj(extracted, dst)
            # Preserve the executable bit on macOS / Linux JDK layouts —
            # ``java`` inside ``bin/`` is +x on the upstream tarball
            # and we want to keep it that way.
            if m.mode & 0o111:
                target.chmod(target.stat().st_mode | 0o755)


def _resolve_lspatch_url() -> str:
    """Return the latest LSPatch jar download URL.

    Resolution strategy (each step short-circuits the rest):

      1. ``$NPCREATE_LSPATCH_URL`` env var — operator override.
      2. GitHub API ``/releases/latest`` with ``$GITHUB_TOKEN``
         authorization if present. Auth bumps us from the 60/hr
         anonymous limit to 5000/hr per-token.
      3. ``LSPATCH_PINNED_URL`` (v0.8) — last resort when the
         API is rate-limiting or unreachable. This is the
         build that v1.7.10's patched-TikTok class-name probe
         was validated against, so falling back is safe.

    Why we ladder instead of always pinning
    ---------------------------------------
    LSPatch ships occasional security/compat fixes (Android 16/17
    preview support landed in v0.8 itself). We want CI to grab
    the newest stable when GitHub cooperates so customers don't
    silently lag a working upstream — but never to *fail the
    release* when the API hiccups, because a 403 here blocks
    the entire macOS build from publishing the .dmg.
    """
    override = os.environ.get("NPCREATE_LSPATCH_URL", "").strip()
    if override:
        print(f"  -> using NPCREATE_LSPATCH_URL override: {override}")
        return override

    print("  -> querying GitHub for latest LSPatch release")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "np-create-ci-setup/1.0",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        # GitHub recommends "Bearer" for fine-grained PATs;
        # classic GITHUB_TOKEN values accept it too.
        headers["Authorization"] = f"Bearer {token}"
        print("     (using GITHUB_TOKEN — 5000/hr quota)")
    else:
        print("     (no GITHUB_TOKEN — falling back to anon 60/hr quota)")

    req = urllib.request.Request(LSPATCH_RELEASE_API, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        for asset in data.get("assets", []):
            if asset.get("name") == "lspatch.jar":
                url = asset.get("browser_download_url")
                if url:
                    return url
        print(
            "  [!] GitHub API returned no lspatch.jar asset; "
            "falling back to pinned URL"
        )
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        # 403 (rate limit), 5xx (GH outage), DNS hiccup — all
        # benign at the build level. Don't block the release.
        print(f"  [!] GitHub API error: {e!r} -- falling back to pinned URL")

    print(f"  -> pinned lspatch URL: {LSPATCH_PINNED_URL}")
    return LSPATCH_PINNED_URL


# ── per-tool installers ────────────────────────────────────────────


def install_platform_tools(os_name: str, force: bool) -> None:
    print()
    print("[platform-tools]")
    dest = WORKSPACE / ".tools" / os_name / "platform-tools"
    if dest.is_dir() and any(dest.iterdir()) and not force:
        print(f"  [OK] already at {dest.relative_to(WORKSPACE)}")
        return
    url = PT_URLS[os_name]
    cache_path = CACHE / f"platform-tools-{os_name}.zip"
    _download(url, cache_path, force)
    if dest.is_dir():
        shutil.rmtree(dest)
    print(f"  -> extracting to {dest.relative_to(WORKSPACE)}")
    # The zip already has a top-level ``platform-tools/`` dir, so we
    # extract into the parent and let it land at the right place.
    _extract_zip(cache_path, dest.parent, strip_first=False)


def install_jdk(os_name: str, force: bool) -> None:
    print()
    print("[jdk-21]")
    dest = WORKSPACE / ".tools" / os_name / "jdk-21"
    if dest.is_dir() and not force:
        # Existence is good enough — bin/java is asserted in verify().
        print(f"  [OK] already at {dest.relative_to(WORKSPACE)}")
        return
    url = JDK_URLS[os_name]
    if os_name == "windows":
        cache_path = CACHE / f"jdk-{JDK_VER_FILE}-windows.zip"
        _download(url, cache_path, force)
        if dest.is_dir():
            shutil.rmtree(dest)
        print(f"  -> extracting to {dest.relative_to(WORKSPACE)}")
        _extract_zip(cache_path, dest, strip_first=True)
    else:
        cache_path = CACHE / f"jdk-{JDK_VER_FILE}-macos.tar.gz"
        _download(url, cache_path, force)
        if dest.is_dir():
            shutil.rmtree(dest)
        print(f"  -> extracting to {dest.relative_to(WORKSPACE)}")
        # macOS Adoptium tarball has a single top dir
        # ``jdk-21.0.5+11/`` containing ``Contents/Home/bin/java``.
        _extract_tar(cache_path, dest, strip_first=True)


def install_ffmpeg(os_name: str, force: bool) -> None:
    """Download a static ffmpeg build and drop it at
    ``.tools/<os>/ffmpeg{.exe}``.

    ``platform_tools.find_ffmpeg`` walks both that exact path and
    the ``bin/ffmpeg`` subpath inside the per-OS dir, so either
    layout would work — but flat at the per-OS root is what
    ``setup_ffmpeg.py`` produces, and what v1.7.6 customers had,
    so we preserve that layout for byte-for-byte parity with the
    last known-good build.
    """
    print()
    print("[ffmpeg]")
    sfx = ".exe" if os_name == "windows" else ""
    dest_dir = WORKSPACE / ".tools" / os_name
    dest_bin = dest_dir / f"ffmpeg{sfx}"
    if dest_bin.is_file() and not force:
        size_mb = dest_bin.stat().st_size / 1024 / 1024
        print(f"  [OK] already at {dest_bin.relative_to(WORKSPACE)} "
              f"({size_mb:,.1f} MB)")
        return
    url = FFMPEG_URLS[os_name]
    cache_path = CACHE / f"ffmpeg-{os_name}.zip"
    _download(url, cache_path, force)
    member = FFMPEG_BIN_IN_ARCHIVE[os_name]
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"  -> extracting {member} -> {dest_bin.relative_to(WORKSPACE)}")
    # Both upstream archives are .zip but the ffmpeg binary lives
    # at a fixed path inside (gyan.dev nests under
    # ``ffmpeg-<v>-essentials_build/bin/ffmpeg.exe``). Search by
    # filename suffix so we don't have to keep up with version
    # bumps in the archive's top-level dir name.
    target_basename = Path(member).name
    with zipfile.ZipFile(cache_path) as zf:
        # Find the first member whose basename matches.
        chosen = None
        for info in zf.infolist():
            if Path(info.filename).name == target_basename:
                chosen = info
                break
        if chosen is None:
            raise SystemExit(
                f"ffmpeg binary not found in {cache_path.name} "
                f"(looking for any path ending with {target_basename})"
            )
        with zf.open(chosen) as src, dest_bin.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    if os_name != "windows":
        # Preserve +x; macOS / Linux ZIP doesn't carry mode bits in
        # all cases, so set it unconditionally.
        dest_bin.chmod(dest_bin.stat().st_mode | 0o755)
    size_mb = dest_bin.stat().st_size / 1024 / 1024
    print(f"  [OK] installed {dest_bin.relative_to(WORKSPACE)} "
          f"({size_mb:,.1f} MB)")


def install_adb_driver(os_name: str, force: bool) -> None:
    """Drop the Google USB Driver under
    ``.tools/windows/adb-driver/usb_driver/`` so the in-app help
    dialog can point Windows' Device Manager → "Update driver" at
    a known-good local folder (or run the included
    ``android_winusb.inf`` manually). macOS skips this — Apple's
    Mac kernel handles Android ADB via libusb without an OEM
    driver.

    The downloaded zip already contains a single top-level
    ``usb_driver/`` directory with all the .inf / .cat / .dll
    bits Windows expects. We extract straight into the per-OS
    .tools dir so the runtime resolver
    (``platform_tools.find_adb_driver_dir``) can find it without
    walking past archive metadata.
    """
    if os_name != "windows":
        print()
        print("[adb-driver] (skip — macOS handles ADB without an OEM driver)")
        return
    print()
    print("[adb-driver]")
    dest_dir = WORKSPACE / ".tools" / os_name / "adb-driver"
    inf_marker = dest_dir / "usb_driver" / "android_winusb.inf"
    if inf_marker.is_file() and not force:
        size_mb = sum(
            p.stat().st_size for p in dest_dir.rglob("*") if p.is_file()
        ) / 1024 / 1024
        print(f"  [OK] already at {dest_dir.relative_to(WORKSPACE)} "
              f"({size_mb:,.1f} MB)")
        return
    cache_path = CACHE / "google-usb-driver.zip"
    _download(GOOGLE_USB_DRIVER_URL, cache_path, force)
    if dest_dir.is_dir():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"  -> extracting to {dest_dir.relative_to(WORKSPACE)}")
    # The zip contains a single top-level ``usb_driver/`` dir,
    # which is exactly what we want at the destination — extract
    # straight in.
    _extract_zip(cache_path, dest_dir, strip_first=False)
    if not inf_marker.is_file():
        # Defensive: the archive layout occasionally drifts (Google
        # nests under ``android_usb/`` in some old builds). Surface
        # the failure here rather than at customer install time.
        raise SystemExit(
            f"adb-driver extraction completed but {inf_marker} is "
            f"missing — archive layout may have changed; inspect "
            f"{cache_path}."
        )
    size_mb = sum(
        p.stat().st_size for p in dest_dir.rglob("*") if p.is_file()
    ) / 1024 / 1024
    print(f"  [OK] installed {dest_dir.relative_to(WORKSPACE)} "
          f"({size_mb:,.1f} MB)")


def install_mediamtx(os_name: str, force: bool) -> None:
    """Drop the MediaMTX binary + a stub config under
    ``.tools/<os>/mediamtx/`` for v1.8.0's RTMP-based live path.

    Layout produced::

        .tools/<os>/mediamtx/
            mediamtx[.exe]      <- the single-binary RTMP server
            mediamtx.yml        <- generated at runtime by
                                   src/rtmp_server.py (bind addr,
                                   port, paths). The upstream zip
                                   ships its own example yml here
                                   too; we keep it for diff'ing.

    The Windows archive is a flat zip with three files at the
    root (mediamtx.exe, mediamtx.yml, LICENSE). The macOS
    tarball has the same structure but is gzipped.
    """
    print()
    print("[mediamtx]")
    dest_dir = WORKSPACE / ".tools" / os_name / "mediamtx"
    bin_name = "mediamtx.exe" if os_name == "windows" else "mediamtx"
    dest_bin = dest_dir / bin_name
    if dest_bin.is_file() and not force:
        size_mb = dest_bin.stat().st_size / 1024 / 1024
        print(f"  [OK] already at {dest_bin.relative_to(WORKSPACE)} "
              f"({size_mb:,.1f} MB)")
        return
    url = MEDIAMTX_URLS[os_name]
    if os_name == "windows":
        cache_path = CACHE / f"mediamtx-{MEDIAMTX_VERSION}-windows.zip"
        _download(url, cache_path, force)
        dest_dir.mkdir(parents=True, exist_ok=True)
        print(f"  -> extracting to {dest_dir.relative_to(WORKSPACE)}")
        _extract_zip(cache_path, dest_dir, strip_first=False)
    else:
        cache_path = CACHE / f"mediamtx-{MEDIAMTX_VERSION}-macos.tar.gz"
        _download(url, cache_path, force)
        dest_dir.mkdir(parents=True, exist_ok=True)
        print(f"  -> extracting to {dest_dir.relative_to(WORKSPACE)}")
        # macOS tarball is flat — use strip_first=False.
        _extract_tar(cache_path, dest_dir, strip_first=False)
        # Mark the binary executable; tarballs preserve the bit
        # but defensive chmod doesn't hurt.
        if dest_bin.is_file():
            dest_bin.chmod(dest_bin.stat().st_mode | 0o755)
    if not dest_bin.is_file():
        raise SystemExit(
            f"mediamtx extraction completed but {dest_bin} is "
            f"missing — archive layout may have changed; inspect "
            f"{cache_path}."
        )
    size_mb = dest_bin.stat().st_size / 1024 / 1024
    print(f"  [OK] installed {dest_bin.relative_to(WORKSPACE)} "
          f"({size_mb:,.1f} MB)")


def install_lspatch(os_name: str, force: bool) -> None:
    print()
    print("[lspatch]")
    # lspatch.jar is JVM bytecode — same file works on every OS, but
    # we drop a copy into each per-OS dir so build_release.py doesn't
    # need cross-OS lookups when zipping.
    dest_dir = WORKSPACE / ".tools" / os_name / "lspatch"
    dest_jar = dest_dir / "lspatch.jar"
    if dest_jar.is_file() and not force:
        size_mb = dest_jar.stat().st_size / 1024 / 1024
        print(f"  [OK] already at {dest_jar.relative_to(WORKSPACE)} "
              f"({size_mb:,.1f} MB)")
        return
    url = _resolve_lspatch_url()
    cache_path = CACHE / "lspatch.jar"
    _download(url, cache_path, force)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cache_path, dest_jar)
    size_mb = dest_jar.stat().st_size / 1024 / 1024
    print(f"  [OK] installed {dest_jar.relative_to(WORKSPACE)} "
          f"({size_mb:,.1f} MB)")


# ── main ───────────────────────────────────────────────────────────


def _verify(os_name: str) -> int:
    """Best-effort sanity check after install. Returns 0 if OK."""
    sfx = ".exe" if os_name == "windows" else ""
    base = WORKSPACE / ".tools" / os_name
    checks: list[tuple[str, Path]] = [
        ("platform-tools/adb", base / "platform-tools" / f"adb{sfx}"),
        ("lspatch.jar       ", base / "lspatch" / "lspatch.jar"),
        ("ffmpeg            ", base / f"ffmpeg{sfx}"),
        ("mediamtx          ", base / "mediamtx" / f"mediamtx{sfx}"),
    ]
    if os_name == "windows":
        checks.append(("jdk-21/bin/java   ", base / "jdk-21" / "bin" / "java.exe"))
        checks.append((
            "adb-driver/.../inf",
            base / "adb-driver" / "usb_driver" / "android_winusb.inf",
        ))
    else:
        # Adoptium macOS layout: jdk-21/Contents/Home/bin/java
        checks.append(("jdk-21/.../java   ",
                       base / "jdk-21" / "Contents" / "Home" / "bin" / "java"))

    print()
    print("Verification:")
    bad = 0
    for label, path in checks:
        ok = path.is_file()
        mark = "[OK]" if ok else "[!] "
        print(f"  {mark} {label} : {path.relative_to(WORKSPACE)}")
        if not ok:
            bad += 1
    return 0 if bad == 0 else 3


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--os", dest="os_name",
        choices=("windows", "macos"),
        help="target OS (default: current OS)",
    )
    p.add_argument(
        "--force", action="store_true",
        help="redownload + re-extract even if cached",
    )
    p.add_argument(
        "--skip", action="append", default=[],
        choices=("platform-tools", "jdk", "lspatch", "ffmpeg",
                 "adb-driver", "mediamtx"),
        help="skip a specific tool (repeatable)",
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    os_name = args.os_name
    if not os_name:
        if sys.platform == "win32":
            os_name = "windows"
        elif sys.platform == "darwin":
            os_name = "macos"
        else:
            raise SystemExit(
                "Linux not supported by this CI helper "
                "(use the dev workflow's setup_*.sh scripts)."
            )

    print("NP Create -- CI tools setup")
    print(f"  os     : {os_name}")
    print(f"  cache  : {CACHE}")
    print(f"  target : {WORKSPACE / '.tools' / os_name}")

    CACHE.mkdir(parents=True, exist_ok=True)

    if "platform-tools" not in args.skip:
        install_platform_tools(os_name, args.force)
    if "jdk" not in args.skip:
        install_jdk(os_name, args.force)
    if "lspatch" not in args.skip:
        install_lspatch(os_name, args.force)
    if "ffmpeg" not in args.skip:
        install_ffmpeg(os_name, args.force)
    if "adb-driver" not in args.skip:
        install_adb_driver(os_name, args.force)
    if "mediamtx" not in args.skip:
        install_mediamtx(os_name, args.force)

    rc = _verify(os_name)
    if rc == 0:
        print()
        print("Done. Tools ready for build_release.py / installer.iss / build_dmg.sh.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
