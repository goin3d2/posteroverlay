import json

from app.collections.sync import run_collections
from app.context import Job
from app.settings import Artwork, CollectionDef, ServerConfig, Source
from tests.fakes import FakeEmby, FakePlex, emby_streams, plex_media


def configure(ctx, servers, collections, apply=True, **extra):
    s = ctx.settings.get()
    s.servers = servers
    s.collections = collections
    s.apply_changes = apply
    s.tmdb_key = "k"
    s.mdblist_key = "m"
    for k, v in extra.items():
        setattr(s, k, v)
    ctx.settings.save(s)


def libraries(emby, plex):
    e = {
        "dune": emby.add("Movie", "Dune", "1", {"Imdb": "tt1", "Tmdb": "101"}, emby_streams(), path="/m/Dune.mkv"),
        "dune2": emby.add("Movie", "Dune Part Two", "1", {"Imdb": "tt2", "Tmdb": "102"}, emby_streams(), path="/m/Dune2.mkv"),
        "other": emby.add("Movie", "Other", "1", {"Imdb": "tt9", "Tmdb": "109"}, emby_streams(), path="/m/Other.mkv"),
        "show": emby.add("Series", "Show", "2", {"Imdb": "tt5", "Tmdb": "101"}, path="/tv/Show"),
    }
    p = {
        "dune": plex.add("movie", "Dune", "1", ["imdb://tt1", "tmdb://101"], plex_media()),
        "dune2": plex.add("movie", "Dune Part Two", "1", ["imdb://tt2", "tmdb://102"], plex_media()),
        "show": plex.add("show", "Show", "2", ["imdb://tt5", "tmdb://101"]),
    }
    return e, p


def servers(emby, plex):
    return [ServerConfig(id="e1", kind="emby", name="Emby", url=emby.url, token=FakeEmby.TOKEN),
            ServerConfig(id="p1", kind="plex", name="Plex", url=plex.url, token=FakePlex.TOKEN)]


def boxsets(emby):
    return {i["Name"]: i["Id"] for i in emby.items.values() if i["Type"] == "BoxSet"}


def plex_cols(plex):
    return {(m["title"], str(m["librarySectionID"])): m["ratingKey"] for m in plex.meta.values() if m["type"] == "collection"}


def test_sync_both_servers(ctx, emby, plex, web):
    e, p = libraries(emby, plex)
    web.mdb_lists["lists/me/dune"] = [{"imdb_id": "tt1", "mediatype": "movie"}, {"imdb_id": "tt2", "mediatype": "movie"},
                                      {"tmdb_id": 101, "mediatype": "show"}, {"imdb_id": "tt404", "mediatype": "movie"}]
    col = CollectionDef(id="c1", name="Dune Saga", sources=[Source(type="mdblist", value="https://mdblist.com/lists/me/dune")],
                        artwork=Artwork(mode="Generated"))
    configure(ctx, servers(emby, plex), [col], apply=False)

    res = run_collections(ctx, Job("c"))
    assert "Synced 1 of 1" in res and "preview only" in res
    assert not boxsets(emby) and not plex_cols(plex)

    configure(ctx, servers(emby, plex), [col])
    run_collections(ctx, Job("c"))
    bid = boxsets(emby)["Dune Saga"]
    # TMDB 101 as a show matches the series, not the movie that has the same TMDB number.
    assert sorted(emby.children[bid]) == sorted([e["dune"], e["dune2"], e["show"]])
    cols = plex_cols(plex)
    assert sorted(plex.children[cols[("Dune Saga", "1")]]) == sorted([p["dune"], p["dune2"]])
    assert plex.children[cols[("Dune Saga", "2")]] == [p["show"]]
    assert bid in emby.images   # generated poster uploaded
    posts = sum(1 for c in emby.calls if c[0] == "POST" and c[1].endswith("/Images/Primary"))

    # Second run: no changes, poster not uploaded again.
    run_collections(ctx, Job("c"))
    assert sum(1 for c in emby.calls if c[0] == "POST" and c[1].endswith("/Images/Primary")) == posts
    assert len(boxsets(emby)) == 1 and len(plex_cols(plex)) == 2

    # A title leaves the list: removed from both servers.
    web.mdb_lists["lists/me/dune"] = web.mdb_lists["lists/me/dune"][1:]
    run_collections(ctx, Job("c"))
    assert e["dune"] not in emby.children[bid]
    assert p["dune"] not in plex.children[cols[("Dune Saga", "1")]]


