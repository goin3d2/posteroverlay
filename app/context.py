"""Shared app state: settings, database, icons, and the background job runner."""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from typing import Callable, Dict, Optional

from .render.icons import Icons
from .settings import CONFIG_DIR, SettingsStore
from .store import Store

log = logging.getLogger("posteroverlay")


class Job:
    def __init__(self, name: str):
        self.name = name
        self.running = False
        self.phase = ""
        self.done = 0
        self.total = 0
        self.started = 0.0
        self.finished = 0.0
        self.result = ""
        self.error = ""
        self.cancel = threading.Event()

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("name", "running", "phase", "done", "total", "started", "finished",
                                               "result", "error")}


class Context:
    def __init__(self, config_dir: Optional[str] = None):
        self.config_dir = config_dir or CONFIG_DIR
        os.makedirs(self.config_dir, exist_ok=True)
        self.settings = SettingsStore(os.path.join(self.config_dir, "settings.json"))
        self.store = Store(os.path.join(self.config_dir, "posteroverlay.db"))
        self.icons = Icons(os.path.join(self.config_dir, "icons"))
        self.jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def path(self, *parts: str) -> str:
        p = os.path.join(self.config_dir, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def logger(self, scope: str) -> Callable[..., None]:
        def write(message: str, level: str = "info") -> None:
            self.store.log(scope, level, message)
            getattr(log, "warning" if level == "warn" else level if level in ("info", "error") else "info")(
                "[%s] %s", scope, message)
        return write

    def job(self, name: str) -> Job:
        with self._lock:
            return self.jobs.setdefault(name, Job(name))

    def start(self, name: str, fn: Callable[[Job], str], wait: bool = False) -> bool:
        """Run fn in a background thread unless a job with that name is already running."""
        job = self.job(name)
        with self._lock:
            if job.running:
                return False
            job.running, job.phase, job.done, job.total = True, "starting", 0, 0
            job.started, job.result, job.error = time.time(), "", ""
            job.cancel.clear()

        def run():
            try:
                job.result = fn(job) or ""
            except Exception as e:  # noqa: BLE001
                job.error = str(e)
                self.store.log(name, "error", f"{name} failed: {e}")
                log.error("%s failed:\n%s", name, traceback.format_exc())
            finally:
                job.running = False
                job.finished = time.time()
                job.phase = ""

        t = threading.Thread(target=run, name=name, daemon=True)
        t.start()
        if wait:
            t.join()
        return True
