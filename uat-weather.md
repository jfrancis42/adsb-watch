# 978 MHz UAT weather (FIS-B) — notes and options

Scoping notes for a possible future project: getting **weather** (not just
traffic) out of the 978 MHz UAT datalink. Nothing here is built yet — this is
a survey of what's involved and what it could be used for.

## The catch: the current path can't see weather

The `--json-port` feed that `feed_uat.UatFeeder` consumes only emits
`DOWNLINK_SHORT` / `DOWNLINK_LONG` frames — i.e. **aircraft traffic only**.
FIS-B weather rides on **uplink** frames from ground stations, which that port
deliberately drops. (Extracted from dump978's `socket_output.cc`
`JsonOutput::InternalWrite` — it filters to downlink message types.)

So the traffic feeder gives you **zero weather by design**. Getting weather
needs a *different* extraction path:

1. Run dump978 with `--raw-port` (or a second instance) to get the raw uplink
   frames, then
2. Feed those into a **separate FIS-B application-layer decoder**.

Important distinction: `dump978-fa` decodes the UAT **frames** but does **not**
decode FIS-B **products** (NEXRAD bitmaps, text-product APDUs, TFR geometry,
etc.). That product layer is a whole separate piece of software.

> **Uncertain / needs verification:** there are open-source FIS-B product
> decoders in the `fisb-978` / `fisb-decode` family, but I'm not confident of
> the exact names or their current maintenance state. **Step one of any real
> effort is to research the current state of open-source FIS-B decoders** and
> pick one to build on (or decide to write one). Don't assume any specific
> package exists until checked.

## Reception reality

(Inferred from RF geometry, but solid.) On the ground you may hear **FIS-B more
reliably than traffic**:

- FIS-B ground stations transmit **continuously on a fixed schedule** from
  towers — a steady signal if you're within line-of-sight (near a decent-sized
  airport helps).
- Aircraft traffic downlinks are **sparse and low-flying**, especially at night.
- Range is LOS-limited, so a rooftop antenna matters.

## What FIS-B carries

(Well-established US FIS-B product set. All free, broadcast, received
over-the-air with **no internet**.)

- **NEXRAD precipitation** — regional (hi-res, ~2.5 min updates) and CONUS
  (lo-res national mosaic, ~15 min). The visually compelling one.
- **METARs / TAFs** — surface obs and terminal forecasts (text).
- **PIREPs**, **winds & temps aloft**.
- **NOTAMs**, including **TFRs** (with graphical boundaries), FDC/D-NOTAMs.
- **SIGMETs / AIRMETs / G-AIRMET** — convective, icing, turbulence graphics.
- **SUA status** — special-use airspace active/inactive.
- Cloud tops, lightning (product-dependent).

## What you could do with it (tailored to this stack)

1. **NEXRAD overlay on `maps.n0gq.org` or the adsb-watch radar scope.**
   Already have MapLibre GL + PMTiles and a Canvas radar. Decoded NEXRAD is a
   georeferenced bitmap mosaic — drop it as a raster layer under the aircraft.
   RF-sourced weather radar behind your own traffic display.

2. **Publish weather to the MQTT bus** (`10.1.0.20:1883`). METARs / TAFs /
   PIREPs / winds-aloft are compact text products — decode and publish as
   retained topics (`wx/metar/<station>`, etc.). Any app subscribes. Fits the
   rf-bench pub/sub pattern exactly.

3. **Offline / RF-only weather resilience.** Strongest fit with the skynet
   ethos — a national weather picture pulled from the air with the internet
   unplugged. NEXRAD + METARs + TFRs from a rooftop antenna.

4. **TFR / SIGMET / SUA situational display.** Graphical airspace and hazard
   boundaries overlaid on the maps — the "what's restricted / where's the
   convection" view.

5. **Ground-station reception logging & coverage study.** Log which FIS-B
   towers are heard and their transmit schedules over time — same
   "what's actually audible at the antenna" analysis already done for APRS
   (heard-locally-vs-gated) and ADS-B. A tropo-ducting / propagation angle
   falls out of this too.

## Honest scoping

Weather is a **meaningfully bigger** project than the traffic feeder.

- Traffic was a clean win because dump978 hands you decoded JSON.
- FIS-B decoding — especially **NEXRAD bitmap reassembly** and APDU/segment
  reassembly across frames — is real work, and depends on finding a decoder
  worth building on (or writing one).
- **Text products (METAR / TAF / NOTAM) are the low-effort entry point.**
- **NEXRAD is the high-value, high-effort one.**

## Suggested first step

Research the current state of open-source FIS-B decoders (maintenance, license,
input format expected — raw UAT frames vs. something else) before committing to
a design. Everything downstream (MQTT topics, map layers) is easy once there's
a trustworthy product decoder feeding structured output.
