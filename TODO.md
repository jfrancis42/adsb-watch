# adsb-watch — TODO

## Real-time playback of recorded sessions

Session logging is **done** (see `recorder.py`): every received wire line is
written to a timestamped JSONL file (`adsb-watch-YYYYMMDD-HHMMSS.jsonl`) under
`--log-dir` (default `~/.local/share/adsb-watch/logs`), on by default.

**Not yet built: playback.** Replay a recorded file "in real time" — i.e. feed
the recorded lines back into the engine at the same inter-message intervals
they were originally received, so the UI (curses or web) shows the session
exactly as it happened.

### The recording format (what playback consumes)

JSON Lines. First line is a `__meta__` record; every other line is a wire
record:

```json
{"t": 1721340000.123, "src": "__meta__", "info": {"format": "adsb-watch-jsonl", "version": 1, "started_at": 1721340000.123, "uat": true, ...}}
{"t": 1721340000.456, "src": "adsb-sbs", "data": "MSG,3,..."}
{"t": 1721340000.501, "src": "uat-978",  "data": "{\"address\":\"a1b2c3\",...}"}
{"t": 1721340001.002, "src": "gps",       "data": "{\"class\":\"TPV\",...}"}
```

- `t` — epoch seconds when the line was received.
- `src` — stream id; maps 1:1 to the feeder that produced it:
  - `adsb-sbs` → `SbsFeeder._handle(text)`
  - `adsb-avr` → `AvrFeeder._handle_line(bytes)`
  - `uat-978`  → `UatFeeder._handle(text)`
  - `gps`      → `GpsFeeder._handle(bytes)`
  - `__meta__` / any `__*__` → skip (metadata, not wire data)
- `data` — the raw received line (newline-stripped string).

### Sketch of the implementation

- New `playback.py` with a `Playback` driver (mirrors the feeder threads: takes
  the engine, reads the file, dispatches by `src`).
- Read records, and for each, `sleep(next.t - prev.t)` (wall-clock gap) before
  dispatching — that reproduces the original timing. Skip `__*__` records.
- Dispatch each `data` to the matching feeder's parse method. Either
  instantiate the feeders with no socket and call their `_handle*` directly, or
  factor the parse logic out of the socket loop so it can be driven from a line
  instead of a socket. **Prefer the latter** — a small refactor so each feeder
  exposes `handle_line(str|bytes)` cleanly, keeping the network loop and the
  parse path separate. Playback then never touches sockets.
- Speed control: `--playback-speed N` (2.0 = twice as fast, 0.5 = half). Cap
  any single sleep (e.g. a long gap between sessions) with a `--playback-max-gap`
  so you're not stuck waiting through dead air.
- Wire into `main.py`: `--playback FILE` selects playback mode instead of live
  feeders (mutually exclusive with the SDR launchers — don't open dongles).
  Everything downstream (engine, registry lookups, facilities, UI, web) works
  unchanged because playback drives the same `engine.update_*` calls.
- The observer position replays from the recorded `gps` stream; a session
  recorded with `--fixed-lat/lon` has no `gps` records, so `--playback` should
  accept `--fixed-lat/lon` too (or record the fixed observer as a `__meta__`
  field and restore it — the meta record already carries session context).

### Nice-to-haves

- `--playback-loop` to repeat a file.
- A `playback-info FILE` command that prints the meta record + record counts
  per `src` + duration, without replaying.
- Seek/scrub (start at offset T into the recording).
- Compress old logs (`.jsonl.gz`); `Recorder`/playback both handle gzip
  transparently by extension.

## Other

- (from `uat-weather.md`) FIS-B weather decoding — separate, larger project.
