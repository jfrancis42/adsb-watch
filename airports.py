"""Pull airports/runways/frequencies/navaids in a 50-NM radius around the
observer from govt-data, cache them, and expose a thread-safe Facilities
snapshot to the engine.

Designed for a *moving* receiver: every minute the client checks if the
observer has drifted more than `move_threshold_nm` from the center of its
current cache bucket; if so, it refetches. Buckets are quantized to a
0.25-degree (~15 NM) grid so two nearby restarts hit the same cache file.

Cache file layout: one JSON document per bucket, keyed by quantized lat/lon,
re-using `cache.RegistryCache` (its put/get is generic over JSON-able values).
"""
import base64
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from cache import RegistryCache


GRID_DEG = 0.25  # ~15 NM at mid-latitudes — coarse on purpose


def bucket_key(lat: float, lon: float) -> str:
    """Quantize (lat, lon) to a coarse grid so nearby observer positions reuse
    the same cache row. Returned as a string so it can be a dict key."""
    qlat = round(lat / GRID_DEG) * GRID_DEG
    qlon = round(lon / GRID_DEG) * GRID_DEG
    return f'{qlat:.3f},{qlon:.3f}'


@dataclass
class Runway:
    airport_ident: str
    length_ft: Optional[float]
    width_ft:  Optional[float]
    surface:   Optional[str]
    closed:    bool
    le_ident:  str
    le_lat:    Optional[float]
    le_lon:    Optional[float]
    le_elev_ft: Optional[float]
    le_heading_degt: Optional[float]
    he_ident:  str
    he_lat:    Optional[float]
    he_lon:    Optional[float]
    he_elev_ft: Optional[float]
    he_heading_degt: Optional[float]


@dataclass
class Airport:
    ident: str
    name:  str
    type:  str               # 'large_airport', 'small_airport', 'heliport', …
    lat:   float
    lon:   float
    elev_ft: Optional[float]
    iata_code: Optional[str]
    icao_code: Optional[str]
    runways:    list = field(default_factory=list)
    frequencies: list = field(default_factory=list)


@dataclass
class Navaid:
    ident: str
    name:  str
    type:  str
    lat:   float
    lon:   float
    elev_ft: Optional[float]
    frequency_khz: Optional[float]
    associated_airport: Optional[str]


@dataclass
class Facilities:
    """Read-only snapshot handed to the engine. None when no facilities have
    been fetched yet (e.g., no GPS fix)."""
    center_lat: float
    center_lon: float
    radius_nm: float
    fetched_at: float
    airports: list           # list[Airport]
    navaids:  list           # list[Navaid]


