"""Find a server's items for a list of titles (by IMDb/TMDB/TVDB id, or exact path for xtreamfs)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List

from ..servers.base import Item
from .sources import Ref


class LibraryIndex:
    def __init__(self, items: List[Item], path_from: str = "", path_to: str = ""):
        self.items = [i for i in items if i.kind in ("movie", "show")]
        self.by_id: Dict[str, Item] = {i.id: i for i in self.items}
        self.by_key: Dict[tuple, List[str]] = defaultdict(list)
        self.by_path: Dict[str, str] = {}
        self.path_from, self.path_to = path_from, path_to
        for it in self.items:
            for k, v in it.ids.items():
                self.by_key[(it.kind, k, v.lower())].append(it.id)
            if it.path:
                self.by_path[it.path] = it.id

    def map_path(self, path: str) -> str:
        if self.path_from and path.startswith(self.path_from):
            return self.path_to + path[len(self.path_from):]
        return path


@dataclass
class MatchResult:
    ids: List[str] = field(default_factory=list)
    matched: int = 0
    by_path: int = 0
    unmatched: List[Ref] = field(default_factory=list)


def match(refs: List[Ref], index: LibraryIndex) -> MatchResult:
    res = MatchResult()
    seen = set()
    for r in refs:
        found: List[str] = []
        if r.path:
            hit = index.by_path.get(index.map_path(r.path))
            if hit:
                found = [hit]
                res.by_path += 1
        if not found:
            kinds = ["movie", "show"] if r.kind == "any" else [r.kind]
            for kind in kinds:
                # TMDB reuses numbers across movies and shows, so ids only match within one kind.
                for prov, val in (("imdb", r.imdb), ("tmdb", r.tmdb), ("tvdb", r.tvdb)):
                    if val:
                        found = index.by_key.get((kind, prov, str(val).lower()), [])
                        if found:
                            break
                if found:
                    break
        if not found:
            res.unmatched.append(r)
            continue
        res.matched += 1
        # One item per title, as the Emby Collections plugin did: a library often has the same title more than once
        # (own files and VOD copies, versions). A real file beats a .strm copy; ties keep the same item every run.
        best = min(found, key=lambda i: (_is_strm(index.by_id.get(i)), _num(i)))
        if best not in seen:
            seen.add(best)
            res.ids.append(best)
    return res


def _is_strm(item) -> bool:
    return bool(item and (item.path or "").lower().endswith(".strm"))


def _num(i: str):
    return (0, int(i), "") if i.isdigit() else (1, 0, i)
