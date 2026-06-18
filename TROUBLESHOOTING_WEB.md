# Web UI Troubleshooting Guide

## Problem: "DISCONNECTED" status, no data

### Step 1: Verify the server is running

When you run:
```bash
python3 main.py --web --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400
```

You should see:
```
HTTP server listening on http://0.0.0.0:8086
Open http://localhost:8086/radar.html in your browser
WebSocket server listening on ws://0.0.0.0:8765
```

If you don't see the "WebSocket server listening" line, the server didn't start.

### Step 2: Test WebSocket connectivity

Open another terminal and run:
```bash
cd ~/Dropbox/build/adsb-watch
python3 test_websocket.py
```

This will attempt to connect to the WebSocket server and report:
- ✓ Connected! - WebSocket is working
- ✗ Connection refused - Server is not running or port is blocked
- ✗ Timeout - Connected but no data received

### Step 3: Check browser console

1. Open the radar page: http://localhost:8086/radar.html
2. Press F12 to open developer tools
3. Go to the Console tab
4. Look for messages like:
   - "Connecting to ws://localhost:8765"
   - "WebSocket connected" (good!)
   - "WebSocket error" (bad - check the error details)

Common error messages:
- **"WebSocket connection to 'ws://localhost:8765' failed"** - Server not running
- **"net::ERR_CONNECTION_REFUSED"** - Port blocked or server crashed
- **"Connection closed. Code: 1006"** - Server died unexpectedly

### Step 4: Test with minimal server

If the full server isn't working, test with the minimal version:

Terminal 1:
```bash
cd ~/Dropbox/build/adsb-watch
python3 test_server.py
```

Terminal 2:
```bash
cd ~/Dropbox/build/adsb-watch/web
python3 -m http.server 8086
```

Then open http://localhost:8086/radar.html

The test server broadcasts a counter every second. If you see "counter=1, counter=2..." in the server output and the browser shows CONNECTED, then the WebSocket infrastructure works and the issue is with the main server.

### Step 5: Check for port conflicts

```bash
# Check if something is already using port 8765 (WebSocket)
sudo netstat -tlnp | grep 8765

# Check if something is already using port 8086 (HTTP)
sudo netstat -tlnp | grep 8086
```

If another process is using these ports, either:
- Kill that process, or
- Use different ports: `--web-port 8766 --http-port 8087`

### Step 6: Check firewall

If you're connecting from a different machine:
```bash
# Allow WebSocket port
sudo ufw allow 8765/tcp

# Allow HTTP port
sudo ufw allow 8086/tcp
```

### Step 7: Verify websockets library version

```bash
python3 -c "import websockets; print(websockets.__version__)"
```

Should show 11.0 or higher. If it's older:
```bash
pip install --upgrade websockets --break-system-packages
```

## Common Issues

### Issue: Server starts but no data appears

**Symptom:** Browser shows CONNECTED but radar is blank

**Cause:** Observer position not set or no aircraft in range

**Fix:**
1. Check that you passed `--fixed-lat`, `--fixed-lon`, `--fixed-alt-ft`
2. Verify observer position shows in status bar (not "Observer: --")
3. If using real ADS-B data, verify dump1090/readsb is running and feeding data

### Issue: Server crashes immediately

**Symptom:** Main server exits right after "WebSocket server listening"

**Cause:** Probably an exception in the broadcast loop or engine

**Fix:**
1. Run with full error output: `python3 main.py --web ... 2>&1 | tee server.log`
2. Look for Python tracebacks in the output
3. Common causes:
   - Engine snapshot() failing (missing observer position)
   - JSON serialization error (NaN or Infinity values)
   - Thread exception in one of the feeders

### Issue: Connection drops repeatedly

**Symptom:** Status flips between CONNECTED and DISCONNECTED

**Cause:** Server is crashing on each broadcast

**Fix:**
1. Check server output for exceptions
2. Try test_server.py - if that works, the issue is in the engine integration
3. Look for NaN/Infinity in aircraft data (these break JSON serialization)

### Issue: Data updates are slow or frozen

**Symptom:** Aircraft positions don't update, trails don't grow

**Cause:** WebSocket is connected but not receiving messages

**Fix:**
1. Check browser console for "Received message" logs
2. Verify server is printing "Client connected" when you open the page
3. Check if broadcast loop is running (should see activity in server terminal)
4. Try increasing --refresh-hz (e.g., `--refresh-hz 10`)

## Manual Testing Sequence

1. **Start the server:**
   ```bash
   python3 main.py --web --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400
   ```

2. **In another terminal, test WebSocket:**
   ```bash
   python3 test_websocket.py
   ```
   Should print: ✓ Connected! and show snapshot data

3. **Open the page:**
   http://localhost:8086/radar.html

4. **Check browser console (F12):**
   Should see:
   ```
   Connecting to ws://localhost:8765
   WebSocket connected
   Received message, size: <number>
   Snapshot: 0 tracks
   ```

5. **If no tracks, add simulated data:**
   - Modify engine.py to inject test aircraft, or
   - Point to a remote dump1090: `--dump1090-host <ip> --no-launch-dump1090`

## Debug Flags

Add these to get more verbose output:

```python
# In ui_web.py, add at the top:
import logging
logging.basicConfig(level=logging.DEBUG)
```

```javascript
// In radar.js, add to handleSnapshot():
console.log('Full snapshot:', JSON.stringify(data, null, 2));
```

## Still Stuck?

1. Capture full logs:
   ```bash
   python3 main.py --web --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400 2>&1 | tee full.log
   ```

2. In browser console, run:
   ```javascript
   window.radar.ws.readyState  // 0=CONNECTING, 1=OPEN, 2=CLOSING, 3=CLOSED
   ```

3. Check if observer is set:
   ```javascript
   window.radar.observer  // should show {lat, lon, alt_ft}
   ```

4. Force a test message from console:
   ```javascript
   window.radar.handleSnapshot({
     type: 'snapshot',
     timestamp: Date.now()/1000,
     observer: {lat: 39.54, lon: -104.76, alt_ft: 5400},
     tracks: [{
       icao: 'TEST01', callsign: 'TEST', lat: 39.55, lon: -104.77,
       alt_ft: 6000, course_deg: 90, speed_kt: 120,
       distance_nm: 1.0, azimuth_deg: 45
     }],
     history: {},
     feeders: {test: 'ok'},
     counts: {test: 1}
   });
   ```
   A test aircraft should appear on the radar.
