"""Ratings and series status shared by every server: IMDb's daily ratings file, MDBList (Rotten Tomatoes),
TMDB (series status, id lookups, collections). Results are cached by provider id in the store."""
from __future__ import annotations

import datetime as dt
import gzip
import io
import os
import time
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

import requests

from ..store import Store

IMDB_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
MDB = "https://api.mdblist.com/"
TMDB = "https://api.themoviedb.org/3/"

session = requests.Session()
session.headers["User-Agent"] = "posteroverlay/0.1"


class QuotaExhausted(Exception):
    pass


# ------------------------------------------------------------------ IMDb dataset
def refresh_imdb(store: Store, config_dir: str, wanted: Set[str], log: Callable[[str], None]) -> int:
    """Download IMDb's ratings file at most once a day and cache ratings for wanted tt ids."""
    path = os.path.join(config_dir, "imdb-title-ratings.tsv.gz")
    age = store.age("meta:imdb_dataset")
    if not os.path.exists(path) or age is None or age > 20 * 3600:
        r = session.get(IMDB_URL, timeout=120)
        r.raise_for_status()
        with open(path + ".tmp", "wb") as f:
            f.write(r.content)
        os.replace(path + ".tmp", path)
        store.put("meta:imdb_dataset", {"bytes": len(r.content)})
        log(f"IMDb ratings file downloaded ({len(r.content) // 1024} KB)")
    with open(path, "rb") as f:
        found = parse_imdb(f.read(), wanted)
    store.put_many({f"imdb:{k}": v for k, v in found.items()})
    log(f"IMDb ratings: {len(found)} of {len(wanted)} titles found")
    return len(found)


