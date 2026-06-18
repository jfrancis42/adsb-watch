"""Classify an aircraft's flight phase using nearby airport / runway data.

Pure functional — given a snapshot of the aircraft state and a Facilities
snapshot, returns (phase, airport_ident, runway_ident). No I/O, no globals,
trivially unit-testable.

Phase taxonomy:
    PARKED    — on an airport surface, ~zero ground speed, low altitude
    TAXI      — on an airport surface, slow (< taxi-speed cap)
    TAKEOFF   — on or just above a runway, accelerating, climbing
    LANDING   — on or just above a runway, descending, slow-ish
    APPROACH  — aligned with a runway final approach course, low and descending
    DEPART    — aligned with a runway departure course, low and climbing
    AIRBORNE  — none of the above (cruise, transit, en-route)

The classifier works with whatever the aircraft has reported. If altitude or
speed isn't known yet, it returns AIRBORNE rather than guessing.
"""
import math
from dataclasses import dataclass
from typing import Optional


# ---- Tunables (in feet, knots, NM, degrees) -------------------------------

RUNWAY_LATERAL_NM       = 0.10   # ~600 ft from centerline still counts
APPROACH_LATERAL_NM     = 0.50   # ~3000 ft cone for final / departure leg
APPROACH_DISTANCE_NM    = 8.0    # how far down the extended centerline we look
HEADING_TOL_DEG         = 25.0   # alignment with runway heading
ON_FIELD_NM             = 2.5    # large airports span 2+ NM end-to-end
ON_FIELD_AGL_FT         = 200.0  # … and within this many ft above field elevation
TAXI_SPEED_MAX_KT       = 40.0
PARKED_SPEED_MAX_KT     = 3.0
APPROACH_AGL_MAX_FT     = 3000.0
APPROACH_AGL_MIN_FT     = -100.0  # tolerate baro alt below field elevation
APPROACH_VRATE_DESCEND  = -200.0  # fpm, descending
DEPART_VRATE_CLIMB      =  300.0  # fpm, climbing
TAKEOFF_VRATE_CLIMB     =  500.0  # fpm — clear positive rate near runway

R_EARTH_NM = 3440.065


# ---- public API ----------------------------------------------------------

@dataclass
class PhaseResult:
    phase: str                          # one of the constants below
    airport_ident: Optional[str] = None
    runway: Optional[str] = None        # e.g. '25L' (the end of approach/use)
    detail: Optional[str] = None        # human-readable hint, optional

PARKED   = 'PARKED'
TAXI     = 'TAXI'
TAKEOFF  = 'TAKEOFF'
LANDING  = 'LANDING'
APPROACH = 'APPROACH'
DEPART   = 'DEPART'
AIRBORNE = 'AIRBORNE'


def classify(*, lat: Optional[float], lon: Optional[float],
             alt_ft: Optional[float], course_deg: Optional[float],
             speed_kt: Optional[float], vrate_fpm: Optional[float],
             facilities) -> PhaseResult:
    """Main entry point. `facilities` is an `airports.Facilities` (or None).
    Any missing field except lat/lon falls back to permissive defaults."""
    if lat is None or lon is None or facilities is None:
        return PhaseResult(AIRBORNE)

    nearest_ap = _nearest_airport(facilities, lat, lon)
    if nearest_ap is None:
        return PhaseResult(AIRBORNE)
    ap, ap_distance_nm = nearest_ap
    field_elev = ap.elev_ft or 0.0
    agl = (alt_ft - field_elev) if alt_ft is not None else None

    on_field = (ap_distance_nm <= ON_FIELD_NM
                and (agl is None or agl <= ON_FIELD_AGL_FT))

    # ---- Runway-aligned checks (TAKEOFF / LANDING / APPROACH / DEPART) ----
    rwy_match = _match_runway(ap, lat, lon, course_deg, agl, on_field)
    if rwy_match is not None:
        runway_id, on_runway, on_extended = rwy_match
        if on_runway:
            # Accelerating climbing => takeoff; slowing descending or on-the-deck
            # decelerating => landing; otherwise treat as taxi-on-runway.
            if vrate_fpm is not None and vrate_fpm > TAKEOFF_VRATE_CLIMB:
                return PhaseResult(TAKEOFF, ap.ident, runway_id,
                                   f'on rwy {runway_id}, climbing {vrate_fpm:.0f}fpm')
            if vrate_fpm is not None and vrate_fpm < APPROACH_VRATE_DESCEND:
                return PhaseResult(LANDING, ap.ident, runway_id,
                                   f'on rwy {runway_id}, descending {vrate_fpm:.0f}fpm')
            if speed_kt is not None and speed_kt > TAXI_SPEED_MAX_KT:
                # High groundspeed on runway with no vrate yet — call it takeoff
                # roll if airborne soon; otherwise landing rollout.
                return PhaseResult(TAKEOFF if (alt_ft and agl > 50) else LANDING,
                                   ap.ident, runway_id, 'on runway')
            return PhaseResult(TAXI, ap.ident, runway_id, 'on runway, slow')

        if on_extended and agl is not None and agl < APPROACH_AGL_MAX_FT:
            if vrate_fpm is not None and vrate_fpm < APPROACH_VRATE_DESCEND:
                return PhaseResult(APPROACH, ap.ident, runway_id,
                                   f'final {runway_id}')
            if vrate_fpm is not None and vrate_fpm > DEPART_VRATE_CLIMB:
                return PhaseResult(DEPART, ap.ident, runway_id,
                                   f'departure {runway_id}')

    # ---- Surface checks (PARKED / TAXI) ----------------------------------
    if on_field:
        if speed_kt is None:
            return PhaseResult(AIRBORNE, ap.ident)   # not enough info
        if speed_kt < PARKED_SPEED_MAX_KT:
            return PhaseResult(PARKED, ap.ident, detail=f'at {ap.ident}')
        if speed_kt < TAXI_SPEED_MAX_KT:
            return PhaseResult(TAXI, ap.ident, detail=f'taxi at {ap.ident}')

    return PhaseResult(AIRBORNE)


