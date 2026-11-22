# Poster Overlay

One container that gives your **Emby and Plex** servers the same posters and the same collections.

- **Poster overlays**: a status banner on shows (Airing, Returning, Ended, Canceled, Upcoming, from TMDB) and badges for resolution, HDR / Dolby Vision, audio (Atmos, TrueHD, DTS-X…), IMDb, Rotten Tomatoes and TMDB, in a single column or as separate badges. This is the look from the Emby *Poster Overlays* plugin, drawn the same way on both servers.
- **Collections**: each collection is defined once. Members can come from MDBList lists (including IMDb lists added to MDBList), TMDB collections, xtreamfs categories, a collection that already exists on one of your servers, or a tag/label. Each collection is then kept in sync on every server you choose, with its sort title, summary, order and poster (a URL, a Kometa default image, the TMDB collection poster, or one generated here). This replaces the Emby *Collections* plugin.

A web UI on port **8095** lets you set the look with a live preview, manage collections, run jobs and read the activity log. Posters update on a schedule (every 6 hours by default) and collections sync every 12 hours.

## Safety

- **Preview only is the default.** A run works everything out, counts what would change and draws a sample of 200 posters per server in `/config/preview/`, but nothing is sent to Emby or Plex until you turn on *Settings → Apply changes to servers*. Scheduled runs only start once changes are applied.
- **Originals are kept.** Before the first overlay, the untouched poster is saved under `/config/originals/`. Style changes are always redrawn from that original, never on top of an old overlay. If you (or a metadata refresh) put a new poster on a title, the new one becomes the original.
- Every poster written carries a hidden marker, so one of our own posters is never mistaken for an original.
- **Servers → Restore original posters** puts every original back.
- **Collections:** only collections created or adopted here are ever changed. If any source fails, nothing is removed on that run. A sync never empties a collection. *Add only* collections never lose titles. A collection that already exists with the same name is adopted instead of being duplicated.

## Install

### Unraid

Add the template from `unraid/posteroverlay.xml` (*Docker → Add Container → Template*), or fill it in yourself:

| | |
|---|---|
| Repository | `ghcr.io/goin3d2/posteroverlay:latest` |
| Port | `8095` |
| `/config` | `/mnt/user/appdata/posteroverlay` |
| `/xtreamfs` (optional, read only) | your xtreamfs config folder, the one with `categories.json` |

### Docker compose

See `docker-compose.yml`.

## Setting up

1. **Servers** tab: add Emby (address plus an API key from *Dashboard → Advanced → API Keys*) and/or Plex (address plus your Plex token). Press **Test and list libraries** to pick libraries; leave them all ticked to use every movie and TV library.
2. **Settings**: add a TMDB key (show status, TMDB collections) and an MDBList key (Rotten Tomatoes, MDBList lists). IMDb ratings come free from IMDb's daily dataset.
3. **Posters**: set the look. The preview updates as you change things.
4. **Status → Update posters**: check the previews (*Posters → Preview of the last run*).
5. When you're happy, turn on **Apply changes to servers**.

### Finding your Plex token

Open any item in Plex Web, choose *⋯ → Get Info → View XML*, and copy the `X-Plex-Token=` value from the address bar.

### Moving over from the Emby plugins

- **Poster Overlays plugin:** disable or uninstall it before turning on *Apply changes*, or both will draw on Emby's posters. The container warns you if the plugin is still installed. If the plugin already changed posters, use its *Restore originals* first, so the container keeps the real originals.
- **Collections plugin:** add the same collections here. Collections with the same name are adopted, so nothing is duplicated. Then disable the plugin's scheduled sync.

### xtreamfs

Mount xtreamfs's config folder at `/xtreamfs`. Its categories then appear in the collection editor. Titles are matched by TMDB id, or by path when the path xtreamfs wrote is how Emby sees the file. If Emby sees the `.strm` files under a different path, set the path mapping on the Emby server.

## How it works

| | Emby | Plex |
|---|---|---|
| Titles, files, streams | `/Items` in bulk | `/library/sections/*/all` plus item details |
| Poster upload | `POST /Items/{id}/Images/Primary` | `POST /library/metadata/{id}/posters` |
| Collections | BoxSets | one collection per library |

The data is gathered once and shared between servers: IMDb ratings (dataset), Rotten Tomatoes (MDBList, budgeted against your daily quota) and show status (TMDB, cached).

## Development

```
pip install -r requirements-dev.txt
python -m pytest -q                      # runs against in-process fake Emby and Plex servers
CONFIG_DIR=./config uvicorn app.main:app --port 8095
```

Every push to `main` runs the tests and publishes `ghcr.io/goin3d2/posteroverlay:latest` plus `:<version>` (from `app/__init__.py`).
