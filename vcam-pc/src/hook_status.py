"""NP Create -- runtime probe of the TikTok hook on a phone.

Why this module exists
----------------------

After "Patch สำเร็จ" the customer has no UX signal that the hook
actually loaded -- they go straight to TikTok, swipe to Live, and
stare at a black preview when something is broken without knowing
WHY. The Dashboard now shows a small badge:

    🟢 vcam ทำงานอยู่     (installed + patched + running)
    🟡 รอเปิด TikTok      (installed + patched + not running)
    🟡 ยังไม่ Patch       (installed but unpatched)
    ⚪ TikTok ไม่ติดตั้ง   (no variant found)
    🔴 ตรวจสอบไม่ได้      (adb error / device offline)

Detection strategy
------------------

For **each TikTok variant** we know about (regular, Lite,
Business, Douyin, etc.), we ask:

1. ``pm list packages``  — is it installed?
2. ``dumpsys package PKG | grep signatures``  — is the signing
   cert the LSPatch debug-keystore one (fingerprint prefix
   ``e0b8d3e5``)? That prefix is constant across every patch
   we've ever shipped because LSPatch ships a hard-coded
   keystore.
3. ``pidof PKG`` (or ``ps -A`` fallback)  — is the process up?
4. ``dumpsys package PKG | grep versionName``  — for diagnostics.

We pick the **first installed variant that's also patched**, and
fall through to the first installed variant if none are patched.
This prefers showing the customer "🟡 ยังไม่ Patch" over "no
TikTok" when they have an unpatched copy lying around.

Failure modes
-------------

Every command is wrapped with a short timeout. If adb itself
isn't reachable -- device offline, USB unplugged, no devices
authorised -- we return ``HookStatus(error="...")`` with all
booleans False. The UI renders that as the gray "ตรวจสอบไม่ได้"
state and the customer can ignore it.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# TikTok package candidates ordered by popularity in our customer
# base. We stop checking after the first installed variant unless
# we want to surface "you have multiple TikToks installed" -- a
# rare case the customer wouldn't act on, so we don't bother.
TIKTOK_PACKAGES: tuple[str, ...] = (
    "com.ss.android.ugc.trill",          # TikTok Global / Thai
    "com.zhiliaoapp.musically",          # TikTok Global / US
    "com.zhiliaoapp.musically.go",       # TikTok Lite
    "com.ss.android.ugc.aweme",          # Douyin (China)
    "com.ss.android.ugc.aweme.lite",     # Douyin Lite
)


# LSPatch's bundled debug keystore yields a deterministic signature
# fingerprint per shipped LSPatch build. The prefix below is the
# one produced by the LSPatch JAR NP Create has shipped since
# launch — every APK we patch ourselves matches it.
#
# This is intentionally a *list* (singleton today) so future LSPatch
# JAR upgrades that rotate the keystore can drop in additional
# prefixes without code changes on the customer side. Adding bogus
# entries here would risk false positives on un-patched APKs that
# coincidentally share a hex prefix, so we only add prefixes that
# have been verified against an actual built APK.
#
# The single most reliable signal, however, is to compare the live
# signature against the one we *recorded* the moment the APK was
# installed (see ``patched_signature`` on ``DeviceEntry``). The
# prefix list below is the fallback for devices where we never
# captured the install-time fingerprint (legacy entries, manual
# patches done outside NP Create, drift recovery from another PC).
_KNOWN_LSPATCH_FINGERPRINT_PREFIXES: tuple[str, ...] = (
    "e0b8d3e5",   # NP Create LSPatch keystore — verified at v1.7.4
)
# Kept as a single-string back-compat alias because external code
# (and old tests) reference it directly.
_LSPATCH_FINGERPRINT_PREFIX = _KNOWN_LSPATCH_FINGERPRINT_PREFIXES[0]


# LSPatch injects a known wrapper class as the APK's <application>.
# Newer LSPatch fork variants name it slightly differently but they
# all live under ``org.lsposed.lspatch.loader.*``. If we see this
# className in ``dumpsys package`` output we treat the APK as
# patched even when signature parsing fails — there is no benign
# reason for vanilla TikTok to load an LSPatch loader class.
_LSPATCH_CLASS_PREFIXES: tuple[str, ...] = (
    "org.lsposed.lspatch.",
    "com.wind.meditor.",     # LSPatch repackager artefact
)


@dataclass(frozen=True)
class HookStatus:
    """Snapshot of the hook's state on one device. Immutable so
    the UI can pass it through ``after(0, ...)`` without worrying
    about background mutations."""

    installed: bool = False
    package: Optional[str] = None
    version_name: str = ""
    patched: bool = False
    running: bool = False
    fingerprint: str = ""
    error: str = ""

    # ── derived UI helpers ──────────────────────────────────────

    @property
    def color(self) -> str:
        """Hex color for the dashboard badge stripe."""
        if self.error:
            return "#FF5C5C"   # THEME.danger
        if not self.installed:
            return "#7A7A88"   # THEME.fg_muted (neutral gray)
        if self.patched and self.running:
            return "#A6FF4D"   # THEME.success
        if self.patched and not self.running:
            return "#FFB84D"   # THEME.warning
        # installed but not patched
        return "#FFB84D"

    @property
    def label_th(self) -> str:
        """Short Thai label for the badge."""
        if self.error:
            return f"🔴  ตรวจสอบไม่ได้: {self.error}"
        if not self.installed:
            return "⚪  ยังไม่ติดตั้ง TikTok"
        if not self.patched:
            return f"🟡  ยังไม่ Patch  ({self.package})"
        if not self.running:
            return f"🟡  Patch แล้ว — รอเปิด TikTok  (v{self.version_name})"
        return f"🟢  vcam ทำงานอยู่  ({self.package} v{self.version_name})"

    @property
    def is_ready(self) -> bool:
        """``True`` iff the customer can start a TikTok Live
        right now -- patched + running. Used by the Dashboard's
        '"Open TikTok" button to gate its hint text."""
        return self.installed and self.patched and self.running


