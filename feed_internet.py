#!/usr/bin/env python3
"""Internet ADS-B feeders — pull live traffic from public aggregators.

Ported from vestigare (server/feeds/): the REST endpoints, poll cadences, and
the OpenSky SI-unit conversion come from there. The transport is adapted to
adsb-watch's house style — stdlib ``urllib`` in a daemon thread per source,
matching ``SbsFeeder`` / ``RegistryClient`` — so no asyncio/httpx dependency is
added.

Each source normalises to the same field set and pushes into the engine via
``update_aircraft(source='internet')``. The engine gives fresh *local* RTL-SDR
data priority for a given aircraft; internet data only fills aircraft the local
receiver isn't currently hearing (see ``Engine.update_aircraft``).

The engine's 5 Hz dead-reckoning applies to internet-sourced tracks exactly as
it does to local ones — as long as a source reports track + ground speed (all
of these do), the display stays smooth between the ~1 Hz (or slower) network
updates.

Canonical fields we read (superset; all optional except a position):
  hex        ICAO 24-bit hex        flight   callsign
  lat, lon   decimal degrees        alt_baro feet MSL or the string "ground"
  gs         ground speed, knots    track    true track, degrees
  baro_rate  vertical rate, ft/min  (geom_rate as fallback)
"""
import base64
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request

from engine import Engine


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------
# Some aggregators (airplanes.live) reject the default python-urllib
# User-Agent with 403. Send a plain identifying UA on every request.
_USER_AGENT = 'adsb-watch/1.0 (+https://github.com/jfrancis42/adsb-watch)'


def _get_json(url: str, timeout: float, headers: dict | None = None):
    hdrs = {'User-Agent': _USER_AGENT, 'Accept': 'application/json'}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


