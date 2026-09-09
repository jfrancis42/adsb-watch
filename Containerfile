# adsb-watch — ADS-B aggregator/tracker. Serves an HTTP UI on 8086 and a
# WebSocket feed on 8765; the adsb-web sidecar fronts both on :80 with the
# estate allow-list.
#
# No SDR here. This instance runs with --internet and --no-launch-dump1090,
# taking its data from online aggregators, so it needs no USB device and no
# privileged access. If a dongle is ever attached to this host, that becomes a
# --device passthrough and a different image; do not add privileges here
# speculatively.
FROM docker.io/library/python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends gcc libc6-dev; \
    pip install --no-cache-dir -r requirements.txt; \
    apt-get purge -y gcc libc6-dev; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

COPY *.py ./
COPY web ./web

# A REAL HOME, and it is load-bearing. config.py puts logs in
# ~/.local/share/adsb-watch and the aircraft cache in ~/.cache (XDG defaults),
# so a user created with --no-create-home makes the process die on startup with
# PermissionError: [Errno 13] Permission denied: '/home/adsb'. The VM ran as
# jfrancis and had all this for free.
#
# /home/adsb is also a volume mount point (see vars/podman_services.yml): the
# aircraft/registry cache is expensive to rebuild and persisted on the VM, so
# it persists here too rather than being lost on every restart. Podman seeds a
# new named volume from the image, which is why the ownership below matters.
RUN useradd --system --create-home --home-dir /home/adsb \
        --shell /usr/sbin/nologin --uid 1502 adsb \
    && mkdir -p /home/adsb/.cache /home/adsb/.local/share \
    && chown -R 1502:1502 /home/adsb
USER 1502
ENV HOME=/home/adsb

EXPOSE 8086 8765

# Flags copied from the VM's unit verbatim, including the fixed site position.
CMD ["python3", "main.py", \
     "--web", "--web-port", "8765", "--http-port", "8086", \
     "--no-launch-dump1090", "--kml", \
     "--fixed-lat", "39.3553696", "--fixed-lon", "-104.6729929", \
     "--fixed-alt-ft", "6750", \
     "--govt-data-url", "http://10.1.17.42:8091", \
     "--internet", "--expiry", "30", "--predict-stale", "8"]
