"""``DeviceEntry.auto_solve`` persistence + ``update_auto_solve``."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.customer_devices import DeviceLibrary  # noqa: E402


def test_auto_solve_defaults_false(tmp_path: Path):
    lib = DeviceLibrary()
    lib.upsert("SER1", model="Redmi 13C")
    assert lib.get("SER1").auto_solve is False


def test_update_auto_solve_roundtrips(tmp_path: Path):
    path = tmp_path / "devices.json"
    lib = DeviceLibrary()
    lib.upsert("SER1", model="Redmi 13C")
    lib.update_auto_solve("SER1", True)
    assert lib.get("SER1").auto_solve is True
    lib.save(path)

    reloaded = DeviceLibrary.load(path)
    assert reloaded.get("SER1").auto_solve is True


def test_update_auto_solve_unknown_serial_is_noop(tmp_path: Path):
    lib = DeviceLibrary()
    # Should not raise for a serial that isn't tracked.
    lib.update_auto_solve("GHOST", True)
    assert lib.get("GHOST") is None


def test_legacy_config_without_field_loads_false(tmp_path: Path):
    path = tmp_path / "devices.json"
    path.write_text(
        '{"entries": {"SER1": {"model": "Redmi 13C"}}}', encoding="utf-8"
    )
    lib = DeviceLibrary.load(path)
    assert lib.get("SER1").auto_solve is False
