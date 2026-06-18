"""Launch dump1090 / readsb as a child process and tear it down on exit.

Skips launching if something is already serving SBS-1 on the target port —
that way we don't fight a systemd-managed dump1090 instance. Tries known
binaries in order; first one found wins.
"""
import os
import shutil
import signal
import socket
import subprocess
import time


# Order matters: prefer modern fork first, fall back to vanilla dump1090.
#
# readsb's `--net` enables the network subsystem but does NOT open any TCP
# server ports unless you also specify the per-protocol port options. SBS-1
# output (port 30003 = AVR/BaseStation CSV, the format we consume) needs
# `--net-sbs-port=30003`. dump1090 forks open these ports by default with
# just `--net`.
CANDIDATES = [
    ('readsb',              ['--device-type=rtlsdr',    # readsb is net-only by default
                             '--gain=-10',              # AGC
                             '--net', '--quiet',
                             '--net-bind-address=0.0.0.0',
                             '--net-sbs-port=30003',    # SBS-1 (what we consume)
                             '--net-ro-port=30002',     # AVR raw output
                             '--net-bo-port=30005']),   # Beast output
    ('dump1090-fa',         ['--net', '--quiet']),
    ('dump1090-mutability', ['--net', '--quiet']),
    ('dump1090',            ['--net', '--quiet']),
]


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_binary() -> tuple[str, list[str]] | None:
    for binary, args in CANDIDATES:
        path = shutil.which(binary)
        if path:
            return path, args
    return None


class Dump1090Launcher:
    """Spawn dump1090/readsb if needed and reap it on stop().

    Use as a context manager OR call start()/stop() yourself.
    """

    def __init__(self, host: str, port: int, *,
                 binary: str | None = None,
                 extra_args: list[str] | None = None,
                 wait_seconds: float = 8.0):
        self.host = host
        self.port = port
        self.binary = binary
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
        # Don't start if the port already answers — likely managed by systemd.
        if self.host not in ('127.0.0.1', 'localhost', '::1'):
            self.status = 'remote host — not auto-launching'
            return
        if is_port_open(self.host, self.port):
            self.status = f'port {self.port} already in use — not launching'
            return

        if self.binary:
            path = shutil.which(self.binary) or self.binary
            args = []
        else:
            found = find_binary()
            if not found:
                self.status = ('no dump1090/readsb on PATH — '
                               'install dump1090-fa, readsb, or dump1090-mutability')
                return
            path, args = found

        cmd = [path] + args + self.extra_args
        self.command = cmd
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                # New process group so Ctrl-C in our terminal doesn't double-signal.
                start_new_session=True,
            )
        except OSError as e:
            self.status = f'launch failed: {e}'
            return

        self.status = f'starting {os.path.basename(path)} (pid {self.proc.pid})'
        deadline = time.time() + self.wait_seconds
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.status = (f'{os.path.basename(path)} exited '
                               f'rc={self.proc.returncode} (RTL-SDR busy? '
                               f'try `sudo systemctl stop dump1090*`)')
                self.proc = None
                return
            if is_port_open(self.host, self.port):
                self.status = f'launched {os.path.basename(path)} (pid {self.proc.pid})'
                return
            time.sleep(0.25)
        self.status = (f'{os.path.basename(path)} pid {self.proc.pid} did not '
                       f'open port {self.port} within {self.wait_seconds:.0f}s')

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
                # Kill the whole process group in case it spawned children.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait(timeout=2.0)
        except Exception:
            pass
        self.status = 'stopped'
