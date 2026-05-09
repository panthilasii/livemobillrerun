#!/usr/bin/env python3
"""NP Create -- admin helper to sign and publish announcements.

Usage
-----

Interactive (recommended)::

    python tools/publish_announcement.py

Scripted (CI / Line bot)::

    python tools/publish_announcement.py \\
        --id 2026-05-08-tiktok-update \\
        --title "TikTok อัปเดตแอปแล้ว" \\
        --body "อัปเดตแอป TikTok เป็น v37.5 แล้ว ระบบใช้งานได้ปกติ" \\
        --severity info \\
        --min-version 1.4.0 \\
        --expires-in-days 14 \\
        --output dist/announcements/feed.json

Output
------

Writes a single signed JSON file -- copy it to whatever HTTPS host
serves ``DEFAULT_URL`` in ``src/announcements.py``. Recommended:

* GitHub Pages (free, easy): commit the file to a public repo with
  Pages enabled and use ``https://<user>.github.io/<repo>/feed.json``
* Cloudflare R2 + Workers (free tier): better for production
* Any static-file CDN that serves over HTTPS

Why we sign
-----------

The customer ``_pubkey.py`` matches the admin ``.private_key``
seed used by the licensing pipeline. Reusing that keypair means:

* No new key rotation procedure to remember.
* An attacker who hijacks DNS (or runs a coffee-shop MITM) still
  cannot serve fake announcements -- they would need our private
  seed to forge a valid signature.

Security note
-------------

This tool refuses to run if ``.private_key`` is missing. If you
moved the workspace, copy your seed first; do NOT regenerate.
Regenerating rotates the keypair and revokes every issued license.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent

# Make ``src.*`` importable when invoked as a top-level script.
sys.path.insert(0, str(PROJECT))

from src import _ed25519  # noqa: E402

log = logging.getLogger(__name__)

PRIVATE_KEY_PATH = PROJECT / ".private_key"
DEFAULT_OUT = PROJECT / "dist" / "announcements" / "feed.json"


def _load_seed() -> bytes:
    if not PRIVATE_KEY_PATH.is_file():
        sys.exit(
            f"[!] {PRIVATE_KEY_PATH} not found.\n"
            "    Run tools/init_keys.py once, then preserve the seed.\n"
            "    (Generating a new key REVOKES every issued license.)"
        )
    seed_hex = PRIVATE_KEY_PATH.read_text(encoding="utf-8").strip()
    try:
        seed = bytes.fromhex(seed_hex)
    except ValueError as exc:
        sys.exit(f"[!] private key is not valid hex: {exc}")
    if len(seed) != 32:
        sys.exit(f"[!] private key wrong length: {len(seed)} bytes (expected 32)")
    return seed


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or (default or "")


def _build_announcement(args: argparse.Namespace) -> dict:
    """Either pull every required field from CLI args (scripted
    mode) or prompt the admin interactively."""
    interactive = not args.id

    ann_id = args.id or _prompt(
        "ID (เช่น 2026-05-08-tiktok-update)",
        datetime.now().strftime("%Y-%m-%d-msg"),
    )
    title = args.title or _prompt("Title (ภาษาไทยได้)")
    body = args.body or _prompt("Body (ภาษาไทยได้)")

    severity = args.severity or "info"
    if interactive:
        sev = _prompt("Severity (info/warning/critical)", severity)
        if sev in ("info", "warning", "critical"):
            severity = sev

    min_version = args.min_version or (
        _prompt("Min app version (Enter = ทุกเวอร์ชัน)") if interactive else ""
    )
    max_version = args.max_version or (
        _prompt("Max app version (Enter = ทุกเวอร์ชัน)") if interactive else ""
    )

    expires_days = args.expires_in_days
    if interactive and expires_days is None:
        raw = _prompt("หมดอายุภายในกี่วัน (Enter = ไม่หมด)", "30")
        if raw:
            try:
                expires_days = int(raw)
            except ValueError:
                expires_days = 30

    expires_iso: str | None = None
    if expires_days and expires_days > 0:
        expires_iso = (
            datetime.now(timezone.utc) + timedelta(days=expires_days)
        ).isoformat(timespec="seconds")

    action_label = args.action_label or (
        _prompt("ปุ่ม label (Enter = ไม่ต้อง)") if interactive else ""
    )
    action_url = args.action_url or (
        _prompt("ปุ่ม URL (Enter = ไม่ต้อง)") if interactive else ""
    )

    if not (ann_id and title and body):
        sys.exit("[!] id / title / body ห้ามว่าง")

    out = {
        "id": ann_id,
        "title": title,
        "body": body,
        "severity": severity,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if min_version:
        out["min_version"] = min_version
    if max_version:
        out["max_version"] = max_version
    if expires_iso:
        out["expires_at"] = expires_iso
    if action_label:
        out["action_label"] = action_label
    if action_url:
        out["action_url"] = action_url
    return out


def _sign(payload: dict, seed: bytes) -> dict:
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


def _merge_with_existing(
    out_path: Path, new_ann: dict, replace: bool,
) -> dict:
    """If the output file already exists (typical: we publish a
    feed of multiple live announcements at once), decode it,
    splice in the new entry, and re-emit. ``replace=True`` removes
    any prior announcement sharing the same id."""
    existing_anns: list[dict] = []
    if out_path.is_file():
        try:
            envelope = json.loads(out_path.read_text(encoding="utf-8"))
            payload_b64 = envelope.get("payload")
            if payload_b64:
                pb = base64.urlsafe_b64decode(payload_b64 + "==")
                payload = json.loads(pb.decode("utf-8"))
                existing_anns = list(payload.get("announcements") or [])
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.warning("could not read existing feed: %s -- starting fresh", exc)

    if replace:
        existing_anns = [a for a in existing_anns if a.get("id") != new_ann["id"]]

    existing_anns.append(new_ann)
    return {
        "feed_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "announcements": existing_anns,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--severity", choices=("info", "warning", "critical"))
    p.add_argument("--min-version", dest="min_version")
    p.add_argument("--max-version", dest="max_version")
    p.add_argument("--expires-in-days", type=int, dest="expires_in_days")
    p.add_argument("--action-label", dest="action_label")
    p.add_argument("--action-url", dest="action_url")
    p.add_argument(
        "--output", type=Path, default=DEFAULT_OUT,
        help=f"output JSON path (default: {DEFAULT_OUT.relative_to(PROJECT)})",
    )
    p.add_argument(
        "--replace", action="store_true",
        help="replace any existing announcement with the same id",
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    seed = _load_seed()
    new_ann = _build_announcement(args)
    payload = _merge_with_existing(args.output, new_ann, args.replace)
    envelope = _sign(payload, seed)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(envelope, indent=2), encoding="utf-8",
    )
    n = len(payload["announcements"])
    print()
    print(f"  wrote {args.output}  ({n} active announcement{'s' if n != 1 else ''})")
    print()
    print("  Next step: upload the file to wherever DEFAULT_URL points")
    print("  (currently src/announcements.py:DEFAULT_URL).")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
