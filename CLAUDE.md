# adsb-watch — notes for Claude

Curses ADS-B watcher. RTL-SDR → dump1090/readsb → SBS-1 (port 30003) → engine
→ curses UI. Aircraft are also resolved against the FAA registry, and
classified into a flight-phase against a 50-NM-radius airport database, both
served by [govt-data](../govt-data/CLAUDE.md).

## Architecture

The single load-bearing rule: **`engine.py` is UI-agnostic.** The eventual
graphical front-end is meant to drop in by writing a new module that calls
`Engine.snapshot()` and ignoring everything else. Don't put curses (or any
output formatting) into the engine.

```
   feed_adsb.SbsFeeder      ──┐
   feed_gps.GpsFeeder       ──┤
   registry.RegistryClient  ──┤      +-------------+
   airports.FacilitiesClient──┴──>   |   Engine    |  ──> ui_curses.run()
                                     |  (locked)   |        (or future GUI)
                                     +-------------+
                                            ^
                                       launcher.Dump1090Launcher
                                       (subprocess of readsb/dump1090)

   cache.RegistryCache backs both the FAA registry and the airport
   facilities — JSON on disk, atomic writes, 7-day TTL eviction.
```

Threading model:
- One feeder thread each for ADS-B, GPS, registry, facilities. Daemon=True so
  they die with the main thread.
- All call `Engine.update_*` / `Engine.attach_registry` / `Engine.set_facilities`,
  which take the engine's RLock. The UI calls `Engine.snapshot()` which also locks.
- `Engine.snapshot()` returns a fresh, immutable view. The UI never holds
  references to internal `Aircraft` objects.
- `main.py` runs a small `fac-bridge` thread that pumps the FacilitiesClient's
  latest snapshot into the engine and reports its status as a feeder line —
  this avoids putting a govt-data dependency inside the engine itself.

## Module map

| file            | role                                                              |
|-----------------|-------------------------------------------------------------------|
| `engine.py`     | `Aircraft` (mutable), `Track`/`Snapshot` (read-only views), `Engine` |
| `geo.py`        | haversine / bearing / elevation / `closest_approach()`            |
| `phase.py`      | pure-functional `classify()` — flight-phase + airport + runway    |
| `feed_adsb.py`  | `SbsFeeder` (30003 CSV) + `AvrFeeder` (30002 raw hex)             |
| `feed_uat.py`   | `UatFeeder` — 978 MHz UAT traffic from dump978-fa JSON port (opt-in) |
| `feed_internet.py`| `InternetFeeder` — public aggregators (adsb.lol/airplanes.live/OpenSky), opt-in `--internet` |
| `feed_gps.py`   | TPV reader from gpsd JSON                                         |
| `registry.py`   | polls `Engine.pending_registry_lookups()`, calls govt-data        |
| `airports.py`   | `FacilitiesClient` thread + `Airport`/`Runway`/`Navaid`/`Facilities` |
| `cache.py`      | `RegistryCache` — atomic JSON disk cache with TTL eviction        |
| `launcher.py`   | spawn dump1090/readsb child, reap on exit                         |
| `launcher_uat.py`| spawn dump978-fa child (978 MHz), reap on exit                    |
| `sdr_detect.py` | enumerate RTL-SDRs, pick one per band by serial token (978/1090)  |
| `ui_curses.py`  | curses display — only file that imports `curses`                  |
| `ui_web.py`     | WebSocket server for web radar UI                                 |
| `kml.py`        | parse a KML/KMZ overlay (polygons/lines/point labels) for the web UI |
| `web/radar.html`| HTML5 Canvas radar display with CRT phosphor effect               |
| `web/radar.js`  | JavaScript radar rendering + WebSocket client                     |
| `main.py`       | argparse + wiring                                                 |
| `probe.py`      | troubleshooting helper                                            |

## Display data flow (one frame)

```
Engine._aircraft (dict[icao -> Aircraft])
   │  Engine.snapshot():
   │    1. expire by last_seen > expiry_s
   │    2. drop entries with stale or missing position
   │    3. for each survivor, _track_for() computes:
   │         dist, az, el (from observer)
   │         closest_approach() -> (raw_d, raw_t, raw_az)
   │         _smooth_cpa() — EMA over time, with elapsed-time decay
   │         phase.classify(facilities, ac_state) -> phase / airport / runway
   ▼
list[Track]  (immutable; sorted by ui_curses by chosen mode)
```

## Engine invariants worth not breaking

