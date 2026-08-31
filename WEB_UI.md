# Web Radar UI

Green phosphor CRT-style radar display for adsb-watch. Shows aircraft as directional arrows with trails on a circular scope, north-up, observer at center.

## Quick Start

```bash
# With real ADS-B data (requires RTL-SDR + dump1090/readsb):
python3 main.py --web --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400

# Then open: http://localhost:8080/radar.html
```

```bash
# Demo mode (simulated aircraft, no SDR required):
cd web && python3 -m http.server 8080
# Open: http://localhost:8080/demo.html
```

## Architecture

```
main.py --web
  ↓
ui_web.py (WebSocket server :8765 + HTTP server :8080)
  ↓
web/radar.html + web/radar.js
  ↓
Canvas rendering with phosphor persistence effect
```

### Data Flow

1. **Backend** (`ui_web.py`):
   - Calls `engine.snapshot()` at ~4 Hz
   - Maintains 30s position history per aircraft for trails
   - Broadcasts JSON snapshots via WebSocket to all connected clients
   - Serves static HTML/JS files via simple HTTP server

2. **Frontend** (`web/radar.js`):
   - Connects to `ws://hostname:8765`
   - Receives snapshots with current tracks + 30s history
   - Renders on HTML5 Canvas at animation frame rate (~60 fps)
   - Applies CRT phosphor persistence by compositing faded previous frame

## Display Features

### Radar Scope
- **Circular display** with observer at center
- **North-up orientation** (north at top)
- **Range rings** at 1 NM intervals (default: 5 NM radius)
- **Cardinal direction labels** (N/S/E/W)
- **Crosshairs** at center marking observer position

### Aircraft Representation
- **Directional arrow** pointing in direction of flight (course)
- **60-second projection line** showing where aircraft will be in 60 seconds (configurable: None/15s/30s/60s)
- **CPA warning circle** around aircraft projected to pass within alert range (configurable: None/0.5-5 NM, default 1 NM)
- **Position trail** showing last 30 seconds of movement (configurable: None/10s/30s/60s/120s/300s/Full)
- **Data label** offset to the right of each aircraft:
  - Altitude (feet)
  - Speed (mph, converted from knots)
  - Aircraft type (abbreviated: B737, C172, A320, etc.)
- **Callsign** displayed above aircraft when available
- **Dimming** for stale/dead-reckoned positions (>3s without update)

### CRT Phosphor Effect
- Each frame composited with faded copy of previous frame
- 8% decay per frame (~0.13s half-life at 60fps)
- Creates authentic radar "persistence" where trails gradually fade
- Scanline overlay for additional CRT aesthetic
- Slight blur + contrast boost for phosphor glow

### Status Bar
- Observer position (lat, lon, altitude)
- **Scope centre selector**: type an airport code (`KPAE`, `S43`, `DEN`) or the
  magic code `HOME` and press Enter. See "Choosing the scope centre" below.
- Track count and airport count
- **Range selector**: 1, 2, 5, 10, 20, 50, 100 NM (default: 5 NM)
- **Trail length selector**: None, 10s, 30s, 1min, 2min, 5min, Full (default: 30s)
- **Projection time selector**: None, 15s, 30s, 60s (default: 60s)
- **Alert range selector**: None, 0.5, 1, 2, 3, 5 NM (default: 1 NM)
- **Sound effect toggles**: Approaching, Enter, Leave (all default on)
- **Indicator lights**: 1090 and 978 (green when that feeder is connected), GPS
  (green when gpsd is driving the centre — it is also a button, see below)

## Choosing the scope centre

Type an airport code into the **Center** box and press Enter; the scope
re-centres on that airport. `HOME` returns to whatever the instance was
launched pointing at (`--fixed-lat/--fixed-lon/--fixed-alt-ft`, or `--airport`).

The centre carries the airport's **field elevation**, so the AGL altitude mode
stays honest after a move — `HOME` carries the elevation given as
`--fixed-alt-ft`. If govt-data has no elevation for the chosen airport the
confirmation says so, because AGL would otherwise silently be MSL.

**GPS overrides manual selection.** On an instance with a gpsd feeder, the GPS
lamp is lit and the Center box is disabled — gpsd owns the centre and would
overwrite anything you typed on the next fix. Click the **GPS indicator** to
switch it off; the box then works. Click it again to hand the centre back to
gpsd. The scope does not jump when you re-enable GPS: it stays put until the
next fix arrives, so a momentary loss of fix does not blank the display — the
readout says `GPS (no fix yet)` until one does, because until then those are
not GPS coordinates.

The lamp has three states, because "I turned GPS off" and "there is no GPS
here" are different facts:

| GPS lamp | Meaning | Center box |
|---|---|---|
| green | gpsd is driving the centre | disabled |
| dark, clickable | gpsd available but switched off | enabled |
| dimmed, not clickable | no gpsd feeder on this instance | enabled |

