"""All user settings, stored as one JSON file in the config folder."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")

_HEX = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _color(value: str, default: str) -> str:
    v = (value or "").strip()
    if not _HEX.match(v):
        return default
    return ("#" + v.lstrip("#")).upper()


class ServerKind(str, Enum):
    emby = "emby"
    plex = "plex"


class ServerConfig(BaseModel):
    """One Emby or Plex server."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    kind: ServerKind
    name: str = ""
    url: str = ""
    token: str = ""
    enabled: bool = True
    # Library ids/keys to include; empty means every movie and TV library.
    libraries: List[str] = Field(default_factory=list)
    # Emby only: path prefix as Emby sees xtreamfs .strm files, when it differs from categories.json.
    path_map_from: str = ""
    path_map_to: str = ""
    # Draw on another server's original poster when the same title is there (e.g. Plex uses Emby's originals).
    originals_from: str = ""
    # Titles whose file path starts with one of these are left alone (e.g. /vod).
    skip_paths: List[str] = Field(default_factory=list)

    @field_validator("skip_paths")
    @classmethod
    def _paths(cls, v: List[str]) -> List[str]:
        return [p.strip() for p in v or [] if p and p.strip()]

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return (v or "").strip().rstrip("/")


class BannerPosition(str, Enum):
    top = "Top"
    bottom = "Bottom"


class BadgeStyle(str, Enum):
    separate = "Separate"
    column = "Column"


class TextFont(str, Enum):
    condensed = "Condensed"
    regular = "Regular"


class QualityRule(str, Enum):
    latest_season = "LatestSeason"
    best = "BestAvailable"
    most = "MostEpisodes"


BADGE_KEYS = ["resolution", "hdr", "audio", "imdb", "rtcritic", "rtaudience", "tmdb"]


class Style(BaseModel):
    """How posters look. Same options as the Emby Poster Overlays plugin 0.5.2."""

    movies: bool = True
    series: bool = True
    seasons: bool = True

    banner: bool = True
    banner_position: BannerPosition = BannerPosition.top
    banner_height: int = 6
    banner_opacity: int = 85
    banner_letter_spacing: int = 2
    text_font: TextFont = TextFont.regular
    airing_window_days: int = 15
    show_upcoming: bool = True
    banner_text_color: str = "#FFFFFF"
    airing_text: str = "AIRING"
    airing_color: str = "#1E2130"
    returning_text: str = "RETURNING"
    returning_color: str = "#1E2130"
    ended_text: str = "ENDED"
    ended_color: str = "#1E2130"
    canceled_text: str = "CANCELED"
    canceled_color: str = "#1E2130"
    upcoming_text: str = "UPCOMING"
    upcoming_color: str = "#1E2130"

    show_resolution: bool = True
    show_hdr: bool = True
    show_audio: bool = True
    show_basic_audio: bool = False
    show_quality_on_shows: bool = True
    show_imdb: bool = True
    show_rt_critic: bool = True
    show_rt_audience: bool = False
    show_tmdb: bool = False
    show_ratings_on_seasons: bool = False
    badge_order: List[str] = Field(default_factory=lambda: list(BADGE_KEYS))
    badge_style: BadgeStyle = BadgeStyle.column
    badge_height: int = 6
    badge_icon: int = 100
    badge_margin: int = 2
    max_badges: int = 6
    badge_bg_color: str = "#000000"
    badge_bg_opacity: int = 40
    use_icons: bool = True
    quality_rule: QualityRule = QualityRule.latest_season

    def normalized(self) -> "Style":
        d = Style()
        s = self.model_copy(deep=True)

        def clamp(v, lo, hi):
            return max(lo, min(hi, int(v)))

        s.banner_height = clamp(s.banner_height, 4, 20)
        s.banner_opacity = clamp(s.banner_opacity, 20, 100)
        s.banner_letter_spacing = clamp(s.banner_letter_spacing, 0, 30)
        s.airing_window_days = clamp(s.airing_window_days, 1, 60)
        s.badge_height = clamp(s.badge_height, 3, 12)
        s.badge_icon = clamp(s.badge_icon, 50, 200)
        s.badge_margin = clamp(s.badge_margin, 0, 10)
        s.max_badges = clamp(s.max_badges, 1, 10)
        s.badge_bg_opacity = clamp(s.badge_bg_opacity, 0, 100)
        for name in ("banner_text_color", "airing_color", "returning_color", "ended_color",
                     "canceled_color", "upcoming_color", "badge_bg_color"):
            setattr(s, name, _color(getattr(s, name), getattr(d, name)))
        order = [k for k in dict.fromkeys(k.strip().lower() for k in s.badge_order) if k in BADGE_KEYS]
        s.badge_order = order + [k for k in BADGE_KEYS if k not in order]
        return s


