"""Unit test: UatFeeder._handle maps real dump978 ToJson() output correctly.
JSON shapes below match uat_message.cc ToJson() exactly (field names, units,
enum string values verified from source)."""
import os, sys, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feed_uat import UatFeeder

class StubEngine:
    def __init__(self): self.calls = []
    def update_aircraft(self, icao, **kw): self.calls.append((icao, kw))
    def bump_count(self, name, n=1): pass
    def report_feeder(self, *a): pass

def run(name, line, expect_icao, expect_kw, expect_dropped=False):
    e = StubEngine()
    f = UatFeeder(e, 'x', 0)
    f._handle(line)
    if expect_dropped:
        assert not e.calls, f"{name}: expected drop, got {e.calls}"
        print(f"OK  {name}: dropped as expected"); return
    assert len(e.calls) == 1, f"{name}: expected 1 call, got {e.calls}"
    icao, kw = e.calls[0]
    assert icao == expect_icao, f"{name}: icao {icao} != {expect_icao}"
    assert kw == expect_kw, f"{name}:\n  got {kw}\n  exp {expect_kw}"
    print(f"OK  {name}: {icao} {kw}")

import json
# 1. Airborne adsb_icao with position, baro alt, velocity, track, vrate, callsign
run("airborne-full",
    json.dumps({"address_qualifier":"adsb_icao","address":"a1b2c3",
                "position":{"lat":39.12345,"lon":-104.87654},
                "pressure_altitude":8500,"geometric_altitude":8600,
                "ground_speed":121.0,"true_track":271.3,
                "vertical_velocity_barometric":-640,"callsign":"N123AB",
                "metadata":{"rssi":-8.5,"errors":0}}),
    "a1b2c3",
    {"callsign":"N123AB","lat":39.12345,"lon":-104.87654,"alt_ft":8500.0,
     "speed_kt":121.0,"course_deg":271.3,"vrate_fpm":-640.0})

# 2. Geometric-alt fallback (no pressure_altitude), geometric vrate fallback
run("geo-fallback",
    json.dumps({"address_qualifier":"adsb_icao","address":"abc001",
                "geometric_altitude":3200,"ground_speed":90,
                "vertical_velocity_geometric":320}),
    "abc001",
    {"alt_ft":3200.0,"speed_kt":90.0,"vrate_fpm":320.0})

# 3. On-ground: true_heading instead of true_track
run("onground-heading",
    json.dumps({"address_qualifier":"adsb_icao","address":"def002",
                "position":{"lat":40.0,"lon":-105.0},
                "airground_state":"ground","ground_speed":12,"true_heading":88.0}),
    "def002",
    {"lat":40.0,"lon":-105.0,"speed_kt":12.0,"course_deg":88.0})

# 4. TIS-B rebroadcast traffic — accepted
run("tisb-accepted",
    json.dumps({"address_qualifier":"tisb_icao","address":"aa1122",
                "position":{"lat":38.5,"lon":-104.0},"pressure_altitude":5000}),
    "aa1122", {"lat":38.5,"lon":-104.0,"alt_ft":5000.0})

# 5. vehicle/ground station — dropped (not aircraft traffic)
run("vehicle-dropped",
    json.dumps({"address_qualifier":"vehicle","address":"ff9900",
                "position":{"lat":39.0,"lon":-104.0}}),
    None, None, expect_dropped=True)

# 6. callsign-only frame (no position) — still updates callsign
run("callsign-only",
    json.dumps({"address_qualifier":"adsb_icao","address":"c0ffee","callsign":"SWA42"}),
    "c0ffee", {"callsign":"SWA42"})

# 7. garbage / non-JSON — dropped, no crash
run("garbage", "this is not json {", None, None, expect_dropped=True)

# 8. empty address — dropped
run("empty-addr",
    json.dumps({"address_qualifier":"adsb_icao","address":""}),
    None, None, expect_dropped=True)

print("\nAll UAT parser tests passed.")