def _num(x):
    """Return x as a float if it's a real number (not bool/str/None), else None."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None


# --------------------------------------------------------------------------
# Per-source fetchers — each returns a list of canonical aircraft dicts
# --------------------------------------------------------------------------
_ADSB_LOL_BASE       = 'https://api.adsb.lol/v2'
_AIRPLANES_LIVE_BASE = 'https://api.airplanes.live/v2'
_OPENSKY_BASE        = 'https://opensky-network.org/api'


def _fetch_point(base: str, lat: float, lon: float, radius_nm: float,
                 timeout: float) -> list[dict]:
    """adsb.lol and airplanes.live share the readsb/tar1090 point endpoint and
    an already-canonical response ({'ac': [...]})."""
    url = f'{base}/point/{lat}/{lon}/{radius_nm}'
    data = _get_json(url, timeout)
    return data.get('ac') or []


# OpenSky state-vector field indices (states/all response)
_OS_ICAO24, _OS_CALLSIGN = 0, 1
_OS_TIME_POSITION, _OS_LONGITUDE, _OS_LATITUDE = 3, 5, 6
_OS_BARO_ALT_M, _OS_ON_GROUND, _OS_VELOCITY_MS = 7, 8, 9
_OS_TRUE_TRACK, _OS_VERT_RATE_MS, _OS_GEO_ALT_M = 10, 11, 13


def _opensky_bbox(lat: float, lon: float, radius_nm: float) -> dict:
    dlat = radius_nm / 60.0
    dlon = radius_nm / (60.0 * math.cos(math.radians(lat)))
    return {'lamin': lat - dlat, 'lomin': lon - dlon,
            'lamax': lat + dlat, 'lomax': lon + dlon}


def _opensky_normalise(state: list, now: float) -> dict | None:
    """One OpenSky state vector -> canonical dict. OpenSky is SI internally."""
    try:
        lat = state[_OS_LATITUDE]
        lon = state[_OS_LONGITUDE]
    except (IndexError, TypeError):
        return None
    if lat is None or lon is None:
        return None
    hex_id = (state[_OS_ICAO24] or '').strip().lower()
    if not hex_id:
        return None

    on_ground = bool(state[_OS_ON_GROUND])
    alt_m = state[_OS_BARO_ALT_M] if state[_OS_BARO_ALT_M] is not None else state[_OS_GEO_ALT_M]
    if on_ground or alt_m is None:
        alt_baro: float | str = 'ground'
    else:
        alt_baro = alt_m * 3.28084  # m -> ft

    vel = state[_OS_VELOCITY_MS]
    vr  = state[_OS_VERT_RATE_MS]
    t_pos = state[_OS_TIME_POSITION]

    ac: dict = {
        'hex':      hex_id,
        'flight':   (state[_OS_CALLSIGN] or '').strip() or None,
        'lat':      lat,
        'lon':      lon,
        'alt_baro': alt_baro,
        'gs':       vel * 1.94384 if vel is not None else None,      # m/s -> kt
        'track':    state[_OS_TRUE_TRACK],
        'baro_rate': vr * 196.850 if vr is not None else None,       # m/s -> ft/min
        'seen_pos': round(now - t_pos, 1) if t_pos is not None else None,
    }
    return ac


def _fetch_opensky(lat: float, lon: float, radius_nm: float, timeout: float,
                   auth_header: dict) -> list[dict]:
    params = urllib.parse.urlencode(_opensky_bbox(lat, lon, radius_nm))
    url = f'{_OPENSKY_BASE}/states/all?{params}'
    data = _get_json(url, timeout, headers=auth_header)
    states = data.get('states') or []
    now = time.time()
    return [a for s in states if (a := _opensky_normalise(s, now)) is not None]


# --------------------------------------------------------------------------
# Canonical dict -> Engine.update_aircraft kwargs
# --------------------------------------------------------------------------
def canonical_to_kwargs(ac: dict):
    """Map a canonical aircraft dict to (icao, kwargs) for update_aircraft.
    Returns (None, {}) if there's no usable ICAO hex."""
    hex_id = (ac.get('hex') or '').strip()
    if not hex_id:
        return None, {}

    kw: dict = {}
    flight = (ac.get('flight') or '').strip()
    if flight:
        kw['callsign'] = flight

    lat = _num(ac.get('lat'))
    lon = _num(ac.get('lon'))
    if lat is not None and lon is not None:
        kw['lat'], kw['lon'] = lat, lon

    alt = ac.get('alt_baro')
    if alt == 'ground':
        kw['alt_ft'] = 0.0
    else:
        alt_n = _num(alt)
        if alt_n is None:
            alt_n = _num(ac.get('alt_geom'))
        if alt_n is not None:
            kw['alt_ft'] = alt_n

    gs = _num(ac.get('gs'))
    if gs is not None:
        kw['speed_kt'] = gs
    trk = _num(ac.get('track'))
    if trk is not None:
        kw['course_deg'] = trk
    vr = ac.get('baro_rate')
    vr_n = _num(vr) if vr is not None else _num(ac.get('geom_rate'))
    if vr_n is not None:
        kw['vrate_fpm'] = vr_n

    return hex_id, kw


# --------------------------------------------------------------------------
# Source registry
# --------------------------------------------------------------------------
# name -> (label, base_poll_interval_s, needs_opensky_auth)
# Poll intervals.  adsb.lol and airplanes.live share a ~1 req/s limit, and
# polling AT the limit means tripping it: on 2026-08-29 a 1.0 s interval drew a
# steady stream of `429 Too Many Requests` from adsb.lol.  2.0 s halves the
# request rate and still leaves 5x margin against the 10 s track expiry, so the
# display stays populated between polls.
SOURCES = {
    'adsb_lol':       ('adsb.lol',        2.0),
    'airplanes_live': ('airplanes.live',  2.0),
    'opensky':        ('OpenSky',        10.0),
}


def available_sources() -> list[str]:
    return list(SOURCES.keys())


