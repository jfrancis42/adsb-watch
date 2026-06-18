#!/usr/bin/env python3
"""Minimal WebSocket server test - no dependencies on engine."""
import asyncio
import json
import time
try:
    import websockets
except ImportError:
    print("ERROR: websockets library not installed")
    print("Install with: pip install websockets --break-system-packages")
    exit(1)

clients = set()

async def handler(websocket):
    """Handle a WebSocket connection."""
    print(f"Client connected: {websocket.remote_address}")
    clients.add(websocket)
    try:
        async for message in websocket:
            print(f"Received: {message}")
    except websockets.exceptions.ConnectionClosed:
        print(f"Client disconnected: {websocket.remote_address}")
    finally:
        clients.discard(websocket)

async def broadcast_loop():
    """Send test data every second."""
    counter = 0
    while True:
        if clients:
            counter += 1
            message = {
                'type': 'snapshot',
                'timestamp': time.time(),
                'counter': counter,
                'observer': {'lat': 39.54, 'lon': -104.76, 'alt_ft': 5400},
                'tracks': [],
                'history': {},
                'feeders': {'test': 'running'},
                'counts': {'test': counter}
            }
            msg_json = json.dumps(message)
            print(f"Broadcasting to {len(clients)} clients: counter={counter}")
            websockets.broadcast(clients, msg_json)
        await asyncio.sleep(1.0)

async def main():
    print("Starting test WebSocket server on ws://0.0.0.0:8765")
    print("Open http://localhost:8086/radar.html in your browser")
    print("(Make sure HTTP server is running: cd web && python3 -m http.server 8086)")
    print()

    async with websockets.serve(handler, "0.0.0.0", 8765):
        print("✓ WebSocket server listening on ws://0.0.0.0:8765")
        await broadcast_loop()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
