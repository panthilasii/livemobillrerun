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
    # Set after a failed :meth:`restart_server` call. The dashboard
    # / wizard error dialogs read this so the customer sees the
    # *specific* process holding port 5037 instead of the generic
    # "close scrcpy / Android Studio / Vysor" advice — many
    # customers don't have any of those installed, they have
    # Bluestacks / MEmu / Microsoft Phone Link / Mi PC Suite /
    # Samsung Smart Switch holding the port.
    #
    # Empty string means "the last restart succeeded, or hasn't
    # been called yet".
    last_restart_error: str = ""

    def __init__(self, adb_path: str = "adb") -> None:
        self.adb_path = self._resolve(adb_path)
        self.last_restart_error = ""

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

        On failure, ``self.last_restart_error`` is populated with a
        Thai-language diagnostic that names the *specific* process
        holding port 5037 when we can identify it. The UI reads
        that string for the customer dialog. The original generic
        ``False`` return is preserved so the v1.8.0 callers don't
        break.
        """
        self.last_restart_error = ""
        try:
            kr = self._run("kill-server", timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("adb kill-server timed out")
            self.last_restart_error = (
                "adb kill-server ค้างนานเกิน 5 วินาที — มักเกิดจาก "
                "Windows Defender กำลังสแกน adb.exe หรือ daemon "
                "เก่าค้างเป็น zombie. ลองปิด-เปิดเครื่องแล้วลองใหม่"
            )
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
            self.last_restart_error = (
                "adb start-server ค้างนานเกิน 10 วินาที — มักเกิดจาก "
                "Windows Defender / Antivirus กำลัง scan adb.exe "
                "ครั้งแรก. ลองรอ 30 วินาทีแล้วกดใหม่"
            )
            return False
        if sr.returncode != 0:
            stderr = (sr.stderr or "").strip()
            log.error("adb start-server failed: rc=%s err=%r",
                      sr.returncode, stderr)
            self.last_restart_error = self._build_restart_failure_hint(stderr)
            return False
        log.info("adb daemon restarted")
        return True

    def _build_restart_failure_hint(self, adb_stderr: str) -> str:
        """Compose the Thai-language hint for a failed
        ``adb start-server``. Tries to name the process holding
        port 5037 so the customer knows exactly what to close.

        We do this on a best-effort basis — if our cross-platform
        probe fails for any reason we fall back to the original
        generic advice rather than blocking the dialog.
        """
        holder = self._find_port_5037_holder()
        lines = ["adb start-server ล้มเหลว."]
        if adb_stderr:
            # Strip ANSI / progress noise and keep the last
            # meaningful line so the dialog is readable.
            tail = [ln for ln in adb_stderr.splitlines() if ln.strip()]
            if tail:
                lines.append(f"adb แจ้ง: {tail[-1]}")
        if holder is not None:
            pid, name = holder
            lines.append("")
            lines.append(
                f"พบโปรเซส {name!r} (PID {pid}) กำลังครอง port 5037 อยู่."
            )
            lines.append(
                "วิธีแก้: เปิด Task Manager → คลิกขวาที่โปรเซสนี้ → "
                "End task → กลับมากด 'รีสตาร์ท ADB' อีกครั้ง"
            )
        else:
            # We couldn't identify the holder — fall back to the
            # v1.8.0 generic list. Keep it short; the dialog has
            # limited vertical real estate.
            lines.append("")
            lines.append(
                "ลองปิดโปรแกรมที่ใช้ ADB อยู่ — scrcpy / Android Studio "
                "/ Vysor / Bluestacks / MEmu / Microsoft Phone Link / "
                "Mi PC Suite / Samsung Smart Switch — แล้วลองใหม่"
            )
        return "\n".join(lines)

    @staticmethod
    def _find_port_5037_holder() -> tuple[int, str] | None:
        """Return ``(pid, process_name)`` for whoever owns port
        5037 on the local host, or ``None`` if we can't tell.

        Cross-platform via the bundled OS tools (no extra deps):

        * **Windows**  — ``netstat -ano -p tcp`` for the PID,
          ``tasklist /FI`` for the name. Both ship with every
          Windows install since XP.
        * **macOS / Linux** — ``lsof -nP -iTCP:5037 -sTCP:LISTEN``.
          Pre-installed on macOS; usually installed on Linux but
          we fail gracefully if it isn't.

        Returns ``None`` on any error so callers can fall through
        to the generic "close other ADB tools" advice. This is a
        diagnostic helper, never load-bearing.
        """
        try:
            if sys.platform.startswith("win"):
                return AdbController._find_port_holder_windows()
            return AdbController._find_port_holder_unix()
        except Exception:  # noqa: BLE001
            log.debug("port-5037 holder probe failed", exc_info=True)
            return None

    @staticmethod
    def _find_port_holder_windows() -> tuple[int, str] | None:
        """Windows implementation of :meth:`_find_port_5037_holder`.

        ``netstat -ano -p tcp`` output looks like::

            Proto  Local Address    Foreign Address  State       PID
            TCP    127.0.0.1:5037   0.0.0.0:0        LISTENING   12345

        We pick the LISTENING row on 5037 (any local interface) and
        then look up the executable name via ``tasklist``.
        """
        r = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if r.returncode != 0:
            return None
        pid: int | None = None
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            # parts: [Proto, Local, Foreign, State, PID]
            if "LISTENING" not in parts:
                continue
            local = parts[1]
            if not (local.endswith(":5037") or local == "*:5037"):
                continue
            try:
                pid = int(parts[-1])
                break
            except ValueError:
                continue
        if pid is None:
            return None
        # Look up the process name. ``tasklist /FI "PID eq N" /FO CSV``
        # is the most parse-friendly format on every Windows version.
        try:
            t = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except Exception:  # noqa: BLE001
            return (pid, "unknown")
        if t.returncode != 0 or not t.stdout:
            return (pid, "unknown")
        # CSV row: "image_name","pid","session_name","session#","mem"
        first = (t.stdout or "").splitlines()[0]
        name = first.split(",", 1)[0].strip().strip('"')
        return (pid, name or "unknown")

    @staticmethod
    def _find_port_holder_unix() -> tuple[int, str] | None:
        """macOS / Linux implementation of :meth:`_find_port_5037_holder`.

        ``lsof -nP -iTCP:5037 -sTCP:LISTEN -F pcn`` emits a
        line-oriented format that's stable across versions::

            p12345
            cadb
            n127.0.0.1:5037
        """
        if shutil.which("lsof") is None:
            return None
        r = subprocess.run(
            ["lsof", "-nP", "-iTCP:5037", "-sTCP:LISTEN", "-F", "pcn"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        pid: int | None = None
        name = "unknown"
        for line in r.stdout.splitlines():
            if line.startswith("p"):
                try:
                    pid = int(line[1:])
                except ValueError:
                    pid = None
            elif line.startswith("c") and pid is not None:
                name = line[1:] or "unknown"
                break
        if pid is None:
            return None
        return (pid, name)

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
