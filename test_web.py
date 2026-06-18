#!/usr/bin/env python3
"""Quick test to verify web UI can start without runtime errors."""

import sys

# Test imports
try:
    import ui_web
    import websockets
    print("✓ Imports successful")
except ImportError as e:
    print(f"✗ Import failed: {e}")
    sys.exit(1)

# Test that RadarServer can be instantiated
from engine import Engine

try:
    engine = Engine()
    server = ui_web.RadarServer(engine, refresh_hz=4.0, port=8765)
    print("✓ RadarServer instantiation successful")
except Exception as e:
    print(f"✗ RadarServer instantiation failed: {e}")
    sys.exit(1)

# Test snapshot serialization
try:
    engine.update_observer(39.54, -104.76, 5400.0)
    snapshot = engine.snapshot()
    print(f"✓ Engine snapshot successful: {len(snapshot.tracks)} tracks")
except Exception as e:
    print(f"✗ Snapshot failed: {e}")
    sys.exit(1)

print("\nAll basic tests passed. To run the full web UI:")
print("  python3 main.py --web --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400")
print("  Then open http://localhost:8080/radar.html")
