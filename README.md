# adsb-watch

Curses ADS-B traffic display backed by an RTL-SDR. Shows nearby aircraft sorted
by distance (or by predicted closest approach), enriched with FAA registry data
and live phase-of-flight classification (taxi / takeoff / approach / landing /
parked) against a 50-NM-radius airport database — both fed by the local
[govt-data](../govt-data/) service.

```
observer:  39.5400, -104.7600  alt  5400 ft   tracks: 7   sort: current distance   highlight <= 1.0 NM   s=toggle sort  q=quit
adsb-sbs: connected 127.0.0.1:30003 (SBS-1) (12942 msgs) | facilities: fresh (13 airports, drift 0.4 NM) | dump1090: launched readsb (pid 1647272)

CALL     ICAO   N#       MFG        MODEL      OWNER            PHASE    APRT  RWY  ALT     CRS  SPD  DIST   AZ   EL    CPA(az/nm/eta)     AGE
UAL2179  AB1234 N2179U   BOEING     737-924    UNITED AIRLINE   APPROACH KDEN  25   6800   270  140   3.21  092   2.7   270/0.18/00:48    0.4
SWA431   AC9876 N431WN   BOEING     737-700    SOUTHWEST AIRLI  TAKEOFF  KDEN  17R  5500   170  150   2.11  155   1.1   -                 0.8
N12ABC   A012BC N12ABC   CESSNA     172S       SMITH JOHN R     PARKED   KAPA  -    5870   -    -     8.91  175  -0.1   -                 1.2
…
```

## Requirements

- An RTL-SDR (or a remote host with one)
- A demodulator on the SDR host:
  - `readsb` (recommended — modern wiedehopf fork)
  - `dump1090-fa`, `dump1090-mutability`, or `dump1090`
