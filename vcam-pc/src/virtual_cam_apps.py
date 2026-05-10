"""Catalogue of Android Virtual-Camera apps for v1.8.0's Mode B.

What a "virtual camera app" is here
-----------------------------------

Mode B's no-USB pipeline relies on a third-party Android app
that pulls an RTMP stream and exposes itself to the OS as a
camera (Camera2 API or v4l2 loopback). When TikTok asks "which
camera do you want?", the user picks the app's name and TikTok
ends up streaming our PC-side video — without us ever touching
the TikTok APK.

Three apps cover ~95 % of the Thai TikTok-Live seller market we
target. The metadata below drives:

* The "ลง app บนมือถือ" wizard step — Play Store link and
  QR code that the customer scans on the phone.
* The "หา app" check after the customer says "ลงเสร็จแล้ว" —
  ``adb shell pm list packages <pkg>`` (still works over
  Wireless ADB / Mode C; if Mode B is fully WiFi-only we
  rely on the customer's tap to confirm).
* The Thai-language setup walk-through baked into each app
  entry. UltimateRerun4.9's strings were a useful reference;
  ours are reworded so they're idiomatic NP-Create voice and
  match our wizard's tone.

Adding a new app
----------------

Add a ``VirtualCamApp`` instance to ``CATALOG``. ``key`` is the
ID we persist in customer config (so don't rename without
migrating). ``rtmp_input_path`` is the menu path the customer
follows in the app — keep it short enough to fit one line in
the wizard's instruction card.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VirtualCamApp:
    """Static description of one Android virtual-camera app."""

    key: str
    """Stable ID for config / analytics. Lower-case ASCII."""

    name: str
    """Display name shown in the wizard list. Match Play Store
    title so customers recognise it."""

    package: str
    """Android package — used for ``pm list packages`` checks."""

    playstore_url: str
    """``https://play.google.com/store/...`` URL. We render this
    as a QR on the wizard so the customer scans it on the phone
    and lands directly on the install page."""

    description_th: str
    """One-line Thai blurb under the app's name."""

    setup_steps_th: list[str]
    """Numbered Thai walk-through. Step 1 is always "เปิดแอป",
    last step is always "เปิด TikTok → Live → เลือกกล้อง = ..."."""

    rating: int
    """1–5 stars used to sort the picker. Higher = more reliable
    based on our own field testing in BKK live-seller community."""

    notes_th: str = ""
    """Free-form caveat text (subscription tiers, version
    requirements, known bugs). Empty = no caveat."""

    free: bool = True
    """True if the customer can use it without paying. False = the
    wizard adds a "ต้องสมัคร Pro" warning."""


# ── catalogue ────────────────────────────────────────────────


CATALOG: list[VirtualCamApp] = [
    VirtualCamApp(
        key="camerafi",
        name="CameraFi Studio",
        package="com.vaultmicro.camerafi.live",
        playstore_url=(
            "https://play.google.com/store/apps/details"
            "?id=com.vaultmicro.camerafi.live"
        ),
        description_th="แนะนำที่สุด — ฟรี, รองรับ RTMP input + Virtual Camera",
        setup_steps_th=[
            "เปิด CameraFi Studio บนมือถือ",
            "กด Source → Add Source → RTMP",
            "วาง URL ที่แสดงด้านขวา (rtmp://...)",
            "กด Start Virtual Camera (จะมี notification ขึ้น)",
            "เปิด TikTok → Live → เลือกกล้อง = CameraFi",
        ],
        rating=5,
        notes_th=(
            "เวอร์ชั่นฟรีให้ใช้ได้ครบ; เวอร์ชั่น Pro แค่ปลด "
            "watermark + 4K (สำหรับขายของไม่จำเป็น)"
        ),
        free=True,
    ),
    VirtualCamApp(
        key="larix",
        name="Larix Broadcaster",
        package="com.wmspanel.larix_broadcaster",
        playstore_url=(
            "https://play.google.com/store/apps/details"
            "?id=com.wmspanel.larix_broadcaster"
        ),
        description_th="ฟรี, มาตรฐานวงการ broadcast — เสถียรกับ network กระตุก",
        setup_steps_th=[
            "เปิด Larix Broadcaster บนมือถือ",
            "Settings → Connections → New connection",
            "Mode = Input · URL = rtmp://... (ตามด้านขวา)",
            "กลับมาหน้าหลัก → กดถ่ายเริ่ม",
            "เปิด TikTok → Live → เลือกกล้อง = Larix Virtual",
        ],
        rating=4,
        notes_th=(
            "Larix รุ่น Pro ($4.99) ปลด HEVC encode + adaptive "
            "bitrate; รุ่นฟรีพอสำหรับไลฟ์ขายของส่วนใหญ่"
        ),
        free=True,
    ),
    VirtualCamApp(
        key="du_recorder",
        name="DU Recorder",
        package="com.duapps.recorder",
        playstore_url=(
            "https://play.google.com/store/apps/details"
            "?id=com.duapps.recorder"
        ),
        description_th="ฟรี — ใช้ง่ายสุดสำหรับมือใหม่",
        setup_steps_th=[
            "เปิด DU Recorder บนมือถือ",
            "กดเมนู (☰) → เลือก Virtual Camera",
            "กด Enable → ใส่ RTMP URL ที่แสดงด้านขวา",
            "จะมี notification 'Virtual Camera Active'",
            "เปิด TikTok → Live → เลือกกล้อง = DU Recorder",
        ],
        rating=3,
        notes_th=(
            "DU Recorder ในบาง Play Store region ถูกถอดออก; "
            "ถ้าไม่เจอใน Play Store ใช้ตัวอื่นแทน"
        ),
        free=True,
    ),
]


def by_key(key: str) -> VirtualCamApp | None:
    for a in CATALOG:
        if a.key == key:
            return a
    return None


def recommended() -> VirtualCamApp:
    """Return the highest-rated app — what the wizard picks
    by default if the customer doesn't choose explicitly."""
    return max(CATALOG, key=lambda a: a.rating)
