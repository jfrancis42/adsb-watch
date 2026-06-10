"""ADS-B feeder. Two modes:

- SBS-1 (default, port 30003): dump1090's already-decoded BaseStation CSV.
  Almost always enabled by default in dump1090 / readsb. No pyModeS needed.
- AVR (port 30002): raw hex frames; we decode with pyModeS.

The UI calls engine.snapshot().feeders[name] to surface connection state.
"""
import csv
import io
import socket
import threading
import time

from engine import Engine


# ---------------------------------------------------------------------------
# SBS-1 / BaseStation feeder (port 30003)
# ---------------------------------------------------------------------------
# Field reference: http://woodair.net/sbs/article/barebones42_socket_data.htm
# MSG,<TT>,...,SessionID,AircraftID,HexIdent,FlightID,DateGen,TimeGen,
#     DateLog,TimeLog,Callsign,Altitude,GroundSpeed,Track,Lat,Lon,
#     VerticalRate,Squawk,Alert,Emergency,SPI,IsOnGround
SBS_F_HEX     = 4
SBS_F_CALL    = 10
SBS_F_ALT     = 11
SBS_F_SPD     = 12
SBS_F_TRK     = 13
SBS_F_LAT     = 14
SBS_F_LON     = 15
SBS_F_VRATE   = 16


class SbsFeeder(threading.Thread):
    """Read SBS-1 CSV from dump1090/readsb on port 30003."""
    daemon = True
    name_id = 'adsb-sbs'

    def __init__(self, engine: Engine, host: str, port: int):
        super().__init__(name=self.name_id)
        self.engine = engine
        self.host = host
        self.port = port
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
                f'connected {self.host}:{self.port} (SBS-1)')
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
                    self._handle(line.decode('ascii', 'ignore').strip())

    def _handle(self, line: str):
        if not line.startswith('MSG,'):
            return
        try:
            row = next(csv.reader(io.StringIO(line)))
        except Exception:
            return
        if len(row) < 17:
            return

        icao = (row[SBS_F_HEX] or '').strip()
        if not icao:
            return

        kw = {}
        def maybe(idx, key, cast):
            v = row[idx].strip() if idx < len(row) else ''
            if v == '':
                return
            try:
                kw[key] = cast(v)
            except ValueError:
                pass

        if SBS_F_CALL < len(row) and row[SBS_F_CALL].strip():
            kw['callsign'] = row[SBS_F_CALL].strip()
        maybe(SBS_F_ALT,   'alt_ft',     float)
        maybe(SBS_F_SPD,   'speed_kt',   float)
        maybe(SBS_F_TRK,   'course_deg', float)
        maybe(SBS_F_LAT,   'lat',        float)
        maybe(SBS_F_LON,   'lon',        float)
        maybe(SBS_F_VRATE, 'vrate_fpm',  float)

        self.engine.update_aircraft(icao, **kw)
        self.engine.bump_count(self.name_id)


# ---------------------------------------------------------------------------
# AVR feeder (port 30002) — raw hex via pyModeS
# ---------------------------------------------------------------------------

class AvrFeeder(threading.Thread):
    daemon = True
    name_id = 'adsb-avr'

    def __init__(self, engine: Engine, host: str, port: int):
        super().__init__(name=self.name_id)
        self.engine = engine
        self.host = host
        self.port = port
        self._stop = threading.Event()
        self._cpr: dict[str, dict] = {}

    def stop(self):
        self._stop.set()

    def run(self):
        import pyModeS  # imported here so SBS users don't need pyModeS installed
        self._pms = pyModeS
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
                f'connected {self.host}:{self.port} (AVR)')
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
                    self._handle_line(line.strip())

    def _handle_line(self, line: bytes):
        if not line:
            return
        if line.startswith(b'*') and line.endswith(b';'):
            msg = line[1:-1].decode('ascii', 'ignore')
        elif line.startswith(b'@') and line.endswith(b';'):
            msg = line[13:-1].decode('ascii', 'ignore')
        else:
            return
        if len(msg) not in (14, 28):
            return
        self._decode(msg)

    def _decode(self, msg: str):
        pms = self._pms
        try:
            if pms.df(msg) != 17 or pms.crc(msg) != 0:
                return
            icao = pms.adsb.icao(msg)
            tc = pms.adsb.typecode(msg)
        except Exception:
            return
        if not icao or tc is None:
            return
        self.engine.bump_count(self.name_id)

        if 1 <= tc <= 4:
            self.engine.update_aircraft(icao, callsign=pms.adsb.callsign(msg))
        elif 9 <= tc <= 22 and tc != 19:
            self._handle_position(icao, msg)
        elif tc == 19:
            v = pms.adsb.velocity(msg)
            if v:
                spd, trk, vr, _ = v
                self.engine.update_aircraft(icao, speed_kt=spd,
                                            course_deg=trk, vrate_fpm=vr)

    def _handle_position(self, icao: str, msg: str):
        pms = self._pms
        try:
            alt = pms.adsb.altitude(msg)
            oe  = pms.adsb.oe_flag(msg)
        except Exception:
            return
        slot = self._cpr.setdefault(icao, {})
        slot[oe] = (msg, time.time())
        if 0 in slot and 1 in slot:
            (m_e, t_e), (m_o, t_o) = slot[0], slot[1]
            if abs(t_e - t_o) <= 10.0:
                try:
                    pos = pms.adsb.position(m_e, m_o, t_e, t_o)
                except Exception:
                    pos = None
                if pos:
                    lat, lon = pos
                    self.engine.update_aircraft(icao, lat=lat, lon=lon, alt_ft=alt)
                    return
        self.engine.update_aircraft(icao, alt_ft=alt)


# Back-compat alias for callers that imported the old name.
AdsbFeeder = SbsFeeder
