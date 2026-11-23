"""Emby Server client (REST API, X-Emby-Token)."""
from __future__ import annotations

import base64
import time
from typing import Dict, List, Optional

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


REFRESH_POLL = 1.5


class EmbyServer(MediaServer):
    kind = "emby"
    can_fetch_images = True

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
            has_backdrop=bool(it.get("BackdropImageTags")) if kind in ("movie", "show") else None,
        )

    def items(self) -> List[Item]:
        out: List[Item] = []
        for lib in self.libraries():
            if not self.wants_library(lib):
                continue
            types = "Movie" if lib.kind == "movie" else "Series"
            for it in self._paged(ParentId=lib.id, Recursive="true", IncludeItemTypes=types, Fields=ITEM_FIELDS,
                                  EnableImageTypes="Primary,Backdrop"):
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

    def episodes_of(self, show: Item) -> List[Item]:
        out = []
        for it in self._paged(ParentId=show.id, Recursive="true", IncludeItemTypes="Episode", Fields="Path",
                              IsMissing="false", EnableImages="false"):
            item = self._item(it, show.library)
            item.parent = show.id
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
        try:
            return self._get_poster(item_id)
        except ServerError as e:
            if "returned 404" not in str(e) or "could not be found" not in str(e):
                raise
            # Emby's poster is a file that no longer exists (e.g. an xtreamfs poster.jpg that was removed). Refreshing
            # the images with "replace all" makes Emby fetch the online poster into its own metadata folder.
            self._replace_local_poster(item_id)
            self.missing_fixed += 1
            return self._get_poster(item_id)

    def _get_poster(self, item_id: str) -> bytes:
        # EnableImageEnhancers=false: the file itself, without any plugin overlay drawn on it.
        r = self._request("GET", self._base() + f"/Items/{item_id}/Images/Primary",
                          params={"EnableImageEnhancers": "false"}, expect_json=False)
        return r.content

    def poster_marker(self, item_id: str) -> str:
        it = self._get("/Items", Ids=item_id, EnableImageTypes="Primary")
        items = it.get("Items") or []
        return image_marker(items[0].get("ImageTags")) if items else ""

    def upload_poster(self, item_id: str, image: bytes, content_type: str = "image/jpeg") -> str:
        try:
            self._upload(item_id, image, content_type)
        except ServerError as e:
            if "Access to the path" not in str(e):
                raise
            # The poster is a file in a read-only folder (e.g. an xtreamfs poster.jpg next to a .strm), and Emby saves
            # a replacement where the current one is. Refreshing the images with "replace all" switches the poster to
            # the online one, after which Emby keeps uploads in its own metadata folder.
            self._replace_local_poster(item_id)
            self._upload(item_id, image, content_type)
            self.readonly_fixed += 1
        return self.poster_marker(item_id)

    def _upload(self, item_id: str, image: bytes, content_type: str) -> None:
        # Emby takes the image base64-encoded in the request body.
        self._request("POST", self._base() + f"/Items/{item_id}/Images/Primary", data=base64.b64encode(image),
                      headers={"Content-Type": content_type}, expect_json=False)

    def _poster_path(self, item_id: str) -> str:
        for img in self._get(f"/Items/{item_id}/Images") or []:
            if img.get("ImageType") == "Primary":
                return img.get("Path") or ""
        return ""

    def fetch_missing_images(self, item_id: str) -> None:
        # Emby's "Search for missing metadata": fills in what's missing, replaces nothing. Emby works through it in
        # the background.
        self._request("POST", self._base() + f"/Items/{item_id}/Refresh", params={
            "Recursive": "false", "MetadataRefreshMode": "FullRefresh", "ImageRefreshMode": "FullRefresh",
            "ReplaceAllImages": "false", "ReplaceAllMetadata": "false"}, expect_json=False)

    def _replace_local_poster(self, item_id: str) -> None:
        before = self._poster_path(item_id)
        self._request("POST", self._base() + f"/Items/{item_id}/Refresh", params={
            "Recursive": "false", "MetadataRefreshMode": "Default", "ImageRefreshMode": "FullRefresh",
            "ReplaceAllImages": "true", "ReplaceAllMetadata": "false"}, expect_json=False)
        # Emby refreshes in the background.
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(REFRESH_POLL)
            now = self._poster_path(item_id)
            if now != before:
                return
        raise ServerError(f"{self.name}: Emby kept its poster file ({before}) after refreshing its images")

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

    def users(self) -> List[dict]:
        return [{"id": str(u["Id"]), "name": u.get("Name") or ""} for u in self._get("/Users") or []]

    def _sections(self, uid: str) -> List[dict]:
        r = self._get(f"/Users/{uid}/HomeSections", IsEditMode="true")
        return r if isinstance(r, list) else (r or {}).get("Items") or (r or {}).get("Sections") or []

    @staticmethod
    def _row(sections: List[dict], collection_id: str) -> Optional[dict]:
        return next((s for s in sections if s.get("SectionType") == "boxset"
                     and str(s.get("ParentId")) == str(collection_id)), None)

    def home_status(self, collection_id: str, section: str = "") -> str:
        users = self.users()
        have = [u["name"] for u in users if self._row(self._sections(u["id"]), collection_id)]
        return f"home screen of {len(have)} of {len(users)} users" if have else ""

    def set_home(self, collection_id: str, section: str, home, title: str) -> None:
        wanted = set(home.emby_users) if home.emby_users else None
        sort_by, _, order = (home.emby_sort or "ProductionYear|Descending").partition("|")
        for u in self.users():
            on = home.enabled and (wanted is None or u["id"] in wanted)
            row = self._row(self._sections(u["id"]), collection_id)
            if on:
                body = dict(row or {})
                body.update({"SectionType": "boxset", "ParentId": str(collection_id), "CustomName": home.name or title,
                             "SortBy": sort_by, "SortOrder": order or "Ascending", "ViewType": home.emby_view or None,
                             "ItemTypes": (row or {}).get("ItemTypes") or [],
                             "ExcludedFolders": (row or {}).get("ExcludedFolders") or [], "IncludeNextUpInResume": True})
                body.pop("ParentItem", None)
                self._request("POST", self._base() + f"/Users/{u['id']}/HomeSections", json_body=body, expect_json=False)
            elif row:
                self._request("POST", self._base() + f"/Users/{u['id']}/HomeSections/Delete",
                              json_body={"Ids": [row["Id"]]}, expect_json=False)

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
