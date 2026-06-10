"""UI-agnostic ADS-B tracking engine.

Holds aircraft state, observer state, and registry lookups. Feeders push
updates in; UIs read snapshots out. No curses, no I/O — drop in a Qt or
web front-end without touching this file.
"""
import math
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Optional

from geo import (haversine_nm, bearing_deg, elevation_deg,
                 closest_approach, dead_reckon)
import phase as phase_mod


def _ema_angle(prev_deg: float, new_deg: float, alpha: float) -> float:
    """Blend two compass bearings without crossing the 0/360 seam."""
    p = math.radians(prev_deg)
    n = math.radians(new_deg)
    sx = (1 - alpha) * math.sin(p) + alpha * math.sin(n)
    cx = (1 - alpha) * math.cos(p) + alpha * math.cos(n)
    return (math.degrees(math.atan2(sx, cx)) + 360.0) % 360.0


@dataclass
class Aircraft:
    icao: str
    callsign: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_ft: Optional[float] = None
    course_deg: Optional[float] = None
    speed_kt: Optional[float] = None
    vrate_fpm: Optional[float] = None
    last_seen: float = 0.0
    last_pos: float = 0.0
    # Smoothed CPA — EMA over fresh predictions, kept on the Aircraft so
    # the displayed countdown doesn't jitter every time a new velocity report
    # arrives. Reset whenever the aircraft is no longer approaching.
    cpa_nm_smooth:  Optional[float] = None
    cpa_az_smooth:  Optional[float] = None
    cpa_t_smooth:   Optional[float] = None  # seconds-to-CPA at cpa_smooth_at
    cpa_smooth_at:  float = 0.0
    # Filled by RegistryClient — never mutated by ADS-B feeder
    n_number:     Optional[str] = None
    manufacturer: Optional[str] = None
    model:        Optional[str] = None
    owner:        Optional[str] = None
    registry_checked: bool = False


@dataclass
class Observer:
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_ft: float = 0.0
    last_seen: float = 0.0


@dataclass
class Track:
    """A computed view of one aircraft from the observer's perspective.
    This is what UIs render — no lat/lon math required of them."""
    icao: str
    callsign: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    course_deg: Optional[float]
    speed_kt: Optional[float]
    alt_ft: Optional[float]
    distance_nm: Optional[float]
    azimuth_deg: Optional[float]
    elevation_deg: Optional[float]
    cpa_nm: Optional[float]            # closest-approach distance (NM)
    cpa_seconds: Optional[float]       # time-to-CPA (s), smoothed
    cpa_azimuth_deg: Optional[float]   # bearing from observer to CPA point
    closing: bool
    age_s: float
    predicted: bool = False            # True when lat/lon/alt are dead-reckoned, not freshly reported
    predicted_age_s: float = 0.0       # how long the position has been dead-reckoned
    phase: str = 'AIRBORNE'            # PARKED/TAXI/TAKEOFF/LANDING/APPROACH/DEPART/AIRBORNE
    airport: Optional[str] = None      # ICAO ident of the airport, if any
    runway:  Optional[str] = None      # e.g. "25L"
    phase_detail: Optional[str] = None
    n_number:     Optional[str] = None
    manufacturer: Optional[str] = None
    model:        Optional[str] = None
    owner:        Optional[str] = None


@dataclass
class Snapshot:
    observer: Observer
    tracks: list   # list[Track], sorted by distance ascending
    cpa_threshold_nm: float
    generated_at: float
    feeders: dict  # {feeder_name: status_string}
    counts:  dict  # {feeder_name: messages_seen}


