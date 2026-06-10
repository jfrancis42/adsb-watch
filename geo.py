"""Pure geometry helpers. No I/O, no globals — easy to unit-test."""
import math

R_EARTH_NM = 3440.065   # nautical miles
FT_PER_NM  = 6076.12

def haversine_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R_EARTH_NM * math.asin(math.sqrt(a))

def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial true bearing from (1) to (2), 0..360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1)*math.sin(p2) - math.sin(p1)*math.cos(p2)*math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0

def elevation_deg(distance_nm, observer_alt_ft, target_alt_ft):
    """Elevation angle from observer to target, ignoring earth curvature
    (fine for the < ~50 NM ranges ADS-B gives us)."""
    dh_ft = (target_alt_ft or 0) - (observer_alt_ft or 0)
    horiz_ft = distance_nm * FT_PER_NM
    if horiz_ft <= 0:
        return 90.0 if dh_ft > 0 else (-90.0 if dh_ft < 0 else 0.0)
    return math.degrees(math.atan2(dh_ft, horiz_ft))

def project_position(lat, lon, course_deg, distance_nm):
    """Walk `distance_nm` along `course_deg` from (lat, lon)."""
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    brg = math.radians(course_deg)
    d_r = distance_nm / R_EARTH_NM
    p2 = math.asin(math.sin(p1)*math.cos(d_r) + math.cos(p1)*math.sin(d_r)*math.cos(brg))
    l2 = l1 + math.atan2(math.sin(brg)*math.sin(d_r)*math.cos(p1),
                         math.cos(d_r) - math.sin(p1)*math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0

def closest_approach(my_lat, my_lon, ac_lat, ac_lon, course_deg, speed_kt,
                     horizon_s=3600.0):
    """Predicted closest point of approach assuming the aircraft holds course
    and speed, observer stationary, flat-earth around the observer (fine at
    ADS-B ranges).

    Returns (cpa_distance_nm, time_to_cpa_s, bearing_to_cpa_deg) or
    None if the aircraft is not approaching — i.e. no velocity data, or
    its CPA is in the past (moving away)."""
    if speed_kt is None or speed_kt <= 0 or course_deg is None:
        return None

    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * math.cos(math.radians(my_lat))

    x0 = (ac_lon - my_lon) * nm_per_deg_lon
    y0 = (ac_lat - my_lat) * nm_per_deg_lat
    brg = math.radians(course_deg)
    vx = speed_kt * math.sin(brg) / 3600.0   # NM per second, east
    vy = speed_kt * math.cos(brg) / 3600.0   # NM per second, north

    v2 = vx*vx + vy*vy
    if v2 <= 0:
        return None
    t = -(x0*vx + y0*vy) / v2
    if t <= 0:
        return None  # already past CPA — diverging
    t = min(t, horizon_s)
    cx = x0 + vx*t
    cy = y0 + vy*t
    dist = math.hypot(cx, cy)
    az = (math.degrees(math.atan2(cx, cy)) + 360.0) % 360.0
    return dist, t, az
