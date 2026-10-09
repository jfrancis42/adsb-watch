"""Traffic conflicts -- pure functions, no Engine.

Owner's rules (2026-10-08). Two airborne aircraft turn bright YELLOW (level
'warn') when their projected paths come inside the separation for the pair,
and bright RED (level 'alert') when they are inside it now; both sound an
alarm (the UI's job; this module only decides).

  * LIGHT (VFR) aircraft: 500 ft straight-line, 3-D -- a sphere.
  * IFR aircraft, with each other: the radar separation standard, a cylinder
    of 1,000 ft vertical AND 3 NM horizontal in terminal (approach-control)
    airspace or 5 NM outside it; inside it means both are violated. Pairs in
    which either aircraft is on approach, landing, taking off or departing
    are NOT checked: the tower and approach run reduced, visual and
    parallel-runway separation there, and checking them lit Denver's
    arrivals red continuously (7-8 at a time, measured 2026-10-08). The
    phase classifier only calls it APPROACH within 8 NM of the runway below
    3,000 ft AGL, which missed the arrival flow further out (pairs 0.9 NM
    apart descending through 3,400 ft AGL, 15 NM from DEN), so "airport
    traffic" also covers anything within 25 NM of a large or medium airport
    and below 6,000 ft above it (`airport_flow`).
    Exactly 1,000 ft is legal separation, and reported altitudes jitter by
    +/-25 ft, so FL350/FL360 traffic read as 974 ft and went red: inside
    means LESS than 1,000 ft by more than IFR_VERTICAL_TOLERANCE_FT.
    "Terminal" is approximated as below 18,000 ft within 40 NM of a large or
    medium airport -- ADS-B does not say whose airspace an aircraft is in.
  * A light aircraft and an IFR one: 500 ft. ATC does not separate IFR from
    VFR in most airspace, and 500 ft is the near-midair-collision distance.
    (A choice, not the owner's words -- change MIXED_RULE if wanted.)

Everything else on the ground, without an altitude, or unclassifiable is
not checked.

GEOMETRY. Each aircraft is extrapolated in a straight line at its current
ground track, ground speed and vertical rate -- the same assumption as the
radar's own projection lines. For a pair, relative position r and relative
velocity v (feet, feet/second, in a local flat frame -- exact enough over the
few NM that matter), the closest approach is at t* = -r.v / |v|^2, clamped to
[0, lookahead]. Separation there is |r + v t*|. Vertical separation counts:
500 ft straight-line means 500 ft above you is as close as 500 ft beside you.

WHAT "IFR" MEANS: an FAA-filed flight plan (adsb-hub `route` src 'swim');
at or above 18,000 ft (Class A); an emitter category of A2 or heavier
(A2-A6: small to heavy jets, high-performance); or an airline-style callsign
with no category. Not perfect -- a VFR flight-following bizjet counts as IFR
here -- but it is what the broadcast lets us know.

WHAT "LIGHT" MEANS, from what ADS-B actually says:
  * ADS-B emitter category A1 (light, < 15,500 lb), A7 (rotorcraft), B1
    (glider), B2 (lighter-than-air), B4 (ultralight), B6 (UAV). A2 and up
    -- small/large/heavy jets, high-performance -- are out.
  * NO category (TIS-B targets, which are radar-derived returns of aircraft
    with no ADS-B Out -- typically exactly the light GA this is for, and
    local-only receptions) counts as light unless the callsign is an
    airline-style one (three letters + a number). Missing data must not
    silently remove an aircraft from collision checking.
  * NOT IFR: an aircraft with an FAA-filed flight plan (adsb-hub `route` src
    'swim') is IFR and out, whatever its category -- e.g. Key Lime Air's A1
    Metroliners on cargo runs. Anything at or above 18,000 ft MSL is in
    Class A, IFR by definition, and out.
  * AIRBORNE: not reported on the ground, and not PARKED/TAXI by the phase
    classifier. Without an altitude there is no 3-D separation to compute,
    so an aircraft with no altitude is out.

LIMITS, honestly: positions are ADS-B's (typically +/- 100-300 ft) plus feed
latency, and a straight-line projection does not know about turns, so
pattern traffic and formation flights WILL trigger yellow. That is the
owner's chosen threshold; it is a warning, not a TCAS.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

FT_PER_NM = 6076.12
FT_PER_S_PER_KT = FT_PER_NM / 3600.0

LIGHT_CATEGORIES = frozenset({'A1', 'A7', 'B1', 'B2', 'B4', 'B6'})
CLASS_A_FT = 18000.0
NOT_AIRBORNE_PHASES = frozenset({'PARKED', 'TAXI'})
# ICAO airline-style callsign: three letters then a digit (UAL2077, LYM292).
_AIRLINE_CALLSIGN = re.compile(r'^[A-Z]{3}\d')

IFR_CATEGORIES = frozenset({'A2', 'A3', 'A4', 'A5', 'A6'})

DEFAULT_SEPARATION_FT = 500.0          # light/VFR: 3-D sphere
IFR_HORIZONTAL_NM = 5.0                # IFR: cylinder radius en route ...
IFR_TERMINAL_NM = 3.0                  # ... and in terminal airspace ...
IFR_VERTICAL_FT = 1000.0               # ... and half-height
IFR_VERTICAL_TOLERANCE_FT = 100.0      # altimetry noise: inside means < 900 ft
MIXED_RULE = 'vfr'                     # light vs IFR: 'vfr' (500 ft) or 'ifr'
# IFR pairs are not checked when either aircraft is in one of these phases.
AIRPORT_PHASES = frozenset({'APPROACH', 'LANDING', 'TAKEOFF', 'DEPART'})
TERMINAL_RADIUS_NM = 40.0
FLOW_RADIUS_NM = 25.0                  # arrival/departure flow: this close ...
FLOW_AGL_FT = 6000.0                   # ... and this low above the airport
TERMINAL_AIRPORT_TYPES = frozenset({'large_airport', 'medium_airport'})
DEFAULT_LOOKAHEAD_S = 60.0


@dataclass(frozen=True)
class Craft:
    """What the detector needs about one aircraft. Built by the engine from a
    Track plus the Aircraft's category/ground/route; tests build it directly."""
    icao: str
    lat: float | None
    lon: float | None
    alt_ft: float | None
    course_deg: float | None = None
    speed_kt: float | None = None
    vrate_fpm: float | None = None
    category: str | None = None
    callsign: str | None = None
    on_ground: bool = False
    phase: str | None = None
    route: dict | None = None
    terminal: bool = False   # in terminal (approach-control) airspace
    airport_flow: bool = False   # in a large/medium airport's arrival/departure flow


