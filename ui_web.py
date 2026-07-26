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


class RadarServer:
    """WebSocket server that broadcasts engine snapshots to all connected clients."""

    def __init__(self, engine, refresh_hz: float = 4.0, port: int = 8765,
                 overlay: Optional[dict] = None):
        self.engine = engine
        self.refresh_hz = refresh_hz
        self.port = port
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

    async def handler(self, websocket):
        """Handle a single WebSocket connection."""
        self.clients.add(websocket)
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

    def _prune_history(self, now: float):
        """Remove history for aircraft that haven't been seen in 15 minutes."""
        with self.history_lock:
            # Remove history for aircraft that disappeared > 15 min ago
            for icao in list(self.history.keys()):
                last_seen = self.aircraft_last_seen.get(icao, 0)
                if now - last_seen > self.history_retention_s:
                    del self.history[icao]
                    del self.aircraft_last_seen[icao]

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

    async def send_snapshot(self, websocket):
        """Send current state to a single client."""
        snapshot = self.engine.snapshot()
        self._update_history(snapshot)
        self._prune_history(time.time())

        # Build message with snapshot + history
        with self.history_lock:
            history_copy = {icao: list(trail) for icao, trail in self.history.items()}

        message = {
            'type': 'snapshot',
            'timestamp': snapshot.generated_at,
            'observer': {
                'lat': snapshot.observer.lat,
                'lon': snapshot.observer.lon,
                'alt_ft': snapshot.observer.alt_ft,
            } if snapshot.observer.lat is not None else None,
            'tracks': [
                {
                    'icao': t.icao,
                    'callsign': t.callsign,
                    'lat': t.lat,
                    'lon': t.lon,
                    'alt_ft': t.alt_ft,
                    'course_deg': t.course_deg,
                    'speed_kt': t.speed_kt,
                    'distance_nm': t.distance_nm,
                    'azimuth_deg': t.azimuth_deg,
                    'elevation_deg': t.elevation_deg,
                    'cpa_nm': t.cpa_nm,
                    'cpa_seconds': t.cpa_seconds,
                    'cpa_azimuth_deg': t.cpa_azimuth_deg,
                    'closing': t.closing,
                    'age_s': t.age_s,
                    'predicted': t.predicted,
                    'phase': t.phase,
                    'airport': t.airport,
                    'runway': t.runway,
                    'n_number': t.n_number,
                    'manufacturer': t.manufacturer,
                    'model': t.model,
                    'owner': t.owner,
                }
                for t in snapshot.tracks
            ],
            'history': history_copy,
            'feeders': snapshot.feeders,
            'counts': snapshot.counts,
            'facilities': self._serialize_facilities(snapshot.facilities) if snapshot.facilities else None,
        }

        await websocket.send(json.dumps(message))

    async def broadcast_loop(self):
        """Periodically broadcast snapshots to all connected clients."""
        interval = 1.0 / self.refresh_hz
        while True:
            if self.clients:
                # Broadcast to all connected clients
                websockets.broadcast(self.clients, await self._make_snapshot_message())
            await asyncio.sleep(interval)

    async def _make_snapshot_message(self):
        """Build a snapshot message (JSON string)."""
        snapshot = self.engine.snapshot()
        self._update_history(snapshot)
        self._prune_history(time.time())

        with self.history_lock:
            history_copy = {icao: list(trail) for icao, trail in self.history.items()}

        message = {
            'type': 'snapshot',
            'timestamp': snapshot.generated_at,
            'observer': {
                'lat': snapshot.observer.lat,
                'lon': snapshot.observer.lon,
                'alt_ft': snapshot.observer.alt_ft,
            } if snapshot.observer.lat is not None else None,
            'tracks': [
                {
                    'icao': t.icao,
                    'callsign': t.callsign,
                    'lat': t.lat,
                    'lon': t.lon,
                    'alt_ft': t.alt_ft,
                    'course_deg': t.course_deg,
                    'speed_kt': t.speed_kt,
                    'distance_nm': t.distance_nm,
                    'azimuth_deg': t.azimuth_deg,
                    'elevation_deg': t.elevation_deg,
                    'cpa_nm': t.cpa_nm,
                    'cpa_seconds': t.cpa_seconds,
                    'cpa_azimuth_deg': t.cpa_azimuth_deg,
                    'closing': t.closing,
                    'age_s': t.age_s,
                    'predicted': t.predicted,
                    'phase': t.phase,
                    'airport': t.airport,
                    'runway': t.runway,
                    'n_number': t.n_number,
                    'manufacturer': t.manufacturer,
                    'model': t.model,
                    'owner': t.owner,
                }
                for t in snapshot.tracks
            ],
            'history': history_copy,
            'feeders': snapshot.feeders,
            'counts': snapshot.counts,
            'facilities': self._serialize_facilities(snapshot.facilities) if snapshot.facilities else None,
        }

        return json.dumps(message)

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
        overlay: Optional[dict] = None):
    """Entry point for web UI. Starts WebSocket server and HTTP server for static files."""
    server = RadarServer(engine, refresh_hz, port, overlay=overlay)

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