Instances started with `--fixed-lat`/`--airport` have no gpsd feeder at all, so
their GPS lamp is dimmed and manual selection is always available. (Before this
feature the lamp lit green whenever *any* position was known, which meant a
green GPS light on a host with no GPS receiver.)

### It is one shared centre

The engine holds exactly **one** observer, so re-centring moves the scope for
every connected viewer, and their status bar flashes the new centre label. This
is not laziness: the internet feeders query a bounding box around the observer
and `FacilitiesClient` fetches airports around it, so a purely client-side pan
would draw an empty scope over a place no data was ever requested for. Airports
and runways at the new centre appear once the facilities refetch completes —
re-centring nudges that immediately (`FacilitiesClient.wake()`) instead of
letting it wait out the 60 s poll, but the fetch itself is one request per
airport in radius and takes a while.

Run with **`--no-web-recenter`** on a public instance to refuse the whole thing;
the Center box then stays hidden. `--airport` and the curses `o` prompt are
unaffected.

## Coordinate Projection

Uses flat-earth approximation (accurate for < 50 NM):

```
dLat = target_lat - observer_lat
dLon = target_lon - observer_lon

northNM = dLat * 60.0
eastNM = dLon * 60.0 * cos(observer_lat)

x_pixels = eastNM * pixels_per_NM
y_pixels = -northNM * pixels_per_NM  (negative because canvas Y grows down)
```

This matches the curses UI's display logic and avoids expensive spherical projections for every frame.

## WebSocket Message Format

```json
{
  "type": "snapshot",
  "timestamp": 1718726400.123,
  "observer": {
    "lat": 39.54,
    "lon": -104.76,
    "alt_ft": 5400
  },
  "center": {
    "source": "manual",
    "label": "HOME",
    "gps_available": false,
    "awaiting_fix": false,
    "recenter_enabled": true
  },
  "tracks": [
    {
      "icao": "AB1234",
      "callsign": "UAL2179",
      "lat": 39.56,
      "lon": -104.80,
      "alt_ft": 6800,
      "course_deg": 270,
      "speed_kt": 140,
      "distance_nm": 3.21,
      "azimuth_deg": 92,
      "elevation_deg": 2.7,
      "cpa_nm": 0.18,
      "cpa_seconds": 48,
      "cpa_azimuth_deg": 270,
      "closing": true,
      "age_s": 0.4,
      "predicted": false,
      "phase": "APPROACH",
      "airport": "KDEN",
      "runway": "25",
      "n_number": "N2179U",
      "manufacturer": "BOEING",
      "model": "737-924",
      "owner": "UNITED AIRLINES INC"
    }
  ],
  "history": {
    "AB1234": [
      [1718726370.1, 39.555, -104.795, 7200, 270, 145],
      [1718726371.2, 39.556, -104.798, 7100, 270, 143],
      ...
    ]
  },
  "feeders": {
    "adsb-sbs": "connected 127.0.0.1:30003 (SBS-1)",
    "facilities": "fresh (13 airports, drift 0.4 NM)"
  },
  "counts": {
    "adsb-sbs": 12942
  }
}
```

History entries: `[timestamp, lat, lon, alt_ft, course_deg, speed_kt]`

`observer` is `null` until there is a position. `center` is always present —
the controls have to render before the first fix, which under gpsd is exactly
when you might want to pin the scope by hand. `source` is `gps` / `manual` /
`unset` and describes what is *in control*, not whether a fix exists.
`awaiting_fix` is true when gpsd is in control but has not spoken since it took
over — the coordinates on screen are then the manual centre it replaced, and
the readout says `GPS (no fix yet)` rather than labelling them GPS.

### Client → server control messages

```json
{"cmd": "set_center", "airport": "KPAE"}   // or "HOME"
{"cmd": "set_gps",    "enabled": false}
```

Each is answered to the requesting client only:

```json
{
  "type": "center_result",
  "ok": true,
  "message": "Centred on KPAE (Seattle Paine Field International Airport) at 606 ft field elevation.",
  "ident": "KPAE",
  "source": "manual",
  "label": "KPAE",
  "gps_available": false,
  "enabled": true
}
```

Every refusal is an answer with `ok: false` and a message, never a dropped
message — a silently-swallowed command is a text box that looks broken.
Commands are rate-limited to one per connection per 0.5 s (each miss is an HTTP
round-trip to govt-data, and the input is a text box on a public page). Other
clients learn about the move from `center` in the next snapshot.

## Performance Notes

