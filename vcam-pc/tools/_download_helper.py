"""Robust HTTPS downloader for the admin setup scripts.

Why this exists
---------------

Python on macOS ships *without* a populated CA bundle by default —
the user is expected to run ``Install Certificates.command`` from
the Python install dir before HTTPS works. A non-trivial number of
admins skip that step and then ``urllib`` fails with::

    SSL: CERTIFICATE_VERIFY_FAILED

Rather than write a multi-page README about ``Install Certificates``,
we fall back through three strategies so the setup scripts "just
work" on any reasonably-modern Mac, Windows, or Linux box:

1. ``urllib.request`` with the default context (works on most boxes
   if the CA store is healthy).
2. Same, but with the ``certifi`` CA bundle (auto-pip-installed
   the first time we call this; certifi is tiny and a transitive
   dep of pip itself, so it's always available after a moment).
3. Shell-out to the system ``curl`` (which carries its own CA
   bundle on every modern OS).

The caller passes a URL and a destination path; we either return
the path on success or raise ``DownloadError``.
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path


class DownloadError(Exception):
    """Raised when every fallback failed."""


def _try_urllib(url: str, dst: Path, ctx: ssl.SSLContext | None = None) -> None:
    req = urllib.request.Request(
        url,
        headers={
            # Some CDNs reject the default Python user-agent.
            "User-Agent": "NP-Create-Setup/1.4 (+https://line.me/R/ti/p/@npcreate)",
        },
    )
    opener_args: dict = {"timeout": 60}
    if ctx is not None:
        opener_args["context"] = ctx
    with urllib.request.urlopen(req, **opener_args) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f, length=1 << 20)


def _try_certifi(url: str, dst: Path) -> bool:
    try:
        import certifi  # type: ignore
    except ImportError:
        # Try to pip-install certifi quietly. It's tiny (~250 KB),
        # has no native deps, and ships on PyPI for every Python.
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", "--user",
                 "certifi"],
                check=True, capture_output=True, timeout=120,
            )
            import certifi  # type: ignore
        except (subprocess.SubprocessError, ImportError):
            return False
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        _try_urllib(url, dst, ctx=ctx)
        return True
    except Exception as e:
        print(f"  ✗ certifi attempt failed: {e}", file=sys.stderr)
        return False


def _try_curl(url: str, dst: Path) -> bool:
    if shutil.which("curl") is None:
        return False
    cmd = [
        "curl", "--fail", "--silent", "--show-error", "--location",
        "--retry", "3", "--connect-timeout", "30",
        "--user-agent", "NP-Create-Setup/1.4",
        "--output", str(dst),
        url,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=900)
        return True
    except subprocess.SubprocessError as e:
        print(f"  ✗ curl attempt failed: {e}", file=sys.stderr)
        return False


def download(url: str, dst: Path) -> Path:
    """Best-effort HTTPS download with three tiered fallbacks."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    if tmp.is_file():
        try:
            tmp.unlink()
        except OSError:
            pass

    attempts = [
        ("system CA", lambda: _try_urllib_safe(url, tmp)),
        ("certifi CA", lambda: _try_certifi(url, tmp)),
        ("curl", lambda: _try_curl(url, tmp)),
    ]
    last_err = None
    for label, fn in attempts:
        try:
            ok = fn()
            if ok and tmp.stat().st_size > 0:
                tmp.replace(dst)
                return dst
        except Exception as e:
            last_err = e
            print(f"  ✗ {label} attempt failed: {e}", file=sys.stderr)
    raise DownloadError(
        f"All download strategies failed for {url}. "
        f"Last error: {last_err}"
    )


def _try_urllib_safe(url: str, dst: Path) -> bool:
    try:
        _try_urllib(url, dst)
        return True
    except Exception as e:
        # Don't blow up — let the caller try the next tier.
        print(f"  ✗ system-CA attempt failed: {e}", file=sys.stderr)
        return False
