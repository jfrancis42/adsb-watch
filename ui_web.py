#!/usr/bin/env python3
"""WebSocket server for radar display.

Streams Engine snapshots to connected web clients at the configured refresh rate.
Each snapshot includes current aircraft state plus 30s of position history for trails.
"""
import asyncio
import json
import time
import threading
from dataclasses import asdict
from typing import Optional
try:
    import websockets
except ImportError:
    print("Missing websockets library. Install with: pip install websockets")
    raise


class ViewerGate:
    """Thread-safe view of whether any web client is currently connected.

    The internet ADS-B feeders (a separate daemon thread each) poll ``active()``
    to decide whether to hit the aggregators this cycle, so the public instance
    fetches one shared stream when someone's watching and nothing when nobody
    is. A short ``linger_s`` keeps the feed warm across a page refresh (the old
    socket closes a beat before the new one connects) so viewers don't see a
    cold gap.
    """

    def __init__(self, linger_s: float = 3.0):
        self._count = 0
        self._last_active = 0.0
        self._linger_s = linger_s
        self._lock = threading.Lock()

    def add(self):
        with self._lock:
            self._count += 1
            self._last_active = time.time()

    def remove(self):
        with self._lock:
            self._count = max(0, self._count - 1)
            self._last_active = time.time()

    def active(self) -> bool:
        with self._lock:
            if self._count > 0:
                return True
            return (time.time() - self._last_active) < self._linger_s