# ── public probe ───────────────────────────────────────────────


def probe(
    adb_path: str,
    serial: Optional[str] = None,
    *,
    timeout: float = 6.0,
    expected_fingerprint: str = "",
    expected_package: str = "",
) -> HookStatus:
    """Run all detection steps against ``serial`` (or the only
    attached device when ``serial`` is None) and return a
    ``HookStatus``.

    Parameters
    ----------
    adb_path
        Path to the adb binary.
    serial
        Adb serial / wifi address, or None for "the only device".
    timeout
        Per-shell-command timeout in seconds. Total wall time is
        bounded by ~6 × this value even on the slowest path.
    expected_fingerprint
        The signature fingerprint we recorded the last time we
        successfully Patched this device (lowercase hex). When
        provided we use *exact match* as the primary patched
        signal, which is the only fully-reliable check across
        Android versions / OEM ROMs / LSPatch keystore rotations.
        Pass an empty string to fall back to prefix matching.
    expected_package
        The TikTok package this device was last seen running. If
        installed, we prefer this variant over the default
        ordering — important on phones that have BOTH regular
        TikTok and TikTok Lite installed (we'd otherwise probe
        the wrong one and report it as unpatched).
    """
    base = [str(adb_path)]
    if serial:
        base += ["-s", str(serial)]

    # 1) Which package(s) are installed?
    rc, listed = _adb_shell(base, "pm list packages", timeout=timeout)
    if rc is None:
        return HookStatus(error=listed or "adb timeout")

    installed_pkgs = _parse_pm_list(listed)

    # Prefer the customer's known variant if it's still installed.
    # Falls through to popularity order otherwise.
    chosen: Optional[str] = None
    if expected_package and expected_package in installed_pkgs:
        chosen = expected_package
    if chosen is None:
        for pkg in TIKTOK_PACKAGES:
            if pkg in installed_pkgs:
                chosen = pkg
                break

    if chosen is None:
        return HookStatus(installed=False)

    # 2) Signing fingerprint — wider grep + multi-pattern parser.
    # The legacy single-line ``grep -m1 signatures`` only worked
    # against Android 9-10's terse output. Android 11+ moved the
    # interesting bytes onto follow-up lines (``signingInfo:`` /
    # ``signers: [...]``) which the old regex completely missed
    # — that's the bug that made already-patched phones report as
    # "ยังไม่ Patch" on Redmi Note 12 etc.
    rc, sig_out = _adb_shell(
        base,
        # ``grep -A2`` keeps the next two lines so PackageSignatures
        # blocks render in full; ``-iE`` matches the various spellings
        # ("Signatures", "signingInfo", "signers", "cert digests").
        f"dumpsys package {chosen} | "
        "grep -iE -A2 'signatures|signingInfo|signers|cert digests'",
        timeout=timeout,
    )
    sig_blob = sig_out if (rc == 0 and sig_out) else ""
    fingerprint = _extract_fingerprint(sig_blob)

    # 3) Version name (display-only; safe to omit on parse failure).
    rc, ver_out = _adb_shell(
        base, f"dumpsys package {chosen} | grep -m1 versionName",
        timeout=timeout,
    )
    version_name = ""
    if rc == 0 and ver_out:
        m = re.search(r"versionName=([^\s]+)", ver_out)
        if m:
            version_name = m.group(1)

    # 4) Process running? ``pidof`` exists on Android 8+ which
    # covers our entire supported range. Fall back to ``ps -A``
    # on the (rare) phone where pidof is missing or the SELinux
    # policy blocks it.
    running = _is_running(base, chosen, timeout=timeout)

    # 5) Patched? — three independent signals, ANY of them is
    # enough to flip the bit:
    #
    #   a) Exact fingerprint match against what we recorded at
    #      install time (most reliable; no false positives because
    #      vanilla TikTok cannot share LSPatch's keystore).
    #   b) Fingerprint prefix in our known-good list (covers
    #      legacy entries with no recorded baseline).
    #   c) ``className`` in the manifest is the LSPatch loader
    #      class (covers cases where signature parsing fails on
    #      exotic ROMs but the manifest is still readable).
    #
    # Using OR semantics is intentional: each signal has a
    # different failure mode (regex parsing, OEM ROM quirks,
    # debuggable=false bypass), and they don't share a common
    # failure cause. False positives are essentially impossible
    # because (b) requires a long, specific hex prefix and (c)
    # requires a specific Java package path that vanilla TikTok
    # never references.
    patched = False
    if fingerprint:
        if expected_fingerprint and fingerprint == expected_fingerprint.lower():
            patched = True
        else:
            for prefix in _KNOWN_LSPATCH_FINGERPRINT_PREFIXES:
                if fingerprint.startswith(prefix):
                    patched = True
                    break

    # Only trigger the className backup probe when fingerprint
    # extraction returned NOTHING. If we successfully parsed a
    # fingerprint and it just didn't match a known LSPatch prefix,
    # that's a confident "unpatched" answer — running an extra
    # adb roundtrip per probe would waste 200-400ms × N devices ×
    # 8s tick, which adds up fast on a 5-phone install.
    if not patched and not fingerprint:
        rc2, app_out = _adb_shell(
            base,
            f"dumpsys package {chosen} | "
            "grep -iE 'className=|application='",
            timeout=timeout,
        )
        if rc2 == 0 and app_out:
            lower = app_out.lower()
            for cls_prefix in _LSPATCH_CLASS_PREFIXES:
                if cls_prefix in lower:
                    patched = True
                    break

    return HookStatus(
        installed=True,
        package=chosen,
        version_name=version_name,
        patched=patched,
        running=running,
        fingerprint=fingerprint,
    )


