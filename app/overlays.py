"""The poster pipeline for one server: gather data, draw, upload, and keep the untouched originals.

How originals are kept safe:
- Before the first change to a poster, the current image is downloaded (for Emby with image enhancers off) and saved
  under config/originals/<server>/. Every later drawing starts from that file, never from a poster we made.
- Each upload is remembered with the server's image marker (Emby image tag, Plex thumb). If the marker later differs,
  someone changed the poster (new artwork, metadata refresh); that image becomes the new original.
- Every JPEG we write carries a "posteroverlay" comment, so even if the markers are lost an image of ours is never
  saved as an original.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from .context import Context, Job
from .media import classify as qc
from .media.ratings import MdbList, QuotaExhausted, Tmdb, emby_status, map_status, refresh_imdb
from .render import renderer, spec as specs
from .servers.base import Item, MediaServer, ServerError
from .servers.plex import make_server
from .settings import QualityRule, ServerConfig, Settings

EPISODE_SAMPLE = 10
LIST_WORKERS = 6
PREVIEW_LIMIT = 200
DAY = 86400
MISSING_IMAGES_RETRY = 7 * DAY
MISSING_IMAGES_PACE = 0.4   # seconds between refresh requests, so the server isn't swamped


def _ratings_key(item: Item, kind: str) -> Optional[Tuple[str, str, str]]:
    if item.ids.get("imdb"):
        return f"mdb:{kind}:imdb:{item.ids['imdb']}", "imdb", item.ids["imdb"]
    if item.ids.get("tmdb"):
        return f"mdb:{kind}:tmdb:{item.ids['tmdb']}", "tmdb", item.ids["tmdb"]
    return None


class OverlayRun:
    def __init__(self, ctx: Context, cfg: ServerConfig, job: Job, dry_run: bool, only: Optional[List[str]] = None):
        self.ctx = ctx
        self.cfg = cfg
        self.job = job
        self.settings: Settings = ctx.settings.get()
        self.style = self.settings.style.normalized()
        self.dry_run = dry_run or not self.settings.apply_changes
        self.only = set(only or [])
        self.log = ctx.logger(f"overlays:{cfg.id}")
        self.server: MediaServer = make_server(cfg)
        self.store = ctx.store
        self.counts = defaultdict(int)
        self._count_lock = threading.Lock()
        self.max_width = max(400, int(self.settings.original_max_width or 1000))
        self.by_id: Dict[str, Item] = {}
        # Another server whose originals we draw on (same title matched by ids).
        src = self.settings.server(cfg.originals_from) if cfg.originals_from and cfg.originals_from != cfg.id else None
        self.source_cfg = src if src and src.enabled and src.url and src.token else None
        self.source: Optional[MediaServer] = make_server(self.source_cfg) if self.source_cfg else None
        self._source_index: Optional[Dict[tuple, Item]] = None
        self._index_lock = threading.Lock()

    # ------------------------------------------------------------------ data
    def gather(self) -> Tuple[List[Item], List[Item], Dict[str, dict]]:
        """Items to draw on and each one's overlay data."""
        job, s = self.job, self.style
        job.phase = "listing titles"
        items = self.server.items()
        movies = [i for i in items if i.kind == "movie"]
        shows = [i for i in items if i.kind == "show"]
        seasons = self.server.all_seasons() if (s.seasons or s.series) else []
        if self.cfg.skip_paths:
            skip = tuple(self.cfg.skip_paths)
            before = len(movies) + len(shows)
            movies = [m for m in movies if not (m.path or "").startswith(skip)]
            shows = [sh for sh in shows if not (sh.path or "").startswith(skip)]
            kept = {sh.id for sh in shows}
            seasons = [se for se in seasons if se.parent in kept]
            self.log(f"{self.server.name}: {before - len(movies) - len(shows)} titles skipped by folder")
        # Only gather data for what will be drawn (Posters → Which posters).
        if not s.movies:
            movies = []
        if not (s.series or s.seasons):
            shows, seasons = [], []
        if not s.seasons and not s.series:
            seasons = []
        self.by_id = {i.id: i for i in movies + shows + seasons}
        sampled = self.dry_run and not self.only
        if sampled:
            # A preview only gathers data for a spread of sample titles, so it takes minutes on any library.
            n_shows = min(len(shows), PREVIEW_LIMIT // 3) if movies else min(len(shows), PREVIEW_LIMIT)
            n_movies = min(len(movies), PREVIEW_LIMIT - n_shows)
            pick = movies[::max(1, len(movies) // max(1, n_movies))][:n_movies] + \
                shows[::max(1, len(shows) // max(1, n_shows))][:n_shows]
            self.only = {i.id for i in pick}
            self.log(f"Preview: {len(movies) + len(shows)} titles in the library; checking a sample of {len(pick)} "
                     "(and their seasons)")
        if self.only:
            keep = set(self.only) | {se.parent for se in seasons if se.id in self.only}
            vkeys = {m.version_key for m in movies if m.id in keep and m.version_key}
            movies = [m for m in movies if m.id in keep or (m.version_key and m.version_key in vkeys)]
            shows = [sh for sh in shows if sh.id in keep]
            seasons = [se for se in seasons if se.parent in keep]
            if sampled:
                self.only = {i.id for i in movies + shows + seasons}
        self.log(f"{self.server.name}: {len(movies)} movies, {len(shows)} shows, {len(seasons)} seasons")

        self._missing_images(movies, shows, seasons)
        self._ratings(movies, shows)
        status = self._statuses(shows)
        quality = self._quality(movies, shows)

        data: Dict[str, dict] = {}
        for it in movies + shows:
            d: dict = dict(quality.get(it.id, {}))
            kind = "movie" if it.kind == "movie" else "show"
            if it.ids.get("imdb"):
                imdb = self.store.get(f"imdb:{it.ids['imdb']}")
                if imdb:
                    d["imdb"] = imdb
            rk = _ratings_key(it, kind)
            if rk:
                r = self.store.get(rk[0]) or {}
                if not r.get("not_found"):
                    d.setdefault("imdb", r.get("imdb"))
                    if r.get("rt_critic") is not None:
                        d["rt_critic"] = r["rt_critic"]
                    if r.get("rt_audience") is not None:
                        d["rt_audience"] = r["rt_audience"]
            if it.community_rating and 0 < float(it.community_rating) <= 10:
                d["tmdb"] = round(float(it.community_rating), 1)
            if it.kind == "show":
                d["status"] = status.get(it.id, "")
            data[it.id] = {k: v for k, v in d.items() if v is not None}

        show_by_id = {sh.id: sh for sh in shows}
        for se in seasons:
            parent = data.get(se.parent)
            if parent is None:
                continue
            d = {k: v for k, v in parent.items() if k not in ("resolution", "hdr", "audio")}
            d.update(quality.get(f"season:{se.parent}:{se.index}", {}))
            data[se.id] = d
            se.title = f"{show_by_id[se.parent].title} – {se.title}" if se.parent in show_by_id else se.title

        targets = ([m for m in movies] if s.movies else []) + ([sh for sh in shows] if s.series else []) + \
                  ([se for se in seasons if se.id in data] if s.seasons else [])
        if self.only:
            targets = [t for t in targets if t.id in self.only]
        return targets, items, data

    def _ratings(self, movies: List[Item], shows: List[Item]) -> None:
        st = self.settings
        style = self.style
        if style.show_imdb and st.imdb_dataset:
            wanted = {i.ids["imdb"] for i in movies + shows if i.ids.get("imdb")}
            try:
                self.job.phase = "IMDb ratings"
                refresh_imdb(self.store, self.ctx.config_dir, wanted, self.log)
            except Exception as e:  # noqa: BLE001
                self.log(f"IMDb ratings file could not be updated: {e}", "warn")
        need_mdb = style.show_rt_critic or style.show_rt_audience or (style.show_imdb and not st.imdb_dataset)
        if not need_mdb:
            return
        mdb = MdbList(st.mdblist_key, self.store, st.mdblist_reserve, self.log)
        if not mdb.configured:
            self.log("No MDBList key: Rotten Tomatoes badges are skipped")
            return
        today = dt.date.today()
        queue = []
        seen = set()
        for it, kind in [(m, "movie") for m in movies] + [(s, "show") for s in shows]:
            rk = _ratings_key(it, kind)
            if not rk or rk[0] in seen:
                continue
            seen.add(rk[0])
            age = self.store.age(rk[0])
            recent = False
            try:
                recent = bool(it.premiere) and (today - dt.date.fromisoformat(it.premiere)).days < 365
            except ValueError:
                pass
            max_age = (3 if recent else 60) * DAY
            if age is None or age > max_age:
                queue.append((age is None, it.premiere or "", -(age or 0), rk, kind))
        if not queue:
            return
        # Titles with no ratings first (newest first), then the stalest refreshes.
        queue.sort(key=lambda q: (not q[0], "" if not q[0] else q[1]), reverse=False)
        missing = sorted([q for q in queue if q[0]], key=lambda q: q[1], reverse=True)
        stale = sorted([q for q in queue if not q[0]], key=lambda q: q[2], reverse=True)
        queue = missing + stale
        budget = mdb.start()
        per_run = int(st.mdblist_per_run or 0)
        if per_run and budget > per_run:
            budget = mdb.budget = per_run
            self.log(f"MDBList: up to {per_run} titles this run; the rest are fetched on later runs")
        self.job.phase = "Rotten Tomatoes ratings"
        self.job.total, self.job.done = min(len(queue), budget), 0
        fetched = 0
        for _, _, _, rk, kind in queue:
            if self.job.cancel.is_set():
                break
            try:
                r = mdb.ratings(kind, rk[1], rk[2])
            except QuotaExhausted:
                self.log("MDBList: daily allowance used; the rest continue on a later run")
                break
            except RuntimeError as e:
                self.log(str(e), "error")
                break
            if r is not None:
                self.store.put(rk[0], r)
                fetched += 1
            self.job.done += 1
        self.log(f"MDBList: {fetched} titles updated in {mdb.calls} calls, {len(queue) - fetched} still waiting")
        self.store.put(f"meta:mdblist_waiting:{self.cfg.id}", len(queue) - fetched)

    def _missing_images(self, movies: List[Item], shows: List[Item], seasons: List[Item]) -> None:
        """Titles with no poster (or no background) that the server could find online: ask it to fetch them.
        The server downloads in the background, so these get their overlay on the next run."""
        limit = int(self.settings.missing_images_per_run or 0)
        if self.dry_run or self.only or limit <= 0 or not self.server.can_fetch_images:
            return
        has_ids = {sh.id for sh in shows if sh.ids}
        wanted = [it for it in movies + shows if it.ids and (not it.marker or it.has_backdrop is False)]
        wanted += [se for se in seasons if not se.marker and se.parent in has_ids]
        if not wanted:
            return
        prefix = f"imgfetch:{self.cfg.id}:"
        asked = self.store.get_prefix(prefix)
        now = time.time()
        due = [it for it in wanted if now - float(asked.get(it.id) or 0) > MISSING_IMAGES_RETRY]
        waiting = len(wanted) - len(due)
        if not due:
            self.log(f"{len(wanted)} titles have no poster or background; all were asked for in the last week")
            return
        # Movies and shows first (seasons fall back to the show's poster).
        due.sort(key=lambda it: it.kind == "season")
        batch = due[:limit]
        job = self.job
        job.phase = "fetching missing posters and backgrounds"
        job.total, job.done = len(batch), 0
        sent, errors = 0, 0
        for it in batch:
            if job.cancel.is_set():
                break
            try:
                self.server.fetch_missing_images(it.id)
                sent += 1
                errors = 0
            except ServerError as e:
                errors += 1
                if errors >= 20:
                    self.log(f"Asking {self.server.name} for missing images stopped after 20 errors in a row: {e}",
                             "warn")
                    break
            self.store.put(prefix + it.id, now)
            job.done += 1
            time.sleep(MISSING_IMAGES_PACE)
        left = len(due) - sent
        self.log(f"{self.server.name}: {sent} titles with no poster or background were refreshed online; "
                 f"{left} left for later runs, {waiting} asked for in the last week. They get their overlays on the "
                 "next run")

    def _statuses(self, shows: List[Item]) -> Dict[str, str]:
        tmdb = Tmdb(self.settings.tmdb_key)
        out: Dict[str, str] = {}
        if not shows or not (self.style.series or self.style.seasons) or not self.style.banner:
            return out
        self.job.phase = "series status"
        self.job.total, self.job.done = len(shows), 0
        calls = 0
        for sh in shows:
            self.job.done += 1
            fallback = emby_status(sh.status)
            if not tmdb.configured:
                out[sh.id] = fallback
                continue
            tid = sh.ids.get("tmdb")
            if not tid:
                for src, key in (("tvdb_id", "tvdb"), ("imdb_id", "imdb")):
                    ext = sh.ids.get(key)
                    if not ext:
                        continue
                    cache = f"tmdbfind:{key}:{ext}"
                    found = self.store.get(cache)
                    if found is None:
                        try:
                            found = tmdb.find_tv(ext, src) or ""
                            calls += 1
                        except Exception as e:  # noqa: BLE001
                            self.log(f"TMDB lookup failed for {sh.title}: {e}", "warn")
                            found = None
                        if found is not None:
                            self.store.put(cache, found)
                    if found:
                        tid = found
                        break
            if not tid:
                out[sh.id] = fallback
                continue
            cache = f"tmdbstatus:{tid}"
            entry = self.store.get(cache)
            age = self.store.age(cache)
            settled = entry and (entry.get("status") or "").lower() in ("ended", "canceled")
            if entry is None or age is None or age > (14 * DAY if settled else 20 * 3600):
                try:
                    fresh = tmdb.series_status(tid)
                    calls += 1
                    if fresh:
                        entry = fresh
                        self.store.put(cache, fresh)
                except RuntimeError as e:
                    self.log(str(e), "error")
                    tmdb.key = ""
                except Exception as e:  # noqa: BLE001
                    self.log(f"TMDB status failed for {sh.title}: {e}", "warn")
            out[sh.id] = map_status(entry, self.style.airing_window_days) or fallback
        if calls:
            self.log(f"TMDB: {calls} calls")
        return out

    def _quality(self, movies: List[Item], shows: List[Item]) -> Dict[str, dict]:
        """Quality per movie, show and season ("season:<show>:<n>"), cached per file path."""
        s = self.style
        out: Dict[str, dict] = {}
        if not (s.show_resolution or s.show_hdr or s.show_audio):
            return out
        prefix = f"q:{self.cfg.id}:"
        cache = self.store.get_prefix(prefix)
        fresh: Dict[str, dict] = {}

        def file_key(item: Item) -> str:
            return f"{item.id}|{item.path}"

        # Episodes: only the ones that decide a badge (latest seasons / recent episodes).
        episodes_by_show: Dict[str, List[Item]] = defaultdict(list)
        if shows and (s.show_quality_on_shows or s.seasons):
            self.job.phase = "listing episodes"
            self.job.total, self.job.done = len(shows), 0
            # Show by show, several at a time: deep pages of one huge episode listing get slower and slower.
            def list_eps(sh: Item) -> None:
                if self.job.cancel.is_set():
                    return
                try:
                    # Keep only the episodes that decide a badge (newest EPISODE_SAMPLE per season): holding every
                    # episode of a large library at once takes gigabytes.
                    per: Dict[int, List[Item]] = defaultdict(list)
                    for e in self.server.episodes_of(sh):
                        per[e.season if e.season is not None else -1].append(e)
                    episodes_by_show[sh.id] = [e for lst in per.values()
                                               for e in sorted(lst, key=lambda e: e.index or 0, reverse=True)[:EPISODE_SAMPLE]]
                except ServerError as e:
                    self.log(f"{sh.title}: episodes not listed: {e}", "warn")
                self.job.done += 1
            with ThreadPoolExecutor(LIST_WORKERS, thread_name_prefix="episodes") as pool:
                list(pool.map(list_eps, shows))
        wanted_eps: Dict[str, List[Item]] = {}
        for sh in shows:
            eps = episodes_by_show.get(sh.id, [])
            by_season: Dict[int, List[Item]] = defaultdict(list)
            for e in eps:
                by_season[e.season if e.season is not None else -1].append(e)
            for n in by_season:
                by_season[n] = sorted(by_season[n], key=lambda e: e.index or 0, reverse=True)[:EPISODE_SAMPLE]
            wanted_eps[sh.id] = [e for lst in by_season.values() for e in lst]
            sh._by_season = by_season  # type: ignore[attr-defined]

        need = [m for m in movies if file_key(m) not in cache] + \
               [e for lst in wanted_eps.values() for e in lst if file_key(e) not in cache]
        if need:
            self.job.phase = "reading media info"
            self.job.total, self.job.done = len(need), 0
            lock = threading.Lock()

            def read(chunk: List[Item]) -> None:
                if self.job.cancel.is_set():
                    return
                try:
                    streams = self.server.file_streams([c.id for c in chunk])
                except ServerError as e:
                    self.log(str(e), "warn")
                    streams = {}
                got = {}
                for c in chunk:
                    files = streams.get(c.id)
                    if files is not None:
                        got[prefix + file_key(c)] = qc.to_dict(qc.summarize([qc.classify(f, c.path) for f in files]))
                with lock:
                    fresh.update(got)
                    self.job.done += len(chunk)
                    if len(fresh) >= 1000:
                        self.store.put_many(fresh)
                        cache.update({k[len(prefix):]: v for k, v in fresh.items()})
                        fresh.clear()

            chunks = [need[i:i + 100] for i in range(0, len(need), 100)]
            with ThreadPoolExecutor(LIST_WORKERS, thread_name_prefix="mediainfo") as pool:
                list(pool.map(read, chunks))
            if fresh:
                self.store.put_many(fresh)
                cache.update({k[len(prefix):]: v for k, v in fresh.items()})

        def q_of(item: Item) -> qc.Quality:
            return qc.from_dict(cache.get(file_key(item)))

        # Movies: versions of one title (Emby keeps each as its own item) share the best quality.
        groups: Dict[str, List[Item]] = defaultdict(list)
        for m in movies:
            groups[m.version_key or m.ids.get("imdb") or m.id].append(m)
        for group in groups.values():
            best = qc.summarize([q_of(m) for m in group])
            for m in group:
                out[m.id] = qc.to_dict(best)

        most = s.quality_rule == QualityRule.most
        for sh in shows:
            by_season = getattr(sh, "_by_season", {})
            for n, eps in by_season.items():
                out[f"season:{sh.id}:{n}"] = qc.to_dict(qc.summarize(self._per_episode(eps, q_of), most))
            if not by_season:
                continue
            if s.quality_rule == QualityRule.latest_season:
                real = [n for n in by_season if n > 0] or list(by_season)
                sample = by_season[max(real)]
            else:
                sample = sorted([e for lst in by_season.values() for e in lst], key=lambda e: (e.season or 0, e.index or 0),
                                reverse=True)[:EPISODE_SAMPLE * 2]
            out[sh.id] = qc.to_dict(qc.summarize(self._per_episode(sample, q_of), most))
        return out

    @staticmethod
    def _per_episode(eps: List[Item], q_of) -> List[qc.Quality]:
        """Versions of one episode count once (their best)."""
        groups: Dict[str, List[Item]] = defaultdict(list)
        for e in eps:
            groups[f"{e.season}:{e.index}" if e.index is not None else e.id].append(e)
        return [qc.summarize([q_of(e) for e in g]) for g in groups.values()]

    # ------------------------------------------------------------------ drawing
    def apply(self, targets: List[Item], data: Dict[str, dict]) -> str:
        job = self.job
        style_key = specs.style_hash(self.style, self.ctx.icons.generation())
        job.phase = "posters (preview only)" if self.dry_run else "posters"
        job.total, job.done = len(targets), 0
        # Preview runs draw a spread of PREVIEW_LIMIT posters to look at and only count the rest.
        drawn: set = set()
        if self.dry_run:
            step = max(1, len(targets) // PREVIEW_LIMIT)
            drawn = {t.id for t in targets[::step][:PREVIEW_LIMIT]}
        lock = threading.Lock()

        def work(it: Item) -> None:
            if job.cancel.is_set():
                return
            kind = {"movie": "movie", "show": "show", "season": "season"}[it.kind]
            spec = specs.build(data.get(it.id), kind, self.style)
            key = f"{style_key}|{spec.content_key()}" if not spec.empty else ""
            try:
                self._one(it, spec, key, draw=not self.dry_run or it.id in drawn)
            except ServerError as e:
                self._count("failed")
                self.log(f"{it.title}: {e}", "warn")
            except Exception as e:  # noqa: BLE001
                self._count("failed")
                self.log(f"{it.title}: {type(e).__name__}: {e}", "warn")
            with lock:
                job.done += 1

        workers = max(1, min(16, int(self.settings.overlay_workers or 1)))
        if workers == 1:
            for it in targets:
                work(it)
        else:
            with ThreadPoolExecutor(workers, thread_name_prefix="poster") as pool:
                list(pool.map(work, targets))
        if job.cancel.is_set():
            self.log("Stopped")
        c = self.counts
        verb = "would update" if self.dry_run else "updated"
        summary = (f"{self.server.name}: {verb} {c['updated']} posters, {c['unchanged']} unchanged, "
                   f"{c['restored']} restored to the original, {c['skipped']} skipped, {c['failed']} failed")
        if self.source_cfg:
            summary += f"; {c['from_source']} drawn on {self.source_cfg.name}'s original"
        if self.server.missing_fixed:
            summary += (f"; {self.server.missing_fixed} posters pointed at a file that's gone (e.g. a removed xtreamfs "
                        "poster.jpg), so Emby fetched them online first")
        if self.server.readonly_fixed:
            summary += (f"; {self.server.readonly_fixed} posters were read-only files (e.g. xtreamfs poster.jpg), so "
                        "their images were refreshed first")
        if self.dry_run:
            summary += (f". Preview only: nothing was sent to the server; {len(drawn)} sample posters drawn in preview/ "
                        "(turn on \"Apply changes to servers\").")
        self.log(summary)
        return summary

    def _build_source_index(self) -> None:
        self.log(f"Reading {self.source_cfg.name} titles to use its original posters")
        idx: Dict[tuple, Item] = {}
        items = self.source.items()
        for x in items:
            for prov in ("imdb", "tmdb", "tvdb"):
                if x.ids.get(prov):
                    idx.setdefault((x.kind, prov, x.ids[prov]), x)
        try:
            for se in self.source.all_seasons():
                if se.index is not None:
                    idx.setdefault(("season", se.parent, se.index), se)
        except ServerError as e:
            self.log(f"{self.source_cfg.name} seasons not read: {e}", "warn")
        self._source_index = idx

    def _match_source(self, it: Item) -> Optional[Item]:
        """The same title (or season) on the source server, matched by IMDb/TMDB/TVDB id."""
        with self._index_lock:
            if self._source_index is None:
                self._build_source_index()
        idx = self._source_index
        if it.kind == "season":
            show = self.by_id.get(it.parent)
            src_show = self._match_source(show) if show else None
            return idx.get(("season", src_show.id, it.index)) if src_show and it.index is not None else None
        for prov in ("imdb", "tmdb", "tvdb"):
            if it.ids.get(prov) and (it.kind, prov, it.ids[prov]) in idx:
                return idx[(it.kind, prov, it.ids[prov])]
        return None

    def _from_source(self, it: Item) -> Optional[Tuple[bytes, str]]:
        """Original poster of the matching title on the source server, shared with that server's own originals."""
        try:
            m = self._match_source(it)
        except ServerError as e:
            self.log(f"{self.source_cfg.name}: {e}", "warn")
            self.source = None
            return None
        if m is None:
            return None
        sid = self.source_cfg.id
        srow = self.store.poster(sid, m.id) or {}
        spath = srow.get("original_file") or ""
        if spath and os.path.exists(spath):
            with open(spath, "rb") as f:
                return f.read(), spath
        try:
            data = self.source.get_poster(m.id)
        except ServerError:
            return None
        if renderer.is_ours(data):
            return None
        data = _shrink(data, self.max_width)
        spath = self.ctx.path("originals", sid, f"{m.id}.img")
        with open(spath, "wb") as f:
            f.write(data)
        self.store.save_poster(sid, m.id, title=m.title, original_file=spath, original_marker=m.marker)
        return data, spath

    def _original_path(self, item_id: str) -> str:
        return self.ctx.path("originals", self.cfg.id, f"{item_id}.img")

    def _count(self, name: str) -> None:
        with self._count_lock:
            self.counts[name] += 1

    def _one(self, it: Item, spec: specs.Spec, key: str, draw: bool = True) -> None:
        row = self.store.poster(self.cfg.id, it.id) or {}
        ours_now = row.get("applied_marker") and it.marker == row.get("applied_marker")

        if not key:
            # Nothing to draw: put the original back if we changed this poster.
            if row.get("applied_key") and ours_now and row.get("original_file") and os.path.exists(row["original_file"]):
                if not self.dry_run:
                    with open(row["original_file"], "rb") as f:
                        marker = self.server.upload_poster(it.id, f.read(), _ctype(row["original_file"]))
                    self.store.save_poster(self.cfg.id, it.id, applied_key="", applied_marker=marker,
                                           original_marker=marker)
                self._count("restored")
            else:
                self._count("unchanged")
            return

        if ours_now and row.get("applied_key") == key:
            self._count("unchanged")
            return
        if not draw:
            self._count("updated")
            return

        original = None
        orig_path = row.get("original_file") or ""
        if self.source is not None:
            got = self._from_source(it)
            if got:
                original, src_path = got
                if src_path != orig_path:
                    orig_path = src_path
                    self.store.save_poster(self.cfg.id, it.id, title=it.title, original_file=src_path,
                                           original_marker=it.marker)
                self._count("from_source")
        if original is not None:
            pass
        elif orig_path and os.path.exists(orig_path) and (ours_now or it.marker == row.get("original_marker")):
            with open(orig_path, "rb") as f:
                original = f.read()
        else:
            if not it.marker:
                self._count("skipped")
                return
            current = self.server.get_poster(it.id)
            if renderer.is_ours(current):
                if orig_path and os.path.exists(orig_path):
                    with open(orig_path, "rb") as f:
                        original = f.read()
                else:
                    self.log(f"{it.title}: the poster on the server is one of ours but the original is missing; skipped",
                             "warn")
                    self._count("skipped")
                    return
            else:
                original = _shrink(current, self.max_width)
                orig_path = self._original_path(it.id)
                with open(orig_path, "wb") as f:
                    f.write(original)
                self.store.save_poster(self.cfg.id, it.id, title=it.title, original_file=orig_path,
                                       original_marker=it.marker)

        poster = renderer.open_image(original)
        if poster.width > self.max_width:
            poster = poster.resize((self.max_width, round(poster.height * self.max_width / poster.width)))
        out = renderer.render(poster, spec, self.ctx.icons.get)
        jpeg = renderer.to_jpeg(out, key)
        if self.dry_run:
            with open(self.ctx.path("preview", self.cfg.id, f"{it.id}.jpg"), "wb") as f:
                f.write(jpeg)
        else:
            marker = self.server.upload_poster(it.id, jpeg)
            self.store.save_poster(self.cfg.id, it.id, title=it.title, applied_key=key, applied_marker=marker)
        self._count("updated")


def _shrink(data: bytes, max_width: int) -> bytes:
    """Keep originals no wider than max_width (posters are never shown bigger); small ones are kept as they are."""
    import io
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        if im.width <= max_width:
            return data
        im = im.convert("RGB").resize((max_width, round(im.height * max_width / im.width)), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return data


def _ctype(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(8)
    return "image/png" if head.startswith(b"\x89PNG") else "image/jpeg"


def run_overlays(ctx: Context, server_id: str, job: Job, dry_run: bool = False, only: Optional[List[str]] = None) -> str:
    cfg = ctx.settings.get().server(server_id)
    if not cfg:
        raise ValueError(f"No server {server_id}")
    run = OverlayRun(ctx, cfg, job, dry_run, only)
    for w in run.server.warnings():
        run.log(w, "warn")
    if ctx.icons.count()[0] == 0:
        ctx.icons.download(run.settings.icon_source, False, run.log)
    targets, _, data = run.gather()
    return run.apply(targets, data)


def restore_all(ctx: Context, server_id: str, job: Job) -> str:
    """Put every original poster back and forget what was applied."""
    cfg = ctx.settings.get().server(server_id)
    if not cfg:
        raise ValueError(f"No server {server_id}")
    log = ctx.logger(f"overlays:{server_id}")
    server = make_server(cfg)
    rows = [r for r in ctx.store.posters(server_id) if r.get("applied_key")]
    job.phase, job.total, job.done = "restoring originals", len(rows), 0
    restored = failed = 0
    for r in rows:
        if job.cancel.is_set():
            break
        job.done += 1
        path = r.get("original_file")
        if not path or not os.path.exists(path):
            failed += 1
            continue
        try:
            with open(path, "rb") as f:
                marker = server.upload_poster(r["item"], f.read(), _ctype(path))
            ctx.store.save_poster(server_id, r["item"], applied_key="", applied_marker=marker, original_marker=marker)
            restored += 1
        except ServerError as e:
            failed += 1
            log(f"{r.get('title') or r['item']}: {e}", "warn")
    msg = f"{server.name}: {restored} original posters restored, {failed} failed"
    log(msg)
    return msg
