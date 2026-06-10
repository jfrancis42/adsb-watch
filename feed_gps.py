"""Read TPV reports from gpsd (port 2947) and push observer position to the
Engine. Spoken to gpsd directly via JSON so we don't pin a particular
gps3 / gpsd-py3 version."""
import json
import socket
import threading
import time

from engine import Engine


class GpsFeeder(threading.Thread):
    daemon = True

    def __init__(self, engine: Engine, host: str, port: int):
        super().__init__(name='gps-feeder')
        self.engine = engine
        self.host = host
        self.port = port
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self._loop_once()
            except Exception:
                time.sleep(2.0)

    def _loop_once(self):
        with socket.create_connection((self.host, self.port), timeout=10) as s:
            s.settimeout(5.0)
            s.sendall(b'?WATCH={"enable":true,"json":true}\n')
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
                    self._handle(line)

    def _handle(self, line: bytes):
        try:
            obj = json.loads(line)
        except ValueError:
            return
        if obj.get('class') != 'TPV':
            return
        # gpsd mode: 0/1 = no fix, 2 = 2D, 3 = 3D
        if obj.get('mode', 0) < 2:
            return
        lat = obj.get('lat')
        lon = obj.get('lon')
        if lat is None or lon is None:
            return
        alt_ft = (obj.get('altHAE') or obj.get('alt') or 0.0) * 3.28084
        self.engine.update_observer(lat, lon, alt_ft)
