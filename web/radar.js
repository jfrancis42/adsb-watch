// ADS-B Radar Display with CRT Phosphor Effect
// WebSocket client that renders aircraft on a circular radar scope

const PHOSPHOR_GREEN = '#00ff00';
const PHOSPHOR_DIM = '#003300';
const BACKGROUND = '#000000';
const GRID_COLOR = '#00aa00';  // Brighter for visibility in sunlight
const TEXT_COLOR = '#00ff00';
const HIGHLIGHT_COLOR = '#00ff00';
const TRAIL_COLOR = '#006600';
// KML overlay drawn in amber so practice-area boundaries/labels read as a
// distinct layer from the green aircraft, trails, and airports.
// VFR charts are printed on cream/tan paper and are deliberately high-contrast
// in their OWN palette, so green phosphor and amber airspace laid over them
// disappear -- reported as "might as well not even be there". Two corrections,
// applied to the VFR view only:
//
//   SCRIM  dims the chart toward the dark basemap, where the same green was
//          already legible. It dims rather than desaturates, so every chart
//          colour keeps its meaning -- a magenta airway is still magenta.
//   HALO   a dark outline behind every stroke and glyph, so the symbology
//          stays readable even over the palest parts of the chart, and does
//          not depend on the scrim alone.
//
// Both are VFR-only: the dark vector basemap needs neither, and adding them
// there would dim a map the user already finds correct.
const VFR_SCRIM = 0.45;   // 0 = raw chart, 1 = black. Tune here.
const VFR_HALO = 'rgba(0, 0, 0, 0.95)';
const VFR_HALO_BLUR = 4;

// Aircraft data labels over MAP/VFR. #00ff00 is already the most saturated
// green a screen has, so "brighter" means more LUMINOUS: mix in white, and
// outline each glyph in near-black so it reads on the cream VFR chart and on
// the grey basemap alike. RADAR keeps the plain phosphor green.
const MAP_LABEL_COLOR = '#70ff70';
const MAP_LABEL_OUTLINE = 'rgba(0, 0, 0, 0.9)';
const LABEL_FONT_PX = 13;   // was 10; line spacing follows it

const OVERLAY_COLOR = '#ffb000';
const OVERLAY_FILL = 'rgba(255, 176, 0, 0.06)';
const OVERLAY_LINE = 'rgba(255, 176, 0, 0.8)';

