"""Traffic-conflict detection (conflict.py) and its wiring through the engine.

Fake aircraft near Denver. 1 NM of latitude = 1/60 degree; at 39.5 N one
degree of longitude is ~46.3 NM.
"""
import math
import time

import conflict as C
from engine import Engine

LAT, LON = 39.50, -104.70
NM = 1 / 60.0                        # degrees of latitude per NM


def craft(icao, dlat_nm=0.0, alt=6500, course=0, kt=100, vr=0, cat='A1', **kw):
    return C.Craft(icao=icao, lat=LAT + dlat_nm * NM, lon=LON, alt_ft=alt,
                   course_deg=course, speed_kt=kt, vrate_fpm=vr,
                   category=cat, **kw)


# ---- classification ----------------------------------------------------------

def test_classify_light_ifr_and_unchecked():
    assert C.classify(craft('a', cat='A1', callsign='N12345')) == 'vfr'
    assert C.classify(craft('a', cat='A7')) == 'vfr'            # rotorcraft
    assert C.classify(craft('a', cat='B4')) == 'vfr'            # ultralight
    assert C.classify(craft('a', cat='A3')) == 'ifr'            # airliner
    assert C.classify(craft('a', cat='A1', route={'src': 'swim'})) == 'ifr'
    assert C.classify(craft('a', cat='A1', alt=18500)) == 'ifr'  # Class A
    assert C.classify(craft('a', cat='A1', on_ground=True)) is None
    assert C.classify(craft('a', cat='A1', phase='TAXI')) is None
    assert C.classify(craft('a', alt=None)) is None
    assert C.classify(craft('a', cat='C1')) is None             # surface vehicle
    # No category: light unless the callsign is an airline's (TIS-B GA).
    assert C.classify(craft('a', cat=None, callsign='N123AB')) == 'vfr'
    assert C.classify(craft('a', cat=None, callsign='UAL123')) == 'ifr'
    # A schedule GUESS is not a filed plan.
    assert C.classify(craft('a', cat='A1', route={'src': 'adsb.im'})) == 'vfr'


# ---- light aircraft: 500 ft sphere ---------------------------------------------

def test_head_on_light_aircraft_turn_yellow():
    # 1.5 NM apart, head-on at 100 kt each: closing 200 kt, meet in 27 s.
    r = C.assess([craft('a', 0, course=0), craft('b', 1.5, course=180)])
    assert set(r) == {'a', 'b'}
    assert r['a'].level == r['b'].level == 'warn'
    assert r['a'].rule == 'vfr' and r['a'].partners == ('b',)
    assert 25 < r['a'].t_s < 29 and r['a'].sep_ft < 50


def test_within_500ft_now_is_red():
    # 400 ft apart horizontally, same altitude.
    r = C.assess([craft('a', 0), craft('b', 400 / C.FT_PER_NM, course=90)])
    assert r['a'].level == r['b'].level == 'alert'
    assert 390 < r['a'].sep_ft < 410 and r['a'].t_s == 0


def test_500ft_is_straight_line_so_altitude_counts():
    # Directly overhead: 450 ft above is red, 600 ft above is nothing.
    assert C.assess([craft('a'), craft('b', alt=6950)])['a'].level == 'alert'
    assert C.assess([craft('a', kt=0), craft('b', alt=7100, kt=0)]) == {}
    # Head-on but 600 ft apart vertically: they pass 600 ft apart -- clear.
    assert C.assess([craft('a', 0), craft('b', 1.5, alt=7100, course=180)]) == {}


def test_parallel_and_diverging_traffic_is_clear():
    side = 0.5 / 46.3   # 0.5 NM east
    a = craft('a')
    b = C.Craft(icao='b', lat=LAT, lon=LON + side, alt_ft=6500,
                course_deg=0, speed_kt=100, vrate_fpm=0, category='A1')
    assert C.assess([a, b]) == {}
    # Tail to tail, 1000 ft apart and opening: never closer.
    assert C.assess([craft('a', 0, course=180),
                     craft('b', 1000 / C.FT_PER_NM, course=0)]) == {}


def test_beyond_lookahead_is_clear():
    # Head-on 10 NM apart at 100 kt each: meet in 180 s, past the 60 s window.
    assert C.assess([craft('a', 0), craft('b', 10, course=180)]) == {}
    r = C.assess([craft('a', 0), craft('b', 10, course=180)], lookahead_s=200)
    assert r['a'].level == 'warn'