- `Aircraft.last_seen` updates on **any** message; `last_pos` only on a real
  lat/lon. Snapshot filtering uses `last_pos` so we don't keep showing a
  stale fix just because velocity-only frames keep arriving.
- CPA smoothing state (`cpa_*_smooth`) lives on `Aircraft`, not `Track`. UIs
  see only the smoothed values via `Track`.
- `closest_approach()` returns `None` for not-approaching aircraft (no
  velocity, zero speed, or t<=0). UIs render `None` as `-`.
- Bearings are smoothed on the unit circle (`_ema_angle`) — never average
  raw degrees, you'd cross the 0/360 seam.
- `phase.classify()` is **pure**. Don't reach into `Engine` from there. If
  you need new computed phase signals, the function takes them as kwargs.
- `Track` field order matters: dataclass fields with defaults must follow
  fields without. `age_s` is positional; phase/airport/runway/registry
  fields are at the end with defaults.

## Caching (cache.py)

- `RegistryCache(path, ttl_s)` is a generic key→JSON store. Used twice:
  - `registry.json` keyed by ICAO Mode S hex (FAA aircraft registry)
  - `facilities.json` keyed by `'facilities:<bucket>:<radius>'` (airport set)
- Writes are atomic (tempfile + `os.replace`) and rate-limited (default
  `save_interval_s=30`). `cache.flush()` is called from main.py's `finally:`.
- Stale entries (age > TTL) are dropped both at load time AND at `get()`
  time — long-running processes don't keep returning stale rows.
- 404s are cached as `None` so unknown ICAOs aren't re-queried.
- Default TTL is 7 days; override with `--cache-ttl-days N` /
  `$ADSB_CACHE_TTL_S` (seconds).

## Facilities subsystem (airports.py)

Designed for a **mobile** receiver. Periodically (every 60 s by default):
1. Reads observer (lat, lon) from the engine.
2. If we've never fetched, or drifted >10 NM from the cache center, or the
   cached row is older than TTL — refresh.
3. Refresh tries the on-disk cache first (bucketed by 0.25° grid, ~15 NM
   resolution, so two near-identical observer positions share a row).
4. On cache miss: hits govt-data for `/airports/near`, then for each airport
   `/airports/{ident}/runways` and `/airports/{ident}/frequencies`, and
   finally `/navaids/near`.

The data class set in `airports.py` (`Airport`, `Runway`, `Navaid`,
`Facilities`) is what `phase.py` consumes. Both are JSON-serializable through
`_serialize` / `_hydrate`.

## Phase classifier (phase.py)

Pure function `classify(*, lat, lon, alt_ft, course_deg, speed_kt,
vrate_fpm, facilities) -> PhaseResult`.

Returns one of `PARKED / TAXI / TAKEOFF / LANDING / APPROACH / DEPART /
AIRBORNE` plus the airport ident and runway end (`'25'`, `'17R'`, …) if
applicable.

Decision tree, in order:
1. Find nearest airport in the radius. If nothing within radius → AIRBORNE.
2. Test every runway end for alignment + position:
   - **on_runway** (lateral within 0.1 NM, between thresholds): TAKEOFF if
     climbing > +500 fpm; LANDING if descending < −200 fpm; otherwise
     speed-based (TAXI if slow, TAKEOFF/LANDING heuristic if fast).
   - **on_extended** (within 0.5 NM lateral of extended centerline, up to
     8 NM out, AGL < 3000 ft): APPROACH if descending; DEPART if climbing.
   - Heading misaligned with runway by >25° → not a match.
3. **on_field** test (within 2.5 NM of airport ref, AGL < 200 ft): PARKED if
   speed < 3 kt, TAXI if < 40 kt.
4. Fallback: AIRBORNE.

Tunables are constants at the top of `phase.py`. Closed runways are skipped.

## Dependencies

- pyModeS — only used by the AVR feeder. SBS-1 path has no pyModeS import,
  so this stays a soft dependency for the default config.
- `urllib` (stdlib) for both HTTP clients (registry, facilities). No `requests`.
- `curses` (stdlib).

## govt-data integration

Endpoints used:
- `GET /aircraft/hex/{icao_hex}` — FAA registry by Mode S hex
- `GET /airports/{ident}` — single airport lookup by ICAO/IATA/GPS/FAA-local
  code. `airports.resolve_airport()` uses this for `--airport` (center the
  scope on a named airport); returns `latitude_deg`/`longitude_deg`/`elevation_ft`.