def parse_imdb(gz: bytes, wanted: Set[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    with gzip.open(io.BytesIO(gz), "rt", encoding="utf-8") as f:
        next(f, None)
        for line in f:
            tab = line.find("\t")
            if tab <= 0 or line[:tab] not in wanted:
                continue
            parts = line.rstrip("\n").split("\t")
            try:
                rating = float(parts[1])
            except (IndexError, ValueError):
                continue
            if 0 < rating <= 10:
                out[parts[0]] = round(rating, 1)
    return out


# ------------------------------------------------------------------ MDBList
class MdbList:
    def __init__(self, key: str, store: Store, reserve: int, log: Callable[[str], None]):
        self.key = (key or "").strip()
        self.store = store
        self.reserve = reserve
        self.log = log
        self.calls = 0
        self.budget = 0
        self.pace = 0.1

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def quota(self) -> Optional[Tuple[int, int]]:
        r = session.get(MDB + "user", params={"apikey": self.key}, timeout=20)
        if r.status_code in (401, 403):
            raise RuntimeError("MDBList rejected the API key")
        r.raise_for_status()
        d = r.json()
        if "api_requests" not in d:
            return None
        return int(d.get("api_requests") or 0), int(d.get("api_requests_count") or 0)

    def start(self) -> int:
        try:
            q = self.quota()
        except Exception as e:  # noqa: BLE001
            self.log(f"MDBList quota check failed ({e}); allowing 900 calls")
            self.budget = 900
            return self.budget
        if q is None:
            self.budget = 900
        else:
            limit, used = q
            self.budget = max(0, limit - used - self.reserve)
            self.store.put("meta:mdblist_quota", {"limit": limit, "used": used})
            self.log(f"MDBList quota: {used} of {limit} used today, keeping {self.reserve} in reserve; this run may use {self.budget}")
        return self.budget

    def ratings(self, kind: str, provider: str, pid: str) -> Optional[dict]:
        """One title's ratings; raises QuotaExhausted when the day's budget is gone."""
        if self.calls >= self.budget:
            raise QuotaExhausted()
        backoff = [30, 60, 120, 240]
        for attempt in range(len(backoff) + 1):
            self.calls += 1
            try:
                r = session.get(f"{MDB}{provider}/{kind}/{pid}/", params={"apikey": self.key}, timeout=20)
            except requests.RequestException as e:
                self.log(f"MDBList lookup failed for {provider}:{pid}: {e}")
                return None
            if r.status_code == 429:
                q = None
                try:
                    q = self.quota()
                except Exception:  # noqa: BLE001
                    pass
                if q and q[0] - q[1] - self.reserve <= 0:
                    raise QuotaExhausted()
                if attempt == len(backoff):
                    raise QuotaExhausted()
                wait = backoff[attempt]
                try:
                    wait = max(5, min(600, int(r.headers.get("Retry-After", wait))))
                except ValueError:
                    pass
                self.pace = min(self.pace * 2, 2.0)
                self.log(f"MDBList said too many requests; waiting {wait} s, then one call every {self.pace:.1f} s")
                time.sleep(wait * _scale)
                continue
            if r.status_code in (400, 404):
                return {"not_found": True}
            if r.status_code in (401, 403):
                raise RuntimeError("MDBList rejected the API key")
            if r.status_code >= 400:
                self.log(f"MDBList answered {r.status_code} for {provider}:{pid}")
                return None
            time.sleep(self.pace * _scale)
            return parse_mdblist(r.json())
        return None


def parse_mdblist(d: dict) -> dict:
    out: dict = {}
    for r in d.get("ratings") or []:
        src, val = r.get("source"), r.get("value")
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if val <= 0:
            continue
        if src == "imdb" and val <= 10:
            out["imdb"] = round(val, 1)
        elif src == "tomatoes" and val <= 100:
            out["rt_critic"] = int(round(val))
        elif src in ("tomatoesaudience", "popcorn") and val <= 100:
            out["rt_audience"] = int(round(val))
    if not d.get("ratings") and d.get("error"):
        out["not_found"] = True
    return out


_scale = 1.0  # tests set this to 0 to skip waits


# ------------------------------------------------------------------ TMDB
class Tmdb:
    def __init__(self, key: str):
        self.key = (key or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def _get(self, path: str, **params):
        headers = {"Accept": "application/json"}
        if len(self.key) > 40:
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        for attempt in range(3):
            r = session.get(TMDB + path, params=params, headers=headers, timeout=30)
            if r.status_code == 429 and attempt < 2:
                time.sleep(2 * (attempt + 1) * _scale)
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 401:
                raise RuntimeError("TMDB rejected the API key")
            r.raise_for_status()
            return r.json()
        return None

    def series_status(self, tmdb_id: str) -> Optional[dict]:
        d = self._get(f"tv/{tmdb_id}")
        if not d:
            return None
        def air(name):
            ep = d.get(name) or {}
            return ep.get("air_date")
        return {"status": d.get("status"), "last": air("last_episode_to_air"), "next": air("next_episode_to_air")}

    def find_tv(self, external_id: str, source: str) -> Optional[str]:
        d = self._get(f"find/{external_id}", external_source=source) or {}
        res = d.get("tv_results") or []
        return str(res[0]["id"]) if res else None

    def collection(self, collection_id: int) -> Optional[dict]:
        return self._get(f"collection/{collection_id}")


def map_status(entry: Optional[dict], window_days: int, today: Optional[dt.date] = None) -> str:
    """TMDB status to banner status (Kometa / Agregarr mapping)."""
    if not entry or not entry.get("status"):
        return ""
    s = entry["status"].strip().lower()
    today = today or dt.date.today()
    if s == "returning series":
        def days(d):
            try:
                return (dt.date.fromisoformat(d) - today).days
            except (TypeError, ValueError):
                return None
        last, nxt = days(entry.get("last")), days(entry.get("next"))
        if (last is not None and -window_days <= last <= 0) or (nxt is not None and 0 <= nxt <= window_days):
            return "Airing"
        return "Returning"
    return {"ended": "Ended", "canceled": "Canceled", "cancelled": "Canceled", "planned": "Upcoming",
            "in production": "Upcoming", "pilot": "Upcoming"}.get(s, "")


def emby_status(status: str) -> str:
    return {"continuing": "Returning", "ended": "Ended"}.get((status or "").lower(), "")
