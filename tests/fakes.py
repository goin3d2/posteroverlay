"""In-process fake Emby and Plex servers implementing the endpoints the app uses."""
from __future__ import annotations

import base64
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image


def poster_bytes(color=(40, 60, 110), size=(400, 600), text_marker: bytes = b"") -> bytes:
    img = Image.new("RGB", size, color)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


class _Server:
    def __init__(self):
        self.calls = []
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, method):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.calls.append((method, u.path, q))
                status, ctype, data = fake.handle(method, u.path, q, body, self.headers)
                if isinstance(data, (dict, list)):
                    data = json.dumps(data).encode()
                    ctype = "application/json"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in getattr(fake, "_extra_headers", {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

            def do_PUT(self):
                self._go("PUT")

            def do_DELETE(self):
                self._go("DELETE")

        return H


# ---------------------------------------------------------------------------------------------- Emby
class FakeEmby(_Server):
    TOKEN = "emby-token"

    def __init__(self):
        self.items = {}       # id -> dict (Emby item JSON)
        self.images = {}      # id -> bytes
        self.image_ver = {}   # id -> int
        self.children = {}    # boxset id -> list of ids
        self.libs = [{"ItemId": "1", "Name": "Movies", "CollectionType": "movies"},
                     {"ItemId": "2", "Name": "TV shows", "CollectionType": "tvshows"}]
        self.plugins = []
        self.readonly = set()       # items whose poster is a file in a read-only folder
        self.missing = set()        # items whose poster file was deleted
        self.refreshes = []
        self.no_backdrop = set()    # items with no background image
        self.online = {}            # items with no poster that a refresh would download: id -> image
        self.home = {"u1": [{"Id": "s1", "SectionType": "resume"}], "u2": []}   # user -> home sections
        self.next_id = 1000
        super().__init__()

    def nid(self):
        self.next_id += 1
        return str(self.next_id)

    def add(self, type_, name, parent_lib, ids=None, streams=None, path="", **extra):
        iid = extra.pop("Id", None) or self.nid()
        it = {"Id": iid, "Name": name, "Type": type_, "ProviderIds": ids or {}, "Path": path, "_lib": parent_lib,
              "MediaSources": [{"MediaStreams": streams}] if streams is not None else [], **extra}
        self.items[iid] = it
        if type_ in ("Movie", "Series", "Season", "BoxSet"):
            self.set_image(iid, poster_bytes())
        return iid

    def set_image(self, iid, data):
        self.images[iid] = data
        self.image_ver[iid] = self.image_ver.get(iid, 0) + 1

    def tag(self, iid):
        if iid not in self.images:
            return {}
        return {"Primary": f"h{hash(self.images[iid]) & 0xffff:x}_{self.image_ver[iid]}_enh"}

    def view(self, it, fields):
        d = {k: v for k, v in it.items() if not k.startswith("_") and k != "MediaSources"}
        d["ImageTags"] = self.tag(it["Id"])
        if it["Type"] in ("Movie", "Series"):
            d["BackdropImageTags"] = [] if it["Id"] in self.no_backdrop else ["b1"]
        if "MediaSources" in fields:
            d["MediaSources"] = it["MediaSources"]
        if it["Type"] == "BoxSet":
            d["ChildCount"] = len(self.children.get(it["Id"], []))
        return d

    def handle(self, method, path, q, body, headers):
        if headers.get("X-Emby-Token") != self.TOKEN:
            return 401, "text/plain", b"no"
        p = path[len("/emby"):] if path.startswith("/emby") else path
        if p == "/System/Info":
            return 200, "", {"ServerName": "FakeEmby", "Version": "4.11.0.10"}
        if p == "/Library/VirtualFolders":
            return 200, "", self.libs
        if p == "/Plugins":
            return 200, "", self.plugins
        if p == "/Users":
            return 200, "", [{"Id": "u1", "Name": "admin", "Policy": {"IsAdministrator": True}},
                             {"Id": "u2", "Name": "kid", "Policy": {"IsAdministrator": False}}]
        if p.startswith("/Users/") and "/HomeSections" in p:
            uid = p.split("/")[2]
            secs = self.home.setdefault(uid, [])
            if p.endswith("/Delete"):
                ids = json.loads(body)["Ids"]
                self.home[uid] = [s for s in secs if s["Id"] not in ids]
                return 204, "text/plain", b""
            if method == "POST":
                sec = json.loads(body)
                if not sec.get("Id"):
                    sec["Id"] = f"h{self.nid()}"
                self.home[uid] = [s for s in secs if s["Id"] != sec["Id"]] + [sec]
                return 204, "text/plain", b""
            return 200, "", {"Items": secs}
        if p == "/Items" and method == "GET":
            return 200, "", self.query(q)
        if p.startswith("/Users/u1/Items/"):
            iid = p.rsplit("/", 1)[1]
            return 200, "", self.view(self.items[iid], "")
        if p.startswith("/Items/") and p.endswith("/Images/Primary"):
            iid = p.split("/")[2]
            if method == "GET":
                if iid in self.missing:
                    return 404, "text/plain", f"The file '/vod/{iid}/poster.jpg' could not be found.".encode()
                if iid not in self.images:
                    return 404, "text/plain", b"none"
                return 200, "image/jpeg", self.images[iid]
            if iid in self.readonly:
                return 500, "text/plain", f"Access to the path '/vod/{iid}/poster.jpg' is denied.".encode()
            self.set_image(iid, base64.b64decode(body))
            return 204, "text/plain", b""
        if p.startswith("/Items/") and p.endswith("/Refresh") and method == "POST":
            iid = p.split("/")[2]
            self.refreshes.append((iid, q.get("ReplaceAllImages")))
            if q.get("ReplaceAllImages") == "true" and (iid in self.readonly or iid in self.missing):
                self.readonly.discard(iid)                     # now the TMDB poster, kept in Emby's own data
                self.missing.discard(iid)
                self.set_image(iid, poster_bytes((90, 90, 30)))
            if q.get("ReplaceAllImages") == "false" and q.get("ImageRefreshMode") == "FullRefresh":
                if iid in self.online:
                    self.set_image(iid, self.online.pop(iid))
                self.no_backdrop.discard(iid)
            return 204, "text/plain", b""
        if p.startswith("/Items/") and p.endswith("/Images") and method == "GET":
            iid = p.split("/")[2]
            path = f"/vod/{iid}/poster.jpg" if iid in self.readonly or iid in self.missing else "https://image.tmdb.org/t/p/original/x.jpg"
            return 200, "", [{"ImageType": "Primary", "Path": path}] if iid in self.images else []
        if p.startswith("/Items/") and method == "DELETE":
            iid = p.split("/")[2]
            self.items.pop(iid, None)
            self.children.pop(iid, None)
            return 204, "text/plain", b""
        if p.startswith("/Items/") and method == "POST":
            iid = p.split("/")[2]
            upd = json.loads(body)
            for k in ("Name", "ForcedSortName", "Overview", "DisplayOrder"):
                if k in upd:
                    self.items[iid][k] = upd[k]
            return 204, "text/plain", b""
        if p == "/Collections" and method == "POST":
            cid = self.add("BoxSet", q["Name"], "")
            self.children[cid] = [i for i in q.get("Ids", "").split(",") if i]
            return 200, "", {"Id": cid}
        if p.startswith("/Collections/") and p.endswith("/Items"):
            cid = p.split("/")[2]
            ids = [i for i in q.get("Ids", "").split(",") if i]
            cur = self.children.setdefault(cid, [])
            if method == "POST":
                cur.extend(i for i in ids if i not in cur)
            else:
                self.children[cid] = [i for i in cur if i not in ids]
            return 204, "text/plain", b""
        return 404, "text/plain", b"unknown " + p.encode()

    def query(self, q):
        items = list(self.items.values())
        types = set((q.get("IncludeItemTypes") or "").split(",")) - {""}
        if "Ids" in q:
            ids = q["Ids"].split(",")
            items = [self.items[i] for i in ids if i in self.items]
        elif "ParentId" in q:
            pid = q["ParentId"]
            if pid in self.children:
                items = [self.items[i] for i in self.children[pid] if i in self.items]
            elif pid in ("1", "2"):
                items = [i for i in items if i["_lib"] == pid]
            else:
                items = [i for i in items if i.get("SeriesId") == pid or i.get("ParentId") == pid]
        if types:
            items = [i for i in items if i["Type"] in types]
        if q.get("Tags"):
            items = [i for i in items if q["Tags"] in (i.get("Tags") or [])]
        start, limit = int(q.get("StartIndex", 0)), int(q.get("Limit", 100000))
        fields = q.get("Fields", "")
        return {"Items": [self.view(i, fields) for i in items[start:start + limit]], "TotalRecordCount": len(items)}


# ---------------------------------------------------------------------------------------------- Plex
class FakePlex(_Server):
    TOKEN = "plex-token"

    def __init__(self):
        self.meta = {}        # ratingKey -> dict
        self.images = {}      # thumb path -> bytes
        self.children = {}    # collection rk -> [rk]
        self.sections = [{"key": "1", "type": "movie", "title": "Movies"}, {"key": "2", "type": "show", "title": "TV Shows"}]
        self.hubs = {}        # section -> [hub]
        self.next = 100
        super().__init__()

    def nid(self):
        self.next += 1
        return str(self.next)

    def add(self, type_, title, section, guids=None, media=None, **extra):
        rk = extra.pop("ratingKey", None) or self.nid()
        m = {"ratingKey": rk, "type": type_, "title": title, "librarySectionID": int(section) if section else None,
             "Guid": [{"id": g} for g in (guids or [])], "Media": media or [], **extra}
        self.meta[rk] = m
        if type_ in ("movie", "show", "season", "collection"):
            self.set_image(rk, poster_bytes())
        return rk

    def set_image(self, rk, data):
        thumb = f"/library/metadata/{rk}/thumb/{int(time.time() * 1000)}{len(self.images)}"
        self.images[thumb] = data
        self.meta[rk]["thumb"] = thumb

    def listing(self, m, detail=False):
        d = {k: v for k, v in m.items()}
        if not detail:
            d["Media"] = [{k: v for k, v in media.items() if k != "Part"} | {
                "Part": [{"file": p.get("file", "")} for p in media.get("Part") or []]} for media in m.get("Media") or []]
        return d

    def mc(self, items, total=None):
        return {"MediaContainer": {"size": len(items), "totalSize": total if total is not None else len(items),
                                   "Metadata": items}}

    def handle(self, method, path, q, body, headers):
        if headers.get("X-Plex-Token") != self.TOKEN:
            return 401, "text/plain", b"no"
        start = int(headers.get("X-Plex-Container-Start") or 0)
        size = int(headers.get("X-Plex-Container-Size") or 100000)
        if path == "/":
            return 200, "", {"MediaContainer": {"friendlyName": "FakePlex", "version": "1.43"}}
        if path == "/identity":
            return 200, "", {"MediaContainer": {"machineIdentifier": "abc123"}}
        if path == "/library/sections":
            return 200, "", {"MediaContainer": {"Directory": self.sections}}
        if path.startswith("/library/sections/") and path.endswith("/all") and method == "GET":
            sec = path.split("/")[3]
            t = {"1": "movie", "2": "show", "3": "season", "4": "episode"}[q.get("type", "1")]
            items = [m for m in self.meta.values() if str(m.get("librarySectionID")) == sec and m["type"] == t]
            if q.get("label"):
                items = [m for m in items if q["label"] in [l["tag"] for l in m.get("Label") or []]]
            return 200, "", self.mc([self.listing(m) for m in items[start:start + size]], len(items))
        if path.startswith("/library/sections/") and path.endswith("/all") and method == "PUT":
            rk = q["id"]
            if "title.value" in q:
                self.meta[rk]["title"] = q["title.value"]
            if "summary.value" in q:
                self.meta[rk]["summary"] = q["summary.value"]
            return 200, "text/plain", b""
        if path.startswith("/library/sections/") and path.endswith("/collections"):
            sec = path.split("/")[3]
            cols = [m | {"childCount": len(self.children.get(m["ratingKey"], []))} for m in self.meta.values()
                    if m["type"] == "collection" and str(m.get("librarySectionID")) == sec]
            return 200, "", self.mc(cols)
        if path in self.images:
            return 200, "image/jpeg", self.images[path]
        if path == "/library/collections" and method == "POST":
            rk = self.add("collection", q["title"], q["sectionId"], subtype=q["type"])
            self.children[rk] = q["uri"].rsplit("/", 1)[1].split(",")
            return 200, "", self.mc([self.meta[rk]])
        if path.startswith("/library/collections/"):
            parts = path.split("/")
            ck = parts[3]
            if path.endswith("/children"):
                kids = [self.listing(self.meta[r]) for r in self.children.get(ck, []) if r in self.meta]
                return 200, "", self.mc(kids[start:start + size], len(kids))
            if path.endswith("/items") and method == "PUT":
                cur = self.children.setdefault(ck, [])
                cur.extend(r for r in q["uri"].rsplit("/", 1)[1].split(",") if r not in cur)
                return 200, "text/plain", b""
            if len(parts) == 6 and parts[4] == "items" and method == "DELETE":
                self.children[ck] = [r for r in self.children.get(ck, []) if r != parts[5]]
                return 200, "text/plain", b""
        if path.startswith("/hubs/sections/") and "/manage" in path:
            parts = path.split("/")
            sec = parts[3]
            hubs = self.hubs.setdefault(sec, [])
            flags = {k: q[k] for k in ("promotedToRecommended", "promotedToOwnHome", "promotedToSharedHome") if k in q}
            if len(parts) == 5:
                if method == "POST":
                    hubs.append({"identifier": f"custom.collection.{sec}.{q['metadataItemId']}", **flags})
                    return 200, "text/plain", b""
                want = q.get("metadataItemId")
                found = [h for h in hubs if not want or h["identifier"].endswith("." + want)]
                return 200, "", {"MediaContainer": {"Hub": found}}
            ident = parts[5]
            if method == "DELETE":
                self.hubs[sec] = [h for h in hubs if h["identifier"] != ident]
            elif method == "PUT":
                for h in hubs:
                    if h["identifier"] == ident:
                        h.update(flags)
            return 200, "text/plain", b""
        if path.startswith("/library/metadata/"):
            parts = path.split("/")
            rk = parts[3]
            if rk not in self.meta:
                return 404, "text/plain", b"missing"
            if len(parts) == 4 and method == "GET":
                return 200, "", self.mc([self.listing(self.meta[rk], detail=True)])
            if len(parts) == 4 and method == "DELETE":
                self.meta.pop(rk)
                self.children.pop(rk, None)
                return 200, "text/plain", b""
            if parts[4] == "posters" and method == "POST":
                self.set_image(rk, body)
                return 200, "text/plain", b""
            if parts[4] == "prefs":
                self.meta[rk]["collectionSort"] = q.get("collectionSort")
                return 200, "text/plain", b""
            if parts[4] == "children":
                kids = [m for m in self.meta.values() if m.get("parentRatingKey") == rk]
                return 200, "", self.mc(kids)
            if parts[4] == "allLeaves":
                kids = [m for m in self.meta.values() if m.get("grandparentRatingKey") == rk]
                return 200, "", self.mc(kids)
        return 404, "text/plain", ("unknown " + path).encode()


def emby_streams(width=1920, height=1080, hdr="SDR", ext="None", audio="eac3", profile=None, title=None):
    return [{"Type": "Video", "Codec": "hevc", "Width": width, "Height": height, "VideoRange": hdr,
             "ExtendedVideoType": ext, "DisplayTitle": f"{height}p"},
            {"Type": "Audio", "Codec": audio, "Profile": profile, "Title": title, "DisplayTitle": title or audio}]


def plex_media(width=1920, height=1080, dovi=False, trc=None, audio="eac3", audio_title="", file="/m/x.mkv"):
    return [{"id": 1, "width": width, "height": height, "videoResolution": "4k" if width >= 3200 else "1080",
             "audioCodec": audio,
             "Part": [{"file": file, "Stream": [
                 {"streamType": 1, "codec": "hevc", "width": width, "height": height, "DOVIPresent": dovi,
                  "colorTrc": trc, "displayTitle": "4K DoVi" if dovi else ""},
                 {"streamType": 2, "codec": audio, "displayTitle": audio_title, "extendedDisplayTitle": audio_title}]}]}]
