"""What to draw on one poster, built from the item's overlay data and the style. Same rules as the plugin."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..media.classify import BASIC_AUDIO
from ..settings import Style

RGBA = Tuple[int, int, int, int]

LABELS = {
    "res_4k": "4K", "res_1080p": "1080p", "res_720p": "720p", "res_576p": "576p", "res_480p": "480p",
    "hdr_dv": "DV", "hdr_hdr10plus": "HDR10+", "hdr_hdr10": "HDR10", "hdr_hlg": "HLG",
    "audio_truehd_atmos": "TrueHD Atmos", "audio_plus_atmos": "DD+ Atmos", "audio_atmos": "Atmos",
    "audio_dtsx": "DTS:X", "audio_dtshdma": "DTS-HD MA", "audio_truehd": "TrueHD",
    "audio_ddplus": "DD+", "audio_dts": "DTS", "audio_dd": "DD",
}


def color(hex_value: str, alpha: int = 255) -> RGBA:
    h = (hex_value or "#000000").lstrip("#")
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha
    except ValueError:
        return 0, 0, 0, alpha


@dataclass
class Badge:
    icon: Optional[str]
    label: str
    value: Optional[str] = None
    label_color: Optional[RGBA] = None


@dataclass
class Spec:
    banner_text: Optional[str] = None
    banner_color: RGBA = (0, 0, 0, 255)
    banner_text_color: RGBA = (255, 255, 255, 255)
    banner_top: bool = True
    banner_height: float = 0.06
    banner_tracking: float = 0.02
    badges: List[Badge] = field(default_factory=list)
    badge_height: float = 0.06
    icon_scale: float = 1.0
    badge_margin: float = 0.02
    max_badges: int = 6
    badge_bg: RGBA = (0, 0, 0, 102)
    use_icons: bool = True
    column: bool = True
    wide_font: bool = True
    badge_position: str = "TopLeft"

    @property
    def empty(self) -> bool:
        return not self.banner_text and not self.badges

    def content_key(self) -> str:
        parts = [self.banner_text or "-"] + [f"{b.icon or b.label}:{b.value or ''}" for b in self.badges]
        return "|".join(parts)


def style_hash(style: Style, icon_generation: str = "") -> str:
    data = style.model_dump(mode="json")
    for k in ("movies", "series", "seasons", "quality_rule", "airing_window_days"):
        data.pop(k, None)
    raw = json.dumps(data, sort_keys=True) + "|" + icon_generation
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def build(data: Optional[dict], kind: str, s: Style) -> Spec:
    """data: {"status", "resolution", "hdr", "audio", "imdb", "rt_critic", "rt_audience", "tmdb"}."""
    spec = Spec(
        banner_top=s.banner_position.value == "Top",
        banner_height=s.banner_height / 100,
        banner_tracking=s.banner_letter_spacing / 100,
        badge_height=s.badge_height / 100,
        icon_scale=s.badge_icon / 100,
        badge_margin=s.badge_margin / 100,
        max_badges=s.max_badges,
        badge_bg=color(s.badge_bg_color, round(255 * s.badge_bg_opacity / 100)),
        banner_text_color=color(s.banner_text_color),
        use_icons=s.use_icons,
        column=s.badge_style.value == "Column",
        wide_font=s.text_font.value == "Regular",
        badge_position=s.badge_position.value,
    )
    if not data:
        return spec

    if kind != "movie" and s.banner:
        status = data.get("status") or ""
        text, col = {
            "Airing": (s.airing_text, s.airing_color),
            "Returning": (s.returning_text, s.returning_color),
            "Ended": (s.ended_text, s.ended_color),
            "Canceled": (s.canceled_text, s.canceled_color),
            "Upcoming": (s.upcoming_text, s.upcoming_color) if s.show_upcoming else (None, None),
        }.get(status, (None, None))
        if text and text.strip():
            spec.banner_text = text.strip()
            spec.banner_color = color(col, round(255 * s.banner_opacity / 100))

    show_ratings = kind != "season" or s.show_ratings_on_seasons
    show_quality = kind == "movie" or s.show_quality_on_shows
    pct = "" if spec.column else "%"
    for key in s.badge_order:
        b = None
        if key == "resolution" and show_quality and s.show_resolution and data.get("resolution"):
            b = Badge(data["resolution"], LABELS.get(data["resolution"], data["resolution"]))
        elif key == "hdr" and show_quality and s.show_hdr and data.get("hdr"):
            b = Badge(data["hdr"], LABELS.get(data["hdr"], data["hdr"]))
        elif key == "audio" and show_quality and s.show_audio and data.get("audio") and (
                s.show_basic_audio or data["audio"] not in BASIC_AUDIO):
            b = Badge(data["audio"], LABELS.get(data["audio"], data["audio"]))
        elif key == "imdb" and show_ratings and s.show_imdb and data.get("imdb"):
            b = Badge("imdb", "IMDb", f"{data['imdb']:.1f}", (245, 197, 24, 255))
        elif key == "tmdb" and show_ratings and s.show_tmdb and data.get("tmdb"):
            b = Badge("tmdb", "TMDB", f"{data['tmdb']:.1f}", (1, 180, 228, 255))
        elif key == "rtcritic" and show_ratings and s.show_rt_critic and data.get("rt_critic") is not None:
            fresh = data["rt_critic"] >= 60
            b = Badge("rt_critic_fresh" if fresh else "rt_critic_rotten", "RT", f"{data['rt_critic']}{pct}",
                      (250, 50, 10, 255) if fresh else (141, 184, 58, 255))
        elif key == "rtaudience" and show_ratings and s.show_rt_audience and data.get("rt_audience") is not None:
            fresh = data["rt_audience"] >= 60
            b = Badge("rt_audience_fresh" if fresh else "rt_audience_rotten", "AUD", f"{data['rt_audience']}{pct}",
                      (250, 50, 10, 255) if fresh else (141, 184, 58, 255))
        if b and len(spec.badges) < s.max_badges:
            spec.badges.append(b)
    return spec
