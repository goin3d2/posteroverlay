"""Turn media streams into badge keys (resolution, HDR, audio) and summarize many files into one.
Same rules as the Emby Poster Overlays plugin."""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from ..servers.base import Stream

RES_RANK = ["res_480p", "res_576p", "res_720p", "res_1080p", "res_4k"]
HDR_RANK = ["hdr_hlg", "hdr_hdr10", "hdr_hdr10plus", "hdr_dv"]
AUDIO_RANK = ["audio_dd", "audio_dts", "audio_ddplus", "audio_truehd", "audio_dtshdma", "audio_dtsx",
              "audio_atmos", "audio_plus_atmos", "audio_truehd_atmos"]
BASIC_AUDIO = {"audio_dd", "audio_ddplus", "audio_dts"}

DTSX = re.compile(r"dts[\.\-: _]?x(?![0-9a-z])", re.I)
ATMOS = re.compile(r"(?<![a-z])atmos(?![a-z])", re.I)


@dataclass
class Quality:
    resolution: Optional[str] = None
    hdr: Optional[str] = None
    audio: Optional[str] = None

    @property
    def empty(self) -> bool:
        return not (self.resolution or self.hdr or self.audio)


def resolution(width: int, height: int) -> Optional[str]:
    w, h = width or 0, height or 0
    if not w and not h:
        return None
    # Width counts: 3840x1600 scope films are 4K, 1920x800 is 1080p.
    if w >= 3200 or h >= 2000:
        return "res_4k"
    if w >= 1600 or h >= 1000:
        return "res_1080p"
    if w >= 1100 or h >= 700:
        return "res_720p"
    if h >= 560:
        return "res_576p"
    return "res_480p"


def audio(s: Stream) -> Optional[str]:
    codec = (s.codec or "").lower()
    profile = (s.profile or "").lower()
    text = f"{s.profile} {s.title}"
    atmos = bool(ATMOS.search(text))
    if codec in ("truehd", "mlp"):
        return "audio_truehd_atmos" if atmos else "audio_truehd"
    if codec == "eac3":
        return "audio_plus_atmos" if atmos else "audio_ddplus"
    if codec == "ac3":
        return "audio_dd"
    if codec in ("dts", "dca", "dtshd", "dca-ma"):
        if DTSX.search(text):
            return "audio_dtsx"
        if "dts-hd ma" in profile or "dts-hd master" in profile or profile == "ma" or "dts-hd ma" in text.lower():
            return "audio_dtshdma"
        return "audio_dts"
    return None


def upgrade_from_path(key: Optional[str], path: str) -> Optional[str]:
    """Release names often say Atmos / DTS-X when the track title doesn't."""
    if not key or not path:
        return key
    name = os.path.basename(path)
    if ATMOS.search(name):
        if key == "audio_truehd":
            return "audio_truehd_atmos"
        if key == "audio_ddplus":
            return "audio_plus_atmos"
    if key in ("audio_dtshdma", "audio_dts") and DTSX.search(name):
        return "audio_dtsx"
    return key


def classify(streams: Sequence[Stream], path: str = "") -> Quality:
    q = Quality()
    video = next((s for s in streams if s.kind == "video"), None)
    if video:
        q.resolution = resolution(video.width, video.height)
        q.hdr = {"dv": "hdr_dv", "hdr10plus": "hdr_hdr10plus", "hdr10": "hdr_hdr10", "hlg": "hdr_hlg"}.get(video.hdr or "")
    keys = [k for k in (audio(s) for s in streams if s.kind == "audio") if k]
    if keys:
        q.audio = max(keys, key=AUDIO_RANK.index)
    q.audio = upgrade_from_path(q.audio, path)
    return q


def _pick(values: List[Optional[str]], rank: List[str], most_common: bool) -> Optional[str]:
    if most_common:
        counts = Counter(v or "" for v in values)
        best = sorted(counts.items(), key=lambda kv: (-kv[1], -(rank.index(kv[0]) if kv[0] in rank else -1)))[0][0]
        return best or None
    present = [v for v in values if v]
    return max(present, key=rank.index) if present else None


def summarize(files: Iterable[Quality], most_common: bool = False) -> Quality:
    files = [f for f in files if f and not f.empty]
    if not files:
        return Quality()
    return Quality(
        _pick([f.resolution for f in files], RES_RANK, most_common),
        _pick([f.hdr for f in files], HDR_RANK, most_common),
        _pick([f.audio for f in files], AUDIO_RANK, most_common),
    )


def to_dict(q: Quality) -> dict:
    return {"resolution": q.resolution, "hdr": q.hdr, "audio": q.audio}


def from_dict(d: Optional[dict]) -> Quality:
    d = d or {}
    return Quality(d.get("resolution"), d.get("hdr"), d.get("audio"))
