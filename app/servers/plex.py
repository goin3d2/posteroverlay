"""Plex Media Server client (X-Plex-Token, JSON responses)."""
from __future__ import annotations

from typing import Dict, List
from urllib.parse import quote

from .base import Item, Library, MediaServer, RemoteCollection, ServerError, Stream

PAGE = 1000
TYPE_NUM = {"movie": 1, "show": 2, "season": 3, "episode": 4, "collection": 18}


def plex_ids(meta: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for g in meta.get("Guid") or []:
        gid = g.get("id") or ""
        if "://" in gid:
            k, v = gid.split("://", 1)
            if k in ("imdb", "tmdb", "tvdb") and v:
                out[k] = v
    # Old agents: guid="com.plexapp.agents.imdb://tt123?lang=en"
    guid = meta.get("guid") or ""
    for agent, key in (("agents.imdb://", "imdb"), ("agents.themoviedb://", "tmdb"), ("agents.thetvdb://", "tvdb")):
        if agent in guid and key not in out:
            out[key] = guid.split(agent, 1)[1].split("?")[0].split("/")[0]
    return out


def classify_hdr(s: dict) -> str | None:
    title = " ".join(str(s.get(k) or "") for k in ("displayTitle", "extendedDisplayTitle")).lower()
    if s.get("DOVIPresent") or "dovi" in title or "dolby vision" in title:
        return "dv"
    if "hdr10+" in title:
        return "hdr10plus"
    trc = (s.get("colorTrc") or "").lower()
    if trc == "arib-std-b67" or "hlg" in title:
        return "hlg"
    if trc == "smpte2084" or "hdr" in title:
        return "hdr10"
    return None


def to_streams(media: dict) -> List[Stream]:
    out: List[Stream] = []
    for part in media.get("Part") or []:
        for s in part.get("Stream") or []:
            st = s.get("streamType")
            if st == 1:
                out.append(Stream("video", (s.get("codec") or "").lower(), s.get("profile") or "",
                                  s.get("displayTitle") or "", s.get("width") or media.get("width") or 0,
                                  s.get("height") or media.get("height") or 0, classify_hdr(s)))
            elif st == 2:
                codec = (s.get("codec") or "").lower()
                profile = (s.get("profile") or "").lower()
                title = " ".join(str(s.get(k) or "") for k in ("title", "displayTitle", "extendedDisplayTitle"))
                if codec == "dca":
                    codec = "dts"
                if codec in ("dca-ma",) or profile == "ma":
                    codec, profile = "dts", "DTS-HD MA"
                out.append(Stream("audio", codec, profile, title))
        break  # first part is enough
    if not out:
        # Listing data without streams: resolution and codec from the Media element.
        w, h = media.get("width") or 0, media.get("height") or 0
        if w or h:
            out.append(Stream("video", (media.get("videoCodec") or "").lower(), "", "", w, h, None))
        if media.get("audioCodec"):
            out.append(Stream("audio", "dts" if media["audioCodec"] == "dca" else media["audioCodec"].lower()))
    return out


class PlexServer(MediaServer):
    kind = "plex"

    def _headers(self):
        return {"X-Plex-Token": self.token, "Accept": "application/json",
                "X-Plex-Client-Identifier": "posteroverlay", "X-Plex-Product": "posteroverlay"}

    def _get(self, path: str, **params) -> dict:
        res = self._request("GET", path, params=params)
        return res.get("MediaContainer", {}) if isinstance(res, dict) else {}

    def machine_id(self) -> str:
        if not getattr(self, "_mid", None):
            self._mid = self._get("/identity").get("machineIdentifier", "")
        return self._mid

    def test(self) -> str:
        mc = self._get("/")
        return f"{mc.get('friendlyName', 'Plex')} (Plex {mc.get('version', '?')})"

    def libraries(self) -> List[Library]:
        out = []
        for d in self._get("/library/sections").get("Directory") or []:
            kind = {"movie": "movie", "show": "show"}.get(d.get("type"))
            if kind:
                out.append(Library(str(d.get("key")), d.get("title", ""), kind))
        return out

    def _all(self, path: str, **params) -> List[dict]:
        out: List[dict] = []
        start = 0
        while True:
            res = self._request("GET", path, params=params,
                                headers={"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(PAGE)})
            mc = res.get("MediaContainer", {}) if isinstance(res, dict) else {}
            meta = mc.get("Metadata") or []
            out.extend(meta)
            start += len(meta)
            total = mc.get("totalSize", mc.get("size", 0))
            if not meta or start >= total:
                return out

    def _item(self, m: dict, library: str = "") -> Item:
        kind = {"movie": "movie", "show": "show", "season": "season", "episode": "episode", "collection": "collection"}.get(
            m.get("type"), m.get("type", ""))
        return Item(
            id=str(m.get("ratingKey")),
            kind=kind,
            title=m.get("title") or "",
            year=m.get("year"),
            ids=plex_ids(m),
            path="",
            library=str(m.get("librarySectionID") or library),
            parent=str(m.get("parentRatingKey") or m.get("grandparentRatingKey") or ""),
            index=m.get("index"),
            season=m.get("parentIndex"),
            marker=m.get("thumb") or "",
            community_rating=m.get("audienceRating") or m.get("rating"),
            premiere=(m.get("originallyAvailableAt") or "")[:10],
        )

    def items(self) -> List[Item]:
        out: List[Item] = []
        for lib in self.libraries():
            if not self.wants_library(lib):
                continue
            for m in self._all(f"/library/sections/{lib.id}/all", type=TYPE_NUM[lib.kind], includeGuids=1):
                item = self._item(m, lib.id)
                if lib.kind == "movie":
                    parts = [p for media in m.get("Media") or [] for p in media.get("Part") or []]
                    if parts:
                        item.path = parts[0].get("file") or ""
                    item._listing_media = m.get("Media") or []  # type: ignore[attr-defined]
                else:
                    locs = m.get("Location") or []
                    item.path = locs[0].get("path", "") if locs else ""
                item.version_key = item.id  # Plex keeps all versions of a movie in one item
                out.append(item)
        return out

    def _show_libraries(self) -> List[Library]:
        return [lib for lib in self.libraries() if lib.kind == "show" and self.wants_library(lib)]

    def all_seasons(self) -> List[Item]:
        out = []
        for lib in self._show_libraries():
            for m in self._all(f"/library/sections/{lib.id}/all", type=TYPE_NUM["season"]):
                item = self._item(m, lib.id)
                item.parent = str(m.get("parentRatingKey") or "")
                out.append(item)
        return out

    def all_episodes(self) -> List[Item]:
        out = []
        for lib in self._show_libraries():
            for m in self._all(f"/library/sections/{lib.id}/all", type=TYPE_NUM["episode"]):
                item = self._item(m, lib.id)
                item.parent = str(m.get("grandparentRatingKey") or "")
                parts = [p for media in m.get("Media") or [] for p in media.get("Part") or []]
                item.path = parts[0].get("file", "") if parts else ""
                out.append(item)
        return out

    def episodes_of(self, show: Item) -> List[Item]:
        out = []
        for m in self._all(f"/library/metadata/{show.id}/allLeaves"):
            item = self._item(m, show.library)
            item.parent = show.id
            parts = [p for media in m.get("Media") or [] for p in media.get("Part") or []]
            item.path = parts[0].get("file", "") if parts else ""
            out.append(item)
        return out

    def file_streams(self, item_ids: List[str]) -> Dict[str, List[List[Stream]]]:
        # Plex only returns streams in an item's own metadata: one call per title.
        out: Dict[str, List[List[Stream]]] = {}
        for iid in item_ids:
            try:
                meta = (self._get(f"/library/metadata/{iid}").get("Metadata") or [{}])[0]
            except ServerError:
                continue
            out[iid] = [to_streams(media) for media in meta.get("Media") or []]
        return out

    def get_poster(self, item_id: str) -> bytes:
        thumb = self.poster_marker(item_id)
        if not thumb:
            raise ServerError(f"{self.name}: item {item_id} has no poster")
        r = self._request("GET", thumb, expect_json=False)
        return r.content

    def poster_marker(self, item_id: str) -> str:
        meta = (self._get(f"/library/metadata/{item_id}").get("Metadata") or [{}])[0]
        return meta.get("thumb") or ""

    def upload_poster(self, item_id: str, image: bytes, content_type: str = "image/jpeg") -> str:
        # Plex stores the upload and makes it the selected poster.
        self._request("POST", f"/library/metadata/{item_id}/posters", data=image,
                      headers={"Content-Type": content_type}, expect_json=False)
        return self.poster_marker(item_id)

    # ---- collections (Plex collections live in one library section each)
    def collections(self) -> List[RemoteCollection]:
        out = []
        for lib in self.libraries():
            for m in self._all(f"/library/sections/{lib.id}/collections"):
                out.append(RemoteCollection(str(m.get("ratingKey")), m.get("title", ""), lib.id,
                                            int(m.get("childCount") or 0), m.get("thumb") or ""))
        return out

    def collection_members(self, collection_id: str) -> List[str]:
        return [str(m.get("ratingKey")) for m in self._all(f"/library/collections/{collection_id}/children")]

    def _uri(self, item_ids: List[str]) -> str:
        return f"server://{self.machine_id()}/com.plexapp.plugins.library/library/metadata/{','.join(item_ids)}"

    def create_collection(self, name: str, item_ids: List[str], section: str = "") -> str:
        if not section:
            raise ServerError(f"{self.name}: a Plex collection needs a library section")
        kind = next((lib.kind for lib in self.libraries() if lib.id == section), "movie")
        first = item_ids[:100]
        res = self._request("POST", "/library/collections",
                            params={"type": TYPE_NUM[kind], "title": name, "smart": 0, "sectionId": section,
                                    "uri": self._uri(first)})
        meta = (res.get("MediaContainer", {}).get("Metadata") or [{}])[0]
        cid = str(meta.get("ratingKey") or "")
        if not cid:
            raise ServerError(f"{self.name}: creating collection '{name}' returned no id")
        if len(item_ids) > 100:
            self.add_to_collection(cid, item_ids[100:])
        return cid

    def add_to_collection(self, collection_id: str, item_ids: List[str], section: str = "") -> None:
        for i in range(0, len(item_ids), 100):
            self._request("PUT", f"/library/collections/{collection_id}/items",
                          params={"uri": self._uri(item_ids[i:i + 100])}, expect_json=False)

    def remove_from_collection(self, collection_id: str, item_ids: List[str]) -> None:
        for iid in item_ids:
            self._request("DELETE", f"/library/collections/{collection_id}/items/{iid}", expect_json=False)

    def delete_collection(self, collection_id: str) -> None:
        self._request("DELETE", f"/library/metadata/{collection_id}", expect_json=False)

    def update_collection(self, collection_id: str, name: str, sort_name: str, summary: str, sort: str,
                          section: str = "") -> None:
        params = {"type": TYPE_NUM["collection"], "id": collection_id}
        if name:
            params["title.value"] = name
        if sort_name:
            params["titleSort.value"] = sort_name
        if summary:
            params["summary.value"] = summary
        if section and len(params) > 2:
            self._request("PUT", f"/library/sections/{section}/all", params=params, expect_json=False)
        if sort in ("ReleaseDate", "Name"):
            self._request("PUT", f"/library/metadata/{collection_id}/prefs",
                          params={"collectionSort": 0 if sort == "ReleaseDate" else 1}, expect_json=False)

    def items_with_label(self, label: str) -> List[str]:
        out = []
        for lib in self.libraries():
            if not self.wants_library(lib):
                continue
            out.extend(str(m.get("ratingKey")) for m in
                       self._all(f"/library/sections/{lib.id}/all", type=TYPE_NUM[lib.kind], label=label))
        return out


def make_server(cfg) -> MediaServer:
    from .emby import EmbyServer

    cls = EmbyServer if cfg.kind == "emby" else PlexServer
    return cls(cfg.id, cfg.name, cfg.url, cfg.token, cfg.libraries)


__all__ = ["PlexServer", "make_server", "quote"]
