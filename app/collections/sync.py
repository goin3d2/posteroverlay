"""Keep each collection definition in sync on every target server.

Safety (same rules as the Emby Collections plugin):
- Only collections this app created or adopted are changed; each is remembered in the database.
- If any source fails, nothing is removed from that collection on that run.
- A collection is never emptied by a sync, and "Add only" collections never lose titles.
- An existing collection with exactly the same name is adopted instead of creating a duplicate.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

from ..context import Context, Job
from ..servers.base import MediaServer, RemoteCollection, ServerError
from ..servers.plex import make_server
from ..settings import CollectionDef, Settings
from . import artwork as art
from .matching import LibraryIndex, match
from .sources import SourceResult, resolve


class CollectionRun:
    def __init__(self, ctx: Context, job: Job, dry_run: bool):
        self.ctx = ctx
        self.job = job
        self.settings: Settings = ctx.settings.get()
        self.dry_run = dry_run or not self.settings.apply_changes
        self.servers: Dict[str, MediaServer] = {
            s.id: make_server(s) for s in self.settings.servers if s.enabled and s.url and s.token}
        self._indexes: Dict[str, LibraryIndex] = {}
        self._remote: Dict[str, List[RemoteCollection]] = {}
        self.kometa = art.Kometa(ctx.config_dir)

    def index(self, sid: str) -> LibraryIndex:
        if sid not in self._indexes:
            cfg = self.settings.server(sid)
            self._indexes[sid] = LibraryIndex(self.servers[sid].items(), cfg.path_map_from, cfg.path_map_to)
        return self._indexes[sid]

    def remote(self, sid: str, refresh: bool = False) -> List[RemoteCollection]:
        if refresh or sid not in self._remote:
            self._remote[sid] = self.servers[sid].collections()
        return self._remote[sid]

    def run(self, only: Optional[str] = None) -> str:
        defs = [c for c in self.settings.collections if c.enabled and (only is None or c.id == only)]
        if only and not defs:
            defs = [c for c in self.settings.collections if c.id == only]
        self.job.phase = "collections"
        self.job.total, self.job.done = len(defs), 0
        self.kometa.refresh(self.ctx.logger("collections"))
        done = 0
        for c in defs:
            if self.job.cancel.is_set():
                break
            self.job.phase = c.name
            try:
                self.sync_one(c)
                done += 1
            except Exception as e:  # noqa: BLE001
                self.ctx.logger(f"collections:{c.id}")(f"{c.name}: {e}", "error")
            self.job.done += 1
        mode = " (preview only: nothing was sent to the servers)" if self.dry_run else ""
        msg = f"Synced {done} of {len(defs)} collections{mode}"
        self.ctx.logger("collections")(msg)
        return msg

    def sync_one(self, c: CollectionDef) -> None:
        log = self.ctx.logger(f"collections:{c.id}")
        targets = [sid for sid in (c.servers or list(self.servers)) if sid in self.servers]
        if not targets:
            log(f"{c.name}: no enabled target server", "warn")
            return

        def resolve_all(sources) -> List[SourceResult]:
            out = []
            for src in sources:
                try:
                    r = resolve(src, self.settings, self.servers, {sid: self.index(sid) for sid in self.servers})
                except Exception as e:  # noqa: BLE001
                    r = SourceResult(failed=True)
                    r.warnings.append(f"Source {src.type.value} '{src.value}' failed: {e}")
                for n in r.notes:
                    log(f"{c.name}: {n}")
                for w in r.warnings:
                    log(f"{c.name}: {w}", "warn")
                out.append(r)
            return out

        results = resolve_all(c.sources)
        excludes = resolve_all(c.exclude)
        any_failed = any(r.failed for r in results + excludes)
        if c.sources and all(r.failed for r in results):
            log(f"{c.name}: every source failed; nothing changed", "error")
            return

        image_cache: Dict[str, tuple] = {}
        for sid in targets:
            srv = self.servers[sid]
            idx = self.index(sid)

            def ids_for(res_list: List[SourceResult]) -> Set[str]:
                out: Set[str] = set()
                for r in res_list:
                    if sid in r.direct:
                        out.update(i for i in r.direct[sid])
                        continue
                    m = match(r.refs, idx)
                    out.update(m.ids)
                return out

            desired = ids_for(results) - ids_for(excludes)
            matched_refs = sum(len(r.refs) for r in results)
            log(f"{c.name} on {srv.name}: {len(desired)} titles in your library"
                + (f" ({matched_refs} listed by the sources)" if matched_refs else ""))
            allow_remove = (not c.only_add and self.settings.remove_missing and not any_failed and bool(desired))
            if any_failed and not c.only_add:
                log(f"{c.name}: a source failed, so nothing is removed this time", "warn")
            try:
                if srv.kind == "plex":
                    sections: Dict[str, Set[str]] = {}
                    for iid in desired:
                        it = idx.by_id.get(iid)
                        if it:
                            sections.setdefault(it.library, set()).add(iid)
                    mapped = {m["section"]: m for m in self.ctx.store.collection_maps(c.id) if m["server"] == sid}
                    for section in set(sections) | set(mapped):
                        self._apply(c, sid, section, sections.get(section, set()), allow_remove, log, image_cache)
                else:
                    self._apply(c, sid, "", desired, allow_remove, log, image_cache)
            except ServerError as e:
                log(f"{c.name} on {srv.name}: {e}", "error")

    def _apply(self, c: CollectionDef, sid: str, section: str, desired: Set[str], allow_remove: bool, log,
               image_cache: Dict[str, tuple]) -> None:
        srv = self.servers[sid]
        store = self.ctx.store
        where = f"{srv.name}" + (f" (library {section})" if section else "")
        row = next((m for m in store.collection_maps(c.id) if m["server"] == sid and m["section"] == section), None)
        remote = [r for r in self.remote(sid) if not section or r.section == section]
        rid = row["remote_id"] if row and any(r.id == row["remote_id"] for r in remote) else None

        if rid is None:
            taken = {m["remote_id"] for other in self.settings.collections if other.id != c.id
                     for m in store.collection_maps(other.id) if m["server"] == sid}
            same = next((r for r in remote if r.name.strip().lower() == c.name.strip().lower() and r.id not in taken), None)
            if same:
                rid = same.id
                log(f"{c.name}: adopted the existing collection on {where}")
                if not self.dry_run:
                    store.set_collection_map(c.id, sid, section, rid)
            elif desired:
                if self.dry_run:
                    log(f"{c.name}: would create the collection on {where} with {len(desired)} titles")
                    return
                rid = srv.create_collection(c.name, sorted(desired), section)
                store.set_collection_map(c.id, sid, section, rid)
                self._remote.pop(sid, None)
                log(f"{c.name}: created on {where} with {len(desired)} titles")
                self._finish(c, sid, section, rid, log, image_cache, desired)
                return
            else:
                return

        members = set(srv.collection_members(rid))
        add = sorted(desired - members)
        remove = sorted(members - desired) if allow_remove else []
        if self.dry_run:
            log(f"{c.name} on {where}: would add {len(add)} and remove {len(remove)} (has {len(members)})")
            return
        if add:
            srv.add_to_collection(rid, add, section)
        if remove:
            srv.remove_from_collection(rid, remove)
        log(f"{c.name} on {where}: added {len(add)}, removed {len(remove)}, now {len(members) + len(add) - len(remove)}")
        self._finish(c, sid, section, rid, log, image_cache, desired)

    def _apply_home(self, c: CollectionDef, sid: str, section: str, rid: str, log) -> None:
        apply_home(self.ctx, self.servers[sid], c, sid, section, rid, log)

    def _finish(self, c: CollectionDef, sid: str, section: str, rid: str, log, image_cache: Dict[str, tuple],
                desired: Set[str]) -> None:
        srv = self.servers[sid]
        try:
            srv.update_collection(rid, c.name, c.sort_name, c.summary, c.sort.value, section)
        except ServerError as e:
            log(f"{c.name}: details not updated on {srv.name}: {e}", "warn")
        self._apply_home(c, sid, section, rid, log)
        if "img" not in image_cache:
            def member_posters() -> List[bytes]:
                out = []
                for iid in sorted(desired)[:9]:
                    try:
                        out.append(srv.get_poster(iid))
                    except ServerError:
                        pass
                return out
            try:
                image_cache["img"] = art.resolve(c, self.settings, self.kometa, member_posters)
            except Exception as e:  # noqa: BLE001
                log(f"{c.name}: poster not set: {e}", "warn")
                image_cache["img"] = (None, "", "")
        data, key, source = image_cache["img"]
        if not data:
            return
        row = next((m for m in self.ctx.store.collection_maps(c.id) if m["server"] == sid and m["section"] == section), None)
        if row and row.get("artwork_key") == key:
            return
        srv.upload_poster(rid, data, "image/png" if data[:4] == b"\x89PNG" else "image/jpeg")
        self.ctx.store.set_collection_map(c.id, sid, section, rid, key)
        log(f"{c.name}: poster from {source} set on {srv.name}")


def home_key(c: CollectionDef, rid: str) -> str:
    import hashlib
    import json
    return hashlib.sha1(json.dumps([rid, c.name, c.home.model_dump()], sort_keys=True).encode()).hexdigest()[:12]


def apply_home(ctx: Context, srv: MediaServer, c: CollectionDef, sid: str, section: str, rid: str, log,
               force: bool = False) -> None:
    """Show or hide the collection on home screens; skipped when nothing changed since the last time."""
    kv = f"home:{c.id}:{sid}:{section}"
    stored = ctx.store.get(kv)
    key = home_key(c, rid)
    if not force and (stored == key or (stored is None and not c.home.enabled)):
        return
    try:
        srv.set_home(rid, section, c.home, c.name)
        ctx.store.put(kv, key)
        where = f"{srv.name}" + (f" (library {section})" if section else "")
        log(f"{c.name}: home screen {'set' if c.home.enabled else 'row removed'} on {where}")
    except ServerError as e:
        log(f"{c.name}: home screen not set on {srv.name}: {e}", "warn")


def run_collections(ctx: Context, job: Job, dry_run: bool = False, only: Optional[str] = None) -> str:
    return CollectionRun(ctx, job, dry_run).run(only)
