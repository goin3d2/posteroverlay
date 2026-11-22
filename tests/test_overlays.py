import os

from app.context import Job
from app.overlays import restore_all, run_overlays
from app.render.renderer import is_ours, open_image
from app.settings import ServerConfig
from tests.fakes import emby_streams, plex_media, poster_bytes


def setup(ctx, *servers, apply=True, **style):
    s = ctx.settings.get()
    s.servers = list(servers)
    s.apply_changes = apply
    s.tmdb_key = "k"
    s.mdblist_key = "m"
    s.style = type(s.style).model_validate({**s.style.model_dump(mode="json"), **style})
    ctx.settings.save(s)


def emby_library(emby):
    m1 = emby.add("Movie", "Big Movie", "1", {"Imdb": "tt1", "Tmdb": "11"},
                  emby_streams(3840, 2160, "HDR", "DolbyVision", "truehd", title="TrueHD Atmos 7.1"),
                  path="/m/Big.Movie.2160p.mkv", PresentationUniqueKey="big", ProductionYear=2024, PremiereDate="2024-05-01")
    m1b = emby.add("Movie", "Big Movie", "1", {"Imdb": "tt1", "Tmdb": "11"}, emby_streams(1920, 1080),
                   path="/m/Big.Movie.1080p.mkv", PresentationUniqueKey="big")
    m2 = emby.add("Movie", "Old Movie", "1", {"Imdb": "tt2"}, emby_streams(1280, 720, audio="ac3"),
                  path="/m/Old.Movie.720p.mkv", CommunityRating=6.4)
    show = emby.add("Series", "A Show", "2", {"IMDB": "tt3", "Tmdb": "33"}, path="/tv/A Show", Status="Continuing")
    s1 = emby.add("Season", "Season 1", "2", {}, IndexNumber=1, SeriesId=show, ParentId=show)
    s2 = emby.add("Season", "Season 2", "2", {}, IndexNumber=2, SeriesId=show, ParentId=show)
    for season, n, streams in ((1, 3, emby_streams(1920, 1080)), (2, 2, emby_streams(3840, 2160, "HDR", "Hdr10", "eac3", title="Atmos"))):
        for e in range(1, n + 1):
            emby.add("Episode", f"E{e}", "2", {}, streams, path=f"/tv/A Show/S{season}E{e}.mkv", SeriesId=show,
                     ParentIndexNumber=season, IndexNumber=e)
    return m1, m1b, m2, show, s1, s2


def test_emby_overlays_end_to_end(ctx, emby, web):
    m1, m1b, m2, show, s1, s2 = emby_library(emby)
    web.imdb_tsv += "tt1\t8.4\t1000\ntt2\t6.1\t10\ntt3\t9.0\t500\n"
    web.mdb_titles["imdb:tt1"] = [{"source": "tomatoes", "value": 91}, {"source": "popcorn", "value": 70}]
    web.mdb_titles["imdb:tt3"] = [{"source": "tomatoes", "value": 40}]
    web.tmdb_tv["33"] = {"status": "Returning Series", "last_episode_to_air": {"air_date": "2000-01-01"}}
    originals = {k: emby.images[k] for k in (m1, m2, show, s1, s2)}
    srv = ServerConfig(id="e1", kind="emby", name="Emby", url=emby.url, token=FakeEmby.TOKEN)
    setup(ctx, srv, apply=False)

    # Preview only: nothing uploaded.
    res = run_overlays(ctx, "e1", Job("t"))
    assert "would update" in res and "Preview only" in res
    assert all(emby.images[k] == v for k, v in originals.items())
    assert os.path.exists(ctx.path("preview", "e1", f"{m1}.jpg"))

    # Apply.
    setup(ctx, srv, apply=True)
    res = run_overlays(ctx, "e1", Job("t"))
    assert "updated 6 posters" in res, res   # 3 movies (two versions), show, 2 seasons
    for k in (m1, m2, show, s1, s2):
        assert is_ours(emby.images[k])
    # Originals saved untouched.
    row = ctx.store.poster("e1", m1)
    assert open(row["original_file"], "rb").read() == originals[m1]
    # Version of the same title shares the 4K/DV/Atmos badges; IMDb from the dataset beats MDBList.
    assert "res_4k" in row["applied_key"] and "hdr_dv" in row["applied_key"] and "audio_truehd_atmos" in row["applied_key"]
    assert "imdb:8.4" in row["applied_key"] and "rt_critic_fresh:91" in row["applied_key"]
    assert "res_4k" in ctx.store.poster("e1", m1b)["applied_key"]
    # Show: latest season decides quality; status from TMDB (returning, nothing recent).
    show_key = ctx.store.poster("e1", show)["applied_key"]
    assert "RETURNING" in show_key and "res_4k" in show_key and "rt_critic_rotten:40" in show_key
    assert "res_1080p" in ctx.store.poster("e1", s1)["applied_key"]
    assert "res_4k" in ctx.store.poster("e1", s2)["applied_key"]

    # Second run: nothing changes, nothing uploaded.
    uploads_before = sum(1 for c in emby.calls if c[0] == "POST" and c[1].endswith("/Images/Primary"))
    res = run_overlays(ctx, "e1", Job("t"))
    assert "updated 0 posters" in res and "6 unchanged" in res
    assert sum(1 for c in emby.calls if c[0] == "POST" and c[1].endswith("/Images/Primary")) == uploads_before

    # Style change: re-drawn from the saved original (not on top of our own poster).
    setup(ctx, srv, apply=True, banner_position="Bottom")
    res = run_overlays(ctx, "e1", Job("t"))
    assert "updated 6 posters" in res
    assert open(ctx.store.poster("e1", m1)["original_file"], "rb").read() == originals[m1]

    # Someone replaces a poster: the new one becomes the original.
    new_art = poster_bytes((200, 30, 30))
    emby.set_image(m2, new_art)
    run_overlays(ctx, "e1", Job("t"))
    assert open(ctx.store.poster("e1", m2)["original_file"], "rb").read() == new_art
    assert is_ours(emby.images[m2])

    # Restore everything.
    res = restore_all(ctx, "e1", Job("t"))
    assert "6 original posters restored" in res
    assert emby.images[m1] == originals[m1] and emby.images[m2] == new_art and emby.images[show] == originals[show]


