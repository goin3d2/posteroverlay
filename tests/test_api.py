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