class RadarDisplay {
    constructor(canvasId, wsUrl) {
        this.canvas = document.getElementById(canvasId);
        this.ctx = this.canvas.getContext('2d');
        this.wsUrl = wsUrl;
        this.ws = null;

        // Radar state
        this.observer = null;
        this.tracks = [];
        this.history = {};
        // icao -> {n_number, manufacturer, model, owner}.  Static per aircraft,
        // so the server sends it once and later frames omit it.
        this.registry = {};
        this.trailSeconds = 300.0;  // overwritten by the server's full frame
        this.facilities = null;
        this.overlay = null;  // Static KML overlay (polygons/lines/points), sent once on connect
        this.rangeNM = 5.0;

        // View mode: 'radar' (unchanged, the default), 'map' or 'vfr'.
        // RADAR must stay byte-for-byte what it was, so every map behaviour
        // below is gated on this rather than replacing existing code paths.
        this.viewMode = 'radar';
        this.mapLayer = document.getElementById('map-layer');
        this.map = null;             // the MapLibre instance, once loaded
        this.mapLibsPromise = null;  // libs are fetched on FIRST map use only
        this.trailSeconds = 30.0;
        this.projectionSeconds = 60.0;
        this.alertRangeNM = 1.0;

        // Altitude display mode: 'asl' or 'agl'
        this.altitudeMode = 'asl';

        // Sound effect settings
        this.soundApproaching = true;
        this.soundEnter = true;
        this.soundLeave = true;

        // Track aircraft states for sound triggers
        this.aircraftStates = {}; // {icao: {wasApproaching: bool, wasInRange: bool}}

        // Scope-centre control. The centre is server-side state (one engine
        // observer), so this is a request/echo pair, not local state: we send
        // a command and wait for the observer in the next snapshot to change.
        // Everything on screen -- which aircraft exist, which airports are
        // drawn, every distance and CPA -- is computed around that observer,
        // which is why the centre cannot just be a client-side pan.
        this.centerEnabled = false;   // server allows re-centring
        this.gpsAvailable = false;    // a gpsd feeder exists at all
        this.observerSource = 'unset';
        this.centerLabel = null;
        this.centerName = null;
        this.centerMsgTimer = null;
        // Suppress the chorus of enter/leave chimes that a centre jump would
        // otherwise fire: every distance changes at once, so every aircraft
        // "enters" or "leaves" in the same frame. Positions still update.
        this.suppressAlertsUntil = 0;

        // Animation state for phosphor persistence
        this.lastFrame = performance.now();
        this.fadeCanvas = document.createElement('canvas');
        this.fadeCtx = this.fadeCanvas.getContext('2d');

        // Canvas dimensions (must be after fadeCanvas creation)
        this.resize();
        window.addEventListener('resize', () => { this.resize(); this.updateMapView(); });

        const viewSel = document.getElementById('view-select');
        if (viewSel) {
            viewSel.addEventListener('change', (e) => this.setViewMode(e.target.value));
            // ?view=map | vfr | radar -- so a view can be bookmarked, and so
            // the map path can be exercised without a human clicking a menu.
            // Anything unrecognised is ignored and RADAR stands, which is the
            // documented default.
            const want = new URLSearchParams(window.location.search).get('view');
            if (want && ['radar', 'map', 'vfr'].includes(want.toLowerCase())) {
                viewSel.value = want.toLowerCase();
                this.setViewMode(viewSel.value);
            }
        }

        // Setup control event listeners
        document.getElementById('range-select').addEventListener('change', (e) => {
            this.rangeNM = parseFloat(e.target.value);
            this.resize();  // Recalculate pixels per NM
            this.updateMapView();   // the map's zoom IS the range ring
        });

        document.getElementById('trail-select').addEventListener('change', (e) => {
            const value = parseFloat(e.target.value);
            this.trailSeconds = value === -1 ? Infinity : value;
        });

        document.getElementById('projection-select').addEventListener('change', (e) => {
            this.projectionSeconds = parseFloat(e.target.value);
        });

        document.getElementById('alert-range-select').addEventListener('change', (e) => {
            this.alertRangeNM = parseFloat(e.target.value);
        });

        document.getElementById('altitude-mode-select').addEventListener('change', (e) => {
            this.altitudeMode = e.target.value;
        });

        document.getElementById('sound-approaching').addEventListener('change', (e) => {
            this.soundApproaching = e.target.checked;
        });

        document.getElementById('sound-enter').addEventListener('change', (e) => {
            this.soundEnter = e.target.checked;
        });

        document.getElementById('sound-leave').addEventListener('change', (e) => {
            this.soundLeave = e.target.checked;
        });

        // Centre selector: Enter applies, Escape restores the current centre.
        const centerInput = document.getElementById('center-input');
        centerInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                const code = centerInput.value.trim().toUpperCase();
                if (code) this.requestCenter(code);
            } else if (e.key === 'Escape') {
                centerInput.value = '';
                centerInput.blur();
            }
        });

        // Clicking the GPS lamp is how you take the centre away from gpsd --
        // and give it back. Dead when the instance has no receiver.
        document.getElementById('gps-indicator').addEventListener('click', () => {
            if (!this.gpsAvailable) {
                this.showCenterMessage(
                    'No GPS receiver on this instance.', true);
                return;
            }
            this.send({cmd: 'set_gps', enabled: this.observerSource !== 'gps'});
        });

        // Connect and start
        this.connect();
        this.animate();
    }

    resize() {
        const rect = this.canvas.getBoundingClientRect();
        this.canvas.width = rect.width;
        this.canvas.height = rect.height;
        this.fadeCanvas.width = rect.width;
        this.fadeCanvas.height = rect.height;

        // Center point and scale
        this.cx = this.canvas.width / 2;
        this.cy = this.canvas.height / 2;
        this.radius = Math.min(this.cx, this.cy) * 0.9;
        this.pixelsPerNM = this.radius / this.rangeNM;
        this.reportCoverage();
    }

    // How far (NM) this window can see. On RADAR that is the outer ring; over
    // a map the display runs to the window corners, so it is the half
    // diagonal. The server fetches aircraft and airports out to the widest
    // viewer's figure (capped), so the corners are not an empty map.
    coverageNM() {
        if (!this.pixelsPerNM) return this.rangeNM;
        if (this.viewMode === 'radar') return this.rangeNM;
        return Math.hypot(this.cx, this.cy) / this.pixelsPerNM;
    }

    // Debounced: a window drag fires dozens of resize events. `force` resends
    // after a reconnect, when the server has forgotten this client's figure.
    reportCoverage(force = false) {
        clearTimeout(this._coverageTimer);
        this._coverageTimer = setTimeout(() => {
            const nm = Math.ceil(this.coverageNM());
            if (!force && nm === this._coverageSent) return;
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
            try {
                this.ws.send(JSON.stringify({ cmd: 'set_coverage', radius_nm: nm }));
                this._coverageSent = nm;
            } catch (e) { /* next change or reconnect resends */ }
        }, force ? 0 : 250);
    }

    connect() {
        console.log('Connecting to', this.wsUrl);
        this.ws = new WebSocket(this.wsUrl);

        this.ws.onopen = () => {
            console.log('WebSocket connected');
            this.reportCoverage(true);
            // Connection status is now shown via indicator lights
        };

        this.ws.onclose = (event) => {
            console.log('WebSocket disconnected. Code:', event.code, 'Reason:', event.reason);
            // Connection status is now shown via indicator lights
            // Reconnect after 2 seconds
            setTimeout(() => this.connect(), 2000);
        };

        this.ws.onerror = (err) => {
            console.error('WebSocket error:', err);
            console.error('Failed to connect to:', this.wsUrl);
            console.error('Make sure the server is running: python3 main.py --web --fixed-lat 39.54 --fixed-lon -104.76 --fixed-alt-ft 5400');
        };

        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === 'snapshot') {
                this.handleSnapshot(data);
            } else if (data.type === 'overlay') {
                this.overlay = data.overlay;
            } else if (data.type === 'center_result') {
                this.handleCenterResult(data);
            }
        };
    }

    // Web Audio API context for sound generation
    getAudioContext() {
        if (!this.audioContext) {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
        }
        return this.audioContext;
    }

    // Pleasant ascending tone for "approaching"
    playApproachingSound() {
        if (!this.soundApproaching) return;
        const ctx = this.getAudioContext();
        const now = ctx.currentTime;

        const osc = ctx.createOscillator();
        const gain = ctx.createGain();

        osc.connect(gain);
        gain.connect(ctx.destination);

        // Rising tone: C5 -> E5 -> G5 (523Hz -> 659Hz -> 784Hz)
        osc.frequency.setValueAtTime(523, now);
        osc.frequency.linearRampToValueAtTime(659, now + 0.1);
        osc.frequency.linearRampToValueAtTime(784, now + 0.2);

        gain.gain.setValueAtTime(0.3, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.3);

        osc.start(now);
        osc.stop(now + 0.3);
    }

    // Pleasant chime for "entered range"
    playEnterSound() {
        if (!this.soundEnter) return;
        const ctx = this.getAudioContext();
        const now = ctx.currentTime;

        const osc = ctx.createOscillator();
        const gain = ctx.createGain();

        osc.connect(gain);
        gain.connect(ctx.destination);

        // Bright chime: G5 -> C6 (784Hz -> 1047Hz)
        osc.frequency.setValueAtTime(784, now);
        osc.frequency.linearRampToValueAtTime(1047, now + 0.15);

        gain.gain.setValueAtTime(0.4, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.4);

        osc.start(now);
        osc.stop(now + 0.4);
    }

    // Gentle descending tone for "left range"
    playLeaveSound() {
        if (!this.soundLeave) return;
        const ctx = this.getAudioContext();
        const now = ctx.currentTime;

        const osc = ctx.createOscillator();
        const gain = ctx.createGain();

        osc.connect(gain);
        gain.connect(ctx.destination);

        // Descending tone: G5 -> E5 -> C5 (784Hz -> 659Hz -> 523Hz)
        osc.frequency.setValueAtTime(784, now);
        osc.frequency.linearRampToValueAtTime(659, now + 0.1);
        osc.frequency.linearRampToValueAtTime(523, now + 0.2);

        gain.gain.setValueAtTime(0.3, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.3);

        osc.start(now);
        osc.stop(now + 0.3);
    }

    // ---- scope centre ------------------------------------------------

    send(obj) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(obj));
        } else {
            this.showCenterMessage('Not connected.', true);
        }
    }

    requestCenter(code) {
        this.send({cmd: 'set_center', airport: code});
    }

    handleCenterResult(data) {
        this.showCenterMessage(data.message, !data.ok);
        if (data.ok) {
            // Clear the box on success so it reads as "type the next one",
            // not as a stale entry. The applied centre is named in the
            // observer readout.
            document.getElementById('center-input').value = '';
        }
    }

    showCenterMessage(text, isError) {
        const el = document.getElementById('center-msg');
        if (!el || !text) return;
        el.textContent = text;
        el.classList.toggle('error', !!isError);
        el.classList.add('show');
        clearTimeout(this.centerMsgTimer);
        // Errors linger; a success confirmation has done its job quickly.
        this.centerMsgTimer = setTimeout(
            () => el.classList.remove('show'), isError ? 8000 : 4000);
    }

    // Reflect server-side centre state into the controls. Called every frame
    // because the centre is shared: another viewer can move it, and this
    // client finds out the same way it finds out about aircraft.
    updateCenterControls(center) {
        if (!center) return;   // a server older than this feature
        const box   = document.getElementById('center-control');
        const input = document.getElementById('center-input');
        const readout = document.getElementById('observer-pos');
        const gpsInd = document.getElementById('gps-indicator');
        const gpsLight = document.getElementById('gps-light');

        this.centerEnabled  = !!center.recenter_enabled;
        this.gpsAvailable   = !!center.gps_available;
        this.observerSource = center.source || 'unset';

        box.hidden = !this.centerEnabled;

        // GPS is the override: while it drives the centre, manual entry is
        // refused server-side, so the box is disabled rather than left to
        // look available and then bounce.
        const gpsDriving = this.observerSource === 'gps';
        input.disabled = gpsDriving;
        input.title = gpsDriving
            ? 'GPS is driving the centre — click the GPS indicator to turn it off.'
            : 'Airport code (KPAE, S43, DEN) or HOME. Press Enter.';

        const newLabel = center.label || null;
        // Don't label somebody else's coordinates "GPS": right after a
        // handback the position on screen is still the manual centre.
        this.centerName = gpsDriving
            ? (center.awaiting_fix ? 'GPS (no fix yet)' : 'GPS')
            : (newLabel || 'manual');
        if (newLabel !== this.centerLabel) {
            this.centerLabel = newLabel;
            readout.classList.add('flash');
            setTimeout(() => readout.classList.remove('flash'), 700);
        }

        // Three GPS states, kept distinguishable: driving / off but available
        // / no receiver here. A single on-off lamp would make "I turned it
        // off" and "there is nothing to turn on" look identical.
        gpsLight.classList.toggle('on', gpsDriving);
        gpsInd.classList.toggle('clickable', this.gpsAvailable);
        gpsInd.classList.toggle('unavailable', !this.gpsAvailable);
        gpsInd.title = !this.gpsAvailable
            ? 'No GPS receiver on this instance — the centre is set manually.'
            : (gpsDriving ? 'GPS is centring the scope. Click to take manual control.'
                          : 'Manual centring. Click to hand the centre back to GPS.');
    }

    handleSnapshot(data) {
        // A centre jump invalidates every distance at once. Note it before
        // overwriting this.observer so the alert suppression can see the move.
        if (this.observer && data.observer) {
            const dLat = data.observer.lat - this.observer.lat;
            const dLon = data.observer.lon - this.observer.lon;
            const nmLon = 60.0 * Math.cos(this.observer.lat * Math.PI / 180);
            // ~1 NM: bigger than any GPS wander, smaller than any re-centre.
            if (Math.hypot(dLat * 60.0, dLon * nmLon) > 1.0) {
                this.suppressAlertsUntil = Date.now() + 3000;
            }
        }
        const hadObserver = this.observer;
        this.observer = data.observer;
        // Whether the map needs to follow (a re-centre: GPS drift, a manual
        // centre, or the very first fix). ACTED ON AT THE END of this method,
        // not here -- see the note there. Recorded now, before this.observer
        // is used further down, purely so the comparison is against the old
        // value.
        const observerMoved = !hadObserver || !this.observer
            || hadObserver.lat !== this.observer.lat
            || hadObserver.lon !== this.observer.lon;

        this.tracks = data.tracks;

        // Re-attach registry metadata the server sent once and now omits.
        for (const t of this.tracks) {
            if (t.n_number !== undefined) {
                this.registry[t.icao] = {n_number: t.n_number, manufacturer: t.manufacturer,
                                         model: t.model, owner: t.owner};
            } else {
                const reg = this.registry[t.icao];
                if (reg) { t.n_number = reg.n_number; t.manufacturer = reg.manufacturer;
                           t.model = reg.model; t.owner = reg.owner; }
            }
        }

        // The server sends ONE full frame per connection and deltas after it.
        // Before this, every frame carried the complete history (591 KiB) and
        // facilities (62 KiB) three times a second -- 16.6 Mbit/s per viewer.
        if (data.full) {
            this.history = data.history || {};
            if (data.trail_seconds) this.trailSeconds = data.trail_seconds;
        } else {
            // Merge appended points.  Dedup by timestamp: a client that
            // connected mid-stream already holds points the first delta may
            // repeat, because the server's watermark predates its connect.
            if (data.history_delta) {
                for (const [icao, pts] of Object.entries(data.history_delta)) {
                    const trail = this.history[icao] || (this.history[icao] = []);
                    const lastTs = trail.length ? trail[trail.length - 1][0] : -Infinity;
                    for (const pt of pts) if (pt[0] > lastTs) trail.push(pt);
                }
            }
            // Aircraft the server has aged out; without this the trail is
            // kept forever, which a full snapshot used to prevent implicitly.
            if (data.history_purge) {
                for (const icao of data.history_purge) {
                    delete this.history[icao];
                    delete this.registry[icao];   // server re-announces if it returns
                }
            }
            // Trim locally to the same window the server uses, or trails grow
            // without bound now that nothing replaces them wholesale.
            if (this.trailSeconds) {
                const cutoff = (Date.now() / 1000) - this.trailSeconds;
                for (const icao of Object.keys(this.history)) {
                    const trail = this.history[icao];
                    let i = 0;
                    while (i < trail.length && trail[i][0] < cutoff) i++;
                    if (i) trail.splice(0, i);
                }
            }
        }
        // Absent on a delta frame -- keep what we have; only replace when sent.
        if (data.facilities !== undefined) this.facilities = data.facilities;

        // ORDER AND ISOLATION HERE ARE DELIBERATE.
        //
        // The readout is INFORMATION -- where you are, how many aircraft, how
        // many airports. Sound is decoration, and the audio path is the most
        // failure-prone thing on this page: a browser can refuse an
        // AudioContext outright when it has seen no user gesture. Running it
        // first meant one throw in the audio code took the readout with it,
        // and the failure was invisible in the obvious place: aircraft kept
        // drawing (this.tracks was already assigned above, and the render
        // loop is a separate call), so the display looked alive while the
        // position and counts silently stopped updating.
        //
        // So: the centre name first (the readout needs it), then the readout,
        // then everything optional -- each isolated, so no decoration can
        // take out the information again.
        try {
            this.updateCenterControls(data.center);
        } catch (err) {
            console.error('centre controls:', err);
        }

        if (this.observer) {
            // One readout, not two: the centre's name and its coordinates are
            // the same fact, and the status bar has no width to spare.
            const named = this.centerName ? `${this.centerName}  ` : '';
            document.getElementById('observer-pos').textContent =
                `Observer: ${named}${this.observer.lat.toFixed(4)}, ${this.observer.lon.toFixed(4)} @ ${Math.round(this.observer.alt_ft)} ft`;
        }

        const airportCount = this.facilities?.airports?.length || 0;
        document.getElementById('track-count').textContent = `Tracks: ${this.tracks.length} | Airports: ${airportCount}`;

        // Decoration, after the information and unable to harm it.
        try {
            this.checkSoundTriggers();
        } catch (err) {
            console.error('sound triggers:', err);
        }

        // Re-centre the map LAST, and in its own try/catch.
        //
        // This is the whole readout bug. It used to run near the TOP of this
        // method, before this.tracks was even assigned -- so any throw inside
        // it (MapLibre rejecting a jumpTo, a resize before the GL context is
        // ready) aborted the entire snapshot handler: no tracks, no readout,
        // and the display froze on its last good frame while looking alive.
        // The map is a view of the data; it must never be able to stop the
        // data being read.
        if (observerMoved) {
            try {
                this.updateMapView();
            } catch (err) {
                console.error('map view update:', err);
            }
        }

        // Update indicator lights
        const adsbLight = document.getElementById('adsb-light');
        const uatLight = document.getElementById('uat-light');

        // A feeder counts as "on" when connected / passing messages.
        const feederOn = (name) => {
            if (!data.feeders || !data.feeders[name]) return false;
            const s = data.feeders[name].toLowerCase();
            return s.includes('connected') || s.includes('msgs');
        };
        const setLight = (light, on) => {
            if (!light) return;
            light.classList.toggle('on', on);
        };

        // 1090 light: SBS-1 or AVR feeder connected.
        setLight(adsbLight, feederOn('adsb-sbs') || feederOn('adsb-avr'));

        // 978 light: UAT feeder connected. Off when --uat wasn't enabled
        // (no uat-978 feeder is ever reported).
        setLight(uatLight, feederOn('uat-978'));

        // The GPS lamp is driven by updateCenterControls(), not here. It no
        // longer means "we have a position" -- with --fixed-lat/--airport we
        // always have one and there is no receiver, so the old rule lit a
        // green GPS light on an instance with no GPS. It now means "gpsd is
        // driving the centre", which is the thing you click it to change.
    }

    checkSoundTriggers() {
        if (!this.tracks || this.alertRangeNM === 0) return;

        // Just after a centre change every range is different, so states are
        // still recorded -- otherwise the first real crossing after the move
        // would be missed -- but nothing is played.
        const quiet = Date.now() < this.suppressAlertsUntil;
        const currentIcaos = new Set();

        for (const track of this.tracks) {
            currentIcaos.add(track.icao);

            // Determine current state
            const isApproaching = track.cpa_nm !== null && track.cpa_nm <= this.alertRangeNM && track.closing;
            const isInRange = track.distance_nm !== null && track.distance_nm <= this.alertRangeNM;

            // Get previous state
            const prevState = this.aircraftStates[track.icao] || { wasApproaching: false, wasInRange: false };

            // Trigger sounds on state transitions
            if (!quiet) {
                if (isApproaching && !prevState.wasApproaching && !isInRange) {
                    // Just started approaching (not yet in range)
                    this.playApproachingSound();
                }

                if (isInRange && !prevState.wasInRange) {
                    // Just entered range
                    this.playEnterSound();
                }

                if (!isInRange && prevState.wasInRange) {
                    // Just left range
                    this.playLeaveSound();
                }
            }

            // Update state
            this.aircraftStates[track.icao] = {
                wasApproaching: isApproaching,
                wasInRange: isInRange
            };
        }

        // Clean up states for aircraft that are no longer visible
        for (const icao in this.aircraftStates) {
            if (!currentIcaos.has(icao)) {
                delete this.aircraftStates[icao];
            }
        }
    }

    animate() {
        requestAnimationFrame(() => this.animate());

        const now = performance.now();
        const dt = (now - this.lastFrame) / 1000.0;
        this.lastFrame = now;

        this.render(dt);
    }

    render(dt) {
        const overMap = this.viewMode !== 'radar';

        // PHOSPHOR PERSISTENCE IS RADAR-ONLY, and that is not an aesthetic
        // choice. The effect works by compositing the previous frame through
        // a semi-opaque BLACK fill; over a map those black pixels accumulate
        // as a haze that dims the tiles a little more every frame until the
        // map is gone. Trails are drawn explicitly from `this.history` either
        // way, so what is lost over a map is the glow, not the information.
        if (!overMap) {
            // Phosphor persistence effect: fade the previous frame
            this.ctx.drawImage(this.fadeCanvas, 0, 0);
            this.fadeCtx.fillStyle = 'rgba(0, 0, 0, 0.08)';  // Fade rate
            this.fadeCtx.fillRect(0, 0, this.fadeCanvas.width, this.fadeCanvas.height);
            this.fadeCtx.drawImage(this.canvas, 0, 0);

            // Clear current frame
            this.ctx.fillStyle = BACKGROUND;
            this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
        } else {
            // Transparent, so the map shows through. An opaque fill here is
            // what hides it -- the map element is behind the canvas, not
            // inside it.
            this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
            if (this.viewMode === 'vfr') {
                // Dim the chart toward the dark basemap -- the whole canvas,
                // because the chart now fills it rather than just the disc.
                this.ctx.fillStyle = `rgba(0, 0, 0, ${VFR_SCRIM})`;
                this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
            }
        }

        // The halo. Set once, here, so it applies to the grid, the overlay,
        // the airports, the trails and the aircraft without touching any of
        // their drawing code -- and is inherited through their own
        // save()/restore() pairs.
        if (this.viewMode === 'vfr') {
            this.ctx.shadowColor = VFR_HALO;
            this.ctx.shadowBlur = VFR_HALO_BLUR;
        } else {
            this.ctx.shadowColor = 'transparent';
            this.ctx.shadowBlur = 0;
        }

        // Draw persistent grid and labels (drawn every frame, not faded)
        this.drawGrid();

        // Draw trails and aircraft on the fade layer
        this.ctx.save();
        this.ctx.translate(this.cx, this.cy);

        if (this.observer) {
            // Draw KML overlay first (bottom-most layer, under airports)
            if (this.overlay) {
                this.drawOverlay(this.overlay);
            }

            // Draw airports and runways first (bottom layer)
            if (this.facilities && this.facilities.airports) {
                for (const airport of this.facilities.airports) {
                    this.drawAirport(airport);
                }
            }

            // Draw trails (middle layer)
            for (const track of this.tracks) {
                if (track.icao in this.history) {
                    this.drawTrail(track);
                }
            }

            // Draw aircraft (top layer)
            for (const track of this.tracks) {
                this.drawAircraft(track);
            }
        }

        this.ctx.restore();
    }

    // ── map views ────────────────────────────────────────────────────────
    //
    // RADAR is unchanged and costs nothing: none of this runs, and not one
    // byte is fetched from maps.n0gq.org, until a map view is selected.

    // The tile server. Same origin for the libraries and the archives, and it
    // sends Access-Control-Allow-Origin:* so this works from localhost:8080.
    static MAPS_BASE = 'https://maps.n0gq.org';

    ensureMapLibs() {
        // Loaded ONCE, lazily, and in this exact order -- each depends on the
        // one before it. Loading them in parallel appears to work and then
        // fails intermittently, because pmtiles.js registers itself against a
        // maplibregl that may not exist yet.
        if (this.mapLibsPromise) return this.mapLibsPromise;
        const base = RadarDisplay.MAPS_BASE + '/lib';
        const css = document.createElement('link');
        css.rel = 'stylesheet';
        css.href = base + '/maplibre-gl.css';
        document.head.appendChild(css);
        const load = (src) => new Promise((resolve, reject) => {
            const el = document.createElement('script');
            el.src = src;
            el.onload = resolve;
            el.onerror = () => reject(new Error('could not load ' + src));
            document.head.appendChild(el);
        });
        this.mapLibsPromise = load(base + '/maplibre-gl.js')
            .then(() => load(base + '/pmtiles.js'))
            .then(() => load(base + '/protomaps-themes-base.js'))
            .then(() => {
                const proto = new pmtiles.Protocol();
                maplibregl.addProtocol('pmtiles', proto.tile.bind(proto));
            });
        return this.mapLibsPromise;
    }

    setViewMode(mode) {
        this.viewMode = mode;
        this.reportCoverage();
        // The fade buffer is not written while a map view is up, so without
        // this the first frame back on RADAR composites a stale snapshot of
        // whatever was on screen when the map was selected.
        this.fadeCtx.clearRect(0, 0, this.fadeCanvas.width, this.fadeCanvas.height);
        if (mode === 'radar') {
            this.mapLayer.classList.remove('active');
            return;
        }
        this.mapLayer.classList.add('active');
        this.ensureMapLibs()
            .then(() => this.buildMap(mode))
            .catch((err) => {
                // Fall back rather than show a dead black disc: the radar is
                // the job, the map is decoration, and a tile server that is
                // unreachable (no VPN, no internet) must not cost the display.
                console.error('map unavailable:', err);
                this.viewMode = 'radar';
                this.reportCoverage();
                this.mapLayer.classList.remove('active');
                const sel = document.getElementById('view-select');
                if (sel) sel.value = 'radar';
            });
    }

    buildMap(mode) {
        const base = RadarDisplay.MAPS_BASE;
        // Rebuilt on a mode change rather than restyled: MAP is a VECTOR
        // source and VFR is two RASTER ones, so the sources differ, not just
        // the paint.
        if (this.map) { this.map.remove(); this.map = null; }

        const style = {
            version: 8,
            glyphs: base + '/basemaps-assets/fonts/{fontstack}/{range}.pbf',
            sprite: base + '/basemaps-assets/sprites/v4/dark',
            sources: {},
            layers: [],
        };

        if (mode === 'vfr') {
            // FAA sectionals, with the Terminal Area charts above them where
            // they exist -- TAC is transparent outside its metro footprint, so
            // it refines the busy areas and hides nothing elsewhere.
            //
            // No `maxzoom` on either source, deliberately: MapLibre over-zooms
            // a raster past its deepest level by scaling the last tile, so the
            // chart stays usable at ranges tighter than the FAA scanned.
            // Setting maxzoom would make it VANISH there instead, which at
            // 1 NM range is precisely when it is wanted.
            style.sources.sec = { type: 'raster', url: 'pmtiles://' + base + '/sec.pmtiles',
                                  attribution: 'VFR charts © FAA' };
            style.sources.tac = { type: 'raster', url: 'pmtiles://' + base + '/tac.pmtiles' };
            style.layers = [
                { id: 'sec', type: 'raster', source: 'sec' },
                { id: 'tac', type: 'raster', source: 'tac' },
            ];
        } else {
            style.sources.protomaps = {
                type: 'vector',
                url: 'pmtiles://' + base + '/usa.pmtiles',
                attribution: '© OpenStreetMap',
            };
            // Dark, not light: this sits under a green phosphor scope, and a
            // white basemap makes every aircraft and range ring unreadable.
            style.layers = protomaps_themes_base.default('protomaps', 'dark');
        }

        this.map = new maplibregl.Map({
            container: this.mapLayer,
            style: style,
            center: this.observer ? [this.observer.lon, this.observer.lat] : [-98, 38],
            zoom: 8,
            interactive: false,     // slaved to the radar; see the CSS note
            attributionControl: false,
            fadeDuration: 0,        // no cross-fade while the scope is live
        });
        // MapLibre does NOT throw and does not log: a source that fails to
        // load arrives as an 'error' EVENT, so without this listener a broken
        // tile source is a silent black disc and the browser console is clean.
        this.map.on('error', (e) => {
            console.error('map error:', e && e.error ? e.error.message : e);
        });

        this.updateMapView();
    }

    // Centre and zoom the map so the outermost range ring lands on rangeNM.
    updateMapView() {
        if (!this.map || this.viewMode === 'radar') return;

        // The map layer fills the canvas container (CSS inset:0), and the
        // canvas fills the same box, so the map's centre IS (cx, cy). Only
        // the zoom has to be set from the ring.

        if (this.observer) {
            // Web Mercator ground resolution at this latitude:
            //     m/px = 156543.034 * cos(lat) / 2^zoom
            // We need the scope's outer ring (this.radius px) to be exactly
            // rangeNM, so solve for zoom. MapLibre takes fractional zoom, so
            // this is exact rather than rounded to the nearest tile level --
            // which matters, because a rounded zoom would put the aircraft
            // symbols in the wrong place relative to the terrain under them.
            const metresPerPx = (this.rangeNM * 1852.0) / this.radius;
            const latRad = this.observer.lat * Math.PI / 180.0;
            // THE -1 IS NOT A FUDGE. The classic Web Mercator resolution
            // formula, 156543.034 * cos(lat) / 2^z, is defined for 256 px
            // tiles -- but MapLibre GL defines its zoom against 512 px tiles,
            // so MapLibre zoom z has the ground resolution of standard zoom
            // z+1. Without the correction the map renders at EXACTLY twice
            // the radar's scale: measured 2.0041, 2.0036, 2.0039, 2.0048 and
            // 2.0024 at five offsets, which is the sort of error that looks
            // like plausible terrain on a basemap and is only obvious when
            // something with known geometry -- a runway on a VFR chart --
            // fails to line up.
            const zoom = Math.log2(156543.03392804097 * Math.cos(latRad)
                                   / metresPerPx) - 1;
            this.map.jumpTo({
                center: [this.observer.lon, this.observer.lat],
                zoom: Math.max(0, Math.min(24, zoom)),
            });
        }
        this.map.resize();
    }

    drawGrid() {
        const ctx = this.ctx;
        ctx.save();
        ctx.translate(this.cx, this.cy);

        // Range rings: always 5 circles, with 1 NM always present, all at integer distances
        ctx.strokeStyle = GRID_COLOR;
        ctx.lineWidth = 1;

        // Calculate ring positions: 1 NM, then 3 evenly-spaced integers, then max range
        const rings = [];
        rings.push(1);  // Always include 1 NM

        if (this.rangeNM > 1) {
            // Calculate 3 intermediate rings, evenly spaced and rounded to integers
            const step = (this.rangeNM - 1) / 4;
            for (let i = 1; i <= 3; i++) {
                const r = Math.round(1 + step * i);
                // Avoid duplicates
                if (!rings.includes(r) && r < this.rangeNM) {
                    rings.push(r);
                }
            }
            // Always include the outer range
            if (!rings.includes(this.rangeNM)) {
                rings.push(this.rangeNM);
            }
        }

        // Draw each ring
        for (const r of rings) {
            const radius = r * this.pixelsPerNM;
            ctx.beginPath();
            ctx.arc(0, 0, radius, 0, Math.PI * 2);
            ctx.stroke();

            // Label at top of ring
            ctx.fillStyle = TEXT_COLOR;
            ctx.font = '12px "Courier New"';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'bottom';
            ctx.fillText(`${r} NM`, 0, -radius - 5);
        }

        // Cardinal directions
        const compassRadius = this.radius * 1.05;
        ctx.fillStyle = TEXT_COLOR;
        ctx.font = '16px "Courier New"';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';

        // N
        ctx.fillText('N', 0, -compassRadius);
        // S
        ctx.fillText('S', 0, compassRadius);
        // E
        ctx.textAlign = 'left';
        ctx.fillText('E', compassRadius, 0);
        // W
        ctx.textAlign = 'right';
        ctx.fillText('W', -compassRadius, 0);

        // Crosshairs at center (observer)
        ctx.strokeStyle = PHOSPHOR_GREEN;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(-10, 0);
        ctx.lineTo(10, 0);
        ctx.moveTo(0, -10);
        ctx.lineTo(0, 10);
        ctx.stroke();

        ctx.restore();
    }

    // Is a scope-relative point (origin at cx, cy) drawable? On RADAR the
    // scope IS the ring, so anything past it is off the display. On MAP/VFR
    // the map fills the whole window, so the bound is the canvas rectangle
    // (plus a margin, so a symbol or label straddling the edge is drawn
    // partly rather than popping in and out whole).
    inView(pos, margin = 0) {
        if (this.viewMode === 'radar') {
            return Math.sqrt(pos.x * pos.x + pos.y * pos.y) <= this.radius + margin;
        }
        const m = margin + 40;
        return Math.abs(pos.x) <= this.cx + m && Math.abs(pos.y) <= this.cy + m;
    }

    latLonToXY(lat, lon) {
        if (!this.observer) return null;

        // Simple flat-earth projection (accurate enough for small ranges)
        const dLat = lat - this.observer.lat;
        const dLon = lon - this.observer.lon;

        const nmPerDegLat = 60.0;
        const nmPerDegLon = 60.0 * Math.cos(this.observer.lat * Math.PI / 180);

        const northNM = dLat * nmPerDegLat;
        const eastNM = dLon * nmPerDegLon;

        // Convert to pixels (north = -y, east = +x)
        const x = eastNM * this.pixelsPerNM;
        const y = -northNM * this.pixelsPerNM;

        return { x, y };
    }

    drawTrail(track) {
        const trail = this.history[track.icao];
        if (!trail || trail.length < 2 || this.trailSeconds === 0) return;

        const ctx = this.ctx;
        ctx.strokeStyle = TRAIL_COLOR;
        ctx.lineWidth = 1;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';

        const now = Date.now() / 1000;
        const cutoff = this.trailSeconds === Infinity ? 0 : now - this.trailSeconds;

        ctx.beginPath();
        let first = true;

        for (const [timestamp, lat, lon, alt_ft, course_deg, speed_kt] of trail) {
            // Skip old trail points if not showing full trail
            if (timestamp < cutoff) continue;

            const pos = this.latLonToXY(lat, lon);
            if (!pos) continue;

            // Skip if out of range
            if (!this.inView(pos)) continue;

            if (first) {
                ctx.moveTo(pos.x, pos.y);
                first = false;
            } else {
                ctx.lineTo(pos.x, pos.y);
            }
        }

        ctx.stroke();
    }

    drawAircraft(track) {
        if (track.lat === null || track.lon === null) return;

        const pos = this.latLonToXY(track.lat, track.lon);
        if (!pos) return;

        // Skip if out of range
        if (!this.inView(pos)) return;

        const ctx = this.ctx;

        // Draw projection line if we have course, speed, and projection is enabled
        if (this.projectionSeconds > 0 && track.course_deg !== null && track.speed_kt !== null && track.speed_kt > 0) {
            const courseRad = track.course_deg * Math.PI / 180;
            const distanceNM = (track.speed_kt / 3600) * this.projectionSeconds;  // NM = kt * (seconds / 3600)
            const distancePx = distanceNM * this.pixelsPerNM;

            let projX = pos.x + Math.sin(courseRad) * distancePx;
            let projY = pos.y - Math.cos(courseRad) * distancePx;

            // Clip projection line to radar edge (RADAR only -- over a map
            // the canvas edge does the clipping)
            const showDot = this.inView({ x: projX, y: projY });
            if (!showDot) {
                const clipped = this.clipLineToCircle(pos.x, pos.y, projX, projY);
                projX = clipped.x;
                projY = clipped.y;
            }

            ctx.save();
            ctx.strokeStyle = 'rgba(0, 255, 0, 0.4)';
            ctx.lineWidth = 1;
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(pos.x, pos.y);
            ctx.lineTo(projX, projY);
            ctx.stroke();
            ctx.setLineDash([]);

            // Draw dot at projected position (only if within radar range)
            if (showDot) {
                ctx.fillStyle = PHOSPHOR_GREEN;
                ctx.beginPath();
                ctx.arc(projX, projY, 3, 0, Math.PI * 2);
                ctx.fill();
            }
            ctx.restore();
        }

        // Draw warning circle if aircraft is within alert range or will pass within alert range
        const withinAlertRange = this.alertRangeNM > 0 && track.distance_nm !== null && track.distance_nm <= this.alertRangeNM;
        const willPassWithinAlertRange = this.alertRangeNM > 0 && track.cpa_nm !== null && track.cpa_nm <= this.alertRangeNM && track.closing;
        if (withinAlertRange || willPassWithinAlertRange) {
            ctx.save();
            ctx.translate(pos.x, pos.y);
            ctx.strokeStyle = PHOSPHOR_GREEN;
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.arc(0, 0, 14, 0, Math.PI * 2);
            ctx.stroke();
            ctx.restore();
        }

        // Draw arrow pointing in direction of flight
        const course = track.course_deg !== null ? track.course_deg : 0;
        const courseRad = course * Math.PI / 180;

        ctx.save();
        ctx.translate(pos.x, pos.y);
        ctx.rotate(courseRad);

        // Arrow shape (pointing up = north)
        ctx.fillStyle = PHOSPHOR_GREEN;
        ctx.strokeStyle = PHOSPHOR_GREEN;
        ctx.lineWidth = 2;

        // Dim if predicted/stale
        if (track.predicted) {
            ctx.globalAlpha = 0.5;
        }

        ctx.beginPath();
        ctx.moveTo(0, -12);      // tip
        ctx.lineTo(-6, 6);       // left base
        ctx.lineTo(0, 2);        // notch
        ctx.lineTo(6, 6);        // right base
        ctx.closePath();
        ctx.fill();
        ctx.stroke();

        ctx.restore();

        // Data label
        ctx.save();
        ctx.translate(pos.x, pos.y);

        let alt = '---';
        if (track.alt_ft !== null) {
            if (this.altitudeMode === 'agl' && this.observer && this.observer.alt_ft !== null) {
                alt = Math.round(track.alt_ft - this.observer.alt_ft);
            } else {
                alt = Math.round(track.alt_ft);
            }
        }
        const speed = track.speed_kt !== null ? Math.round(track.speed_kt * 1.15078) : '---'; // kt to mph
        const type = this.formatAircraftType(track);

        const label = `${alt}' ${speed}mph\n${type}`;

        const overMap = this.viewMode !== 'radar';
        ctx.fillStyle = overMap ? MAP_LABEL_COLOR : TEXT_COLOR;
        ctx.font = `${overMap ? 'bold ' : ''}${LABEL_FONT_PX}px "Courier New"`;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        ctx.lineJoin = 'round';
        ctx.lineWidth = 3;
        ctx.strokeStyle = MAP_LABEL_OUTLINE;
        // Outline first, fill over it, so the stroke only shows as a rim.
        const text = (str, x, y) => {
            if (overMap) ctx.strokeText(str, x, y);
            ctx.fillText(str, x, y);
        };

        const lines = label.split('\n');
        const lineHeight = Math.round(LABEL_FONT_PX * 1.2);
        const xOffset = 15;
        const yOffset = -lines.length * lineHeight / 2;

        for (let i = 0; i < lines.length; i++) {
            text(lines[i], xOffset, yOffset + i * lineHeight);
        }

        // Callsign above if available
        if (track.callsign) {
            ctx.textBaseline = 'bottom';
            text(track.callsign, xOffset, yOffset - 2);
        }

        ctx.restore();
    }

    // Clip a line segment to the radar circle
    // Returns the clipped endpoint if the line extends beyond the circle
    clipLineToCircle(startX, startY, endX, endY) {
        // Over a map nothing is bounded by the ring; the canvas clips.
        if (this.viewMode !== 'radar') return { x: endX, y: endY };

        // Check if endpoint is outside radar
        const endDist = Math.sqrt(endX * endX + endY * endY);
        if (endDist <= this.radius) {
            // Already inside, no clipping needed
            return { x: endX, y: endY };
        }

        // Line direction
        const dx = endX - startX;
        const dy = endY - startY;
        const len = Math.sqrt(dx * dx + dy * dy);
        if (len === 0) return { x: endX, y: endY };

        // Normalize direction
        const ndx = dx / len;
        const ndy = dy / len;

        // Ray from start in direction (ndx, ndy)
        // Circle equation: x^2 + y^2 = r^2
        // Parametric ray: (startX + t*ndx, startY + t*ndy)
        // Substitute into circle equation and solve for t

        const a = ndx * ndx + ndy * ndy;  // Always 1 for normalized vector
        const b = 2 * (startX * ndx + startY * ndy);
        const c = startX * startX + startY * startY - this.radius * this.radius;

        const discriminant = b * b - 4 * a * c;
        if (discriminant < 0) {
            // No intersection (shouldn't happen if endpoint is outside)
            return { x: endX, y: endY };
        }

        // Two solutions; we want the farther one (positive t)
        const t1 = (-b + Math.sqrt(discriminant)) / (2 * a);
        const t2 = (-b - Math.sqrt(discriminant)) / (2 * a);
        const t = Math.max(t1, t2);

        return {
            x: startX + t * ndx,
            y: startY + t * ndy
        };
    }

    formatAircraftType(track) {
        if (track.manufacturer && track.model) {
            // Abbreviate common manufacturers
            let mfg = track.manufacturer;
            if (mfg.startsWith('BOEING')) mfg = 'B';
            else if (mfg.startsWith('AIRBUS')) mfg = 'A';
            else if (mfg.startsWith('CESSNA')) mfg = 'C';
            else if (mfg.startsWith('PIPER')) mfg = 'P';
            else if (mfg.startsWith('BEECH')) mfg = 'BE';
            else if (mfg.startsWith('CIRRUS')) mfg = 'SR';
            else mfg = mfg.substring(0, 3);

            let model = track.model;
            // Clean up model strings
            model = model.replace(/^(B|A)-?/, ''); // Remove Boeing/Airbus prefix
            model = model.split(' ')[0]; // First word only

            return `${mfg}${model}`;
        }

        if (track.model) {
            return track.model.substring(0, 8);
        }

        // Fallback to callsign if no registry data
        if (track.callsign) {
            return track.callsign;
        }

        return 'UNKNOWN';
    }

    drawOverlay(overlay) {
        const ctx = this.ctx;
        ctx.save();

        // Clip everything to the radar circle so boundaries/labels don't spill
        // past the scope edge -- RADAR only; over a map they fill the window.
        if (this.viewMode === 'radar') {
            ctx.beginPath();
            ctx.arc(0, 0, this.radius, 0, Math.PI * 2);
            ctx.clip();
        }

        // Polygons — outlined + faintly filled.
        for (const poly of overlay.polygons || []) {
            if (!poly.coords || poly.coords.length < 2) continue;
            ctx.beginPath();
            let first = true;
            for (const [lat, lon] of poly.coords) {
                const pos = this.latLonToXY(lat, lon);
                if (!pos) continue;
                if (first) { ctx.moveTo(pos.x, pos.y); first = false; }
                else ctx.lineTo(pos.x, pos.y);
            }
            ctx.closePath();
            ctx.fillStyle = OVERLAY_FILL;
            ctx.fill();
            ctx.strokeStyle = OVERLAY_LINE;
            ctx.lineWidth = 1;
            ctx.stroke();
        }

        // Lines.
        for (const line of overlay.lines || []) {
            if (!line.coords || line.coords.length < 2) continue;
            ctx.beginPath();
            let first = true;
            for (const [lat, lon] of line.coords) {
                const pos = this.latLonToXY(lat, lon);
                if (!pos) continue;
                if (first) { ctx.moveTo(pos.x, pos.y); first = false; }
                else ctx.lineTo(pos.x, pos.y);
            }
            ctx.strokeStyle = OVERLAY_LINE;
            ctx.lineWidth = 1;
            ctx.stroke();
        }

        // Point labels — only those within the scope.
        ctx.fillStyle = OVERLAY_COLOR;
        ctx.font = '10px "Courier New"';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        for (const pt of overlay.points || []) {
            const pos = this.latLonToXY(pt.lat, pt.lon);
            if (!pos) continue;
            if (!this.inView(pos)) continue;
            if (pt.name) ctx.fillText(pt.name, pos.x, pos.y);
        }

        ctx.restore();
    }

    drawAirport(airport) {
        if (!airport.runways || airport.runways.length === 0) return;

        const ctx = this.ctx;
        let drewAnyRunway = false;

        // Draw each runway
        for (const runway of airport.runways) {
            if (runway.closed) continue;  // Skip closed runways

            if (!runway.le_lat || !runway.le_lon || !runway.he_lat || !runway.he_lon) continue;

            const lePos = this.latLonToXY(runway.le_lat, runway.le_lon);
            const hePos = this.latLonToXY(runway.he_lat, runway.he_lon);

            if (!lePos || !hePos) continue;

            // Check if runway is within radar range
            if (!this.inView(lePos) && !this.inView(hePos)) continue;

            // Draw runway outline
            const widthNM = (runway.width_ft || 100) / 6076.12;  // Convert feet to NM
            const widthPx = widthNM * this.pixelsPerNM;

            ctx.save();

            // Draw extended centerline (approach pattern) - 8 NM out from each end
            const approachDistNM = 8.0;
            const approachDistPx = approachDistNM * this.pixelsPerNM;

            // Low-end approach (LE heading is the approach direction)
            if (runway.le_heading_degt !== null && runway.le_heading_degt !== undefined) {
                const heading = runway.le_heading_degt;
                const headingRad = heading * Math.PI / 180;
                let extendX = Math.sin(headingRad) * approachDistPx;
                let extendY = -Math.cos(headingRad) * approachDistPx;

                // Calculate endpoint
                let endX = lePos.x - extendX;
                let endY = lePos.y - extendY;

                // Clip to radar edge along the line direction
                if (!this.inView({ x: endX, y: endY })) {
                    const clipped = this.clipLineToCircle(lePos.x, lePos.y, endX, endY);
                    endX = clipped.x;
                    endY = clipped.y;
                }

                ctx.strokeStyle = 'rgba(0, 255, 0, 0.6)';  // Brighter for sunlight visibility
                ctx.lineWidth = 1;
                ctx.setLineDash([5, 5]);
                ctx.beginPath();
                ctx.moveTo(lePos.x, lePos.y);
                ctx.lineTo(endX, endY);
                ctx.stroke();
                ctx.setLineDash([]);
            }

            // High-end approach (HE heading is the approach direction)
            if (runway.he_heading_degt !== null && runway.he_heading_degt !== undefined) {
                const heading = runway.he_heading_degt;
                const headingRad = heading * Math.PI / 180;
                let extendX = Math.sin(headingRad) * approachDistPx;
                let extendY = -Math.cos(headingRad) * approachDistPx;

                // Calculate endpoint
                let endX = hePos.x - extendX;
                let endY = hePos.y - extendY;

                // Clip to radar edge along the line direction
                if (!this.inView({ x: endX, y: endY })) {
                    const clipped = this.clipLineToCircle(hePos.x, hePos.y, endX, endY);
                    endX = clipped.x;
                    endY = clipped.y;
                }

                ctx.strokeStyle = 'rgba(0, 255, 0, 0.6)';  // Brighter for sunlight visibility
                ctx.lineWidth = 1;
                ctx.setLineDash([5, 5]);
                ctx.beginPath();
                ctx.moveTo(hePos.x, hePos.y);
                ctx.lineTo(endX, endY);
                ctx.stroke();
                ctx.setLineDash([]);
            }

            // Draw runway rectangle
            const dx = hePos.x - lePos.x;
            const dy = hePos.y - lePos.y;
            const length = Math.sqrt(dx * dx + dy * dy);
            const angle = Math.atan2(dy, dx);

            ctx.translate(lePos.x, lePos.y);
            ctx.rotate(angle);

            ctx.fillStyle = 'rgba(0, 255, 0, 0.2)';
            ctx.strokeStyle = PHOSPHOR_GREEN;
            ctx.lineWidth = 1;

            ctx.fillRect(0, -widthPx / 2, length, widthPx);
            ctx.strokeRect(0, -widthPx / 2, length, widthPx);

            // Draw runway identifiers at each end
            ctx.fillStyle = TEXT_COLOR;
            ctx.font = '10px "Courier New"';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(runway.le_ident, 0, 0);
            ctx.fillText(runway.he_ident, length, 0);

            ctx.restore();
            drewAnyRunway = true;  // Mark that we successfully drew at least one runway
        }

        // Only draw airport marker if we actually drew at least one runway
        if (!drewAnyRunway) return;

        // Draw airport marker (small circle at airport reference point)
        const airportPos = this.latLonToXY(airport.lat, airport.lon);
        if (airportPos) {
            if (this.inView(airportPos)) {
                ctx.save();
                ctx.translate(airportPos.x, airportPos.y);
                ctx.strokeStyle = PHOSPHOR_GREEN;
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.arc(0, 0, 3, 0, Math.PI * 2);
                ctx.stroke();

                // Airport identifier
                ctx.fillStyle = TEXT_COLOR;
                ctx.font = '10px "Courier New"';
                ctx.textAlign = 'left';
                ctx.textBaseline = 'middle';
                ctx.fillText(airport.ident, 6, 0);
                ctx.restore();
            }
        }
    }
}

// Initialize when page loads
window.addEventListener('DOMContentLoaded', () => {
    // Determine WebSocket URL.
    //
    // Direct/local access (http://host:8086/radar.html): the WS server is a
    // separate port on the same host, so connect to ws://host:8765.
    //
    // Behind a TLS reverse proxy (https://adsb.n0gq.org/): the page is HTTPS,
    // so a plaintext ws:// to :8765 would be blocked as mixed content and the
    // raw WS port isn't exposed publicly anyway. Use a same-origin wss:// URL
    // and let nginx proxy the /ws path back to the server's :8765.
    let wsUrl;
    if (window.location.protocol === 'https:') {
        wsUrl = `wss://${window.location.host}/ws`;
    } else {
        wsUrl = `ws://${window.location.hostname}:8765`;
    }
    const radar = new RadarDisplay('radar', wsUrl);

    // Make radar globally accessible for debugging
    window.radar = radar;
});
