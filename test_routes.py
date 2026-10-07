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


class Filed(unittest.TestCase):

    def test_filed_request_is_answered_from_the_hub_and_cached(self):
        import asyncio
        s = ui_web.RadarServer(Engine())
        calls = []

        def fake(ref):
            calls.append(ref)
            return {"ref": ref, "wp": [[39.8, -104.6], [33.9, -118.4]], "route": "KDEN..KLAX"}
        s._fetch_filed = fake
        sent = []

        class WS:
            async def send(self, m):
                sent.append(json.loads(m))
        ws = WS()
        asyncio.run(s._handle_filed(ws, json.dumps({"cmd": "filed", "ref": "159141703"})))
        self.assertEqual(sent[0]["type"], "filed")
        self.assertEqual(sent[0]["wp"][1], [33.9, -118.4])
        # junk refs are swallowed, never fetched
        self.assertTrue(asyncio.run(s._handle_filed(ws, json.dumps({"cmd": "filed", "ref": "../x"}))))
        self.assertEqual(calls, ["159141703"])
        # not a filed message: left for the other handlers
        self.assertFalse(asyncio.run(s._handle_filed(ws, json.dumps({"cmd": "set_coverage"}))))

    def test_route_ref_reaches_the_browser(self):
        _, kw = F.canonical_to_kwargs(hub_aircraft())
        self.assertEqual(kw["route"]["ref"], "123")


class WxPush(unittest.TestCase):

    def test_changed_products_are_reported_once_and_uncached(self):
        import io
        from unittest import mock
        w = ui_web.WxProxy("http://hub")
        idx = {"products": {"radar": {"t": 1.0}, "tfr": {"t": 5.0}}}

        def fake(url, timeout=10):
            return io.BytesIO(json.dumps(idx).encode())
        with mock.patch("urllib.request.urlopen", fake):
            self.assertEqual(w.changed(), [], "first look: nothing to report")
            w.cache["/wx/radar.png"] = (1e12, (200, "image/png", b"old"))
            w.cache["/wx/tfr"] = (1e12, (200, "application/json", b"{}"))
            idx["products"]["radar"]["t"] = 2.0
            self.assertEqual(w.changed(), ["radar"])
            self.assertNotIn("/wx/radar.png", w.cache, "the new frame is fetched, not the cached one")
            self.assertIn("/wx/tfr", w.cache)
            self.assertEqual(w.changed(), [])


if __name__ == "__main__":
    unittest.main()