class RadarServer:
    """WebSocket server that broadcasts engine snapshots to all connected clients."""

    def __init__(self, engine, refresh_hz: float = 4.0, port: int = 8765,
                 overlay: Optional[dict] = None, viewer_gate: Optional['ViewerGate'] = None):
        self.engine = engine
        self.refresh_hz = refresh_hz
        self.port = port
        # Shared with the internet feeders so they only poll when a client is
        # watching. None in local/curses use, where the feeders always poll.
        self.viewer_gate = viewer_gate
        # Static KML/KMZ overlay (polygons/lines/points), or None. Sent once
        # per client on connect — it never changes, so it stays out of the
        # per-frame snapshot broadcast.
        self.overlay = overlay
        self.clients = set()
        # Track position history: {icao: [(timestamp, lat, lon, alt_ft, course_deg, speed_kt), ...]}
        self.history = {}
        self.history_lock = threading.Lock()
        # Track when each aircraft was last seen (to know when to purge history)
        self.aircraft_last_seen = {}
        self.history_retention_s = 900.0  # Keep history for 15 minutes after aircraft disappears
        self.trail_seconds = 300.0  # Trim each aircraft's trail to the last 5 minutes
        # --- incremental broadcast state -------------------------------- #
        # The full snapshot goes out ONCE per client, on connect.  After that
        # only what actually changed is broadcast.  Measured before this
        # change: 3.24 msg/s at a mean 626 KiB = 16.6 Mbit/s PER CLIENT, of
        # which 591 KiB was `history` and 62 KiB `facilities` -- both resent
        # in full three times a second.  The part that genuinely changes,
        # `tracks`, is 38 KiB.  A tab left open for four hours moved ~24 GB.
        self.last_broadcast_ts = 0.0     # watermark: points newer than this are new
        self.purged_since_broadcast = set()   # icaos dropped by _prune_history
        self.facilities_sig = None       # so static facilities are sent only on change
        self.announced_registry = set()  # icaos whose static registry data has been sent

    async def handler(self, websocket):
        """Handle a single WebSocket connection."""
        self.clients.add(websocket)
        # Re-announce every aircraft's registry data on the next broadcast.
        #
        # `announced_registry` is server-wide but connect frames are
        # per-client, so a client that joins late holds metadata only for the
        # aircraft airborne at its connect time.  An aircraft announced BEFORE
        # it joined -- one that dropped out of `tracks` and came back, its
        # history not yet aged out -- would be suppressed as already-announced
        # and stay permanently nameless for that client.  Measured: 140 such
        # tracks in a 30 s window.  Clearing costs one slightly larger frame
        # per connect (~8 KiB) and makes the omission impossible.
        self.announced_registry.clear()
        if self.viewer_gate is not None:
            self.viewer_gate.add()
        print(f"Client connected: {websocket.remote_address}")
        try:
            # Send the static overlay once (it never changes after load).
            if self.overlay is not None:
                await websocket.send(json.dumps({'type': 'overlay',
                                                 'overlay': self.overlay}))
            # Send initial snapshot immediately
            await self.send_snapshot(websocket)
            # Keep connection alive and handle any incoming messages
            async for message in websocket:
                # Future: handle control messages (zoom, trail length, etc.)
                print(f"Received message from client: {message}")
        except websockets.exceptions.ConnectionClosedOK:
            pass
        except websockets.exceptions.ConnectionClosed as e:
            print(f"Connection closed: {e}")
        finally:
            print(f"Client disconnected: {websocket.remote_address}")
            self.clients.discard(websocket)
            if self.viewer_gate is not None:
                self.viewer_gate.remove()

    def _prune_history(self, now: float):
        """Remove history for aircraft that haven't been seen in 15 minutes."""
        with self.history_lock:
            # Remove history for aircraft that disappeared > 15 min ago
            for icao in list(self.history.keys()):
                last_seen = self.aircraft_last_seen.get(icao, 0)
                if now - last_seen > self.history_retention_s:
                    del self.history[icao]
                    del self.aircraft_last_seen[icao]
                    # A full snapshot dropped these implicitly.  A delta must
                    # name them, or the client keeps the trail forever.
                    self.purged_since_broadcast.add(icao)
                    # Forget the registry announcement too.  The client drops
                    # its copy on purge, so if this aircraft comes back the
                    # server must describe it again -- otherwise it returns
                    # permanently nameless.  This also bounds the set.
                    self.announced_registry.discard(icao)

    def _update_history(self, snapshot):
        """Add current aircraft positions to history."""
        now = time.time()
        with self.history_lock:
            # Update last_seen for all currently visible aircraft
            for track in snapshot.tracks:
                self.aircraft_last_seen[track.icao] = now

                if track.lat is None or track.lon is None:
                    continue
                if track.icao not in self.history:
                    self.history[track.icao] = []
                trail = self.history[track.icao]
                # Only add if position changed or it's been >1s since last point
                if not trail or (trail[-1][1] != track.lat or trail[-1][2] != track.lon) or (now - trail[-1][0] > 1.0):
                    trail.append((now, track.lat, track.lon, track.alt_ft, track.course_deg, track.speed_kt))
                # Trim to the trailing window so trails don't grow without bound.
                cutoff = now - self.trail_seconds
                while trail and trail[0][0] < cutoff:
                    trail.pop(0)

    # Rounding.  Every numeric below arrives as a full-precision float --
    # `distance_nm 8.946862082756043`, `azimuth_deg 274.70185484360843` -- and
    # JSON writes all 17 digits.  None of it is meaningful: lat/lon to 5dp is
    # ~1.1 m, and the display rounds far harder than that anyway.
    @staticmethod
    def _r(v, nd):
        return round(v, nd) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

    def _build_message(self, full: bool) -> str:
        """Build one frame.

        ONE builder for both the per-client connect frame and the broadcast.
        These were two near-identical copies until 2026-08-29; every change
        had to be made twice, and the one time it was not -- the `full` flag
        landing only in the broadcast copy -- clients silently discarded their
        initial history.  Divergence here is not a style problem, it is the
        bug.

        full=True  -> everything, sent once when a client connects.
        full=False -> only what changed since the previous broadcast.
        """
        snapshot = self.engine.snapshot()
        self._update_history(snapshot)
        self._prune_history(time.time())

        r = self._r
        if full:
            with self.history_lock:
                hist = {icao: [[r(p[0], 1), r(p[1], 5), r(p[2], 5),
                                r(p[3], 0), r(p[4], 1), r(p[5], 1)] for p in trail]
                        for icao, trail in self.history.items()}
            purged = []
        else:
            since = self.last_broadcast_ts
            with self.history_lock:
                hist = {}
                for icao, trail in self.history.items():
                    fresh = [[r(p[0], 1), r(p[1], 5), r(p[2], 5),
                              r(p[3], 0), r(p[4], 1), r(p[5], 1)]
                             for p in trail if p[0] > since]
                    if fresh:
                        hist[icao] = fresh
                purged = sorted(self.purged_since_broadcast)
                self.purged_since_broadcast.clear()
            self.last_broadcast_ts = time.time()

        tracks = []
        for t in snapshot.tracks:
            d = {
                'icao': t.icao,
                'callsign': t.callsign,
                'lat': r(t.lat, 5),
                'lon': r(t.lon, 5),
                'alt_ft': r(t.alt_ft, 0),
                'course_deg': r(t.course_deg, 1),
                'speed_kt': r(t.speed_kt, 1),
                'distance_nm': r(t.distance_nm, 2),
                'azimuth_deg': r(t.azimuth_deg, 1),
                'elevation_deg': r(t.elevation_deg, 2),
                'cpa_nm': r(t.cpa_nm, 2),
                'cpa_seconds': r(t.cpa_seconds, 0),
                'cpa_azimuth_deg': r(t.cpa_azimuth_deg, 1),
                'closing': t.closing,
                'age_s': r(t.age_s, 1),
                'predicted': t.predicted,
                'source': t.source,
                'phase': t.phase,
                'airport': t.airport,
                'runway': t.runway,
            }
            # Registry metadata never changes for a given ICAO, so send it
            # once per aircraft instead of four times a second.  The connect
            # frame always carries it for everything currently up, so a client
            # joining mid-stream is complete; later arrivals are announced in
            # the delta that first shows them.
            if full or t.icao not in self.announced_registry:
                d['n_number'] = t.n_number
                d['manufacturer'] = t.manufacturer
                d['model'] = t.model
                d['owner'] = t.owner
                self.announced_registry.add(t.icao)
            tracks.append(d)

        message = {
            'type': 'snapshot',
            'full': full,
            'timestamp': snapshot.generated_at,
            'observer': {
                'lat': r(snapshot.observer.lat, 5),
                'lon': r(snapshot.observer.lon, 5),
                'alt_ft': r(snapshot.observer.alt_ft, 0),
            } if snapshot.observer.lat is not None else None,
            'tracks': tracks,
            'feeders': snapshot.feeders,
            'counts': snapshot.counts,
        }
        if full:
            message['history'] = hist
            message['trail_seconds'] = self.trail_seconds
        else:
            message['history_delta'] = hist
            message['history_purge'] = purged

        facilities = self._serialize_facilities(snapshot.facilities) if snapshot.facilities else None
        sig = None if facilities is None else hash(json.dumps(facilities, sort_keys=True))
        if full or sig != self.facilities_sig:
            message['facilities'] = facilities
            self.facilities_sig = sig

        return json.dumps(message)

    async def send_snapshot(self, websocket):
        """Send the complete current state to a single client (on connect)."""
        await websocket.send(self._build_message(full=True))

    async def broadcast_loop(self):
        """Periodically broadcast to all connected clients."""
        interval = 1.0 / self.refresh_hz
        while True:
            if self.clients:
                websockets.broadcast(self.clients, self._build_message(full=False))
            await asyncio.sleep(interval)

    def _serialize_facilities(self, facilities):
        """Convert Facilities object to JSON-serializable dict."""
        # Only include airports that have at least one runway
        return {
            'airports': [
                {
                    'ident': a.ident,
                    'name': a.name,
                    'lat': a.lat,
                    'lon': a.lon,
                    'runways': [
                        {
                            'le_ident': r.le_ident,
                            'le_lat': r.le_lat,
                            'le_lon': r.le_lon,
                            'le_heading_degt': r.le_heading_degt,
                            'he_ident': r.he_ident,
                            'he_lat': r.he_lat,
                            'he_lon': r.he_lon,
                            'he_heading_degt': r.he_heading_degt,
                            'length_ft': r.length_ft,
                            'width_ft': r.width_ft,
                            'closed': r.closed,
                        }
                        for r in a.runways
                    ]
                }
                for a in facilities.airports
                if a.runways and len(a.runways) > 0  # Only include airports with runways
            ]
        }

    async def serve(self):
        """Start the WebSocket server and broadcast loop."""
        async with websockets.serve(self.handler, "0.0.0.0", self.port):
            print(f"WebSocket server listening on ws://0.0.0.0:{self.port}")
            await self.broadcast_loop()


def run(engine, refresh_hz: float = 4.0, port: int = 8765, http_port: int = 8080,
        overlay: Optional[dict] = None, viewer_gate: Optional['ViewerGate'] = None):
    """Entry point for web UI. Starts WebSocket server and HTTP server for static files."""
    server = RadarServer(engine, refresh_hz, port, overlay=overlay,
                         viewer_gate=viewer_gate)

    # Start HTTP server for static files in a background thread
    import http.server
    import socketserver
    import os
    import threading

    web_dir = os.path.join(os.path.dirname(__file__), 'web')
    os.makedirs(web_dir, exist_ok=True)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=web_dir, **kwargs)

        def log_message(self, format, *args):
            pass  # Suppress HTTP logs

    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("", http_port), Handler)
    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()

    print(f"HTTP server listening on http://0.0.0.0:{http_port}")
    print(f"Open http://localhost:{http_port}/radar.html in your browser")

    # Run WebSocket server (blocks)
    asyncio.run(server.serve())


if __name__ == '__main__':
    print("This module should be run via main.py --web")
