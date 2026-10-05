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
