"""SQLite state: poster originals and what was applied, rating/status caches, collection mappings, activity."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS posters (
    server TEXT NOT NULL,
    item TEXT NOT NULL,
    title TEXT,
    original_file TEXT,       -- path of the saved original under /config/originals
    original_marker TEXT,     -- server's image id/tag when the original was saved
    applied_key TEXT,         -- what we drew (style hash + content)
    applied_marker TEXT,      -- server's image id/tag right after our upload
    updated REAL,
    PRIMARY KEY (server, item)
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT,
    updated REAL
);
CREATE TABLE IF NOT EXISTS collection_map (
    collection TEXT NOT NULL,   -- our collection definition id
    server TEXT NOT NULL,
    section TEXT NOT NULL,      -- Plex library section key; '' for Emby
    remote_id TEXT NOT NULL,
    artwork_key TEXT,
    PRIMARY KEY (collection, server, section)
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time REAL,
    scope TEXT,
    level TEXT,
    message TEXT
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def _q(self, sql: str, args: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    # ---- posters
    def poster(self, server: str, item: str) -> Optional[Dict[str, Any]]:
        rows = self._q("SELECT * FROM posters WHERE server=? AND item=?", (server, item))
        return dict(rows[0]) if rows else None

    def save_poster(self, server: str, item: str, **fields: Any) -> None:
        current = self.poster(server, item) or {"server": server, "item": item}
        current.update(fields)
        current["updated"] = time.time()
        cols = ["server", "item", "title", "original_file", "original_marker", "applied_key", "applied_marker", "updated"]
        self._q(
            f"INSERT OR REPLACE INTO posters ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [current.get(c) for c in cols],
        )

    def delete_poster(self, server: str, item: str) -> None:
        self._q("DELETE FROM posters WHERE server=? AND item=?", (server, item))

    def posters(self, server: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM posters WHERE server=?", (server,))]

    def poster_counts(self) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = {}
        for r in self._q(
            "SELECT server, COUNT(*) n, SUM(applied_key IS NOT NULL AND applied_key != '') applied FROM posters GROUP BY server"
        ):
            out[r["server"]] = {"tracked": r["n"], "applied": r["applied"] or 0}
        return out

    # ---- key/value cache (JSON values)
    def get(self, key: str, max_age: Optional[float] = None) -> Any:
        rows = self._q("SELECT v, updated FROM kv WHERE k=?", (key,))
        if not rows:
            return None
        if max_age is not None and time.time() - rows[0]["updated"] > max_age:
            return None
        return json.loads(rows[0]["v"])

    def age(self, key: str) -> Optional[float]:
        rows = self._q("SELECT updated FROM kv WHERE k=?", (key,))
        return time.time() - rows[0]["updated"] if rows else None

    def put(self, key: str, value: Any) -> None:
        self._q("INSERT OR REPLACE INTO kv (k, v, updated) VALUES (?,?,?)", (key, json.dumps(value), time.time()))

    def put_many(self, items: Dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(
                    "INSERT OR REPLACE INTO kv (k, v, updated) VALUES (?,?,?)",
                    [(k, json.dumps(v), now) for k, v in items.items()],
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def get_prefix(self, prefix: str) -> Dict[str, Any]:
        rows = self._q("SELECT k, v FROM kv WHERE k >= ? AND k < ?", (prefix, prefix + "￿"))
        return {r["k"][len(prefix):]: json.loads(r["v"]) for r in rows}

    def count_prefix(self, prefix: str) -> int:
        return self._q("SELECT COUNT(*) n FROM kv WHERE k >= ? AND k < ?", (prefix, prefix + "￿"))[0]["n"]

    # ---- collections
    def collection_maps(self, collection: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM collection_map WHERE collection=?", (collection,))]

    def set_collection_map(self, collection: str, server: str, section: str, remote_id: str, artwork_key: str = "") -> None:
        self._q(
            "INSERT OR REPLACE INTO collection_map (collection, server, section, remote_id, artwork_key) VALUES (?,?,?,?,?)",
            (collection, server, section, remote_id, artwork_key),
        )

    def delete_collection_map(self, collection: str, server: Optional[str] = None, section: Optional[str] = None) -> None:
        if server is None:
            self._q("DELETE FROM collection_map WHERE collection=?", (collection,))
        else:
            self._q("DELETE FROM collection_map WHERE collection=? AND server=? AND section=?", (collection, server, section or ""))

    # ---- activity
    def log(self, scope: str, level: str, message: str) -> None:
        self._q("INSERT INTO activity (time, scope, level, message) VALUES (?,?,?,?)", (time.time(), scope, level, message))
        self._q("DELETE FROM activity WHERE id < (SELECT MAX(id) - 5000 FROM activity)")

    def activity(self, after: int = 0, scope: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        if scope:
            rows = self._q(
                "SELECT * FROM activity WHERE id > ? AND scope LIKE ? ORDER BY id DESC LIMIT ?", (after, scope + "%", limit)
            )
        else:
            rows = self._q("SELECT * FROM activity WHERE id > ? ORDER BY id DESC LIMIT ?", (after, limit))
        return [dict(r) for r in reversed(rows)]
