import os

DUMP1090_HOST = os.environ.get('DUMP1090_HOST', '127.0.0.1')
DUMP1090_PORT = int(os.environ.get('DUMP1090_PORT', '30003'))  # SBS-1 BaseStation CSV

GPSD_HOST = os.environ.get('GPSD_HOST', '127.0.0.1')
GPSD_PORT = int(os.environ.get('GPSD_PORT', '2947'))

# govt-data is reachable at https://data.n0gq.org but requires HTTP Basic
# auth. Set GOVT_DATA_USER and GOVT_DATA_PASS in your environment (or pass
# --govt-data-* flags) — credentials are not bundled with this code. Ask
# the maintainer for read-only access if you don't have a working pair.
GOVT_DATA_URL  = os.environ.get('GOVT_DATA_URL',  'https://data.n0gq.org')
GOVT_DATA_USER = os.environ.get('GOVT_DATA_USER', '')
GOVT_DATA_PASS = os.environ.get('GOVT_DATA_PASS', '')

EXPIRY_SECONDS   = float(os.environ.get('ADSB_EXPIRY',     '10.0'))
CPA_HIGHLIGHT_NM = float(os.environ.get('CPA_HIGHLIGHT_NM','1.0'))
REFRESH_HZ       = float(os.environ.get('REFRESH_HZ',      '5'))

# Persistent registry cache (FAA lookups via govt-data).
_default_cache_dir = os.environ.get(
    'XDG_CACHE_HOME', os.path.join(os.path.expanduser('~'), '.cache'))
REGISTRY_CACHE_PATH = os.environ.get(
    'ADSB_CACHE_PATH', os.path.join(_default_cache_dir, 'adsb-watch', 'registry.json'))
REGISTRY_CACHE_TTL_S = float(os.environ.get('ADSB_CACHE_TTL_S', str(7 * 24 * 3600)))
