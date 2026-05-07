#!/usr/bin/env python3
"""Live Studio Pro — admin license generator.

Usage::

    # Default tier: 3 devices / 30 days (matches BRAND.default_*)
    python tools/gen_license.py --customer "คุณสมชาย"

    # Custom: 1 device, 7 days, JSON output for piping into a webhook
    python tools/gen_license.py -c "Test" -n 1 -d 7 -j

Exit codes: 0 success, 2 bad arguments, 3 secret missing.

Setup
-----

The HMAC secret lives at ``vcam-pc/.license_secret`` (UTF-8, single
line). Create it once with::

    head -c 64 /dev/urandom | base64 > vcam-pc/.license_secret
    chmod 600 vcam-pc/.license_secret

Add ``.license_secret`` to ``.gitignore`` and back the file up to a
password manager — losing it means you can't issue or verify keys
that match shipped builds.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

# Allow running this file from anywhere — the project root is one
# directory above tools/, and the package lives inside src/.
HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))

from src.branding import BRAND  # noqa: E402
from src.license_key import (  # noqa: E402
    SECRET_PATH,
    generate_key,
    verify_key,
)


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="gen_license",
        description=f"{BRAND.name} — admin license generator",
    )
    p.add_argument("-c", "--customer", required=True, help="customer name")
    p.add_argument(
        "-n",
        "--devices",
        type=int,
        default=BRAND.default_devices_per_key,
        help=(
            f"how many phones this key may activate "
            f"(default {BRAND.default_devices_per_key})"
        ),
    )
    p.add_argument(
        "-d",
        "--days",
        type=int,
        default=BRAND.default_license_days,
        help=(
            f"lifetime in days from today "
            f"(default {BRAND.default_license_days})"
        ),
    )
    p.add_argument(
        "--expiry",
        help="explicit expiry date YYYY-MM-DD (overrides --days)",
    )
    p.add_argument("-j", "--json", action="store_true", help="print JSON")
    p.add_argument(
        "--quiet",
        action="store_true",
        help="print just the key, no human-readable framing",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv if argv is not None else sys.argv[1:])

    if not SECRET_PATH.is_file():
        print(
            f"\n[!] Secret missing at {SECRET_PATH}\n"
            "    Create it once:\n"
            f"      head -c 64 /dev/urandom | base64 > {SECRET_PATH.relative_to(PROJECT)}\n"
            f"      chmod 600 {SECRET_PATH.relative_to(PROJECT)}\n",
            file=sys.stderr,
        )
        return 3

    expiry = (
        date.fromisoformat(args.expiry)
        if args.expiry
        else date.today() + timedelta(days=args.days)
    )

    key = generate_key(
        customer=args.customer,
        max_devices=args.devices,
        expiry=expiry,
    )

    # Sanity check: round-trip verify before handing the key to anyone
    v = verify_key(key)
    assert v.customer == args.customer
    assert v.max_devices == args.devices
    assert v.expiry == expiry

    if args.json:
        print(
            json.dumps(
                {
                    "key": key,
                    "customer": v.customer,
                    "max_devices": v.max_devices,
                    "expiry": v.expiry.isoformat(),
                    "days_left": v.days_left,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.quiet:
        print(key)
        return 0

    print()
    print(f"  ╭─── {BRAND.name} — License Key ───────────")
    print(f"  │ ลูกค้า    : {v.customer}")
    print(f"  │ จำนวน    : {v.max_devices} เครื่อง")
    print(f"  │ หมดอายุ  : {v.expiry.isoformat()} ({v.days_left} วัน)")
    print(f"  ├──────────────────────────────────────────")
    print(f"  │ {key}")
    print(f"  ╰──────────────────────────────────────────")
    print()
    print("  ก๊อปคีย์ส่งให้ลูกค้าใน Line ได้เลย")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
