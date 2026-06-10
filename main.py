#!/usr/bin/env python3
"""Wire the engine, the three feeders, and the curses UI together.

By default we also auto-launch dump1090/readsb in the background — turn that
off with --no-launch-dump1090 if you have one already running under systemd
(we'll detect that anyway by checking the port first).
"""
import argparse

import config
import os
from airports import FacilitiesClient
from cache import RegistryCache
from engine import Engine
from feed_adsb import SbsFeeder, AvrFeeder
from feed_gps import GpsFeeder
from launcher import Dump1090Launcher
from registry import RegistryClient
import threading
import ui_curses


def main():
    p = argparse.ArgumentParser(description='ADS-B watcher with FAA registry overlay')
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
    p.add_argument('--cache-path',    default=config.REGISTRY_CACHE_PATH,
                   help='On-disk JSON cache for FAA registry lookups.')
    p.add_argument('--cache-ttl-days', type=float,
                   default=config.REGISTRY_CACHE_TTL_S / 86400.0,
                   help='How long cached registry entries are reused (default 7).')
    p.add_argument('--no-cache',      action='store_true',
                   help='Disable the on-disk registry cache for this run.')
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
                    registry_cache=cache)

    if args.avr:
        port = 30002 if args.dump1090_port == 30003 else args.dump1090_port
    else:
        port = args.dump1090_port

    launcher = None
    if args.launch_dump1090:
        launcher = Dump1090Launcher(args.dump1090_host, port,
                                    binary=args.dump1090_binary)
        launcher.start()
        engine.report_feeder('dump1090', launcher.status)

    if args.avr:
        adsb = AvrFeeder(engine, args.dump1090_host, port)
    else:
        adsb = SbsFeeder(engine, args.dump1090_host, port)
    reg  = RegistryClient(engine, args.govt_data_url,
                          config.GOVT_DATA_USER, config.GOVT_DATA_PASS)

    adsb.start()
    reg.start()

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

    if args.fixed_lat is not None and args.fixed_lon is not None:
        engine.update_observer(args.fixed_lat, args.fixed_lon,
                               args.fixed_alt_ft, manual=True)
    else:
        gps = GpsFeeder(engine, args.gpsd_host, args.gpsd_port)
        gps.start()

    try:
        ui_curses.run(engine, args.refresh_hz)
    finally:
        adsb.stop()
        reg.stop()
        if facilities:
            facilities.stop()
        if cache:
            cache.flush()
        if facility_cache:
            facility_cache.flush()
        if launcher:
            launcher.stop()


if __name__ == '__main__':
    main()
