#!/usr/bin/env python3
"""Live Studio Pro — admin license generator (Ed25519).

Usage::

    # Default tier: 3 devices / 30 days (matches BRAND.default_*)
    python tools/gen_license.py --customer "คุณสมชาย"

    # Custom: 1 device, 7 days, JSON output for piping into a webhook
    python tools/gen_license.py -c "Test" -n 1 -d 7 -j

Exit codes: 0 success, 2 bad arguments, 3 private key missing.

Setup
-----

Run once on the admin machine::

    python tools/init_keys.py

That creates the keypair: ``vcam-pc/.private_key`` (admin only,
gitignored) and ``vcam-pc/src/_pubkey.py`` (baked into all builds).

Back up ``.private_key`` to a password manager — losing it means
you can't issue any more keys that match shipped builds. Knowing
only the public key (which ships with every customer bundle) is
not enough to forge keys: that's the whole point of asymmetric
crypto.
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
    PRIVATE_KEY_PATH,
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

    if not PRIVATE_KEY_PATH.is_file():
        print(
            f"\n[!] Private key missing at {PRIVATE_KEY_PATH}\n"
            "    Generate the keypair once on this machine:\n"
            "      python tools/init_keys.py\n",
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
