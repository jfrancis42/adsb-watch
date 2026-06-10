"""On-disk JSON cache for FAA registry lookups.

Schema (line-oriented JSON file is fine; we just use one root dict):

    {
      "version": 1,
      "entries": {
        "A1B2C3": {"fetched_at": 1717977600, "data": {"n_number": "N12345", ...}},
        "FFFFFF": {"fetched_at": 1717977600, "data": null}
      }
    }

`data` is `null` for 404 / not-in-DB results, so we don't keep retrying
unknown ICAOs.

Entries older than `ttl_s` are filtered out at load time AND at lookup
time, so a long-running process eventually stops returning stale rows
even if it never restarts.

Writes are atomic via tempfile + os.replace, and rate-limited (we don't
fsync after every single new aircraft — that would make a busy receiver
hammer the disk). The pending-write flag triggers a flush on a timer or
on close().
"""
import json
import os
import tempfile
import threading
import time
from typing import Optional

CACHE_VERSION = 1


class RegistryCache:
    def __init__(self, path: str, ttl_s: float, save_interval_s: float = 30.0):
        self.path = path
        self.ttl_s = ttl_s
        self.save_interval_s = save_interval_s
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._dirty = False
        self._last_save = 0.0
        self._load()

    # ---- public API -------------------------------------------------------

    def get(self, icao: str) -> tuple[bool, Optional[dict]]:
        """Returns (hit, data). `hit=False` means caller must do a fresh
        lookup. `hit=True, data=None` is a cached 404."""
        icao = icao.upper()
        now = time.time()
        with self._lock:
            entry = self._entries.get(icao)
            if entry is None:
                return False, None
            if now - entry['fetched_at'] > self.ttl_s:
                # Stale — drop and treat as miss so we re-fetch.
                del self._entries[icao]
                self._dirty = True
                return False, None
            return True, entry['data']

    def put(self, icao: str, data: Optional[dict]):
        """Store a successful lookup or a 404 sentinel (data=None)."""
        icao = icao.upper()
        with self._lock:
            self._entries[icao] = {
                'fetched_at': time.time(),
                'data': data,
            }
            self._dirty = True
            self._maybe_save_locked()

    def flush(self):
        """Force a write to disk. Called on shutdown."""
        with self._lock:
            if self._dirty:
                self._save_locked()

    def stats(self) -> dict:
        with self._lock:
            return {'entries': len(self._entries), 'path': self.path}

    # ---- internals --------------------------------------------------------

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            # Corrupt or unreadable — start clean. We'll overwrite on next save.
            return
        if not isinstance(raw, dict) or raw.get('version') != CACHE_VERSION:
            return
        entries = raw.get('entries') or {}
        now = time.time()
        kept: dict[str, dict] = {}
        for icao, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            fetched = entry.get('fetched_at')
            if not isinstance(fetched, (int, float)):
                continue
            if now - fetched > self.ttl_s:
                continue
            kept[icao.upper()] = {
                'fetched_at': float(fetched),
                'data': entry.get('data'),
            }
        with self._lock:
            self._entries = kept

    def _maybe_save_locked(self):
        now = time.time()
        if now - self._last_save < self.save_interval_s:
            return
        self._save_locked()

    def _save_locked(self):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        # Tempfile in same dir so os.replace is atomic on the same filesystem.
        fd, tmp_path = tempfile.mkstemp(
            prefix='.registry-', suffix='.json',
            dir=os.path.dirname(self.path) or '.')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(
                    {'version': CACHE_VERSION, 'entries': self._entries},
                    f, separators=(',', ':'))
            os.replace(tmp_path, self.path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return
        self._dirty = False
        self._last_save = time.time()