# ---- runway / airport matching --------------------------------------------

def _nearest_airport(facilities, lat: float, lon: float):
    """Returns (Airport, distance_nm) or None."""
    best = None
    best_d = math.inf
    for ap in facilities.airports:
        d = _haversine_nm(lat, lon, ap.lat, ap.lon)
        if d < best_d:
            best, best_d = ap, d
    if best is None:
        return None
    return best, best_d


def _match_runway(airport, lat, lon, course_deg, agl_ft, on_field):
    """Test every runway end at the airport. Returns the best match
    (runway_id, on_runway_bool, on_extended_bool) or None."""
    if not airport.runways:
        return None

    candidates = []
    for rw in airport.runways:
        if rw.closed:
            continue
        for end_id, e_lat, e_lon, e_hdg, other_lat, other_lon in (
            (rw.le_ident, rw.le_lat, rw.le_lon, rw.le_heading_degt, rw.he_lat, rw.he_lon),
            (rw.he_ident, rw.he_lat, rw.he_lon, rw.he_heading_degt, rw.le_lat, rw.le_lon),
        ):
            if e_lat is None or e_lon is None or other_lat is None or other_lon is None:
                continue
            if e_hdg is None:
                continue
            cand = _runway_geometry(
                end_id=end_id, e_lat=e_lat, e_lon=e_lon, e_hdg=e_hdg,
                other_lat=other_lat, other_lon=other_lon,
                ac_lat=lat, ac_lon=lon, ac_course=course_deg)
            if cand is not None:
                candidates.append(cand)

    if not candidates:
        return None
    # Prefer "on runway" matches over "on extended centerline".
    candidates.sort(key=lambda c: (not c['on_runway'], not c['on_extended'],
                                   abs(c['lateral_nm'])))
    best = candidates[0]
    if not (best['on_runway'] or best['on_extended']):
        return None
    return best['end_id'], best['on_runway'], best['on_extended']


def _runway_geometry(*, end_id, e_lat, e_lon, e_hdg, other_lat, other_lon,
                     ac_lat, ac_lon, ac_course):
    """Compute lateral offset from runway centerline and along-track distance
    from the runway threshold. The "approach end" of a runway is the point
    where aircraft land — so for runway 25, e_lat/e_lon is the 25 threshold and
    other_lat/other_lon is the far (07) end.

    Returns dict with on_runway / on_extended / lateral_nm, or None if the
    aircraft isn't reasonably aligned with this end."""
    # Flat-earth projection centered on the runway threshold.
    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * math.cos(math.radians(e_lat))
    ax = (ac_lon - e_lon) * nm_per_deg_lon
    ay = (ac_lat - e_lat) * nm_per_deg_lat

    # Runway axis unit vector — points from this threshold toward the far end.
    rx = (other_lon - e_lon) * nm_per_deg_lon
    ry = (other_lat - e_lat) * nm_per_deg_lat
    rlen = math.hypot(rx, ry)
    if rlen <= 1e-9:
        return None
    ux, uy = rx / rlen, ry / rlen

    # along = signed distance from threshold along runway. lateral = perpendicular.
    along    = ax * ux + ay * uy
    lateral  = abs(-ax * uy + ay * ux)

    # Heading alignment (only meaningful if we know the aircraft's course).
    if ac_course is not None:
        diff = (ac_course - e_hdg + 540.0) % 360.0 - 180.0
        if abs(diff) > HEADING_TOL_DEG:
            return None

    on_runway   = (along >= -RUNWAY_LATERAL_NM
                   and along <= rlen + RUNWAY_LATERAL_NM
                   and lateral <= RUNWAY_LATERAL_NM)
    on_extended = (along < 0
                   and along >= -APPROACH_DISTANCE_NM
                   and lateral <= APPROACH_LATERAL_NM)
    return {
        'end_id': end_id, 'on_runway': on_runway,
        'on_extended': on_extended, 'lateral_nm': lateral,
    }


def _haversine_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R_EARTH_NM * math.asin(math.sqrt(a))
