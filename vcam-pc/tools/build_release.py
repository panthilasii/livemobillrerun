#!/usr/bin/env python3
"""Live Studio Pro — release bundler.

Produces ZIP archives for shipping to customers (or as an admin
backup) targeting macOS or Windows. The same source tree is used
for both audiences; the *only* differences are which auxiliary
files we copy in:

==================  ==========  ==========
File                Customer    Admin
==================  ==========  ==========
src/                yes         yes
src/_pubkey.py      yes         yes
src/_ed25519.py     yes         yes
.private_key        **NEVER**   yes
tools/init_keys.py  **NEVER**   yes
tools/gen_license.py **NEVER**  yes
tests/              no          yes
.tools/<os>/        yes         yes
apk/                yes         yes
run.bat / .command  yes         yes
README              yes         admin variant
==================  ==========  ==========

Usage::

    # Build a Windows zip for customers (default target)
    python tools/build_release.py --target customer --os windows

    # Build a macOS admin bundle
    python tools/build_release.py --target admin --os macos

    # Build all four combinations
    python tools/build_release.py --all

Exit codes: 0 success, 2 bad args, 3 prerequisite missing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import textwrap
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent          # vcam-pc/
WORKSPACE = PROJECT.parent     # repo root

sys.path.insert(0, str(PROJECT))

from src.branding import BRAND  # noqa: E402

# ── what to ship ─────────────────────────────────────────────────

# Files inside vcam-pc/src/ never shipped — none currently. Listed
# here for completeness so future "secret" modules are caught.
SRC_BLOCKLIST: set[str] = set()

# Tool scripts we ship to customers (verify-only).
CUSTOMER_TOOLS: set[str] = set()

# Admin-only tools (signing, key rotation, packaging).
ADMIN_TOOLS: set[str] = {
    "gen_license.py",
    "init_keys.py",
    "build_release.py",
    "setup_windows_tools.py",
}

# Top-level project files always shipped.
ALWAYS_SHIP_TOP = (
    "config.json",
    "device_profiles.json",
    "requirements.txt",
    "README.md",
)

# Customer-facing run launcher names. We auto-generate them so the
# ship payload doesn't depend on extra repo files.
LAUNCHER_NAMES = {
    "windows": "run.bat",
    "macos": "run.command",
    "linux": "run.sh",
}


# ── apk + tools sourcing ─────────────────────────────────────────


def find_vcam_apk() -> Path | None:
    """Locate the vcam-app APK to bundle. Prefer release > debug."""
    cands = [
        WORKSPACE / "apk" / "vcam-app-release.apk",
        WORKSPACE / "apk" / "vcam-app-debug.apk",
        WORKSPACE / "vcam-app/app/build/outputs/apk/release/app-release.apk",
        WORKSPACE / "vcam-app/app/build/outputs/apk/debug/app-debug.apk",
    ]
    for c in cands:
        if c.is_file():
            return c
    return None


def find_tools_dir(os_name: str) -> Path | None:
    """Find the .tools/<os>/ payload, falling back to the legacy
    macOS-only flat layout if user hasn't reorganised yet."""
    per_os = WORKSPACE / ".tools" / os_name
    if per_os.is_dir():
        return per_os
    if os_name == "macos" and (WORKSPACE / ".tools").is_dir():
        return WORKSPACE / ".tools"
    return None


# Subset of the tools directory we actually ship. Anything outside
# these paths is build-time only (NDK, cmake, gradle, build-tools)
# and would balloon the customer ZIP from ~400 MB to >2 GB without
# adding any runtime value.
SHIP_TOOLS_PATTERNS = (
    "lspatch/",                         # lspatch.jar
    "jdk-21/",                          # JDK 21 (~330 MB)
    "platform-tools/",                  # adb + bundled libs (~38 MB)
    "android-sdk/platform-tools/",      # legacy macOS layout
)


def _under_pattern(rel: str, patterns: tuple[str, ...]) -> bool:
    rel_posix = rel.replace(os.sep, "/")
    return any(rel_posix.startswith(p) for p in patterns)