def test_emby_never_takes_our_poster_as_original(ctx, emby, web):
    m1, *_ = emby_library(emby)
    srv = ServerConfig(id="e1", kind="emby", name="Emby", url=emby.url, token=FakeEmby.TOKEN)
    setup(ctx, srv)
    run_overlays(ctx, "e1", Job("t"))
    # Lose the database row (e.g. a wiped config) but keep the original file: our poster must not become the original.
    path = ctx.store.poster("e1", m1)["original_file"]
    original = open(path, "rb").read()
    ctx.store.save_poster("e1", m1, applied_marker="lost", original_marker="lost")
    run_overlays(ctx, "e1", Job("t"))
    assert open(path, "rb").read() == original


def test_emby_plugin_warning(ctx, emby, web):
    emby.plugins = [{"Name": "Poster Overlays", "Version": "0.5.3.0"}]
    emby_library(emby)
    setup(ctx, ServerConfig(id="e1", kind="emby", name="Emby", url=emby.url, token=FakeEmby.TOKEN), apply=False)
    run_overlays(ctx, "e1", Job("t"))
    assert any("Poster Overlays plugin" in a["message"] for a in ctx.store.activity())


def test_plex_overlays_end_to_end(ctx, plex, web):
    web.imdb_tsv += "tt1\t7.7\t1\n"
    web.tmdb_tv["33"] = {"status": "Ended"}
    m1 = plex.add("movie", "Big Movie", "1", ["imdb://tt1", "tmdb://11"],
                  plex_media(3840, 2160, dovi=True, audio="truehd", audio_title="English (TRUEHD 7.1 Atmos)"))
    show = plex.add("show", "A Show", "2", ["tmdb://33", "imdb://tt3"])
    s1 = plex.add("season", "Season 1", "2", [], parentRatingKey=show, index=1)
    for e in (1, 2):
        plex.add("episode", f"E{e}", "2", [], plex_media(1920, 1080, audio="eac3", file=f"/tv/A/S1E{e}.mkv"),
                 grandparentRatingKey=show, parentRatingKey=s1, parentIndex=1, index=e)
    original = plex.images[plex.meta[m1]["thumb"]]
    setup(ctx, ServerConfig(id="p1", kind="plex", name="Plex", url=plex.url, token=FakePlex.TOKEN))
    res = run_overlays(ctx, "p1", Job("t"))
    assert "updated 3 posters" in res, res
    key = ctx.store.poster("p1", m1)["applied_key"]
    assert "res_4k" in key and "hdr_dv" in key and "audio_truehd_atmos" in key and "imdb:7.7" in key
    assert "ENDED" in ctx.store.poster("p1", show)["applied_key"]
    assert is_ours(plex.images[plex.meta[m1]["thumb"]])
    assert "unchanged" in run_overlays(ctx, "p1", Job("t"))
    assert "updated 0 posters" in run_overlays(ctx, "p1", Job("t"))
    restore_all(ctx, "p1", Job("t"))
    assert plex.images[plex.meta[m1]["thumb"]] == original


def test_only_selected_items(ctx, emby, web):
    m1, m1b, m2, *_ = emby_library(emby)
    setup(ctx, ServerConfig(id="e1", kind="emby", name="Emby", url=emby.url, token=FakeEmby.TOKEN))
    res = run_overlays(ctx, "e1", Job("t"), only=[m2])
    assert "updated 1 posters" in res
    assert is_ours(emby.images[m2]) and not is_ours(emby.images[m1])


from tests.fakes import FakeEmby, FakePlex  # noqa: E402


