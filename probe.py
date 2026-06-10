#!/usr/bin/env python3
"""Probe a dump1090/readsb host to see which feed ports are open and what
they emit. Run this if the curses UI shows zero tracks.

    python3 probe.py [host]                    # all known ports, host=127.0.0.1
    python3 probe.py other-box.local                 # remote host
    python3 probe.py other-box.local 30003 8080      # only the listed ports

Probes all ports concurrently with a short connect timeout, so a firewalled
host doesn't make the script wait one timeout per port serially.
"""
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

CONNECT_TIMEOUT = 1.5   # seconds — kept low so dropped packets don't stall us
SAMPLE_TIMEOUT  = 3.0   # seconds — long enough to see *something* on quiet feeds

KNOWN_PORTS = {
    30001: 'beast input',
    30002: 'AVR (raw hex *...;)',
    30003: 'SBS-1 (BaseStation CSV)',
    30004: 'beast input (alt)',
    30005: 'beast output',
    30104: 'beast output (alt)',
    8080:  'http (data/aircraft.json)',
}


def probe_port(host: str, port: int, label: str) -> str:
    try:
        s = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except OSError as e:
        return f'{port:>5}  {label:<26} CLOSED ({e.__class__.__name__}: {e})'
    s.settimeout(SAMPLE_TIMEOUT)
    sample = b''
    try:
        sample = s.recv(256)
    except socket.timeout:
        pass
    finally:
        s.close()

    if port == 8080:
        # data/aircraft.json sits behind HTTP; do a one-shot GET
        try:
            s = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
            s.settimeout(SAMPLE_TIMEOUT)
            s.sendall(b'GET /data/aircraft.json HTTP/1.0\r\nHost: probe\r\n\r\n')
            sample = s.recv(256)
            s.close()
        except OSError:
            pass

    snippet = sample[:80].decode('ascii', 'replace').replace('\n', ' ').replace('\r', '')
    if not snippet:
        return f'{port:>5}  {label:<26} OPEN   (silent — receiver may be idle)'
    return f'{port:>5}  {label:<26} OPEN   sample={snippet!r}'


def main():
    args = sys.argv[1:]
    host = args[0] if args else '127.0.0.1'
    if len(args) > 1:
        port_specs = [(int(p), KNOWN_PORTS.get(int(p), '?')) for p in args[1:]]
    else:
        port_specs = list(KNOWN_PORTS.items())

    print(f'probing {host} ({CONNECT_TIMEOUT}s connect, {SAMPLE_TIMEOUT}s sample)...')
    with ThreadPoolExecutor(max_workers=len(port_specs)) as ex:
        futures = [ex.submit(probe_port, host, p, label) for p, label in port_specs]
        for f in futures:
            print(f.result())


if __name__ == '__main__':
    main()