# ── zip writer ───────────────────────────────────────────────────


def _walk_filtered(root: Path, skip_names: set[str]) -> list[Path]:
    """Iterate over files under ``root`` skipping any whose path
    contains one of ``skip_names`` (exact directory or file name).
    """
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(part in skip_names for part in p.parts):
            continue
        out.append(p)
    return out


# Names dropped from any ship — junk from tooling.
_SHIP_SKIP_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".DS_Store",
    "Thumbs.db",
    ".gradle",
    ".idea",
    "build",     # gradle/pyinstaller artefacts inside subprojects
    "dist",
    "videos",    # user's working clips, not ours to ship
    "logs",
    "cache",     # vcam-pc/cache/ — runtime
    ".cache",
}


def add_dir(zf: zipfile.ZipFile, src_dir: Path, arc_dir: str,
            extra_skip: set[str] = frozenset()) -> int:
    n = 0
    for f in _walk_filtered(src_dir, _SHIP_SKIP_NAMES | set(extra_skip)):
        rel = f.relative_to(src_dir)
        zf.write(f, f"{arc_dir}/{rel.as_posix()}")
        n += 1
    return n


def add_file(zf: zipfile.ZipFile, src: Path, arc: str) -> None:
    zf.write(src, arc)


# ── launcher generation ──────────────────────────────────────────


def _launcher_body(os_name: str) -> str:
    if os_name == "windows":
        return textwrap.dedent("""\
            @echo off
            REM Live Studio Pro — Windows launcher
            cd /d "%~dp0\\vcam-pc"
            where py >nul 2>&1
            if %ERRORLEVEL%==0 (
                py -3 -m pip install --quiet --user -r requirements.txt
                py -3 -m src.main --studio
            ) else (
                where python >nul 2>&1
                if %ERRORLEVEL%==0 (
                    python -m pip install --quiet --user -r requirements.txt
                    python -m src.main --studio
                ) else (
                    echo [!] Python 3 not found.
                    echo     Install Python 3.13 from https://python.org/downloads
                    pause
                )
            )
            pause
            """)
    if os_name == "macos":
        return textwrap.dedent("""\
            #!/usr/bin/env bash
            # Live Studio Pro — macOS launcher
            set -e
            cd "$(dirname "$0")/vcam-pc"
            if ! command -v python3 >/dev/null 2>&1; then
              osascript -e 'display dialog "ต้องติดตั้ง Python 3 ก่อนใช้งาน\\nไปที่ https://python.org/downloads แล้วลงเวอร์ชัน 3.13"'
              exit 1
            fi
            python3 -m pip install --quiet --user -r requirements.txt
            exec python3 -m src.main --studio
            """)
    # linux
    return textwrap.dedent("""\
        #!/usr/bin/env bash
        set -e
        cd "$(dirname "$0")/vcam-pc"
        python3 -m pip install --quiet --user -r requirements.txt
        exec python3 -m src.main --studio
        """)


# ── README generation ────────────────────────────────────────────


