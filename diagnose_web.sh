#!/bin/bash
# Quick diagnostic for web UI connectivity issues

echo "=== ADS-B Watch Web UI Diagnostics ==="
echo

echo "1. Checking Python and dependencies..."
python3 --version
python3 -c "import websockets; print('websockets version:', websockets.__version__)" 2>&1
python3 -c "import ui_web; print('✓ ui_web imports successfully')" 2>&1
echo

echo "2. Checking ports..."
echo "Port 8765 (WebSocket):"
if netstat -tln 2>/dev/null | grep -q ':8765 '; then
    echo "  ⚠ Port 8765 is in use:"
    netstat -tlnp 2>/dev/null | grep ':8765 ' || netstat -tln 2>/dev/null | grep ':8765 '
else
    echo "  ✓ Port 8765 is available"
fi

echo "Port 8086 (HTTP):"
if netstat -tln 2>/dev/null | grep -q ':8086 '; then
    echo "  ⚠ Port 8086 is in use:"
    netstat -tlnp 2>/dev/null | grep ':8086 ' || netstat -tln 2>/dev/null | grep ':8086 '
else
    echo "  ✓ Port 8086 is available"
fi
echo

echo "3. Checking web files..."
if [ -f web/radar.html ]; then
    echo "  ✓ web/radar.html exists"
else
    echo "  ✗ web/radar.html missing"
fi

if [ -f web/radar.js ]; then
    echo "  ✓ web/radar.js exists"
else
    echo "  ✗ web/radar.js missing"
fi
echo

echo "4. Testing basic imports..."
python3 test_web.py
echo

echo "=== Next Steps ==="
echo
echo "To start the server:"
echo "  python3 main.py --web --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400"
echo
echo "To test WebSocket connectivity (in another terminal):"
echo "  python3 test_websocket.py"
echo
echo "To run minimal test server:"
echo "  python3 test_server.py"
echo
echo "Then open: http://localhost:8086/radar.html"
echo
echo "See TROUBLESHOOTING_WEB.md for detailed debugging steps."