def in_airport_flow(lat, lon, alt_ft, airports) -> bool:
    """Within FLOW_RADIUS_NM of a large or medium airport and below
    FLOW_AGL_FT above its field elevation: arriving or departing traffic,
    which the tower and approach separate by their own (reduced) rules."""
    if lat is None or lon is None or alt_ft is None:
        return False
    for ap in airports or ():
        if getattr(ap, 'type', None) not in TERMINAL_AIRPORT_TYPES:
            continue
        elev = getattr(ap, 'elev_ft', None) or 0.0
        if alt_ft - elev > FLOW_AGL_FT:
            continue
        dlat = (ap.lat - lat) * 60.0
        dlon = (ap.lon - lon) * 60.0 * math.cos(math.radians(lat))
        if dlat * dlat + dlon * dlon <= FLOW_RADIUS_NM ** 2:
            return True
    return False


def in_terminal_airspace(lat, lon, alt_ft, airports) -> bool:
    """Below 18,000 ft and within TERMINAL_RADIUS_NM of a large or medium
    airport. `airports` is any iterable of objects with .type/.lat/.lon."""
    if lat is None or lon is None or alt_ft is None or alt_ft >= CLASS_A_FT:
        return False
    for ap in airports or ():
        if getattr(ap, 'type', None) not in TERMINAL_AIRPORT_TYPES:
            continue
        dlat = (ap.lat - lat) * 60.0
        dlon = (ap.lon - lon) * 60.0 * math.cos(math.radians(lat))
        if dlat * dlat + dlon * dlon <= TERMINAL_RADIUS_NM ** 2:
            return True
    return False


@dataclass(frozen=True)
class Conflict:
    """One aircraft's worst conflict. `level` is 'alert' (within the
    separation now) or 'warn' (projected to come within it)."""
    level: str
    partners: tuple          # icao of every aircraft it conflicts with
    sep_ft: float            # 3-D separation now (alert) or at the worst point
    t_s: float               # seconds until it (0 for alert)
    rule: str = 'vfr'        # 'vfr' (500 ft sphere) or 'ifr' (5 NM x 1000 ft)
    h_nm: float = 0.0        # horizontal separation at that point
    v_ft: float = 0.0        # vertical separation at that point


def _airborne(c: Craft) -> bool:
    if c.lat is None or c.lon is None or c.alt_ft is None:
        return False
    return not (c.on_ground or (c.phase or '').upper() in NOT_AIRBORNE_PHASES)


