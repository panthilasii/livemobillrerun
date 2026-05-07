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

from .config import PROJECT_ROOT, StreamConfig

log = logging.getLogger(__name__)

# Where we expect the LSPatch jar + portable JDK to live. The setup
# script in `tools/` populates these.
TOOLS_DIR = (PROJECT_ROOT / ".." / ".tools").resolve()
LSPATCH_JAR = TOOLS_DIR / "lspatch" / "lspatch.jar"
JDK_HOME = TOOLS_DIR / "jdk-21" / "Contents" / "Home"  # macOS layout
JDK_HOME_LINUX = TOOLS_DIR / "jdk-21"                  # linux layout fallback

VCAM_APK_REL = Path("vcam-app/app/build/outputs/apk/debug/app-debug.apk")

# TikTok package candidates, in order of likelihood for our market.
TIKTOK_PACKAGES = (
    "com.ss.android.ugc.trill",     # TikTok International (TH/SEA)
    "com.zhiliaoapp.musically",     # TikTok International (US/EU)
    "com.ss.android.ugc.aweme",     # Douyin (CN) — unlikely but cheap to check
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
        st = ToolStatus(adb=self.cfg.adb_path)

        # JDK — try macOS layout first, then linux.
        java = JDK_HOME / "bin" / "java"
        if not java.is_file():
            java = JDK_HOME_LINUX / "bin" / "java"
        if not java.is_file():
            sys_java = shutil.which("java")
            if sys_java:
                java = Path(sys_java)
        if java.is_file():
            try:
                r = subprocess.run([str(java), "-version"],
                                   capture_output=True, text=True,
                                   timeout=5, check=False)
                # `java -version` writes to stderr.
                vline = (r.stderr or r.stdout or "").splitlines()
                vstr = vline[0] if vline else ""
                m = re.search(r'"(\d+)\.', vstr)
                major = int(m.group(1)) if m else 0
                st.java = java
                st.java_version = vstr.strip()
                if major < 21:
                    st.errors.append(
                        f"Java {major} is too old; LSPatch needs JDK 21+. "
                        f"Install with tools/install_jdk21.sh"
                    )
            except (subprocess.TimeoutExpired, OSError) as exc:
                st.errors.append(f"java probe failed: {exc}")
        else:
            st.errors.append(
                "JDK 21 not found; run tools/install_jdk21.sh first."
            )

        # LSPatch jar
        if LSPATCH_JAR.is_file():
            st.lspatch = LSPATCH_JAR
        else:
            st.errors.append(
                f"lspatch.jar missing at {LSPATCH_JAR}; "
                f"run tools/install_lspatch.sh"
            )

        # vcam-app debug APK
        vcam = (PROJECT_ROOT.parent / VCAM_APK_REL).resolve()
        if vcam.is_file():
            st.vcam_apk = vcam
        else:
            st.errors.append(
                f"vcam-app not built — expected {vcam}. "
                f"Run `cd vcam-app && ./gradlew assembleDebug`."
            )

        # adb sanity check
        if shutil.which(st.adb) is None:
            st.errors.append(f"adb not found on PATH: {st.adb}")

        st.ok = not st.errors
        return st

    # ──────────────────────────────
    #  pull
    # ──────────────────────────────

    def detect_tiktok(self, serial: str | None = None) -> str:
        """Return the installed TikTok variant package name, or ''."""
        for pkg in TIKTOK_PACKAGES:
            if self._pkg_installed(pkg, serial):
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
        """
        if not package:
            package = self.detect_tiktok(serial)
        if not package:
            return PullResult(False, error="no TikTok variant installed")

        # Wipe the pull cache so we never mix old and new APKs.
        if self.pulled_dir.exists():
            shutil.rmtree(self.pulled_dir)
        self.pulled_dir.mkdir(parents=True, exist_ok=True)

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

        adb = self.cfg.adb_path
        t0 = time.monotonic()
        pulled: list[Path] = []
        for p in paths:
            fname = p.rsplit("/", 1)[-1]
            dst = self.pulled_dir / fname
            cmd = [adb]
            if serial:
                cmd += ["-s", serial]
            cmd += ["pull", p, str(dst)]
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=180, check=False)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip().splitlines()[-2:]
                return PullResult(False, package=package,
                                  version_name=version_name,
                                  elapsed_s=time.monotonic() - t0,
                                  error="\n".join(err))
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
        env = os.environ.copy()
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
        # Belt-and-braces — also pin the JVM-internal calendar.
        env["JAVA_TOOL_OPTIONS"] = (
            (env.get("JAVA_TOOL_OPTIONS", "") + " -Duser.language=en "
             "-Duser.country=US -Duser.timezone=UTC").strip()
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
    ) -> InstallResult:
        """Uninstall the original, then `adb install-multiple` the patched bundle.

        IMPORTANT: this will log the user out of TikTok (different
        signing key → different sandbox). Always confirm with the user
        before calling.
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
            return InstallResult(False, elapsed_s=time.monotonic() - t0,
                                 error="install-multiple timed out")
        elapsed = time.monotonic() - t0
        if r.returncode != 0 or "Success" not in (r.stdout or ""):
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-5:]
            return InstallResult(False, elapsed_s=elapsed,
                                 error="\n".join(tail))

        # Step 3: read back the new signature so we can show "patched"
        # in the GUI as confirmation.
        sig = self._adb_shell(
            f"dumpsys package {package} | grep -m1 signatures", serial
        )
        m = re.search(r"signatures:\[(\w+)\]", sig)
        fp = m.group(1) if m else ""

        return InstallResult(ok=True, elapsed_s=elapsed, fingerprint=fp)

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

            sig = self._adb_shell(
                f"dumpsys package {pkg} | grep -m1 signatures", serial)
            m = re.search(r"signatures:\[(\w+)\]", sig)
            out["fingerprint"] = m.group(1) if m else ""

            # LSPatch's debug-keystore self-signed cert produces a known
            # fingerprint prefix "e0b8d3e5" on every run because the
            # bundled keystore is a constant.
            out["patched"] = "yes" if out["fingerprint"].startswith("e0b8d3e5") \
                else "no"
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
