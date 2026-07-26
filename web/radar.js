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
        this.facilities = null;
        this.overlay = null;  // Static KML overlay (polygons/lines/points), sent once on connect
        this.rangeNM = 5.0;
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

        // Animation state for phosphor persistence
        this.lastFrame = performance.now();
        this.fadeCanvas = document.createElement('canvas');
        this.fadeCtx = this.fadeCanvas.getContext('2d');

        // Canvas dimensions (must be after fadeCanvas creation)
        this.resize();
        window.addEventListener('resize', () => this.resize());

        // Setup control event listeners
        document.getElementById('range-select').addEventListener('change', (e) => {
            this.rangeNM = parseFloat(e.target.value);
            this.resize();  // Recalculate pixels per NM
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
    }

    connect() {
        console.log('Connecting to', this.wsUrl);
        this.ws = new WebSocket(this.wsUrl);

        this.ws.onopen = () => {
            console.log('WebSocket connected');
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

    handleSnapshot(data) {
        this.observer = data.observer;
        this.tracks = data.tracks;
        this.history = data.history;
        this.facilities = data.facilities;

        // Check for sound trigger events
        this.checkSoundTriggers();

        // Update status bar
        if (this.observer) {
            document.getElementById('observer-pos').textContent =
                `Observer: ${this.observer.lat.toFixed(4)}, ${this.observer.lon.toFixed(4)} @ ${Math.round(this.observer.alt_ft)} ft`;
        }

        const airportCount = this.facilities?.airports?.length || 0;
        document.getElementById('track-count').textContent = `Tracks: ${this.tracks.length} | Airports: ${airportCount}`;

        // Update indicator lights
        const adsbLight = document.getElementById('adsb-light');
        const uatLight = document.getElementById('uat-light');
        const gpsLight = document.getElementById('gps-light');

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

        // GPS light: on if observer position is set.
        setLight(gpsLight,
            this.observer && this.observer.lat !== null && this.observer.lon !== null);
    }

    checkSoundTriggers() {
        if (!this.tracks || this.alertRangeNM === 0) return;

        const currentIcaos = new Set();

        for (const track of this.tracks) {
            currentIcaos.add(track.icao);

            // Determine current state
            const isApproaching = track.cpa_nm !== null && track.cpa_nm <= this.alertRangeNM && track.closing;
            const isInRange = track.distance_nm !== null && track.distance_nm <= this.alertRangeNM;

            // Get previous state
            const prevState = this.aircraftStates[track.icao] || { wasApproaching: false, wasInRange: false };

            // Trigger sounds on state transitions
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
        // Phosphor persistence effect: fade the previous frame
        this.ctx.drawImage(this.fadeCanvas, 0, 0);
        this.fadeCtx.fillStyle = 'rgba(0, 0, 0, 0.08)';  // Fade rate
        this.fadeCtx.fillRect(0, 0, this.fadeCanvas.width, this.fadeCanvas.height);
        this.fadeCtx.drawImage(this.canvas, 0, 0);

        // Clear current frame
        this.ctx.fillStyle = BACKGROUND;
        this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

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
            const dist = Math.sqrt(pos.x * pos.x + pos.y * pos.y);
            if (dist > this.radius) continue;

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
        const dist = Math.sqrt(pos.x * pos.x + pos.y * pos.y);
        if (dist > this.radius) return;

        const ctx = this.ctx;

        // Draw projection line if we have course, speed, and projection is enabled
        if (this.projectionSeconds > 0 && track.course_deg !== null && track.speed_kt !== null && track.speed_kt > 0) {
            const courseRad = track.course_deg * Math.PI / 180;
            const distanceNM = (track.speed_kt / 3600) * this.projectionSeconds;  // NM = kt * (seconds / 3600)
            const distancePx = distanceNM * this.pixelsPerNM;

            let projX = pos.x + Math.sin(courseRad) * distancePx;
            let projY = pos.y - Math.cos(courseRad) * distancePx;

            // Clip projection line to radar edge
            const projDist = Math.sqrt(projX * projX + projY * projY);
            const showDot = projDist <= this.radius;
            if (projDist > this.radius) {
                // Clip to radar circle along the line direction
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

        ctx.fillStyle = TEXT_COLOR;
        ctx.font = '10px "Courier New"';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';

        const lines = label.split('\n');
        const lineHeight = 12;
        const xOffset = 15;
        const yOffset = -lines.length * lineHeight / 2;

        for (let i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], xOffset, yOffset + i * lineHeight);
        }

        // Callsign above if available
        if (track.callsign) {
            ctx.textBaseline = 'bottom';
            ctx.fillText(track.callsign, xOffset, -15);
        }

        ctx.restore();
    }

    // Clip a line segment to the radar circle
    // Returns the clipped endpoint if the line extends beyond the circle
    clipLineToCircle(startX, startY, endX, endY) {
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
        // past the scope edge.
        ctx.beginPath();
        ctx.arc(0, 0, this.radius, 0, Math.PI * 2);
        ctx.clip();

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
            const dist = Math.sqrt(pos.x * pos.x + pos.y * pos.y);
            if (dist > this.radius) continue;
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
            const leDist = Math.sqrt(lePos.x * lePos.x + lePos.y * lePos.y);
            const heDist = Math.sqrt(hePos.x * hePos.x + hePos.y * hePos.y);
            if (leDist > this.radius && heDist > this.radius) continue;

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
                const endDist = Math.sqrt(endX * endX + endY * endY);
                if (endDist > this.radius) {
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
                const endDist = Math.sqrt(endX * endX + endY * endY);
                if (endDist > this.radius) {
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
            const dist = Math.sqrt(airportPos.x * airportPos.x + airportPos.y * airportPos.y);
            if (dist <= this.radius) {
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
    // Determine WebSocket URL (same host, port 8765)
    const wsUrl = `ws://${window.location.hostname}:8765`;
    const radar = new RadarDisplay('radar', wsUrl);

    // Make radar globally accessible for debugging
    window.radar = radar;
});