# --------------------------------------------------------------------------
# Threaded feeder (one per selected source)
# --------------------------------------------------------------------------
class InternetFeeder(threading.Thread):
    """Poll one internet ADS-B source and push canonical updates into the engine
    tagged source='internet'. Reads the observer position fresh each poll, so a
    moving (gpsd) receiver re-centres the query automatically."""
    daemon = True

    def __init__(self, engine: Engine, source: str, get_observer,
                 radius_nm: float, recorder=None, should_poll=None):
        if source not in SOURCES:
            raise ValueError(f'unknown internet source {source!r}; '
                             f'choose from {", ".join(SOURCES)}')
        self.name_id = f'net-{source}'
        super().__init__(name=self.name_id)
        self.engine = engine
        self.source = source
        self.label, self.interval = SOURCES[source]
        self.get_observer = get_observer
        self.radius_nm = radius_nm
        self.recorder = recorder
        # Optional zero-arg predicate: return False to skip polling this cycle
        # (e.g. no web viewers connected). None => always poll. This is what
        # makes the public instance demand-driven — one shared poll stream for
        # all viewers, and none at all when nobody is watching.
        self.should_poll = should_poll
        self._stop = threading.Event()

        # OpenSky: optional auth improves the rate limit (5 s vs 10 s anon).
        self._auth_header: dict = {}
        if source == 'opensky':
            user = os.environ.get('OPENSKY_USERNAME')
            pw   = os.environ.get('OPENSKY_PASSWORD')
            if user and pw:
                token = base64.b64encode(f'{user}:{pw}'.encode()).decode()
                self._auth_header = {'Authorization': f'Basic {token}'}
                self.interval = 5.0

    def stop(self):
        self._stop.set()

    def _fetch(self, lat: float, lon: float) -> list[dict]:
        if self.source == 'adsb_lol':
            return _fetch_point(_ADSB_LOL_BASE, lat, lon, self.radius_nm, 8.0)
        if self.source == 'airplanes_live':
            return _fetch_point(_AIRPLANES_LIVE_BASE, lat, lon, self.radius_nm, 8.0)
        if self.source == 'opensky':
            return _fetch_opensky(lat, lon, self.radius_nm, 12.0, self._auth_header)
        return []

    def _ingest(self, aircraft: list[dict]) -> int:
        pushed = 0
        for ac in aircraft:
            icao, kw = canonical_to_kwargs(ac)
            if icao is None or 'lat' not in kw:
                continue  # need a position to plot
            self.engine.update_aircraft(icao, source='internet', **kw)
            pushed += 1
        return pushed

    def run(self):
        # Backoff caps, and why there are two.
        #
        # A single 429 used to double the backoff toward a 30 s cap while
        # tracks expire after 10 s, so one throttled poll blanked the whole
        # display for 15+ seconds -- aircraft appeared for ~10 s, faded to
        # predicted, vanished, and repeated on a 25 s cycle.  The transient
        # cap is therefore kept BELOW the expiry window: a rate-limit blip
        # costs a frame or two, not the screen.
        #
        # A persistently failing feed is a different problem and must not be
        # retried every few seconds forever -- airplanes.live has been
        # answering 403 to everything, including /ping, since at least
        # 2026-08-29.  After a run of consecutive failures the cap opens up so
        # a dead endpoint is polled sparingly.
        backoff = self.interval
        fails = 0
        transient_cap = 8.0
        persistent_cap = 120.0 if self.source == 'opensky' else 120.0
        while not self._stop.is_set():
            if self.should_poll is not None and not self.should_poll():
                # No consumers right now (e.g. no web clients) — stay idle and
                # don't hit the aggregator. Re-check on the normal cadence.
                self.engine.report_feeder(self.name_id,
                                          f'{self.label}: idle (no viewers)')
                self._stop.wait(self.interval)
                continue
            pos = self.get_observer()
            if pos is None:
                self.engine.report_feeder(self.name_id,
                                          f'{self.label}: waiting for observer position')
                self._stop.wait(self.interval)
                continue
            lat, lon = pos
            try:
                aircraft = self._fetch(lat, lon)
                if self.recorder is not None:
                    self.recorder.log(self.name_id, json.dumps({'ac': aircraft}))
                n = self._ingest(aircraft)
                self.engine.bump_count(self.name_id, n)
                self.engine.report_feeder(
                    self.name_id, f'connected {self.label} ({n} ac in {self.radius_nm:g} NM)')
                backoff = self.interval
                fails = 0
            except Exception as e:
                fails += 1
                self.engine.report_feeder(
                    self.name_id,
                    f'{self.label} error: {type(e).__name__}: {e}'
                    + (f' (x{fails})' if fails > 1 else ''))
                self._stop.wait(backoff)
                cap = transient_cap if fails < 5 else persistent_cap
                backoff = min(backoff * 2, cap)
                continue
            self._stop.wait(self.interval)