- `GET /airports/near?lat&lon&radius_nm&limit` — airports in radius
- `GET /airports/{ident}/runways` — runway endpoints + true headings
- `GET /airports/{ident}/frequencies` — freqs in kHz (mostly cached, not yet
  surfaced in the UI)
- `GET /navaids/near?lat&lon&radius_nm&limit`

HTTP Basic auth; creds in govt-data's `auth.json`. 404 from `/aircraft/hex/`
means "not in DB" — registry client caches `None` and stops asking. Network
errors don't mark anything cached, so transient blips retry on the next poll.

See [[govt-data-api]] for the full endpoint list.

## RTL-SDR / dump1090 conventions

- Port **30003** is what we want — SBS-1 BaseStation CSV, already-decoded.
  `feed_adsb.SbsFeeder` is the default.
- Port **30002** is raw AVR hex (`*8D...;`) and needs pyModeS to decode;
  `--avr` switches to it.
- Port **30005** is Beast binary; we don't speak it.
- Auto-launch order in `launcher.CANDIDATES`: readsb → dump1090-fa →
  dump1090-mutability → dump1090. First on PATH wins. Skipped entirely if
  port 30003 is already serving (so a systemd-managed instance is fine).
- **readsb gotcha**: `readsb --net` does NOT open TCP ports by default and
  also defaults to net-only (no SDR). The launcher passes
  `--device-type=rtlsdr --gain=-10 --net-bind-address=0.0.0.0
  --net-sbs-port=30003 --net-ro-port=30002 --net-bo-port=30005` to make it
  behave like dump1090.
- On Mint/Ubuntu, `dump1090-mutability` ships a systemd unit that starts
  on boot. If you want adsb-watch to launch its own copy, stop+disable it.
- On Arch, `dump1090-fa-git` (AUR) installs its binary as `/usr/bin/dump1090`.
  Confusing but harmless.
- 10.1.0.10 is the SDR host; 10.1.17.20 hosts govt-data. Both reachable on
  the LAN. Run with `--dump1090-host 10.1.0.10 --no-launch-dump1090` to use
  the existing receiver there.

## 978 MHz UAT (feed_uat.py / launcher_uat.py / sdr_detect.py)

UAT (Universal Access Transceiver) is the second US ADS-B datalink, on 978 MHz,
used mostly by low-altitude GA. **Off by default — enable with `--uat`.**

- **Physical fact**: 978 needs its own RTL-SDR. One dongle can't do 1090 and 978
  at once. So `--uat` implies a *second* dongle.
- **Decoder**: FlightAware's `dump978-fa`. It reads the SDR via SoapySDR
  (rtlsdr factory) and re-serves decoded traffic as newline-delimited JSON on
  `--json-port` (we default to **30979**). It only emits DOWNLINK_SHORT/LONG
  (aircraft *traffic*) on that port — never FIS-B weather — so "traffic only"
  is free; no filtering needed on our side.
- **Feeder**: `UatFeeder` connects to that JSON port and maps each message onto
  `engine.update_aircraft()`. UAT addresses are the same 24-bit ICAO hex as
  1090, so a dual-link aircraft merges into one track. Units already match
  (ft / kt / deg-true / fpm). Prefers `pressure_altitude` then geometric;
  `true_track` then heading; barometric vrate then geometric.
- **Accepted `address_qualifier`s**: adsb_icao, adsb_other, tisb_icao,
  tisb_trackfile, adsr_other. `vehicle` / `fixed_beacon` (ground stations) are
  dropped — not aircraft traffic.

### The two-dongle problem (device selection)

With >1 dongle plugged in, each decoder must be told which to open or they race.
`sdr_detect.py` enumerates via `rtl_test` (preferred; always present with
librtlsdr) or `SoapySDRUtil --find`, and picks per band by a **serial-number
token**: a dongle whose serial contains `978` → UAT, `1090` → ADS-B.

- Out-of-the-box on the dev hardware: NooElec dongles ship as `stx:978:35` /
  `stx:1090:39`, so auto-detection just works.
- Other users: label dongles once with `rtl_eeprom -s 1090` / `-s 978`, or
  override explicitly with `--uat-device` (SoapySDR string, e.g.
  `driver=rtlsdr,serial=00000978` or `driver=rtlsdr,rtl=1`) and `--adsb-device`
  (index or serial for readsb/dump1090 `--device`).
