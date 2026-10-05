"""Exit failover for the internet feeders: python3 -m pytest test_exits.py -q"""
import io
import json
import socket
import threading
import urllib.error

import pytest

from engine import Engine
from exits import ExitRotator, is_network, is_refusal, parse_exits
from feed_internet import InternetFeeder


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "msg", {}, io.BytesIO(b""))


def test_parse():
    assert parse_exits(None) == [("direct", None)]
    assert parse_exits(["direct", "us-hub=socks5h://10.22.0.9:1080"]) == \
        [("direct", None), ("us-hub", "socks5h://10.22.0.9:1080")]
    with pytest.raises(ValueError):
        parse_exits(["nourl"])


def test_classification():
    for code in (401, 403, 429):
        assert is_refusal(http_error(code)) and not is_network(http_error(code))
    for exc in (http_error(503), urllib.error.URLError("refused"), socket.timeout(),
                ConnectionResetError(), ValueError("bad json")):
        assert is_network(exc), exc


def test_rotation_and_failback():
    r = ExitRotator([("direct", None), ("us", "socks5h://h:1"), ("eu", "socks5h://h:2")],
                    failback_s=900)
    assert r.network_failure(0) == "exit direct failed -> us" and r.name == "us"
    assert r.maybe_failback(100) is None
    assert "failing back us -> direct" in r.maybe_failback(1000)
    assert ExitRotator([("direct", None)]).network_failure() is None


