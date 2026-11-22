"""Web UI and API, plus the scheduler that runs overlays and collection syncs."""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw

from . import __version__
from .collections import artwork as art
from .collections.sources import CATEGORY_CANDIDATES, find_categories, xtream_categories
from .collections.sync import run_collections
from .context import Context, Job
from .overlays import restore_all, run_overlays
from .render import renderer
from .render import spec as specs
from .servers.base import ServerError
from .servers.plex import make_server
from .settings import CollectionDef, ServerConfig, Settings, Style

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
MASK = "••••••••"

ctx = Context()
_stop = threading.Event()


@asynccontextmanager
async def lifespan(_app):
    if os.environ.get("DISABLE_SCHEDULER") != "1":
        threading.Thread(target=scheduler, args=(_stop,), daemon=True, name="scheduler").start()
    ctx.logger("app")(f"Poster Overlay {__version__} started")
    yield
    _stop.set()


app = FastAPI(title="Poster Overlay", version=__version__, lifespan=lifespan)


# --------------------------------------------------------------------------------------- settings
def _masked(s: Settings) -> dict:
    d = s.model_dump(mode="json")
    for srv in d["servers"]:
        srv["token"] = MASK if srv["token"] else ""
    for k in ("tmdb_key", "mdblist_key"):
        d[k] = MASK if d[k] else ""
    return d


def _unmask(new: Settings, old: Settings) -> Settings:
    for srv in new.servers:
        if srv.token == MASK:
            prev = old.server(srv.id)
            srv.token = prev.token if prev else ""
    for k in ("tmdb_key", "mdblist_key"):
        if getattr(new, k) == MASK:
            setattr(new, k, getattr(old, k))
    return new


