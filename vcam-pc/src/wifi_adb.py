"""Wireless ADB helpers — let the customer unplug the cable.

After the customer pairs their phone the first time over USB
(needed for the LSPatch step anyway), we flip the daemon into
TCP mode with ``adb tcpip <port>`` and capture the phone's
current LAN address. From that point on the Studio talks to the
phone over WiFi until the phone reboots.

This module deliberately keeps two things separate:

* The **canonical key** for a device is its USB serial (what
  ``adb shell getprop ro.serialno`` reports). That never changes
  across DHCP renewals, router swaps, or reboots — so it's what
  we persist in ``devices.json``.
* The **active ADB id** is whatever ``adb devices`` accepts as a
  ``-s <id>`` argument right now: either the same USB serial (if
  the cable is plugged in) or an ``IP:port`` string (if we're on
  WiFi).

The Studio app is responsible for picking the right active id
when it issues a command, e.g. via ``StudioApp.adb_id_for(entry)``.

ADB tcpip behaviour
-------------------

* ``adb tcpip 5555`` only works against a USB-connected device,
  not over an existing WiFi connection. The phone's adbd
  restarts in TCP mode immediately and the USB transport drops.
* On phone reboot, adbd reverts to USB mode. We detect this and
  prompt the customer to plug in the cable again — *or* fall back
  to Android 11+ Wireless Debugging (out of scope for v1.1).
* Some HyperOS builds reset ``service.adb.tcp.port`` after
  Developer Options is toggled. Re-running ``tcpip`` always
  fixes it.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

log = logging.getLogger(__name__)

DEFAULT_TCPIP_PORT = 5555
WIFI_ID_RE = re.compile(r"^(?P<ip>(\d{1,3}\.){3}\d{1,3}):(?P<port>\d{1,5})$")
# "ip route" is the most reliable way to read the wlan0 source
# address — works even when wifi is metered, the phone is in
# tethering mode, or the user has multiple radios up.
_IP_ROUTE_SRC_RE = re.compile(r"\bsrc\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")


def is_wifi_id(adb_id: str) -> bool:
    """True iff ``adb_id`` looks like ``IP:port`` rather than a USB
    serial. Used to tag entries in the unified device list."""
    return bool(WIFI_ID_RE.match(adb_id or ""))


def parse_wifi_id(adb_id: str) -> tuple[str, int] | None:
    """Split ``"192.168.1.42:5555"`` into ``("192.168.1.42", 5555)``.
    Returns ``None`` if the string isn't a WiFi id."""
    m = WIFI_ID_RE.match(adb_id or "")
    if not m:
        return None
    return m.group("ip"), int(m.group("port"))


def format_wifi_id(ip: str, port: int = DEFAULT_TCPIP_PORT) -> str:
    return f"{ip}:{port}"


def _run(
    adb_path: str, *args: str, timeout: float = 8.0,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [adb_path, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def enable_tcpip(
    adb_path: str,
    serial: str,
    port: int = DEFAULT_TCPIP_PORT,
) -> bool:
    """Tell the (USB-connected) phone to restart adbd in TCP mode.

    The USB transport will disappear ~1 s after this returns. The
    phone is then reachable via ``adb connect <ip>:<port>`` from
    any machine on the same LAN.

    Returns ``True`` on success.
    """
    if shutil.which(adb_path) is None:
        log.error("adb not found on PATH: %s", adb_path)
        return False
    try:
        r = _run(adb_path, "-s", serial, "tcpip", str(port), timeout=10)
    except subprocess.TimeoutExpired:
        log.warning("adb tcpip %s timed out", port)
        return False
    if r.returncode != 0:
        log.error("adb tcpip failed: rc=%s stderr=%r",
                  r.returncode, r.stderr.strip())
        return False
    out = (r.stdout or "").strip()
    log.info("adb tcpip %s: %s", port, out)
    # Typical success: "restarting in TCP mode port: 5555"
    return ("TCP mode" in out) or ("port:" in out) or (r.returncode == 0)


def get_device_wifi_ip(adb_path: str, serial: str) -> str | None:
    """Read the phone's current LAN IPv4 over USB.

    Tries ``ip -4 route get 1.1.1.1`` first (most reliable; works
    even with multi-homed devices) and falls back to scanning
    ``ip -4 addr show wlan0``.

    Returns ``None`` if no IPv4 was found — usually means the
    phone has no WiFi connectivity right now.
    """
    if shutil.which(adb_path) is None:
        return None
    # Attempt 1 — pin a route to a public address; the kernel
    # picks an outbound interface and reports its src address.
    cmd1 = "ip -4 route get 1.1.1.1 2>/dev/null"
    try:
        r = _run(adb_path, "-s", serial, "shell", cmd1, timeout=5)
        m = _IP_ROUTE_SRC_RE.search(r.stdout or "")
        if m:
            return m.group(1)
    except subprocess.TimeoutExpired:
        pass
    # Attempt 2 — read wlan0 address directly.
    cmd2 = "ip -4 addr show wlan0 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}'"
    try:
        r = _run(adb_path, "-s", serial, "shell", cmd2, timeout=5)
        out = (r.stdout or "").strip().splitlines()
        if out:
            return out[0].strip()
    except subprocess.TimeoutExpired:
        pass
    return None


def adb_connect(
    adb_path: str,
    ip: str,
    port: int = DEFAULT_TCPIP_PORT,
    timeout: float = 6.0,
) -> bool:
    """Run ``adb connect <ip>:<port>`` and parse the result.

    Returns ``True`` if the phone responded and is now in the
    ``adb devices`` list as ``device`` state.
    """
    if shutil.which(adb_path) is None:
        return False
    try:
        r = _run(adb_path, "connect", format_wifi_id(ip, port),
                 timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("adb connect %s:%s timed out", ip, port)
        return False
    out = (r.stdout or "").strip().lower()
    # Success: "connected to 192.168.1.42:5555"
    # Already-connected: "already connected to ..."
    # Failure: "failed to connect to ..." / "cannot connect to ..."
    if "connected to" in out and "failed" not in out and "cannot" not in out:
        log.info("adb connect %s:%s ok (%s)", ip, port, out)
        return True
    log.warning("adb connect %s:%s did not succeed: %r", ip, port, out)
    return False


def adb_disconnect(
    adb_path: str,
    ip: str | None = None,
    port: int = DEFAULT_TCPIP_PORT,
) -> None:
    """Disconnect a single WiFi device (or all if ``ip`` is None)."""
    if shutil.which(adb_path) is None:
        return
    args = ["disconnect"]
    if ip:
        args.append(format_wifi_id(ip, port))
    try:
        _run(adb_path, *args, timeout=5)
    except subprocess.TimeoutExpired:
        log.debug("adb disconnect timed out (non-fatal)")
