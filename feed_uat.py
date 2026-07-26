"""UAT (978 MHz) feeder.

978 MHz is the second US ADS-B datalink — the Universal Access Transceiver,
used mostly by low-altitude general-aviation aircraft. It carries three kinds
of data: aircraft *traffic* downlinks, TIS-B/ADS-R rebroadcast traffic, and
FIS-B (weather / NOTAMs). This feeder handles **traffic only** — which is all
dump978-fa's JSON port ever emits (it filters to DOWNLINK_SHORT/LONG frames),
so there's nothing to filter on our side.

Wire protocol: dump978-fa `--json-port` streams one decoded JSON object per
line (newline-delimited, no header). Schema (fields present only when known):

    {"address_qualifier": "adsb_icao",
     "address": "a1b2c3",
     "position": {"lat": 39.1, "lon": -104.8},
     "pressure_altitude": 8500, "geometric_altitude": 8600,
     "ground_speed": 120.0, "true_track": 271.3,
     "vertical_velocity_barometric": -640,
     "callsign": "N12345", ...}

Altitudes are feet, ground_speed knots, track degrees true, vertical velocity
fpm — the same units engine.update_aircraft() already expects, so the mapping
is direct. Address is the 24-bit ICAO hex for adsb_icao/tisb_icao qualifiers,
which merges cleanly with the 1090 MHz track table (a dual-link aircraft is
genuinely the same ICAO on both).

The UI calls engine.snapshot().feeders[name] / .counts[name] to surface
connection state and message rate, just like the ADS-B feeders.
"""
import json
import socket
import threading

from engine import Engine


# address_qualifier values (see dump978 uat_message.h AddressQualifier). We
# accept the ones that represent real traffic with a usable address. Non-ICAO
# track-file IDs (tisb_trackfile) are accepted too — they show as tracks but
# won't resolve against the FAA registry (govt-data 404s, cached as "unknown").
_TRAFFIC_QUALIFIERS = {
    'adsb_icao', 'adsb_other',
    'tisb_icao', 'tisb_trackfile',
    'adsr_other',
}


class UatFeeder(threading.Thread):
    """Read decoded UAT traffic JSON from dump978-fa on its --json-port."""
    daemon = True
    name_id = 'uat-978'

    def __init__(self, engine: Engine, host: str, port: int, recorder=None):
        super().__init__(name=self.name_id)
        self.engine = engine
        self.host = host
        self.port = port
        self.recorder = recorder
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self.engine.report_feeder(self.name_id,
                    f'connecting {self.host}:{self.port}')
                self._loop_once()
                self.engine.report_feeder(self.name_id, 'eof — reconnecting')
            except Exception as e:
                self.engine.report_feeder(self.name_id,
                    f'error: {type(e).__name__}: {e}')
            self._stop.wait(2.0)

    def _loop_once(self):
        with socket.create_connection((self.host, self.port), timeout=10) as s:
            s.settimeout(15.0)
            self.engine.report_feeder(self.name_id,
                f'connected {self.host}:{self.port} (UAT/978)')
            buf = b''
            while not self._stop.is_set():
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    text = line.decode('utf-8', 'ignore').strip()
                    if self.recorder is not None and text:
                        self.recorder.log(self.name_id, text)
                    self._handle(text)

    def _handle(self, line: str):
        if not line:
            return
        try:
            msg = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return

        qual = msg.get('address_qualifier')
        if qual is not None and qual not in _TRAFFIC_QUALIFIERS:
            return

        icao = (msg.get('address') or '').strip()
        if not icao:
            return

        kw = {}

        callsign = msg.get('callsign')
        if isinstance(callsign, str) and callsign.strip():
            kw['callsign'] = callsign.strip()

        pos = msg.get('position')
        if isinstance(pos, dict):
            lat, lon = pos.get('lat'), pos.get('lon')
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                kw['lat'] = float(lat)
                kw['lon'] = float(lon)

        # Prefer barometric altitude to match the 1090 SBS feed (which reports
        # pressure altitude); fall back to geometric when baro is absent.
        alt = msg.get('pressure_altitude')
        if alt is None:
            alt = msg.get('geometric_altitude')
        if isinstance(alt, (int, float)):
            kw['alt_ft'] = float(alt)

        spd = msg.get('ground_speed')
        if isinstance(spd, (int, float)):
            kw['speed_kt'] = float(spd)

        # true_track is the airborne course; on-ground frames may carry only a
        # heading instead. Use whichever is present, preferring true track.
        crs = msg.get('true_track')
        if crs is None:
            crs = msg.get('true_heading')
        if crs is None:
            crs = msg.get('magnetic_heading')
        if isinstance(crs, (int, float)):
            kw['course_deg'] = float(crs)

        vr = msg.get('vertical_velocity_barometric')
        if vr is None:
            vr = msg.get('vertical_velocity_geometric')
        if isinstance(vr, (int, float)):
            kw['vrate_fpm'] = float(vr)

        self.engine.update_aircraft(icao, **kw)
        self.engine.bump_count(self.name_id)
