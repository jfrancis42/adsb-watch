#!/usr/bin/env python3
"""Egress exits for the internet feeders: fail over when a PATH breaks.

An exit is a way out to the internet: ``direct`` (this host's own route) or a
SOCKS5 proxy -- in the n0gq estate the two hub proxies, Oregon and Stockholm,
the only ones the DMZ may use (skynet docs/firewalls.md 14.1b).

Configured with ``--internet-exit NAME=URL`` (repeatable) or
``$ADSB_EXITS="direct,us-hub=socks5h://10.22.0.9:1080,eu-hub=socks5h://10.22.0.13:1080"``.
Default: ``direct`` only, i.e. exactly the old behaviour.

WHAT ROTATES AND WHAT DOES NOT
------------------------------
* A NETWORK failure -- timeout, refused/reset connection, proxy down, DNS,
  HTTP 5xx, a body that is not JSON -- means the path is broken: move to the
  next exit at once and keep the radar fed.
* A REFUSAL -- HTTP 401/403/429 -- is the provider limiting *us*. It is NOT
  answered by changing address: that would be evading the limit. The feeder
  keeps its existing backoff on the same exit and the same source.
* After ``failback_s`` on a later exit, the first one is tried again --
  checked before every poll, not only after a success: the hub proxies are
  datacenter addresses that adsb.lol rate-limits far harder than the office
  line, so a feeder left on a hub can sit in 429s for hours (2026-10-05:
  a DNS blip at 10:47 UTC kept the radar on the hubs, blanking every few
  seconds). Returning to the FIRST exit is going home, not evading a limit.

Ported from adsb-log (sources.PathManager), which verified the same rule
against a dead proxy in production on 2026-10-04.
"""
from __future__ import annotations

import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

REFUSED = {401, 403, 429}


def parse_exits(specs: list[str] | None) -> list[tuple[str, str | None]]:
    """['direct', 'us-hub=socks5h://10.22.0.9:1080'] -> [(name, url|None)]."""
    if not specs:
        env = os.environ.get('ADSB_EXITS', '')
        specs = [s.strip() for s in env.split(',') if s.strip()] or ['direct']
    out = []
    for s in specs:
        name, _, url = s.partition('=')
        name = name.strip()
        url = url.strip() or None
        if name == 'direct':
            url = None
        elif url is None:
            raise ValueError(f'exit {name!r} needs a proxy URL (NAME=socks5h://host:port)')
        out.append((name, url))
    return out


def _opener(url: str | None) -> urllib.request.OpenerDirector:
    if url is None:
        return urllib.request.build_opener()
    import socks                      # PySocks
    from sockshandler import SocksiPyHandler
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ('socks5', 'socks5h'):
        raise ValueError(f'unsupported proxy scheme {u.scheme!r} (socks5/socks5h)')
    return urllib.request.build_opener(
        SocksiPyHandler(socks.SOCKS5, u.hostname, u.port or 1080,
                        rdns=(u.scheme == 'socks5h')))


def is_refusal(exc: BaseException) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code in REFUSED


def is_network(exc: BaseException) -> bool:
    """Anything that says the PATH is broken (not the provider's decision)."""
    if is_refusal(exc):
        return False
    return isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError,
                            ConnectionError, OSError, ValueError))


class ExitRotator:
    """One per feeder thread (no locking needed)."""

    def __init__(self, exits: list[tuple[str, str | None]], failback_s: float = 120):
        self.exits = exits
        self.openers = [_opener(u) for _, u in exits]
        self.current = 0
        self.failback_s = failback_s
        self.switched_at = 0.0

    @property
    def name(self) -> str:
        return self.exits[self.current][0]

    @property
    def opener(self) -> urllib.request.OpenerDirector:
        return self.openers[self.current]

    def network_failure(self, now: float | None = None) -> str | None:
        """Step to the next exit. Returns a status line, or None if only one."""
        if len(self.exits) < 2:
            return None
        old = self.name
        self.current = (self.current + 1) % len(self.exits)
        self.switched_at = now if now is not None else time.time()
        return f'exit {old} failed -> {self.name}'

    def maybe_failback(self, now: float | None = None) -> str | None:
        now = now if now is not None else time.time()
        if self.current and now - self.switched_at >= self.failback_s:
            old = self.name
            self.current, self.switched_at = 0, now
            return f'failing back {old} -> {self.name}'
        return None