def classify(c: Craft) -> str | None:
    """'ifr', 'vfr' (light, not IFR) or None (not checked). See module doc."""
    if not _airborne(c):
        return None
    cat = (c.category or '').upper() or None
    cs = (c.callsign or '').strip().upper()
    if ((isinstance(c.route, dict) and c.route.get('src') == 'swim')
            or c.alt_ft >= CLASS_A_FT
            or cat in IFR_CATEGORIES
            or (cat is None and _AIRLINE_CALLSIGN.match(cs))):
        return 'ifr'
    if cat is None or cat in LIGHT_CATEGORIES:
        return 'vfr'
    return None          # B3 parachutist, C* surface vehicles/obstacles, ...


def is_light(c: Craft) -> bool:
    return classify(c) == 'vfr'


def _velocity_fps(c: Craft) -> tuple[float, float, float]:
    """(east, north, up) in ft/s. Unknown track/speed/vrate is taken as 0 --
    the aircraft is treated as holding still in that axis, which still lets
    a moving neighbour be projected into it."""
    gs = (c.speed_kt or 0.0) * FT_PER_S_PER_KT
    if c.course_deg is None:
        ve = vn = 0.0
    else:
        a = math.radians(c.course_deg)
        ve, vn = gs * math.sin(a), gs * math.cos(a)
    vu = (c.vrate_fpm or 0.0) / 60.0
    return ve, vn, vu


def pair_geometry(a: Craft, b: Craft, lookahead_s: float):
    """(separation now ft, closest separation within lookahead ft, t of it s)
    for two aircraft with positions and altitudes."""
    mid = math.radians((a.lat + b.lat) / 2.0)
    # b relative to a, in feet: north from latitude, east scaled by cos(lat).
    rn = (b.lat - a.lat) * 60.0 * FT_PER_NM
    re_ = (b.lon - a.lon) * 60.0 * FT_PER_NM * math.cos(mid)
    ru = b.alt_ft - a.alt_ft
    ae, an, au = _velocity_fps(a)
    be, bn, bu = _velocity_fps(b)
    ve, vn, vu = be - ae, bn - an, bu - au
    now = math.sqrt(re_ * re_ + rn * rn + ru * ru)
    v2 = ve * ve + vn * vn + vu * vu
    if v2 <= 1e-9:
        return now, now, 0.0
    t = -(re_ * ve + rn * vn + ru * vu) / v2
    t = min(max(t, 0.0), lookahead_s)
    dx, dy, dz = re_ + ve * t, rn + vn * t, ru + vu * t
    return now, math.sqrt(dx * dx + dy * dy + dz * dz), t


def _rel(a: Craft, b: Craft):
    """b relative to a: position (ft) and velocity (ft/s), east/north/up."""
    mid = math.radians((a.lat + b.lat) / 2.0)
    rn = (b.lat - a.lat) * 60.0 * FT_PER_NM
    re_ = (b.lon - a.lon) * 60.0 * FT_PER_NM * math.cos(mid)
    ru = b.alt_ft - a.alt_ft
    ae, an, au = _velocity_fps(a)
    be, bn, bu = _velocity_fps(b)
    return (re_, rn, ru), (be - ae, bn - an, bu - au)


def ifr_geometry(a: Craft, b: Craft, lookahead_s: float, h_ft: float, v_ft: float):
    """IFR cylinder test. Returns (inside_now, first t in [0, lookahead] at
    which both horizontal <= h_ft and vertical <= v_ft, or None), plus the
    horizontal/vertical separation at that t (or now)."""
    (x, y, z), (vx, vy, vz) = _rel(a, b)

    def at(t):
        return math.hypot(x + vx * t, y + vy * t), abs(z + vz * t)

    h0, v0 = at(0.0)
    if h0 <= h_ft and v0 <= v_ft:
        return True, 0.0, h0, v0
    # Horizontal: |p + v t|^2 <= h^2, a quadratic in t.
    A = vx * vx + vy * vy
    B = 2 * (x * vx + y * vy)
    C = x * x + y * y - h_ft * h_ft
    if A <= 1e-12:
        hlo, hhi = (0.0, lookahead_s) if C <= 0 else (None, None)
    else:
        disc = B * B - 4 * A * C
        if disc < 0:
            return False, None, h0, v0
        sq = math.sqrt(disc)
        hlo, hhi = (-B - sq) / (2 * A), (-B + sq) / (2 * A)
    if hlo is None:
        return False, None, h0, v0
    # Vertical: |z + vz t| <= v, linear.
    if abs(vz) <= 1e-12:
        vlo, vhi = (0.0, lookahead_s) if abs(z) <= v_ft else (None, None)
    else:
        t1, t2 = (-v_ft - z) / vz, (v_ft - z) / vz
        vlo, vhi = min(t1, t2), max(t1, t2)
    if vlo is None:
        return False, None, h0, v0
    lo = max(hlo, vlo, 0.0)
    hi = min(hhi, vhi, lookahead_s)
    if lo > hi:
        return False, None, h0, v0
    h, v = at(lo)
    return False, lo, h, v


