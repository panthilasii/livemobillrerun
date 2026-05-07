"""Brand identity for Live Studio Pro.

Single source of truth for the product name, version, theme colors,
and contact info shown across the UI. Changing values here updates
the title bar, About dialog, support links, etc.

Color palette: deep purple primary (#6B47DC) on near-black background
(#0F0F14) — premium, subscription-friendly, distinct from generic
TikTok blue. Uses a lime accent (#A6FF4D) only for go-live / record
states so the eye instantly knows when the system is "hot".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Brand:
    """Top-level brand metadata."""

    name: str = "Live Studio Pro"
    short_name: str = "Live Studio"
    version: str = "1.0.0-beta"
    tagline_th: str = "ระบบไลฟ์มือโปร — เสถียร ใช้ง่าย"
    tagline_en: str = "Pro live-streaming made simple"

    line_oa: str = "@livestudio"
    contact_url: str = "https://line.me/R/ti/p/@livestudio"
    support_hours: str = "9:00–22:00 น. ทุกวัน"

    license_prefix: str = "888"


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

    primary: str = "#6B47DC"
    primary_hover: str = "#7B5AE8"
    primary_dim: str = "#4A2FA0"

    success: str = "#A6FF4D"
    warning: str = "#FFB84D"
    danger: str = "#FF5C5C"

    online_dot: str = "#A6FF4D"
    offline_dot: str = "#7A7A88"

    border: str = "#2A2A38"
    divider: str = "#22222E"


BRAND = Brand()
THEME = Theme()
