"""LSPatch pipeline — fuse vcam-app into the user's TikTok APK.

This is the *non-root* injection path. We use the open-source LSPatch
tool (https://github.com/JingMatrix/LSPatch) to embed our Xposed module
(`vcam-app-debug.apk`) directly into TikTok's APK. The patched APK
boots a tiny Xposed framework loader on its own — no root, no Magisk,
no LSPosed required.

End-to-end flow (one button in the GUI):

```
 1. Pull the user's installed TikTok APKs from the phone (base + splits)
 2. Patch them all with LSPatch, embedding vcam-app
 3. Uninstall the original TikTok
 4. install-multiple the patched APKs
```

After the install:

```
 5. The user logs into TikTok again (signature changed → fresh sandbox).
 6. The vcam-app's CameraHook fires the moment TikTok's main process
    starts. From then on, going Live replaces the camera with whatever
    MP4 sits at /sdcard/vcam_final.mp4 (see hook_mode.py).
```

Tooling we depend on:

* `JDK 21+`        — LSPatch is built against Java 21 class files.
* `lspatch.jar`    — the LSPatch CLI (downloaded once into `.tools/`).
* `adb`            — already on PATH from earlier phases.

The user does NOT need to unlock the bootloader. They only need:
  - Developer options ON
  - USB debugging ON
  - Install via USB ON

Anti-pattern guard rails this file enforces:

* Never patch & install in one step without an explicit user
  confirmation — the install destroys the original TikTok session.
* Never assume splits aren't required. We always use install-multiple
  with all patched splits, otherwise Android will reject with
  INSTALL_FAILED_MISSING_SPLIT.
* Never re-run LSPatch on already-patched APKs (if cache is stale,
  blow it away first).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import platform_tools
from .config import PROJECT_ROOT, StreamConfig
# Re-exported so the install pipeline and the live probe never
# disagree about what counts as "TikTok". If you need to add a new
# region/Lite/beta variant, edit ``hook_status.TIKTOK_PACKAGES`` and
# both modules pick it up.
from .hook_status import TIKTOK_PACKAGES

log = logging.getLogger(__name__)

# Patterns we use to *discover* TikTok-like packages that aren't in
# the hard-coded list above. Some OEM stores, beta channels, and
# regional builds ship under names like ``com.zhiliaoapp.musically.preload``
# or ``com.tiktok.something`` — we'd rather find them than fail with
# a misleading "no TikTok variant installed" message that pushes
# the customer to uninstall+reinstall their working app.
_TIKTOK_PKG_PATTERNS = re.compile(
    r"^(?:com\.ss\.android\.ugc\.(?:trill|aweme|musically|tiktok)"
    r"|com\.zhiliaoapp\.musically"
    r"|com\.tiktok\.[\w.]+)"
    r"(?:\.[\w]+)*$"
)


# ────────────────────────────────────────────────────────────
#  result types
# ────────────────────────────────────────────────────────────

@dataclass
class ToolStatus:
    """Set by `probe_tools()` so the GUI can refuse to start gracefully."""
    java: Path | None = None
    java_version: str = ""
    lspatch: Path | None = None
    vcam_apk: Path | None = None
    adb: str = "adb"
    ok: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class PullResult:
    ok: bool
    package: str = ""
    version_name: str = ""
    apks: list[Path] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: str = ""


@dataclass
class PatchResult:
    ok: bool
    output_dir: Path
    patched_apks: list[Path] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: str = ""
    log_tail: str = ""


@dataclass
class InstallResult:
    ok: bool
    elapsed_s: float = 0.0
    error: str = ""
    fingerprint: str = ""
    # When ``install-multiple`` of the patched bundle fails, the
    # pipeline re-installs the original APKs to leave the device in
    # a working state. These flags tell the GUI which message to
    # show the customer.
    rollback_attempted: bool = False
    rollback_ok: bool = False
    rollback_error: str = ""


# ────────────────────────────────────────────────────────────
#  java probe (quarantine-tolerant)
# ────────────────────────────────────────────────────────────


# First-launch ``java -version`` on macOS triggers Gatekeeper +
# syspolicyd to walk every ``Contents/Home/lib/*.dylib`` checking
# notarization tickets. The Adoptium JDK isn't notarized as one
# bundle, so the kernel does it per-file the first time and the
# whole process can take 10-30 s on slower Macs (HDD, Intel, or
# under AV scan). After the first launch macOS caches the verdict
# and subsequent runs are sub-second. We give the first probe a
# generous timeout so we don't false-alarm the customer with a
# "java probe failed" the moment they double-click ``run.command``
# from ``~/Downloads``.
_JAVA_PROBE_TIMEOUT_S = 30.0


def _jdk_root_from_java(java: Path) -> Path | None:
    """Return the ``jdk-21/`` directory containing ``java``.

    macOS layout: ``…/jdk-21/Contents/Home/bin/java``.
    Linux/Windows layout: ``…/jdk-21/bin/java[.exe]``.
    Falls back to ``None`` when ``java`` lives outside our bundled
    layout (e.g. system ``/usr/bin/java``) so the caller knows not
    to try ``xattr`` on a path it doesn't own.
    """
    for parent in java.parents:
        if parent.name == "jdk-21":
            return parent
    return None


def _strip_quarantine_macos(target: Path) -> bool:
    """Recursively strip ``com.apple.quarantine`` from ``target``.

    No-op (returns False) on non-macOS or when ``xattr`` isn't on
    PATH. Returns True when the command exited 0 — the caller can
    use that as a "worth retrying the probe" signal.

    Why this exists
    ---------------
    Customers who download the macOS ZIP via Safari + extract in
    ``~/Downloads`` end up with the quarantine xattr on every JDK
    binary. macOS then runs syspolicyd against ``java`` and every
    bundled dylib on first launch — which we've seen take >30 s
    on Intel Macs with HDD + Bitdefender. Stripping the xattr
    short-circuits that check so the next probe returns instantly.
    """
    if platform_tools.current_os() != "macos":
        return False
    xattr = shutil.which("xattr")
    if xattr is None:
        log.debug("xattr not on PATH; skipping quarantine strip")
        return False
    try:
        r = subprocess.run(
            [xattr, "-dr", "com.apple.quarantine", str(target)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if r.returncode == 0:
            log.info("stripped com.apple.quarantine from %s", target)
            return True
        log.debug(
            "xattr exit=%s stderr=%s", r.returncode, (r.stderr or "").strip(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("xattr failed: %s", exc)
    return False


def _strip_motw_windows(target: Path) -> bool:
    """Recursively clear the Mark-of-the-Web ADS from files under ``target``.

    Windows analogue of ``_strip_quarantine_macos``. After a
    customer extracts the ZIP via File Explorer, every file in
    ``.tools\\windows\\jdk-21\\`` carries a ``Zone.Identifier``
    NTFS alternate data stream that tells SmartScreen / Smart App
    Control "this came from the internet — re-validate every
    launch". For a JDK with hundreds of bundled DLLs, that means
    every cold ``java -version`` re-runs SmartScreen against each
    DLL load, which we've measured at 5-20 s on Win11 systems
    with Defender + 3rd-party AV layered on top.

    PowerShell's ``Unblock-File`` is the supported way to strip
    the ADS — it's safer than ``Remove-Item :Zone.Identifier``
    because it's idempotent and handles read-only / system files
    gracefully. We pipe the whole ``jdk-21/`` tree through it in
    one shot rather than invoking once per file (avoids
    Powershell's 200-300 ms per-call cold-start cost stacking up).
    """
    if platform_tools.current_os() != "windows":
        return False
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if powershell is None:
        log.debug("powershell not on PATH; skipping MOTW strip")
        return False
    # ``-NoProfile`` skips loading the user's profile (saves ~1 s
    # cold start). ``-ExecutionPolicy Bypass`` is required because
    # corporate Win11 images often default to ``Restricted`` which
    # would otherwise refuse the inline command unsigned.
    cmd = [
        powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-Command",
        f"Get-ChildItem -Recurse -LiteralPath '{target}' -ErrorAction "
        "SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue",
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20, check=False,
        )
        if r.returncode == 0:
            log.info("cleared Mark-of-the-Web from %s", target)
            return True
        log.debug(
            "Unblock-File exit=%s stderr=%s",
            r.returncode, (r.stderr or "").strip(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("Unblock-File failed: %s", exc)
    return False


# Substring → human-readable name used in the cloud-sync warning.
# Order matters only for display; matching is independent so a
# folder under both OneDrive and iCloud (rare) lists both.
_CLOUD_SYNC_HINTS: tuple[tuple[str, str], ...] = (
    ("OneDrive",       "OneDrive"),
    ("iCloud",         "iCloud Drive"),
    ("CloudDocs",      "iCloud Drive"),
    ("Mobile Documents", "iCloud Drive"),
    ("Dropbox",        "Dropbox"),
    ("Google Drive",   "Google Drive"),
    ("GoogleDrive",    "Google Drive"),
    ("DriveFS",        "Google Drive"),
    ("pCloud",         "pCloud"),
    ("Box Sync",       "Box"),
    ("Box\\",          "Box"),
)


def detect_cloud_sync_folder(path: Path) -> str:
    """Return the cloud-sync provider name if ``path`` lives under one.

    Empty string when the folder is on local storage. We surface
    this *before* running ``java -version`` because cloud-sync
    placeholder files (Files-On-Demand) cause the JVM to hang for
    seconds while the OS fetches every DLL/dylib on demand —
    a confusing failure mode the customer can fix by moving the
    folder to local disk *once* (vs. re-download every launch).
    """
    s = str(path).replace("\\", "/")
    seen: set[str] = set()
    out: list[str] = []
    for needle, label in _CLOUD_SYNC_HINTS:
        if needle.replace("\\", "/") in s and label not in seen:
            seen.add(label)
            out.append(label)
    return ", ".join(out)


def _self_heal_jdk(jdk_root: Path) -> bool:
    """Best-effort fix-up for both macOS quarantine and Windows MOTW.

    Returns True if either heuristic reported success — the caller
    uses that to decide whether retrying ``java -version`` is
    worth the wait. False on Linux / when no helper tool was
    available, in which case the retry is skipped (we already
    know it'll just time out again).
    """
    os_name = platform_tools.current_os()
    if os_name == "macos":
        return _strip_quarantine_macos(jdk_root)
    if os_name == "windows":
        return _strip_motw_windows(jdk_root)
    return False


def _timeout_message(timeout: float, java: Path) -> str:
    """Long-form Thai diagnostic for the timeout case.

    Includes copy-paste commands for both macOS and Windows so
    the customer can self-heal without having to ask support
    "what do I type". The message intentionally lists the most
    common root causes in priority order — folder location first
    (>50 % of cases), then antivirus, then cloud-sync.
    """
    cloud = detect_cloud_sync_folder(java)
    cloud_line = (
        f"\n• โฟลเดอร์อยู่ใน {cloud} — ย้ายไปไว้บนดิสก์เครื่อง "
        f"(ไม่ sync cloud) แล้วลองใหม่"
        if cloud
        else ""
    )
    os_name = platform_tools.current_os()
    if os_name == "macos":
        cmd = f"xattr -dr com.apple.quarantine \"{java.parents[3]}\""
        os_hint = (
            "\n• เปิด Terminal วาง:\n"
            f"    {cmd}\n"
            "  (ถ้ากำลังอยู่ใน Downloads ให้ย้ายไป Applications/Documents ก่อน)"
        )
    elif os_name == "windows":
        target = java.parents[1] if len(java.parents) >= 2 else java.parent
        cmd = (
            f"Get-ChildItem -Recurse -LiteralPath '{target}' | Unblock-File"
        )
        os_hint = (
            "\n• เปิด PowerShell วาง:\n"
            f"    {cmd}\n"
            "  หรือเพิ่มโฟลเดอร์โปรแกรมเป็น exclusion ใน Windows Defender"
        )
    else:
        os_hint = ""
    return (
        f"java probe timed out after {timeout:.0f}s.\n\n"
        "สาเหตุที่พบบ่อย (เรียงจากมากไปน้อย):\n"
        "• โฟลเดอร์อยู่ใน Downloads → ย้ายไป Applications "
        "(macOS) หรือ C:\\NP-Create\\ (Windows)\n"
        "• Antivirus / Defender / Smart App Control สแกน java "
        "ครั้งแรก — รอสักครู่แล้วลองใหม่ หรือเพิ่ม exclusion"
        f"{cloud_line}"
        f"{os_hint}"
    )


def _path_has_non_ascii(p: Path) -> bool:
    """True when the absolute path contains any non-ASCII codepoint.

    The bundled OpenJDK launcher (``java.exe`` / ``libjli``) has a
    long history of failing to locate ``java.dll`` / ``libjli.so``
    when its own path contains non-ASCII characters — particularly
    on Windows where parts of the launcher still use the ANSI
    filesystem APIs (e.g. ``GetModuleFileNameA``) and silently
    truncate Thai / Vietnamese / Chinese codepoints. The customer-
    visible symptom is a cryptic ``Error: could not find java.dll``
    even though the file is right there on disk. We detect this
    proactively so the error message can tell the customer to
    move the folder to an ASCII path (e.g. ``C:\\NP-Create\\``).
    """
    try:
        str(p).encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def _java_dll_message(java: Path) -> str:
    """Diagnostic for ``Error: could not find java.dll``.

    This error is almost always one of:
      1. Path contains non-ASCII chars (Thai username, Chinese
         folder name, …) — the OpenJDK launcher's ANSI codepath
         can't open its own DLL via the truncated path.
      2. ``bin/`` is incomplete — AV quarantined ``java.dll`` /
         ``jli.dll`` mid-extract, or the ZIP was extracted via a
         tool that dropped files >260 chars (Windows path-length
         limit for non-Unicode-aware extractors).
      3. The customer manually moved files around and split
         ``java.exe`` from its sibling ``java.dll``.
    """
    parts = []
    if _path_has_non_ascii(java):
        parts.append(
            "• Path มีอักษรไม่ใช่ภาษาอังกฤษ (เช่น ชื่อ user เป็นไทย) — "
            "ย้ายโฟลเดอร์โปรแกรมไป path ภาษาอังกฤษล้วน เช่น "
            "C:\\NP-Create\\"
        )
    parts.extend([
        "• ไฟล์ใน jdk-21\\bin\\ ไม่ครบ — แตก ZIP ใหม่ครั้งเดียวให้สมบูรณ์ "
        "และเพิ่มโฟลเดอร์โปรแกรมเป็น exclusion ใน Defender / AV",
        "• อย่าย้ายไฟล์ใน jdk-21/ แยกออกจากกัน",
    ])
    return (
        "java.exe เรียก java.dll ไม่เจอ.\n\nสาเหตุที่พบบ่อย:\n"
        + "\n".join(parts)
    )


def _looks_like_version_line(line: str) -> bool:
    """Heuristic: does ``line`` look like a real ``java -version``
    first line?

    Real JDK output:
        openjdk version "21.0.5" 2024-10-15 LTS
        java version "21.0.5" 2024-10-15
    Failure modes that LOOK like output but aren't:
        Error: could not find java.dll
        Error: opening registry key 'Software\\JavaSoft\\...'
        Picked up _JAVA_OPTIONS: ...
    """
    low = line.strip().lower()
    if not low:
        return False
    if low.startswith("error"):
        return False
    if low.startswith("picked up "):
        # Java emits this informational line when JAVA_TOOL_OPTIONS
        # or _JAVA_OPTIONS is set BEFORE the version line. The
        # caller should look at the next line, not this one.
        return False
    return ("version" in low) and ('"' in line)


def _run_java_version(java: Path, timeout: float) -> tuple[bool, str, str]:
    """Run ``java -version`` once and return (ok, version_line, err).

    ``java -version`` prints to stderr — we read both streams just
    in case a future JDK changes that.

    Filtering noise
    ---------------
    The first line of stderr isn't always the version string:

    * Some installs print a ``Picked up _JAVA_OPTIONS: …``
      banner first — we skip past it to find the real version.
    * A broken JDK can print ``Error: could not find java.dll``
      (non-ASCII path) or ``Error: opening registry key …``
      (corrupt registry on Windows). We treat those as failures
      with a dedicated Thai diagnostic instead of mis-parsing
      them as "Java 0".
    """
    try:
        r = subprocess.run(
            [str(java), "-version"],
            capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "", _timeout_message(timeout, java)
    except OSError as exc:
        return False, "", (
            f"java probe failed: {exc}\n\n"
            "เป็นไปได้ว่า:\n"
            "• ไฟล์ JDK ไม่ครบ (โดน antivirus ลบ jvm.dll/libjvm) — "
            "ลองแตก ZIP โปรแกรมใหม่ทั้งก้อน\n"
            "• Path มีอักษรพิเศษ — ย้ายโฟลเดอร์ไปไว้ใน path ภาษาอังกฤษ\n"
            "• เครื่อง ARM ไม่มี Rosetta — ติดตั้ง Rosetta "
            "(macOS) หรือดาวน์โหลด JDK ตรงสถาปัตยกรรม"
        )

    raw = (r.stderr or r.stdout or "").strip()
    if not raw:
        return False, "", "java -version produced no output"

    # Special-case the "could not find java.dll" failure mode
    # that ships under non-ASCII Windows paths — it's specific
    # enough to deserve its own diagnostic with a copy-paste fix.
    if "could not find java.dll" in raw.lower():
        return False, "", _java_dll_message(java)

    # Walk the lines looking for the first one that looks like
    # a real version banner. Any real OpenJDK output puts that
    # within the first 2-3 lines.
    for line in raw.splitlines():
        if _looks_like_version_line(line):
            return True, line.strip(), ""

    # No recognisable version banner — surface the raw output
    # truncated so the customer can screenshot it for support.
    short = raw.replace("\n", " | ")[:200]
    return False, "", f"java -version returned unexpected output: {short}"


def _probe_java_version(java: Path) -> tuple[bool, str, str]:
    """Resilient ``java -version`` for bundled JDKs.

    On the first call after extracting the customer ZIP, macOS
    Gatekeeper / Windows SmartScreen can block ``java`` for
    10-30 s while they walk every bundled dylib/dll. We:

    1. Run with a generous 30 s timeout (was 5 s — too tight for
       first-launch notarization checks).
    2. On timeout, if we own the ``jdk-21/`` tree (i.e. it's our
       bundled JDK rather than a system install), strip the
       OS-specific "downloaded from internet" marker
       (``com.apple.quarantine`` on macOS, ``Zone.Identifier`` on
       Windows) and retry exactly once.
    3. Surface a Thai-language error listing the most common
       root causes with copy-paste self-heal commands when both
       attempts fail.

    Returns (ok, version_string, error_message).
    """
    ok, vstr, err = _run_java_version(java, _JAVA_PROBE_TIMEOUT_S)
    if ok:
        return True, vstr, ""

    # Only the timeout path is worth a quarantine-strip + retry —
    # OSError / no-output failures point at a different problem
    # (binary missing the exec bit, wrong arch, …) that ``xattr``
    # won't help with.
    if "timed out" not in err:
        return False, "", err

    jdk_root = _jdk_root_from_java(java)
    if jdk_root is None or not jdk_root.is_dir():
        return False, "", err
    if not _self_heal_jdk(jdk_root):
        return False, "", err

    log.info("retrying java probe after self-heal")
    ok2, vstr2, err2 = _run_java_version(java, _JAVA_PROBE_TIMEOUT_S)
    if ok2:
        return True, vstr2, ""
    return False, "", err2 or err


def warm_up_java(java: Path | None) -> None:
    """Fire ``java -version`` in the background to warm OS caches.

    Called from ``StudioApp.__init__`` so by the time the customer
    actually clicks "Patch", Gatekeeper / SmartScreen have already
    finished validating every bundled dylib/dll (the verdict is
    cached for the rest of the boot). Without this warm-up the
    customer sees the Studio open instantly but pays the 10-30 s
    cold-start cost the first time they patch a phone — which
    looks like the patch button is broken.

    Best-effort: runs in a daemon thread, swallows every exception,
    never raises. Self-heals quarantine/MOTW first when present so
    the warm-up itself is more likely to complete inside the
    timeout window.
    """
    if java is None:
        return
    import threading

    def _warm() -> None:
        try:
            jdk_root = _jdk_root_from_java(java)
            if jdk_root is not None:
                _self_heal_jdk(jdk_root)
            ok, vstr, err = _run_java_version(java, _JAVA_PROBE_TIMEOUT_S)
            if ok:
                log.info("java warm-up ok: %s", vstr)
            else:
                log.info("java warm-up failed: %s", err.splitlines()[0])
        except Exception:
            log.debug("java warm-up crashed", exc_info=True)

    threading.Thread(
        target=_warm, daemon=True, name="java-warm-up",
    ).start()


def jdk_diagnostic(java: Path | None) -> dict:
    """Collect quick JDK health facts for the support diagnostic ZIP.

    No subprocess calls — we look at on-disk artefacts only so
    this is safe to call from the startup diagnostic path even
    when ``java`` itself is hung. The numbers tell support whether
    the JDK was extracted completely (jvm.dll / libjvm.dylib
    should be ~10-20 MB; AV truncation usually drops them to 0).
    """
    info: dict = {
        "java_path": str(java) if java else "",
        "java_exists": bool(java and java.is_file()),
        "jdk_root": "",
        "jvm_size": 0,
        "cloud_sync": "",
        "non_ascii_path": False,
        "java_dll_present": False,
    }
    if java is None:
        return info
    info["cloud_sync"] = detect_cloud_sync_folder(java)
    info["non_ascii_path"] = _path_has_non_ascii(java)
    jdk_root = _jdk_root_from_java(java)
    if jdk_root is None:
        return info
    info["jdk_root"] = str(jdk_root)
    if platform_tools.current_os() == "windows":
        candidate = jdk_root / "bin" / "server" / "jvm.dll"
        # The launcher backend lives at ``bin/java.dll``; missing
        # this is the smoking gun for the "could not find java.dll"
        # error we surface in ``_java_dll_message``.
        info["java_dll_present"] = (jdk_root / "bin" / "java.dll").is_file()
    elif platform_tools.current_os() == "macos":
        candidate = (
            jdk_root / "Contents" / "Home" / "lib" / "server" / "libjvm.dylib"
        )
    else:
        candidate = jdk_root / "lib" / "server" / "libjvm.so"
    try:
        if candidate.is_file():
            info["jvm_size"] = candidate.stat().st_size
    except OSError:
        pass
    return info


# ────────────────────────────────────────────────────────────
#  pipeline
# ────────────────────────────────────────────────────────────

class LSPatchPipeline:
    """Pull → patch → install. Each step is independently callable."""

    def __init__(self, cfg: StreamConfig) -> None:
        self.cfg = cfg
        self.cache_dir = (PROJECT_ROOT.parent / ".cache" / "lspatch").resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pulled_dir = self.cache_dir / "pulled"
        self.patched_dir = self.cache_dir / "patched"

    # ──────────────────────────────
    #  tool discovery
    # ──────────────────────────────

    def probe_tools(self) -> ToolStatus:
        # Resolve every tool through the cross-platform resolver so
        # macOS / Windows / Linux all pick the right binary layout.
        paths = platform_tools.discover()
        # Prefer the configured adb if it works (lets the user override
        # via config.json), else fall back to whatever resolver found.
        configured_adb = self.cfg.adb_path or "adb"
        adb_str = (
            configured_adb
            if shutil.which(configured_adb)
            else (str(paths.adb) if paths.adb else configured_adb)
        )
        st = ToolStatus(adb=adb_str)

        if paths.java is not None:
            ok, vstr, err = _probe_java_version(paths.java)
            if ok:
                st.java = paths.java
                st.java_version = vstr
                m = re.search(r'"(\d+)\.', vstr) or re.search(
                    r'"(\d+)"', vstr
                )
                major = int(m.group(1)) if m else 0
                if major < 21:
                    st.errors.append(
                        f"Java {major} is too old; LSPatch needs JDK 21+. "
                        f"Run setup script in tools/ to install bundled JDK."
                    )
            else:
                st.errors.append(err)
        else:
            st.errors.append(
                "JDK 21 not found. Expected under "
                f".tools/{platform_tools.current_os()}/jdk-21/."
            )

        if paths.lspatch_jar is not None:
            st.lspatch = paths.lspatch_jar
        else:
            st.errors.append(
                "lspatch.jar missing — expected at "
                f".tools/{platform_tools.current_os()}/lspatch/lspatch.jar"
            )

        if paths.vcam_apk is not None:
            st.vcam_apk = paths.vcam_apk
        else:
            st.errors.append(
                "vcam-app APK not found. Looked under apk/ and "
                "vcam-app/app/build/outputs/apk/."
            )

        if shutil.which(st.adb) is None:
            st.errors.append(f"adb not found on PATH: {st.adb}")

        st.ok = not st.errors
        return st

    # ──────────────────────────────
    #  pull
    # ──────────────────────────────

    def detect_tiktok(self, serial: str | None = None) -> str:
        """Return the installed TikTok variant package name, or ''.

        Detection runs in two passes:

        1. Fast path — exact match against the canonical
           ``TIKTOK_PACKAGES`` list. ~99% of customers hit this.

        2. Discovery path — list every installed package and match
           against a pattern that covers regional/beta/OEM-store
           variants we don't have hardcoded (e.g. preload SKUs that
           come with some Xiaomi ROMs). This prevents the patch
           pipeline from telling a customer "no TikTok variant
           installed" when their TikTok is actually right there
           under an unfamiliar package name.
        """
        for pkg in TIKTOK_PACKAGES:
            if self._pkg_installed(pkg, serial):
                return pkg

        # Discovery path: scan installed packages for anything that
        # *looks* like TikTok. ``pm list packages`` is much cheaper
        # than calling ``pm path`` per candidate, and gives us the
        # full inventory in one round-trip.
        listing = self._adb_shell("pm list packages", serial)
        for line in listing.splitlines():
            line = line.strip()
            if not line.startswith("package:"):
                continue
            pkg = line[len("package:"):].strip()
            if _TIKTOK_PKG_PATTERNS.match(pkg):
                log.info(
                    "detect_tiktok: discovered non-canonical TikTok "
                    "package %r on %s", pkg, serial or "default",
                )
                return pkg
        return ""

    def _pkg_installed(self, pkg: str, serial: str | None) -> bool:
        out = self._adb_shell(f"pm path {pkg}", serial)
        return bool(out and out.startswith("package:"))

    def pull_tiktok(
        self,
        package: str = "",
        serial: str | None = None,
    ) -> PullResult:
        """`adb pull` every APK that makes up TikTok into self.pulled_dir.

        TikTok ships as a base.apk + 30-50 split APKs (locale, ABI,
        feature modules). All of them must be patched and re-installed
        together, otherwise PackageManager refuses with
        INSTALL_FAILED_MISSING_SPLIT.

        Device keep-awake
        -----------------
        Before any pull we wake the screen and force
        ``svc power stayon usb`` so the phone's OEM battery
        manager (looking at you, Vivo / Oppo) can't suspend
        ``adbd`` halfway through a 100-300 MB transfer. The
        ``finally`` restores stayon to the default so we don't
        leave the customer's screen on forever.
        """
        if not package:
            package = self.detect_tiktok(serial)
        if not package:
            return PullResult(False, error="no TikTok variant installed")

        # Wipe the pull cache so we never mix old and new APKs.
        if self.pulled_dir.exists():
            shutil.rmtree(self.pulled_dir)
        self.pulled_dir.mkdir(parents=True, exist_ok=True)

        # Keep the device awake for the entire pull. Best-effort
        # and reverted in the ``finally``. See the block comment
        # over ``_keep_device_awake`` for the full rationale.
        self._keep_device_awake(serial)
        try:
            # Each line of `pm path` is `package:/data/app/.../base.apk`.
            out = self._adb_shell(f"pm path {package}", serial)
            paths = [
                line[len("package:"):].strip()
                for line in out.splitlines()
                if line.startswith("package:")
            ]
            if not paths:
                return PullResult(False, package=package,
                                  error="pm path returned no APKs")

            version = self._adb_shell(
                f"dumpsys package {package} | grep -m1 versionName", serial
            )
            m = re.search(r"versionName=(\S+)", version)
            version_name = m.group(1) if m else "?"

            t0 = time.monotonic()
            pulled: list[Path] = []
            for p in paths:
                fname = p.rsplit("/", 1)[-1]
                dst = self.pulled_dir / fname
                ok, err = self._pull_apk_with_fallback(p, dst, serial)
                if not ok:
                    return PullResult(False, package=package,
                                      version_name=version_name,
                                      elapsed_s=time.monotonic() - t0,
                                      error=err)
                pulled.append(dst)

            # Unwrap any APK that's already LSPatched. Re-patching a
            # patched APK fails with "Cannot read entry … overlaps" because
            # apkzlib chokes on the deeply-nested zip layout. Replacing the
            # outer wrapper with its embedded ``assets/lspatch/origin.apk``
            # gives us a clean base for the next round.
            unwrapped = self._unwrap_lspatched(pulled)

            return PullResult(
                ok=True,
                package=package,
                version_name=version_name,
                apks=unwrapped,
                elapsed_s=time.monotonic() - t0,
            )
        finally:
            self._release_keep_awake(serial)

    @staticmethod
    def _unwrap_lspatched(apks: list[Path]) -> list[Path]:
        """For every APK that's already LSPatched, replace its bytes
        with the original wrapped inside ``assets/lspatch/origin.apk``.

        Why the two-stage extract?
        --------------------------
        Python 3.13's ``zipfile`` added a "possible zip bomb"
        defensive check that refuses to ``open()`` any entry whose
        local-header bytes overlap another entry's. apkzlib (the
        Android-toolchain zip writer) intentionally produces
        overlapping entries when an APK has been LSPatched twice —
        so ``zf.open("assets/lspatch/origin.apk")`` raises
        ``BadZipFile: Overlapped entries`` even though the entry
        is perfectly readable.

        Workaround: shell out to ``unzip -p`` (the BSD/Info-ZIP CLI
        that ships with macOS / every Linux distro). It happily
        ignores the overlap and gives us the bytes we want. We
        keep the ``zipfile.namelist()`` lookup because it doesn't
        require reading entry data, and only fall through to
        ``unzip`` for the actual extraction.
        """
        import zipfile

        unzip = shutil.which("unzip")
        out: list[Path] = []
        for apk in apks:
            try:
                with zipfile.ZipFile(apk, "r") as zf:
                    if "assets/lspatch/origin.apk" not in zf.namelist():
                        out.append(apk)
                        continue
                tmp = apk.with_suffix(apk.suffix + ".origin")

                extracted = False
                # 1. Try Python's native extraction first (fastest, no
                #    subprocess overhead) — but it might trip the new
                #    overlap check.
                try:
                    with zipfile.ZipFile(apk, "r") as zf:
                        with zf.open("assets/lspatch/origin.apk") as src, \
                             tmp.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                    extracted = True
                except zipfile.BadZipFile as e:
                    if "Overlapped" not in str(e):
                        raise
                    # 2. Fallback: shell `unzip -p` which doesn't have
                    #    the overlap-bomb check.
                    if unzip is None:
                        raise RuntimeError(
                            "Python zipfile rejected this LSPatched APK as "
                            "having overlapped entries, and `unzip` is not "
                            "installed for the fallback path."
                        ) from e
                    with tmp.open("wb") as dst:
                        proc = subprocess.run(
                            [unzip, "-p", str(apk),
                             "assets/lspatch/origin.apk"],
                            stdout=dst, stderr=subprocess.PIPE,
                            check=False, timeout=120,
                        )
                    if proc.returncode != 0 or tmp.stat().st_size == 0:
                        raise RuntimeError(
                            f"unzip -p failed: rc={proc.returncode} "
                            f"err={(proc.stderr or b'').decode(errors='replace')[:200]}"
                        )
                    extracted = True
                    log.info(
                        "unwrap %s: used unzip -p fallback (zipfile "
                        "tripped the overlap check)", apk.name,
                    )

                if extracted:
                    tmp.replace(apk)
                    log.info("unwrapped lspatched APK: %s", apk.name)
            except Exception:
                log.exception("unwrap of %s failed; using as-is", apk.name)
                # tmp may be partial; clean up so a later run isn't
                # fooled into thinking we already unwrapped this APK.
                try:
                    tmp = apk.with_suffix(apk.suffix + ".origin")
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
            out.append(apk)
        return out

    # ──────────────────────────────
    #  patch
    # ──────────────────────────────

    def patch(
        self,
        apks: list[Path],
        sigbypass_level: int = 2,
    ) -> PatchResult:
        """Run LSPatch over base + every split, embedding vcam-app.

        sigbypass_level=2 means LSPatch hooks both PackageManager AND
        openat() so TikTok's runtime self-signature checks see the
        original signature, not the LSPatch debug key.
        """
        st = self.probe_tools()
        if not st.ok:
            return PatchResult(False, self.patched_dir,
                               error="; ".join(st.errors))
        assert st.java and st.lspatch and st.vcam_apk  # narrow for mypy

        if self.patched_dir.exists():
            shutil.rmtree(self.patched_dir)
        self.patched_dir.mkdir(parents=True, exist_ok=True)

        cmd: list[str] = [
            str(st.java),
            "-jar", str(st.lspatch),
            *[str(a) for a in apks],
            "-m", str(st.vcam_apk),
            "-l", str(sigbypass_level),
            "-f",  # force overwrite
            "-o", str(self.patched_dir),
        ]
        log.info("LSPatch: %s", " ".join(cmd))

        # Force English/Gregorian locale: Java's apkzlib uses
        # MsDosDateTimeUtils.packCurrentDate which only accepts years
        # 1980-2107. On Thai macOS the JVM defaults to BuddhistCalendar
        # (year = 2569) and the patch crashes with VerifyException.
        # Same fix needed if the customer's Windows locale is Thai.
        env = platform_tools.make_subprocess_env(
            extra_path=[st.java.parent] if st.java else None,
        )

        t0 = time.monotonic()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=600, check=False, env=env)
        except subprocess.TimeoutExpired:
            return PatchResult(False, self.patched_dir,
                               elapsed_s=time.monotonic() - t0,
                               error="lspatch timed out (>10 min)")
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
            return PatchResult(False, self.patched_dir,
                               elapsed_s=elapsed,
                               error="lspatch exited non-zero",
                               log_tail="\n".join(tail))

        outputs = sorted(self.patched_dir.glob("*-lspatched.apk"))
        if not outputs:
            tail = (proc.stdout or "").strip().splitlines()[-15:]
            return PatchResult(False, self.patched_dir,
                               elapsed_s=elapsed,
                               error="lspatch produced no output APKs",
                               log_tail="\n".join(tail))

        return PatchResult(
            ok=True,
            output_dir=self.patched_dir,
            patched_apks=outputs,
            elapsed_s=elapsed,
            log_tail=(proc.stdout or "").strip().splitlines()[-3:][0]
            if proc.stdout else "",
        )

    # ──────────────────────────────
    #  install
    # ──────────────────────────────

    def install(
        self,
        package: str,
        patched_apks: list[Path],
        serial: str | None = None,
        uninstall_first: bool = True,
        original_apks: list[Path] | None = None,
    ) -> InstallResult:
        """Uninstall the original, then `adb install-multiple` the patched bundle.

        IMPORTANT: this will log the user out of TikTok (different
        signing key → different sandbox). Always confirm with the user
        before calling.

        Rollback safety
        ---------------
        If the patched ``install-multiple`` fails *after* we've already
        uninstalled the original, the customer's phone is left without
        TikTok entirely — a state that's both confusing and
        irrecoverable from inside our app (Play Store sign-in, OTP,
        etc. are all out-of-band). To prevent this, callers can pass
        ``original_apks=`` (the same list ``pull_tiktok`` returned).
        On failure we then re-install those originals so the customer's
        phone is back to its pre-Patch state, and the GUI can show a
        "rolled back, please retry" message instead of "TikTok is
        gone, sorry".
        """
        adb = self.cfg.adb_path
        if not patched_apks:
            return InstallResult(False, error="no patched APKs to install")

        t0 = time.monotonic()

        # Step 1: uninstall the original. If it isn't installed that's
        # fine — `adb uninstall` returns nonzero but we ignore.
        if uninstall_first:
            cmd = [adb]
            if serial:
                cmd += ["-s", serial]
            cmd += ["uninstall", package]
            subprocess.run(cmd, capture_output=True, text=True,
                           timeout=30, check=False)

        # Step 2: install-multiple the entire patched bundle.
        cmd = [adb]
        if serial:
            cmd += ["-s", serial]
        cmd += ["install-multiple", "-r", *[str(p) for p in patched_apks]]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=600, check=False)
        except subprocess.TimeoutExpired:
            return self._rollback_install(
                package=package,
                original_apks=original_apks,
                serial=serial,
                t0=t0,
                error="install-multiple timed out",
            )
        elapsed = time.monotonic() - t0
        if r.returncode != 0 or "Success" not in (r.stdout or ""):
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-5:]
            return self._rollback_install(
                package=package,
                original_apks=original_apks,
                serial=serial,
                t0=t0,
                error="\n".join(tail),
            )

        # Step 3: read back the new signature so we can show "patched"
        # in the GUI as confirmation. Uses the shared multi-pattern
        # parser from ``hook_status`` so we don't drift between the
        # install-time fingerprint and the runtime probe — they MUST
        # extract the same hex string for the per-device baseline to
        # match on subsequent probes.
        from . import hook_status as _hs
        sig = self._adb_shell(
            f"dumpsys package {package} | "
            "grep -iE -A2 'signatures|signingInfo|signers|cert digests'",
            serial,
        )
        fp = _hs._extract_fingerprint(sig or "")

        return InstallResult(ok=True, elapsed_s=elapsed, fingerprint=fp)

    def _rollback_install(
        self,
        *,
        package: str,
        original_apks: list[Path] | None,
        serial: str | None,
        t0: float,
        error: str,
    ) -> InstallResult:
        """Try to re-install the original APKs after a failed patch install.

        Returns an ``InstallResult(ok=False, ...)`` describing both the
        primary failure and whether the rollback succeeded. The GUI
        uses ``rollback_ok`` to show the customer either:

        * "Patch failed but TikTok เดิมยังอยู่ — ลองใหม่ได้เลย" (rolled back)
        * "Patch failed — TikTok หายไปจากเครื่อง โปรดลง TikTok ใหม่
          จาก Play Store" (rollback skipped or failed)
        """
        elapsed = time.monotonic() - t0

        # No originals to roll back to: bail with the primary error.
        if not original_apks:
            return InstallResult(
                ok=False,
                elapsed_s=elapsed,
                error=error,
                rollback_attempted=False,
            )

        # Some pulls dump APKs into ``self.pulled_dir`` which we wipe
        # at the start of every pull. If the files have been deleted
        # since (e.g. another pull happened in parallel), we can't
        # roll back even though the caller passed the list.
        existing = [p for p in original_apks if p.exists()]
        if not existing:
            log.warning(
                "rollback skipped: pulled APKs missing on disk "
                "(%d expected)", len(original_apks),
            )
            return InstallResult(
                ok=False,
                elapsed_s=elapsed,
                error=error,
                rollback_attempted=False,
            )

        adb = self.cfg.adb_path
        cmd = [adb]
        if serial:
            cmd += ["-s", serial]
        cmd += ["install-multiple", "-r", *[str(p) for p in existing]]

        log.warning(
            "patch install failed (%s); attempting rollback of %d APKs",
            error.splitlines()[0] if error else "unknown", len(existing),
        )
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=600, check=False,
            )
        except subprocess.TimeoutExpired:
            return InstallResult(
                ok=False,
                elapsed_s=time.monotonic() - t0,
                error=error,
                rollback_attempted=True,
                rollback_ok=False,
                rollback_error="rollback install-multiple timed out",
            )

        ok = r.returncode == 0 and "Success" in (r.stdout or "")
        rb_err = ""
        if not ok:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-5:]
            rb_err = "\n".join(tail) or "rollback failed (unknown)"
            log.error("rollback failed: %s", rb_err)
        else:
            log.info("rollback succeeded — original TikTok restored")

        return InstallResult(
            ok=False,
            elapsed_s=time.monotonic() - t0,
            error=error,
            rollback_attempted=True,
            rollback_ok=ok,
            rollback_error=rb_err,
        )

    # ──────────────────────────────
    #  status
    # ──────────────────────────────

    def installed_status(
        self,
        serial: str | None = None,
    ) -> dict[str, str]:
        """Tell the GUI: which TikTok is installed, is it patched?"""
        out: dict[str, str] = {
            "package": "",
            "version": "",
            "fingerprint": "",
            "patched": "unknown",
        }
        for pkg in TIKTOK_PACKAGES:
            if not self._pkg_installed(pkg, serial):
                continue
            out["package"] = pkg
            ver = self._adb_shell(
                f"dumpsys package {pkg} | grep -m1 versionName", serial)
            m = re.search(r"versionName=(\S+)", ver)
            out["version"] = m.group(1) if m else "?"

            from . import hook_status as _hs
            sig = self._adb_shell(
                f"dumpsys package {pkg} | "
                "grep -iE -A2 'signatures|signingInfo|signers|cert digests'",
                serial,
            )
            out["fingerprint"] = _hs._extract_fingerprint(sig or "")

            # LSPatch's debug-keystore self-signed cert produces one
            # of a small set of known fingerprint prefixes (the
            # tuple is maintained centrally in hook_status). Match
            # against the whole list so legacy + current LSPatch
            # builds both detect as patched.
            fp = out["fingerprint"]
            patched = any(
                fp.startswith(p)
                for p in _hs._KNOWN_LSPATCH_FINGERPRINT_PREFIXES
            )
            out["patched"] = "yes" if patched else "no"
            break
        return out

    # ──────────────────────────────
    #  internals
    # ──────────────────────────────

    def _adb_shell(self, cmd: str, serial: str | None = None) -> str:
        adb = self.cfg.adb_path
        args = [adb]
        if serial:
            args += ["-s", serial]
        args += ["shell", cmd]
        try:
            r = subprocess.run(args, capture_output=True, text=True,
                               timeout=10, check=False)
        except subprocess.TimeoutExpired:
            return ""
        return (r.stdout or "").strip()

    # ──────────────────────────────
    #  pull helpers
    # ──────────────────────────────

    @staticmethod
    def _clean_adb_progress(text: str) -> str:
        """Strip ``adb pull``'s in-progress ``[ NN%]`` lines from output.

        ``adb pull`` writes a real-time progress bar to stderr while
        the transfer runs:

            ``[  0%] /data/app/.../base.apk``
            ``[ 47%] /data/app/.../base.apk``
            ``[100%] /data/app/.../base.apk``
            ``adb: error: failed to copy ... permission denied``

        Our pre-fix code took ``stderr.splitlines()[-2:]`` which
        sometimes captured the progress line instead of the actual
        error — customers saw ``pull ล้มเหลว: [ 0%]`` with no clue
        what went wrong. Filter the progress noise so the real
        message bubbles up.
        """
        kept: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # ``[NN%]`` or ``[ NN%]`` progress (any digits 0-9).
            if re.match(r"^\[\s*\d+%\]", stripped):
                continue
            kept.append(stripped)
        return "\n".join(kept)

    @staticmethod
    def _is_device_unreachable(text: str) -> bool:
        """Detect adb errors that mean "the device went away".

        When ``adb`` reports any of these, retrying through a
        different transfer mode (``exec-out``, ``/sdcard`` staging)
        is pointless — they all funnel through the same transport
        and will fail the same way. Worse, the customer sees three
        scary errors that all really say the same thing.

        We match on the lower-cased trail of an adb command so the
        check is robust to ``adb`` localisations and minor wording
        changes between platform-tools versions.
        """
        if not text:
            return False
        lower = text.lower()
        return any(
            marker in lower
            for marker in (
                # ``error: device 'XYZ' not found`` — adb can't find
                # the serial in its device list.
                "not found",
                # ``error: no devices/emulators found`` — daemon
                # knows of no devices at all.
                "no devices",
                # ``error: device offline`` — adb sees the device
                # but the transport is in a half-broken state
                # (often from a sleep/wake cycle on the phone).
                "device offline",
                # ``error: device unauthorized`` — RSA fingerprint
                # was revoked or the "always allow" prompt was
                # answered "Cancel".
                "device unauthorized",
                # ``failed to get feature set`` — adb client and
                # the device-side adbd couldn't negotiate. Happens
                # right after the device disappears mid-command.
                "failed to get feature set",
                # ``cannot connect to daemon`` — the local adb
                # server died. Different bug, same outcome: pull
                # ladder won't recover.
                "cannot connect to daemon",
                # ``protocol fault`` — wire-level corruption,
                # almost always a flaky USB cable.
                "protocol fault",
            )
        )

    def _device_state(self, serial: str | None) -> str:
        """Probe ``adb get-state`` and return a canonical state.

        Returns one of:

        * ``'device'``       — fully online and authorized.
        * ``'offline'``      — adb sees it, transport broken.
        * ``'unauthorized'`` — pairing prompt not accepted.
        * ``'no-device'``    — adb has never heard of this serial,
          or it disappeared. This is the case for the customer in
          the v1.8.x bug screenshot.
        * ``''``             — probe itself failed (adb missing /
          timed out). Caller should treat as "unknown".

        Mirrors the helper in ``studio_pages._adb_get_state`` but
        lives here so the pipeline doesn't import from the UI layer.
        """
        adb = self.cfg.adb_path
        if not adb:
            return ""
        args = [adb]
        if serial:
            args += ["-s", serial]
        args += ["get-state"]
        try:
            r = subprocess.run(args, capture_output=True, text=True,
                               timeout=3.0, check=False)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""
        if r.returncode == 0:
            return (r.stdout or "").strip()
        # Non-zero return: distinguish "device unknown to adb"
        # from generic ``adb get-state`` failures so the caller
        # can render an actionable error.
        stderr = (r.stderr or "").lower()
        if "not found" in stderr or "no devices" in stderr:
            return "no-device"
        return ""

    # ──────────────────────────────
    #  keep-awake / reconnect (v1.8.x customer recurrence fix)
    # ──────────────────────────────
    #
    # Recurring customer issue: the pull ladder kept failing on
    # Vivo Funtouch / Oppo ColorOS devices with "device 'XYZ' not
    # found" mid-pull. Forensics on the screenshot:
    #
    # 1. ``pm path`` + ``dumpsys`` succeed → device IS online at
    #    pipeline start.
    # 2. ``adb pull`` of base.apk (100-300 MB on TikTok) is invoked.
    # 3. Partway through the transfer the device drops; adb prints
    #    progress lines but never gets to write the final error,
    #    so stderr (after stripping ``[ NN%]``) is empty.
    # 4. Attempts 2 and 3 see "device not found" immediately.
    #
    # Root causes seen in the wild:
    #
    # * Phone screen turns off → OEM aggressive battery management
    #   suspends adbd. Confirmed on Vivo, Oppo, some MIUI.
    # * ``adb authorization timeout`` set to ~1 h on Vivo (AOSP
    #   default is 7 days) — expires mid-session.
    # * Windows USB selective suspend — host side suspends what it
    #   thinks is an "idle" device.
    # * Transient flap — cable wiggle, USB hub re-enumeration.
    #
    # Three preventative layers, in increasing intrusiveness:
    #
    # A. **Keep awake** — before the pipeline starts pulling, wake
    #    the screen and set ``svc power stayon usb`` so the phone
    #    can't doze adbd off while plugged in. Restored in
    #    ``finally`` so we leave the device the way we found it.
    #
    # B. **Pre-flight check** — verify ``adb get-state`` is
    #    ``device`` *right before* each pull. If the device has
    #    drifted to ``unauthorized`` / ``offline`` since the last
    #    successful shell call, bail with a precise hint instead
    #    of burning the 3-attempt ladder on a doomed transfer.
    #
    # C. **Auto-recover** — if a pull drops and ``_is_device_unreachable``
    #    triggers, wait briefly for the device to come back (most
    #    flaps are < 3 s) and retry the SAME pull once before
    #    pronouncing dead. Many customers never see the error
    #    after this.

    def _keep_device_awake(self, serial: str | None) -> None:
        """Wake the phone and force screen-on-while-USB-plugged.

        Best-effort: every call is wrapped so a failure here can
        never block the pipeline. The two shell commands are
        idempotent — calling them on an already-awake / already-
        ``stayon`` device is a no-op.

        Commands used:

        * ``input keyevent KEYCODE_WAKEUP`` (key 224) — turns on
          the screen if asleep. Available since API 24. Safe on
          older builds (input rejects unknown keycodes, doesn't
          fault).
        * ``svc power stayon usb`` — keep screen on while USB
          power is connected. Volatile (resets at reboot); no
          permission required. Available since Android 5.
        """
        try:
            self._adb_shell("input keyevent KEYCODE_WAKEUP", serial)
        except Exception as e:  # noqa: BLE001
            log.debug("keep_awake: KEYCODE_WAKEUP failed: %s", e)
        try:
            self._adb_shell("svc power stayon usb", serial)
        except Exception as e:  # noqa: BLE001
            log.debug("keep_awake: svc power stayon failed: %s", e)

    def _release_keep_awake(self, serial: str | None) -> None:
        """Undo ``_keep_device_awake``. Call from a ``finally``.

        Best-effort: if the device has already disconnected by
        the time we get here we silently swallow the failure.
        Leaving ``stayon usb`` set is harmless on the customer
        side (just keeps screen on while plugged in until a
        reboot or another tool sets it back to false) — but it's
        polite to clean up.
        """
        try:
            self._adb_shell("svc power stayon false", serial)
        except Exception as e:  # noqa: BLE001
            log.debug("release_keep_awake: failed: %s", e)

    def _pre_pull_check(self, serial: str | None) -> tuple[bool, str]:
        """Verify the device is in a state where ``adb pull`` can
        actually succeed. Returns ``(ok, error_message)``.

        Called from :meth:`_pull_apk_with_fallback` right before
        the first transport call. Burns ~50 ms but cuts the
        misleading 3-attempt cascade from ~10 s to ~0.1 s when
        the device has drifted to ``unauthorized`` / ``offline``
        / ``no-device`` since the last successful shell command.

        We trust the state probe because earlier ladder rungs
        already confirmed adb itself is healthy (``pm path`` ran
        for us). So a state mismatch here is the device fault.
        """
        state = self._device_state(serial)
        if state == "device":
            return True, ""
        if state == "unauthorized":
            return False, (
                "มือถือยังไม่อนุญาต USB debugging — ดูที่หน้าจอมือถือ "
                "แล้วกด ✓ Allow + ติ๊ก 'Always allow from this computer'"
            )
        if state == "offline":
            return False, (
                "adb เห็นมือถือแต่ transport ค้าง — มักเกิดหลังมือถือ "
                "Sleep/Wake. แก้: ปิด-เปิด USB debugging ใน Developer "
                "options หรือรัน ``adb kill-server && adb start-server``"
            )
        if state == "no-device":
            return False, (
                "มือถือหลุดจาก adb แล้ว — เช็คสาย USB, ดูว่าหน้าจอ "
                "ปลดล็อกอยู่, แล้วลองใหม่"
            )
        # state == "" → probe itself failed. Don't block the
        # pipeline on a flaky probe; let the ladder try and
        # surface whatever real error comes back.
        return True, ""

    def _wait_for_device_back(
        self,
        serial: str | None,
        timeout: float = 8.0,
    ) -> bool:
        """Poll ``adb get-state`` until the device returns to
        ``'device'`` or ``timeout`` seconds elapse. Returns
        ``True`` if it came back, ``False`` otherwise.

        We poll every 0.5 s rather than using ``adb wait-for-device``
        for two reasons:

        1. ``wait-for-device`` blocks indefinitely until *any*
           device shows up. With a serial filter it gets weird if
           the device returns with a different transport-id.
        2. We want a hard timeout to keep the customer's progress
           bar moving. Eight seconds covers the typical USB flap
           (1-3 s) plus a safety margin for slow hubs, without
           making the customer wonder if the app froze.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._device_state(serial) == "device":
                return True
            time.sleep(0.5)
        return False

    def _pull_apk_with_fallback(
        self,
        remote: str,
        local: Path,
        serial: str | None,
    ) -> tuple[bool, str]:
        """Robust ``adb pull`` for ``/data/app/<random>/base.apk``.

        ``adb pull`` from ``/data/app/`` fails on certain OEM ROMs
        (Vivo Funtouch, Oppo ColorOS, some MIUI builds) because
        their SELinux policy blocks the ``shell`` user from
        reading the ``base.apk`` directly even though the file
        mode would otherwise allow it. We've also seen the same
        error after a TikTok background self-update where
        PackageManager briefly holds an exclusive lock on the
        APK file (``EBUSY``).

        Strategy ladder
        ---------------

        1. ``adb pull`` — the fast path; works on >90 % of devices.

        2. ``adb exec-out cat <remote>`` — uses the ``exec`` service
           rather than ``sync``. Different SELinux transition; on
           a few OEMs this works when ``pull`` doesn't.

        3. Stage via ``/sdcard/`` — copy the APK to a world-readable
           location first, then ``pull`` from there. The catch is
           we need a writable temp dir on the phone; ``/sdcard/Download/``
           is reliably world-writable on every modern Android.

        Returns ``(ok, error_message)``. ``error_message`` is
        Thai-friendly and lists the steps tried so support can
        copy-paste it without further clarification.
        """
        adb = self.cfg.adb_path
        attempts: list[str] = []

        # ── 0. pre-flight: confirm the device is in a state
        # where the ladder *can* succeed. If not, bail with a
        # specific hint — no point burning the 3-attempt cascade
        # on a doomed transfer when adb already knows the device
        # has drifted to unauthorized / offline / no-device.
        ok_state, state_err = self._pre_pull_check(serial)
        if not ok_state:
            log.warning(
                "pull pre-flight failed (serial=%r): %s", serial, state_err,
            )
            return False, (
                f"ดึง APK '{remote.rsplit('/', 1)[-1]}' ไม่สำเร็จ.\n\n"
                f"สาเหตุ: {state_err}"
            )

        # ── 1. native adb pull ─────────────────────────────────
        cmd = [adb]
        if serial:
            cmd += ["-s", serial]
        cmd += ["pull", remote, str(local)]
        t1 = time.monotonic()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=180, check=False)
        except subprocess.TimeoutExpired:
            attempts.append("pull: timeout > 180s")
        else:
            if r.returncode == 0 and local.is_file() and local.stat().st_size > 0:
                return True, ""
            raw1 = r.stderr or r.stdout or ""
            err = self._clean_adb_progress(raw1)
            # When stderr is empty after stripping progress lines,
            # adb almost always means "transport dropped mid-transfer
            # before I could print a final error". Log the elapsed
            # time + partial bytes so support can tell a mid-pull
            # USB flap apart from an instant permission denial.
            if not err:
                elapsed = time.monotonic() - t1
                partial = local.stat().st_size if local.is_file() else 0
                log.warning(
                    "adb pull silent failure: serial=%r remote=%r "
                    "elapsed=%.1fs partial=%d bytes rc=%s",
                    serial, remote, elapsed, partial, r.returncode,
                )
            attempts.append(f"pull: {err.splitlines()[-1] if err else 'failed'}")
            # Auto-recover: if adb says the device is gone, give
            # it a moment to come back (most flaps last 1-3 s) and
            # retry the *same* pull once. Customers should never
            # see the error dialog for a transient drop.
            if self._is_device_unreachable(raw1) or not err:
                if self._wait_for_device_back(serial, timeout=8.0):
                    log.info(
                        "pull retry after %s: device returned",
                        "device-unreachable" if self._is_device_unreachable(raw1)
                        else "silent failure",
                    )
                    attempts.append("→ recovered, retrying pull")
                    t1b = time.monotonic()
                    try:
                        r_retry = subprocess.run(
                            cmd, capture_output=True, text=True,
                            timeout=180, check=False,
                        )
                    except subprocess.TimeoutExpired:
                        attempts.append("pull retry: timeout > 180s")
                    else:
                        if (
                            r_retry.returncode == 0
                            and local.is_file()
                            and local.stat().st_size > 0
                        ):
                            log.info(
                                "pull recovered on retry: serial=%r "
                                "elapsed=%.1fs", serial, time.monotonic() - t1b,
                            )
                            return True, ""
                        raw_retry = r_retry.stderr or r_retry.stdout or ""
                        err_retry = self._clean_adb_progress(raw_retry)
                        attempts.append(
                            "pull retry: "
                            + (err_retry.splitlines()[-1] if err_retry else "failed")
                        )
                        if self._is_device_unreachable(raw_retry):
                            return False, self._device_disconnect_error(
                                remote, local, attempts, serial,
                            )
                else:
                    # Didn't come back in time. Short-circuit;
                    # exec-out and /sdcard rungs would just fail
                    # with another "device not found" each.
                    if self._is_device_unreachable(raw1):
                        return False, self._device_disconnect_error(
                            remote, local, attempts, serial,
                        )

        # ── 2. exec-out cat ────────────────────────────────────
        # Quote the remote path with single quotes so the shell
        # doesn't expand ``~~`` or interpret ``==`` weirdly. Both
        # appear in Android 10+ randomised /data/app/ subdirs.
        cmd = [adb]
        if serial:
            cmd += ["-s", serial]
        # We pass the cat command as a single string to ``exec-out``
        # so the shell on the phone sees it as one tokenised
        # command — same approach as ``adb shell "cat ..."`` but
        # using exec-out for binary-clean stdout streaming.
        quoted = remote.replace("'", "'\\''")
        cmd += ["exec-out", f"cat '{quoted}'"]
        try:
            with local.open("wb") as fh:
                r2 = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE,
                                    timeout=180, check=False)
        except subprocess.TimeoutExpired:
            attempts.append("exec-out cat: timeout > 180s")
        else:
            if r2.returncode == 0 and local.is_file() and local.stat().st_size > 0:
                log.info("pulled via exec-out cat (pull failed): %s", remote)
                return True, ""
            stderr2 = r2.stderr or b""
            if isinstance(stderr2, bytes):
                stderr2 = stderr2.decode("utf-8", "replace")
            cleaned = self._clean_adb_progress(stderr2)
            tail = cleaned.splitlines()[-1] if cleaned else ""
            attempts.append("exec-out cat: " + (tail or "empty output"))
            if self._is_device_unreachable(stderr2):
                return False, self._device_disconnect_error(
                    remote, local, attempts, serial,
                )

        # ── 3. stage via /sdcard/Download ──────────────────────
        # Last-resort copy: shell ``cp`` to a world-writable spot
        # on the phone, then pull from there. Uses ``run-as``-free
        # path because TikTok isn't debuggable. We pick a
        # collision-resistant filename so two parallel patches on
        # different devices don't race on the same staging path.
        import uuid as _uuid
        stage_name = f"npc_{_uuid.uuid4().hex[:8]}_{local.name}"
        stage_path = f"/sdcard/Download/{stage_name}"

        cp_out = self._adb_shell(f"cp '{quoted}' '{stage_path}'", serial)
        if "Permission denied" in cp_out or "No such file" in cp_out:
            attempts.append(f"sdcard cp: {cp_out.strip()}")
        else:
            cmd = [adb]
            if serial:
                cmd += ["-s", serial]
            cmd += ["pull", stage_path, str(local)]
            try:
                r3 = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=180, check=False)
            except subprocess.TimeoutExpired:
                attempts.append("sdcard pull: timeout > 180s")
            else:
                # Always try to clean up the staging file — both on
                # success (don't leave it cluttering the customer's
                # Downloads folder on the phone) and on failure
                # (sliver of disk space).
                self._adb_shell(f"rm -f '{stage_path}'", serial)
                if r3.returncode == 0 and local.is_file() and local.stat().st_size > 0:
                    log.info("pulled via /sdcard staging (pull/cat failed): %s", remote)
                    return True, ""
                raw3 = r3.stderr or r3.stdout or ""
                err = self._clean_adb_progress(raw3)
                attempts.append(
                    f"sdcard pull: {err.splitlines()[-1] if err else 'failed'}"
                )
                if self._is_device_unreachable(raw3):
                    return False, self._device_disconnect_error(
                        remote, local, attempts, serial,
                    )

        # All three attempts failed — surface the trail so support
        # has something concrete to look at.
        msg = (
            f"ดึง APK '{remote.rsplit('/', 1)[-1]}' ไม่สำเร็จ.\n\n"
            "ลองแล้ว 3 ทาง:\n"
            + "\n".join(f"  {i+1}. {a}" for i, a in enumerate(attempts))
            + "\n\nเป็นไปได้ว่า:\n"
            "• ROM/OEM บล็อกการอ่าน /data/app (Vivo / Oppo บางรุ่น) — "
            "ลองเปิด Developer options → Disable adb authorization timeout\n"
            "• TikTok กำลังอัปเดตตัวเองอยู่เบื้องหลัง — รอ 1 นาทีแล้วลองใหม่\n"
            "• พื้นที่ /sdcard/Download/ เต็ม — ล้าง cache แล้วลองใหม่\n"
            "• สาย USB หลวม — ถอด-เสียบใหม่"
        )
        # Make sure we don't leave a partial / zero-byte file on
        # disk — the install pipeline would otherwise pick it up
        # as if pull had succeeded.
        if local.is_file() and local.stat().st_size == 0:
            try:
                local.unlink()
            except OSError:
                pass
        return False, msg

    def _device_disconnect_error(
        self,
        remote: str,
        local: Path,
        attempts: list[str],
        serial: str | None,
    ) -> str:
        """Build the Thai-language error for the device-gone case.

        Called from :meth:`_pull_apk_with_fallback` when any of the
        three transfer strategies hits a "device not found / offline
        / unauthorized" error. We probe ``adb get-state`` so the
        message can tell the customer *exactly* what to fix instead
        of the generic Vivo/Oppo SELinux hint that confused users
        in the v1.8.x customer report (see the screenshot in PR
        notes for that release).
        """
        # Clean up any zero-byte partial — same invariant as the
        # bottom of ``_pull_apk_with_fallback``.
        if local.is_file() and local.stat().st_size == 0:
            try:
                local.unlink()
            except OSError:
                pass

        state = self._device_state(serial)
        log.warning(
            "pull aborted: device unreachable (state=%r, serial=%r)",
            state, serial,
        )

        # Pick the hint list based on what adb actually thinks the
        # device is doing right now. Each state has a different
        # remediation; lumping them together (as the old code did)
        # cost us a support round-trip per ticket.
        if state == "unauthorized":
            hints = (
                "• มือถือยังไม่อนุญาต USB debugging — "
                "ดูที่หน้าจอมือถือ แล้วกด ✓ Allow / ติ๊ก 'Always allow from this computer'\n"
                "• ถ้าไม่มี dialog เด้ง: Developer options → "
                "Revoke USB debugging authorizations → ถอด-เสียบสาย USB ใหม่"
            )
        elif state == "offline":
            hints = (
                "• adb เห็นเครื่องแต่ transport พัง — มักเกิดหลังมือถือ Sleep/Wake\n"
                "• แก้: ปิด-เปิด USB debugging (Developer options) "
                "หรือ ``adb kill-server && adb start-server``\n"
                "• ถ้ายังไม่หาย: ถอด-เสียบสาย USB ใหม่"
            )
        else:
            # 'no-device' or unknown — the most common case. The
            # device literally isn't in adb's device list, which
            # means USB cable, USB debugging toggle, or adbd
            # restart on the phone (TikTok crashing the OS, etc.).
            hints = (
                "• สาย USB หลวม / สายเสีย — ลองสายอื่นหรือพอร์ตอื่นบนคอม\n"
                "• USB debugging ถูกปิดอัตโนมัติ (มือถือบางรุ่นปิดเมื่อ Sleep) — "
                "เปิดใหม่ใน Developer options\n"
                "• adbd บนมือถือดับ (TikTok crash / มือถือ reboot) — "
                "ปลดล็อกหน้าจอ + เสียบสาย USB ใหม่\n"
                "• ถ้ายังหาย: รัน ``adb kill-server && adb start-server`` "
                "บนคอม แล้วลองใหม่"
            )

        return (
            f"ดึง APK '{remote.rsplit('/', 1)[-1]}' ไม่สำเร็จ "
            f"เพราะมือถือหลุดจาก adb.\n\n"
            "สาเหตุที่เจอ:\n"
            + "\n".join(f"  {i+1}. {a}" for i, a in enumerate(attempts))
            + (f"\n  (adb get-state = {state!r})" if state else "")
            + "\n\nวิธีแก้:\n"
            + hints
        )
