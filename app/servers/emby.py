"""Emby Server client (REST API, X-Emby-Token)."""
from __future__ import annotations

import base64
from typing import Dict, List

from .base import Item, Library, MediaServer, RemoteCollection, ServerError, Stream, norm_ids

ITEM_FIELDS = "ProviderIds,Path,PresentationUniqueKey,PremiereDate,ProductionYear,CommunityRating,Status,ParentId"
PAGE = 2000


def image_marker(tags: Dict[str, str]) -> str:
    """Emby's Primary image tag. Its third part is the image-enhancer config (a plugin's overlay), so drop it:
    the marker should change only when the image file itself changes."""
    tag = (tags or {}).get("Primary") or ""
    parts = tag.split("_")
    return "_".join(parts[:2]) if len(parts) >= 2 else tag


def classify_hdr(s: dict) -> str | None:
    ext = (s.get("ExtendedVideoType") or "").lower()
    rng = (s.get("VideoRange") or "").lower()
    trc = (s.get("ColorTransfer") or "").lower()
    if "dolby" in ext or "dolby" in rng or "dovi" in rng:
        return "dv"
    if "hdr10plus" in ext or "hdr10+" in rng:
        return "hdr10plus"
    if "hlg" in ext or "hyperlog" in ext or trc == "arib-std-b67":
        return "hlg"
    if "hdr10" in ext or trc == "smpte2084" or rng == "hdr":
        return "hdr10"
    return None


def to_streams(source: dict) -> List[Stream]:
    out: List[Stream] = []
    for s in source.get("MediaStreams") or []:
        t = (s.get("Type") or "").lower()
        if t == "video":
            codec = (s.get("Codec") or "").lower()
            if codec in ("mjpeg", "png", "bmp"):
                continue
            out.append(Stream("video", codec, s.get("Profile") or "", s.get("DisplayTitle") or "",
                              s.get("Width") or 0, s.get("Height") or 0, classify_hdr(s)))
        elif t == "audio":
            out.append(Stream("audio", (s.get("Codec") or "").lower(), s.get("Profile") or "",
                              " ".join(x for x in (s.get("Title"), s.get("DisplayTitle")) if x)))
    return out


