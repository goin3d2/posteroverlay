import gzip
import io
import json
import os
import sys

import pytest
import requests
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import context as context_mod  # noqa: E402
from app.media import ratings  # noqa: E402
from app.render.icons import SOURCE_PATHS  # noqa: E402
from tests.fakes import FakeEmby, FakePlex  # noqa: E402


class FakeWeb(requests.adapters.BaseAdapter):
    """Answers TMDB, MDBList, IMDb, GitHub and image URLs from dictionaries."""

    def __init__(self):
        super().__init__()
        self.tmdb_tv = {}         # tmdb id -> {"status":..., "last_episode_to_air":..., ...}
        self.tmdb_collections = {}
        self.mdb_titles = {}      # "imdb:tt1" -> ratings list
        self.mdb_lists = {}       # list path -> items
        self.mdb_quota = (25000, 0)
        self.imdb_tsv = "tconst\taverageRating\tnumVotes\n"
        self.images = {}
        self.calls = []

    def send(self, request, **kwargs):
        from urllib.parse import parse_qs, urlparse
        u = urlparse(request.url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append(request.url)
        status, body, headers = 404, b"{}", {}
        if u.netloc == "api.themoviedb.org":
            parts = u.path.split("/")
            if parts[2] == "tv" and parts[3] in self.tmdb_tv:
                status, body = 200, json.dumps(self.tmdb_tv[parts[3]]).encode()
            elif parts[2] == "collection" and parts[3] in self.tmdb_collections:
                status, body = 200, json.dumps(self.tmdb_collections[parts[3]]).encode()
            elif parts[2] == "find":
                status, body = 200, b'{"tv_results": []}'
        elif u.netloc == "api.mdblist.com":
            path = u.path.strip("/")
            if path == "user":
                status, body = 200, json.dumps({"api_requests": self.mdb_quota[0], "api_requests_count": self.mdb_quota[1]}).encode()
            elif path.endswith("/items"):
                lp = path[: -len("/items")]
                if lp in self.mdb_lists:
                    status, body = 200, json.dumps(self.mdb_lists[lp]).encode()
            else:
                _, prov, pid = path.split("/")[-3:] if path.count("/") >= 2 else ("", "", "")
                key = path.split("/")[0] + ":" + path.split("/")[-1]
                if key in self.mdb_titles:
                    status, body = 200, json.dumps({"ratings": self.mdb_titles[key]}).encode()
        elif u.netloc == "datasets.imdbws.com":
            status, body = 200, gzip.compress(self.imdb_tsv.encode())
        elif u.netloc == "api.github.com":
            status, body = 503, b"{}"
        elif request.url in self.images:
            status, body = 200, self.images[request.url]
        r = requests.Response()
        r.status_code, r._content, r.url = status, body, request.url
        r.headers.update(headers)
        r.request = request
        return r

    def close(self):
        pass


def _icon(color):
    img = Image.new("RGBA", (200, 100), color)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def web(monkeypatch):
    fw = FakeWeb()
    s = requests.Session()
    s.mount("http://", fw)
    s.mount("https://", fw)
    # Every outside call goes through the fake; local fake servers are reached through a real session.
    monkeypatch.setattr(ratings, "session", s)
    real_get = requests.get

    def fake_get(url, *a, **kw):
        if url.startswith("http://127.0.0.1"):
            return real_get(url, *a, **kw)
        return s.get(url, *a, **kw)
    monkeypatch.setattr(requests, "get", fake_get)
    ratings._scale = 0
    return fw


@pytest.fixture
def ctx(tmp_path, web):
    c = context_mod.Context(str(tmp_path / "config"))
    for i, key in enumerate(SOURCE_PATHS):
        with open(os.path.join(c.icons.folder, key + ".png"), "wb") as f:
            f.write(_icon((50 + i * 8, 120, 200, 255)))
    return c


@pytest.fixture
def emby():
    e = FakeEmby()
    yield e
    e.close()


@pytest.fixture
def plex():
    p = FakePlex()
    yield p
    p.close()
