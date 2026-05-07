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
from dataclasses import dataclass

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
        """Resolve ``adb_path`` to an actual binary, falling back to
        the bundled ``.tools/<os>/platform-tools/adb`` if the user's
        PATH doesn't have ``adb``. Keeps the configured value if it
        already works, so power users can override via config.json."""
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