def _readme(target: str, os_name: str) -> str:
    is_admin = target == "admin"
    title = (
        f"{BRAND.name} — Admin Bundle"
        if is_admin
        else f"{BRAND.name} — Customer Bundle"
    )
    sec_admin = textwrap.dedent("""\
        ## ADMIN ONLY — สำหรับเจ้าของระบบเท่านั้น

        ห้ามแชร์ไฟล์ `.private_key` หรือ `tools/gen_license.py` ให้ใครเด็ดขาด
        - `.private_key` คือกุญแจส่วนตัวที่ใช้เซ็น license key ทุกใบที่ขายไป
        - หากหลุด ต้อง rotate กุญแจ (`python tools/init_keys.py --force`)
          ซึ่งจะทำให้ license key ทุกใบที่ขายไปแล้ว ใช้ไม่ได้ทันที

        ### ออกคีย์ให้ลูกค้า

        ```
        python tools/gen_license.py -c "ชื่อลูกค้า"            # ดีฟอลต์ 3 เครื่อง / 30 วัน
        python tools/gen_license.py -c "VIP" -n 5 -d 365      # 5 เครื่อง / 1 ปี
        ```

        ### Build customer bundle

        ```
        python tools/build_release.py --target customer --os windows
        python tools/build_release.py --target customer --os macos
        ```
        ผลลัพธ์อยู่ใน `dist/`
        """)
    sec_install = textwrap.dedent(f"""\
        ## วิธีติดตั้ง

        1. แตก zip ไฟล์นี้ลงเครื่อง (เก็บไว้ที่ Desktop ก็ได้)
        2. {'เปิด `run.bat` (ดับเบิ้ลคลิก)' if os_name == 'windows' else 'ดับเบิ้ลคลิก `run.command`'}
        3. ใส่ License Key ที่ได้รับจากผู้ขาย
        4. เชื่อมโทรศัพท์ Android ผ่าน USB (เปิด USB Debugging)
        5. ทำตามขั้นตอนใน "เพิ่มเครื่องใหม่"

        ## ปัญหาที่อาจเจอ

        - **Python ยังไม่ได้ลง** — ระบบจะแจ้งเตือน ให้ไปที่ https://python.org/downloads
          แล้วลงเวอร์ชัน 3.13 (Windows: ติ๊ก "Add Python to PATH")
        - **adb ไม่เจอเครื่อง** — เปิด "USB Debugging" ในมือถือก่อน + ตอบ "Allow"
          เมื่อมือถือถามขออนุญาต
        - **ติดต่อแอดมิน** — Line: {BRAND.line_oa} ({BRAND.support_hours})
        """)
    sections = [
        f"# {title}\n\nเวอร์ชัน {BRAND.version}\n",
        sec_install,
    ]
    if is_admin:
        sections.append(sec_admin)
    return "\n".join(sections)


# ── build entrypoint ─────────────────────────────────────────────