def _extract_fingerprint(sig_out: str) -> str:
    """Pull the package-signature fingerprint out of ``dumpsys
    package`` output. Tries the formats we've seen in the wild,
    in rough order of specificity, and returns the longest hex
    string we can confidently identify. Empty string when the
    output yields nothing parseable.

    Why a list of patterns instead of one mega-regex
    ------------------------------------------------
    Different Android versions / OEM ROMs print the signature
    block differently. Examples we've collected from support
    tickets:

    * Android 9-10 stock::

          signatures=PackageSignatures{abcd1234 [4567ef...]}

    * Android 11 (AOSP)::

          signingInfo:
              PackageSignatures{abcd1234 [4567ef...]}

    * Android 12+ / API 31+ / MIUI 14::

          signingInfo:
              PackageSignatures{
                  schemeVersion: 3
                  signers: [4567ef89...]
              }

    * Android 13 / HyperOS / OPPO ColorOS variants sometimes
      print the legacy ``signatures:[hex]`` form on top of the
      newer block, sometimes only the new one.

    The single-pattern regex we used in 1.7.4 caught only the
    very first format above and silently mis-detected the rest
    as "unpatched" — a false-negative customers see as their
    phone refusing to come up green even after a successful
    Patch. Trying patterns in order, then falling through to a
    "longest hex" heuristic, lets us cover all known shapes
    without piling brittle character-class soup into one regex.
    """
    candidates: list[str] = []

    # Pattern A: legacy "signatures:[hex]"
    for m in re.finditer(r"signatures:\[([0-9a-fA-F]+)\]", sig_out):
        candidates.append(m.group(1))

    # Pattern B: PackageSignatures{...[hex]} — Android 9-11
    for m in re.finditer(
        r"PackageSignatures\{[^\[\}]*\[([0-9a-fA-F]+)\]", sig_out,
    ):
        candidates.append(m.group(1))

    # Pattern C: PackageSignature{hex ...} — Android 11+
    for m in re.finditer(r"PackageSignature\{([0-9a-fA-F]+)", sig_out):
        candidates.append(m.group(1))

    # Pattern D: signers: [hex, ...] — Android 12+ / scheme v3
    for m in re.finditer(
        r"signers\s*:\s*\[\s*([0-9a-fA-F]+)", sig_out,
    ):
        candidates.append(m.group(1))

    # Pattern E: cert digests:\n   [hex] — some Samsung ROMs
    for m in re.finditer(
        r"cert\s+digests?\s*:?\s*\[?\s*([0-9a-fA-F]+)", sig_out,
        re.IGNORECASE,
    ):
        candidates.append(m.group(1))

    # Heuristic fallback: the longest hex token of length ≥ 8 in
    # the blob is almost always the signature fingerprint (other
    # hex in dumpsys output — version codes, flags — is shorter).
    if not candidates:
        for m in re.finditer(r"\b([0-9a-fA-F]{8,})\b", sig_out):
            candidates.append(m.group(1))

    if not candidates:
        return ""
    # Prefer the longest match. Ties broken by first occurrence.
    return max(candidates, key=len).lower()