def test_climb_into_traffic_counts():
    # b is 1000 ft above and climbing away... no: a climbs INTO b, same spot.
    r = C.assess([craft('a', alt=6000, kt=0, vr=1000), craft('b', alt=6700, kt=0)])
    assert r['a'].level == 'warn' and 15 < r['a'].t_s < 45


def test_ground_traffic_is_ignored():
    assert C.assess([craft('a', on_ground=True), craft('b', 0.01)]) == {}


def test_mixed_light_and_ifr_uses_500ft():
    # An airliner and a Cessna 2 NM apart: not an IFR conflict (5 NM) ...
    assert C.assess([craft('a', 0, kt=0, cat='A1'),
                     craft('b', 2, kt=0, cat='A3')]) == {}
    # ... but on a collision course they go yellow under the 500 ft rule.
    r = C.assess([craft('a', 0, cat='A1'), craft('b', 1.5, course=180, cat='A3')])
    assert r['a'].rule == 'vfr' and r['a'].level == 'warn'


# ---- IFR: 5 NM / 3 NM x 1000 ft cylinder ----------------------------------------

def ifr(icao, dlat_nm, alt=35000, course=0, kt=450, **kw):
    return craft(icao, dlat_nm, alt=alt, course=course, kt=kt, cat='A3', **kw)


def test_ifr_inside_5nm_and_1000ft_is_red():
    r = C.assess([ifr('a', 0), ifr('b', 4, alt=35500)])
    assert r['a'].level == 'alert' and r['a'].rule == 'ifr'
    assert 3.9 < r['a'].h_nm < 4.1 and 490 < r['a'].v_ft < 510


def test_ifr_1000ft_apart_is_separated_despite_altimetry_jitter():
    # FL350/FL360 reported as 974 ft apart: legal, not a conflict ...
    assert C.assess([ifr('a', 0), ifr('b', 3, alt=35974)]) == {}
    # ... 850 ft apart is.
    assert C.assess([ifr('a', 0), ifr('b', 3, alt=35850)])['a'].level == 'alert'


def test_ifr_arrival_flow_is_exempt():
    class Ap:
        type, lat, lon, elev_ft = 'large_airport', LAT + 15 * NM, LON, 5434
    flow = [C.Craft(**{**c.__dict__, 'airport_flow':
                       C.in_airport_flow(c.lat, c.lon, c.alt_ft, [Ap])})
            for c in (ifr('a', 0, alt=8800, kt=200), ifr('b', 0.9, alt=8900, kt=200))]
    assert all(c.airport_flow for c in flow) and C.assess(flow) == {}
    # Same pair at 13,000 ft (7,600 above the field): checked.
    high = [ifr('a', 0, alt=13000, kt=200), ifr('b', 0.9, alt=13100, kt=200)]
    assert not C.in_airport_flow(high[0].lat, high[0].lon, 13000, [Ap])
    assert C.assess(high)['a'].level == 'alert'


def test_ifr_needs_both_horizontal_and_vertical():
    # 4 NM but 1500 ft apart (same speed, no closure): separated.
    assert C.assess([ifr('a', 0), ifr('b', 4, alt=36500)]) == {}
    # 600 ft apart vertically but 6 NM horizontally, parallel: separated.
    assert C.assess([ifr('a', 0), ifr('b', 6, alt=35600)]) == {}


def test_ifr_projected_loss_is_yellow():
    # Head-on 12 NM apart at 450 kt each: inside 5 NM after ~28 s.
    r = C.assess([ifr('a', 0), ifr('b', 12, course=180)])
    assert r['a'].level == 'warn' and 25 < r['a'].t_s < 31
    assert 4.9 < r['a'].h_nm < 5.1


def test_ifr_terminal_airspace_is_3nm():
    # 4 NM in trail at 9000 ft: a loss en route, fine in terminal airspace.
    pair = [ifr('a', 0, alt=9000, kt=180), ifr('b', 4, alt=9000, kt=180)]
    assert C.assess(pair)['a'].level == 'alert'
    term = [C.Craft(**{**c.__dict__, 'terminal': True}) for c in pair]
    assert C.assess(term) == {}
    closer = [C.Craft(**{**c.__dict__, 'terminal': True})
              for c in (ifr('a', 0, alt=9000, kt=180), ifr('b', 2.5, alt=9000, kt=180))]
    assert C.assess(closer)['a'].level == 'alert'


