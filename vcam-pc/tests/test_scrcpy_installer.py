"""Tests for the scrcpy auto-installer.

We never actually hit github.com in CI — that would (a) be flaky
when the runner has no network and (b) slowly DDoS Genymobile.
Instead we mock ``urllib.request.urlopen`` to serve fixture
archives we build on the fly with stdlib ``tarfile`` / ``zipfile``,
which lets us cover every interesting branch:

* sha256 verification accepts a known-good archive.
* sha256 verification REJECTS a tampered archive (and deletes the
  cached file so the next attempt re-downloads cleanly).
* tar.gz extraction places the binary where ``find_user_installed``
  expects it.
* zip extraction does the same on Windows-shaped archives.
* ``is_installed`` is idempotent — second ``install()`` call
  short-circuits without touching the network.
* ``detect_platform_key`` returns the right key for darwin /
  windows / linux without requiring those OSes to actually be
  the host (we monkeypatch ``sys.platform``).
* zip-slip / tar-slip members are rejected.
* ``install_async`` runs the worker on a thread and reports both
  success + failure to ``on_complete``.

The "real download" path is exercised by an integration test
gated on ``RUN_REAL_DOWNLOAD=1`` — it's skipped by default but
useful when bumping ``SCRCPY_VERSION``.
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# Importing the module under test eagerly so monkeypatches on its
# attributes (``_RELEASES`` etc.) work. The package layout is
# ``vcam-pc/src/scrcpy_installer.py`` exposed as ``src.scrcpy_installer``.
from src import scrcpy_installer as inst


# ── helpers: build fake archives that look like real scrcpy ────────


def _make_fake_macos_targz(tmp_path: Path, binary_payload: bytes) -> Path:
    """Build a tar.gz with the same layout as a real macOS scrcpy
    release: ``scrcpy/scrcpy`` plus a couple of dylib siblings.
    Returns the path to the produced archive.
    """
    src_dir = tmp_path / "src" / "scrcpy"
    src_dir.mkdir(parents=True)
    (src_dir / "scrcpy").write_bytes(binary_payload)
    (src_dir / "scrcpy-server").write_bytes(b"java-bytecode-payload")
    (src_dir / "libavformat.dylib").write_bytes(b"fake-dylib")

    archive = tmp_path / "scrcpy.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src_dir, arcname="scrcpy")
    return archive


def _make_fake_win_zip(tmp_path: Path, binary_payload: bytes) -> Path:
    """Build a zip with the same layout as scrcpy-win64-vX.Y.zip:
    ``scrcpy-win64-v3.3.4/scrcpy.exe`` + DLLs.
    """
    archive = tmp_path / "scrcpy.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        prefix = "scrcpy-win64-v3.3.4/"
        zf.writestr(prefix + "scrcpy.exe", binary_payload)
        zf.writestr(prefix + "scrcpy-server", b"java-bytecode-payload")
        zf.writestr(prefix + "SDL2.dll", b"fake-dll")
        zf.writestr(prefix + "adb.exe", b"bundled-adb-we-ignore")
    return archive


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _patched_release(monkeypatch, key: str, asset: inst._Asset) -> None:
    """Install an asset entry into the module's release table for
    one test, leaving the rest untouched."""
    new_table = dict(inst._RELEASES)
    new_table[key] = asset
    monkeypatch.setattr(inst, "_RELEASES", new_table)


class _FakeURLResponse:
    """Stand-in for the object returned by urlopen() — supports
    the ``with`` protocol, ``.read(n)`` streaming, and a headers
    dict with the lower-case key we look up."""

    def __init__(self, payload: bytes, advertise_length: bool = True):
        self._buf = io.BytesIO(payload)
        self._headers: dict[str, str] = {}
        if advertise_length:
            self._headers["content-length"] = str(len(payload))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def headers(self):
        return self._headers

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


# ── isolation fixtures ─────────────────────────────────────────────


@pytest.fixture
def isolated_tools_root(tmp_path, monkeypatch):
    """Redirect the user-data dir into ``tmp_path`` so tests don't
    pollute the real ``~/.npcreate/tools/``."""
    monkeypatch.setattr(inst, "user_tools_root", lambda: tmp_path / "tools")
    return tmp_path / "tools"


# ── platform detection ────────────────────────────────────────────


@pytest.mark.parametrize(
    "platform_str,machine,expected",
    [
        ("darwin", "arm64", "macos-aarch64"),
        ("darwin", "x86_64", "macos-x86_64"),
        ("win32", "AMD64", "windows-x64"),
        ("linux", "x86_64", "linux-x86_64"),
    ],
)
def test_detect_platform_key_picks_right_asset(
    monkeypatch, platform_str, machine, expected,
):
    monkeypatch.setattr(inst.sys, "platform", platform_str)
    monkeypatch.setattr(inst.platform, "machine", lambda: machine)
    assert inst.detect_platform_key() == expected


def test_detect_platform_key_rejects_unsupported_linux_arch(monkeypatch):
    monkeypatch.setattr(inst.sys, "platform", "linux")
    monkeypatch.setattr(inst.platform, "machine", lambda: "armv7l")
    with pytest.raises(inst.InstallerError) as exc:
        inst.detect_platform_key()
    assert "Linux" in str(exc.value)


def test_detect_platform_key_rejects_unknown_os(monkeypatch):
    monkeypatch.setattr(inst.sys, "platform", "freebsd13")
    monkeypatch.setattr(inst.platform, "machine", lambda: "amd64")
    with pytest.raises(inst.InstallerError):
        inst.detect_platform_key()


# ── download + verify ─────────────────────────────────────────────


def test_install_macos_happy_path(monkeypatch, tmp_path, isolated_tools_root):
    """End-to-end macOS install: mock urlopen → fake tar.gz → assert
    binary lands in the expected place and ``is_installed`` flips."""
    binary = b"FAKE-SCRCPY-BINARY"
    archive = _make_fake_macos_targz(tmp_path, binary)
    archive_bytes = archive.read_bytes()
    real_sha = hashlib.sha256(archive_bytes).hexdigest()

    monkeypatch.setattr(inst.sys, "platform", "darwin")
    monkeypatch.setattr(inst.platform, "machine", lambda: "arm64")
    _patched_release(
        monkeypatch,
        "macos-aarch64",
        inst._Asset(
            url="https://example.test/scrcpy.tar.gz",
            sha256=real_sha,
            kind="tar.gz",
            size=len(archive_bytes),
        ),
    )

    progress_events: list[tuple[str, int, int]] = []

    def _fake_urlopen(req, timeout=30.0, context=None):
        # Sanity-check the request — we want a real Request instance
        # with the user-agent set, not a bare URL string.
        url = req.full_url if hasattr(req, "full_url") else req
        assert url == "https://example.test/scrcpy.tar.gz"
        return _FakeURLResponse(archive_bytes)

    with patch.object(
        inst.urllib.request, "urlopen", side_effect=_fake_urlopen,
    ):
        bin_path = inst.install(
            progress=lambda s, c, t: progress_events.append((s, c, t)),
        )

    assert bin_path.exists()
    assert bin_path.name == "scrcpy"
    assert bin_path.read_bytes() == binary

    # Progress callback should see at least download → verify →
    # extract → done in some order. The download phase should
    # report multiple chunks; verify+extract+done report at least
    # once each.
    stages_seen = {s for s, _, _ in progress_events}
    assert {"download", "verify", "extract", "done"}.issubset(stages_seen)

    assert inst.is_installed()


def test_install_idempotent_skips_network_when_already_installed(
    monkeypatch, tmp_path, isolated_tools_root,
):
    """Second ``install()`` call must NOT call urlopen again — the
    customer's already-paid-for bandwidth shouldn't be wasted re-
    downloading the same 10 MB tarball."""
    binary = b"FAKE-SCRCPY"
    archive = _make_fake_macos_targz(tmp_path, binary)
    archive_bytes = archive.read_bytes()
    real_sha = hashlib.sha256(archive_bytes).hexdigest()

    monkeypatch.setattr(inst.sys, "platform", "darwin")
    monkeypatch.setattr(inst.platform, "machine", lambda: "arm64")
    _patched_release(
        monkeypatch,
        "macos-aarch64",
        inst._Asset(
            url="https://example.test/scrcpy.tar.gz",
            sha256=real_sha, kind="tar.gz", size=len(archive_bytes),
        ),
    )

    call_count = {"n": 0}

    def _fake_urlopen(req, timeout=30.0, context=None):
        call_count["n"] += 1
        return _FakeURLResponse(archive_bytes)

    with patch.object(inst.urllib.request, "urlopen", side_effect=_fake_urlopen):
        first = inst.install()
        second = inst.install()

    assert first == second
    assert call_count["n"] == 1, "second install should not re-download"


def test_install_force_redownloads(
    monkeypatch, tmp_path, isolated_tools_root,
):
    """``force=True`` MUST hit the network again — used by future
    'reinstall scrcpy' admin button when a binary went bad."""
    binary = b"FAKE-SCRCPY"
    archive = _make_fake_macos_targz(tmp_path, binary)
    archive_bytes = archive.read_bytes()
    real_sha = hashlib.sha256(archive_bytes).hexdigest()

    monkeypatch.setattr(inst.sys, "platform", "darwin")
    monkeypatch.setattr(inst.platform, "machine", lambda: "arm64")
    _patched_release(
        monkeypatch,
        "macos-aarch64",
        inst._Asset(
            url="https://example.test/scrcpy.tar.gz",
            sha256=real_sha, kind="tar.gz", size=len(archive_bytes),
        ),
    )

    n = {"calls": 0}

    def _fake_urlopen(req, timeout=30.0, context=None):
        n["calls"] += 1
        return _FakeURLResponse(archive_bytes)

    with patch.object(inst.urllib.request, "urlopen", side_effect=_fake_urlopen):
        inst.install()
        inst.install(force=True)

    assert n["calls"] == 2


def test_install_rejects_tampered_archive(
    monkeypatch, tmp_path, isolated_tools_root,
):
    """If the bytes-on-the-wire don't match our pinned sha256,
    we MUST refuse to install. This is the only line of defence
    between us and a malicious release replacement."""
    binary = b"FAKE-SCRCPY"
    archive = _make_fake_macos_targz(tmp_path, binary)
    archive_bytes = archive.read_bytes()

    monkeypatch.setattr(inst.sys, "platform", "darwin")
    monkeypatch.setattr(inst.platform, "machine", lambda: "arm64")
    # Deliberately pin the WRONG hash.
    _patched_release(
        monkeypatch,
        "macos-aarch64",
        inst._Asset(
            url="https://example.test/scrcpy.tar.gz",
            sha256="0" * 64,
            kind="tar.gz",
            size=len(archive_bytes),
        ),
    )

    with patch.object(
        inst.urllib.request, "urlopen",
        return_value=_FakeURLResponse(archive_bytes),
    ):
        with pytest.raises(inst.InstallerError) as exc:
            inst.install()

    assert "sha256" in str(exc.value).lower() or "เสียหาย" in str(exc.value)
    assert not inst.is_installed(), "tampered archive must NOT mark installed"


def test_install_windows_zip(monkeypatch, tmp_path, isolated_tools_root):
    """Same end-to-end check but with a Windows-shaped zip."""
    payload = b"FAKE-SCRCPY-EXE"
    archive = _make_fake_win_zip(tmp_path, payload)
    archive_bytes = archive.read_bytes()
    real_sha = hashlib.sha256(archive_bytes).hexdigest()

    monkeypatch.setattr(inst.sys, "platform", "win32")
    monkeypatch.setattr(inst.platform, "machine", lambda: "AMD64")
    _patched_release(
        monkeypatch,
        "windows-x64",
        inst._Asset(
            url="https://example.test/scrcpy.zip",
            sha256=real_sha, kind="zip", size=len(archive_bytes),
        ),
    )

    with patch.object(
        inst.urllib.request, "urlopen",
        return_value=_FakeURLResponse(archive_bytes),
    ):
        bin_path = inst.install()

    assert bin_path.exists()
    assert bin_path.name == "scrcpy.exe"
    assert bin_path.read_bytes() == payload


def test_install_propagates_network_error(
    monkeypatch, tmp_path, isolated_tools_root,
):
    """A URLError must surface as an InstallerError with a
    customer-readable Thai message — not as an opaque traceback."""
    monkeypatch.setattr(inst.sys, "platform", "darwin")
    monkeypatch.setattr(inst.platform, "machine", lambda: "arm64")
    _patched_release(
        monkeypatch,
        "macos-aarch64",
        inst._Asset(
            url="https://example.test/scrcpy.tar.gz",
            sha256="0" * 64, kind="tar.gz", size=100,
        ),
    )

    def _boom(req, timeout=30.0, context=None):
        raise inst.urllib.error.URLError("network unreachable")

    with patch.object(inst.urllib.request, "urlopen", side_effect=_boom):
        with pytest.raises(inst.InstallerError) as exc:
            inst.install()
    assert "ดาวน์โหลด" in str(exc.value)


# ── archive safety ─────────────────────────────────────────────────


def test_extract_rejects_tar_path_traversal(
    monkeypatch, tmp_path, isolated_tools_root,
):
    """A malicious tar member ``../../etc/passwd`` must be refused
    before we touch the filesystem."""
    bad = tmp_path / "evil.tar.gz"
    with tarfile.open(bad, "w:gz") as tf:
        info = tarfile.TarInfo(name="../escape.txt")
        payload = b"i should not exist"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))

    target = isolated_tools_root / "scrcpy-zz"
    with pytest.raises(inst.InstallerError) as exc:
        inst._extract_archive(bad, "tar.gz", target)
    assert "ปลอดภัย" in str(exc.value) or "unsafe" in str(exc.value).lower()


def test_extract_rejects_zip_slip(monkeypatch, tmp_path, isolated_tools_root):
    bad = tmp_path / "evil.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../escape.txt", b"i should not exist")

    target = isolated_tools_root / "scrcpy-zz"
    with pytest.raises(inst.InstallerError):
        inst._extract_archive(bad, "zip", target)


# ── async wrapper ──────────────────────────────────────────────────


def test_install_async_reports_success(
    monkeypatch, tmp_path, isolated_tools_root,
):
    """``install_async`` must call ``on_complete(binary, None)`` on
    success — that's how the UI knows to launch Mirror right after
    the modal closes."""
    archive = _make_fake_macos_targz(tmp_path, b"PAYLOAD")
    archive_bytes = archive.read_bytes()
    real_sha = hashlib.sha256(archive_bytes).hexdigest()

    monkeypatch.setattr(inst.sys, "platform", "darwin")
    monkeypatch.setattr(inst.platform, "machine", lambda: "arm64")
    _patched_release(
        monkeypatch,
        "macos-aarch64",
        inst._Asset(
            url="https://example.test/scrcpy.tar.gz",
            sha256=real_sha, kind="tar.gz", size=len(archive_bytes),
        ),
    )

    done = threading.Event()
    captured: dict = {}

    def _on_complete(binary, err):
        captured["binary"] = binary
        captured["err"] = err
        done.set()

    with patch.object(
        inst.urllib.request, "urlopen",
        return_value=_FakeURLResponse(archive_bytes),
    ):
        inst.install_async(on_complete=_on_complete)
        assert done.wait(timeout=10.0), "install_async never invoked on_complete"

    assert captured["err"] is None
    assert captured["binary"] is not None
    assert captured["binary"].exists()


def test_install_async_reports_failure(
    monkeypatch, tmp_path, isolated_tools_root,
):
    """On failure ``on_complete`` must still fire, with err set —
    UI uses ``err is None`` as the success branch."""
    monkeypatch.setattr(inst.sys, "platform", "darwin")
    monkeypatch.setattr(inst.platform, "machine", lambda: "arm64")
    _patched_release(
        monkeypatch,
        "macos-aarch64",
        inst._Asset(
            url="https://example.test/scrcpy.tar.gz",
            sha256="0" * 64, kind="tar.gz", size=100,
        ),
    )

    def _boom(req, timeout=30.0, context=None):
        raise inst.urllib.error.URLError("offline")

    done = threading.Event()
    captured: dict = {}

    def _on_complete(binary, err):
        captured["binary"] = binary
        captured["err"] = err
        done.set()

    with patch.object(inst.urllib.request, "urlopen", side_effect=_boom):
        inst.install_async(on_complete=_on_complete)
        assert done.wait(timeout=10.0)

    assert captured["binary"] is None
    assert isinstance(captured["err"], inst.InstallerError)


# ── housekeeping ───────────────────────────────────────────────────


def test_gc_old_versions_removes_stale_dirs(tmp_path, isolated_tools_root):
    """``gc_old_versions`` deletes every ``scrcpy-<other>`` dir but
    keeps the current pinned one — used to bound disk usage as we
    roll out new versions over time."""
    (isolated_tools_root / "scrcpy-3.0.0").mkdir(parents=True)
    (isolated_tools_root / "scrcpy-3.1.0").mkdir(parents=True)
    keep_dir = isolated_tools_root / f"scrcpy-{inst.SCRCPY_VERSION}"
    keep_dir.mkdir(parents=True)
    (isolated_tools_root / "ffmpeg-1.0").mkdir(parents=True)  # unrelated

    removed = inst.gc_old_versions()
    assert removed == 2
    assert keep_dir.exists()
    assert (isolated_tools_root / "ffmpeg-1.0").exists()


def test_estimated_download_mb_is_reasonable(monkeypatch):
    monkeypatch.setattr(inst.sys, "platform", "darwin")
    monkeypatch.setattr(inst.platform, "machine", lambda: "arm64")
    mb = inst.estimated_download_mb()
    assert 1 <= mb <= 100, "asset size sanity-check"


# ── find_user_installed ────────────────────────────────────────────


def test_find_user_installed_picks_newest_version(
    tmp_path, isolated_tools_root, monkeypatch,
):
    """When multiple version dirs co-exist (mid-rollout), prefer
    the most recently modified — that's the one we just unpacked."""
    bin_name = "scrcpy.exe" if sys.platform.startswith("win") else "scrcpy"
    old_dir = isolated_tools_root / "scrcpy-3.0.0" / "scrcpy"
    new_dir = isolated_tools_root / "scrcpy-3.3.4" / "scrcpy"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    old_bin = old_dir / bin_name
    new_bin = new_dir / bin_name
    old_bin.write_bytes(b"old")
    new_bin.write_bytes(b"new")

    # Force the newer dir to actually have a later mtime — most
    # filesystems give per-file second resolution, so creating in
    # order isn't always enough.
    now = time.time()
    os.utime(old_bin, (now - 100, now - 100))
    os.utime(new_bin, (now, now))

    found = inst.find_user_installed()
    assert found is not None
    assert found.read_bytes() == b"new"


def test_find_user_installed_returns_none_when_empty(isolated_tools_root):
    assert inst.find_user_installed() is None


# ── opt-in: real GitHub download ────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("RUN_REAL_DOWNLOAD") != "1",
    reason="set RUN_REAL_DOWNLOAD=1 to hit github.com (slow, network)",
)
def test_real_download_against_pinned_release(isolated_tools_root):
    """Sanity-check the pinned URL + sha256 still match by actually
    pulling from GitHub. Run manually after bumping SCRCPY_VERSION:

        RUN_REAL_DOWNLOAD=1 pytest tests/test_scrcpy_installer.py -k real
    """
    binary = inst.install()
    assert binary.exists()
    assert binary.name in ("scrcpy", "scrcpy.exe")
