#!/usr/bin/env python3
"""NP Create -- admin helper to package, sign, and publish an
auto-update patch.

Workflow
--------

1. Bump ``BRAND.version`` in ``src/branding.py`` (e.g. ``1.5.1``).
2. Run this script from the project root::

       python tools/publish_update.py --notes "แก้ปัญหา X, Y"

3. The script writes two files into ``dist/updates/``:

   * ``npcreate-src-1.5.1.zip`` -- the patch (just the ``src/``
     tree at the new version, NO ``.private_key``, NO tests).
   * ``manifest.json``           -- signed envelope pointing at the
     download URL.

4. Upload BOTH files to whatever HTTPS host serves
   ``DEFAULT_MANIFEST_URL`` from ``src/auto_update.py``. With GitHub
   Pages the easy path is::

       cp dist/updates/manifest.json npcreate.github.io/updates/manifest.json
       cp dist/updates/npcreate-src-1.5.1.zip npcreate.github.io/updates/

   Pages serves the new manifest; customers' apps pick it up within
   the next 6 h poll cycle.

Why we sign
-----------

Same trust chain as licenses + announcements -- knowing the public
key (which ships in the customer build) does not let an attacker
forge an update; they would need our ``.private_key`` seed.

What we DON'T sign
------------------

The patch ZIP itself. We sign its **SHA256** inside the manifest
instead, which gives us the same security with one fewer signing
operation per release.

Safety knobs
------------

* Refuses to run if ``.private_key`` is missing.
* Refuses to overwrite an existing ``manifest.json`` whose version
  is >= the new one (you'd otherwise downgrade your fleet).
* Always ships the patch with the project's ``min_compat_version``
  set conservatively to ``current major.minor.0`` so an old
  customer who hasn't run in months can't apply a patch designed
  for a different schema.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent

# Make ``src.*`` importable when invoked as a top-level script.
sys.path.insert(0, str(PROJECT))

from src import _ed25519  # noqa: E402
from src.branding import BRAND  # noqa: E402

log = logging.getLogger(__name__)

PRIVATE_KEY_PATH = PROJECT / ".private_key"
SRC_DIR = PROJECT / "src"
OUT_DIR = PROJECT / "dist" / "updates"


# Files inside src/ we deliberately leave OUT of the patch:
#   - __pycache__/  -- build artefact
#   - *.pyc         -- ditto
#   - tests inside src/ if any
# The auto-updater extracts what we put here over the customer's
# existing src/, so anything missing here remains at the customer's
# previous version. That's fine for caches but DEFINITELY wrong for
# real files; this list is the canonical "build ignore" list.
EXCLUDE_GLOB_FRAGMENTS = (
    "__pycache__",
    ".DS_Store",
    ".pytest_cache",
)


def _load_seed() -> bytes:
    if not PRIVATE_KEY_PATH.is_file():
        sys.exit(
            f"[!] {PRIVATE_KEY_PATH} not found.\n"
            "    Run tools/init_keys.py once, then preserve the seed.\n"
            "    (Generating a new key REVOKES every issued license + "
            "all signed manifests.)"
        )
    seed_hex = PRIVATE_KEY_PATH.read_text(encoding="utf-8").strip()
    try:
        seed = bytes.fromhex(seed_hex)
    except ValueError as exc:
        sys.exit(f"[!] private key not hex: {exc}")
    if len(seed) != 32:
        sys.exit(f"[!] private key wrong length: {len(seed)}")
    return seed


def _should_skip(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    return any(frag in parts for frag in EXCLUDE_GLOB_FRAGMENTS) \
        or rel_path.endswith(".pyc")


def build_patch_zip(out_zip: Path) -> int:
    """Walk ``src/`` and pack every file into ``out_zip``. Returns
    the byte size of the resulting archive (caller logs it).
    """
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()

    n_files = 0
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(SRC_DIR.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(SRC_DIR).as_posix()
            if _should_skip(rel):
                continue
            zf.write(path, arcname=rel)
            n_files += 1

    print(f"  packed {n_files} files into {out_zip.name}")
    return out_zip.stat().st_size


def _sign_payload(payload: dict, seed: bytes) -> dict:
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    sig = _ed25519.sign(seed, payload_bytes)
    return {
        "format_version": 1,
        "payload": base64.urlsafe_b64encode(payload_bytes)
            .decode("ascii").rstrip("="),
        "signature": sig.hex(),
    }


def _read_existing_version(manifest_path: Path) -> str | None:
    if not manifest_path.is_file():
        return None
    try:
        env = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload_b64 = env.get("payload") or ""
        pb = base64.urlsafe_b64decode(payload_b64 + "==")
        payload = json.loads(pb.decode("utf-8"))
        return str(payload.get("version") or "")
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _ge(a: str, b: str) -> bool:
    """Return ``True`` if ``a >= b`` as semver tuples."""
    def _t(s: str) -> tuple[int, ...]:
        try:
            return tuple(int(c) for c in s.split("-")[0].split("."))
        except (ValueError, AttributeError):
            return (0,)
    return _t(a) >= _t(b)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--version", default=BRAND.version,
        help=(f"version label for the patch "
              f"(default: branding.BRAND.version = {BRAND.version})"),
    )
    p.add_argument(
        "--notes", default="ปรับปรุงทั่วไป",
        help="Thai-language changelog shown to the customer",
    )
    p.add_argument(
        "--download-url-template",
        default="https://npcreate.github.io/updates/npcreate-src-{version}.zip",
        help=(
            "URL template where the patch ZIP will be uploaded. "
            "{version} is substituted from --version."
        ),
    )
    p.add_argument(
        "--out", type=Path, default=OUT_DIR,
        help=f"output directory (default: {OUT_DIR.relative_to(PROJECT)})",
    )
    p.add_argument(
        "--min-compat",
        help=(
            "Lowest currently-installed version that can apply this "
            "patch. Defaults to '<major>.<minor>.0' so an unrelated "
            "old install can't pull a patch designed for a newer "
            "schema."
        ),
    )
    p.add_argument(
        "--allow-downgrade", action="store_true",
        help="skip the 'manifest already at higher version' guard",
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    seed = _load_seed()
    new_version = str(args.version)

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = out_dir / "manifest.json"
    if not args.allow_downgrade:
        existing = _read_existing_version(manifest_path)
        if existing and _ge(existing, new_version):
            sys.exit(
                f"[!] dist/updates/manifest.json already at v{existing} "
                f">= v{new_version}. Bump --version or pass "
                f"--allow-downgrade if you really mean to publish a "
                f"rollback."
            )

    patch_path = out_dir / f"npcreate-src-{new_version}.zip"
    size = build_patch_zip(patch_path)
    sha256 = hashlib.sha256(patch_path.read_bytes()).hexdigest()

    if args.min_compat:
        min_compat = args.min_compat
    else:
        # Auto-pick: same major.minor as the new build, patch=0.
        try:
            major, minor, _ = new_version.split(".")[:3]
            min_compat = f"{major}.{minor}.0"
        except (ValueError, IndexError):
            min_compat = "1.0.0"

    payload = {
        "version": new_version,
        "kind": "source",
        "download_url": args.download_url_template.format(version=new_version),
        "sha256": sha256,
        "notes_th": args.notes,
        "min_compat_version": min_compat,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    envelope = _sign_payload(payload, seed)

    manifest_path.write_text(
        json.dumps(envelope, indent=2), encoding="utf-8",
    )

    print()
    print(f"  version       : {new_version}")
    print(f"  patch zip     : {patch_path} ({size / 1024:.1f} KB)")
    print(f"  sha256        : {sha256}")
    print(f"  download url  : {payload['download_url']}")
    print(f"  min_compat    : {min_compat}")
    print(f"  manifest      : {manifest_path}")
    print()
    print("  Next step: upload BOTH files to your update host. With")
    print("  GitHub Pages this typically means:")
    print(f"    cp {manifest_path.name} <pages-repo>/updates/manifest.json")
    print(f"    cp {patch_path.name} <pages-repo>/updates/")
    print("    cd <pages-repo> && git add . && git commit && git push")
    print()
    print("  Customers will see the update banner within 6 h.")
    print()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
