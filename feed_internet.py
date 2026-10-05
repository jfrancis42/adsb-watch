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
from geo import haversine_nm
from exits import ExitRotator, is_network


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------
# Some aggregators (airplanes.live) reject the default python-urllib
# User-Agent with 403. Send a plain identifying UA on every request.
_USER_AGENT = 'adsb-watch/1.0 (+https://github.com/jfrancis42/adsb-watch)'


def _get_json(url: str, timeout: float, headers: dict | None = None, opener=None):
    hdrs = {'User-Agent': _USER_AGENT, 'Accept': 'application/json'}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    open_ = opener.open if opener is not None else urllib.request.urlopen
    with open_(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


def _stamp(aircraft: list[dict], ref_now) -> list[dict]:
    """Give each aircraft ``_pos_t``: the epoch time of its position fix,
    = the answer's own clock minus ``seen_pos``.

    Without it every position was taken as fixed NOW, the moment it arrived.
    An aggregator re-serving an aircraft's last fix (seen_pos growing) then
    snapped it back to that point on every poll -- the plane FROZE -- and the
    radar re-reading the hub faster than the hub refreshes (1 s vs 2 s) put
    each plane back by a second's travel: planes jumped BACKWARDS along their
    tracks (2026-10-05: 12% of all steps). The engine now drops a fix that is
    not newer than the one it has, and dead-reckons from the real fix time."""
    if not isinstance(ref_now, (int, float)) or isinstance(ref_now, bool):
        return aircraft
    ref = ref_now / 1000.0 if ref_now > 1e11 else float(ref_now)   # adsb.lol: ms
    for ac in aircraft:
        sp = ac.get('seen_pos')
        if isinstance(sp, (int, float)) and not isinstance(sp, bool):
            ac['_pos_t'] = ref - float(sp)
    return aircraft


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

# adsb-hub (~/Dropbox/build/adsb-hub): the estate's one ADS-B poller, which
# also merges the office's own dump1090-fa receivers. Address it by NAME.
_HUB_URL = os.environ.get('ADSB_HUB_URL',
                          'http://adsb-hub.n0gq.org:8080/aircraft.json')


def _fetch_hub(url: str, lat: float, lon: float, radius_nm: float,
               timeout: float) -> tuple[list[dict], dict]:
    """-> (aircraft, hub status). ?live=1 tells the hub a viewer is watching,
    which makes it poll the internet every 2 s instead of 5."""
    q = urllib.parse.urlencode({'lat': lat, 'lon': lon, 'nm': radius_nm, 'live': 1})
    data = _get_json(f'{url}?{q}', timeout)
    return _stamp(data.get('aircraft') or [], data.get('now')), data.get('feed') or {}


def _hub_projected(ac: dict) -> dict:
    """Plot the HUB's dead reckoning: it projects every fix to its answer's
    `now` (dr_*), and that is the position used. The engine is told when the
    real fix was (_fix_t) so age and "predicted" stay honest. Read four times
    a second, the engine's own projection only bridges < 0.25 s between
    samples -- the hub does the dead reckoning (owner's rule, 2026-10-05)."""
    if ac.get('dr_lat') is None or ac.get('dr_lon') is None or '_pos_t' not in ac:
        return ac
    b = dict(ac)
    fix_t = b['_pos_t']
    b['lat'], b['lon'] = b['dr_lat'], b['dr_lon']
    if 'dr_alt' in b:
        b['alt_baro'] = b['dr_alt']
    b['_pos_t'] = fix_t + float(b.get('seen_pos') or 0.0)     # the hub's `now`
    b['_fix_t'] = fix_t
    return b


def _fetch_point(base: str, lat: float, lon: float, radius_nm: float,
                 timeout: float, opener=None) -> list[dict]:
    """adsb.lol and airplanes.live share the readsb/tar1090 point endpoint and
    an already-canonical response ({'ac': [...]})."""
    url = f'{base}/point/{lat}/{lon}/{radius_nm}'
    data = _get_json(url, timeout, opener=opener)
    return _stamp(data.get('ac') or [], data.get('now'))


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
    if t_pos is not None:
        ac['_pos_t'] = float(t_pos)
    return ac


def _fetch_opensky(lat: float, lon: float, radius_nm: float, timeout: float,
                   auth_header: dict, opener=None) -> list[dict]:
    params = urllib.parse.urlencode(_opensky_bbox(lat, lon, radius_nm))
    url = f'{_OPENSKY_BASE}/states/all?{params}'
    data = _get_json(url, timeout, headers=auth_header, opener=opener)
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
    if isinstance(ac.get('_pos_t'), float):
        kw['pos_time'] = ac['_pos_t']
    if isinstance(ac.get('_fix_t'), float):
        kw['fix_time'] = ac['_fix_t']
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
#
# 'hub' is the default since 2026-10-05: one poller for the estate instead of
# this radar and adsb-log both polling from the office address (adsb.lol
# answered with 429s that froze and blanked the radar). Reading the hub over
# the LAN costs nothing, hence 0.25 s: the hub dead-reckons, and four samples
# a second keep the plot smooth without a second projection here. When the scope is centred where the hub's
# circle does not reach -- or the hub is down -- this feeder polls adsb.lol
# itself, at adsb.lol's own 2 s.
SOURCES = {
    'hub':            ('adsb-hub',        0.25),
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
                 radius_nm: float, recorder=None, should_poll=None, exits=None):
        if source not in SOURCES:
            raise ValueError(f'unknown internet source {source!r}; '
                             f'choose from {", ".join(SOURCES)}')
        self.name_id = f'net-{source}'
        super().__init__(name=self.name_id)
        self.engine = engine
        self.source = source
        self.label, self.interval = SOURCES[source]
        self.get_observer = get_observer
        # A number, or a zero-arg callable read on every poll (web viewers'
        # window coverage -- see ui_web.Coverage).
        self.radius_nm = radius_nm
        self.recorder = recorder
        # Optional zero-arg predicate: return False to skip polling this cycle
        # (e.g. no web viewers connected). None => always poll. This is what
        # makes the public instance demand-driven — one shared poll stream for
        # all viewers, and none at all when nobody is watching.
        self.should_poll = should_poll
        self._stop = threading.Event()
        # Egress exits (exits.py): a broken PATH moves to the next exit; a
        # refusal (401/403/429) does not -- it keeps the backoff below.
        self.rotator = ExitRotator(exits or [('direct', None)])
        self._net_streak = 0
        # hub: its circle (lat, lon, radius), learned from its answers; None
        # until the first one, so the hub is always tried first.
        self.hub_url = _HUB_URL
        self.hub_circle: tuple[float, float, float] | None = None
        self.hub_error = ''

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

    def _radius(self) -> float:
        r = self.radius_nm() if callable(self.radius_nm) else self.radius_nm
        # Whole NM: a fractional radius changes the URL on every resize for
        # no benefit.
        return float(math.ceil(r))

    def _hub_covers(self, lat: float, lon: float, radius_nm: float) -> bool:
        if self.hub_circle is None:
            return True
        hlat, hlon, hr = self.hub_circle
        return haversine_nm(hlat, hlon, lat, lon) + radius_nm <= hr + 1.0

    def _fetch_via_hub(self, lat: float, lon: float, radius_nm: float) -> list[dict]:
        """The hub when it covers the view and answers; else adsb.lol directly.
        A hub failure is NOT a broken exit: it never rotates the exits, which
        are for reaching the internet, and the hub is on the LAN."""
        if self._hub_covers(lat, lon, radius_nm):
            try:
                ac, feed = _fetch_hub(self.hub_url, lat, lon, radius_nm, 5.0)
                c = feed.get('center') or {}
                if {'lat', 'lon', 'radius_nm'} <= c.keys():
                    self.hub_circle = (c['lat'], c['lon'], c['radius_nm'])
                self.label, self.interval, self.hub_error = 'adsb-hub', 0.25, ''
                return [_hub_projected(a) for a in ac]
            except Exception as e:            # noqa: BLE001 -- any hub failure
                self.hub_error = f'{type(e).__name__}: {e}'
                why = 'hub down'
        else:
            why = 'outside hub'
        self.label, self.interval = f'adsb.lol direct ({why})', 2.0
        return _fetch_point(_ADSB_LOL_BASE, lat, lon, radius_nm, 8.0, self.rotator.opener)

    def _fetch(self, lat: float, lon: float, radius_nm: float) -> list[dict]:
        op = self.rotator.opener
        if self.source == 'hub':
            return self._fetch_via_hub(lat, lon, radius_nm)
        if self.source == 'adsb_lol':
            return _fetch_point(_ADSB_LOL_BASE, lat, lon, radius_nm, 8.0, op)
        if self.source == 'airplanes_live':
            return _fetch_point(_AIRPLANES_LIVE_BASE, lat, lon, radius_nm, 8.0, op)
        if self.source == 'opensky':
            return _fetch_opensky(lat, lon, radius_nm, 12.0, self._auth_header, op)
        return []

    def _ingest(self, aircraft: list[dict]) -> int:
        pushed = 0
        for ac in aircraft:
            icao, kw = canonical_to_kwargs(ac)
            if icao is None or 'lat' not in kw:
                continue  # need a position to plot
            # The hub marks what the office's own receivers heard: that is
            # LOCAL data, and the engine gives it precedence like any local feed.
            src = 'local' if ac.get('src') == 'rf' else 'internet'
            self.engine.update_aircraft(icao, source=src, **kw)
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
            radius = self._radius()
            # Before the fetch, not after a success: a hub that answers
            # nothing but 429 never produces a success to fail back from.
            fb = self.rotator.maybe_failback()
            if fb:
                self.engine.report_feeder(self.name_id, f'{self.label}: {fb}')
            try:
                aircraft = self._fetch(lat, lon, radius)
                if self.recorder is not None:
                    self.recorder.log(self.name_id, json.dumps({'ac': aircraft}))
                n = self._ingest(aircraft)
                self.engine.bump_count(self.name_id, n)
                via = (f' via {self.rotator.name}'
                       if len(self.rotator.exits) > 1 and self.label != 'adsb-hub' else '')
                self.engine.report_feeder(
                    self.name_id, f'connected {self.label}{via} ({n} ac in {radius:g} NM)')
                backoff = self.interval
                fails = 0
                self._net_streak = 0
            except Exception as e:
                # A broken PATH: next exit and retry at once, so a dead proxy
                # does not blank the radar. One full round of exits failing
                # falls through to the normal backoff below. A REFUSAL never
                # gets here as a path problem -- is_network() is False for it.
                if is_network(e) and len(self.rotator.exits) > 1:
                    self._net_streak += 1
                    msg = self.rotator.network_failure()
                    self.engine.report_feeder(
                        self.name_id, f'{self.label}: {type(e).__name__}; {msg}')
                    if self._net_streak < len(self.rotator.exits):
                        continue
                    self._net_streak = 0
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
