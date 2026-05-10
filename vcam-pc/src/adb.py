"""Thin wrapper around the `adb` command.

Only the bits we need:
- list connected devices
- run a single shell command
- set up `adb reverse tcp:<port> tcp:<port>`
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class AdbDevice:
    serial: str
    state: str  # "device", "unauthorized", "offline", ...
    model: str = ""
    product: str = ""

    @property
    def online(self) -> bool:
        return self.state == "device"


class AdbController:
    def __init__(self, adb_path: str = "adb") -> None:
        self.adb_path = self._resolve(adb_path)

    @staticmethod
    def _resolve(adb_path: str) -> str:
        """Resolve ``adb_path`` to an actual binary.

        Resolution order
        ----------------

        1. ``adb_path`` *if* it's an absolute path that points at a
           real file on disk — power users can override via
           ``config.json`` to use a custom adb (e.g. a newer Google
           Android platform-tools install).

        2. **Frozen-mode bundled adb** — when the app is running as
           the Inno Setup / .dmg PyInstaller bundle, the customer
           almost certainly does *not* have a working adb on their
           system PATH. Even if some other Android tool happened to
           drop an adb on PATH (e.g. a stale Genymotion install),
           we *deliberately* prefer the bundled one — its version
           matches the lspatch / scrcpy combo we ship and is
           known-good. v1.7.8 lacked this preference, which let a
           broken system adb shadow the bundled one and silently
           prevented the "Allow USB Debugging" popup from firing.

        3. ``shutil.which(adb_path)`` — non-frozen + non-default
           setups (a developer with adb on PATH).

        4. ``platform_tools.find_adb()`` — last-ditch fallback,
           also covers the case where ``adb_path`` was the bare
           string ``"adb"`` (the shipping default).
        """
        if adb_path and Path(adb_path).is_absolute() and Path(adb_path).is_file():
            return adb_path

        if getattr(sys, "frozen", False):
            try:
                from . import platform_tools

                bundled = platform_tools.find_adb()
                if bundled is not None:
                    return str(bundled)
            except Exception:
                # Fall through to the original lookup chain so a
                # bug in platform_tools never strands the customer
                # with no adb at all.
                pass

        if adb_path and shutil.which(adb_path) is not None:
            return adb_path
        try:
            from . import platform_tools

            bundled = platform_tools.find_adb()
            if bundled is not None:
                return str(bundled)
        except Exception:
            pass
        return adb_path

    # ── plumbing ────────────────────────────────────────────────

    def _run(self, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
        cmd = [self.adb_path, *args]
        log.debug("adb: %s", " ".join(cmd))
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def is_available(self) -> bool:
        if shutil.which(self.adb_path) is None:
            return False
        try:
            r = self._run("version", timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    def restart_server(self) -> bool:
        """``adb kill-server`` then ``adb start-server``.

        Why we need this
        ----------------
        On Windows the most common ADB sticking-point isn't a missing
        driver — it's a stale daemon. Customers who:

        * Used scrcpy, Vysor, or Android Studio earlier — those ship
          their own adb (often a different version), and whichever
          one wins the port-5037 race is what subsequent ``adb
          devices`` calls talk to. If the surviving daemon is from
          v40+ but our bundled adb is v34, you can get silent
          protocol mismatches where the device list never refreshes
          even after the customer taps "Allow USB Debugging".
        * Have rebooted Windows mid-session — adb's USB descriptors
          go stale and the customer ends up with a permanently
          ``unauthorized`` row that won't transition to ``device``
          no matter how many times they tap Allow.

        ``adb kill-server`` tears down whatever's running on 5037,
        ``adb start-server`` spawns a fresh daemon under our bundled
        adb's identity (so the RSA key matches what's saved on the
        phone too). The combination clears 95 % of "ADB sees my
        phone but it's stuck on Allow / unauthorized" reports.

        Returns ``True`` if both commands completed without an error
        return code; ``False`` (and logs the stderr) otherwise. The
        caller is expected to re-poll ``adb devices`` ~1 s after a
        successful restart so the UI updates with fresh state.
        """
        try:
            kr = self._run("kill-server", timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("adb kill-server timed out")
            return False
        # kill-server exits non-zero ("error: cannot connect to
        # daemon") if the daemon was already dead — that's fine,
        # the goal is "no daemon running" and we got there.
        if kr.returncode != 0:
            log.debug("adb kill-server rc=%s err=%r",
                      kr.returncode, (kr.stderr or "").strip())
        try:
            sr = self._run("start-server", timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("adb start-server timed out")
            return False
        if sr.returncode != 0:
            log.error("adb start-server failed: rc=%s err=%r",
                      sr.returncode, (sr.stderr or "").strip())
            return False
        log.info("adb daemon restarted")
        return True

    # ── device enumeration ─────────────────────────────────────

    def devices(self) -> list[AdbDevice]:
        r = self._run("devices", "-l")
        if r.returncode != 0:
            log.error("adb devices failed: %s", r.stderr.strip())
            return []
        out: list[AdbDevice] = []
        for line in r.stdout.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            serial, state = parts[0], parts[1]
            kv = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
            out.append(
                AdbDevice(
                    serial=serial,
                    state=state,
                    model=kv.get("model", ""),
                    product=kv.get("product", ""),
                )
            )
        return out

    def shell(
        self,
        command: str,
        serial: str | None = None,
        timeout: float = 10.0,
    ) -> str:
        args = []
        if serial:
            args += ["-s", serial]
        args += ["shell", command]
        try:
            r = self._run(*args, timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("adb shell '%s' timed out after %ss", command, timeout)
            return ""
        if r.returncode != 0:
            log.warning("adb shell '%s' rc=%s err=%s", command, r.returncode, r.stderr.strip())
        return r.stdout.strip()

    def get_props(self, serial: str | None = None) -> dict[str, str]:
        """Return a subset of ro.* properties we care about."""
        keys = [
            "ro.soc.model",
            "ro.board.platform",
            "ro.product.device",
            "ro.product.model",
            "ro.product.cpu.abi",
            "ro.build.version.release",
            "ro.build.version.sdk",
            "ro.miui.ui.version.name",
            "ro.mi.os.version.name",
            "ro.boot.flash.locked",
            "ro.boot.verifiedbootstate",
        ]
        out: dict[str, str] = {}
        for k in keys:
            out[k] = self.shell(f"getprop {k}", serial=serial) or ""
        return out

    # ── reverse port forwarding ────────────────────────────────

    def reverse(self, port: int, serial: str | None = None) -> bool:
        args = []
        if serial:
            args += ["-s", serial]
        args += ["reverse", f"tcp:{port}", f"tcp:{port}"]
        r = self._run(*args, timeout=5)
        if r.returncode != 0:
            log.error("adb reverse failed: %s", r.stderr.strip())
            return False
        return True

    def reverse_remove(self, port: int, serial: str | None = None) -> None:
        args = []
        if serial:
            args += ["-s", serial]
        args += ["reverse", "--remove", f"tcp:{port}"]
        self._run(*args, timeout=5)

    def reverse_list(self) -> list[str]:
        r = self._run("reverse", "--list", timeout=5)
        return [line for line in r.stdout.splitlines() if line.strip()]
