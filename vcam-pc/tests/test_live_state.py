"""Live session state on ``DeviceEntry`` / ``DeviceLibrary``.

What we lock in
---------------

* ``start_live`` records the current timestamp + persists across
  save/load so an app crash mid-broadcast doesn't lose the timer.
* Calling ``start_live`` twice in a row keeps the ORIGINAL start
  time -- the customer accidentally double-clicking must not
  reset their broadcast counter to 0.
* ``stop_live`` clears the flag, returns the duration, and
  accumulates ``total_live_seconds``.
* ``stop_live`` on a phone that wasn't live returns 0 (no-op,
  no negative durations, no spurious accumulation).
* ``live_elapsed_seconds`` handles malformed timestamps without
  crashing (legacy data corruption).
* ``list_live_serials`` enumerates only currently-live phones.
* Legacy JSON without the new fields loads cleanly.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest

from src.customer_devices import DeviceEntry, DeviceLibrary


# ── start_live ──────────────────────────────────────────────────


class TestStartLive:
    def test_records_timestamp(self):
        lib = DeviceLibrary()
        ts = lib.start_live("AB123")
        assert ts
        # ISO 8601 parses cleanly (regression: a previous version
        # used isoformat(timespec="microseconds") which broke
        # datetime.fromisoformat in older Pythons).
        datetime.fromisoformat(ts)
        assert lib.get("AB123").is_live()

    def test_creates_entry_if_missing(self):
        """Convenience: customer marks a serial live before the
        device-poller has registered it. We auto-create rather
        than silently drop."""
        lib = DeviceLibrary()
        assert lib.get("NEWSERIAL") is None
        lib.start_live("NEWSERIAL")
        assert lib.get("NEWSERIAL") is not None
        assert lib.get("NEWSERIAL").is_live()

    def test_double_start_keeps_first_timestamp(self):
        """Customer mashes the Start button -- we must NOT reset
        the timer. The customer's first broadcast minute is more
        valuable than a clean state machine."""
        lib = DeviceLibrary()
        first = lib.start_live("AB123")
        time.sleep(0.05)
        second = lib.start_live("AB123")
        assert first == second


# ── stop_live ───────────────────────────────────────────────────


class TestStopLive:
    def test_returns_duration_and_accumulates(self):
        lib = DeviceLibrary()
        lib.start_live("AB123")
        # Force the start timestamp into the past so we get a
        # measurable elapsed value without sleeping for seconds.
        e = lib.get("AB123")
        past = (datetime.now() - timedelta(seconds=120))
        e.live_started_at = past.isoformat(timespec="seconds")

        duration = lib.stop_live("AB123")
        assert duration >= 119
        assert duration <= 121
        assert not lib.get("AB123").is_live()
        # Accumulator updated.
        assert lib.get("AB123").total_live_seconds == duration

    def test_stop_when_not_live_is_noop(self):
        lib = DeviceLibrary()
        lib.upsert("AB123")
        assert lib.stop_live("AB123") == 0
        assert lib.get("AB123").total_live_seconds == 0

    def test_two_sessions_accumulate(self):
        lib = DeviceLibrary()
        lib.start_live("AB123")
        e = lib.get("AB123")
        e.live_started_at = (datetime.now() - timedelta(seconds=60)).isoformat(timespec="seconds")
        lib.stop_live("AB123")

        lib.start_live("AB123")
        e.live_started_at = (datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds")
        lib.stop_live("AB123")

        # Within 2 s of 90 to allow for the tiny window between
        # setting the timestamp and computing elapsed.
        total = lib.get("AB123").total_live_seconds
        assert 88 <= total <= 92, total

    def test_unknown_serial_noop(self):
        lib = DeviceLibrary()
        assert lib.stop_live("GHOST") == 0


# ── live_elapsed_seconds ────────────────────────────────────────


class TestElapsed:
    def test_zero_when_not_live(self):
        e = DeviceEntry(serial="x")
        assert e.live_elapsed_seconds() == 0

    def test_malformed_timestamp_returns_zero(self):
        """A corrupt JSON file from an older crash must not blow
        up the timer rendering loop."""
        e = DeviceEntry(serial="x", live_started_at="not-a-date")
        assert e.live_elapsed_seconds() == 0

    def test_in_the_past_yields_positive(self):
        past = (datetime.now() - timedelta(seconds=42)).isoformat(timespec="seconds")
        e = DeviceEntry(serial="x", live_started_at=past)
        assert 41 <= e.live_elapsed_seconds() <= 44

    def test_future_timestamp_clamps_to_zero(self):
        """Edge case: clock-drift between phones could leave a
        timestamp slightly in the future. Negative elapsed would
        render as 'ไลฟ์อยู่ -3:00' in the UI which is worse than
        clamping to zero."""
        future = (datetime.now() + timedelta(seconds=120)).isoformat(timespec="seconds")
        e = DeviceEntry(serial="x", live_started_at=future)
        assert e.live_elapsed_seconds() == 0


# ── list_live_serials ───────────────────────────────────────────


class TestListLiveSerials:
    def test_only_live_devices(self):
        lib = DeviceLibrary()
        lib.upsert("OFF1")
        lib.upsert("OFF2")
        lib.start_live("LIVE1")
        lib.start_live("LIVE2")
        ser = sorted(lib.list_live_serials())
        assert ser == ["LIVE1", "LIVE2"]

    def test_empty_when_nothing_live(self):
        lib = DeviceLibrary()
        lib.upsert("X")
        lib.upsert("Y")
        assert lib.list_live_serials() == []


# ── persistence ─────────────────────────────────────────────────


class TestPersistence:
    def test_round_trip_keeps_live_state(self, tmp_path):
        path = tmp_path / "devices.json"
        lib = DeviceLibrary()
        lib.start_live("AB123")
        ts = lib.get("AB123").live_started_at
        lib.save(path)

        loaded = DeviceLibrary.load(path)
        assert loaded.get("AB123").live_started_at == ts
        assert loaded.get("AB123").is_live()

    def test_legacy_json_loads_clean(self, tmp_path):
        """v1.5.x devices.json had no live_started_at /
        total_live_seconds keys. They must default to safe
        values (empty / 0) so an upgrade-in-place doesn't crash
        the loader."""
        path = tmp_path / "devices.json"
        path.write_text(json.dumps({
            "entries": {
                "OLD1": {"label": "A", "model": "Redmi"},
            },
        }), encoding="utf-8")

        lib = DeviceLibrary.load(path)
        e = lib.get("OLD1")
        assert e.live_started_at == ""
        assert e.total_live_seconds == 0
        assert not e.is_live()

    def test_total_live_seconds_persists(self, tmp_path):
        path = tmp_path / "devices.json"
        lib = DeviceLibrary()
        lib.upsert("AB123")
        lib.get("AB123").total_live_seconds = 7200
        lib.save(path)

        loaded = DeviceLibrary.load(path)
        assert loaded.get("AB123").total_live_seconds == 7200
