#!/usr/bin/env python3
"""Tests for scope re-centring (engine observer provenance + CenterControl).

Run: python3 test_center.py

These encode the rules, not just the code: GPS overrides manual selection,
a manual centre carries the field elevation that the AGL readout depends on,
and every refusal answers the caller instead of failing silently.
"""
import json
import threading
import time
import unittest

from airports import AirportFix, AirportLookupError, FacilitiesClient
from engine import Engine
import ui_web


HOME = (39.3553696, -104.6729929, 6750.0)

KPAE = AirportFix(ident='KPAE', name='Snohomish County (Paine Field)',
                  lat=47.90630, lon=-122.28160, elev_ft=606.0,
                  has_elevation=True)
S43 = AirportFix(ident='S43', name='Harvey Field', lat=47.90580,
                 lon=-122.10500, elev_ft=0.0, has_elevation=False)

AIRPORTS = {'KPAE': KPAE, 'S43': S43}


def resolver(code):
    try:
        return AIRPORTS[code]
    except KeyError:
        raise AirportLookupError(f'airport {code!r} not found in govt-data')


def control(engine, allow=True, on_change=None):
    return ui_web.CenterControl(engine, resolver=resolver, allow=allow,
                                on_change=on_change,
                                error_cls=AirportLookupError)


def fixed_engine():
    """An instance started with --fixed-lat/--fixed-lon: no GPS receiver."""
    e = Engine()
    e.set_home(*HOME)
    e.set_center(*HOME, label='HOME')
    return e


def gps_engine():
    """An instance started under gpsd: no HOME, GPS in control."""
    e = Engine()
    e.set_gps_available(True)
    e.update_observer(39.0, -105.0, 5000.0)   # a fix, as GpsFeeder would
    return e


class ManualCentring(unittest.TestCase):

    def test_airport_centres_and_carries_field_elevation(self):
        e = fixed_engine()
        res = control(e).set_airport('kpae')      # case-insensitive
        self.assertTrue(res['ok'], res['message'])
        obs = e.snapshot().observer
        self.assertAlmostEqual(obs.lat, KPAE.lat)
        self.assertAlmostEqual(obs.lon, KPAE.lon)
        # AGL is centre-relative: without the field elevation every AGL
        # readout silently becomes MSL.
        self.assertEqual(obs.alt_ft, 606.0)
        self.assertEqual(obs.label, 'KPAE')
        self.assertEqual(obs.source, 'manual')

    def test_missing_field_elevation_is_reported_not_hidden(self):
        e = fixed_engine()
        res = control(e).set_airport('S43')
        self.assertTrue(res['ok'])
        self.assertIn('AGL', res['message'])
        self.assertIn('MSL', res['message'])

    def test_home_returns_to_the_startup_centre_and_its_elevation(self):
        e = fixed_engine()
        c = control(e)
        c.set_airport('KPAE')
        res = c.set_airport('HOME')
        self.assertTrue(res['ok'], res['message'])
        obs = e.snapshot().observer
        self.assertAlmostEqual(obs.lat, HOME[0])
        self.assertAlmostEqual(obs.lon, HOME[1])
        self.assertEqual(obs.alt_ft, 6750.0)      # the house, not sea level
        self.assertEqual(obs.label, 'HOME')

    def test_unknown_code_is_an_answer_not_an_exception(self):
        e = fixed_engine()
        res = control(e).set_airport('ZZZZ')
        self.assertFalse(res['ok'])
        self.assertIn('ZZZZ', res['message'])
        # ...and the scope did not move.
        self.assertAlmostEqual(e.snapshot().observer.lat, HOME[0])

    def test_empty_code_is_rejected(self):
        self.assertFalse(control(fixed_engine()).set_airport('   ')['ok'])

    def test_recentring_nudges_the_facilities_client(self):
        # Without this the newly chosen airport has no airports or runways
        # drawn on it for up to a minute, which reads as a broken display.
        nudges = []
        c = control(fixed_engine(), on_change=lambda: nudges.append(1))
        c.set_airport('KPAE')
        c.set_airport('HOME')
        self.assertEqual(len(nudges), 2)

    def test_disabled_instance_refuses_everything(self):
        e = fixed_engine()
        c = control(e, allow=False)
        self.assertFalse(c.set_airport('KPAE')['ok'])
        self.assertFalse(c.set_gps(False)['ok'])
        self.assertAlmostEqual(e.snapshot().observer.lat, HOME[0])