def test_ifr_airport_phases_are_exempt():
    a = ifr('a', 0, alt=6000, kt=150, phase='APPROACH')
    b = ifr('b', 1.5, alt=6200, kt=150, phase='APPROACH')
    assert C.assess([a, b]) == {}
    # ...but the 500 ft rule still holds between a light aircraft and one.
    cessna = craft('c', 0 + 300 / C.FT_PER_NM, alt=6000, kt=0)
    assert C.assess([a, cessna])['c'].level == 'alert'


def test_in_terminal_airspace():
    class Ap:
        def __init__(self, t, lat, lon):
            self.type, self.lat, self.lon = t, lat, lon
    den = Ap('large_airport', 39.86, -104.67)
    strip = Ap('small_airport', 39.5, -104.7)
    assert C.in_terminal_airspace(39.5, -104.7, 9000, [den])       # ~21 NM
    assert not C.in_terminal_airspace(39.5, -104.7, 19000, [den])  # Class A
    assert not C.in_terminal_airspace(38.5, -104.7, 9000, [den])   # ~82 NM
    assert not C.in_terminal_airspace(39.5, -104.7, 9000, [strip])


# ---- through the engine ------------------------------------------------------------

def _feed(eng, icao, dlat_nm, alt, course, kt=100, cat='A1', **kw):
    eng.update_aircraft(icao, lat=LAT + dlat_nm * NM, lon=LON, alt_ft=alt,
                        course_deg=course, speed_kt=kt, vrate_fpm=0,
                        category=cat, on_ground=False, source='internet', **kw)


def test_engine_marks_conflicts_and_holds_them():
    eng = Engine()
    _feed(eng, 'aaaaaa', 0, 6500, 0)
    _feed(eng, 'bbbbbb', 1.5, 6500, 180)
    _feed(eng, 'cccccc', 20, 6500, 0)                      # far away
    t = {x.icao: x for x in eng.snapshot().tracks}
    assert t['AAAAAA'].conflict == 'warn' and t['BBBBBB'].conflict == 'warn'
    assert t['AAAAAA'].conflict_with == ('BBBBBB',)
    assert t['AAAAAA'].conflict_rule == 'vfr'
    assert t['CCCCCC'].conflict is None
    assert t['AAAAAA'].category == 'A1'

    # b turns away: the geometry clears, but the colour holds a moment ...
    _feed(eng, 'bbbbbb', 1.5, 6500, 90)
    eng.update_aircraft('BBBBBB', lat=LAT + 1.5 * NM, lon=LON + 0.001,
                        source='internet', pos_time=time.time() + 0.01)
    assert {x.icao: x for x in eng.snapshot().tracks}['AAAAAA'].conflict == 'warn'
    # ... and drops once the hold has run out.
    eng.CONFLICT_HOLD_S = 0.0
    eng._conflict_hold.clear()
    assert {x.icao: x for x in eng.snapshot().tracks}['AAAAAA'].conflict is None


def test_engine_red_when_close_and_ground_flag_clears_it():
    eng = Engine()
    _feed(eng, 'aaaaaa', 0, 6500, 0)
    _feed(eng, 'bbbbbb', 300 / C.FT_PER_NM, 6500, 90)
    assert {x.icao: x for x in eng.snapshot().tracks}['AAAAAA'].conflict == 'alert'
    eng2 = Engine()
    _feed(eng2, 'aaaaaa', 0, 0, 0)
    _feed(eng2, 'bbbbbb', 300 / C.FT_PER_NM, 0, 90)
    for i in ('aaaaaa', 'bbbbbb'):
        eng2.update_aircraft(i, on_ground=True, alt_ft=0.0)
    assert all(x.conflict is None for x in eng2.snapshot().tracks)


def test_hub_record_carries_category_and_ground():
    from feed_internet import canonical_to_kwargs
    _, kw = canonical_to_kwargs({'hex': 'abc123', 'category': 'a1',
                                 'alt_baro': 'ground', 'lat': 1, 'lon': 2})
    assert kw['category'] == 'A1' and kw['on_ground'] is True
    _, kw = canonical_to_kwargs({'hex': 'abc123', 'alt_baro': 5000})
    assert kw['on_ground'] is False