class EmbyServer(MediaServer):
    kind = "emby"

    def _headers(self):
        return {"X-Emby-Token": self.token, "Accept": "application/json"}

    def _base(self) -> str:
        return "" if self.url.endswith("/emby") else "/emby"

    def _get(self, path: str, **params):
        return self._request("GET", self._base() + path, params=params)

    def test(self) -> str:
        info = self._get("/System/Info")
        return f"{info.get('ServerName', 'Emby')} (Emby {info.get('Version', '?')})"

    def libraries(self) -> List[Library]:
        libs = []
        for v in self._get("/Library/VirtualFolders") or []:
            ct = (v.get("CollectionType") or "").lower()
            kind = {"movies": "movie", "tvshows": "show"}.get(ct)
            if kind:
                libs.append(Library(str(v.get("ItemId")), v.get("Name", ""), kind))
        return libs

    def _paged(self, **params) -> List[dict]:
        out: List[dict] = []
        start = 0
        while True:
            page = self._get("/Items", StartIndex=start, Limit=PAGE, **params)
            items = page.get("Items") or []
            out.extend(items)
            start += len(items)
            if not items or start >= (page.get("TotalRecordCount") or 0):
                return out

    def _item(self, it: dict, library: str = "") -> Item:
        kind = {"Movie": "movie", "Series": "show", "Season": "season", "Episode": "episode", "BoxSet": "collection"}.get(
            it.get("Type"), (it.get("Type") or "").lower())
        return Item(
            id=str(it.get("Id")),
            kind=kind,
            title=it.get("Name") or "",
            year=it.get("ProductionYear"),
            ids=norm_ids(it.get("ProviderIds")),
            path=it.get("Path") or "",
            library=library,
            parent=str(it.get("SeriesId") or it.get("ParentId") or ""),
            index=it.get("IndexNumber"),
            season=it.get("ParentIndexNumber"),
            marker=image_marker(it.get("ImageTags")),
            community_rating=it.get("CommunityRating"),
            status=it.get("Status") or "",
            version_key=it.get("PresentationUniqueKey") or "",
            premiere=(it.get("PremiereDate") or "")[:10],
        )

    def items(self) -> List[Item]:
        out: List[Item] = []
        for lib in self.libraries():
            if not self.wants_library(lib):
                continue
            types = "Movie" if lib.kind == "movie" else "Series"
            for it in self._paged(ParentId=lib.id, Recursive="true", IncludeItemTypes=types, Fields=ITEM_FIELDS,
                                  EnableImageTypes="Primary"):
                out.append(self._item(it, lib.id))
        # A title in two libraries (or a .strm and a file) appears once per item; that's what Emby shows.
        return out

    def _show_libraries(self) -> List[Library]:
        return [lib for lib in self.libraries() if lib.kind == "show" and self.wants_library(lib)]

    def all_seasons(self) -> List[Item]:
        out = []
        for lib in self._show_libraries():
            for it in self._paged(ParentId=lib.id, Recursive="true", IncludeItemTypes="Season",
                                  Fields="ParentId,PresentationUniqueKey", EnableImageTypes="Primary"):
                item = self._item(it, lib.id)
                item.parent = str(it.get("SeriesId") or it.get("ParentId") or "")
                out.append(item)
        return out

    def all_episodes(self) -> List[Item]:
        out = []
        for lib in self._show_libraries():
            for it in self._paged(ParentId=lib.id, Recursive="true", IncludeItemTypes="Episode", Fields="Path",
                                  IsMissing="false", EnableImages="false"):
                item = self._item(it, lib.id)
                item.parent = str(it.get("SeriesId") or "")
                out.append(item)
        return out

    def file_streams(self, item_ids: List[str]) -> Dict[str, List[List[Stream]]]:
        out: Dict[str, List[List[Stream]]] = {}
        for i in range(0, len(item_ids), 100):
            chunk = item_ids[i:i + 100]
            page = self._get("/Items", Ids=",".join(chunk), Fields="MediaSources", EnableImages="false")
            for it in page.get("Items") or []:
                out[str(it["Id"])] = [to_streams(src) for src in it.get("MediaSources") or []]
        return out

    def get_poster(self, item_id: str) -> bytes:
        # EnableImageEnhancers=false: the file itself, without any plugin overlay drawn on it.
        r = self._request("GET", self._base() + f"/Items/{item_id}/Images/Primary",
                          params={"EnableImageEnhancers": "false"}, expect_json=False)
        return r.content

    def poster_marker(self, item_id: str) -> str:
        it = self._get("/Items", Ids=item_id, EnableImageTypes="Primary")
        items = it.get("Items") or []
        return image_marker(items[0].get("ImageTags")) if items else ""

    def upload_poster(self, item_id: str, image: bytes, content_type: str = "image/jpeg") -> str:
        # Emby takes the image base64-encoded in the request body.
        self._request("POST", self._base() + f"/Items/{item_id}/Images/Primary", data=base64.b64encode(image),
                      headers={"Content-Type": content_type}, expect_json=False)
        return self.poster_marker(item_id)

    def warnings(self) -> List[str]:
        out = []
        try:
            for p in self._get("/Plugins") or []:
                if p.get("Name") == "Poster Overlays":
                    out.append(
                        f"The Poster Overlays plugin {p.get('Version', '')} is installed on {self.name}. It draws its own "
                        "badges on top of every poster, so posters from this container would get two sets. "
                        "Uninstall the plugin (or untick Movies/Series/Seasons in its settings) before applying overlays here.")
        except ServerError:
            pass
        return out

    # ---- collections
    def collections(self) -> List[RemoteCollection]:
        out = []
        for it in self._paged(Recursive="true", IncludeItemTypes="BoxSet", Fields="ChildCount", EnableImageTypes="Primary"):
            out.append(RemoteCollection(str(it["Id"]), it.get("Name", ""), "", it.get("ChildCount") or 0,
                                        image_marker(it.get("ImageTags"))))
        return out

    def collection_members(self, collection_id: str) -> List[str]:
        return [str(i["Id"]) for i in self._paged(ParentId=collection_id)]

    def create_collection(self, name: str, item_ids: List[str], section: str = "") -> str:
        res = self._request("POST", self._base() + "/Collections", params={"Name": name, "Ids": ",".join(item_ids[:200])})
        cid = str(res.get("Id") or "")
        if not cid:
            raise ServerError(f"{self.name}: creating collection '{name}' returned no id")
        if len(item_ids) > 200:
            self.add_to_collection(cid, item_ids[200:])
        return cid

    def add_to_collection(self, collection_id: str, item_ids: List[str], section: str = "") -> None:
        for i in range(0, len(item_ids), 200):
            self._request("POST", self._base() + f"/Collections/{collection_id}/Items",
                          params={"Ids": ",".join(item_ids[i:i + 200])}, expect_json=False)

    def remove_from_collection(self, collection_id: str, item_ids: List[str]) -> None:
        for i in range(0, len(item_ids), 200):
            self._request("DELETE", self._base() + f"/Collections/{collection_id}/Items",
                          params={"Ids": ",".join(item_ids[i:i + 200])}, expect_json=False)

    def delete_collection(self, collection_id: str) -> None:
        self._request("DELETE", self._base() + f"/Items/{collection_id}", expect_json=False)

    def update_collection(self, collection_id: str, name: str, sort_name: str, summary: str, sort: str,
                          section: str = "") -> None:
        # Emby updates an item by posting the whole item back.
        user = self._admin_user()
        item = self._get(f"/Users/{user}/Items/{collection_id}")
        changed = False
        for key, value in (("Name", name), ("ForcedSortName", sort_name), ("Overview", summary)):
            if value and item.get(key) != value:
                item[key] = value
                changed = True
        if sort in ("ReleaseDate", "Name"):
            want = "PremiereDate" if sort == "ReleaseDate" else "SortName"
            if item.get("DisplayOrder") != want:
                item["DisplayOrder"] = want
                changed = True
        if changed:
            self._request("POST", self._base() + f"/Items/{collection_id}", json_body=item, expect_json=False)

    def _admin_user(self) -> str:
        if not hasattr(self, "_admin"):
            users = self._get("/Users") or []
            admin = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), users[0] if users else None)
            if not admin:
                raise ServerError(f"{self.name}: no users found")
            self._admin = admin["Id"]
        return self._admin

    def items_with_label(self, label: str) -> List[str]:
        return [str(i["Id"]) for i in self._paged(Recursive="true", IncludeItemTypes="Movie,Series", Tags=label)]
