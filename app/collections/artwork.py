"""Collection posters: a URL, a Kometa default image, a TMDB collection poster, or one drawn here."""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import time
from typing import Callable, List, Optional, Tuple

import requests
from PIL import Image, ImageDraw, ImageFilter

from ..media.ratings import Tmdb
from ..render.renderer import font
from ..settings import ArtworkMode, CollectionDef, Settings

KOMETA_RAW = "https://raw.githubusercontent.com/Kometa-Team/Default-Images/master/"
BUNDLED = os.path.join(os.path.dirname(__file__), "kometa-images.txt.gz")
PREFERRED = ["streaming/color/", "network/color/", "studio/", "franchise/", "universe/", "genre/", "seasonal/",
             "decade/", "year/", "country/color/", "chart/color/", "content_rating/", "award/"]
W, H = 1000, 1500


def norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum() or ch in "+&")


class Kometa:
    def __init__(self, config_dir: str):
        self.cache = os.path.join(config_dir, "kometa-images.txt")
        self._paths: Optional[List[str]] = None

    def paths(self) -> List[str]:
        if self._paths is None:
            if os.path.exists(self.cache):
                with open(self.cache, encoding="utf-8") as f:
                    self._paths = [line.strip() for line in f if line.strip()]
            else:
                with gzip.open(BUNDLED, "rt", encoding="utf-8") as f:
                    self._paths = [line.strip() for line in f if line.strip()]
        return self._paths

    def refresh(self, log: Callable[[str], None]) -> None:
        """Re-list the image set from GitHub at most once a week; the bundled list is the fallback."""
        if os.path.exists(self.cache) and time.time() - os.path.getmtime(self.cache) < 7 * 86400:
            return
        try:
            r = requests.get("https://api.github.com/repos/Kometa-Team/Default-Images/git/trees/master?recursive=1",
                             headers={"Accept": "application/vnd.github+json"}, timeout=60)
            r.raise_for_status()
            paths = [e["path"] for e in r.json().get("tree", []) if e.get("type") == "blob"
                     and e["path"].lower().endswith((".jpg", ".png")) and not e["path"].startswith("separators/")]
            if len(paths) > 1000:
                with open(self.cache + ".tmp", "w", encoding="utf-8") as f:
                    f.write("\n".join(paths))
                os.replace(self.cache + ".tmp", self.cache)
                self._paths = paths
                log(f"Kometa image list refreshed: {len(paths)} images")
        except Exception as e:  # noqa: BLE001
            log(f"Kometa image list not refreshed ({e}); using the bundled list")

    def search(self, query: str, limit: int = 20, exact_only: bool = False) -> List[str]:
        query = (query or "").strip()
        if "/" in query:
            query = os.path.splitext(os.path.basename(query))[0]
        q = norm(query)
        alt = norm(re.sub(r"\s*\bplus\b", "+", query, flags=re.I))
        if not q:
            return []

        def score(stem: str, s: str) -> int:
            return 3 if stem == s else 2 if stem.startswith(s) else 1 if s in stem else -1

        def rank(p: str) -> int:
            return next((i for i, pre in enumerate(PREFERRED) if p.startswith(pre)), len(PREFERRED))

        scored = []
        for p in self.paths():
            stem = norm(os.path.splitext(os.path.basename(p))[0])
            sc = max(score(stem, q), score(stem, alt))
            if sc >= (3 if exact_only else 0):
                scored.append((-sc, rank(p), len(p), p))
        scored.sort()
        return [x[3] for x in scored[:limit]]


def _download(url: str) -> bytes:
    r = requests.get(url, timeout=60, headers={"User-Agent": "posteroverlay/0.1"})
    r.raise_for_status()
    Image.open(io.BytesIO(r.content)).verify()
    return r.content


def _hex(c: str) -> Tuple[int, int, int]:
    c = (c or "#000000").lstrip("#")
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return 0, 0, 0


