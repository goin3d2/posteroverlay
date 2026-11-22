"""What the rest of the app needs from a media server, in server-neutral terms."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import requests

ID_KEYS = ("imdb", "tmdb", "tvdb")


def norm_ids(raw: Dict[str, str]) -> Dict[str, str]:
    """Lower-case provider ids and keep only imdb/tmdb/tvdb (Emby uses Imdb, IMDB...; Plex uses imdb://)."""
    out: Dict[str, str] = {}
    for k, v in (raw or {}).items():
        key = str(k).lower()
        if key in ID_KEYS and v not in (None, "", "0"):
            out[key] = str(v).strip()
    return out


@dataclass
class Stream:
    kind: str                 # "video" | "audio"
    codec: str = ""
    profile: str = ""
    title: str = ""           # any human text the server has: display title, track title
    width: int = 0
    height: int = 0
    hdr: Optional[str] = None  # "dv" | "hdr10plus" | "hdr10" | "hlg"


@dataclass
class Item:
    id: str
    kind: str                 # "movie" | "show" | "season" | "episode"
    title: str = ""
    year: Optional[int] = None
    ids: Dict[str, str] = field(default_factory=dict)
    path: str = ""
    library: str = ""
    parent: str = ""          # show id for seasons/episodes
    index: Optional[int] = None   # season number (seasons), episode number (episodes)
    season: Optional[int] = None  # episodes: season number
    marker: str = ""          # identifies the current primary image (changes when the poster changes)
    community_rating: Optional[float] = None
    status: str = ""          # Emby series status: Continuing / Ended
    version_key: str = ""     # items with the same key are versions of one title
    premiere: str = ""


@dataclass
class Library:
    id: str
    name: str
    kind: str                 # "movie" | "show"


@dataclass
class RemoteCollection:
    id: str
    name: str
    section: str = ""
    count: int = 0
    marker: str = ""


class ServerError(Exception):
    pass


class MediaServer:
    """Base class. Concrete clients: EmbyServer, PlexServer."""

    kind = "base"

    def __init__(self, server_id: str, name: str, url: str, token: str, libraries: Sequence[str] = ()):
        self.server_id = server_id
        self.name = name or self.kind
        self.url = url.rstrip("/")
        self.token = token
        self.only_libraries = [str(x) for x in libraries or []]
        self.session = requests.Session()
        self.timeout = 60

    # ---- plumbing
    def _request(self, method: str, path: str, *, params=None, data=None, headers=None, json_body=None,
                 expect_json=True, timeout=None):
        url = self.url + path
        h = self._headers()
        h.update(headers or {})
        try:
            r = self.session.request(method, url, params=params, data=data, json=json_body, headers=h,
                                     timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise ServerError(f"{self.name}: {method} {path} failed: {e}") from e
        if r.status_code in (401, 403):
            raise ServerError(f"{self.name}: the token was rejected ({r.status_code})")
        if r.status_code >= 400:
            raise ServerError(f"{self.name}: {method} {path} returned {r.status_code}: {r.text[:200]}")
        if not expect_json:
            return r
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    def _headers(self) -> Dict[str, str]:
        return {}

    def wants_library(self, lib: Library) -> bool:
        return not self.only_libraries or lib.id in self.only_libraries or lib.name in self.only_libraries

    # ---- interface
    def test(self) -> str:
        raise NotImplementedError

    def libraries(self) -> List[Library]:
        raise NotImplementedError

    def items(self) -> List[Item]:
        """Every movie and show in the selected libraries."""
        raise NotImplementedError

    def all_seasons(self) -> List[Item]:
        """Every season in the selected libraries (.parent = show id, .index = season number)."""
        raise NotImplementedError

    def all_episodes(self) -> List[Item]:
        """Every episode, light: id, .parent = show id, .season, .index, .path (no streams)."""
        raise NotImplementedError

    def episodes_of(self, show: Item) -> List[Item]:
        """Episodes of one show, same shape as all_episodes()."""
        return [e for e in self.all_episodes() if e.parent == show.id]

    def file_streams(self, item_ids: List[str]) -> Dict[str, List[List[Stream]]]:
        """Streams of each file (version) of the given movies/episodes."""
        raise NotImplementedError

    def get_poster(self, item_id: str) -> bytes:
        raise NotImplementedError

    def poster_marker(self, item_id: str) -> str:
        raise NotImplementedError

    def upload_poster(self, item_id: str, image: bytes, content_type: str = "image/jpeg") -> str:
        """Upload and select a poster; returns the new marker."""
        raise NotImplementedError

    def warnings(self) -> List[str]:
        return []

    # ---- collections
    def collections(self) -> List[RemoteCollection]:
        raise NotImplementedError

    def collection_members(self, collection_id: str) -> List[str]:
        raise NotImplementedError

    def create_collection(self, name: str, item_ids: List[str], section: str = "") -> str:
        raise NotImplementedError

    def add_to_collection(self, collection_id: str, item_ids: List[str], section: str = "") -> None:
        raise NotImplementedError

    def remove_from_collection(self, collection_id: str, item_ids: List[str]) -> None:
        raise NotImplementedError

    def users(self) -> List[dict]:
        """[{id, name}] for servers with per-user home screens (Emby); [] otherwise."""
        return []

    def home_status(self, collection_id: str, section: str = "") -> str:
        """Short description of where the collection is shown on home screens."""
        return ""

    def set_home(self, collection_id: str, section: str, home, title: str) -> None:
        """Show the collection on home screens as home (HomeSettings) says, or remove it when home.enabled is off."""

    def delete_collection(self, collection_id: str) -> None:
        raise NotImplementedError

    def update_collection(self, collection_id: str, name: str, sort_name: str, summary: str, sort: str,
                          section: str = "") -> None:
        raise NotImplementedError

    def items_with_label(self, label: str) -> List[str]:
        raise NotImplementedError

    def collection_exists(self, collection_id: str) -> bool:
        return any(c.id == collection_id for c in self.collections())


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())
