"""Curses front-end. Reads Engine snapshots and renders a sorted table.
Aircraft predicted to pass within `cpa_threshold_nm` are shown in reverse
video. This file is the ONLY place that touches curses; swap it out for a
Qt/web/etc. renderer without changing engine.py."""
import curses
import time

from engine import Engine


# ('header', width). The CPA column is wide because it packs three values
# (az / dist / time-to-go) into one cell — see `_fmt_cpa`.
COLUMNS = [
    ('CALL',  8),
    ('ICAO',  6),
    ('N#',    8),
    ('MFG',  10),
    ('MODEL', 10),
    ('OWNER', 16),
    ('PHASE',  8),
    ('APRT',   5),
    ('RWY',    4),
    ('ALT',    6),
    ('CRS',    4),
    ('SPD',    4),
    ('DIST',   6),
    ('AZ',     4),
    ('EL',     5),
    ('CPA(az/nm/eta)', 18),
    ('AGE',    5),   # extra column for the predicted-marker suffix '*'
]

SORT_NOW = 'now'   # current straight-line distance
SORT_CPA = 'cpa'   # predicted closest approach


def _fmt_eta(seconds: float) -> str:
    """Compact countdown: <1 min as Mss, otherwise MM:SS, capped at 59:59."""
    s = max(0, int(round(seconds)))
    if s >= 3600:
        return '>1h'
    return f'{s // 60:2d}:{s % 60:02d}'


def _fmt_cpa(track) -> str:
    if track.cpa_nm is None or track.cpa_seconds is None:
        return '-'
    az  = f'{int(round(track.cpa_azimuth_deg)) % 360:03d}'
    dnm = f'{track.cpa_nm:5.2f}'
    eta = _fmt_eta(track.cpa_seconds)
    return f'{az}/{dnm}/{eta}'


def _fmt(track) -> list[str]:
    def f(v, fmt, blank='-'):
        return blank if v is None else format(v, fmt)
    return [
        (track.callsign or '').strip()[:8],
        track.icao,
        (track.n_number or '')[:8],
        (track.manufacturer or '')[:10],
        (track.model or '')[:10],
        (track.owner or '')[:16],
        # AIRBORNE is the boring default — render it as '-' so the
        # interesting phases (TAKEOFF/LANDING/APPROACH/etc) actually pop.
        ('-' if track.phase in (None, 'AIRBORNE') else track.phase)[:8],
        (track.airport or '')[:5],
        (track.runway or '')[:4],
        f(track.alt_ft, '6.0f'),
        f(track.course_deg, '4.0f'),
        f(track.speed_kt, '4.0f'),
        f(track.distance_nm, '6.2f'),
        f(track.azimuth_deg, '4.0f'),
        f(track.elevation_deg, '5.1f'),
        _fmt_cpa(track),
        # '*' suffix flags a dead-reckoned row, in case A_DIM is invisible
        # on the terminal in use.
        f'{track.age_s:4.1f}{"*" if track.predicted else " "}',
    ]


def _sort_key(track, mode: str):
    """Sort key: missing values go to the bottom in either mode."""
    if mode == SORT_CPA:
        v = track.cpa_nm
    else:
        v = track.distance_nm
    return (v is None, v if v is not None else 0.0)


def _is_highlight(track, mode: str, threshold_nm: float) -> bool:
    """Highlight semantics follow the active sort:
       - NOW mode: aircraft currently within the threshold.
       - CPA mode: aircraft predicted to pass within the threshold."""
    if mode == SORT_CPA:
        return track.closing
    return (track.distance_nm is not None
            and track.distance_nm <= threshold_nm)


def run(engine: Engine, refresh_hz: float):
    curses.wrapper(_main, engine, refresh_hz)


def _main(stdscr, engine: Engine, refresh_hz: float):
    curses.curs_set(0)
    stdscr.nodelay(True)
    delay = max(0.05, 1.0 / refresh_hz)
    sort_mode = SORT_NOW

    while True:
        ch = stdscr.getch()
        if ch in (ord('q'), ord('Q'), 27):
            return
        if ch in (ord('s'), ord('S')):
            sort_mode = SORT_CPA if sort_mode == SORT_NOW else SORT_NOW
        if ch in (ord('o'), ord('O')):
            _prompt_observer(stdscr, engine)

        snap = engine.snapshot()
        _draw(stdscr, snap, sort_mode)
        time.sleep(delay)


