"""Bring in what the Emby Poster Overlays plugin already fetched (its overlay-data.json), so the first run here
does not have to look up every rating and show status again."""
from __future__ import annotations

import calendar
import time
from typing import Any, Dict

from .store import Store


def _when(s: Any) -> float:
    try:
        return float(calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError):
        return 0.0


def _day(s: Any) -> str:
    return str(s)[:10] if s else ""


def import_plugin_data(data: Dict[str, Any], store: Store) -> Dict[str, int]:
    out: Dict[str, tuple] = {}
    counts = {"ratings": 0, "statuses": 0, "lookups": 0}
    for key, r in (data.get("Ratings") or {}).items():
        kind, _, rest = key.partition(":")
        if kind not in ("movie", "show") or not rest or not isinstance(r, dict):
            continue
        t = _when(r.get("FetchedUtc"))
        if r.get("NotFound"):
            value = {"not_found": True}
        else:
            value = {k: v for k, v in (("imdb", r.get("Imdb")), ("rt_critic", r.get("RtCritic")),
                                       ("rt_audience", r.get("RtAudience"))) if v is not None}
        out[f"mdb:{key}"] = (value, t)
        counts["ratings"] += 1
    for tid, s in (data.get("Status") or {}).items():
        if not isinstance(s, dict) or not s.get("TmdbStatus"):
            continue
        out[f"tmdbstatus:{tid}"] = ({"status": s["TmdbStatus"], "last": _day(s.get("LastAirDate")),
                                     "next": _day(s.get("NextAirDate"))}, _when(s.get("FetchedUtc")))
        counts["statuses"] += 1
    for key, tid in (data.get("TmdbIdLookups") or {}).items():
        out[f"tmdbfind:{key}"] = (tid or "", time.time())
        counts["lookups"] += 1
    counts["written"] = store.put_older(out)
    return counts
