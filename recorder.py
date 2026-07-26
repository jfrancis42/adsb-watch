"""Wire-level recorder — logs every line each feeder receives, with a receive
timestamp, so a session can later be replayed in real time.

Why the wire level? It's the most faithful capture point: the exact bytes off
each socket, before any parsing, tagged with which stream produced them and
when they arrived. That means replay is trivial (re-inject each `data` payload
into the matching feeder's `_handle()`, sleeping by timestamp deltas) and even
malformed / unparseable lines are preserved.

File format — JSON Lines (one JSON object per line):

    {"t": 1721340000.123, "src": "__meta__", "info": {...}}   # always first
    {"t": 1721340000.456, "src": "adsb-sbs", "data": "MSG,3,..."}
    {"t": 1721340000.501, "src": "uat-978",  "data": "{\"address\":...}"}
    {"t": 1721340001.002, "src": "gps",       "data": "{\"class\":\"TPV\"...}"}

- `t`    — epoch seconds (time.time()) when the line was received.
- `src`  — the feeder's stream id (matches feeder.name_id: 'adsb-sbs',
           'adsb-avr', 'uat-978', 'gps'). Records whose src starts with '__'
           are metadata, not wire data — replay tooling should skip them.
- `data` — the raw received line as a string (already newline-stripped).

Filenames are start-time-stamped: `adsb-watch-YYYYMMDD-HHMMSS.jsonl`.

See TODO.md for the planned real-time playback feature that consumes these
files. The replay contract is: src -> feeder that emitted it; replay sleeps the
inter-record wall-clock gaps (optionally scaled) and feeds `data` back in.
"""
import json
import os
import threading
import time


# Flush to disk every this many records (bounds data loss on an ungraceful
# exit without an fsync per line at high message rates).
_FLUSH_EVERY = 64


class Recorder:
    """Thread-safe JSONL session recorder. All feeders share one instance."""

    def __init__(self, path: str, *, meta: dict | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._fh = open(path, 'w', encoding='utf-8')
        self._since_flush = 0
        self._closed = False
        # First line is a metadata record for the replay tool.
        info = {'format': 'adsb-watch-jsonl', 'version': 1,
                'started_at': time.time()}
        if meta:
            info.update(meta)
        self._write_record({'t': info['started_at'], 'src': '__meta__',
                            'info': info})
        self._fh.flush()

    def _write_record(self, rec: dict):
        # Caller holds the lock (or we're in __init__ before threads exist).
        self._fh.write(json.dumps(rec, separators=(',', ':')))
        self._fh.write('\n')

    def log(self, src: str, data: str, t: float | None = None):
        """Record one received wire line. `t` defaults to now."""
        if self._closed:
            return
        rec = {'t': t if t is not None else time.time(),
               'src': src, 'data': data}
        with self._lock:
            if self._closed:
                return
            try:
                self._write_record(rec)
            except (OSError, ValueError):
                return
            self._since_flush += 1
            if self._since_flush >= _FLUSH_EVERY:
                self._fh.flush()
                self._since_flush = 0

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass


def default_log_path(log_dir: str, *, now: float | None = None) -> str:
    """Build a start-time-stamped log path under `log_dir`, creating the dir."""
    os.makedirs(log_dir, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S',
                          time.localtime(now if now is not None else time.time()))
    return os.path.join(log_dir, f'adsb-watch-{stamp}.jsonl')