def _prompt_observer(stdscr, engine: Engine):
    """Modal prompt: ask the user for lat / lon / alt_ft and pin the engine
    observer there. Pressing Esc or leaving a field blank cancels."""
    curses.curs_set(1)
    stdscr.nodelay(False)
    h, w = stdscr.getmaxyx()
    y = h - 4

    def ask(label: str, default=None) -> str | None:
        stdscr.move(y, 0); stdscr.clrtoeol()
        prompt = f'{label}: '
        if default is not None:
            prompt += f'[{default}] '
        stdscr.addnstr(y, 0, prompt, w - 1, curses.A_BOLD)
        stdscr.refresh()
        curses.echo()
        try:
            raw = stdscr.getstr(y, len(prompt), 32).decode('utf-8', 'replace').strip()
        except KeyboardInterrupt:
            return None
        finally:
            curses.noecho()
        if raw == '':
            return None if default is None else str(default)
        return raw

    obs = engine.snapshot().observer
    try:
        s_lat = ask('latitude  (deg)', f'{obs.lat:.6f}' if obs.lat is not None else None)
        if s_lat is None: return
        s_lon = ask('longitude (deg)', f'{obs.lon:.6f}' if obs.lon is not None else None)
        if s_lon is None: return
        s_alt = ask('altitude  (ft)',  f'{obs.alt_ft:.0f}' if obs.alt_ft else '0')
        if s_alt is None: return
        lat = float(s_lat); lon = float(s_lon); alt = float(s_alt)
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            raise ValueError('lat/lon out of range')
    except ValueError as e:
        # Show the error briefly, then bail.
        stdscr.move(y, 0); stdscr.clrtoeol()
        stdscr.addnstr(y, 0, f'invalid input: {e} (press any key)', w - 1,
                       curses.A_REVERSE)
        stdscr.refresh()
        stdscr.getch()
        return
    finally:
        curses.curs_set(0)
        stdscr.nodelay(True)

    engine.update_observer(lat, lon, alt, manual=True)


def _draw(stdscr, snap, sort_mode):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    obs = snap.observer
    if obs.lat is None:
        obs_str = 'observer: NO GPS FIX'
    else:
        obs_str = (f'observer: {obs.lat:8.4f}, {obs.lon:9.4f}  '
                   f'alt {obs.alt_ft:5.0f} ft')
    sort_label = 'predicted CPA' if sort_mode == SORT_CPA else 'current distance'
    header = (f'{obs_str}   tracks: {len(snap.tracks)}   '
              f'sort: {sort_label}   '
              f'highlight <= {snap.cpa_threshold_nm:.1f} NM   '
              f's=sort  o=set observer  q=quit')
    stdscr.addnstr(0, 0, header, w - 1, curses.A_BOLD)

    # Feeder status line — surfaces connection errors
    feeders = ' | '.join(
        f'{n}: {snap.feeders.get(n, "?")} ({snap.counts.get(n, 0)} msgs)'
        for n in sorted(snap.feeders)
    )
    if feeders:
        stdscr.addnstr(1, 0, feeders, w - 1)

    # Column headers
    col = 0
    for name, width in COLUMNS:
        if col + width >= w:
            break
        stdscr.addnstr(3, col, name.ljust(width), width, curses.A_UNDERLINE)
        col += width + 1

    # Rows
    y = 4
    tracks = sorted(snap.tracks, key=lambda t: _sort_key(t, sort_mode))
    for track in tracks:
        if y >= h:
            break
        # Highlight (reverse video) takes precedence over predicted (dim);
        # combining them is supported by curses but reads as muddy on most
        # terminals. A closing aircraft you also can't see right now should
        # still scream at you.
        if _is_highlight(track, sort_mode, snap.cpa_threshold_nm):
            attr = curses.A_REVERSE
        elif track.predicted:
            attr = curses.A_DIM
        else:
            attr = curses.A_NORMAL
        col = 0
        for value, (_, width) in zip(_fmt(track), COLUMNS):
            if col + width >= w:
                break
            stdscr.addnstr(y, col, value.ljust(width), width, attr)
            col += width + 1
        y += 1

    stdscr.refresh()