def test_failed_source_never_removes(ctx, emby, plex, web):
    e, p = libraries(emby, plex)
    web.mdb_lists["lists/me/a"] = [{"imdb_id": "tt1"}, {"imdb_id": "tt2"}]
    col = CollectionDef(id="c1", name="Two Sources", artwork=Artwork(mode="None"),
                        sources=[Source(type="mdblist", value="me/a"), Source(type="mdblist", value="me/b")])
    web.mdb_lists["lists/me/b"] = [{"imdb_id": "tt9"}]
    configure(ctx, servers(emby, plex)[:1], [col])
    run_collections(ctx, Job("c"))
    bid = boxsets(emby)["Two Sources"]
    assert len(emby.children[bid]) == 3
    # Source b disappears (404) and a loses a title: nothing is removed.
    del web.mdb_lists["lists/me/b"]
    web.mdb_lists["lists/me/a"] = [{"imdb_id": "tt1"}]
    run_collections(ctx, Job("c"))
    assert len(emby.children[bid]) == 3
    assert any("nothing is removed" in a["message"] for a in ctx.store.activity())


def test_never_emptied_and_only_add(ctx, emby, plex, web):
    e, _ = libraries(emby, plex)
    web.mdb_lists["lists/me/a"] = [{"imdb_id": "tt1"}, {"imdb_id": "tt2"}]
    col = CollectionDef(id="c1", name="Keep", artwork=Artwork(mode="None"), sources=[Source(type="mdblist", value="me/a")])
    configure(ctx, servers(emby, plex)[:1], [col])
    run_collections(ctx, Job("c"))
    bid = boxsets(emby)["Keep"]
    web.mdb_lists["lists/me/a"] = [{"imdb_id": "tt404"}]   # nothing in the library any more
    run_collections(ctx, Job("c"))
    assert len(emby.children[bid]) == 2

    col2 = CollectionDef(id="c1", name="Keep", only_add=True, artwork=Artwork(mode="None"),
                         sources=[Source(type="mdblist", value="me/a")])
    web.mdb_lists["lists/me/a"] = [{"imdb_id": "tt9"}]
    configure(ctx, servers(emby, plex)[:1], [col2])
    run_collections(ctx, Job("c"))
    assert sorted(emby.children[bid]) == sorted([e["dune"], e["dune2"], e["other"]])


def test_adopts_existing_collection(ctx, emby, plex, web):
    e, p = libraries(emby, plex)
    existing = emby.add("BoxSet", "dune saga", "")
    emby.children[existing] = [e["dune"]]
    web.mdb_lists["lists/me/dune"] = [{"imdb_id": "tt1"}, {"imdb_id": "tt2"}]
    col = CollectionDef(id="c1", name="Dune Saga", artwork=Artwork(mode="None"),
                        sources=[Source(type="mdblist", value="me/dune")])
    configure(ctx, servers(emby, plex)[:1], [col])
    run_collections(ctx, Job("c"))
    assert len(boxsets(emby)) == 1
    assert sorted(emby.children[existing]) == sorted([e["dune"], e["dune2"]])


