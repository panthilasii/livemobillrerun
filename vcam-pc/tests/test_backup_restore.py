"""Backup / restore -- round-trip the customer's portable state.

We patch ``PROJECT_ROOT`` and ``_HOME_DEVICES`` to live inside
``tmp_path`` so the tests never touch the real install. Each
sub-test sets up a representative state, creates a backup, then
restores it into a fresh empty install and checks for full
fidelity.

Coverage targets
----------------

* Happy path: every saved file lands at the destination unchanged.
* The signing seed (``.private_key``) MUST NOT be included even if
  it sits in PROJECT_ROOT.
* Path traversal entries (``../escape.txt``) are filtered on
  restore -- malicious ZIPs cannot escape PROJECT_ROOT.
* A foreign ZIP (no manifest) is refused with ``ValueError``
  rather than partially overwriting state.
* Schema mismatch raises ``ValueError`` and leaves the destination
  alone.
* Missing optional files don't break either side.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from src import backup_restore


# ── fixtures ────────────────────────────────────────────────────


def _patch_paths(monkeypatch, root: Path, home_devices: Path) -> None:
    """Redirect all on-disk locations into the test directory."""
    monkeypatch.setattr(backup_restore, "PROJECT_ROOT", root)
    monkeypatch.setattr(backup_restore, "_HOME_DEVICES", home_devices)


def _populated_install(root: Path, home_devices: Path) -> None:
    """Plausible v1.5 customer install with config + devices +
    license history + activation."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({
        "encode_width": 1920, "tcp_port": 4747,
    }), encoding="utf-8")
    (root / "device_profiles.json").write_text(json.dumps({
        "profiles": {"Redmi 14C": {"rotation": 90}},
    }), encoding="utf-8")
    (root / "customer_devices.json").write_text(json.dumps({
        "entries": {
            "RIDX9876": {"label": "Phone A", "model": "Redmi 14C"},
        },
    }), encoding="utf-8")
    (root / "license_history.json").write_text("[]", encoding="utf-8")
    (root / "activation.json").write_text(json.dumps({
        "license_key": "AAA-BBB-CCC", "machine_id": "abc-123",
    }), encoding="utf-8")
    home_devices.parent.mkdir(parents=True, exist_ok=True)
    home_devices.write_text(json.dumps({"entries": {}}), encoding="utf-8")


# ── round-trip ──────────────────────────────────────────────────


class TestRoundTrip:
    def test_create_then_restore_full_fidelity(self, tmp_path, monkeypatch):
        # Source install
        src_root = tmp_path / "src"
        src_home = tmp_path / "home_src" / ".npcreate" / "devices.json"
        _populated_install(src_root, src_home)

        # ── create backup ──
        _patch_paths(monkeypatch, src_root, src_home)
        zip_path = tmp_path / "backup.zip"
        backup_restore.create_backup(zip_path)
        assert zip_path.is_file()

        # ── restore into a different (empty) destination ──
        dst_root = tmp_path / "dst"
        dst_home = tmp_path / "home_dst" / ".npcreate" / "devices.json"
        dst_root.mkdir()
        _patch_paths(monkeypatch, dst_root, dst_home)

        restored = backup_restore.restore_backup(zip_path)
        assert len(restored) >= 5

        # Every file we put on disk must be present and equal at
        # the destination. We compare bytes because JSON formatting
        # round-trips depend on whitespace settings we don't
        # promise.
        for fname in (
            "config.json", "device_profiles.json", "customer_devices.json",
            "license_history.json", "activation.json",
        ):
            src = src_root / fname
            dst = dst_root / fname
            assert dst.is_file(), f"{fname} not restored"
            assert src.read_bytes() == dst.read_bytes()

        # Home-dir devices file restored too.
        assert dst_home.is_file()


# ── private_key excluded ────────────────────────────────────────


