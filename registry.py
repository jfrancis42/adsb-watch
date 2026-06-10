"""Look up ICAO Mode S addresses against the govt-data REST API
(/aircraft/hex/{hex}) and push the result back into the engine.

Behaviour:
- 200 OK   → cache it, mark aircraft `registry_checked=True`. No more lookups.
- 404      → cache as "not in DB" (None), mark checked. No more lookups.
- anything else (timeout, connection refused, 5xx, parse error) → exponential
  backoff per-ICAO. So a flaky network or a momentarily-down server doesn't
  cause us to hammer the API for every pending aircraft every second.

We also rate-limit *between* lookups (small inter-request delay) so that on
first contact with a busy receiver we don't burst the server.
"""
import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from engine import Engine


# Per-ICAO backoff schedule (seconds). Each consecutive failure advances
# one step. Success or 404 clears the state. The final entry caps the
# backoff — a permanently-failing ICAO is retried at most every 5 min.
BACKOFF_SCHEDULE_S = (5, 15, 30, 60, 120, 300)

# Pause between successive HTTP requests, regardless of success/failure.
# Caps the burst rate at ~10 req/s when many aircraft are pending.
INTER_REQUEST_PAUSE_S = 0.1


class RegistryClient(threading.Thread):
    daemon = True

    def __init__(self, engine: Engine, base_url: str, user: str, password: str,
                 poll_interval: float = 1.0):
        super().__init__(name='registry')
        self.engine = engine
        self.base_url = base_url.rstrip('/')
        self.poll_interval = poll_interval
        token = base64.b64encode(f'{user}:{password}'.encode()).decode()
        self._auth = f'Basic {token}'
        self._stop = threading.Event()
        # icao -> {'fails': int, 'next_at': float}. Entries are set on
        # failure and cleared on success/404; an aircraft that's never
        # failed has no entry here.
        self._retry: dict[str, dict] = {}

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                pending = self.engine.pending_registry_lookups()
                for icao in pending:
                    if self._stop.is_set():
                        break
                    if not self._due(icao):
                        continue
                    self._lookup(icao)
                    # Rate-limit between requests; doubles as a fast bail
                    # on stop().
                    if self._stop.wait(INTER_REQUEST_PAUSE_S):
                        return
            except Exception:
                pass
            self._stop.wait(self.poll_interval)

    def _due(self, icao: str) -> bool:
        st = self._retry.get(icao)
        return st is None or time.time() >= st['next_at']

    def _mark_failure(self, icao: str):
        st = self._retry.get(icao) or {'fails': 0}
        st['fails'] += 1
        idx = min(st['fails'] - 1, len(BACKOFF_SCHEDULE_S) - 1)
        st['next_at'] = time.time() + BACKOFF_SCHEDULE_S[idx]
        self._retry[icao] = st

    def _lookup(self, icao: str):
        url = f'{self.base_url}/aircraft/hex/{urllib.parse.quote(icao)}'
        req = urllib.request.Request(url, headers={'Authorization': self._auth})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.engine.attach_registry(icao)   # mark checked, no data
                self._retry.pop(icao, None)
            else:
                # 401 / 403 / 5xx — transient or auth problem; retry with backoff.
                self._mark_failure(icao)
            return
        except Exception:
            # Network error / timeout / parse error — back off, retry later.
            self._mark_failure(icao)
            return
        self.engine.attach_registry(
            icao,
            n_number=data.get('n_number'),
            manufacturer=data.get('manufacturer'),
            model=data.get('model'),
            owner=data.get('owner'),
        )
        self._retry.pop(icao, None)