def test_mirror_emby_collection_to_plex(ctx, emby, plex, web):
    e, p = libraries(emby, plex)
    src = emby.add("BoxSet", "Favourites", "")
    emby.children[src] = [e["dune2"], e["show"]]
    col = CollectionDef(id="c1", name="Favourites", servers=["p1"], artwork=Artwork(mode="None"),
                        sources=[Source(type="server_collection", value="e1:Favourites")])
    configure(ctx, servers(emby, plex), [col])
    run_collections(ctx, Job("c"))
    cols = plex_cols(plex)
    assert plex.children[cols[("Favourites", "1")]] == [p["dune2"]]
    assert plex.children[cols[("Favourites", "2")]] == [p["show"]]
    assert emby.children[src] == [e["dune2"], e["show"]]    # source left alone


def test_xtream_source_by_path_and_tmdb(ctx, emby, plex, web, tmp_path):
    e, p = libraries(emby, plex)
    cats = {"movies": {"7": {"name": "Sci-Fi", "items": [{"path": "/vod/Movies/Dune.mkv"}, {"tmdb": "102"}]}}, "series": {}}
    path = tmp_path / "categories.json"
    path.write_text(json.dumps(cats))
    col = CollectionDef(id="c1", name="Sci-Fi", artwork=Artwork(mode="None"), sources=[Source(type="xtream", value="movie:7")])
    srv = servers(emby, plex)
    srv[0].path_map_from, srv[0].path_map_to = "/vod/Movies", "/m"
    configure(ctx, srv, [col], xtream_categories_path=str(path))
    run_collections(ctx, Job("c"))
    assert sorted(emby.children[boxsets(emby)["Sci-Fi"]]) == sorted([e["dune"], e["dune2"]])
    assert plex.children[plex_cols(plex)[("Sci-Fi", "1")]] == [p["dune2"]]   # Plex: TMDB match only


def test_tmdb_collection_and_metadata(ctx, emby, plex, web):
    e, p = libraries(emby, plex)
    web.tmdb_collections["555"] = {"name": "Dune Collection", "parts": [{"id": 101, "title": "Dune"}, {"id": 102}],
                                   "poster_path": "/dune.jpg"}
    from tests.fakes import poster_bytes
    web.images["https://image.tmdb.org/t/p/original/dune.jpg"] = poster_bytes((1, 2, 3))
    col = CollectionDef(id="c1", name="Dune", sort_name="Dune 0", summary="Spice", sort="ReleaseDate",
                        sources=[Source(type="tmdb_collection", value="555")], artwork=Artwork(mode="Auto", tmdb_collection_id=555))
    configure(ctx, servers(emby, plex), [col])
    run_collections(ctx, Job("c"))
    bid = boxsets(emby)["Dune"]
    assert sorted(emby.children[bid]) == sorted([e["dune"], e["dune2"]])     # the show with TMDB 101 is not a movie
    assert emby.items[bid]["ForcedSortName"] == "Dune 0" and emby.items[bid]["Overview"] == "Spice"
    assert emby.items[bid]["DisplayOrder"] == "PremiereDate"
    rk = plex_cols(plex)[("Dune", "1")]
    assert plex.meta[rk]["summary"] == "Spice" and plex.meta[rk]["collectionSort"] == "0"
    assert emby.images[bid] == poster_bytes((1, 2, 3))
    assert ctx.store.collection_maps("c1")[0]["artwork_key"] == "tmdb:/dune.jpg"


def test_label_source(ctx, emby, plex, web):
    e, p = libraries(emby, plex)
    emby.items[e["other"]]["Tags"] = ["Kids"]
    plex.meta[p["dune"]]["Label"] = [{"tag": "Kids"}]
    col = CollectionDef(id="c1", name="Kids", artwork=Artwork(mode="None"), sources=[Source(type="label", value="Kids")])
    configure(ctx, servers(emby, plex), [col])
    run_collections(ctx, Job("c"))
    assert emby.children[boxsets(emby)["Kids"]] == [e["other"]]
    assert plex.children[plex_cols(plex)[("Kids", "1")]] == [p["dune"]]
