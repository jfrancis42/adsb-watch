#!/usr/bin/env python3
"""Wire the engine, the three feeders, and the curses UI together.

By default we also auto-launch dump1090/readsb in the background — turn that
off with --no-launch-dump1090 if you have one already running under systemd
(we'll detect that anyway by checking the port first).
"""
import argparse

import config
import os
from airports import FacilitiesClient, lookup_airport, AirportLookupError
from audio import AudioAlerter
from cache import RegistryCache
from engine import Engine
from feed_adsb import SbsFeeder, AvrFeeder
from feed_gps import GpsFeeder
from feed_internet import InternetFeeder, SOURCES as INTERNET_SOURCES
from feed_uat import UatFeeder
import kml as kml_loader
from launcher import Dump1090Launcher
from launcher_uat import Dump978Launcher
from recorder import Recorder, default_log_path
from registry import RegistryClient
import sdr_detect
import threading
import ui_curses
import ui_web


def main():
    p = argparse.ArgumentParser(description='ADS-B watcher with FAA registry overlay')
    p.add_argument('--web', action='store_true',
                   help='Launch web radar UI instead of curses (http://localhost:8086/radar.html)')
    p.add_argument('--web-port', type=int, default=8765,
                   help='WebSocket port for web UI (default 8765)')
    p.add_argument('--http-port', type=int, default=8086,
                   help='HTTP port for web UI (default 8086)')
    p.add_argument('--dump1090-host', default=config.DUMP1090_HOST)
    p.add_argument('--dump1090-port', type=int, default=config.DUMP1090_PORT,
                   help='Port to connect to (default 30003 SBS-1, or 30002 with --avr)')
    p.add_argument('--avr', action='store_true',
                   help='Use AVR raw hex feed (port 30002) instead of SBS-1.')
    p.add_argument('--no-launch-dump1090', dest='launch_dump1090',
                   action='store_false',
                   help='Do not auto-launch dump1090/readsb; assume it is already running.')
    p.add_argument('--dump1090-binary',
                   help='Force a specific binary (readsb, dump1090-fa, dump1090, …).')
    # --- 978 MHz UAT (default off) ---------------------------------------
    p.add_argument('--uat', action='store_true',
                   help='Also decode 978 MHz UAT traffic (needs a second RTL-SDR '
                        'tuned to 978 and dump978-fa). Default off.')
    p.add_argument('--uat-host', default=config.UAT_HOST,
                   help='Host serving dump978 JSON (default 127.0.0.1).')
    p.add_argument('--uat-json-port', type=int, default=config.UAT_JSON_PORT,
                   help='dump978-fa --json-port to connect to (default 30979).')
    p.add_argument('--no-launch-dump978', dest='launch_dump978',
                   action='store_false',
                   help='Do not auto-launch dump978-fa; assume it is already running.')
    p.add_argument('--uat-device',
                   help='SoapySDR device string for the 978 dongle, e.g. '
                        '"driver=rtlsdr,serial=00000978" or "driver=rtlsdr,rtl=1". '
                        'Default: auto-detect a dongle whose serial contains "978".')
    p.add_argument('--uat-gain',
                   help='Fixed gain (dB) for the 978 dongle; default auto-gain.')
    p.add_argument('--adsb-device',
                   help='RTL-SDR selector (index or serial) for the 1090 dongle, '
                        'passed to readsb/dump1090 --device. Default: auto-detect a '
                        'dongle whose serial contains "1090" when >1 dongle is present.')
    # --- Internet ADS-B (public aggregators, default off) ----------------
    p.add_argument('--internet', action='store_true',
                   help='Also pull live ADS-B from public internet aggregators '
                        '(adsb.lol, airplanes.live, OpenSky). Local RTL-SDR data '
                        'takes priority per-aircraft; internet fills the gaps. '
                        'Needs an observer position (gpsd or --fixed-lat/-lon). '
                        'Default off.')
    p.add_argument('--internet-source', action='append', metavar='SOURCE',
                   help='Which internet source(s) to use (repeatable). Choose '
                        'from: adsb_lol, airplanes_live, opensky. Default: '
                        'adsb_lol (airplanes_live disabled 2026-08-29: the '
                        'service returns 403 to everyone). OpenSky honours '
                        '$OPENSKY_USERNAME/$OPENSKY_PASSWORD for a better rate.')
    p.add_argument('--predict-stale', type=float, default=None, metavar='SECONDS',
                   help='Mark a position as predicted (dead-reckoned) once it is '
                        'this old. Default 3 s, which suits RF. Raise it for a '
                        'polled internet feed whose poll interval approaches it, '
                        'or nearly every aircraft reads as predicted.')
    p.add_argument('--internet-radius-nm', type=float, default=50.0,
                   help='Query radius (NM) around the observer for internet '
                        'sources (default 50).')
    p.add_argument('--local-priority-s', type=float, default=5.0,
                   help='Seconds a local RTL-SDR fix suppresses internet data '
                        'for the same aircraft (default 5; auto-clamped below '
                        '--expiry). Only relevant with --internet.')
    p.add_argument('--gpsd-host',     default=config.GPSD_HOST)
    p.add_argument('--gpsd-port',     type=int, default=config.GPSD_PORT)
    p.add_argument('--govt-data-url', default=config.GOVT_DATA_URL)
    p.add_argument('--cpa-nm',        type=float, default=config.CPA_HIGHLIGHT_NM)
    p.add_argument('--expiry',        type=float, default=config.EXPIRY_SECONDS)
    p.add_argument('--refresh-hz',    type=float, default=config.REFRESH_HZ)
    p.add_argument('--fixed-lat',     type=float,
                   help='Skip gpsd; pin the observer to this latitude.')
    p.add_argument('--fixed-lon',     type=float)
    p.add_argument('--fixed-alt-ft',  type=float, default=0.0)
    p.add_argument('--airport', metavar='CODE',
                   help='Center the radar on an airport instead of gpsd/--fixed-*. '
                        'Accepts ICAO, IATA, GPS, or FAA local codes (e.g. KAWO, '
                        'S43, DEN); resolved to lat/lon/elevation via govt-data.')
    p.add_argument('--no-web-recenter', dest='web_recenter',
                   action='store_false',
                   help='Do not let web clients move the scope centre. The '
                        'engine has ONE observer, so a client that re-centres '
                        'moves it for every connected viewer — use this on a '
                        'public instance. Curses and --airport are unaffected.')
    p.add_argument('--cache-path',    default=config.REGISTRY_CACHE_PATH,
                   help='On-disk JSON cache for FAA registry lookups.')
    p.add_argument('--cache-ttl-days', type=float,
                   default=config.REGISTRY_CACHE_TTL_S / 86400.0,
                   help='How long cached registry entries are reused (default 7).')
    p.add_argument('--no-cache',      action='store_true',
                   help='Disable the on-disk registry cache for this run.')
    p.add_argument('--no-log',        dest='log', action='store_false',
                   help='Disable session logging (on by default — every received '
                        'line is recorded to a timestamped JSONL file for replay).')
    p.add_argument('--log-dir',       default=config.LOG_DIR,
                   help='Directory for session logs (default '
                        '~/.local/share/adsb-watch/logs). A start-time-stamped '
                        'file adsb-watch-YYYYMMDD-HHMMSS.jsonl is created in it.')
    p.add_argument('--log-file',
                   help='Exact log file path, overriding --log-dir and the '
                        'auto-generated timestamped name.')
    p.add_argument('--audio-flag',    action='store_true',
                   help='Play an audible "ding ding ding" when an aircraft is '
                        'within ~1 minute of passing within the CPA threshold '
                        '(default 1 NM, see --cpa-nm). One alert per pass.')
    p.add_argument('--audio-lead-s',  type=float, default=60.0,
                   help='Seconds-to-CPA threshold for the audible alert.')
    # --- KML/KMZ overlay (web UI only, default off) ----------------------
    p.add_argument('--kml', action='store_true',
                   help='Overlay a KML/KMZ file (boundaries + labels) on the '
                        'web radar. Default off. See --kml-file.')
    p.add_argument('--kml-file',
                   default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        'COPA_v7_01-12-2026.kmz'),
                   help='KML or KMZ file to overlay when --kml is given '
                        '(default: the bundled COPA_v7_01-12-2026.kmz).')
    args = p.parse_args()

    cache = facility_cache = None
    if not args.no_cache:
        cache = RegistryCache(args.cache_path,
                              ttl_s=args.cache_ttl_days * 86400.0)
        # Separate cache file for facilities — different schema, different
        # access pattern (one entry per geographic bucket).
        facility_cache_path = os.path.join(
            os.path.dirname(args.cache_path), 'facilities.json')
        facility_cache = RegistryCache(
            facility_cache_path, ttl_s=args.cache_ttl_days * 86400.0)
    engine = Engine(expiry_s=args.expiry, cpa_threshold_nm=args.cpa_nm,
                    registry_cache=cache, local_priority_s=args.local_priority_s,
                    predict_stale_s=args.predict_stale)

    # Session logger — records every received wire line for later replay.
    recorder = None
    if args.log:
        log_path = args.log_file or default_log_path(args.log_dir)
        try:
            recorder = Recorder(log_path, meta={
                'uat': args.uat,
                'dump1090_host': args.dump1090_host,
                'dump1090_port': (30002 if args.avr and args.dump1090_port == 30003
                                  else args.dump1090_port),
                'uat_host': args.uat_host,
                'uat_json_port': args.uat_json_port,
                'avr': args.avr,
            })
            print(f'Logging session to {log_path}')
        except OSError as e:
            print(f'WARNING: could not open log file {log_path}: {e} — '
                  f'continuing without logging.')
            recorder = None

    if args.avr:
        port = 30002 if args.dump1090_port == 30003 else args.dump1090_port
    else:
        port = args.dump1090_port

    uat = uat_launcher = None
    # --- Resolve which physical dongle each decoder should open ----------
    # Only matters when we're auto-launching a decoder locally AND the user
    # didn't pin the device by hand. Enumerate once, up front, before any
    # decoder grabs a dongle (rtl_test momentarily opens device 0).
    adsb_device = args.adsb_device
    uat_device  = args.uat_device
    need_detect = (
        (args.launch_dump1090 and adsb_device is None)
        or (args.uat and args.launch_dump978 and uat_device is None)
    )
    if need_detect:
        dongles = sdr_detect.enumerate_dongles()
        # Auto-select per band only when the choice is otherwise ambiguous
        # (more than one dongle present). With a single dongle, leave the
        # selector unset and let each decoder open the sole device — but note
        # a single dongle can't do both bands at once.
        if len(dongles) > 1:
            if adsb_device is None:
                d = sdr_detect.pick_for_band('1090', dongles)
                if d is not None:
                    adsb_device = d.device_arg()
            if args.uat and uat_device is None:
                d = sdr_detect.pick_for_band('978', dongles)
                if d is not None:
                    uat_device = d.soapy_device()
            if args.uat and (adsb_device is None or uat_device is None):
                print('WARNING: multiple RTL-SDR dongles found but could not '
                      'match one to each band by serial. Set --adsb-device '
                      'and/or --uat-device explicitly (see `rtl_test`), or '
                      'label dongles with `rtl_eeprom -s 1090`/`-s 978`.')
        elif args.uat and args.launch_dump978 and args.launch_dump1090:
            # <2 dongles but both decoders will auto-launch — they'll fight
            # over the one radio (978 and 1090 can't share a dongle).
            print(f'WARNING: --uat needs a second RTL-SDR (found {len(dongles)}). '
                  f'1090 and 978 cannot share one dongle; expect the second '
                  f'decoder to fail to open the device.')

    launcher = None
    if args.launch_dump1090:
        launcher = Dump1090Launcher(args.dump1090_host, port,
                                    binary=args.dump1090_binary,
                                    device=adsb_device)
        launcher.start()
        engine.report_feeder('dump1090', launcher.status)

    if args.avr:
        adsb = AvrFeeder(engine, args.dump1090_host, port, recorder=recorder)
    else:
        adsb = SbsFeeder(engine, args.dump1090_host, port, recorder=recorder)
    reg  = RegistryClient(engine, args.govt_data_url,
                          config.GOVT_DATA_USER, config.GOVT_DATA_PASS)

    adsb.start()
    reg.start()

    # --- 978 MHz UAT (optional) ------------------------------------------
    if args.uat:
        if args.launch_dump978:
            uat_launcher = Dump978Launcher(args.uat_host, args.uat_json_port,
                                           device=uat_device, gain=args.uat_gain)
            uat_launcher.start()
            engine.report_feeder('dump978', uat_launcher.status)
        uat = UatFeeder(engine, args.uat_host, args.uat_json_port,
                        recorder=recorder)
        uat.start()

    facilities = None
    if facility_cache is not None:
        facilities = FacilitiesClient(
            args.govt_data_url, config.GOVT_DATA_USER, config.GOVT_DATA_PASS,
            cache=facility_cache,
            ttl_s=args.cache_ttl_days * 86400.0)
        facilities.attach_observer(engine.get_observer_position)
        # Background bridge: whenever the client refreshes, push the snapshot
        # into the engine. Cheaper than the engine polling.
        def bridge():
            import time as _t
            while not facilities._stop.is_set():
                snap = facilities.snapshot()
                if snap is not None:
                    engine.set_facilities(snap)
                    engine.report_feeder('facilities', facilities.status)
                else:
                    engine.report_feeder('facilities', facilities.status or 'starting')
                _t.sleep(2.0)
        threading.Thread(target=bridge, daemon=True, name='fac-bridge').start()
        facilities.start()

    # The startup centre is also HOME — the magic code the web UI offers to
    # get back to whatever this instance was launched pointing at. Its
    # elevation rides along because AGL readouts are centre-relative: HOME
    # without its field elevation would report MSL and call it AGL.
    if args.airport:
        try:
            fix = lookup_airport(
                args.govt_data_url, config.GOVT_DATA_USER,
                config.GOVT_DATA_PASS, args.airport)
        except AirportLookupError as e:
            p.error(f'--airport {args.airport!r}: {e}')
        engine.set_home(fix.lat, fix.lon, fix.elev_ft)
        engine.set_center(fix.lat, fix.lon, fix.elev_ft, label=fix.ident)
        print(f'Radar centered on {fix.ident} ({fix.name}): '
              f'{fix.lat:.5f}, {fix.lon:.5f}, {fix.elev_ft:.0f} ft')
        if not fix.has_elevation:
            print(f'NOTE: {fix.ident} has no field elevation in govt-data; '
                  f'AGL readings will equal MSL until you centre elsewhere.')
    elif args.fixed_lat is not None and args.fixed_lon is not None:
        engine.set_home(args.fixed_lat, args.fixed_lon, args.fixed_alt_ft)
        engine.set_center(args.fixed_lat, args.fixed_lon, args.fixed_alt_ft,
                          label='HOME')
    else:
        # Under gpsd there is no fixed HOME — the receiver's position *is*
        # home — so no set_home() call. CenterControl reads that absence and
        # makes 'HOME' mean "hand the centre back to GPS".
        engine.set_gps_available(True)
        gps = GpsFeeder(engine, args.gpsd_host, args.gpsd_port,
                        recorder=recorder)
        gps.start()

    # --- Internet ADS-B feeders (optional) -------------------------------
    # In web mode, gate the feeders on whether any browser is connected so we
    # pull one shared internet stream when someone's watching and none when
    # nobody is. The curses UI has no such notion, so its feeders always poll.
    viewer_gate = ui_web.ViewerGate() if args.web else None
    net_feeders = []
    if args.internet:
        # airplanes_live is DISABLED BY DEFAULT, not removed.
        #
        # Since 2026-08-29 api.airplanes.live answers 403 to everything,
        # including /ping.  Verified from three unrelated public IPs -- the
        # office Starlink, Hetzner Oregon and Linode Stockholm -- so this is
        # the service refusing everyone or now requiring auth, NOT this site
        # being rate-limited or blacklisted.  Nothing here can fix it.
        #
        # Leaving it enabled costs a failing request every couple of minutes
        # and puts a permanent red error in the feeder status, which buries
        # real problems.  The driver code is untouched: re-enable with
        #     --internet-source adsb_lol --internet-source airplanes_live
        # and if the service comes back, restore it to this default list.
        sources = args.internet_source or ['adsb_lol']
        unknown = [s for s in sources if s not in INTERNET_SOURCES]
        if unknown:
            p.error(f'unknown --internet-source {unknown}; choose from '
                    f'{", ".join(INTERNET_SOURCES)}')
        for src in sources:
            f = InternetFeeder(engine, src, engine.get_observer_position,
                               radius_nm=args.internet_radius_nm,
                               recorder=recorder,
                               should_poll=(viewer_gate.active
                                            if viewer_gate else None))
            f.start()
            net_feeders.append(f)
        gated = ' (gated on web viewers)' if viewer_gate else ''
        print(f'Internet ADS-B enabled: {", ".join(sources)} '
              f'(radius {args.internet_radius_nm:g} NM, '
              f'local priority {engine.local_priority_s:g}s){gated}')

    audio = None
    if args.audio_flag:
        audio = AudioAlerter(engine, lead_time_s=args.audio_lead_s)
        audio.start()

    # --- Optional KML/KMZ overlay (web UI only) --------------------------
    overlay = None
    if args.kml:
        try:
            overlay = kml_loader.load(args.kml_file)
            print(f"Loaded KML overlay {args.kml_file}: "
                  f"{len(overlay['polygons'])} polygons, "
                  f"{len(overlay['lines'])} lines, "
                  f"{len(overlay['points'])} points")
        except Exception as e:
            print(f"WARNING: could not load KML overlay {args.kml_file}: {e} — "
                  f"continuing without it.")
            overlay = None
        if not args.web:
            print("NOTE: --kml only affects the web UI (--web); the curses UI "
                  "does not draw overlays.")

    # --- Web scope re-centring ------------------------------------------
    # The resolver is injected so ui_web keeps knowing nothing about
    # govt-data, the same reason the facilities bridge lives here. on_change
    # nudges the facilities client so the newly chosen airport gets its
    # airports/runways at once instead of at the next 60 s poll.
    center_control = None
    if args.web:
        center_control = ui_web.CenterControl(
            engine,
            resolver=lambda code: lookup_airport(
                args.govt_data_url, config.GOVT_DATA_USER,
                config.GOVT_DATA_PASS, code),
            on_change=(facilities.wake if facilities is not None else None),
            allow=args.web_recenter,
            error_cls=AirportLookupError)
        if not args.web_recenter:
            print('Web scope re-centring disabled (--no-web-recenter).')

    try:
        if args.web:
            ui_web.run(engine, args.refresh_hz, port=args.web_port,
                       http_port=args.http_port, overlay=overlay,
                       viewer_gate=viewer_gate, center_control=center_control)
        else:
            ui_curses.run(engine, args.refresh_hz)
    finally:
        adsb.stop()
        reg.stop()
        if uat:
            uat.stop()
        for f in net_feeders:
            f.stop()
        if facilities:
            facilities.stop()
        if audio:
            audio.stop()
        if cache:
            cache.flush()
        if facility_cache:
            facility_cache.flush()
        if launcher:
            launcher.stop()
        if uat_launcher:
            uat_launcher.stop()
        if recorder:
            recorder.close()


if __name__ == '__main__':
    main()
