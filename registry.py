"""Look up ICAO Mode S addresses against the govt-data REST API
(/aircraft/hex/{hex}) and push the result back into the engine.

We poll the engine for "pending" ICAOs once per second; the engine itself
remembers which ones have already been resolved so we don't hit the API
repeatedly for the same plane. 404s mark the aircraft checked-but-empty,
so we don't keep retrying ones that aren't in the FAA database."""
import threading
import time
import urllib.parse
import urllib.request
import json
import base64

from engine import Engine


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

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                pending = self.engine.pending_registry_lookups()
                for icao in pending:
                    if self._stop.is_set():
                        break
                    self._lookup(icao)
            except Exception:
                pass
            self._stop.wait(self.poll_interval)

    def _lookup(self, icao: str):
        url = f'{self.base_url}/aircraft/hex/{urllib.parse.quote(icao)}'
        req = urllib.request.Request(url, headers={'Authorization': self._auth})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.engine.attach_registry(icao)   # mark checked, no data
            return
        except Exception:
            # Network blip — don't mark checked, we'll retry next pass.
            return
        self.engine.attach_registry(
            icao,
            n_number=data.get('n_number'),
            manufacturer=data.get('manufacturer'),
            model=data.get('model'),
            owner=data.get('owner'),
        )
