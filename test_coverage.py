#!/usr/bin/env python3
"""Tests for the data radius following what web viewers can see.

Run: python3 test_coverage.py

Over a map the display reaches the window corners, so the internet query and
the airport fetch must reach that far too -- but never below the configured
floor (an instance with no viewers behaves as it always did) and never above
the cap (the aggregators reject larger point queries).
"""
import json
import unittest

from airports import FacilitiesClient
from engine import Engine
from feed_internet import InternetFeeder
import ui_web


class CoverageTest(unittest.TestCase):

    def test_floor_with_no_viewers(self):
        self.assertEqual(ui_web.Coverage(floor_nm=50, cap_nm=250).radius_nm(), 50)

    def test_widest_viewer_wins_and_disconnect_releases_it(self):
        c = ui_web.Coverage(floor_nm=50, cap_nm=250)
        c.set('a', 120)
        c.set('b', 80)
        self.assertEqual(c.radius_nm(), 120)
        c.remove('a')
        self.assertEqual(c.radius_nm(), 80)
        c.remove('b')
        self.assertEqual(c.radius_nm(), 50)

    def test_cap(self):
        c = ui_web.Coverage(floor_nm=50, cap_nm=250)
        c.set('a', 900)
        self.assertEqual(c.radius_nm(), 250)

    def test_garbage_from_a_client_is_ignored(self):
        c = ui_web.Coverage(floor_nm=50, cap_nm=250)
        for bad in (None, 'far', float('nan'), -10, 0):
            c.set('a', bad)
        self.assertEqual(c.radius_nm(), 50)

    def test_on_grow_fires_only_when_the_radius_increases(self):
        calls = []
        c = ui_web.Coverage(floor_nm=50, cap_nm=250,
                            on_grow=lambda: calls.append(1))
        c.set('a', 40)      # below floor: no change
        c.set('a', 120)     # grows
        c.set('a', 100)     # shrinks
        self.assertEqual(len(calls), 1)


class _FakeWS:
    remote_address = ('test', 0)


class ServerRoutingTest(unittest.TestCase):

    def test_set_coverage_is_consumed_and_recorded(self):
        c = ui_web.Coverage(floor_nm=50, cap_nm=250)
        srv = ui_web.RadarServer(Engine(), coverage=c)
        ws = _FakeWS()
        msg = json.dumps({'cmd': 'set_coverage', 'radius_nm': 140})
        self.assertTrue(srv._handle_coverage(ws, msg))
        self.assertEqual(c.radius_nm(), 140)

    def test_other_commands_pass_through(self):
        srv = ui_web.RadarServer(Engine(), coverage=ui_web.Coverage())
        msg = json.dumps({'cmd': 'set_center', 'airport': 'KAPA'})
        self.assertFalse(srv._handle_coverage(_FakeWS(), msg))


class FeederRadiusTest(unittest.TestCase):

    def test_callable_radius_is_read_each_poll_and_rounded_up(self):
        r = [50.0]
        f = InternetFeeder(Engine(), 'adsb_lol', lambda: None,
                           radius_nm=lambda: r[0])
        self.assertEqual(f._radius(), 50.0)
        r[0] = 113.2
        self.assertEqual(f._radius(), 114.0)

    def test_fixed_radius_still_works(self):
        f = InternetFeeder(Engine(), 'adsb_lol', lambda: None, radius_nm=50.0)
        self.assertEqual(f._radius(), 50.0)


class FacilitiesRadiusTest(unittest.TestCase):

    def _client(self):
        return FacilitiesClient('http://example.invalid', 'u', 'p', cache=None)

    def test_no_provider_keeps_the_old_radius(self):
        self.assertEqual(self._client().wanted_radius(), 50.0)

    def test_provider_widens_in_cache_friendly_steps(self):
        c = self._client()
        c.attach_radius(lambda: 113.0)
        self.assertEqual(c.wanted_radius(), 125.0)

    def test_provider_never_shrinks_below_the_base(self):
        c = self._client()
        c.attach_radius(lambda: 5.0)
        self.assertEqual(c.wanted_radius(), 50.0)


if __name__ == '__main__':
    unittest.main()
