"""Where collection members come from. Each source returns titles by id (or path); matching to each server's
library happens later, per server."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from ..media.ratings import Tmdb
from ..settings import Settings, Source, SourceType

MDB = "https://api.mdblist.com/"
LIST_URL = re.compile(r"mdblist\.com/lists/(?P<path>[^?#]+)", re.I)


@dataclass
class Ref:
    kind: str = "any"          # movie | show | any
    imdb: Optional[str] = None
    tmdb: Optional[str] = None
    tvdb: Optional[str] = None
    path: Optional[str] = None
    title: str = ""


@dataclass
class SourceResult:
    refs: List[Ref] = field(default_factory=list)
    # Items found directly on a server, by server id (labels/tags are per server).
    direct: Dict[str, List[str]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    failed: bool = False


def mdblist_path(value: str) -> str:
    v = (value or "").strip()
    m = LIST_URL.search(v)
    if m:
        v = m.group("path")
    v = v.strip("/")
    if v.lower().startswith("external/"):
        return "external/lists/" + v[len("external/"):]
    if not v:
        raise ValueError("Empty MDBList list")
    return "lists/" + v


def _s(d, *names) -> Optional[str]:
    if not isinstance(d, dict):
        return None
    for n in names:
        v = d.get(n)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = str(int(v))
        if isinstance(v, str) and v.strip() and v.strip() != "0":
            return v.strip()
    return None


def parse_mdblist_items(data) -> List[Ref]:
    arrays = []
    if isinstance(data, list):
        arrays.append((data, "any"))
    elif isinstance(data, dict):
        for name, kind in (("movies", "movie"), ("shows", "show"), ("items", "any")):
            if isinstance(data.get(name), list):
                arrays.append((data[name], kind))
    out: List[Ref] = []
    for arr, kind in arrays:
        for e in arr:
            if not isinstance(e, dict):
                continue
            k = kind
            mt = (_s(e, "mediatype", "type") or "").lower()
            if mt.startswith("movie"):
                k = "movie"
            elif mt.startswith("show") or mt.startswith("tv"):
                k = "show"
            ids = e.get("ids") or {}
            r = Ref(kind=k, title=e.get("title") or "",
                    imdb=_s(e, "imdb_id", "imdbid") or _s(ids, "imdb"),
                    tmdb=_s(e, "tmdb_id", "tmdbid") or _s(ids, "tmdb") or _s(e, "id"),
                    tvdb=_s(e, "tvdb_id", "tvdbid") or _s(ids, "tvdb"))
            if r.imdb or r.tmdb or r.tvdb:
                out.append(r)
    return out


def resolve_mdblist(value: str, key: str) -> SourceResult:
    if not key:
        raise RuntimeError("MDBList API key is not set")
    res = SourceResult()
    path = mdblist_path(value)
    for page in range(20):
        r = requests.get(f"{MDB}{path}/items/", params={"apikey": key, "limit": 1000, "offset": page * 1000,
                                                       "unified": "true"}, timeout=60)
        if r.status_code == 404:
            raise RuntimeError(f"MDBList list not found: {value}")
        if r.status_code in (401, 403):
            raise RuntimeError("MDBList rejected the API key")
        if r.status_code == 429:
            raise RuntimeError("MDBList rate limit reached; try again later")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"MDBList: {data['error']}")
        items = parse_mdblist_items(data)
        res.refs.extend(items)
        if r.headers.get("X-Has-More", "").lower() != "true" or not items:
            break
    movies = sum(1 for x in res.refs if x.kind == "movie")
    res.notes.append(f"MDBList {path[len('lists/'):] if path.startswith('lists/') else path}: {len(res.refs)} titles "
                     f"({movies} movies, {len(res.refs) - movies} shows)")
    if not res.refs:
        res.warnings.append(f"MDBList list {value} returned no titles")
    return res


def resolve_tmdb_collection(value: str, key: str) -> SourceResult:
    tmdb = Tmdb(key)
    if not tmdb.configured:
        raise RuntimeError("TMDB API key is not set")
    data = tmdb.collection(int(str(value).strip()))
    if not data:
        raise RuntimeError(f"TMDB collection {value} not found")
    res = SourceResult(refs=[Ref(kind="movie", tmdb=str(p["id"]), title=p.get("title") or "")
                             for p in data.get("parts") or [] if p.get("id")])
    res.notes.append(f"TMDB collection '{data.get('name')}': {len(res.refs)} movies")
    return res


CATEGORY_CANDIDATES = ["/xtreamfs/categories.json", "/xtreamfs/config/categories.json", "/vod/categories.json",
                       "/strm/categories.json"]


def find_categories(path: str) -> Optional[str]:
    """The configured categories.json, or the first one found where xtreamfs usually writes it."""
    for p in [path] + [c for c in CATEGORY_CANDIDATES if c != path]:
        if p and os.path.isfile(p):
            return p
    return None


def xtream_categories(path: str) -> List[dict]:
    path = find_categories(path)
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    out = []
    for section, kind in (("movies", "movie"), ("series", "series")):
        for cid, cat in (doc.get(section) or {}).items():
            out.append({"value": f"{kind}:{cid}", "kind": kind, "id": cid, "name": cat.get("name") or cid,
                        "count": len(cat.get("items") or [])})
    return sorted(out, key=lambda c: (c["kind"], c["name"].lower()))


def resolve_xtream(value: str, path: str) -> SourceResult:
    found = find_categories(path)
    if not found:
        raise RuntimeError(f"No xtreamfs categories.json at {path}. Map the folder that holds it (xtreamfs's config "
                           "folder, or the top of its .strm folder) to /xtreamfs.")
    path = found
    kind, _, cid = (value or "").partition(":")
    if not cid:
        raise ValueError(f"xtreamfs category should look like movie:123 or series:45, not '{value}'")
    is_movie = kind.startswith("movie")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    cat = (doc.get("movies" if is_movie else "series") or {}).get(cid)
    res = SourceResult()
    if not cat:
        res.warnings.append(f"xtreamfs category {value} is not in categories.json (not selected, or removed)")
        return res
    for it in cat.get("items") or []:
        res.refs.append(Ref(kind="movie" if is_movie else "show", path=it.get("path"), tmdb=it.get("tmdb")))
    res.notes.append(f"xtreamfs {'movie' if is_movie else 'series'} category '{cat.get('name', cid)}': "
                     f"{len(res.refs)} titles ({sum(1 for r in res.refs if r.tmdb)} with a TMDB id)")
    return res


def resolve(source: Source, settings: Settings, servers: Dict[str, object], indexes: Dict[str, object]) -> SourceResult:
    """servers: server id -> MediaServer; indexes: server id -> LibraryIndex (for server_collection and labels)."""
    t = source.type
    if t == SourceType.mdblist:
        return resolve_mdblist(source.value, settings.mdblist_key)
    if t == SourceType.tmdb_collection:
        return resolve_tmdb_collection(source.value, settings.tmdb_key)
    if t == SourceType.xtream:
        return resolve_xtream(source.value, settings.xtream_categories_path)
    if t == SourceType.label:
        res = SourceResult()
        for sid, srv in servers.items():
            ids = srv.items_with_label(source.value)
            res.direct[sid] = ids
            res.notes.append(f"{srv.name}: {len(ids)} titles labelled '{source.value}'")
        return res
    if t == SourceType.server_collection:
        sid, _, name = (source.value or "").partition(":")
        srv = servers.get(sid)
        if srv is None:
            raise RuntimeError(f"Source server {sid} is not set up or not enabled")
        index = indexes[sid]
        wanted = [c for c in srv.collections() if c.name.lower() == name.strip().lower() or c.id == name.strip()]
        if not wanted:
            raise RuntimeError(f"No collection named '{name}' on {srv.name}")
        res = SourceResult()
        members: List[str] = []
        for c in wanted:
            members.extend(srv.collection_members(c.id))
        res.direct[sid] = members
        for mid in members:
            it = index.by_id.get(mid)
            if it is not None:
                res.refs.append(Ref(kind=it.kind, imdb=it.ids.get("imdb"), tmdb=it.ids.get("tmdb"),
                                    tvdb=it.ids.get("tvdb"), title=it.title))
        res.notes.append(f"{srv.name} collection '{wanted[0].name}': {len(members)} titles")
        return res
    raise ValueError(f"Unknown source type {t}")
