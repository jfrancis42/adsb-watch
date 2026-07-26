"""Enumerate RTL-SDR dongles and pick one per band by serial-number convention.

The two-dongle problem: 1090 MHz (ADS-B) and 978 MHz (UAT) each need their own
RTL-SDR, and once more than one dongle is plugged in you must tell each decoder
which is which — otherwise dump1090/readsb and dump978 race for the same one.

Convention this module implements: a dongle whose USB serial contains the token
``978`` is the UAT dongle; one containing ``1090`` is the ADS-B dongle. The
NooElec units this project was developed against ship as ``stx:978:35`` and
``stx:1090:39``, so detection works out of the box. Any user can adopt the same
scheme by writing a matching serial with ``rtl_eeprom -s <serial>`` (e.g.
``rtl_eeprom -d 0 -s 1090`` / ``rtl_eeprom -d 1 -s 978``).

Detection is best-effort and always overridable — pass an explicit device
string (``--uat-device`` / ``--device``) and this module is bypassed entirely.

Enumeration prefers ``rtl_test`` (ships with librtlsdr, always present when you
have an RTL-SDR) and falls back to ``SoapySDRUtil --find``. Callers should run
detection once at startup, before launching any decoder, since ``rtl_test``
briefly opens device 0 while listing.
"""
import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Dongle:
    index: int
    serial: str
    name: str = ''

    def soapy_device(self) -> str:
        """SoapySDR device string for dump978's --sdr. Serial is preferred
        (stable across replug/renumber); fall back to enumeration index."""
        if self.serial:
            return f'driver=rtlsdr,serial={self.serial}'
        return f'driver=rtlsdr,rtl={self.index}'

    def device_arg(self) -> str:
        """--device value for readsb / dump1090 (accepts index or serial)."""
        return self.serial or str(self.index)


# "  0:  NooElec, NESDR Nano 2, SN: stx:1090:39"
_RTL_TEST_LINE = re.compile(r'^\s*(\d+):\s*(.*?),\s*SN:\s*(\S+)\s*$')


def _enumerate_rtl_test() -> list[Dongle]:
    exe = shutil.which('rtl_test')
    if not exe:
        return []
    try:
        # rtl_test prints the device list on its first lines, then begins a
        # tuner test; a short timeout is plenty to capture the listing. It
        # opens device 0 momentarily — harmless as long as this runs before
        # any decoder is launched.
        proc = subprocess.run([exe, '-t'], capture_output=True, text=True,
                              timeout=4)
        text = (proc.stdout or '') + (proc.stderr or '')
    except (subprocess.TimeoutExpired, OSError) as e:
        text = getattr(e, 'stdout', '') or ''
        if isinstance(text, bytes):
            text = text.decode('utf-8', 'ignore')
    out = []
    for line in text.splitlines():
        m = _RTL_TEST_LINE.match(line)
        if m:
            out.append(Dongle(index=int(m.group(1)),
                              name=m.group(2).strip(),
                              serial=m.group(3).strip()))
    return out


def _enumerate_soapy() -> list[Dongle]:
    exe = shutil.which('SoapySDRUtil')
    if not exe:
        return []
    try:
        proc = subprocess.run([exe, '--find=driver=rtlsdr'],
                              capture_output=True, text=True, timeout=6)
        text = proc.stdout or ''
    except (subprocess.TimeoutExpired, OSError):
        return []
    out = []
    idx = serial = name = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('Found device'):
            if idx is not None:
                out.append(Dongle(index=idx, serial=serial or '', name=name or ''))
            try:
                idx = int(s.split()[-1])
            except ValueError:
                idx = 0
            serial = name = None
        elif s.startswith('serial'):
            serial = s.split('=', 1)[1].strip()
        elif s.startswith('product'):
            name = s.split('=', 1)[1].strip()
    if idx is not None:
        out.append(Dongle(index=idx, serial=serial or '', name=name or ''))
    return out


def enumerate_dongles() -> list[Dongle]:
    """Return the RTL-SDR dongles present, as (index, serial, name).

    Empty list means either no dongles or no enumeration tool available.
    """
    dongles = _enumerate_rtl_test()
    if not dongles:
        dongles = _enumerate_soapy()
    return dongles


def pick_for_band(band_token: str, dongles: list[Dongle]) -> Dongle | None:
    """Choose the dongle for a band by its serial-number token ('978'/'1090').

    Returns the single matching dongle, or None when there's no match or the
    match is ambiguous (more than one serial contains the token — the caller
    should then require an explicit device string).
    """
    matches = [d for d in dongles if band_token in d.serial]
    if len(matches) == 1:
        return matches[0]
    return None
