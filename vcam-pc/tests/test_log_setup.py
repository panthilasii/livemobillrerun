"""Logging configuration + diagnostic export.

What we lock in
---------------

* ``configure_logging`` is idempotent: calling it twice does NOT
  duplicate handlers (each call would otherwise double the log
  volume on disk and spam the console).
* The on-disk file is UTF-8 (Thai characters in messages must
  survive a round-trip through the rotating handler).
* The redactor recognises every "this looks like a secret" key
  fragment we list, including substrings (``access_token``).
* The diagnostic ZIP contains the expected files and has redacted
  config, but unredacted devices.
* No license/private-key/token value ever ends up in the ZIP
  bytes -- regression test against accidental future leaks.
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src import log_setup


# ── configure_logging ───────────────────────────────────────────


class TestConfigureLogging:
    def test_handlers_attached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log_setup, "LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(
            log_setup, "LOG_FILE", tmp_path / "logs" / "npcreate.log",
        )
        # Reset root handlers so we have a clean state.
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)

        out = log_setup.configure_logging(verbose=False)

        assert out.exists()
        # We expect exactly one file handler + one stream handler.
        managed = [h for h in root.handlers
                   if getattr(h, "_npcreate_managed", False)]
        assert len(managed) == 2

    def test_idempotent(self, tmp_path, monkeypatch):
        """Re-calling does NOT pile on more handlers."""
        monkeypatch.setattr(log_setup, "LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(
            log_setup, "LOG_FILE", tmp_path / "logs" / "npcreate.log",
        )
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)

        log_setup.configure_logging()
        log_setup.configure_logging()
        log_setup.configure_logging()

        managed = [h for h in root.handlers
                   if getattr(h, "_npcreate_managed", False)]
        assert len(managed) == 2, (
            f"got {len(managed)} managed handlers after 3 calls -- "
            "configure_logging is not idempotent"
        )

    def test_thai_text_round_trip(self, tmp_path, monkeypatch):
        """Thai messages survive the file handler's encoding."""
        log_path = tmp_path / "logs" / "npcreate.log"
        monkeypatch.setattr(log_setup, "LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(log_setup, "LOG_FILE", log_path)
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)

        log_setup.configure_logging()
        logging.getLogger("test").info("ทดสอบภาษาไทย ๑๒๓")

        # Force flush.
        for h in root.handlers:
            try:
                h.flush()
            except Exception:
                pass

        text = log_path.read_text(encoding="utf-8")
        assert "ทดสอบภาษาไทย ๑๒๓" in text


# ── redaction ───────────────────────────────────────────────────


class TestRedact:
    @pytest.mark.parametrize("key", [
        "license_key",
        "License",
        "RAW_LICENSE",
        "access_token",
        "refresh_token",
        "private_key",
        "client_secret",
        "password",
        "seed",
    ])
    def test_sensitive_keys_scrubbed(self, key):
        out = log_setup._redact_value(key, "real-secret-12345")
        assert "real-secret" not in str(out)
        assert "redacted" in str(out)

    def test_unrelated_keys_pass_through(self):
        assert log_setup._redact_value("encode_width", 1920) == 1920
        assert log_setup._redact_value("device_serial", "ABC123") == "ABC123"
        assert log_setup._redact_value("model", "Redmi 14C") == "Redmi 14C"

    def test_recursive_dict(self):
        config = {
            "encode_width": 1920,
            "license_key": "AAAA-BBBB-CCCC",
            "nested": {
                "tiktok": {
                    "access_token": "leak",
                    "shop_id": "SHOP-001",
                },
            },
        }
        out = log_setup._redact_value("config", config)
        assert out["encode_width"] == 1920
        assert "AAAA" not in str(out["license_key"])
        assert "leak" not in str(out["nested"]["tiktok"]["access_token"])
        assert out["nested"]["tiktok"]["shop_id"] == "SHOP-001"

    def test_list_under_sensitive_key_redacted(self):
        out = log_setup._redact_value(
            "license_history", ["KEY-1", "KEY-2"],
        )
        assert "KEY-1" not in str(out)
        assert "KEY-2" not in str(out)


# ── collect_diagnostic_zip ──────────────────────────────────────


