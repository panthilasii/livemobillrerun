"""Operator CLI -- ``python -m app.cli ...``.

One-time bootstrap on a fresh server::

    python -m app.cli init-db
    python -m app.cli init-keys                  # Ed25519 signing seed
    python -m app.cli create-admin --email you@np.local --password ...

Routine maintenance::

    python -m app.cli show-pubkey                # paste into vcam-pc
    python -m app.cli list-admins
    python -m app.cli set-password --email ... --password ...
    python -m app.cli prune-uploads --days 90    # delete old support zips

We deliberately keep this as plain ``argparse`` (no Typer / Click)
so the operator can run it inside the Docker container without
any extra deps beyond what the server already needs.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import auth, crypto, db
from .config import SETTINGS


def _cmd_init_db(args: argparse.Namespace) -> int:
    db.init_db()
    print(f"✓ schema initialised at {SETTINGS.db_path}")
    return 0


def _cmd_init_keys(args: argparse.Namespace) -> int:
    try:
        seed_path, pub_hex = crypto.init_new_keypair(force=args.force)
    except crypto.CryptoError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    print(f"✓ private seed   : {seed_path} (chmod 600)")
    print(f"✓ public key     : {SETTINGS.public_key_path}")
    print()
    print("Paste this into vcam-pc/src/_pubkey.py:")
    print()
    print(f'PUBLIC_KEY_HEX = "{pub_hex}"')
    print()
    print(
        "Then rebuild the customer bundle so customer apps verify "
        "against this server's keys."
    )
    return 0


def _cmd_show_pubkey(args: argparse.Namespace) -> int:
    try:
        print(crypto.public_key_hex())
    except crypto.CryptoError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    return 0


def _cmd_create_admin(args: argparse.Namespace) -> int:
    db.init_db()
    try:
        pwd_hash = auth.hash_password(args.password)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    with db.connect() as cx:
        existing = cx.execute(
            "SELECT id FROM admins WHERE email = ?", (args.email.lower(),),
        ).fetchone()
        if existing:
            print(f"✗ admin {args.email} already exists (id={existing['id']})", file=sys.stderr)
            return 3
        cur = cx.execute(
            "INSERT INTO admins (email, password_hash, display_name, "
            "created_at, is_active) VALUES (?, ?, ?, ?, 1)",
            (
                args.email.lower(),
                pwd_hash,
                args.display_name or args.email,
                db.now_iso(),
            ),
        )
        new_id = cur.lastrowid
    print(f"✓ created admin id={new_id} email={args.email}")
    return 0


def _cmd_list_admins(args: argparse.Namespace) -> int:
    db.init_db()
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT id, email, display_name, last_login_at, is_active "
            "FROM admins ORDER BY id"
        ).fetchall()
    if not rows:
        print("(no admins yet — run create-admin first)")
        return 0
    print(f"{'ID':>3}  {'EMAIL':30}  {'NAME':20}  {'LAST_LOGIN':19}  ACTIVE")
    for r in rows:
        print(
            f"{r['id']:>3}  {r['email'][:30]:30}  "
            f"{(r['display_name'] or '')[:20]:20}  "
            f"{(r['last_login_at'] or '-')[:19]:19}  "
            f"{'Y' if r['is_active'] else 'N'}"
        )
    return 0


def _cmd_set_password(args: argparse.Namespace) -> int:
    db.init_db()
    try:
        pwd_hash = auth.hash_password(args.password)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    with db.connect() as cx:
        cur = cx.execute(
            "UPDATE admins SET password_hash = ? WHERE email = ?",
            (pwd_hash, args.email.lower()),
        )
        if cur.rowcount == 0:
            print(f"✗ no admin with email {args.email}", file=sys.stderr)
            return 3
    print(f"✓ password reset for {args.email}")
    return 0


def _cmd_prune_uploads(args: argparse.Namespace) -> int:
    """Remove uploaded support zips + their DB rows older than N
    days. Run by cron (or manually) on the VPS so the upload dir
    doesn't grow without bound."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=args.days)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    n_files = 0
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT id, log_path FROM support_tickets WHERE submitted_at < ?",
            (cutoff,),
        ).fetchall()
        for r in rows:
            full = SETTINGS.upload_dir / r["log_path"]
            try:
                full.unlink(missing_ok=True)
                n_files += 1
            except OSError:
                pass
        cx.execute(
            "DELETE FROM support_tickets WHERE submitted_at < ?", (cutoff,),
        )
    print(f"✓ pruned {n_files} support upload(s) older than {args.days} days")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init-db", help="create schema")
    p.set_defaults(fn=_cmd_init_db)

    p = sub.add_parser("init-keys", help="generate Ed25519 keypair")
    p.add_argument("--force", action="store_true", help="overwrite existing")
    p.set_defaults(fn=_cmd_init_keys)

    p = sub.add_parser("show-pubkey", help="print pub key hex")
    p.set_defaults(fn=_cmd_show_pubkey)

    p = sub.add_parser("create-admin", help="add admin user")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--display-name", default="")
    p.set_defaults(fn=_cmd_create_admin)

    p = sub.add_parser("list-admins")
    p.set_defaults(fn=_cmd_list_admins)

    p = sub.add_parser("set-password", help="reset admin password")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.set_defaults(fn=_cmd_set_password)

    p = sub.add_parser("prune-uploads", help="delete old support zips")
    p.add_argument("--days", type=int, default=90)
    p.set_defaults(fn=_cmd_prune_uploads)

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
