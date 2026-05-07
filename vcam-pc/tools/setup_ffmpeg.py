#!/usr/bin/env python3
"""NP Create — admin helper: download a static ffmpeg build into
``.tools/<os>/`` so the customer ZIP is fully self-contained.

We pin upstream sources that are widely trusted *and* publish
"static" / "essentials" tarballs (no shared-library hunt at runtime):

* macOS  — https://www.osxexperts.net/  (universal2 ARM+Intel)
* Windows — https://www.gyan.dev/ffmpeg/builds/  (essentials, GPL)
* Linux  — https://johnvansickle.com/ffmpeg/  (static x86_64)

Idempotent: skips downloads that already exist. Pass ``--force``
to redownload, ``--os`` to scope to a single platform.

After this script the customer ZIP gains a ~70 MB ffmpeg binary
under ``.tools/<os>/ffmpeg{.exe}`` and the resolver in
``platform_tools.find_ffmpeg()`` picks it up automatically.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from tarfile import TarFile

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
WORKSPACE = PROJECT.parent

CACHE = WORKSPACE / ".cache" / "ffmpeg"

# Pinned URLs. Each tarball/zip exposes a single ``ffmpeg`` (or
# ``ffmpeg.exe``) binary that runs without any system libs other
# than the standard C runtime — exactly what we want for shipping.
FFMPEG_SOURCES: dict[str, dict[str, str]] = {
    "macos": {
        "url": "https://www.osxexperts.net/ffmpeg711arm.zip",
        "archive": "ffmpeg-macos-arm64.zip",
        "binary_in_archive": "ffmpeg",
    },
    "windows": {
        # gyan.dev "essentials" — GPL, ~85 MB, batteries included.
        "url": (
            "https://www.gyan.dev/ffmpeg/builds/"
            "ffmpeg-release-essentials.zip"
        ),
        "archive": "ffmpeg-windows-essentials.zip",
        "binary_in_archive": "bin/ffmpeg.exe",
    },
    "linux": {
        "url": (
            "https://johnvansickle.com/ffmpeg/releases/"
            "ffmpeg-release-amd64-static.tar.xz"
        ),
        "archive": "ffmpeg-linux-static.tar.xz",
        "binary_in_archive": "ffmpeg",
    },
}


def _download(url: str, dst: Path, force: bool = False) -> Path:
    if dst.is_file() and not force:
        size_mb = dst.stat().st_size / 1024 / 1024
        print(f"  ✓ cached {dst.name} ({size_mb:,.1f} MB)")
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"  → downloading {url}")
    tmp = dst.with_suffix(dst.suffix + ".part")
    with urllib.request.urlopen(url) as resp, tmp.open("wb") as f:
        shutil.copyfileobj(resp, f, length=1 << 20)
    tmp.replace(dst)
    print(
        f"  ✓ wrote {dst.name} "
        f"({dst.stat().st_size / 1024 / 1024:,.1f} MB)"
    )
    return dst


def _extract_binary_from_zip(archive: Path, member_suffix: str) -> bytes:
    """Return the raw bytes of the first archive member whose path
    ends with ``member_suffix``. Used for the gyan.dev Windows zip
    where the binary lives a few directories deep but the prefix
    name (``ffmpeg-7.x-essentials_build``) changes between versions.
    """
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            if name.endswith(member_suffix) and not name.endswith("/"):
                return zf.read(name)
    raise RuntimeError(
        f"no member ending with {member_suffix!r} in {archive}"
    )


def _extract_binary_from_tar(archive: Path, member_suffix: str) -> bytes:
    with TarFile.open(archive, "r:*") as tf:
        for m in tf.getmembers():
            if m.isfile() and m.name.endswith(member_suffix):
                f = tf.extractfile(m)
                assert f is not None
                return f.read()
    raise RuntimeError(
        f"no member ending with {member_suffix!r} in {archive}"
    )


def install_one(os_name: str, force: bool = False) -> Path:
    if os_name not in FFMPEG_SOURCES:
        raise SystemExit(f"unknown os: {os_name}")
    spec = FFMPEG_SOURCES[os_name]
    CACHE.mkdir(parents=True, exist_ok=True)

    archive_path = _download(
        spec["url"], CACHE / spec["archive"], force=force,
    )

    # Extract just the ffmpeg binary, drop everything else.
    if archive_path.suffix in (".zip",):
        data = _extract_binary_from_zip(
            archive_path, spec["binary_in_archive"],
        )
    else:
        data = _extract_binary_from_tar(
            archive_path, spec["binary_in_archive"],
        )

    suffix = ".exe" if os_name == "windows" else ""
    dst = WORKSPACE / ".tools" / os_name / f"ffmpeg{suffix}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    if os_name != "windows":
        try:
            dst.chmod(0o755)
        except OSError:
            pass
    size_mb = dst.stat().st_size / 1024 / 1024
    print(f"  ✓ installed {dst} ({size_mb:,.1f} MB)")
    return dst


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--os", dest="os_names", action="append",
        choices=tuple(FFMPEG_SOURCES.keys()),
        help="target OS (repeatable; default = all)",
    )
    p.add_argument("--force", action="store_true",
                   help="redownload even if files already cached")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    targets = args.os_names or list(FFMPEG_SOURCES.keys())
    print("NP Create — ffmpeg setup")
    print(f"  targets : {', '.join(targets)}")
    print(f"  cache   : {CACHE}")

    outs = []
    for osn in targets:
        print(f"\n— {osn} —")
        outs.append(install_one(osn, force=args.force))

    print()
    print("Done. The bundled ffmpeg(s) will be picked up automatically by:")
    print("  • src.platform_tools.find_ffmpeg()")
    print("  • tools/build_release.py (auto-included in customer ZIP)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