class FacilitiesClient(threading.Thread):
    """Background thread that owns the Facilities snapshot. The engine reads
    it via `client.snapshot()`; the engine never makes HTTP calls itself."""
    daemon = True

    # Backoff schedule (seconds) when a refresh fails. Each consecutive
    # failure advances one step; success resets to the start. Capped at
    # 10 min so a long outage doesn't hammer the server but also doesn't
    # leave the user waiting hours after the network comes back.
    BACKOFF_SCHEDULE_S = (30, 60, 120, 300, 600)

    def __init__(self, base_url: str, user: str, password: str,
                 cache: RegistryCache,
                 radius_nm: float = 50.0,
                 move_threshold_nm: float = 10.0,
                 poll_interval_s: float = 60.0,
                 ttl_s: float = 7 * 86400.0):
        super().__init__(name='facilities')
        self.base_url = base_url.rstrip('/')
        self.cache = cache
        self.radius_nm = radius_nm
        self.move_threshold_nm = move_threshold_nm
        self.poll_interval_s = poll_interval_s
        self.ttl_s = ttl_s
        token = base64.b64encode(f'{user}:{password}'.encode()).decode()
        self._auth = f'Basic {token}'
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._snapshot: Optional[Facilities] = None
        self._observer_provider = None  # callable -> (lat, lon) or None
        self.status = 'idle'
        self._fail_count = 0
        self._next_attempt_at = 0.0

    # ---- public API ------------------------------------------------------

    def attach_observer(self, provider):
        """`provider` is a callable that returns (lat, lon) or None — usually
        the engine's snapshot.observer accessor."""
        self._observer_provider = provider

    def snapshot(self) -> Optional[Facilities]:
        with self._lock:
            return self._snapshot

    def stop(self):
        self._stop.set()

    # ---- thread loop -----------------------------------------------------

    def run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                self.status = f'error: {type(e).__name__}: {e}'
            self._stop.wait(self.poll_interval_s)

    def _tick(self):
        if self._observer_provider is None:
            self.status = 'no observer attached'
            return
        pos = self._observer_provider()
        if pos is None:
            self.status = 'waiting for GPS fix'
            return
        lat, lon = pos

        snap = self.snapshot()
        # Refetch if we've never fetched, drifted, or exceeded TTL.
        if snap is not None:
            drift = _haversine_nm(snap.center_lat, snap.center_lon, lat, lon)
            fresh = (time.time() - snap.fetched_at) < self.ttl_s
            if drift < self.move_threshold_nm and fresh:
                self.status = (f'fresh ({len(snap.airports)} airports, '
                               f'drift {drift:.1f} NM)')
                return

        # Honour the backoff schedule — if a previous refresh failed, wait
        # the prescribed time before trying again.
        wait_s = self._next_attempt_at - time.time()
        if wait_s > 0:
            self.status = (f'backoff: retry in {wait_s:.0f}s '
                           f'(fails={self._fail_count})')
            return

        self._refresh(lat, lon)

    def _record_failure(self, reason: str):
        self._fail_count += 1
        idx = min(self._fail_count - 1, len(self.BACKOFF_SCHEDULE_S) - 1)
        delay = self.BACKOFF_SCHEDULE_S[idx]
        self._next_attempt_at = time.time() + delay
        self.status = (f'{reason}; retry in {delay}s '
                       f'(fail #{self._fail_count})')

    def _record_success(self):
        self._fail_count = 0
        self._next_attempt_at = 0.0

    def _refresh(self, lat: float, lon: float):
        key = f'facilities:{bucket_key(lat, lon)}:{int(self.radius_nm)}'
        hit, cached = self.cache.get(key)
        if hit and cached is not None:
            facs = _hydrate(cached)
            self.status = (f'loaded cache ({len(facs.airports)} airports) '
                           f'@ {bucket_key(lat, lon)}')
            with self._lock:
                self._snapshot = facs
            self._record_success()
            return

        self.status = f'fetching radius={self.radius_nm} NM @ {lat:.3f},{lon:.3f}'
        try:
            airports_raw = self._http_get(
                f'/airports/near?lat={lat}&lon={lon}'
                f'&radius_nm={self.radius_nm}&limit=500')
        except Exception as e:
            self._record_failure(f'airports fetch failed: {e}')
            return

        airports: list[Airport] = []
        for ap in airports_raw:
            ident = ap['ident']
            airport = Airport(
                ident=ident, name=ap.get('name') or '', type=ap.get('type') or '',
                lat=ap['latitude_deg'], lon=ap['longitude_deg'],
                elev_ft=ap.get('elevation_ft'),
                iata_code=ap.get('iata_code'),
                icao_code=ap.get('icao_code'),
            )
            try:
                airport.runways = [
                    Runway(
                        airport_ident=ident,
                        length_ft=r.get('length_ft'),
                        width_ft=r.get('width_ft'),
                        surface=r.get('surface'),
                        closed=bool(r.get('closed')),
                        le_ident=str(r.get('le_ident') or ''),
                        le_lat=r.get('le_latitude_deg'),
                        le_lon=r.get('le_longitude_deg'),
                        le_elev_ft=r.get('le_elevation_ft'),
                        le_heading_degt=r.get('le_heading_degt'),
                        he_ident=str(r.get('he_ident') or ''),
                        he_lat=r.get('he_latitude_deg'),
                        he_lon=r.get('he_longitude_deg'),
                        he_elev_ft=r.get('he_elevation_ft'),
                        he_heading_degt=r.get('he_heading_degt'),
                    )
                    for r in self._http_get(f'/airports/{urllib.parse.quote(ident)}/runways')
                ]
            except Exception:
                airport.runways = []
            try:
                airport.frequencies = self._http_get(
                    f'/airports/{urllib.parse.quote(ident)}/frequencies')
            except Exception:
                airport.frequencies = []
            airports.append(airport)

        try:
            navaids_raw = self._http_get(
                f'/navaids/near?lat={lat}&lon={lon}'
                f'&radius_nm={self.radius_nm}&limit=500')
        except Exception:
            navaids_raw = []
        navaids = [
            Navaid(
                ident=n.get('ident', ''), name=n.get('name', ''),
                type=n.get('type', ''),
                lat=n['latitude_deg'], lon=n['longitude_deg'],
                elev_ft=n.get('elevation_ft'),
                frequency_khz=n.get('frequency_khz'),
                associated_airport=n.get('associated_airport') or None,
            )
            for n in navaids_raw if n.get('latitude_deg') is not None
        ]

        facs = Facilities(
            center_lat=lat, center_lon=lon, radius_nm=self.radius_nm,
            fetched_at=time.time(), airports=airports, navaids=navaids)
        self.cache.put(key, _serialize(facs))
        with self._lock:
            self._snapshot = facs
        self.status = (f'fetched {len(airports)} airports + {len(navaids)} navaids')
        self._record_success()

    # ---- HTTP helper -----------------------------------------------------

    def _http_get(self, path: str):
        url = f'{self.base_url}{path}'
        req = urllib.request.Request(url, headers={'Authorization': self._auth})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())


# ---------------------------------------------------------------------------
# (de)serialization for the JSON cache
# ---------------------------------------------------------------------------

def _serialize(f: Facilities) -> dict:
    return {
        'center_lat': f.center_lat, 'center_lon': f.center_lon,
        'radius_nm': f.radius_nm, 'fetched_at': f.fetched_at,
        'airports': [
            {
                'ident': a.ident, 'name': a.name, 'type': a.type,
                'lat': a.lat, 'lon': a.lon, 'elev_ft': a.elev_ft,
                'iata_code': a.iata_code, 'icao_code': a.icao_code,
                'runways': [r.__dict__ for r in a.runways],
                'frequencies': a.frequencies,
            } for a in f.airports
        ],
        'navaids': [n.__dict__ for n in f.navaids],
    }


def _hydrate(d: dict) -> Facilities:
    airports = [
        Airport(
            ident=a['ident'], name=a['name'], type=a['type'],
            lat=a['lat'], lon=a['lon'], elev_ft=a.get('elev_ft'),
            iata_code=a.get('iata_code'), icao_code=a.get('icao_code'),
            runways=[Runway(**r) for r in a.get('runways', [])],
            frequencies=a.get('frequencies', []),
        ) for a in d.get('airports', [])
    ]
    navaids = [Navaid(**n) for n in d.get('navaids', [])]
    return Facilities(
        center_lat=d['center_lat'], center_lon=d['center_lon'],
        radius_nm=d['radius_nm'], fetched_at=d['fetched_at'],
        airports=airports, navaids=navaids,
    )


def _haversine_nm(lat1, lon1, lat2, lon2):
    # Inlined to avoid the `geo` import in this module's hot path.
    R = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))