class SourceType(str, Enum):
    mdblist = "mdblist"          # MDBList list URL / user/slug / id (IMDb lists via MDBList external lists)
    tmdb_collection = "tmdb_collection"  # TMDB collection id
    xtream = "xtream"            # xtreamfs category: movie:<id> / series:<id>
    server_collection = "server_collection"  # an existing collection on one of the servers: "<server id>:<name>"
    label = "label"              # Emby tag / Plex label name


class Source(BaseModel):
    type: SourceType
    value: str
    label: str = ""


class ArtworkMode(str, Enum):
    auto = "Auto"
    url = "Url"
    kometa = "Kometa"
    tmdb = "TmdbCollection"
    generated = "Generated"
    none = "None"


class Artwork(BaseModel):
    mode: ArtworkMode = ArtworkMode.auto
    url: str = ""
    kometa_path: str = ""
    tmdb_collection_id: int = 0
    generated_style: str = "Title"   # Title | Mosaic
    color_top: str = "#1F2A44"
    color_bottom: str = "#0B0F19"


class CollectionSort(str, Enum):
    default = "Default"
    release = "ReleaseDate"
    name = "Name"


class CollectionDef(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    enabled: bool = True
    sort_name: str = ""
    summary: str = ""
    # Server ids this collection is kept on. Empty: every enabled server.
    servers: List[str] = Field(default_factory=list)
    sources: List[Source] = Field(default_factory=list)
    exclude: List[Source] = Field(default_factory=list)
    only_add: bool = False
    sort: CollectionSort = CollectionSort.default
    artwork: Artwork = Field(default_factory=Artwork)


class Settings(BaseModel):
    servers: List[ServerConfig] = Field(default_factory=list)
    tmdb_key: str = ""
    mdblist_key: str = ""
    mdblist_reserve: int = 100
    # Most MDBList lookups in one run (0: as many as the daily allowance allows). Keeps runs short.
    mdblist_per_run: int = 2000
    imdb_dataset: bool = True
    icon_source: str = (
        "https://raw.githubusercontent.com/Kometa-Team/Kometa/"
        "e1327dc4e7f709277e3e2be88df76c3eefe349e2/defaults/overlays/images"
    )
    xtream_categories_path: str = "/xtreamfs/categories.json"
    # Originals are kept at most this wide (pixels); posters are drawn and uploaded at that size.
    original_max_width: int = 1000
    # Posters drawn and uploaded at the same time.
    overlay_workers: int = 4
    style: Style = Field(default_factory=Style)
    collections: List[CollectionDef] = Field(default_factory=list)

    # Safety: nothing is written to a server until this is on.
    apply_changes: bool = False
    overlays_enabled: bool = True
    collections_enabled: bool = True
    overlay_hours: int = 6
    collection_hours: int = 12
    remove_missing: bool = True

    def server(self, server_id: str) -> Optional[ServerConfig]:
        return next((s for s in self.servers if s.id == server_id), None)


class SettingsStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(CONFIG_DIR, "settings.json")
        self._lock = threading.Lock()
        self._settings = self._load()

    def _load(self) -> Settings:
        try:
            with open(self.path, encoding="utf-8") as f:
                return Settings.model_validate(json.load(f))
        except FileNotFoundError:
            return Settings()

    def get(self) -> Settings:
        with self._lock:
            return self._settings.model_copy(deep=True)

    def save(self, settings: Settings) -> Settings:
        settings.style = settings.style.normalized()
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(settings.model_dump_json(indent=2))
            os.replace(tmp, self.path)
            self._settings = settings.model_copy(deep=True)
        return self.get()