@app.get("/api/settings")
def get_settings():
    return _masked(ctx.settings.get())


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    old = ctx.settings.get()
    body.setdefault("collections", [c.model_dump(mode="json") for c in old.collections])
    try:
        new = Settings.model_validate(body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    if new.apply_changes != old.apply_changes:
        ctx.logger("settings")("Apply changes to servers turned " + ("ON" if new.apply_changes else "off"))
    return _masked(ctx.settings.save(_unmask(new, old)))


# ---------------------------------------------------------------------------------------- servers
@app.post("/api/servers/test")
def test_server(body: dict = Body(...)):
    cfg = ServerConfig.model_validate(body)
    if cfg.token == MASK:
        prev = ctx.settings.get().server(cfg.id)
        cfg.token = prev.token if prev else ""
    srv = make_server(cfg)
    try:
        info = srv.test()
        libs = [{"id": lib.id, "name": lib.name, "kind": lib.kind} for lib in srv.libraries()]
        return {"ok": True, "message": info, "libraries": libs, "warnings": srv.warnings()}
    except ServerError as e:
        return {"ok": False, "message": str(e), "libraries": [], "warnings": []}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Could not reach the server: {e}", "libraries": [], "warnings": []}


def _server(sid: str):
    cfg = ctx.settings.get().server(sid)
    if not cfg:
        raise HTTPException(404, "No such server")
    return make_server(cfg)


@app.get("/api/servers/{sid}/collections")
def server_collections(sid: str):
    try:
        return [{"id": c.id, "name": c.name, "section": c.section, "count": c.count} for c in _server(sid).collections()]
    except ServerError as e:
        raise HTTPException(502, str(e))


# ------------------------------------------------------------------------------------------- jobs
def _overlay_job(server_ids: List[str], only: Optional[List[str]] = None):
    def fn(job: Job) -> str:
        out = []
        for sid in server_ids:
            if job.cancel.is_set():
                break
            out.append(run_overlays(ctx, sid, job, only=only))
        return "\n".join(out)
    return fn


def _enabled_ids() -> List[str]:
    return [s.id for s in ctx.settings.get().servers if s.enabled and s.url and s.token]


@app.post("/api/jobs/overlays")
def start_overlays(server: Optional[str] = None):
    ids = [server] if server else _enabled_ids()
    if not ids:
        raise HTTPException(400, "No server is set up yet")
    return {"started": ctx.start("overlays", _overlay_job(ids))}


@app.post("/api/jobs/restore")
def start_restore(server: str):
    return {"started": ctx.start("restore", lambda job: restore_all(ctx, server, job))}


@app.post("/api/jobs/collections")
def start_collections(only: Optional[str] = None):
    return {"started": ctx.start("collections", lambda job: run_collections(ctx, job, only=only))}


@app.post("/api/jobs/icons")
def start_icons(force: bool = True):
    def fn(job: Job) -> str:
        job.phase = "downloading icons"
        ok, bad = ctx.icons.download(ctx.settings.get().icon_source, force, ctx.logger("icons"))
        return f"{ok} icons ready, {bad} failed"
    return {"started": ctx.start("icons", fn)}


@app.post("/api/jobs/{name}/cancel")
def cancel(name: str):
    job = ctx.jobs.get(name)
    if job and job.running:
        job.cancel.set()
    return {"ok": True}


@app.get("/api/status")
def status():
    s = ctx.settings.get()
    counts = ctx.store.poster_counts()
    have, total = ctx.icons.count()
    return {
        "version": __version__,
        "apply_changes": s.apply_changes,
        "jobs": {k: j.as_dict() for k, j in ctx.jobs.items()},
        "servers": [{"id": x.id, "name": x.name or x.kind, "kind": x.kind.value, "enabled": x.enabled,
                     "posters": counts.get(x.id, {})} for x in s.servers],
        "icons": {"have": have, "total": total},
        "mdblist_quota": ctx.store.get("meta:mdblist_quota"),
        "mdblist_waiting": sum(int(v or 0) for v in ctx.store.get_prefix("meta:mdblist_waiting:").values()),
        "imdb_titles": ctx.store.count_prefix("imdb:"),
        "schedule": {
            "overlays": {"enabled": s.overlays_enabled, "hours": s.overlay_hours,
                         "last": ctx.store.get("sched:overlays")},
            "collections": {"enabled": s.collections_enabled, "hours": s.collection_hours,
                            "last": ctx.store.get("sched:collections")},
        },
        "collections": len(s.collections),
    }


@app.get("/api/activity")
def activity(after: int = 0, scope: Optional[str] = None, limit: int = Query(300, le=2000)):
    return ctx.store.activity(after=after, scope=scope, limit=limit)


# ---------------------------------------------------------------------------------------- preview
SAMPLE = {
    "movie": {"resolution": "res_4k", "hdr": "hdr_dv", "audio": "audio_truehd_atmos", "imdb": 8.1, "rt_critic": 92,
              "rt_audience": 85, "tmdb": 7.9},
    "show": {"status": "Airing", "resolution": "res_1080p", "hdr": "hdr_hdr10", "audio": "audio_plus_atmos",
             "imdb": 7.4, "rt_critic": 54, "rt_audience": 71, "tmdb": 7.2},
}


def _sample_poster() -> Image.Image:
    img = Image.new("RGB", (1000, 1500))
    d = ImageDraw.Draw(img)
    for y in range(1500):
        t = y / 1499
        d.line([(0, y), (1000, y)], fill=(round(40 + 50 * t), round(70 + 20 * t), round(120 - 60 * t)))
    d.ellipse((250, 450, 750, 950), fill=(220, 150, 70))
    d.rectangle((0, 1100, 1000, 1500), fill=(25, 30, 45))
    return img


@app.post("/api/preview")
def preview(body: dict = Body(...)):
    """Render with an unsaved style: {style, kind: movie|show|season, status?, server?, item?}."""
    style = Style.model_validate(body.get("style") or ctx.settings.get().style.model_dump()).normalized()
    kind = body.get("kind") or "movie"
    data = dict(SAMPLE["movie" if kind == "movie" else "show"])
    if body.get("status"):
        data["status"] = body["status"]
    poster = None
    sid, item = body.get("server"), body.get("item")
    if sid and item:
        row = ctx.store.poster(sid, item) or {}
        path = row.get("original_file")
        try:
            raw = open(path, "rb").read() if path and os.path.exists(path) else _server(sid).get_poster(item)
            poster = renderer.open_image(raw)
        except Exception:  # noqa: BLE001
            poster = None
    spec = specs.build(data, kind, style)
    out = renderer.render(poster or _sample_poster(), spec, ctx.icons.get)
    if out.width > 700:
        out = out.resize((700, round(out.height * 700 / out.width)), Image.LANCZOS)
    buf = io.BytesIO()
    out.convert("RGB").save(buf, "JPEG", quality=88)
    return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/previews/{sid}")
def preview_list(sid: str):
    folder = os.path.join(ctx.config_dir, "preview", sid)
    if not os.path.isdir(folder):
        return []
    names = {r["item"]: r.get("title") or "" for r in ctx.store.posters(sid)}
    files = sorted(os.scandir(folder), key=lambda e: -e.stat().st_mtime)[:200]
    return [{"item": e.name[:-4], "title": names.get(e.name[:-4], ""), "time": e.stat().st_mtime} for e in files
            if e.name.endswith(".jpg")]


@app.get("/api/previews/{sid}/{item}.jpg")
def preview_file(sid: str, item: str):
    path = os.path.join(ctx.config_dir, "preview", os.path.basename(sid), os.path.basename(item) + ".jpg")
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------------------------ collections
@app.get("/api/collections")
def list_collections():
    s = ctx.settings.get()
    out = []
    for c in s.collections:
        d = c.model_dump(mode="json")
        d["mapped"] = ctx.store.collection_maps(c.id)
        out.append(d)
    return out


def _save_collection(c: CollectionDef):
    s = ctx.settings.get()
    s.collections = [x for x in s.collections if x.id != c.id] + [c] if not any(x.id == c.id for x in s.collections) \
        else [c if x.id == c.id else x for x in s.collections]
    ctx.settings.save(s)
    return c.model_dump(mode="json")


@app.post("/api/collections")
def create_collection(body: dict = Body(...)):
    body.pop("id", None)
    try:
        return _save_collection(CollectionDef.model_validate(body))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@app.put("/api/collections/{cid}")
def update_collection(cid: str, body: dict = Body(...)):
    body["id"] = cid
    try:
        return _save_collection(CollectionDef.model_validate(body))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@app.delete("/api/collections/{cid}")
def delete_collection(cid: str, remove_from_servers: bool = False):
    s = ctx.settings.get()
    c = next((x for x in s.collections if x.id == cid), None)
    if not c:
        raise HTTPException(404)
    removed = 0
    if remove_from_servers and s.apply_changes:
        for m in ctx.store.collection_maps(cid):
            cfg = s.server(m["server"])
            if cfg:
                try:
                    make_server(cfg).delete_collection(m["remote_id"])
                    removed += 1
                except ServerError as e:
                    ctx.logger(f"collections:{cid}")(f"{c.name}: not deleted on {cfg.name}: {e}", "warn")
    ctx.store.delete_collection_map(cid)
    s.collections = [x for x in s.collections if x.id != cid]
    ctx.settings.save(s)
    ctx.logger("collections")(f"Removed '{c.name}'" + (f" and deleted it on {removed} server(s)" if removed else ""))
    return {"ok": True, "deleted_on_servers": removed}


@app.get("/api/collections/{cid}/artwork")
def collection_artwork(cid: str):
    s = ctx.settings.get()
    c = next((x for x in s.collections if x.id == cid), None)
    if not c:
        raise HTTPException(404)
    try:
        data, _, source = art.resolve(c, s, art.Kometa(ctx.config_dir), lambda: [])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    if not data:
        raise HTTPException(404, "Poster left as is")
    return Response(data, media_type="image/png" if data[:4] == b"\x89PNG" else "image/jpeg",
                    headers={"X-Source": source, "Cache-Control": "no-store"})


@app.get("/api/xtream/categories")
def xtream():
    path = ctx.settings.get().xtream_categories_path
    found = find_categories(path)
    return {"path": path, "found": found or "", "looked_in": [path] + [c for c in CATEGORY_CANDIDATES if c != path],
            "categories": xtream_categories(path)}


@app.get("/api/kometa/search")
def kometa_search(q: str, limit: int = 24):
    return [{"path": p, "url": art.KOMETA_RAW + p} for p in art.Kometa(ctx.config_dir).search(q, limit)]


# -------------------------------------------------------------------------------------- scheduler
def _due(key: str, hours: int) -> bool:
    last = ctx.store.get(key) or 0
    return hours > 0 and time.time() - float(last) >= hours * 3600


def scheduler(stop: threading.Event) -> None:
    time.sleep(int(os.environ.get("SCHEDULER_DELAY", "90")))
    while not stop.is_set():
        try:
            s = ctx.settings.get()
            # Nothing runs by itself in preview mode: a scheduled preview of a whole library only fills the disk.
            if not s.apply_changes:
                stop.wait(60)
                continue
            if s.overlays_enabled and _due("sched:overlays", s.overlay_hours) and _enabled_ids():
                if ctx.start("overlays", _overlay_job(_enabled_ids())):
                    ctx.store.put("sched:overlays", time.time())
            if s.collections_enabled and s.collections and _due("sched:collections", s.collection_hours) \
                    and not ctx.job("overlays").running:
                if ctx.start("collections", lambda job: run_collections(ctx, job)):
                    ctx.store.put("sched:collections", time.time())
        except Exception:  # noqa: BLE001
            logging.getLogger("posteroverlay").exception("scheduler")
        stop.wait(60)


@app.post("/api/import/emby-plugin")
async def import_emby_plugin(request: Request):
    """Body: the plugin's overlay-data.json (Emby config folder, plugins/PosterOverlays/)."""
    import json as _json
    from .importer import import_plugin_data
    raw = await request.body()
    try:
        data = _json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise HTTPException(400, "That is not the plugin's overlay-data.json")
    if not isinstance(data, dict) or "Ratings" not in data:
        raise HTTPException(400, "That is not the plugin's overlay-data.json")
    counts = import_plugin_data(data, ctx.store)
    msg = (f"Imported from the Emby plugin: {counts['ratings']} ratings, {counts['statuses']} show statuses "
           f"({counts['written']} newer than what was here)")
    ctx.logger("import")(msg)
    return {**counts, "message": msg}


@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")


@app.exception_handler(ServerError)
def _server_error(_, exc: ServerError):
    return JSONResponse({"detail": str(exc)}, status_code=502)