# ── plumbing ───────────────────────────────────────────────────


def _adb_shell(
    base_cmd: list[str], shell_cmd: str, *, timeout: float,
) -> tuple[Optional[int], str]:
    """Run ``adb [-s SERIAL] shell <shell_cmd>``. Returns
    ``(return_code, combined_output)``. ``return_code`` is ``None``
    on timeout / spawn failure -- callers treat that as "skip"
    rather than "no"."""
    try:
        r = subprocess.run(
            base_cmd + ["shell", shell_cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"timeout running: {shell_cmd}"
    except (OSError, ValueError) as exc:
        return None, f"adb spawn failed: {exc}"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _parse_pm_list(text: str) -> set[str]:
    """``pm list packages`` produces lines like ``package:com.x``.
    Strip the prefix and return a set."""
    out: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            out.add(line[len("package:"):].strip())
    return out


def _is_running(
    base_cmd: list[str], package: str, *, timeout: float,
) -> bool:
    """Two-tier check: ``pidof`` first (fast + cheap), ``ps -A``
    grep fallback. Either signal is enough -- we don't need the
    pid value."""
    rc, out = _adb_shell(base_cmd, f"pidof {package}", timeout=timeout)
    if rc == 0 and out.strip().isdigit():
        return True
    # pidof prints nothing AND returns 1 when missing; if rc was
    # 127 (command not found) we should NOT trust the negative.
    if rc not in (0, 1):
        # Fall back to ps. ``-A`` matches on Android (toybox ps).
        rc2, out2 = _adb_shell(
            base_cmd, f"ps -A 2>/dev/null | grep -F {package}",
            timeout=timeout,
        )
        if rc2 == 0 and package in (out2 or ""):
            return True
    return False


__all__ = [
    "HookStatus",
    "TIKTOK_PACKAGES",
    "probe",
    "_extract_fingerprint",
    "_KNOWN_LSPATCH_FINGERPRINT_PREFIXES",
]