class TestDiagnosticZip:
    def _setup(self, tmp_path: Path, monkeypatch):
        """Stand up a fake PROJECT_ROOT in tmp_path and write a
        plausible config + activation + devices into it."""
        proj = tmp_path / "proj"
        proj.mkdir()
        log_dir = proj / "logs"
        log_dir.mkdir()
        log_file = log_dir / "npcreate.log"
        log_file.write_text(
            "2026-05-08 10:00:00 [INFO] test: started up\n"
            "2026-05-08 10:00:01 [DEBUG] adb: device online\n",
            encoding="utf-8",
        )

        (proj / "config.json").write_text(json.dumps({
            "encode_width": 1920,
            "license_key": "TOPSECRETKEY-12345",
            "tcp_port": 4747,
        }), encoding="utf-8")

        (proj / "activation.json").write_text(json.dumps({
            "license_key": "ACTIVATION-KEY-XXXX",
            "machine_id": "abc-123",
            "version": 1,
        }), encoding="utf-8")

        (proj / "customer_devices.json").write_text(json.dumps({
            "devices": [{
                "serial": "RIDX9876",
                "model": "Redmi 14C",
                "wifi_ip": "192.168.1.50",
            }],
        }), encoding="utf-8")

        monkeypatch.setattr(log_setup, "PROJECT_ROOT", proj)
        monkeypatch.setattr(log_setup, "LOG_DIR", log_dir)
        monkeypatch.setattr(log_setup, "LOG_FILE", log_file)
        return proj

    def test_zip_has_expected_files(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        out = tmp_path / "diag.zip"
        log_setup.collect_diagnostic_zip(out)

        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())

        for required in [
            "system_info.json",
            "logs/npcreate.log",
            "config.redacted.json",
            "activation.redacted.json",
            "customer_devices.json",
            "README.txt",
        ]:
            assert required in names, f"missing {required} in {names}"

    def test_license_key_scrubbed(self, tmp_path, monkeypatch):
        """The customer's actual license key MUST NOT appear
        anywhere in the ZIP bytes."""
        self._setup(tmp_path, monkeypatch)
        out = tmp_path / "diag.zip"
        log_setup.collect_diagnostic_zip(out)

        zip_bytes = out.read_bytes()
        # ZIP compression breaks naive substring search, so we
        # walk member-by-member instead.
        with zipfile.ZipFile(out) as zf:
            for name in zf.namelist():
                content = zf.read(name)
                assert b"TOPSECRETKEY-12345" not in content, (
                    f"license key leaked in {name}"
                )
                assert b"ACTIVATION-KEY-XXXX" not in content, (
                    f"activation leaked in {name}"
                )

    def test_devices_NOT_redacted(self, tmp_path, monkeypatch):
        """Device serials are needed for support and aren't
        sensitive; verify they survive the export untouched."""
        self._setup(tmp_path, monkeypatch)
        out = tmp_path / "diag.zip"
        log_setup.collect_diagnostic_zip(out)

        with zipfile.ZipFile(out) as zf:
            devs_raw = zf.read("customer_devices.json").decode("utf-8")
            devs = json.loads(devs_raw)

        assert devs["devices"][0]["serial"] == "RIDX9876"
        assert devs["devices"][0]["wifi_ip"] == "192.168.1.50"

    def test_system_info_has_app_version(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        out = tmp_path / "diag.zip"
        log_setup.collect_diagnostic_zip(out)

        with zipfile.ZipFile(out) as zf:
            info = json.loads(zf.read("system_info.json"))

        from src.branding import BRAND
        assert info["app_version"] == BRAND.version
        assert "platform" in info
        assert "python" in info

    def test_runs_when_files_missing(self, tmp_path, monkeypatch):
        """If config / activation / devices are absent, the export
        still produces a valid ZIP with whatever IS available. The
        customer's bug report is the priority -- not perfection."""
        proj = tmp_path / "empty"
        proj.mkdir()
        (proj / "logs").mkdir()
        log_file = proj / "logs" / "npcreate.log"
        log_file.write_text("...", encoding="utf-8")

        monkeypatch.setattr(log_setup, "PROJECT_ROOT", proj)
        monkeypatch.setattr(log_setup, "LOG_DIR", proj / "logs")
        monkeypatch.setattr(log_setup, "LOG_FILE", log_file)

        out = tmp_path / "diag.zip"
        log_setup.collect_diagnostic_zip(out)
        # Must be a valid ZIP and at least contain system_info + readme.
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert "system_info.json" in names
        assert "README.txt" in names


# ── filename suggestion ─────────────────────────────────────────


class TestFilename:
    def test_default_filename_has_app_version(self):
        from src.branding import BRAND
        name = log_setup.suggest_diagnostic_filename()
        assert BRAND.version in name
        assert name.endswith(".zip")
        assert name.startswith("npcreate-diag-")