class GpsOverride(unittest.TestCase):

    def test_gps_refuses_manual_selection_until_switched_off(self):
        e = gps_engine()
        c = control(e)
        res = c.set_airport('KPAE')
        self.assertFalse(res['ok'])
        self.assertIn('GPS', res['message'])
        self.assertAlmostEqual(e.snapshot().observer.lat, 39.0)

    def test_switching_gps_off_then_selecting_an_airport(self):
        e = gps_engine()
        c = control(e)
        self.assertTrue(c.set_gps(False)['ok'])
        self.assertEqual(e.snapshot().observer.source, 'manual')
        self.assertTrue(c.set_airport('KPAE')['ok'])
        self.assertAlmostEqual(e.snapshot().observer.lat, KPAE.lat)
        # A gpsd fix arriving now must NOT drag the scope back.
        e.update_observer(39.0, -105.0, 5000.0)
        self.assertAlmostEqual(e.snapshot().observer.lat, KPAE.lat)

    def test_switching_gps_back_on_hands_control_to_the_next_fix(self):
        e = gps_engine()
        c = control(e)
        c.set_gps(False)
        c.set_airport('KPAE')
        res = c.set_gps(True)
        self.assertTrue(res['ok'])
        # Turning GPS on does not itself move the scope — it stays put until
        # gpsd speaks, so a momentary loss of fix doesn't blank the display.
        self.assertAlmostEqual(e.snapshot().observer.lat, KPAE.lat)
        self.assertEqual(e.snapshot().observer.source, 'gps')
        e.update_observer(39.0, -105.0, 5000.0)
        self.assertAlmostEqual(e.snapshot().observer.lat, 39.0)

    def test_home_under_gpsd_means_hand_it_back_to_gps(self):
        e = gps_engine()
        c = control(e)
        c.set_gps(False)
        c.set_airport('KPAE')
        res = c.set_airport('HOME')
        self.assertTrue(res['ok'], res['message'])
        self.assertEqual(e.snapshot().observer.source, 'gps')

    def test_handback_is_marked_until_gpsd_actually_supplies_a_fix(self):
        # Between the handback and the next fix, the position on display is
        # still the manual centre — labelling it "GPS" would be a lie.
        e = gps_engine()
        c = control(e)
        c.set_gps(False)
        c.set_airport('KPAE')
        c.set_gps(True)
        self.assertTrue(e.snapshot().observer.awaiting_fix)
        e.update_observer(39.0, -105.0, 5000.0)          # gpsd speaks
        self.assertFalse(e.snapshot().observer.awaiting_fix)

    def test_gps_toggle_is_dead_without_a_receiver(self):
        res = control(fixed_engine()).set_gps(True)
        self.assertFalse(res['ok'])
        self.assertIn('No GPS', res['message'])

    def test_source_is_gps_before_the_first_fix(self):
        # gpsd is in control even with no fix yet, so a UI must not offer
        # manual entry the next fix would immediately overwrite.
        e = Engine()
        e.set_gps_available(True)
        self.assertEqual(e.snapshot().observer.source, 'gps')


class Payload(unittest.TestCase):

    def test_centre_block_rides_every_frame_even_with_no_fix(self):
        e = Engine()
        e.set_gps_available(True)              # gpsd running, no fix yet
        server = ui_web.RadarServer(e, center_control=control(e))
        msg = json.loads(server._build_message(full=True))
        self.assertIsNone(msg['observer'])     # no position yet
        self.assertEqual(msg['center'], {
            'source': 'gps', 'label': None, 'gps_available': True,
            'awaiting_fix': False, 'recenter_enabled': True})

    def test_delta_frames_carry_the_centre_too(self):
        # Another viewer can move the centre at any time; a client that only
        # learned it on connect would show a stale label forever.
        e = fixed_engine()
        server = ui_web.RadarServer(e, center_control=control(e))
        msg = json.loads(server._build_message(full=False))
        self.assertEqual(msg['center']['label'], 'HOME')
        self.assertEqual(msg['center']['source'], 'manual')

    def test_recenter_enabled_is_false_when_locked_down(self):
        e = fixed_engine()
        server = ui_web.RadarServer(e, center_control=control(e, allow=False))
        msg = json.loads(server._build_message(full=True))
        self.assertFalse(msg['center']['recenter_enabled'])

    def test_no_control_at_all_still_serializes(self):
        server = ui_web.RadarServer(fixed_engine())
        msg = json.loads(server._build_message(full=True))
        self.assertFalse(msg['center']['recenter_enabled'])


class FacilitiesWake(unittest.TestCase):
    """The nudge has to actually shorten the wait, and must not cost the
    prompt shutdown that waiting on `_stop` used to give for free."""

    def test_wake_short_circuits_the_poll_sleep_and_stop_still_works(self):
        client = FacilitiesClient('http://example.invalid', 'u', 'p',
                                  cache=None, poll_interval_s=60.0)
        ticks = []
        client._tick = lambda: ticks.append(time.time())
        t = threading.Thread(target=client.run, daemon=True)
        t.start()
        try:
            time.sleep(6.5)              # 5 s warm-up + the first two ticks
            before = len(ticks)
            client.wake()
            time.sleep(0.5)
            self.assertGreater(len(ticks), before,
                               'wake() did not short-circuit the poll sleep')
        finally:
            client.stop()
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive(), 'stop() no longer exits promptly')


class Dispatch(unittest.TestCase):

    def test_unknown_command_is_ignored(self):
        self.assertIsNone(control(fixed_engine()).handle({'cmd': 'zoom'}))

    def test_set_center_routes(self):
        e = fixed_engine()
        self.assertTrue(control(e).handle({'cmd': 'set_center',
                                           'airport': 'KPAE'})['ok'])

    def test_set_gps_routes(self):
        e = gps_engine()
        self.assertTrue(control(e).handle({'cmd': 'set_gps',
                                           'enabled': False})['ok'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