class Script:
    """Stands in for the network: each call pops the next outcome."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.exits_used = []


def run_feeder(monkeypatch, outcomes, exits):
    eng = Engine()
    script = Script(outcomes)
    f = InternetFeeder(eng, "adsb_lol", lambda: (39.35, -104.67), 50, exits=exits)
    waits = []

    def fake_fetch(lat, lon, radius):
        script.exits_used.append(f.rotator.name)
        out = script.outcomes.pop(0)
        if not script.outcomes:
            f.stop()
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(f, "_fetch", fake_fetch)
    real_wait = f._stop.wait
    monkeypatch.setattr(f._stop, "wait", lambda t: (waits.append(t), real_wait(0))[1])
    f.run()
    return script.exits_used, waits


EXITS = [("direct", None), ("us-hub", "socks5h://10.22.0.9:1080"), ("eu-hub", "socks5h://10.22.0.13:1080")]
AC = [{"hex": "abc123", "lat": 39.4, "lon": -104.7, "alt_baro": 9000, "gs": 200, "track": 90}]


def test_dead_path_steps_to_next_exit_without_backoff(monkeypatch):
    used, waits = run_feeder(monkeypatch, [socket.timeout(), AC, AC], EXITS)
    assert used == ["direct", "us-hub", "us-hub"], "timeout -> next exit, then stays"
    assert all(w <= 2.0 for w in waits), f"no backoff for a path failure: {waits}"


def test_refusal_never_changes_exit(monkeypatch):
    used, waits = run_feeder(monkeypatch, [http_error(429), http_error(429), AC], EXITS)
    assert used == ["direct", "direct", "direct"], "429 keeps the same address"
    assert waits and waits[0] >= 2.0, "and backs off instead"


def test_failback_while_hub_only_refuses(monkeypatch):
    """2026-10-05: stuck on a hub that answered nothing but 429 -- the failback
    only ran after a success, so it never came. It must run before each poll."""
    eng = Engine()
    f = InternetFeeder(eng, "adsb_lol", lambda: (39.35, -104.67), 50, exits=EXITS)
    f.rotator.network_failure(0)      # direct -> us-hub, long ago
    f.rotator.failback_s = 60
    used, outcomes = [], [http_error(429), AC]

    def fake_fetch(lat, lon, radius):
        used.append(f.rotator.name)
        out = outcomes.pop(0)
        if not outcomes:
            f.stop()
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(f, "_fetch", fake_fetch)
    real_wait = f._stop.wait
    monkeypatch.setattr(f._stop, "wait", lambda t: real_wait(0))
    f.run()
    assert used == ["direct", "direct"], used


# --- adsb-hub (2026-10-05) ----------------------------------------------------

import feed_internet as FI


def hub_feeder(monkeypatch, hub, direct):
    eng = Engine()
    f = InternetFeeder(eng, "hub", lambda: (39.35, -104.67), 50, exits=EXITS)
    monkeypatch.setattr(FI, "_fetch_hub", hub)
    monkeypatch.setattr(FI, "_fetch_point", direct)
    return eng, f


FEED = {"center": {"lat": 39.35, "lon": -104.67, "radius_nm": 100}}


def test_hub_is_read_and_its_receivers_count_as_local(monkeypatch):
    rf = dict(AC[0], hex="def456", src="rf")
    eng, f = hub_feeder(monkeypatch, lambda *a: ([AC[0], rf], FEED),
                        lambda *a: pytest.fail("no direct poll while the hub answers"))
    assert len(f._fetch(39.35, -104.67, 50)) == 2
    assert f.label == "adsb-hub" and f.interval == 0.25
    assert f.hub_circle == (39.35, -104.67, 100)
    calls = []
    monkeypatch.setattr(eng, "update_aircraft", lambda icao, source, **kw: calls.append((icao, source)))
    f._ingest([AC[0], rf])
    assert calls == [("abc123", "internet"), ("def456", "local")]


def test_hub_down_falls_back_to_adsb_lol_without_rotating_exits(monkeypatch):
    def down(*a):
        raise ConnectionRefusedError("hub")
    eng, f = hub_feeder(monkeypatch, down, lambda *a: AC)
    assert f._fetch(39.35, -104.67, 50) == AC
    assert f.label == "adsb.lol direct (hub down)" and f.interval == 2.0
    assert f.rotator.name == "direct", "a hub failure is not a broken internet exit"


def test_view_outside_the_hub_circle_polls_directly(monkeypatch):
    eng, f = hub_feeder(monkeypatch, lambda *a: pytest.fail("hub cannot cover this"),
                        lambda *a: AC)
    f.hub_circle = (39.35, -104.67, 100)
    assert f._fetch(40.8, -111.9, 50) == AC          # Salt Lake City
    assert f.label == "adsb.lol direct (outside hub)"


# --- position fix times (2026-10-05: planes froze and jumped backwards) ------

def test_repeated_or_older_fix_does_not_move_the_plane_back():
    eng = Engine()
    now = __import__("time").time()
    eng.update_aircraft("abc123", lat=39.40, lon=-104.70, course_deg=90, speed_kt=200,
                        source="internet", pos_time=now - 1)
    a = eng._aircraft["ABC123"]
    assert a.last_pos == now - 1, "dead-reckon from the FIX time, not arrival"
    # the hub re-serves the same fix a second later; then an older one
    eng.update_aircraft("abc123", lat=39.40, lon=-104.70, source="internet", pos_time=now - 1)
    eng.update_aircraft("abc123", lat=39.39, lon=-104.71, source="internet", pos_time=now - 3)
    assert (a.lat, a.lon, a.last_pos) == (39.40, -104.70, now - 1)
    eng.update_aircraft("abc123", lat=39.40, lon=-104.69, source="internet", pos_time=now)
    assert a.lon == -104.69 and a.last_pos == now


def test_fetchers_stamp_fix_times_from_the_answers_own_clock():
    ac = FI._stamp([{"hex": "a", "seen_pos": 2.5}, {"hex": "b"}], 1_790_000_000_000)  # ms
    assert ac[0]["_pos_t"] == 1_789_999_997.5 and "_pos_t" not in ac[1]
    _, kw = FI.canonical_to_kwargs({**AC[0], "_pos_t": 123.0})
    assert kw["pos_time"] == 123.0


def test_hub_projection_is_plotted_and_fix_age_kept():
    a = {"hex": "abc123", "lat": 39.0, "lon": -104.0, "dr_lat": 39.01, "dr_lon": -104.0,
         "alt_baro": 9000, "dr_alt": 9100, "seen_pos": 4.0, "_pos_t": 1000.0}
    b = FI._hub_projected(a)
    assert (b["lat"], b["alt_baro"], b["_pos_t"], b["_fix_t"]) == (39.01, 9100, 1004.0, 1000.0)
    eng = Engine(predict_stale_s=3)
    now = __import__("time").time()
    _, kw = FI.canonical_to_kwargs({**b, "_pos_t": now, "_fix_t": now - 4})
    eng.update_aircraft("abc123", source="internet", **kw)
    [t] = eng.snapshot().tracks
    assert t.lat == 39.01 and t.predicted, "plotted at the hub's projection, flagged by fix age"
