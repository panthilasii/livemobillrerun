"""Tiny in-process translator for the vcam-pc Tkinter GUI.

Why a translator and not gettext / .po files? The whole app is one
window with ~80 user-visible strings. A flat dict per language keeps
the build simple, plays nicely with the GUI's static layout, and lets
us hot-swap languages from a Settings menu later without rebuilding.

Usage:

    from .i18n import T
    tk.Button(text=T("Start"))

If a string is missing from the active locale's table, the original
English source is returned unchanged (so a partial translation never
leaves the GUI blank).
"""

from __future__ import annotations

import os
from typing import Final


# Default language. Override with the VCAM_LANG env var or the
# `set_language` function below. Currently supported: "th", "en".
_DEFAULT_LANG: Final[str] = os.environ.get("VCAM_LANG", "th")


# ── Thai translations ──────────────────────────────────────────────
#
# Keys are the original English source strings. Values are the Thai
# replacements. New strings can be added at any time.
_TH: dict[str, str] = {
    # Window title
    "livemobillrerun — vcam-pc":
        "livemobillrerun — กล้องเสมือน",

    # Section headers
    "1. Device profile": "1. โปรไฟล์อุปกรณ์",
    "2. Playlist + ADB": "2. เพลย์ลิสต์ + ADB",
    "3. Stream settings": "3. ตั้งค่าการสตรีม",
    "4. Control": "4. ควบคุม",
    "5. Live Mode (TikTok Screen Share)": "5. โหมดไลฟ์ (TikTok Screen Share)",
    "6. Hook Mode": "6. โหมด Hook",
    "7. LSPatch — embed CameraHook into TikTok (no root)":
        "7. LSPatch — ฝัง CameraHook ลง TikTok (ไม่ต้อง root)",

    # Inline labels & buttons
    "refresh": "รีเฟรช",
    "Refresh": "รีเฟรช",
    "Refresh status": "รีเฟรชสถานะ",
    "rescan": "สแกนใหม่",
    "Open folder": "เปิดโฟลเดอร์",
    "Add videos...": "เพิ่มวีดีโอ...",
    "Delete selected": "ลบที่เลือก",
    "Select all": "เลือกทั้งหมด",
    "adb:": "adb:",
    "apk:": "apk:",
    "Install vcam app on phone": "ติดตั้งแอป vcam ลงโทรศัพท์",
    "status:": "สถานะ:",
    "Start streamer + phone": "เริ่มสตรีม + โทรศัพท์",
    "Stop": "หยุด",
    "Open app on phone": "เปิดแอปบนโทรศัพท์",
    "Go Live on TikTok": "ขึ้นไลฟ์บน TikTok",
    "Encode + push MP4": "Encode + push MP4",
    "Encoding…": "กำลัง Encode…",
    "Pushing…": "กำลัง push…",
    "Activate hook": "เปิด Hook",
    "Deactivate hook": "ปิด Hook",
    "Patch & install TikTok": "Patch + ติดตั้ง TikTok",
    "Working…": "กำลังทำงาน…",

    # Live stream toggle (Section 6)
    "Use LIVE stream": "ใช้สตรีมสด",
    "Use MP4 loop": "ใช้ MP4 วนลูป",
    "Live-stream mode: OFF (using MP4 loop)":
        "โหมดสตรีมสด: ปิด (เล่น MP4 วนลูป)",
    "Live-stream mode: ON (PC → phone over TCP)":
        "โหมดสตรีมสด: เปิด (PC → โทรศัพท์ ผ่าน TCP)",
    "Start the streamer first (Section 4 → Start) so port 8888 has data to send.":
        "เริ่มสตรีมเมอร์ก่อน (Section 4 → เริ่ม) เพื่อให้พอร์ต 8888 มีข้อมูลส่ง",

    # Status strings
    "idle": "รอ",
    "phone yuv: —": "โทรศัพท์ yuv: —",
    "hook file: —": "ไฟล์ hook: —",
    "enabled flag: —": "ธงเปิดใช้: —",
    "TikTok: —": "TikTok: —",
    "patched: —": "patched: —",

    # Tooltips / help text
    "Tip: Cmd-click or Shift-click to select multiple files":
        "เคล็ด: Cmd-คลิก หรือ Shift-คลิก เพื่อเลือกหลายไฟล์",

    # Phase 4c/4d descriptions
    (
        "Encodes the playlist into a single TikTok-friendly MP4 and "
        "pushes it to /sdcard/vcam_final.mp4 on the phone. The "
        "CameraHook embedded in TikTok (see Section 7) picks the file "
        "up and feeds it to TikTok's encoder in place of the camera. "
        "Works on stock locked phones — no root, no Mi Unlock."
    ): (
        "Encode เพลย์ลิสต์เป็น MP4 ที่ TikTok เล่นได้แล้ว push ไปที่ "
        "/sdcard/Android/data/com.ss.android.ugc.trill/files/ บน "
        "โทรศัพท์. CameraHook ที่ฝังใน TikTok (ดู section 7) จะ "
        "หยิบไฟล์ไปใส่ encoder แทนกล้อง. ใช้ได้กับเครื่องที่ "
        "บูตล็อค ไม่ต้อง root ไม่ต้อง Mi Unlock."
    ),
    (
        "Pulls the user's installed TikTok APKs over ADB, runs LSPatch "
        "to embed vcam-app as an Xposed module, then re-installs the "
        "patched bundle. After this, TikTok itself loads the hook on "
        "every launch — no Magisk, no LSPosed, no bootloader unlock. "
        "Requires only USB Debugging + 'Install via USB'. The user "
        "will be logged out of TikTok (signature changes)."
    ): (
        "ดึง TikTok APK ทั้งหมดผ่าน ADB, รัน LSPatch ฝัง vcam-app "
        "เป็นโมดูล Xposed, แล้วติดตั้ง bundle ที่ patch แล้วใหม่. "
        "หลังจากนี้ TikTok จะโหลด hook เองทุกครั้งที่เปิด — ไม่ต้อง "
        "Magisk, ไม่ต้อง LSPosed, ไม่ต้องปลดล็อค bootloader. "
        "ใช้แค่ USB Debugging + 'Install via USB'. คุณจะออกจาก "
        "ระบบ TikTok เพราะลายเซ็นเปลี่ยน."
    ),
}


_EN: dict[str, str] = {}  # English just returns identity


_TABLES: dict[str, dict[str, str]] = {
    "th": _TH,
    "en": _EN,
}

_active_lang: str = _DEFAULT_LANG


def set_language(code: str) -> None:
    """Switch the active locale. ``code`` is either ``"th"`` or ``"en"``."""
    global _active_lang
    if code in _TABLES:
        _active_lang = code


def language() -> str:
    return _active_lang


def T(s: str) -> str:
    """Translate ``s`` from English to the active locale.

    If the active locale's table doesn't contain ``s``, return ``s``
    unchanged so the GUI never shows an empty label.
    """
    return _TABLES.get(_active_lang, {}).get(s, s)
