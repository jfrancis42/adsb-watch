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

```bash
# Local SDR — auto-launches readsb/dump1090 in the background.
python3 main.py

# Remote SDR (RTL-SDR on another box on your LAN):
python3 main.py --dump1090-host other-box.local --no-launch-dump1090

# No GPS handy? Pin the observer:
python3 main.py --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400
```

The auto-launcher tries `readsb`, `dump1090-fa`, `dump1090-mutability`,
`dump1090` in that order. If port 30003 is already serving (e.g. systemd unit),
it leaves it alone and just connects.

### Keys

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
| `--gpsd-host`         | `GPSD_HOST`        | `127.0.0.1`             |
| `--gpsd-port`         | `GPSD_PORT`        | `2947`                  |
| `--fixed-lat/-lon/-alt-ft` | —             | skip gpsd, pin observer |
| `--govt-data-url`     | `GOVT_DATA_URL`    | `https://data.n0gq.org` |
| `--cache-path`        | `ADSB_CACHE_PATH`  | `$XDG_CACHE_HOME/adsb-watch/registry.json` |
| `--cache-ttl-days`    | `ADSB_CACHE_TTL_S` | 7 days                  |
| `--no-cache`          | —                  | on-disk cache enabled   |
| `--cpa-nm`            | `CPA_HIGHLIGHT_NM` | `1.0`                   |
| `--expiry`            | `ADSB_EXPIRY`      | `10.0` seconds          |
| `--refresh-hz`        | `REFRESH_HZ`       | `4`                     |

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
| `feed_gps.py`   | gpsd JSON feeder thread                                         |
| `registry.py`   | govt-data `/aircraft/hex/{hex}` lookup thread                   |
| `airports.py`   | govt-data airport / runway / navaid client thread + dataclasses |
| `cache.py`      | atomic JSON disk cache with TTL eviction                        |
| `launcher.py`   | spawn/reap dump1090/readsb child process                        |
| `ui_curses.py`  | curses front-end (the *only* file that touches curses)          |
| `main.py`       | argparse + thread wiring                                        |
| `probe.py`      | port probe for dump1090 troubleshooting                         |