def _gradient(top: str, bottom: str) -> Image.Image:
    a, b = _hex(top), _hex(bottom)
    col = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / (H - 1)
        col.putpixel((0, y), tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    return col.resize((W, H))


def _title(img: Image.Image, text: str, box: Tuple[int, int, int, int], start: int) -> None:
    """Title wrapped to the box, shrunk until it fits, with a soft shadow."""
    x0, y0, x1, y1 = box
    words = (text or "").upper().split()
    size = start
    while size > 24:
        f = font(size, True)
        lines, line = [], ""
        for w in words:
            trial = (line + " " + w).strip()
            if f.getlength(trial) <= (x1 - x0) or not line:
                line = trial
            else:
                lines.append(line)
                line = w
        if line:
            lines.append(line)
        lh = size * 1.12
        if len(lines) * lh <= (y1 - y0) and all(f.getlength(l) <= (x1 - x0) for l in lines):
            break
        size = int(size * 0.92)
    f = font(size, True)
    lh = size * 1.12
    y = y0 + ((y1 - y0) - len(lines) * lh) / 2 + size
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    txt = Image.new("RGBA", img.size, (0, 0, 0, 0))
    td = ImageDraw.Draw(txt)
    for l in lines:
        x = (x0 + x1) / 2 - f.getlength(l) / 2
        sd.text((x, y + 4), l, font=f, fill=(0, 0, 0, 160), anchor="ls")
        td.text((x, y), l, font=f, fill=(255, 255, 255, 255), anchor="ls")
        y += lh
    base = img.convert("RGBA")
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(size * 0.06)))
    base.alpha_composite(txt)
    img.paste(base.convert("RGB"))


def generated_title(c: CollectionDef) -> bytes:
    img = _gradient(c.artwork.color_top, c.artwork.color_bottom)
    _title(img, c.name, (80, 380, 920, 1200), 130)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


def generated_mosaic(c: CollectionDef, posters: List[bytes]) -> bytes:
    img = _gradient(c.artwork.color_top, c.artwork.color_bottom)
    tiles = posters[:9]
    cols = 3 if len(tiles) >= 9 else 2 if len(tiles) >= 4 else 1
    tiles = tiles[:cols * cols]
    tw, th = W // cols, H // cols
    for i, data in enumerate(tiles):
        try:
            t = Image.open(io.BytesIO(data)).convert("RGB")
        except OSError:
            continue
        s = max(tw / t.width, th / t.height)
        t = t.resize((max(1, round(t.width * s)), max(1, round(t.height * s))), Image.LANCZOS)
        t = t.crop(((t.width - tw) // 2, (t.height - th) // 2, (t.width - tw) // 2 + tw, (t.height - th) // 2 + th))
        img.paste(t, ((i % cols) * tw, (i // cols) * th))
    shade = Image.new("L", (1, H))
    for y in range(H):
        shade.putpixel((0, y), 0 if y < H * 0.55 else round(230 * (y - H * 0.55) / (H * 0.45)))
    dark = Image.new("RGB", (W, H), (0, 0, 0))
    img = Image.composite(dark, img, shade.resize((W, H)))
    _title(img, c.name, (60, 1110, 940, 1440), 110)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


def resolve(c: CollectionDef, settings: Settings, kometa: Kometa,
            member_posters: Callable[[], List[bytes]]) -> Tuple[Optional[bytes], str, str]:
    """Returns (image, key, where it came from). image None: leave the poster alone."""
    a = c.artwork
    mode = a.mode
    if mode == ArtworkMode.none:
        return None, "", "left as is"
    if mode in (ArtworkMode.url, ArtworkMode.auto) and a.url.strip():
        return _download(a.url.strip()), "url:" + a.url.strip(), "poster URL"
    if mode in (ArtworkMode.kometa, ArtworkMode.auto):
        path = a.kometa_path.strip()
        if not path:
            hits = kometa.search(c.name, 1, exact_only=True)
            path = hits[0] if hits else ""
        if path:
            return _download(KOMETA_RAW + path), "kometa:" + path, f"Kometa image {path}"
        if mode == ArtworkMode.kometa:
            raise RuntimeError(f"No Kometa image matches '{c.name}'")
    if mode in (ArtworkMode.tmdb, ArtworkMode.auto) and a.tmdb_collection_id:
        data = Tmdb(settings.tmdb_key).collection(a.tmdb_collection_id) if settings.tmdb_key else None
        if data and data.get("poster_path"):
            url = "https://image.tmdb.org/t/p/original" + data["poster_path"]
            return _download(url), "tmdb:" + data["poster_path"], "TMDB collection poster"
        if mode == ArtworkMode.tmdb:
            raise RuntimeError("TMDB collection poster not found (is the TMDB key set?)")
    style_key = hashlib.sha1(f"{c.name}|{a.color_top}|{a.color_bottom}|{a.generated_style}".encode()).hexdigest()[:12]
    if a.generated_style == "Mosaic":
        posters = member_posters()
        if posters:
            key = "mosaic:" + style_key + ":" + hashlib.sha1(b"".join(p[:2048] for p in posters)).hexdigest()[:8]
            return generated_mosaic(c, posters), key, "generated mosaic"
    return generated_title(c), "title:" + style_key, "generated title poster"