def test_plex_draws_on_emby_originals(ctx, emby, plex, web):
    from PIL import Image
    import io
    clean = poster_bytes((10, 200, 10), size=(2000, 3000))
    em = emby.add("Movie", "Big Movie", "1", {"Imdb": "tt1"}, emby_streams(), path="/m/Big.mkv")
    emby.set_image(em, clean)
    web.tmdb_tv["33"] = {"status": "Ended"}
    eshow = emby.add("Series", "A Show", "2", {"Tvdb": "77", "Tmdb": "33"}, path="/tv/A Show")
    es1 = emby.add("Season", "Season 1", "2", {}, IndexNumber=1, SeriesId=eshow, ParentId=eshow)
    emby.set_image(es1, poster_bytes((0, 0, 220)))
    # Plex's own poster already has someone else's badges on it.
    pm = plex.add("movie", "Big Movie", "1", ["imdb://tt1"], plex_media())
    plex.set_image(pm, poster_bytes((255, 0, 255)))
    pshow = plex.add("show", "A Show", "2", ["tvdb://77", "tmdb://33"])
    ps1 = plex.add("season", "Season 1", "2", [], parentRatingKey=pshow, index=1)
    lone = plex.add("movie", "Only On Plex", "1", ["imdb://tt8"], plex_media())
    lone_art = plex.images[plex.meta[lone]["thumb"]]
    e = ServerConfig(id="e1", kind="emby", name="Emby", url=emby.url, token=FakeEmby.TOKEN)
    p = ServerConfig(id="p1", kind="plex", name="Plex", url=plex.url, token=FakePlex.TOKEN, originals_from="e1")
    setup(ctx, e, p)
    res = run_overlays(ctx, "p1", Job("t"))
    assert "drawn on Emby's original" in res, res
    row = ctx.store.poster("p1", pm)
    # Plex shares Emby's original file (shrunk to 1000 px) instead of keeping its own copy.
    assert row["original_file"] == ctx.store.poster("e1", em)["original_file"]
    assert Image.open(row["original_file"]).width == 1000
    drawn = Image.open(io.BytesIO(plex.images[plex.meta[pm]["thumb"]])).convert("RGB")
    assert drawn.width == 1000 and drawn.getpixel((500, 1400))[1] > 150      # green Emby art, not the magenta one
    sdrawn = Image.open(io.BytesIO(plex.images[plex.meta[ps1]["thumb"]])).convert("RGB")
    assert sdrawn.getpixel((300, 500))[2] > 150                                  # season matched through its show
    assert ctx.store.poster("p1", lone)["original_file"].endswith(f"p1/{lone}.img")
    # Emby's own run later reuses the same original and does not treat it as changed.
    run_overlays(ctx, "e1", Job("t"))
    assert open(ctx.store.poster("e1", em)["original_file"], "rb").read()[:2] == b"\xff\xd8"
    assert is_ours(emby.images[em])
    # Restoring Plex puts Emby's clean art there (the old badged Plex poster is not wanted back).
    restore_all(ctx, "p1", Job("t"))
    assert Image.open(io.BytesIO(plex.images[plex.meta[pm]["thumb"]])).convert("RGB").getpixel((500, 1400))[1] > 150
    assert plex.images[plex.meta[lone]["thumb"]] == lone_art


def test_skip_paths(ctx, emby, web):
    m1, m1b, m2, *_ = emby_library(emby)
    vod = emby.add("Movie", "Vod Movie", "1", {"Imdb": "tt7"}, emby_streams(), path="/vod/Movies/x.strm")
    srv = ServerConfig(id="e1", kind="emby", name="Emby", url=emby.url, token=FakeEmby.TOKEN, skip_paths=["/vod"])
    setup(ctx, srv)
    run_overlays(ctx, "e1", Job("t"))
    assert not is_ours(emby.images[vod]) and is_ours(emby.images[m2])


def test_preview_draws_only_a_sample(ctx, emby, web, monkeypatch):
    import app.overlays as ov
    monkeypatch.setattr(ov, "PREVIEW_LIMIT", 2)
    emby_library(emby)
    setup(ctx, ServerConfig(id="e1", kind="emby", name="Emby", url=emby.url, token=FakeEmby.TOKEN), apply=False)
    res = run_overlays(ctx, "e1", Job("t"))
    assert "would update 6 posters" in res and "2 sample posters" in res, res
    assert len(os.listdir(ctx.path("preview", "e1", "x")[:-2])) == 2
    assert len(os.listdir(os.path.join(ctx.config_dir, "originals", "e1"))) == 2


def test_mdblist_limited_per_run(ctx, emby, web):
    emby_library(emby)
    for t in ("tt1", "tt2", "tt3"):
        web.mdb_titles[f"imdb:{t}"] = [{"source": "tomatoes", "value": 80}]
    srv = ServerConfig(id="e1", kind="emby", name="Emby", url=emby.url, token=FakeEmby.TOKEN)
    setup(ctx, srv, apply=False)
    s = ctx.settings.get(); s.mdblist_per_run = 2; ctx.settings.save(s)
    run_overlays(ctx, "e1", Job("t"))
    assert ctx.store.get("meta:mdblist_waiting:e1") == 1
    run_overlays(ctx, "e1", Job("t"))
    assert ctx.store.get("meta:mdblist_waiting:e1") == 0