def build_one(target: str, os_name: str, dist: Path) -> Path:
    if target not in ("customer", "admin"):
        raise ValueError(f"unknown target: {target}")
    if os_name not in ("windows", "macos", "linux"):
        raise ValueError(f"unknown os: {os_name}")

    # Required inputs.
    apk = find_vcam_apk()
    if not apk:
        raise SystemExit(
            "[!] vcam-app APK not found. Build it first:\n"
            "      cd vcam-app && ./gradlew assembleDebug"
        )
    tools_dir = find_tools_dir(os_name)
    if not tools_dir and os_name != "linux":
        print(
            f"[!] No bundled tools found for {os_name} at "
            f".tools/{os_name}/. The zip will lack adb/ffmpeg/JDK; "
            f"customer must install them manually.",
            file=sys.stderr,
        )

    # Public key is mandatory — that's what verifies licenses.
    pubkey = PROJECT / "src" / "_pubkey.py"
    if not pubkey.is_file():
        raise SystemExit(
            "[!] Public key missing. Run `python tools/init_keys.py` "
            "once on the admin machine first."
        )

    bundle_name = (
        f"{BRAND.short_name.replace(' ', '-')}-"
        f"{target}-{os_name}-{BRAND.version}"
    )
    out_zip = dist / f"{bundle_name}.zip"
    dist.mkdir(parents=True, exist_ok=True)

    # Determine which tools-subdir files are admin-only.
    tools_skip = set() if target == "admin" else ADMIN_TOOLS

    print(f"\n→ {target.upper()} bundle for {os_name}")
    print(f"   archive : {out_zip}")
    if tools_dir:
        print(f"   tools/  : {tools_dir}")
    print(f"   apk     : {apk}")

    with zipfile.ZipFile(
        out_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True
    ) as zf:
        prefix = bundle_name

        # ── vcam-pc/src/ ─────────────────────────────────────────
        n_src = 0
        for f in _walk_filtered(PROJECT / "src", _SHIP_SKIP_NAMES):
            rel = f.relative_to(PROJECT / "src")
            if rel.name in SRC_BLOCKLIST:
                continue
            zf.write(f, f"{prefix}/vcam-pc/src/{rel.as_posix()}")
            n_src += 1
        print(f"   src/    : {n_src} files")

        # ── vcam-pc/tools/ ───────────────────────────────────────
        n_tools = 0
        for f in (PROJECT / "tools").glob("*"):
            if not f.is_file():
                continue
            if f.name in tools_skip:
                continue
            zf.write(f, f"{prefix}/vcam-pc/tools/{f.name}")
            n_tools += 1
        print(f"   tools/  : {n_tools} files (skipped {sorted(tools_skip)})")

        # ── vcam-pc top-level ───────────────────────────────────
        for fname in ALWAYS_SHIP_TOP:
            f = PROJECT / fname
            if f.is_file():
                zf.write(f, f"{prefix}/vcam-pc/{fname}")

        # ── tests/ (admin only) ─────────────────────────────────
        if target == "admin":
            n_tests = add_dir(
                zf, PROJECT / "tests", f"{prefix}/vcam-pc/tests"
            )
            print(f"   tests/  : {n_tests} files")

        # ── private_key (admin only) ────────────────────────────
        priv = PROJECT / ".private_key"
        if target == "admin" and priv.is_file():
            zf.write(priv, f"{prefix}/vcam-pc/.private_key")
            print("   .private_key : included (ADMIN ONLY)")
        elif target == "customer" and priv.is_file():
            print("   .private_key : EXCLUDED (customer-safe)")

        # ── apk ─────────────────────────────────────────────────
        zf.write(apk, f"{prefix}/apk/vcam-app.apk")

        # ── tools (.tools/<os>/) ────────────────────────────────
        if tools_dir:
            n_t = 0
            for f in _walk_filtered(tools_dir, _SHIP_SKIP_NAMES):
                rel = f.relative_to(tools_dir).as_posix()
                if not _under_pattern(rel, SHIP_TOOLS_PATTERNS):
                    continue
                # Normalise legacy android-sdk/platform-tools/ →
                # platform-tools/ so the customer ship has a flat,
                # predictable layout regardless of where dev built it.
                if rel.startswith("android-sdk/"):
                    rel = rel[len("android-sdk/"):]
                zf.write(f, f"{prefix}/.tools/{os_name}/{rel}")
                n_t += 1
            print(f"   .tools/ : {n_t} files (only adb + JDK + lspatch)")

        # ── launcher + README ───────────────────────────────────
        launcher = LAUNCHER_NAMES[os_name]
        body = _launcher_body(os_name)
        info = zipfile.ZipInfo(f"{prefix}/{launcher}")
        # 0o100755 = regular file with -rwxr-xr-x. zip stores Unix
        # mode in the high 16 bits of external_attr; Finder + Linux
        # honour this, so the customer can double-click without
        # `chmod +x` first. (Windows ignores the bit harmlessly.)
        info.external_attr = (0o100755 << 16) if os_name != "windows" else (0o100644 << 16)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, body)
        zf.writestr(f"{prefix}/README_TH.md", _readme(target, os_name))

    size_mb = out_zip.stat().st_size / 1024 / 1024
    print(f"   ✓ wrote {size_mb:,.1f} MB")
    return out_zip


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--target", choices=("customer", "admin"), default="customer",
    )
    p.add_argument(
        "--os", dest="os_name",
        choices=("windows", "macos", "linux"),
        default="windows",
    )
    p.add_argument("--all", action="store_true",
                   help="build all four combinations")
    p.add_argument(
        "--dist", default=str(WORKSPACE / "dist"),
        help="output directory (default: <workspace>/dist)",
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    dist = Path(args.dist).resolve()

    if args.all:
        outs = []
        for tgt in ("customer", "admin"):
            for osn in ("windows", "macos"):
                outs.append(build_one(tgt, osn, dist))
        print("\nBuilt:")
        for p2 in outs:
            print(f"  • {p2}")
        return 0

    out = build_one(args.target, args.os_name, dist)
    print(f"\nDone: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