class Engine:
    """Thread-safe state holder. All public methods grab `_lock`."""

    def __init__(self, expiry_s: float = 10.0, cpa_threshold_nm: float = 1.0,
                 registry_cache=None):
        self._lock = threading.RLock()
        self._aircraft: dict[str, Aircraft] = {}
        self._observer = Observer()
        self.expiry_s = expiry_s
        self.cpa_threshold_nm = cpa_threshold_nm
        self._feeders: dict[str, str] = {}
        self._counts:  dict[str, int] = {}
        # Optional persistent registry cache (cache.RegistryCache instance, or
        # any object with .get(icao) -> (hit, data) and .put(icao, data)). If
        # None, we skip caching — registry lookups still work, they just hit
        # the network every time the aircraft reappears after expiry.
        self._registry_cache = registry_cache
        self._facilities = None  # airports.Facilities snapshot or None

    def report_feeder(self, name: str, status: str):
        with self._lock:
            self._feeders[name] = status

    def bump_count(self, name: str, n: int = 1):
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + n

    # ----- feeder API ------------------------------------------------------

    def update_observer(self, lat, lon, alt_ft=0.0, *, manual: bool = False):
        """Set observer position. `manual=True` (the UI's 'o' prompt or
        --fixed-* flags) latches the position so subsequent gpsd updates
        won't overwrite it. Pass manual=False from gpsd."""
        with self._lock:
            if getattr(self, '_observer_manual', False) and not manual:
                return
            self._observer = Observer(lat=lat, lon=lon, alt_ft=alt_ft or 0.0,
                                      last_seen=time.time())
            if manual:
                self._observer_manual = True

    def set_facilities(self, facilities):
        """Called by FacilitiesClient when the airport set has been refreshed."""
        with self._lock:
            self._facilities = facilities

    def get_observer_position(self):
        with self._lock:
            o = self._observer
        if o.lat is None or o.lon is None:
            return None
        return (o.lat, o.lon)

    def update_aircraft(self, icao: str, *, callsign=None, lat=None, lon=None,
                        alt_ft=None, course_deg=None, speed_kt=None,
                        vrate_fpm=None):
        icao = icao.upper()
        now = time.time()
        with self._lock:
            ac = self._aircraft.get(icao)
            if ac is None:
                ac = Aircraft(icao=icao)
                # Re-attach any cached registry data so a re-appearing plane
                # comes back fully populated without another HTTP hit.
                if self._registry_cache is not None:
                    hit, cached = self._registry_cache.get(icao)
                    if hit:
                        if cached is not None:
                            ac.n_number     = cached.get('n_number')
                            ac.manufacturer = cached.get('manufacturer')
                            ac.model        = cached.get('model')
                            ac.owner        = cached.get('owner')
                        ac.registry_checked = True
            if callsign is not None:   ac.callsign = callsign.strip() or ac.callsign
            if alt_ft   is not None:   ac.alt_ft = alt_ft
            if course_deg is not None: ac.course_deg = course_deg
            if speed_kt   is not None: ac.speed_kt = speed_kt
            if vrate_fpm  is not None: ac.vrate_fpm = vrate_fpm
            if lat is not None and lon is not None:
                ac.lat, ac.lon = lat, lon
                ac.last_pos = now
            ac.last_seen = now
            self._aircraft[icao] = ac

    def attach_registry(self, icao: str, *, n_number=None, manufacturer=None,
                        model=None, owner=None):
        icao = icao.upper()
        # Build the cache payload first; None means 404 (cached as a sentinel).
        if (n_number is None and manufacturer is None
                and model is None and owner is None):
            payload = None
        else:
            payload = {
                'n_number': n_number, 'manufacturer': manufacturer,
                'model': model, 'owner': owner,
            }
        if self._registry_cache is not None:
            self._registry_cache.put(icao, payload)
        with self._lock:
            ac = self._aircraft.get(icao)
            if ac is None:
                return
            ac.n_number     = n_number
            ac.manufacturer = manufacturer
            ac.model        = model
            ac.owner        = owner
            ac.registry_checked = True

    def pending_registry_lookups(self) -> list[str]:
        """Return ICAOs we haven't tried to look up yet."""
        with self._lock:
            return [a.icao for a in self._aircraft.values()
                    if not a.registry_checked]

    # ----- read API --------------------------------------------------------

    def snapshot(self) -> Snapshot:
        now = time.time()
        with self._lock:
            self._expire(now)
            obs = replace(self._observer)
            # Only surface aircraft whose position is fresh — drop ICAOs we've
            # only heard squitter/velocity from, and drop ones whose last
            # position fix is older than expiry_s (even if they keep sending
            # other message types).
            visible = [a for a in self._aircraft.values()
                       if a.lat is not None and a.lon is not None
                       and (now - a.last_pos) <= self.expiry_s]
            tracks = [self._track_for(a, obs, now) for a in visible]
        tracks.sort(key=lambda t: (t.distance_nm is None, t.distance_nm or 0.0))
        with self._lock:
            feeders = dict(self._feeders)
            counts  = dict(self._counts)
        return Snapshot(observer=obs, tracks=tracks,
                        cpa_threshold_nm=self.cpa_threshold_nm,
                        generated_at=now,
                        feeders=feeders, counts=counts)

    # ----- internals -------------------------------------------------------

    def _expire(self, now: float):
        dead = [k for k, a in self._aircraft.items()
                if now - a.last_seen > self.expiry_s]
        for k in dead:
            del self._aircraft[k]

    # EMA smoothing factor for CPA values. 0.3 ≈ 3-update time constant —
    # fast enough to track real maneuvers, slow enough to stop one noisy
    # velocity report from yanking the time-to-CPA across the screen.
    _CPA_ALPHA = 0.3

    # Aircraft positions older than this (in seconds) are considered "stale"
    # — we still display them by dead-reckoning forward, but the UI marks
    # them as predicted rather than real.
    PREDICT_STALE_THRESHOLD_S = 3.0

    def _track_for(self, a: Aircraft, obs: Observer, now: float) -> Track:
        # Dead-reckon position forward from the last real fix. We don't
        # mutate the Aircraft (real reports must be the only thing that
        # writes lat/lon/alt) — we hand the projected values into the
        # snapshot so geometry / phase / CPA all use them.
        elapsed = max(0.0, now - a.last_pos)
        if a.lat is not None and a.lon is not None:
            dr_lat, dr_lon, dr_alt = dead_reckon(
                a.lat, a.lon, a.alt_ft,
                a.course_deg, a.speed_kt, a.vrate_fpm,
                elapsed_s=elapsed)
        else:
            dr_lat = dr_lon = dr_alt = None

        predicted_age = elapsed
        is_predicted = elapsed >= self.PREDICT_STALE_THRESHOLD_S

        dist = az = el = None
        cpa_d = cpa_t = cpa_az = None
        closing = False
        have_obs = obs.lat is not None and obs.lon is not None
        have_pos = dr_lat is not None and dr_lon is not None
        if have_obs and have_pos:
            dist = haversine_nm(obs.lat, obs.lon, dr_lat, dr_lon)
            az   = bearing_deg(obs.lat, obs.lon, dr_lat, dr_lon)
            el   = elevation_deg(dist, obs.alt_ft, dr_alt or 0.0)
            if a.course_deg is not None and a.speed_kt:
                pred = closest_approach(obs.lat, obs.lon, dr_lat, dr_lon,
                                        a.course_deg, a.speed_kt)
                if pred is not None:
                    cpa_d, cpa_t, cpa_az = self._smooth_cpa(a, now, *pred)
                    closing = cpa_d <= self.cpa_threshold_nm and cpa_t > 0
                else:
                    self._reset_cpa(a)
            else:
                self._reset_cpa(a)
        # Phase classification uses the projected position so a parked
        # aircraft on the apron stays "PARKED" smoothly between updates.
        ph = phase_mod.classify(
            lat=dr_lat, lon=dr_lon, alt_ft=dr_alt,
            course_deg=a.course_deg, speed_kt=a.speed_kt,
            vrate_fpm=a.vrate_fpm, facilities=self._facilities)

        return Track(
            icao=a.icao, callsign=a.callsign,
            lat=dr_lat, lon=dr_lon,
            course_deg=a.course_deg, speed_kt=a.speed_kt, alt_ft=dr_alt,
            distance_nm=dist, azimuth_deg=az, elevation_deg=el,
            cpa_nm=cpa_d, cpa_seconds=cpa_t, cpa_azimuth_deg=cpa_az,
            closing=closing,
            predicted=is_predicted,
            predicted_age_s=predicted_age,
            phase=ph.phase, airport=ph.airport_ident,
            runway=ph.runway, phase_detail=ph.detail,
            age_s=now - a.last_seen,
            n_number=a.n_number, manufacturer=a.manufacturer,
            model=a.model, owner=a.owner,
        )

    def _smooth_cpa(self, a: Aircraft, now: float,
                    raw_d: float, raw_t: float, raw_az: float):
        """Smooth raw CPA prediction with an EMA. Time-to-CPA is decayed by the
        elapsed wall-clock between updates first, so the displayed countdown
        keeps ticking smoothly between predictions instead of resetting to a
        stale value. Returns (d, t, az) — already smoothed."""
        if a.cpa_nm_smooth is None:
            a.cpa_nm_smooth, a.cpa_t_smooth, a.cpa_az_smooth = raw_d, raw_t, raw_az
            a.cpa_smooth_at = now
            return raw_d, raw_t, raw_az

        # Tick the previous time estimate down by the elapsed seconds so it
        # blends with the new raw estimate on the same scale.
        decayed_t = max(0.0, a.cpa_t_smooth - (now - a.cpa_smooth_at))
        alpha = self._CPA_ALPHA

        a.cpa_nm_smooth = (1 - alpha) * a.cpa_nm_smooth + alpha * raw_d
        a.cpa_t_smooth  = (1 - alpha) * decayed_t       + alpha * raw_t
        # Bearings need angular smoothing — interpolate on the unit circle.
        a.cpa_az_smooth = _ema_angle(a.cpa_az_smooth, raw_az, alpha)
        a.cpa_smooth_at = now
        return a.cpa_nm_smooth, a.cpa_t_smooth, a.cpa_az_smooth

    def _reset_cpa(self, a: Aircraft):
        a.cpa_nm_smooth = a.cpa_t_smooth = a.cpa_az_smooth = None
        a.cpa_smooth_at = 0.0
