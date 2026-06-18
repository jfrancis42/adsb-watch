#!/usr/bin/env python3
"""Test WebSocket connectivity."""
import asyncio
import websockets
import json

async def test_connect():
    uri = "ws://localhost:8765"
    print(f"Attempting to connect to {uri}...")
    try:
        async with websockets.connect(uri) as websocket:
            print("✓ Connected!")

            # Wait for a message
            print("Waiting for data...")
            message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            data = json.loads(message)

            print(f"✓ Received message type: {data.get('type')}")
            print(f"  Observer: {data.get('observer')}")
            print(f"  Tracks: {len(data.get('tracks', []))}")
            print(f"  Timestamp: {data.get('timestamp')}")

    except asyncio.TimeoutError:
        print("✗ Timeout waiting for data")
    except ConnectionRefusedError:
        print("✗ Connection refused - is the server running?")
        print("  Start with: python3 main.py --web --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400")
    except Exception as e:
        print(f"✗ Error: {e}")

if __name__ == '__main__':
    asyncio.run(test_connect())
