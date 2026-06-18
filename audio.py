"""Audio alerter — rings the speakers when an aircraft is predicted to
pass close to the observer.

Runs in its own thread, polls `Engine.snapshot()`, and fires a
"ding ding ding" once per qualifying close approach. The alert latches per
ICAO so a single pass produces one ring; the latch clears as soon as the
engine stops predicting an approach for that aircraft (CPA passed, plane
moved away, or it left coverage), so a return pass will ring again.

Trigger condition (per snapshot tick):
    cpa_nm     <= engine.cpa_threshold_nm   (same threshold the UI highlights)
    cpa_seconds in (0, lead_time_s]         (default lead_time_s = 60)

This module is UI-agnostic — `main.py` wires it in behind `--audio-flag`,
and the engine remains unaware that it exists.
"""
import os
import shutil
import subprocess
import sys
import threading
import time


_PLAYERS = [
    ('paplay',),
    ('pw-play',),
    ('aplay', '-q'),
    ('play', '-q'),
]

_SOUND_CANDIDATES = [
    '/usr/share/sounds/freedesktop/stereo/bell.oga',
    '/usr/share/sounds/freedesktop/stereo/message-new-instant.oga',
    '/usr/share/sounds/alsa/Front_Center.wav',
]

DING_COUNT = 3
DING_GAP_S = 0.15


def _pick_player():
    for entry in _PLAYERS:
        if shutil.which(entry[0]) is not None:
            return list(entry)
    return None


def _pick_sound():
    for p in _SOUND_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


class AudioAlerter(threading.Thread):
    def __init__(self, engine, lead_time_s: float = 60.0, poll_hz: float = 4.0):
        super().__init__(daemon=True, name='audio-alert')
        self.engine = engine
        self.lead_time_s = lead_time_s
        self._delay = 1.0 / max(0.5, poll_hz)
        self._stop = threading.Event()
        self._alerted: set[str] = set()
        self._player = _pick_player()
        self._sound = _pick_sound()
        if self._player is None:
            self.status = 'no audio player found'
        elif self._sound is None:
            self.status = 'no sound file found'
        else:
            self.status = 'armed'
        engine.report_feeder('audio', self.status)

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self._delay):
            try:
                snap = self.engine.snapshot()
            except Exception:
                continue
            threshold = snap.cpa_threshold_nm
            for t in snap.tracks:
                if (t.cpa_nm is not None and t.cpa_seconds is not None
                        and t.cpa_nm <= threshold
                        and 0 < t.cpa_seconds <= self.lead_time_s):
                    if t.icao not in self._alerted:
                        self._alerted.add(t.icao)
                        self.engine.bump_count('audio', 1)
                        self._ding()
            # Drop latch for any aircraft no longer being predicted to
            # approach (cpa cleared by Engine when past CPA / diverging /
            # gone). A subsequent approach will re-arm and re-fire.
            still_approaching = {t.icao for t in snap.tracks
                                 if t.cpa_nm is not None}
            self._alerted &= still_approaching

    def _ding(self):
        threading.Thread(target=self._play_sequence, daemon=True).start()

    def _play_sequence(self):
        for i in range(DING_COUNT):
            self._play_once()
            if i < DING_COUNT - 1:
                time.sleep(DING_GAP_S)

    def _play_once(self):
        if self._player is not None and self._sound is not None:
            try:
                subprocess.run(
                    [*self._player, self._sound],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0)
                return
            except Exception:
                pass
        sys.stdout.write('\a')
        sys.stdout.flush()
