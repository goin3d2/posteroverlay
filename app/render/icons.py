"""Badge icons: downloaded once into the config folder (icons/), your own files in icons/custom win."""
from __future__ import annotations

import hashlib
import os
import threading
from typing import Callable, Dict, Optional, Tuple

import requests
from PIL import Image

SOURCE_PATHS: Dict[str, str] = {
    "res_4k": "resolution/4k.png",
    "res_1080p": "resolution/1080p.png",
    "res_720p": "resolution/720p.png",
    "res_576p": "resolution/576p.png",
    "res_480p": "resolution/480p.png",
    "hdr_dv": "resolution/dv.png",
    "hdr_hdr10plus": "resolution/plus.png",
    "hdr_hdr10": "resolution/hdr.png",
    "hdr_hlg": "resolution/hlg.png",
    "audio_truehd_atmos": "audio_codec/compact/truehd_atmos.png",
    "audio_plus_atmos": "audio_codec/compact/plus_atmos.png",
    "audio_atmos": "audio_codec/compact/atmos.png",
    "audio_dtsx": "audio_codec/compact/dtsx.png",
    "audio_dtshdma": "audio_codec/compact/ma.png",
    "audio_truehd": "audio_codec/compact/truehd.png",
    "audio_ddplus": "audio_codec/compact/plus.png",
    "audio_dts": "audio_codec/compact/dts.png",
    "audio_dd": "audio_codec/compact/digital.png",
    "imdb": "rating/IMDb.png",
    "tmdb": "rating/TMDb.png",
    "rt_critic_fresh": "rating/RT-Crit-Fresh.png",
    "rt_critic_rotten": "rating/RT-Crit-Rotten.png",
    "rt_audience_fresh": "rating/RT-Aud-Fresh.png",
    "rt_audience_rotten": "rating/RT-Aud-Rotten.png",
}


class Icons:
    def __init__(self, folder: str):
        self.folder = folder
        self.custom = os.path.join(folder, "custom")
        os.makedirs(self.custom, exist_ok=True)
        self._cache: Dict[str, Optional[Image.Image]] = {}
        self._lock = threading.Lock()

    def path(self, key: str) -> str:
        custom = os.path.join(self.custom, key + ".png")
        return custom if os.path.exists(custom) else os.path.join(self.folder, key + ".png")

    def get(self, key: Optional[str]) -> Optional[Image.Image]:
        if not key:
            return None
        with self._lock:
            if key not in self._cache:
                p = self.path(key)
                img = None
                if os.path.exists(p):
                    try:
                        img = Image.open(p).convert("RGBA")
                    except OSError:
                        img = None
                self._cache[key] = img
            return self._cache[key]

    def reload(self) -> None:
        with self._lock:
            self._cache.clear()

    def generation(self) -> str:
        """Changes whenever an icon file changes (part of the poster cache key)."""
        h = hashlib.sha1()
        for d in (self.folder, self.custom):
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if name.endswith(".png"):
                    st = os.stat(os.path.join(d, name))
                    h.update(f"{d}/{name}:{st.st_size}:{int(st.st_mtime)}".encode())
        return h.hexdigest()[:8]

    def count(self) -> Tuple[int, int]:
        return sum(1 for k in SOURCE_PATHS if os.path.exists(self.path(k))), len(SOURCE_PATHS)

    def download(self, base_url: str, force: bool, log: Callable[[str], None]) -> Tuple[int, int]:
        base = (base_url or "").strip().rstrip("/")
        ok = failed = 0
        for key, rel in SOURCE_PATHS.items():
            target = os.path.join(self.folder, key + ".png")
            if os.path.exists(target) and not force:
                continue
            try:
                r = requests.get(f"{base}/{rel}", timeout=30)
                r.raise_for_status()
                Image.open(__import__("io").BytesIO(r.content)).verify()
                with open(target + ".tmp", "wb") as f:
                    f.write(r.content)
                os.replace(target + ".tmp", target)
                ok += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                log(f"Icon {key} could not be downloaded: {e}")
        self.reload()
        log(f"Icons: {ok} downloaded, {failed} failed")
        return ok, failed