- **Optional, for 978 MHz UAT (`--uat`):** a *second* RTL-SDR tuned to 978 and
  `dump978-fa` (FlightAware). See [978 MHz UAT](#978-mhz-uat-traffic) below.
- Network access to a govt-data instance for FAA registry, airport,
  runway, frequency, and navaid data. Defaults to `https://data.n0gq.org`,
  which requires HTTP Basic auth — see [Credentials](#credentials) below.
  Stand up your own with [govt-data](https://github.com/jfrancis42/govt-data)
  and override with `--govt-data-url`.
- `gpsd` (or pin the observer with `--fixed-lat/--fixed-lon`)
- Python 3.10+, `pip install -r requirements.txt`

## Credentials

`govt-data` requires HTTP Basic auth. The recommended way to provide
credentials is via environment variables:

```bash
export GOVT_DATA_USER=yourusername
export GOVT_DATA_PASS=yourpassword
python3 main.py
```

Equivalently you can `unset GOVT_DATA_*` and the program will fall through
to the empty defaults baked into `config.py`, which produces a `401` from
the server — you'll see this in the curses status line as
`facilities fetch failed: HTTP Error 401`. Set both vars and the failure
clears on the next poll.

For a one-off run without exporting:

```bash
GOVT_DATA_USER=u GOVT_DATA_PASS=p python3 main.py
```

If you need access to `data.n0gq.org` itself, ask the maintainer. The
credentials are not bundled with this repository.

## Run it

### Curses UI (default)

```bash
# Local SDR — auto-launches readsb/dump1090 in the background.
python3 main.py

# Remote SDR (RTL-SDR on another box on your LAN):
python3 main.py --dump1090-host other-box.local --no-launch-dump1090

# No GPS handy? Pin the observer:
python3 main.py --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400

# …or center the scope on an airport by code (ICAO / IATA / GPS / FAA local):
python3 main.py --airport KDEN      # Denver International
python3 main.py --airport S43       # Harvey Field
```

`--airport` resolves the code to lat / lon / field elevation via govt-data and
pins the observer there. It takes precedence over `--fixed-lat/-lon` and gpsd,
and fails fast with a clear message if the code is unknown.

Whichever of these you start with becomes **HOME** — the code the web UI's
centre selector offers to get back to (see below). Its elevation comes along,
so the AGL readout stays referenced to the ground at home.

The auto-launcher tries `readsb`, `dump1090-fa`, `dump1090-mutability`,
`dump1090` in that order. If port 30003 is already serving (e.g. systemd unit),
it leaves it alone and just connects.

### 978 MHz UAT traffic

978 MHz UAT (Universal Access Transceiver) is the second US ADS-B datalink,
used mostly by low-altitude general aviation. It's **off by default** — turn it
on with `--uat`. Because one RTL-SDR can't cover both 1090 and 978 at once, this
needs a **second dongle** tuned to 978 and the `dump978-fa` decoder on the SDR
host.

```bash
# Both bands, auto-launching readsb (1090) and dump978-fa (978):
python3 main.py --uat --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400
```

**Which dongle is which?** With two dongles plugged in, each decoder must be
pointed at the right one. adsb-watch auto-detects by a serial-number
convention: a dongle whose USB serial contains `978` is used for UAT, one
containing `1090` for ADS-B. Label your dongles once and it just works:

```bash
rtl_eeprom -d 0 -s 1090     # (replug required to take effect)
rtl_eeprom -d 1 -s 978
```

Or override explicitly (no relabeling needed) — list devices with `rtl_test`:

```bash
python3 main.py --uat \
  --adsb-device 0 \
  --uat-device 'driver=rtlsdr,rtl=1'
```

`--adsb-device` takes an index or serial (readsb/dump1090 `--device`);
`--uat-device` takes a SoapySDR device string (e.g. `driver=rtlsdr,serial=00000978`).

Already running dump978 under systemd? Point at it and skip the auto-launch:

```bash
python3 main.py --uat --uat-host 10.1.0.10 --uat-json-port 30979 --no-launch-dump978
```

Only aircraft *traffic* is ingested — dump978's JSON port emits no FIS-B
weather, so nothing extra to filter. UAT aircraft share the ICAO Mode S address
space with 1090, so a dual-link aircraft appears as a single merged track.

Installing `dump978-fa`: it's a FlightAware tool. On most systems build from
[FlightAware/dump978](https://github.com/flightaware/dump978)
(`make dump978-fa`; deps: boost, libusb, rtl-sdr, SoapySDR + the rtlsdr Soapy
module). On Arch, build from source rather than the AUR package (its boost
compat patch is stale).

### Internet ADS-B (no receiver required)

adsb-watch can pull live traffic from public internet aggregators instead of
(or alongside) a local RTL-SDR. It's **off by default** — enable with
`--internet`:

```bash
# Internet-only — no SDR needed. Pin an observer so there's a scope centre.
python3 main.py --no-launch-dump1090 --internet \
  --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400

# Local receiver PLUS internet fill-in (local data wins per-aircraft):
python3 main.py --internet
```

Sources (repeat `--internet-source` to pick a subset; default is
`adsb_lol` + `airplanes_live`):

| source           | endpoint                | poll   | notes |
|------------------|-------------------------|--------|-------|
| `adsb_lol`       | api.adsb.lol/v2         | 1 s    | readsb/tar1090 backend |
| `airplanes_live` | api.airplanes.live/v2   | 1 s    | same format as adsb.lol |
| `opensky`        | opensky-network.org     | 10 s (anon) / 5 s (auth) | bounding-box; set `OPENSKY_USERNAME`/`OPENSKY_PASSWORD` for the better rate |

```bash
python3 main.py --internet --internet-source opensky --internet-radius-nm 80
```

**Local data takes priority.** When both a local RTL-SDR and an internet source
report the same aircraft (matched by ICAO hex), the local position wins — the
internet copy is ignored while the local fix is fresh. If the aircraft drops off
the local receiver (out of antenna range, terrain shadow), internet data
seamlessly takes over after `--local-priority-s` seconds (default 5, auto-clamped
below `--expiry` so the track never blinks out during the handover). Aircraft the
local receiver never hears are shown from internet data alone.

Internet updates arrive at ~1 Hz (slower for OpenSky), but the same **5 Hz
dead-reckoning** used for local traffic applies to internet tracks too — each
aircraft's position is projected forward from its last report using its course,
speed, and vertical rate, so the display stays smooth between network updates.
An observer position is required (from gpsd or `--fixed-lat/--fixed-lon`) since
the aggregators are queried by a point + radius.

### Web radar UI

```bash
# Launch web UI with CRT phosphor effect
python3 main.py --web --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400

# Then open http://localhost:8080/radar.html in your browser
```

The web UI displays a circular radar scope with:
- Green phosphor CRT effect with persistence/fade
- Aircraft as arrows pointing in direction of flight
- 30-second position trails
- Range rings (default 5 NM radius with 1 NM intervals)
- North-up orientation with observer at center
- Aircraft labels showing altitude (ft), speed (mph), and type

WebSocket server runs on port 8765, HTTP server on 8080 (override with `--web-port` / `--http-port`).

#### Choosing the scope centre

The status bar has a **Center** box: type an airport code (`KPAE`, `S43`,
`DEN`) or the magic code `HOME` and press Enter, and the scope re-centres
there. The centre carries the airport's field elevation, so the AGL altitude
mode stays correct after a move; `HOME` carries `--fixed-alt-ft`.

**GPS overrides manual selection.** On an instance with a gpsd feeder the GPS
lamp is lit, the Center box is disabled, and a manual selection is refused —
gpsd owns the centre and the next fix would overwrite anything you typed. Click
the **GPS indicator** to switch it off, then choose; click it again to hand the
centre back. Re-enabling GPS doesn't move the scope: it stays where it is until
the next fix arrives. Instances started with `--fixed-lat`/`--airport` have no
gpsd feeder, so their GPS lamp is dimmed and manual selection is always
available.

There is only one centre, and it is shared: re-centring moves the scope for
every connected viewer. That is forced by the design — the internet feeders
query a box around the observer and the airport data is fetched around it, so a
client-side-only pan would show an empty scope over a place nothing was fetched
for. Use **`--no-web-recenter`** on a public instance to turn the box off;
`--airport` and the curses `o` key still work. Airports and runways at the new
centre appear once the facilities refetch finishes (it starts immediately, but
it is one request per airport in radius).

Full detail, including the WebSocket control messages, in `WEB_UI.md`.

#### KML/KMZ overlay

The web radar can overlay a KML or KMZ file — polygon boundaries, lines, and
point labels — drawn in **amber** so it reads as a distinct layer beneath the
green aircraft, trails, and airports. It's **off by default**; enable it with
`--kml`:

```bash
python3 main.py --web --kml \
  --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400
```

`--kml-file PATH` picks the file to overlay (default:
`COPA_v7_01-12-2026.kmz`, the Colorado Pilots Association practice areas):

```bash
python3 main.py --web --kml --kml-file /path/to/your-airspace.kmz ...
```

`kml.py` reads a `.kml` directly or `doc.kml` inside a `.kmz`, flattening every
placemark to its polygons / lines / points plus each element's name. The overlay
is static, so the server sends it once when a browser connects rather than on
every frame. This flag only affects the web UI — the curses UI ignores it.

**Getting the COPA practice-areas file:** the default `COPA_v7_01-12-2026.kmz`
is the Colorado Pilots Association's practice-area map, downloadable from the
[COPA practice areas page](https://coloradopilots.org/content.aspx?page_id=22&club_id=612720&module_id=540533).
It is **not** bundled in this repository — download it into the project directory
(or point `--kml-file` at wherever you saved it). The version/date in the
filename changes as COPA revises the boundaries; pass the current filename to
`--kml-file`.

### Hosted instance

A public instance runs at **https://adsb.n0gq.org** (internet-fed, centered on
the Colorado front range, COPA practice-area overlay on). It's deployed to
`10.1.17.20` as the `adsb-watch` systemd service and fronted by the n0gq.org
nginx TLS proxies. To (re)deploy or stand up your own:

```bash
cd ansible
ansible-playbook -i inventory.ini provision.yml
#   --tags dns      DNS A records only
#   --tags deploy   the 10.1.17.20 service only
#   --tags nginx    the us/eu proxy vhosts only
```

The playbook installs the service, its Python venv, `/etc/adsb-watch.env`
(govt-data creds), the DNS records, and the TLS reverse-proxy vhosts. See
`CLAUDE.md` → "Production deployment" for the architecture and the skynet
proxy conventions it follows.

### Keys (curses UI)

| key       | action                                              |
|-----------|-----------------------------------------------------|
| `s`       | toggle sort: current distance ↔ predicted CPA       |
| `o`       | set observer position by hand (lat / lon / alt ft); latches against further gpsd updates |
| `q` / Esc | quit                                                |

The reverse-video highlight follows the active sort. In **distance** mode,
aircraft *currently* within the threshold are highlighted. In **CPA** mode,
aircraft *predicted to pass* within the threshold are highlighted.

### Columns

| col                | meaning                                                |
|--------------------|--------------------------------------------------------|
| CALL / ICAO / N#   | ADS-B callsign, Mode S hex, FAA N-number               |
| MFG / MODEL / OWNER| FAA registry data (from govt-data)                     |
| PHASE              | flight phase — see [Phase classifier](#phase-classifier) |
| APRT               | airport ICAO ident if associated with one              |
| RWY                | runway end (e.g. `25`, `17R`) for takeoff / landing / approach / depart |
| ALT                | altitude (ft, baro)                                    |
| CRS / SPD          | course (deg true), groundspeed (kt)                    |
| DIST / AZ / EL     | from observer: distance (NM), azimuth, elevation       |
| CPA(az/nm/eta)     | predicted closest approach: bearing / NM / countdown   |
| AGE                | seconds since last message; suffixed with `*` when the displayed position is dead-reckoned (no fresh fix in 3+ s) |

`-` in CPA means the aircraft is not approaching (no velocity, or already past CPA).
`-` in PHASE means AIRBORNE (the boring default — only the interesting phases
TAKEOFF / LANDING / APPROACH / DEPART / TAXI / PARKED render explicitly so they
visually pop). `-` in APRT / RWY means no airport association.

### Dead-reckoning

The display redraws at 5 Hz. ADS-B reports come in at ~1 Hz at best, often
slower. Between updates, each aircraft's lat / lon / altitude are projected
forward from its last reported state using its course, ground speed, and
vertical rate — so the table stays smooth.

After **3 seconds** without a real update, the row is rendered dim and the
AGE column gets a `*` suffix to mark the position as predicted rather than
real. After **10 seconds** the aircraft is dropped from the display
entirely (configurable via `--expiry`).

### Phase classifier

Phases are inferred from position, course, speed, vertical rate, and the
runway geometry of nearby airports:

| phase    | meaning                                                   |
|----------|-----------------------------------------------------------|
| AIRBORNE | en-route / cruising / nothing else fits                   |
| APPROACH | aligned with a runway final, low and descending           |
| DEPART   | aligned with a runway departure leg, low and climbing     |
| LANDING  | on a runway, descending                                   |
| TAKEOFF  | on a runway, climbing                                     |
| TAXI     | on an airport surface, slow (< 40 kt)                     |
| PARKED   | on an airport surface, ~zero ground speed                 |

Tunable thresholds live at the top of `phase.py` (lateral tolerance,
heading tolerance, AGL ceiling, vertical-rate gates).

## Caching

All slow lookups are cached on disk (default 7 days, override with `--cache-ttl-days`):

- **`registry.json`** — FAA registry (`/aircraft/hex/{icao}`) by ICAO Mode S
  address. 404s are also cached, so unknown ICAOs aren't re-queried. Survives
  aircraft expiry — if a plane drops out of range and reappears, no extra
  HTTP call.
- **`facilities.json`** — Airports + runways + frequencies + navaids in a 50-NM
  radius around the receiver. Bucketed by 0.25° grid (~15 NM) so nearby
  restarts share a row. The receiver is assumed to be **mobile** — if the
  observer drifts more than 10 NM from the cache center, a refresh fires
  automatically.

Default location: `$XDG_CACHE_HOME/adsb-watch/{registry,facilities}.json`
(typically `~/.cache/adsb-watch/`).

Disable on-disk cache for a run with `--no-cache`. (In-memory dedup of
registry lookups still applies — repeated lookups in the same session are
free either way.)

## Tunables

Most everything is overridable via flag or `$ENV`:

| flag                  | env                | default                 |
|-----------------------|--------------------|-------------------------|
| `--dump1090-host`     | `DUMP1090_HOST`    | `127.0.0.1`             |
| `--dump1090-port`     | `DUMP1090_PORT`    | `30003` (SBS-1 CSV)     |
| `--avr`               | —                  | use raw 30002 instead   |
| `--no-launch-dump1090`| —                  | auto-launch on          |
| `--dump1090-binary`   | —                  | first found on PATH     |
| `--adsb-device`       | —                  | auto-detect (serial `1090`) |
| `--uat`               | —                  | 978 MHz UAT off         |
| `--uat-host`          | `UAT_HOST`         | `127.0.0.1`             |
| `--uat-json-port`     | `UAT_JSON_PORT`    | `30979`                 |
| `--no-launch-dump978` | —                  | auto-launch on (with `--uat`) |
| `--uat-device`        | —                  | auto-detect (serial `978`) |
| `--uat-gain`          | —                  | auto-gain               |
| `--gpsd-host`         | `GPSD_HOST`        | `127.0.0.1`             |
| `--gpsd-port`         | `GPSD_PORT`        | `2947`                  |
| `--fixed-lat/-lon/-alt-ft` | —             | skip gpsd, pin observer |
| `--airport`           | —                  | center on airport code (ICAO/IATA/GPS/local) |
| `--no-web-recenter`   | —                  | web clients may re-centre the scope |
| `--govt-data-url`     | `GOVT_DATA_URL`    | `https://data.n0gq.org` |
| `--cache-path`        | `ADSB_CACHE_PATH`  | `$XDG_CACHE_HOME/adsb-watch/registry.json` |
| `--cache-ttl-days`    | `ADSB_CACHE_TTL_S` | 7 days                  |
| `--no-cache`          | —                  | on-disk cache enabled   |
| `--cpa-nm`            | `CPA_HIGHLIGHT_NM` | `1.0`                   |
| `--expiry`            | `ADSB_EXPIRY`      | `10.0` seconds          |
| `--refresh-hz`        | `REFRESH_HZ`       | `4`                     |
| `--kml`               | —                  | web KML overlay off     |
| `--kml-file`          | —                  | `COPA_v7_01-12-2026.kmz` |
| `--internet`          | —                  | internet ADS-B off      |
| `--internet-source`   | —                  | `adsb_lol` + `airplanes_live` |
| `--internet-radius-nm`| —                  | `50`                    |
| `--local-priority-s`  | —                  | `5` (clamped < `--expiry`) |
| `--internet` (OpenSky)| `OPENSKY_USERNAME` / `OPENSKY_PASSWORD` | anonymous |

## Diagnostic

If no aircraft show up:

```bash
python3 probe.py [host]   # which dump1090 ports are open and what they emit
```

The curses header also surfaces feeder state (connected / errors / message
counts), the launcher's child-process status, and the facilities-fetch
status (`fetching`, `loaded cache (N airports) @ <bucket>`, `fresh`, etc).

## Files

| file            | role                                                            |
|-----------------|-----------------------------------------------------------------|
| `engine.py`     | UI-agnostic state + geometry; `Engine.snapshot()` is the API    |
| `geo.py`        | pure haversine / bearing / elevation / closest-approach math    |
| `phase.py`      | pure-functional flight-phase classifier                         |
| `feed_adsb.py`  | SBS-1 (30003) and AVR (30002) feeder threads                    |
| `feed_uat.py`   | 978 MHz UAT traffic feeder (dump978-fa JSON port; `--uat`)      |
| `feed_internet.py`| internet ADS-B feeders (adsb.lol / airplanes.live / OpenSky; `--internet`) |
| `feed_gps.py`   | gpsd JSON feeder thread                                         |
| `registry.py`   | govt-data `/aircraft/hex/{hex}` lookup thread                   |
| `airports.py`   | govt-data airport / runway / navaid client thread + dataclasses |
| `cache.py`      | atomic JSON disk cache with TTL eviction                        |
| `launcher.py`   | spawn/reap dump1090/readsb child process                        |
| `launcher_uat.py`| spawn/reap dump978-fa child process (978 MHz)                   |
| `sdr_detect.py` | enumerate RTL-SDRs; pick one per band by serial token           |
| `kml.py`        | parse a KML/KMZ overlay (polygons/lines/labels) for the web UI (`--kml`) |
| `ui_web.py`     | WebSocket + static-file server for the web radar UI             |
| `ui_curses.py`  | curses front-end (the *only* file that touches curses)          |
| `main.py`       | argparse + thread wiring                                        |
| `probe.py`      | port probe for dump1090 troubleshooting                         |
