import importlib
import os

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeEmby


@pytest.fixture
def client(tmp_path, monkeypatch, web):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    import app.context
    monkeypatch.setattr(app.context, "CONFIG_DIR", str(tmp_path / "cfg"))
    import app.main
    main = importlib.reload(app.main)
    with TestClient(main.app) as c:
        yield c, main


def test_settings_tokens_masked_and_kept(client, emby):
    c, main = client
    s = c.get("/api/settings").json()
    assert s["apply_changes"] is False
    s["servers"] = [{"id": "e1", "kind": "emby", "name": "Emby", "url": emby.url + "/", "token": FakeEmby.TOKEN}]
    s["tmdb_key"] = "secret"
    out = c.put("/api/settings", json=s).json()
    assert out["servers"][0]["token"] == main.MASK and out["tmdb_key"] == main.MASK
    assert out["servers"][0]["url"] == emby.url
    # Saving the masked values back keeps the real ones.
    c.put("/api/settings", json=out)
    real = main.ctx.settings.get()
    assert real.servers[0].token == FakeEmby.TOKEN and real.tmdb_key == "secret"
    t = c.post("/api/servers/test", json=out["servers"][0]).json()
    assert t["ok"] and {l["name"] for l in t["libraries"]} == {"Movies", "TV shows"}
    bad = c.post("/api/servers/test", json={**out["servers"][0], "token": "nope"}).json()
    assert not bad["ok"]


def test_collections_crud_and_preview(client):
    c, _ = client
    made = c.post("/api/collections", json={"name": "Marvel", "sources": [{"type": "mdblist", "value": "me/marvel"}]}).json()
    assert made["id"] and made["artwork"]["mode"] == "Auto"
    made["only_add"] = True
    c.put(f"/api/collections/{made['id']}", json=made)
    lst = c.get("/api/collections").json()
    assert len(lst) == 1 and lst[0]["only_add"] and lst[0]["mapped"] == []
    # Settings saves must not drop collections.
    s = c.get("/api/settings").json()
    s.pop("collections")
    c.put("/api/settings", json=s)
    assert len(c.get("/api/collections").json()) == 1
    assert c.delete(f"/api/collections/{made['id']}").json()["ok"]
    assert c.get("/api/collections").json() == []

    r = c.post("/api/preview", json={"kind": "show", "status": "Ended"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and len(r.content) > 5000
    st = c.get("/api/status").json()
    assert st["apply_changes"] is False and "overlays" in st["schedule"]
    assert c.get("/").status_code == 200 and "Poster Overlay" in c.get("/").text


def test_import_emby_plugin_cache(client):
    import json as _json
    c, main = client
    plugin = {"Version": 1, "Items": {},
              "Ratings": {"movie:imdb:tt1": {"Imdb": 7.6, "RtCritic": 92, "RtAudience": None, "NotFound": False,
                                             "FetchedUtc": "2026-11-20T14:50:59.3008466Z"},
                          "show:tmdb:5": {"NotFound": True, "FetchedUtc": "2026-11-01T00:00:00Z"}},
              "Status": {"217198": {"TmdbStatus": "Returning Series", "LastAirDate": "2023-01-27T00:00:00Z",
                                    "FetchedUtc": "2026-11-21T20:41:08.63Z"}},
              "TmdbIdLookups": {"imdb:tt9": "123"}}
    main.ctx.store.put("mdb:show:tmdb:5", {"rt_critic": 50})      # newer than the plugin's entry: kept
    r = c.post("/api/import/emby-plugin", content=("﻿" + _json.dumps(plugin)).encode())
    assert r.status_code == 200, r.text
    assert r.json()["ratings"] == 2 and r.json()["statuses"] == 1
    st = main.ctx.store
    assert st.get("mdb:movie:imdb:tt1") == {"imdb": 7.6, "rt_critic": 92}
    assert st.get("mdb:show:tmdb:5") == {"rt_critic": 50}
    assert st.get("tmdbstatus:217198") == {"status": "Returning Series", "last": "2023-01-27", "next": ""}
    assert st.get("tmdbfind:imdb:tt9") == "123"
    assert 1.5 * 86400 < st.age("mdb:movie:imdb:tt1")       # keeps the plugin's fetch date
    assert c.post("/api/import/emby-plugin", content=b"{}").status_code == 400