def assess(crafts, *, separation_ft: float = DEFAULT_SEPARATION_FT,
           lookahead_s: float = DEFAULT_LOOKAHEAD_S,
           ifr_horizontal_nm: float = IFR_HORIZONTAL_NM,
           ifr_terminal_nm: float = IFR_TERMINAL_NM,
           ifr_vertical_ft: float = IFR_VERTICAL_FT) -> dict:
    """{icao: Conflict} for every checked aircraft in a conflict; others absent."""
    checked = [(c, k) for c in crafts if (k := classify(c)) is not None]
    out: dict[str, dict] = {}
    rank = {'alert': 2, 'warn': 1}

    def note(icao, other, level, sep, t, rule, h, v):
        cur = out.get(icao)
        if cur is None:
            out[icao] = dict(level=level, partners=[other], sep=sep, t=t,
                             rule=rule, h=h, v=v)
            return
        cur['partners'].append(other)
        if (rank[level], -sep) > (rank[cur['level']], -cur['sep']):
            cur.update(level=level, sep=sep, t=t, rule=rule, h=h, v=v)

    for i in range(len(checked)):
        a, ka = checked[i]
        for j in range(i + 1, len(checked)):
            b, kb = checked[j]
            rule = 'ifr' if (ka == kb == 'ifr' or
                             (ka != kb and MIXED_RULE == 'ifr')) else 'vfr'
            if rule == 'ifr':
                if ((a.phase or '').upper() in AIRPORT_PHASES
                        or (b.phase or '').upper() in AIRPORT_PHASES
                        or a.airport_flow or b.airport_flow):
                    continue
                h_ft = (ifr_terminal_nm if (a.terminal and b.terminal)
                        else ifr_horizontal_nm) * FT_PER_NM
            else:
                h_ft = 0.0
            # Cheap reject: they can close at most (sum of speeds) x lookahead.
            sa, sb = (a.speed_kt or 0.0), (b.speed_kt or 0.0)
            horiz_reach = ((sa + sb) * FT_PER_S_PER_KT * lookahead_s
                           + (h_ft if rule == 'ifr' else separation_ft))
            vert_reach = ((abs(a.vrate_fpm or 0.0) + abs(b.vrate_fpm or 0.0)) / 60
                          * lookahead_s
                          + (ifr_vertical_ft if rule == 'ifr' else separation_ft))
            if (abs(a.lat - b.lat) * 60.0 * FT_PER_NM > horiz_reach
                    or abs(a.alt_ft - b.alt_ft) > vert_reach):
                continue
            if rule == 'ifr':
                inside, t, h, v = ifr_geometry(
                    a, b, lookahead_s, h_ft,
                    ifr_vertical_ft - IFR_VERTICAL_TOLERANCE_FT)
                if inside or t is not None:
                    level = 'alert' if inside else 'warn'
                    sep = math.hypot(h, v)
                    for x, y in ((a, b), (b, a)):
                        note(x.icao, y.icao, level, sep, t or 0.0, 'ifr',
                             h / FT_PER_NM, v)
                continue
            now, closest, t = pair_geometry(a, b, lookahead_s)
            if now <= separation_ft or closest <= separation_ft:
                level = 'alert' if now <= separation_ft else 'warn'
                sep, tt = (now, 0.0) if level == 'alert' else (closest, t)
                (x, y, z), (vx, vy, vz) = _rel(a, b)
                h = math.hypot(x + vx * tt, y + vy * tt)
                v = abs(z + vz * tt)
                for p, q in ((a, b), (b, a)):
                    note(p.icao, q.icao, level, sep, tt, 'vfr', h / FT_PER_NM, v)

    return {k: Conflict(level=v['level'], partners=tuple(sorted(v['partners'])),
                        sep_ft=v['sep'], t_s=v['t'], rule=v['rule'],
                        h_nm=v['h'], v_ft=v['v'])
            for k, v in out.items()}
