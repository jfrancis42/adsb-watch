#!/usr/bin/env python3
"""adsb-hub routes (where an aircraft is going) through feed -> engine -> web.

python3 -m unittest test_routes -v
"""

import json
import unittest

import feed_internet as F
import ui_web
from engine import Engine

HUB_AC = {"hex": "a1b2c3", "flight": "UAL1  ", "lat": 39.6, "lon": -104.8,
          "alt_baro": 9000, "gs": 250, "track": 230,
          "route": {"orig": "KDEN", "dest": "KLAX", "codes": "KDEN-KLAX",
                    "plausible": True, "src": "swim", "ref": "123", "eta": "2026-10-07T01:00:00Z"}}
AIRPORTS = {"KDEN": {"icao": "KDEN", "iata": "DEN", "name": "Denver Intl", "lat": 39.86, "lon": -104.67},
            "KLAX": {"icao": "KLAX", "iata": "LAX", "name": "Los Angeles Intl", "lat": 33.94, "lon": -118.41}}


def hub_aircraft():
    ac = [json.loads(json.dumps(HUB_AC))]
    F._attach_airports(ac, AIRPORTS)
    return ac[0]


class Routes(unittest.TestCase):

    def test_hub_route_becomes_a_compact_kwarg_with_its_airports(self):
        _, kw = F.canonical_to_kwargs(hub_aircraft())
        r = kw["route"]
        self.assertEqual((r["orig"], r["dest"], r["src"], r["plausible"]), ("KDEN", "KLAX", "swim", True))
        self.assertEqual(r["d"]["iata"], "LAX")
        self.assertNotIn("ref", r)

    def test_no_route_no_kwarg(self):
        _, kw = F.canonical_to_kwargs({"hex": "a1b2c3", "lat": 1.0, "lon": 2.0})
        self.assertNotIn("route", kw)

    def test_route_is_sent_once_then_only_when_it_changes(self):
        e = Engine()
        e.update_observer(39.5, -104.7, 6000.0)
        icao, kw = F.canonical_to_kwargs(hub_aircraft())
        e.update_aircraft(icao, source="internet", **kw)
        s = ui_web.RadarServer(e)
        full = json.loads(s._build_message(full=True))
        self.assertEqual(full["tracks"][0]["route"]["dest"], "KLAX")
        delta = json.loads(s._build_message(full=False))
        self.assertNotIn("route", delta["tracks"][0], "unchanged: not resent at 4 Hz")
        kw["route"] = dict(kw["route"], dest="KSFO")
        e.update_aircraft(icao, source="internet", **kw)
        delta = json.loads(s._build_message(full=False))
        self.assertEqual(delta["tracks"][0]["route"]["dest"], "KSFO")


if __name__ == "__main__":
    unittest.main()
