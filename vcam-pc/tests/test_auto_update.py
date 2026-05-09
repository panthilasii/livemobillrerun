"""Auto-update module: signing, version compare, atomic apply.

We exercise the public surface of ``src.auto_update`` end-to-end
WITHOUT touching the network: ``urllib.request.urlopen`` is monkey-
patched with canned responses.

What we lock in
---------------

Manifest verification

* A correctly-signed manifest produces an ``UpdateManifest`` ONLY if
  ``version > BRAND.version``.
* A manifest signed with the wrong seed must be rejected (silent
  ``None`` return -- never raises into the UI).
* A manifest with malformed envelope (missing ``signature``,
  garbage payload, non-JSON HTTP body) returns ``None``.
* Network errors return ``None``.

Version compare

* ``is_newer`` is strict-greater-than on the (major, minor, patch)
  tuple.
* Bad version strings degrade to ``False`` (we never auto-update on
  ambiguity).

Patch apply

* The happy path replaces ``src/`` and leaves ``src.bak/`` for
  rollback.
* If the patch ZIP is missing ``main.py`` we abort BEFORE swapping
  -- the live install must remain bootable.
* Path traversal in ZIP entries (``../../etc/passwd``) is rejected.
* SHA256 mismatch on the downloaded archive is rejected.
* Size cap triggers when the server feeds us a giant blob.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src import _ed25519, auto_update
from src._pubkey import PUBLIC_KEY_HEX
from src.branding import BRAND


# ── fixtures ────────────────────────────────────────────────────


def _real_seed() -> bytes:
    """Read the admin signing seed; skip if absent."""
    p = Path(__file__).resolve().parent.parent / ".private_key"
    if not p.is_file():
        pytest.skip(".private_key not on disk; skipping signing tests")
    seed = bytes.fromhex(p.read_text(encoding="utf-8").strip())
    _, derived_pub = _ed25519.keypair_from_seed(seed)
    if derived_pub.hex() != PUBLIC_KEY_HEX:
        pytest.skip(".private_key does not match _pubkey.py")
    return seed


def _signed_envelope(payload: dict, seed: bytes) -> dict:
    """Sign exactly like ``tools/publish_update.py`` does."""
    pb = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    sig = _ed25519.sign(seed, pb)
    return {
        "format_version": 1,
        "payload": base64.urlsafe_b64encode(pb).decode("ascii").rstrip("="),
        "signature": sig.hex(),
    }


def _manifest_payload(version: str, **overrides) -> dict:
    """Plausible manifest payload with sensible defaults."""
    out = {
        "version": version,
        "kind": "source",
        "download_url": f"https://example.invalid/p-{version}.zip",
        "sha256": "0" * 64,
        "notes_th": "ทดสอบ",
        "min_compat_version": "1.0.0",
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out.update(overrides)
    return out


class _Resp:
    """Minimal urllib response stand-in."""
    def __init__(self, data: bytes, headers: dict | None = None) -> None:
        self._buf = io.BytesIO(data)
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read() if n < 0 else self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _patch_urlopen(*responses):
    """Patch ``urlopen`` to return ``responses`` in order. Each
    response is either bytes (treated as a 200 with no headers) or
    a ``_Resp`` instance for fine control.

    The mock is a callable that yields one canned response per
    call, in order, so a single test can sequence
    ``GET manifest -> GET patch.zip``.
    """
    queue = list(responses)

    def _open(*_a, **_kw):
        if not queue:
            raise AssertionError("urlopen called more times than responses")
        nxt = queue.pop(0)
        if isinstance(nxt, bytes):
            return _Resp(nxt)
        if isinstance(nxt, _Resp):
            return nxt
        if isinstance(nxt, Exception):
            raise nxt
        raise AssertionError(f"unexpected response type: {type(nxt)}")

    return patch.object(auto_update.urllib.request, "urlopen", side_effect=_open)


# ── version compare ─────────────────────────────────────────────


class TestIsNewer:
    @pytest.mark.parametrize("a,b,expected", [
        ("1.5.1", "1.5.0", True),
        ("1.5.0", "1.5.0", False),  # equal is NOT newer
        ("1.5.0", "1.5.1", False),
        ("1.6.0", "1.5.99", True),
        ("2.0.0", "1.99.99", True),
        ("1.4.10", "1.4.9", True),
        ("1.4.10", "1.4.2", True),  # numeric, not lex, compare
    ])
    def test_basic(self, a, b, expected):
        assert auto_update.is_newer(a, b) is expected

    def test_garbage_never_newer(self):
        assert auto_update.is_newer("garbage", "1.0.0") is False
        assert auto_update.is_newer("1.0.0", "also bad") is False

    def test_pre_release_strips(self):
        # Pre-release tags are stripped; the underlying version
        # compare ignores them so we never advance into a pre-rel.
        assert auto_update.is_newer("1.5.1-beta", "1.5.0") is True
        assert auto_update.is_newer("1.5.0-beta", "1.5.0") is False


# ── manifest verify (signed) ────────────────────────────────────


class TestFetchManifest:
    """End-to-end: ``urlopen`` returns canned bytes, we walk the
    public ``fetch_manifest`` and check the verified output."""

    def test_valid_signed_newer_returns_manifest(self):
        seed = _real_seed()
        future = _bump(BRAND.version, +1)
        env = _signed_envelope(_manifest_payload(future), seed)
        with _patch_urlopen(json.dumps(env).encode("utf-8")):
            m = auto_update.fetch_manifest("https://example.invalid/m.json")
        assert m is not None
        assert m.version == future
        assert m.kind == "source"

    def test_same_version_returns_none(self):
        """Customer is already on this version -- nothing to do."""
        seed = _real_seed()
        env = _signed_envelope(_manifest_payload(BRAND.version), seed)
        with _patch_urlopen(json.dumps(env).encode("utf-8")):
            m = auto_update.fetch_manifest("https://example.invalid/m.json")
        assert m is None

    def test_older_version_returns_none(self):
        seed = _real_seed()
        old = _bump(BRAND.version, -1)
        env = _signed_envelope(_manifest_payload(old), seed)
        with _patch_urlopen(json.dumps(env).encode("utf-8")):
            m = auto_update.fetch_manifest("https://example.invalid/m.json")
        assert m is None

    def test_wrong_seed_rejected(self):
        """Forged signature must be rejected silently."""
        fake = b"\x42" * 32
        env = _signed_envelope(
            _manifest_payload(_bump(BRAND.version, +1)), fake,
        )
        with _patch_urlopen(json.dumps(env).encode("utf-8")):
            m = auto_update.fetch_manifest("https://example.invalid/m.json")
        assert m is None

    def test_garbage_bytes_returns_none(self):
        with _patch_urlopen(b"not even json"):
            m = auto_update.fetch_manifest("https://example.invalid/m.json")
        assert m is None

    def test_missing_payload_returns_none(self):
        env = {"format_version": 1, "signature": "deadbeef"}
        with _patch_urlopen(json.dumps(env).encode("utf-8")):
            m = auto_update.fetch_manifest("https://example.invalid/m.json")
        assert m is None

    def test_network_error_returns_none(self):
        import urllib.error
        with _patch_urlopen(urllib.error.URLError("dns")):
            m = auto_update.fetch_manifest("https://example.invalid/m.json")
        assert m is None

    def test_min_compat_above_current_marks_full(self):
        """If min_compat is above the running version, we still
        return the manifest but rewrite ``kind`` to ``full`` so the
        UI sends the customer to the installer page."""
        seed = _real_seed()
        bumped = _bump(BRAND.version, +1)
        # Pick a min_compat that's higher than the running version
        # (i.e. higher than BRAND.version). Use a major bump.
        unreachable = _bump_major(BRAND.version, +5)
        env = _signed_envelope(
            _manifest_payload(bumped, min_compat_version=unreachable),
            seed,
        )
        with _patch_urlopen(json.dumps(env).encode("utf-8")):
            m = auto_update.fetch_manifest("https://example.invalid/m.json")
        assert m is not None
        assert m.kind == "full"


# ── helpers ─────────────────────────────────────────────────────


def _bump(v: str, delta: int) -> str:
    parts = [int(p) for p in v.split(".")]
    parts[-1] += delta
    if parts[-1] < 0:
        parts[-1] = 0
        parts[-2] = max(0, parts[-2] - 1)
    return ".".join(str(p) for p in parts)


def _bump_major(v: str, delta: int) -> str:
    parts = [int(p) for p in v.split(".")]
    parts[0] = max(0, parts[0] + delta)
    return ".".join(str(p) for p in parts)


# ── download_patch ──────────────────────────────────────────────


class TestDownloadPatch:
    def _manifest(self, *, sha256: str, kind: str = "source",
                  url: str = "https://example.invalid/p.zip"
                  ) -> auto_update.UpdateManifest:
        return auto_update.UpdateManifest(
            version="9.9.9", kind=kind,
            download_url=url, sha256_hex=sha256,
            notes_th="x",
        )

    def test_happy_path_writes_zip_to_staging(self, tmp_path, monkeypatch):
        # tmp_path → staging dir so we don't pollute /tmp
        monkeypatch.setattr(auto_update.tempfile, "gettempdir",
                            lambda: str(tmp_path))
        zip_bytes = b"this is a fake zip"
        manifest = self._manifest(
            sha256=hashlib.sha256(zip_bytes).hexdigest(),
        )
        with _patch_urlopen(zip_bytes):
            out = auto_update.download_patch(manifest)
        assert out.exists()
        assert out.read_bytes() == zip_bytes
        assert out.suffix == ".zip"

    def test_sha256_mismatch_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auto_update.tempfile, "gettempdir",
                            lambda: str(tmp_path))
        manifest = self._manifest(sha256="0" * 64)  # WRONG hash
        with _patch_urlopen(b"some bytes"), \
                pytest.raises(auto_update.UpdateError, match="sha256"):
            auto_update.download_patch(manifest)

    def test_kind_full_refuses_auto_apply(self, tmp_path):
        manifest = self._manifest(sha256="0" * 64, kind="full")
        with pytest.raises(auto_update.UpdateError, match="full"):
            auto_update.download_patch(manifest)

    def test_size_cap_protects_us(self, monkeypatch, tmp_path):
        monkeypatch.setattr(auto_update.tempfile, "gettempdir",
                            lambda: str(tmp_path))
        # Pretend the server says Content-Length is gigantic.
        big_resp = _Resp(
            b"x" * 64,
            headers={"Content-Length": str(auto_update.MAX_PATCH_BYTES + 1)},
        )
        manifest = self._manifest(sha256="0" * 64)
        with _patch_urlopen(big_resp), \
                pytest.raises(auto_update.UpdateError, match="too large"):
            auto_update.download_patch(manifest)


# ── apply_patch ─────────────────────────────────────────────────


class TestApplyPatch:
    """Atomic swap behaviour. We build a fake ``src/`` tree on disk
    then ask ``apply_patch`` to replace it. Tests verify the post-
    state matches expectations."""

    def _make_patch_zip(self, tmp_path: Path, files: dict,
                         *, prefix: str = "") -> Path:
        z = tmp_path / "patch.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for rel, data in files.items():
                zf.writestr(prefix + rel, data)
        return z

    def _make_fake_src(self, parent: Path) -> Path:
        src = parent / "src"
        src.mkdir()
        (src / "main.py").write_text("# OLD main\n", encoding="utf-8")
        (src / "lib.py").write_text("# OLD lib\n", encoding="utf-8")
        sub = src / "subpkg"
        sub.mkdir()
        (sub / "x.py").write_text("# OLD x\n", encoding="utf-8")
        return src

    def test_happy_path_replaces_src(self, tmp_path):
        src = self._make_fake_src(tmp_path)
        patch = self._make_patch_zip(tmp_path, {
            "main.py": "# NEW main\n",
            "lib.py": "# NEW lib\n",
            "subpkg/x.py": "# NEW x\n",
        })

        auto_update.apply_patch(patch, src_dir=src)

        # New content is in place.
        assert (src / "main.py").read_text(encoding="utf-8") == "# NEW main\n"
        assert (src / "lib.py").read_text(encoding="utf-8") == "# NEW lib\n"
        assert (src / "subpkg" / "x.py").read_text(encoding="utf-8") == "# NEW x\n"

        # Old content still on disk in src.bak for rollback.
        bak = tmp_path / "src.bak"
        assert bak.is_dir()
        assert (bak / "main.py").read_text(encoding="utf-8") == "# OLD main\n"

    def test_missing_main_py_aborts_before_swap(self, tmp_path):
        """Refuse to apply a patch that would brick the install."""
        src = self._make_fake_src(tmp_path)
        # Patch contains lib.py but NOT main.py.
        patch = self._make_patch_zip(tmp_path, {
            "lib.py": "# NEW lib\n",
        })

        with pytest.raises(auto_update.UpdateError, match="main.py"):
            auto_update.apply_patch(patch, src_dir=src)

        # Live src/ is untouched.
        assert (src / "main.py").read_text(encoding="utf-8") == "# OLD main\n"
        assert not (tmp_path / "src.bak").exists()
        assert not (tmp_path / "src.new").exists()

    def test_path_traversal_rejected(self, tmp_path):
        src = self._make_fake_src(tmp_path)
        # Malicious zip member tries to escape src/.
        patch = self._make_patch_zip(tmp_path, {
            "main.py": "# ok\n",
            "../../../etc/passwd": "haha\n",
        })

        with pytest.raises(auto_update.UpdateError, match="unsafe"):
            auto_update.apply_patch(patch, src_dir=src)

        # Live src/ is untouched.
        assert (src / "main.py").read_text(encoding="utf-8") == "# OLD main\n"

    def test_strips_common_prefix(self, tmp_path):
        """Some zips ship ``src/foo.py`` instead of ``foo.py`` --
        we transparently strip the prefix so both archive shapes
        work."""
        src = self._make_fake_src(tmp_path)
        patch = self._make_patch_zip(tmp_path, {
            "main.py": "# NEW main\n",
            "lib.py": "# NEW lib\n",
        }, prefix="src/")

        auto_update.apply_patch(patch, src_dir=src)
        assert (src / "main.py").read_text(encoding="utf-8") == "# NEW main\n"

    def test_refuses_non_src_directory(self, tmp_path):
        bogus = tmp_path / "myapp"
        bogus.mkdir()
        (bogus / "main.py").write_text("ok", encoding="utf-8")
        patch = self._make_patch_zip(tmp_path, {"main.py": "x"})
        with pytest.raises(auto_update.UpdateError, match="non-'src'"):
            auto_update.apply_patch(patch, src_dir=bogus)

    def test_replaces_existing_bak(self, tmp_path):
        """If a previous apply left src.bak around, we should
        clobber it so the customer's disk doesn't grow each
        update."""
        src = self._make_fake_src(tmp_path)
        # Pre-existing bak from an old apply.
        old_bak = tmp_path / "src.bak"
        old_bak.mkdir()
        (old_bak / "stale.txt").write_text("STALE", encoding="utf-8")

        patch = self._make_patch_zip(tmp_path, {
            "main.py": "# NEW main\n",
        })
        auto_update.apply_patch(patch, src_dir=src)

        # Old bak content should be replaced by the previous src/.
        assert not (tmp_path / "src.bak" / "stale.txt").exists()
        assert (tmp_path / "src.bak" / "main.py").read_text(
            encoding="utf-8",
        ) == "# OLD main\n"


# ── _common_prefix_to_strip ─────────────────────────────────────


class TestCommonPrefix:
    def test_all_share_prefix(self):
        members = ["src/a.py", "src/b/c.py", "src/d/"]
        assert auto_update._common_prefix_to_strip(members) == "src/"

    def test_no_common_prefix(self):
        members = ["a.py", "b/c.py"]
        assert auto_update._common_prefix_to_strip(members) == ""

    def test_macosx_dir_ignored(self):
        members = ["__MACOSX/a", "__MACOSX/b"]
        assert auto_update._common_prefix_to_strip(members) == ""

    def test_empty(self):
        assert auto_update._common_prefix_to_strip([]) == ""
