"""Cross-platform tool resolver.

Single source of truth for *where* the bundled tools live across
macOS / Windows / Linux. Every other module that wants ``adb``,
``ffmpeg``, ``java`` (JDK 21) or ``lspatch.jar`` calls into here so
the rest of the codebase stays OS-agnostic.

Layout
------

The portable toolchain lives in ``<project>/.tools/``. We prefer
the per-OS subdirectory layout::

    .tools/
      macos/
        platform-tools/adb
        ffmpeg
        jdk-21/Contents/Home/bin/java
        lspatch/lspatch.jar
      windows/
        platform-tools/adb.exe
        ffmpeg.exe
        jdk-21/bin/java.exe
        lspatch/lspatch.jar
      linux/
        platform-tools/adb
        ffmpeg
        jdk-21/bin/java
        lspatch/lspatch.jar

Legacy macOS-only layouts (``.tools/jdk-21/...`` directly) are still
supported for backward compatibility on dev machines that pre-date
this resolver.

Resolution order
~~~~~~~~~~~~~~~~

For each tool we try, in order:

1. ``.tools/<os>/...`` (the cross-platform bundle layout)
2. ``.tools/...`` (legacy macOS layout)
3. ``shutil.which(<binary>)`` (system-wide install)

The first hit wins. Resolution returns ``None`` if nothing is
found — callers decide whether that's fatal or not.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT


# ── platform identification ──────────────────────────────────────


def current_os() -> str:
    """Return ``"macos" | "windows" | "linux"``."""
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s == "windows":
        return "windows"
    return "linux"


def is_windows() -> bool:
    return current_os() == "windows"


def is_macos() -> bool:
    return current_os() == "macos"


def exe_suffix() -> str:
    return ".exe" if is_windows() else ""


# ── filesystem layout ────────────────────────────────────────────


def _tools_root_base() -> Path:
    """Locate the directory that *contains* the per-OS .tools/ tree.

    The layout differs between launch modes — and getting it wrong
    silently breaks adb/JDK/lspatch lookup, which presents to the
    customer as "the wizard never finds my phone" because the
    bundled adb.exe is unreachable. So this is the single canonical
    source of truth.

    Layouts we have to satisfy
    --------------------------

    * **Dev tree / portable customer ZIP** (run.bat → python -m
      src.main). ``sys.frozen`` is False. ``PROJECT_ROOT`` resolves
      via ``Path(__file__).parent.parent`` to ``vcam-pc/``. The
      ``.tools/`` dir sits at ``vcam-pc/../.tools/`` — i.e. parent
      of PROJECT_ROOT.

    * **Inno Setup install** (``installer.iss`` lays ``.tools/`` at
      ``{app}\\.tools\\``, sibling of NP-Create.exe). Frozen mode,
      ``PROJECT_ROOT`` = install dir = ``{app}``. ``.tools/`` sits
      *inside* PROJECT_ROOT.

    * **macOS .app drag-to-Applications** (``build_dmg.sh`` lays
      ``.tools/`` at ``Contents/MacOS/.tools/``). Frozen mode,
      ``PROJECT_ROOT`` = ``Contents/MacOS/``. ``.tools/`` sits
      inside PROJECT_ROOT.

    * **Portable ZIP with PyInstaller bundle in app/** (some
      customers double-click ``app/NP-Create.exe`` from inside the
      portable ZIP instead of using ``run.bat``). Frozen mode,
      ``PROJECT_ROOT`` = ``<bundle>/app/``. ``.tools/`` sits at
      ``<bundle>/.tools/`` — *parent* of PROJECT_ROOT.

    Defensive walk
    --------------
    Rather than hard-code one rule per mode and pray we covered
    every distribution combo, we *walk up* from the natural
    starting point looking for the first directory that has a
    ``.tools/`` child. This handles all four layouts with a single
    code path and silently absorbs any future installer layout we
    haven't thought of yet.

    The walk is bounded (3 levels) so a missing ``.tools/`` falls
    through to a sensible default rather than wandering into the
    user's home directory and picking up a stale toolchain there.
    """
    if getattr(sys, "frozen", False):
        # Frozen: start at the .exe / .app dir and walk up.
        # PyInstaller --onefile bootloader sets sys.executable to the
        # installer-dropped binary path, so PROJECT_ROOT is the
        # install directory.
        start = PROJECT_ROOT
    else:
        # Source / portable ZIP via run.bat: PROJECT_ROOT is
        # vcam-pc/, ``.tools/`` is one level up.
        start = PROJECT_ROOT.parent

    candidates = [start, start.parent, start.parent.parent]
    for cand in candidates:
        if (cand / ".tools").is_dir():
            return cand

    # Nothing on disk yet — return the most-likely answer for this
    # mode so the resolver can still report a useful "not found"
    # path in error messages.
    return start


# .tools/ root: workspace root in dev / portable, install dir when
# frozen. See ``_tools_root_base`` for why the two paths diverge.
LEGACY_TOOLS_ROOT = (_tools_root_base() / ".tools").resolve()


def tools_root_for(os_name: str | None = None) -> Path:
    """Path to the per-OS tools subdirectory (may not exist yet)."""
    return LEGACY_TOOLS_ROOT / (os_name or current_os())


def _extra_tools_roots() -> list[Path]:
    """Additional ``.tools/`` candidates beyond the canonical layout.

    Two cases worth covering:

    * ``NPCREATE_TOOLS_ROOT`` env var — power-user override for
      shared toolchains (e.g. one customer wired their installs to
      a network-mounted `Z:\\np-create-tools\\` so all 6 phones'
      LSPatch caches live in one place).
    * macOS .app bundle ``Resources/`` — if a future build of
      ``build_dmg.sh`` ever copies ``.tools/macos/`` into
      ``NP-Create.app/Contents/Resources/.tools/`` (the canonical
      Apple location for bundled assets), this candidate keeps
      runtime resolution working without touching code.
    """
    out: list[Path] = []
    env = os.environ.get("NPCREATE_TOOLS_ROOT", "").strip()
    if env:
        out.append(Path(env).expanduser().resolve())
    if getattr(sys, "frozen", False) and is_macos():
        # PROJECT_ROOT in frozen .app = .../Contents/MacOS/. The
        # Apple-canonical asset dir is the sibling Resources/.
        resources = PROJECT_ROOT.parent / "Resources"
        if resources.is_dir():
            out.append(resources.resolve())
    return out


def _candidates(rel: str) -> list[Path]:
    """Generate candidate paths for ``rel`` (a sub-path inside .tools/).

    Order: env-override first, then per-OS, then legacy flat, then
    macOS .app/Contents/Resources/ if relevant. The first existing
    path wins (see ``_first_existing``).
    """
    extras = _extra_tools_roots()
    bases = [tools_root_for(), LEGACY_TOOLS_ROOT]
    # Env override wins over installed defaults so the power-user
    # path can short-circuit a stale bundled tool.
    out: list[Path] = []
    for extra in extras:
        out.append(extra / current_os() / rel)
        out.append(extra / rel)
    for base in bases:
        out.append(base / rel)
    return out


def _first_existing(rels: list[str]) -> Path | None:
    for rel in rels:
        for cand in _candidates(rel):
            if cand.is_file():
                return cand.resolve()
    return None


# ── tool lookup ──────────────────────────────────────────────────


def find_adb() -> Path | None:
    """Return Path to a working ``adb`` binary, or None.

    Lookup order (first hit wins):

    1. ``.tools/<os>/platform-tools/adb`` — the canonical Google
       Android platform-tools zip, downloaded by
       ``tools/setup_windows_tools.py`` / ``setup_macos_tools.py``.
       Preferred because this is the upstream, current adb that
       Google ships, with full feature set (e.g. ``adb pair`` for
       Android 11 wireless debugging).

    2. ``.tools/<os>/android-sdk/platform-tools/adb`` — legacy
       layout from older dev workstations that pre-date the
       per-OS subdirectory split.

    3. ``.tools/<os>/scrcpy/adb`` — scrcpy's GitHub-released zip
       bundles a working adb.exe alongside the screen-mirror
       binary. We extract scrcpy via ``tools/setup_scrcpy.py`` for
       *every* CI build (it's the screen mirror dependency), so
       this path is populated on every customer install — even
       ones where ``setup_*_tools.py`` was skipped. This was the
       Windows 1.7.8 regression: CI bundled scrcpy's adb but
       ``find_adb`` only searched ``platform-tools/``, so the
       installer left customers with no working adb on disk.

    4. ``shutil.which("adb")`` — system-wide install. Power users
       on Linux / dev machines that already have Android Studio.
    """
    sfx = exe_suffix()
    rels = [
        f"platform-tools/adb{sfx}",
        f"android-sdk/platform-tools/adb{sfx}",
        f"scrcpy/adb{sfx}",
    ]
    bundled = _first_existing(rels)
    if bundled:
        return bundled
    sys_adb = shutil.which("adb")
    return Path(sys_adb).resolve() if sys_adb else None


def find_ffmpeg() -> Path | None:
    sfx = exe_suffix()
    bundled = _first_existing([f"ffmpeg{sfx}", f"bin/ffmpeg{sfx}"])
    if bundled:
        return bundled
    sys_ff = shutil.which("ffmpeg")
    return Path(sys_ff).resolve() if sys_ff else None


def find_java() -> Path | None:
    """Return Path to JDK 21+ ``java``. macOS has the ``Contents/Home``
    indirection; Windows + Linux don't.
    """
    sfx = exe_suffix()
    rels = [
        # macOS bundle layout
        f"jdk-21/Contents/Home/bin/java{sfx}",
        # Windows / Linux flat layout
        f"jdk-21/bin/java{sfx}",
        # If user installed under "jdk" instead
        f"jdk/Contents/Home/bin/java{sfx}",
        f"jdk/bin/java{sfx}",
    ]
    bundled = _first_existing(rels)
    if bundled:
        return bundled
    sys_java = shutil.which("java")
    return Path(sys_java).resolve() if sys_java else None


def find_lspatch_jar() -> Path | None:
    return _first_existing(["lspatch/lspatch.jar"])


def find_adb_driver_dir() -> Path | None:
    """Return the directory containing Google's USB driver INF file
    (Windows-only). macOS / Linux callers always get None — Apple
    + libusb on Linux handle Android ADB without a kernel driver.

    The dir layout we expect (matches what
    ``tools/setup_ci_tools.install_adb_driver`` produces)::

        .tools/windows/adb-driver/usb_driver/
            android_winusb.inf      <- the file Windows wants
            androidwinusb86.cat
            androidwinusba64.cat
            amd64/  i386/

    Customers point Device Manager → "Update driver" → "Browse"
    at the *parent* ``adb-driver/`` dir (or its ``usb_driver/``
    subdir). The in-app help dialog
    (``ui.studio_pages.WizardPage._show_driver_help``) opens this
    folder via Explorer / shows the path so non-technical users
    don't have to navigate the install tree manually.
    """
    if not is_windows():
        return None
    rels = [
        "adb-driver/usb_driver/android_winusb.inf",
        "adb-driver/android_winusb.inf",
    ]
    inf = _first_existing(rels)
    if inf is None:
        return None
    return inf.parent.resolve()


def find_scrcpy() -> Path | None:
    """Locate the ``scrcpy`` binary used for the on-PC mirror window.

    scrcpy is the Genymobile project (https://github.com/Genymobile/scrcpy)
    that streams an Android screen over the same ADB transport we
    already use. We **auto-install** it on first Mirror click via
    :mod:`scrcpy_installer` so customers don't have to know what
    Homebrew or scoop are.

    Lookup order, first hit wins:

    1. ``.tools/<os>/scrcpy/scrcpy(.exe)`` — bundled inside the
       customer zip by ``tools/build_release.py``. New customers
       get this for free, no first-click download.
    2. ``~/.npcreate/tools/scrcpy-<version>/.../scrcpy(.exe)``
       — installed by ``scrcpy_installer.install()`` on first
       Mirror click. This is the path existing customers (who got
       NP Create before bundling shipped) end up on.
    3. ``shutil.which("scrcpy")`` — power users on Linux who
       prefer their distro's package over our auto-installer.
    4. Well-known Windows install dirs scoop/choco use even when
       PATH hasn't been refreshed in the current shell.

    Returns ``None`` if absolutely nothing is on disk; the UI then
    pops the auto-install dialog.
    """
    sfx = exe_suffix()
    bundled = _first_existing([f"scrcpy/scrcpy{sfx}", f"scrcpy{sfx}"])
    if bundled:
        return bundled

    # Imported lazily to avoid a circular import — scrcpy_installer
    # imports platform_tools indirectly via the rest of the package
    # tree on some module-load orders.
    try:
        from . import scrcpy_installer
        user_installed = scrcpy_installer.find_user_installed()
        if user_installed is not None:
            return user_installed.resolve()
    except Exception:
        # Never let a bug in the installer-lookup hide a
        # system-installed scrcpy from the rest of the resolver.
        pass

    sys_scrcpy = shutil.which("scrcpy")
    if sys_scrcpy:
        return Path(sys_scrcpy).resolve()

    # Windows-only: scoop/choco install to predictable spots that
    # often aren't on PATH for a fresh GUI launch (PATH is read at
    # explorer-process start time, not at install time). We probe
    # the well-known dirs so customers don't have to log out + back
    # in just to mirror their phone.
    if os.name == "nt":
        for env in ("USERPROFILE", "LOCALAPPDATA", "PROGRAMFILES"):
            base = os.environ.get(env, "")
            if not base:
                continue
            for rel in (
                r"scoop\apps\scrcpy\current\scrcpy.exe",
                r"scoop\shims\scrcpy.exe",
                r"chocolatey\bin\scrcpy.exe",
                r"scrcpy\scrcpy.exe",
            ):
                p = Path(base) / rel
                if p.is_file():
                    return p.resolve()
    return None


def find_vcam_apk() -> Path | None:
    """Return Path to the prebuilt vcam-app APK shipped alongside the
    customer bundle, falling back to the dev `gradlew assembleDebug`
    output.

    Search order matters -- this list MUST stay in sync with what
    ``tools/build_release.py`` writes into the ZIP (see
    ``vcam-app-release.apk`` rename comment in that file). We keep
    ``vcam-app.apk`` in the list as a backstop for the legacy 1.4.5
    bundles already in customer hands; from 1.4.6 onward the
    ``-release`` name is canonical.

    Search base
    -----------
    Just like ``LEGACY_TOOLS_ROOT``, the canonical layout differs
    between launch modes:

    * Dev / portable ZIP — ``apk/`` sits at the workspace root,
      one level above ``PROJECT_ROOT`` (which points at vcam-pc/).
    * PyInstaller frozen — ``apk/`` ships next to NP-Create.exe
      (Inno Setup) or inside the .app bundle (build_dmg.sh), which
      is exactly what ``PROJECT_ROOT`` already resolves to.

    Pre-1.7.9 we used ``PROJECT_ROOT.parent`` unconditionally,
    which silently missed the bundled APK in frozen mode and made
    Patch fail with "vcam-app APK not found" on .exe installs.
    """
    base = _tools_root_base()
    candidates = [
        # 1.4.6+ canonical name (what build_release.py writes)
        base / "apk" / "vcam-app-release.apk",
        base / "apk" / "vcam-app-debug.apk",
        # Legacy 1.4.5 customer bundles -- accepted so updating to
        # 1.4.6 doesn't break customers who copy in just the new
        # src/ folder over an existing extract.
        base / "apk" / "vcam-app.apk",
        # Dev workspace (gradle output) -- used when running from
        # the source tree, not from a customer ZIP.
        base / "vcam-app/app/build/outputs/apk/release/app-release.apk",
        base / "vcam-app/app/build/outputs/apk/debug/app-debug.apk",
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return None


# ── subprocess env ───────────────────────────────────────────────


def make_subprocess_env(extra_path: list[Path] | None = None) -> dict[str, str]:
    """Return an env dict suitable for spawning JDK / adb subprocesses.

    * Prepends ``extra_path`` (e.g. JDK ``bin/`` and ADB folder) onto
      ``PATH`` so child JVMs find their helpers.
    * Forces ``LANG=C`` and ``LC_ALL=C`` to dodge the macOS-Thai
      Buddhist-calendar bug in ``apkzlib`` that mis-parses dates.
    * Sets ``JAVA_TOOL_OPTIONS`` so the JVM also picks an English
      locale even if the user's shell ignores ``LANG``.
    """
    env = os.environ.copy()
    sep = os.pathsep
    if extra_path:
        env["PATH"] = sep.join(str(p) for p in extra_path) + sep + env.get(
            "PATH", ""
        )
    env["LANG"] = "C"
    env["LC_ALL"] = "C"
    env["JAVA_TOOL_OPTIONS"] = (
        env.get("JAVA_TOOL_OPTIONS", "")
        + " -Duser.language=en -Duser.country=US -Duser.timezone=UTC"
    ).strip()
    return env


# ── status object ────────────────────────────────────────────────


@dataclass
class ToolPaths:
    adb: Path | None
    ffmpeg: Path | None
    java: Path | None
    lspatch_jar: Path | None
    vcam_apk: Path | None

    def missing(self) -> list[str]:
        out: list[str] = []
        if self.adb is None:
            out.append("adb")
        if self.ffmpeg is None:
            out.append("ffmpeg")
        if self.java is None:
            out.append("JDK 21 (java)")
        if self.lspatch_jar is None:
            out.append("lspatch.jar")
        if self.vcam_apk is None:
            out.append("vcam-app APK")
        return out

    @property
    def ok(self) -> bool:
        return not self.missing()


def discover() -> ToolPaths:
    return ToolPaths(
        adb=find_adb(),
        ffmpeg=find_ffmpeg(),
        java=find_java(),
        lspatch_jar=find_lspatch_jar(),
        vcam_apk=find_vcam_apk(),
    )


# ── CLI quick check ──────────────────────────────────────────────


def _print_status() -> int:
    p = discover()
    print(f"OS         : {current_os()}")
    print(f"Tools root : {LEGACY_TOOLS_ROOT}")
    rows = [
        ("adb         ", p.adb),
        ("ffmpeg      ", p.ffmpeg),
        ("java (JDK21)", p.java),
        ("lspatch.jar ", p.lspatch_jar),
        ("vcam-app APK", p.vcam_apk),
    ]
    for label, path in rows:
        mark = "✓" if path else "✗"
        print(f"  {mark} {label} : {path or '(not found)'}")
    return 0 if p.ok else 1


if __name__ == "__main__":
    sys.exit(_print_status())