- Canvas size matches viewport (responsive, resizes on window resize)
- Rendering is ~60 fps (browser's requestAnimationFrame)
- WebSocket updates at 4 Hz (configurable via `--refresh-hz`)
- Trail rendering: simple line drawing, < 120 points per aircraft (30s @ 4 Hz)
- No reflow/repaints — all drawing on single canvas
- Phosphor fade compositing adds negligible overhead (drawImage + fillRect)

Tested smooth with 20+ aircraft on a 1080p display.

## Files

| File | Purpose |
|------|---------|
| `ui_web.py` | WebSocket + HTTP server, position history tracking |
| `web/radar.html` | Page shell, status bar, canvas container, CRT effects |
| `web/radar.js` | Radar rendering, WebSocket client, coordinate projection |
| `web/demo.html` | Standalone demo with simulated aircraft (no backend) |

## Configuration

Command-line flags (passed to `main.py --web`):

| Flag | Default | Description |
|------|---------|-------------|
| `--web-port` | 8765 | WebSocket port |
| `--http-port` | 8080 | HTTP server port |
| `--refresh-hz` | 4.0 | Snapshot broadcast rate (Hz) |
| `--cpa-nm` | 1.0 | CPA highlight threshold (not yet used in web UI) |
| `--expiry` | 10.0 | Aircraft expiry time (seconds) |

JavaScript constants (in `radar.js`):

```javascript
const PHOSPHOR_GREEN = '#00ff00';
const TRAIL_COLOR = '#006600';
const GRID_COLOR = '#00aa00';  // Brighter for sunlight visibility
```

Range, trail length, projection time, and alert range are all user-configurable via dropdown controls in the status bar.

## Sound Effects

The radar plays pleasant musical tones to alert you to aircraft proximity:

- **Approaching sound** (rising C5→E5→G5): Plays when an aircraft is projected to pass within your alert range
- **Enter sound** (bright G5→C6 chime): Plays when an aircraft actually enters your alert range
- **Leave sound** (descending G5→E5→C5): Plays when an aircraft leaves your alert range

Each sound can be independently toggled on/off via checkboxes in the status bar. All default to on.

The sounds use the Web Audio API to generate pure tones — no audio files required. Sounds only trigger on state transitions (not repeatedly), and only for the first transition of each type per aircraft.

## Airport Display

When facilities data is loaded, the radar displays:

- **Airport markers** (small circles) at airport reference points
- **Runway outlines** with width and orientation from OurAirports data
- **Runway identifiers** (e.g., "25L", "17R") at each threshold
- **Approach centerlines** extending 8 NM from each runway threshold (dashed green lines)

Only airports with valid runway data are shown. Closed runways are excluded.

## Future Enhancements

Planned but not yet implemented:

- **Click aircraft for detail popup**: Full registry, phase, CPA countdown
- **Range/bearing measurement tool**: Click-drag to measure distance/bearing
- **Replay mode**: Scrub through recorded history
- **Settings panel**: Toggle scanlines, phosphor persistence, labels
- **Multi-observer mode**: Side-by-side displays for different locations

The UI architecture supports these — they just need control message handlers in `ui_web.py` and UI elements in `radar.html`.

## Troubleshooting

**WebSocket won't connect:**
- Check firewall allows port 8765
- Verify `ui_web.py` prints "WebSocket server listening on ws://0.0.0.0:8765"
- Open browser console (F12) and look for connection errors

**Blank radar (no aircraft):**
- Check status bar shows "CONNECTED"
- Verify observer position is set (not "Observer: --")
- Confirm dump1090/readsb is running and feeding data (check main.py output)
- Try demo.html to verify rendering works

**Choppy/laggy display:**
- Reduce `--refresh-hz` (try 2 Hz)
- Check browser console for errors
- Verify WebSocket isn't disconnecting/reconnecting (should stay CONNECTED)

**Aircraft positions wrong:**
- Observer position must be accurate — double-check `--fixed-lat/lon`
- If using gpsd, verify TPV messages arriving (check main.py feeder status)

## Demo Mode

Standalone HTML file with simulated aircraft, no backend required:

```bash
cd ~/Dropbox/build/adsb-watch/web
python3 -m http.server 8080
# Open http://localhost:8080/demo.html
```

Four simulated aircraft slowly orbit the observer with trails. Useful for:
- Testing radar rendering without SDR hardware
- Demonstrating the UI to others
- Debugging display issues (trails, labels, phosphor effect)

## Development Notes

The web UI follows the "UI-agnostic engine" principle from `CLAUDE.md`:
- Never touches `Engine._aircraft` directly
- Only calls `Engine.snapshot()`
- All geometry (distance, bearing, CPA) computed in `engine.py`
- Display code only handles projection (lat/lon → x/y pixels) and rendering

To add new computed fields (e.g. closure rate, time-since-last-turn):
1. Add the field to the `Track` dataclass in `engine.py`
2. Compute it in `Engine._track_for()`
3. Include it in the WebSocket message in `ui_web.py`
4. Read it from `track.*` in `radar.js` and render accordingly

Don't compute geometry in JavaScript — keep the display layer thin.
