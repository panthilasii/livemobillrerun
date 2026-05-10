"""Brand identity for NP Create.

Single source of truth for the product name, version, theme colors,
and contact info shown across the UI. Changing values here updates
the title bar, About dialog, support links, license CLI output, and
every auto-generated launcher / README.

Color palette: brand red (#C8102E) on near-black background
(#0F0F14) — matches the company logo (red ↑ growth arrow on
deep-black wordmark) and reads as "premium, broadcast, urgent",
which is what TikTok-Live sellers want.

A lime accent (#A6FF4D) stays reserved for the go-live / record
state so the eye instantly knows when the system is "hot" — bright
lime-green on red-and-black is the highest-contrast combination
available without resorting to plain white.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


@dataclass(frozen=True)
class Brand:
    """Top-level brand metadata."""

    # Product name shown in the title bar, About dialog, and license
    # output. Also used as the customer-facing label everywhere.
    name: str = "NP Create"
    short_name: str = "NP Create"
    company_th: str = "บริษัท เอ็นพี ครีเอ็ท จำกัด"
    company_en: str = "NP Create Co., Ltd."

    version: str = "1.8.1"

    tagline_th: str = "ระบบไลฟ์มือโปร — เสถียร ใช้ง่าย"
    tagline_en: str = "Online Advertising & Digital Marketing"

    line_oa: str = "@npcreate"
    contact_url: str = "https://line.me/R/ti/p/@npcreate"
    support_hours: str = "9:00–22:00 น. ทุกวัน"

    # License key prefix. Kept at "888" because Thai customers
    # associate the triple-eight with luck/value, and switching
    # the prefix would invalidate every key already shipped.
    license_prefix: str = "888"

    # Default subscription tier: 1 license key may activate this many
    # phones. Tweak here and every CLI/UI default updates in lock-step.
    default_devices_per_key: int = 3
    default_license_days: int = 30

    # URL of the central admin server (`vcam-server/`) that handles
    # phone-home activations + revocation broadcasts. Empty string =
    # offline-only mode (the customer app falls back to pure local
    # license verification, exactly like before the server existed).
    # Once we deploy the server publicly, change this to e.g.
    # ``"https://admin.np-create.com"`` and rebuild the customer
    # bundle.
    license_server_url: str = ""

    # Asset locations. Resolved as absolute paths so callers can pass
    # them directly to PhotoImage / iconbitmap regardless of cwd.
    logo_path: Path = ASSETS_DIR / "logo.png"
    logo_256_path: Path = ASSETS_DIR / "logo_256.png"
    logo_128_path: Path = ASSETS_DIR / "logo_128.png"
    logo_64_path: Path = ASSETS_DIR / "logo_64.png"
    icon_ico_path: Path = ASSETS_DIR / "logo.ico"
    icon_icns_path: Path = ASSETS_DIR / "logo.icns"


@dataclass(frozen=True)
class Theme:
    """Color tokens for the dark UI."""

    bg_main: str = "#0F0F14"
    bg_sidebar: str = "#15151C"
    bg_card: str = "#1C1C26"
    bg_input: str = "#22222E"
    bg_hover: str = "#2A2A38"

    fg_primary: str = "#FFFFFF"
    fg_secondary: str = "#B8B8C8"
    fg_muted: str = "#7A7A88"

    # Brand red — sampled from the NP Create logo and tuned for
    # screen contrast against #0F0F14 (passes WCAG AA for body text).
    primary: str = "#C8102E"
    primary_hover: str = "#E11D2E"
    primary_dim: str = "#8A0916"

    success: str = "#A6FF4D"
    warning: str = "#FFB84D"
    danger: str = "#FF5C5C"

    online_dot: str = "#A6FF4D"
    offline_dot: str = "#7A7A88"

    border: str = "#2A2A38"
    divider: str = "#22222E"


BRAND = Brand()
THEME = Theme()