class TestPrivateKeyNotLeaked:
    def test_signing_seed_is_never_in_backup(self, tmp_path, monkeypatch):
        root = tmp_path / "src"
        home = tmp_path / "home" / ".npcreate" / "devices.json"
        _populated_install(root, home)
        # Plant a seed file in PROJECT_ROOT to make sure even if a
        # future bug widens the include set, the test catches it.
        (root / ".private_key").write_text(
            "deadbeef" * 8, encoding="utf-8",
        )
        _patch_paths(monkeypatch, root, home)

        zip_path = tmp_path / "backup.zip"
        backup_restore.create_backup(zip_path)

        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            assert ".private_key" not in names
            for name in names:
                content = zf.read(name)
                assert b"deadbeef" not in content, (
                    f"signing seed leaked into ZIP member {name}"
                )

    def test_restore_skips_private_key_in_a_handcrafted_zip(
        self, tmp_path, monkeypatch,
    ):
        """Even if a customer hand-crafts a ZIP with .private_key
        inside (or restores someone else's malicious bundle), the
        restore must NOT write it to disk."""
        root = tmp_path / "dst"
        root.mkdir()
        home = tmp_path / "home" / ".npcreate" / "devices.json"
        _patch_paths(monkeypatch, root, home)

        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps({
                "schema": 1,
                "app_name": "x",
                "app_version": "1.0",
                "created_at": "now",
                "files": [".private_key"],
            }))
            zf.writestr(".private_key", "deadbeef")
            zf.writestr("config.json", "{}")

        backup_restore.restore_backup(zip_path)
        assert not (root / ".private_key").exists()
        assert (root / "config.json").is_file()


# ── path traversal blocked ──────────────────────────────────────


class TestPathTraversal:
    def test_dot_dot_member_rejected(self, tmp_path, monkeypatch):
        root = tmp_path / "dst"
        root.mkdir()
        home = tmp_path / "home" / ".npcreate" / "devices.json"
        _patch_paths(monkeypatch, root, home)

        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps({
                "schema": 1,
                "app_name": "x",
                "app_version": "1.0",
                "created_at": "now",
                "files": ["../oops.txt"],
            }))
            zf.writestr("../oops.txt", "escape attempt")

        backup_restore.restore_backup(zip_path)
        # "../oops.txt" must NOT have been written outside root.
        assert not (tmp_path / "oops.txt").exists()


# ── manifest-less zips refused ──────────────────────────────────


class TestUnknownZipsRejected:
    def test_random_zip_raises_value_error(self, tmp_path, monkeypatch):
        root = tmp_path / "dst"
        root.mkdir()
        home = tmp_path / "home" / ".npcreate" / "devices.json"
        _patch_paths(monkeypatch, root, home)

        # A ZIP that's perfectly valid but isn't ours.
        zip_path = tmp_path / "random.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("hello.txt", "world")

        with pytest.raises(ValueError, match="manifest"):
            backup_restore.restore_backup(zip_path)
        # And nothing got written.
        assert not (root / "hello.txt").exists()

    def test_unknown_schema_rejected(self, tmp_path, monkeypatch):
        root = tmp_path / "dst"
        root.mkdir()
        home = tmp_path / "home" / ".npcreate" / "devices.json"
        _patch_paths(monkeypatch, root, home)

        zip_path = tmp_path / "future.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps({
                "schema": 99,
                "app_name": "x",
                "app_version": "9.9",
                "created_at": "now",
                "files": [],
            }))
        with pytest.raises(ValueError, match="schema"):
            backup_restore.restore_backup(zip_path)


# ── manifest peek ───────────────────────────────────────────────


class TestPeek:
    def test_read_manifest(self, tmp_path, monkeypatch):
        root = tmp_path / "src"
        home = tmp_path / "home" / ".npcreate" / "devices.json"
        _populated_install(root, home)
        _patch_paths(monkeypatch, root, home)

        zip_path = tmp_path / "b.zip"
        backup_restore.create_backup(zip_path)

        m = backup_restore.read_backup_manifest(zip_path)
        assert m is not None
        assert m.schema == 1
        assert m.app_version
        assert "config.json" in m.files

    def test_read_manifest_returns_none_for_garbage(self, tmp_path):
        zip_path = tmp_path / "garbage.zip"
        zip_path.write_bytes(b"not a zip")
        assert backup_restore.read_backup_manifest(zip_path) is None