- Detection runs **once at startup** in main.py, before any decoder launches
  (rtl_test momentarily opens device 0). With a single dongle present, selectors
  stay unset and each decoder opens the sole device.

### Ports / flags

- `--uat` — enable (default off).
- `--uat-host` / `--uat-json-port` — where dump978 JSON is (default
  127.0.0.1:30979). Point at a remote/systemd dump978 and add
  `--no-launch-dump978`.
- `--uat-device` — SoapySDR device string override.
- `--uat-gain` — fixed gain dB; default auto-gain.
- `--adsb-device` — 1090 dongle selector override.

### Building dump978-fa (Arch note)

The AUR `dump978-fa-git` PKGBUILD carries a boost-1.90 compat patch that fails
("already applied") on current trees. Build from source instead — it needs no
patch on boost 1.91:

```
git clone --depth 1 https://github.com/flightaware/dump978.git
cd dump978 && make dump978-fa      # deps: boost, libusb, rtl-sdr, soapysdr, soapyrtlsdr
sudo cp dump978-fa /usr/local/bin/
```

On 10.1.0.10 (mother) this is already installed at `/usr/local/bin/dump978-fa`.

## Internet ADS-B (feed_internet.py, --internet)

Pulls live traffic from public aggregators when there's no local receiver, or to
fill gaps in local coverage. **Off by default.** Ported from vestigare
(`server/feeds/`) — the REST endpoints, poll cadences, and OpenSky SI-unit
conversion come from there — but re-implemented as **stdlib-urllib daemon
threads** (not vestigare's asyncio/httpx) to match this repo's feeder style and
add no dependency. `InternetFeeder` polls one source, normalises to a canonical
dict, and calls `engine.update_aircraft(..., source='internet')`.

Two invariants make this work without touching the prediction path:

- **The 5 Hz dead-reckoning lives in `Engine._track_for()`, not in any feeder.**
  It runs on any aircraft that has course+speed, regardless of source. So
  internet tracks (which carry `track`/`gs`/`baro_rate`) get the same smoothing
  as local ones for free — the ~1 Hz network cadence is invisible on-screen.
- **Local-vs-internet priority is resolved in `Engine.update_aircraft()`, the
  single merge point.** Each aircraft records `last_local_pos`. While that's
  fresher than `local_priority_s`, internet *kinematic* updates (lat/lon + alt/
  course/speed/vrate) for that ICAO are dropped — local RTL-SDR wins. Callsign
  always merges (identity, not position). `local_priority_s` is clamped to
  `expiry_s - 1` in `__init__` so a plane leaving local range is handed to
  internet data *before* it would expire (no blink). `Track.source` carries
  `'local'`/`'internet'` out to UIs.

Sources: `adsb_lol`, `airplanes_live` (both the tar1090 point endpoint, 1 s),
`opensky` (bbox, 10 s anon / 5 s with `$OPENSKY_USERNAME`+`$OPENSKY_PASSWORD`).
airplanes.live 403s the default urllib User-Agent — `_get_json` sends an
explicit UA. Feeders read the observer position fresh each poll, so a moving
gpsd receiver re-centres the query.

## Adding a new front-end

1. Write `ui_yours.py` with a `run(engine, refresh_hz)` entry point.
2. Loop on `engine.snapshot()`. Don't touch `Engine._aircraft` directly.
3. Wire it from `main.py` — keep `ui_curses` as a fallback flag.
4. Resist the urge to do geometry in the UI. If you need a new computed
   field, add it to `Track` and compute it in `Engine._track_for`.

## Web UI (ui_web.py)

The web UI is a WebSocket server + HTML5 Canvas radar display:

- **WebSocket server** (`ui_web.py`): Streams `Engine.snapshot()` to connected
  clients at the configured refresh rate (default 4 Hz). Also maintains 30s of
  position history per aircraft for trail rendering.
- **HTML5 Canvas** (`web/radar.html` + `web/radar.js`): Circular radar scope
  with green CRT phosphor effect, aircraft as directional arrows, trails,
  range rings, and data labels (alt/speed/type).
- **Flat-earth projection**: For display ranges (< 50 NM), the JS side uses a
  simple Cartesian projection around the observer — 60 NM/degree latitude,
  `60 * cos(lat)` NM/degree longitude. Accurate enough and keeps the math fast.
- **Phosphor persistence**: Each frame is composited with a fading copy of the
  previous frame (`fadeCanvas`) to simulate CRT phosphor decay. Aircraft and
  trails are drawn on the main canvas, which is copied to `fadeCanvas` and
  dimmed by 8% per frame.

Launch with `python3 main.py --web --fixed-lat ... --fixed-lon ... --fixed-alt-ft ...`,
then open `http://localhost:8080/radar.html`. WebSocket port 8765, HTTP port 8080
(both configurable).

### KML/KMZ overlay (kml.py, web UI only)

`--kml` overlays a KML/KMZ file — polygon boundaries, lines, and point labels —
on the web radar, drawn in **amber** so it reads as a distinct layer under the
green aircraft/trails/airports. **Off by default.** `--kml-file PATH` chooses the
file (default: the bundled `COPA_v7_01-12-2026.kmz`, the Colorado Pilots
Association practice areas). Curses UI ignores it.

- `kml.py` reads a `.kml` directly or `doc.kml` inside a `.kmz`, and flattens
  every placemark to `{polygons, lines, points}` with each element's `<name>`.
  Coords are emitted `[lat, lon]` to match the JS `latLonToXY(lat, lon)` sig
  (KML stores `lon,lat,alt`). Folder hierarchy and per-feature styles are dropped.
- The overlay is **static**: `ui_web.RadarServer` sends it once per client as a
  `{'type':'overlay', ...}` message on connect, kept out of the 5 Hz snapshot
  broadcast. `radar.js` caches it in `this.overlay` and draws it in `render()`,
  clipped to the scope circle.

### WebSocket URL selection (radar.js)

`radar.js` picks the WS URL by page scheme:
- **HTTP** (`http://host:8086/radar.html`, direct/local): `ws://host:8765` —
  the WS server is a separate port on the same host.
- **HTTPS** (`https://adsb.n0gq.org/`, behind the TLS proxy): `wss://host/ws` —
  same-origin, so no mixed-content block; nginx proxies `/ws` → `:8765`. A
  plaintext `ws://…:8765` from an HTTPS page would be blocked and :8765 isn't
  public anyway.

## Production deployment (adsb.n0gq.org)

Runs as a systemd service on **10.1.17.20** (dmz), fronted by TLS at
`https://adsb.n0gq.org`. There is no SDR on 10.1.17.20 — traffic comes from the
`--internet` feeds. It mirrors the interactive command used on 10.1.0.10 (the
SDR host), minus the local receiver:

```
main.py --web --web-port 8765 --http-port 8086 --no-launch-dump1090 --kml \
        --fixed-lat 39.3553696 --fixed-lon -104.6729929 --fixed-alt-ft 6750 \
        --govt-data-url http://10.1.17.20:8091 --internet
```

- **systemd unit**: `adsb-watch.service` (in-repo, installed to
  `/etc/systemd/system/`). Reads `GOVT_DATA_USER`/`GOVT_DATA_PASS` (and optional
  `OPENSKY_*`) from `/etc/adsb-watch.env`.
- **Code path on 10.1.17.20**: `/home/jfrancis/adsb-watch` (rsynced from
  `~/Dropbox/build/adsb-watch`; the gitignored KMZ is copied separately so
  `--kml` has a file).
- **TLS/DNS**: `adsb.n0gq.org` A records → us (5.78.187.228) + eu
  (172.232.139.96). nginx on both proxies terminates TLS (wildcard `*.n0gq.org`
  cert) and forwards over WireGuard to `10.1.17.20:8086` (static) and
  `10.1.17.20:8765` (`/ws`). Bare `/` 302-redirects to `/radar.html`.
- **Deploy**: `cd ansible && ansible-playbook -i inventory.ini provision.yml`
  (tags: `dns`, `deploy`, `nginx`). See `ansible/provision.yml`.
- **Restart**: `ssh 10.1.17.20 "sudo systemctl restart adsb-watch"`.

Two skynet gotchas the playbook handles:
- The n0gq.org vhosts listen on `127.0.0.1:444 ssl proxy_protocol` (NOT `:443`)
  — the nginx **stream module** owns 443 and demuxes by SNI. Copying the
  (stale) `listen 443` pattern from an old vhost breaks routing.
- `nginx -t` must run with `OPENSSL_CONF=/etc/ssl/openssl-oqs.cnf`, or it
  rejects the post-quantum `ssl_conf_command Groups X25519MLKEM768:…` that
  every n0gq.org vhost sets (the systemd unit sets this env; a bare `nginx -t`
  does not).
- The `:444` listen means redirects need `absolute_redirect off` so `Location`
  doesn't leak `:444` to the browser.
