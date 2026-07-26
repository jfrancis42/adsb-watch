"""Launch dump978-fa as a child process and tear it down on exit.

978 MHz UAT needs its own decoder (dump978-fa) and — because a single RTL-SDR
can't cover both 1090 and 978 at once — its own dongle. dump978-fa reads the
SDR through SoapySDR and can re-serve decoded traffic as newline-delimited
JSON on a TCP port, which feed_uat.UatFeeder consumes.

Device selection (the two-dongle problem)
------------------------------------------
With more than one RTL-SDR plugged in, you must tell each decoder which dongle
to use, or dump978 and dump1090/readsb will race for the same one. dump978
takes a SoapySDR device string via `--sdr`; pass it through `device=`:

    device='driver=rtlsdr,rtl=1'                 # by enumeration index
    device='driver=rtlsdr,serial=00000978'       # by dongle serial number

List indices/serials with `rtl_test` or `SoapySDRUtil --find`. Renumber a
dongle's serial with `rtl_eeprom -s <serial>` if you want stable names. If only
one dongle is present, the default `driver=rtlsdr` picks it.

Skips launching if something is already serving on the target JSON port — so a
systemd-managed dump978 is left alone (point --uat-host/--uat-json-port at it
and pass --no-launch-dump978).
"""
import os
import shutil
import signal
import socket
import subprocess
import time


BINARY = 'dump978-fa'


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Dump978Launcher:
    """Spawn dump978-fa if needed and reap it on stop().

    Use as a context manager OR call start()/stop() yourself.
    """

    def __init__(self, host: str, json_port: int, *,
                 device: str | None = None,
                 binary: str | None = None,
                 gain: str | None = None,
                 extra_args: list[str] | None = None,
                 wait_seconds: float = 8.0):
        self.host = host
        self.json_port = json_port
        # SoapySDR device string. None → let dump978 pick the sole/default
        # dongle (fine when only one is plugged in). main.py resolves this to
        # a serial/index-pinned string when multiple dongles are present.
        self.device = device or 'driver=rtlsdr'
        self.binary = binary or BINARY
        self.gain = gain
        self.extra_args = extra_args or []
        self.wait_seconds = wait_seconds
        self.proc: subprocess.Popen | None = None
        self.status = 'idle'
        self.command: list[str] | None = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        # Only auto-launch locally; a remote dump978 must be started there.
        if self.host not in ('127.0.0.1', 'localhost', '::1'):
            self.status = 'remote host — not auto-launching'
            return
        if is_port_open(self.host, self.json_port):
            self.status = f'port {self.json_port} already in use — not launching'
            return

        path = shutil.which(self.binary) or self.binary
        if not shutil.which(self.binary):
            self.status = (f'no {self.binary} on PATH — build/install dump978-fa '
                           f'(FlightAware/dump978)')
            return

        # `--json-port PORT` binds all interfaces when given a bare port.
        cmd = [path, '--sdr', self.device]
        if self.gain is not None:
            cmd += ['--sdr-gain', str(self.gain)]
        else:
            cmd += ['--sdr-auto-gain']
        cmd += ['--json-port', str(self.json_port)]
        cmd += self.extra_args
        self.command = cmd
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            self.status = f'launch failed: {e}'
            return

        self.status = f'starting {self.binary} (pid {self.proc.pid})'
        deadline = time.time() + self.wait_seconds
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.status = (f'{self.binary} exited rc={self.proc.returncode} '
                               f'(978 dongle busy or wrong device string? '
                               f'check --uat-device)')
                self.proc = None
                return
            if is_port_open(self.host, self.json_port):
                self.status = f'launched {self.binary} (pid {self.proc.pid})'
                return
            time.sleep(0.25)
        self.status = (f'{self.binary} pid {self.proc.pid} did not open port '
                       f'{self.json_port} within {self.wait_seconds:.0f}s')

    def stop(self):
        if not self.proc:
            return
        proc = self.proc
        self.proc = None
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait(timeout=2.0)
        except Exception:
            pass
        self.status = 'stopped'
