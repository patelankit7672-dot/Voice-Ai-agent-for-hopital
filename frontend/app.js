/* ==========================================================================
 * Varanasi Hospital — AI Voice Assistant (browser client)
 *
 * SECURITY
 * --------
 * There is no API key in this file, and there must never be one.
 * The only credential this page ever holds is the short-lived, single-use
 * token returned by GET /api/voice-token, which is kept in a local variable
 * for the few seconds it takes to open the WebSocket and is then discarded.
 * It is never written to localStorage, sessionStorage, a cookie or the DOM.
 *
 * VOICE PROTOCOL (AssemblyAI Voice Agent API, verified September 2026)
 *   1. GET /api/voice-token                     -> { token, session_config }
 *   2. open wss://agents.assemblyai.com/v1/ws?token=<token>
 *   3. send { type: "session.update", session: <session_config> } immediately
 *   4. wait for session.ready, then stream { type: "input.audio", audio: b64 }
 *   5. play reply.audio (base64 PCM16 @ 24 kHz in the `data` field)
 *   6. answer tool.call with tool.result
 *   7. send { type: "session.end" } before closing
 * ======================================================================== */

'use strict';

/* ------------------------------------------------------------------ *
 * Constants
 * ------------------------------------------------------------------ */

const SAMPLE_RATE = 24000;          // Voice Agent API requires 24 kHz PCM16
const CHUNK_SECONDS = 0.05;         // ~50 ms per chunk, as the docs recommend
const ECHO_GUARD_MS = 350;          // ignore voice activity while the agent is audible
// How long after the agent stops to keep the microphone gated on speakers.
// Covers the room's reverb tail, which the recogniser would otherwise hear.
const MIC_GATE_TAIL_MS = 250;
// Absolute floor for treating microphone input as the caller rather than
// leakage. A quiet array's noise floor tends toward zero, so a purely
// relative threshold degenerates to "any sound at all".
const BARGE_IN_FLOOR = 900;
// Consecutive ~50 ms chunks above the threshold before Arin is cut off.
// Speech sustains; a tap or a breath does not.
const BARGE_IN_CHUNKS = 4;
// How long a session may receive pure digital silence before the caller is
// told their microphone is producing nothing.
const SILENCE_WATCHDOG_MS = 5000;
// Hard ceiling on bringing a session up, so the UI can never sit on
// "Connecting…" forever.
const CONNECT_TIMEOUT_MS = 15000;
const TOOL_RESULT_MAX_HOLD_MS = 2500;
// Jitter buffer. 0.06 was too shallow for 10 ms streamed chunks: a late
// burst dropped the cursor behind the clock and a gap was stitched into the
// sentence — 51 gaps totalling 3654 ms in one measured reply. A fifth of a
// second absorbs normal network jitter and is imperceptible at the start of
// a spoken reply.
const PLAYBACK_LEAD_SECONDS = 0.2;
// Coalesce streamed chunks to roughly this size before scheduling, instead of
// creating a BufferSource for every 10 ms.
const COALESCE_SAMPLES = Math.round(SAMPLE_RATE * 0.12);   // ~120 ms
const COALESCE_MAX_WAIT_MS = 40;

const STATUS = {
  idle:       { text: 'Disconnected',                      dot: 'idle',       mic: '' },
  connecting: { text: 'Connecting…',                       dot: 'connecting', mic: 'is-connecting' },
  connected:  { text: 'Connected — Arin is ready',         dot: 'connected',  mic: 'is-listening' },
  listening:  { text: '🎤 Arin is listening…',              dot: 'listening',  mic: 'is-listening' },
  thinking:   { text: '🧠 Arin is thinking…',               dot: 'connecting', mic: 'is-connecting' },
  speaking:   { text: '🔊 Arin is speaking…',               dot: 'speaking',   mic: 'is-speaking' },
  error:      { text: 'Disconnected',                      dot: 'error',      mic: '' },
};

/* ------------------------------------------------------------------ *
 * DOM
 * ------------------------------------------------------------------ */

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const el = {
  micButton:        $('#mic-button'),
  micButtonLabel:   $('#mic-button-label'),
  startButton:      $('#start-button'),
  startButtonText:  $('#start-button-text'),
  reconnectButton:  $('#reconnect-button'),
  statusDot:        $('#status-dot'),
  statusText:       $('#status-text'),
  transcript:       $('#transcript'),
  transcriptEmpty:  $('#transcript-empty'),
  toolActivity:     $('#tool-activity'),
  errorBox:         $('#error-box'),
  langNote:         $('#lang-note'),
  micSelect:        $('#mic-select'),
  audioSetup:       $('#audio-setup'),
  outputSelect:     $('#output-select'),
  outputStatus:     $('#output-status'),
  outputNote:       $('#output-note'),
  audioSetupNote:   $('#audio-setup-note'),
  langSelect:       $('#lang-select'),
  micLevelHint:     $('#mic-level-hint'),
  emergencyBanner:  $('#emergency-banner'),
  emergencyHeadline:$('#emergency-headline'),
  emergencyList:    $('#emergency-instructions'),
  emergencyNote:    $('#emergency-note'),
  appointmentsList: $('#appointments-list'),
  hospitalInfo:     $('#hospital-info'),
};

/* ------------------------------------------------------------------ *
 * Small helpers
 * ------------------------------------------------------------------ */

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function apiGet(path) {
  const res = await fetch(path, { headers: { Accept: 'application/json' } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
}

async function apiPost(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(body.detail)
      ? body.detail.map((d) => d.msg || '').filter(Boolean).join('; ')
      : body.detail;
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return body;
}

function to12Hour(hhmm) {
  const [h, m] = String(hhmm).split(':').map(Number);
  if (Number.isNaN(h)) return hhmm;
  const suffix = h < 12 ? 'AM' : 'PM';
  return `${h % 12 || 12}:${String(m).padStart(2, '0')} ${suffix}`;
}

function formatDate(iso) {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' });
}

/* Base64 <-> PCM16 --------------------------------------------------- */

function int16ToBase64(int16) {
  const bytes = new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength);
  let binary = '';
  const STEP = 0x8000;
  for (let i = 0; i < bytes.length; i += STEP) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + STEP));
  }
  return btoa(binary);
}

function base64ToInt16(b64) {
  const binary = atob(b64);
  const usable = binary.length - (binary.length % 2);   // PCM16 needs even bytes
  const bytes = new Uint8Array(usable);
  for (let i = 0; i < usable; i += 1) bytes[i] = binary.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

/*
 * `new AudioContext({ sampleRate })` is a REQUEST, not a guarantee: some
 * browsers and audio devices hand back 44100 or 48000 regardless. Audio sent
 * at the wrong rate is decoded at the wrong speed and recognition silently
 * fails, so every chunk is resampled to SAMPLE_RATE before it goes out.
 */
function resampleInt16(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const outLength = Math.floor(input.length / ratio);
  const output = new Int16Array(outLength);
  for (let i = 0; i < outLength; i += 1) {
    const pos = i * ratio;
    const idx = Math.floor(pos);
    const frac = pos - idx;
    const a = input[idx];
    const b = idx + 1 < input.length ? input[idx + 1] : a;
    output[i] = a + (b - a) * frac;       // linear interpolation
  }
  return output;
}

/* ------------------------------------------------------------------ *
 * UI state
 * ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ *
 * Hindi speech (Sarvam)
 *
 * AssemblyAI has no Hindi voice, so in a Hindi session its audio is ignored
 * and the agent's TEXT is sent to our own /api/speech/hindi, which voices it
 * with Sarvam's native Hindi model. The Sarvam key stays on the server; this
 * file only ever sends text and receives audio.
 *
 * Sentences are synthesised as soon as they complete rather than waiting for
 * the whole reply, so speech starts while the agent is still writing. A
 * single promise chain keeps them strictly in order — parallel requests would
 * otherwise resolve out of sequence and scramble the sentences.
 * ------------------------------------------------------------------ */

const SENTENCE_END = /[।.!?]\s*$/;

class HindiSpeaker {
  constructor(player) {
    this.player = player;
    this.buffer = '';
    this.chain = Promise.resolve();
    this.generation = 0;      // bumped on interruption to drop stale audio
    this.pending = 0;         // sentences being synthesised right now
  }

  /** Feed agent text as it arrives; speaks each completed sentence. */
  push(textChunk) {
    this.buffer += textChunk;
    let match;
    // Split on sentence boundaries so speech can start early.
    while ((match = this.buffer.match(/^[\s\S]*?[।.!?]/))) {
      const sentence = match[0];
      this.buffer = this.buffer.slice(sentence.length);
      this.speak(sentence.trim());
    }
  }

  /** Speak whatever is left when the reply ends. */
  flush() {
    const rest = this.buffer.trim();
    this.buffer = '';
    if (rest) this.speak(rest);
  }

  speak(text) {
    if (!text) return;
    // A chunk of pure punctuation still returns audio from the service —
    // measured at 2.3s for a lone "।" — which would be a stray noise between
    // sentences. Only send something with an actual letter or digit in it.
    if (!/[\p{L}\p{N}]/u.test(text)) return;
    const generation = this.generation;
    this.pending += 1;
    this.chain = this.chain.then(async () => {
      try {
        if (generation !== this.generation) return;   // interrupted meanwhile
        const body = await apiPost('/api/speech/hindi', { text });
        if (generation !== this.generation || !this.player) return;
        this.player.enqueue(base64ToInt16(body.audio));
      } catch (err) {
        // A failed sentence must not stop the rest of the reply.
        console.warn('Hindi speech failed for one sentence:', err.message);
      } finally {
        this.pending = Math.max(0, this.pending - 1);
      }
    });
  }

  /** Drop queued and in-flight speech — the caller interrupted. */
  cancel() {
    this.generation += 1;
    this.pending = 0;
    this.buffer = '';
  }
}

/**
 * Is the agent's voice currently coming out of the speakers, or about to be?
 *
 * Covers three states, because any of them means microphone activity is
 * probably the agent's own voice leaking back in rather than the caller:
 *   1. audio is scheduled and playing now
 *   2. audio arrived moments ago (the tail of a sentence)
 *   3. Hindi sentences are still being synthesised over the network
 */
function agentIsAudible(tailMs = ECHO_GUARD_MS) {
  if (app.hindiSpeaker && app.hindiSpeaker.pending > 0) return true;
  if (!app.player) return false;
  if (app.player.isPlaying) return true;
  return Date.now() - app.player.lastEnqueueAt < tailMs;
}

/* ------------------------------------------------------------------ *
 * Audio output routing (headphones)
 *
 * Browsers choose the output device, not the page — unless the page asks.
 * `setSinkId` is that ask, and support is uneven, so this handles three
 * levels and is honest with the caller about which one they are on:
 *
 *   1. AudioContext.setSinkId — routes Web Audio directly. Cleanest.
 *   2. HTMLMediaElement.setSinkId — playback is bridged through a
 *      MediaStreamAudioDestinationNode into an <audio> element, which can be
 *      pointed at a device.
 *   3. Neither — the page cannot choose, and says so rather than pretending.
 *
 * Output only. The microphone graph is never touched by any of this: a
 * caller can send audio from the laptop mic while hearing Arin in a headset.
 * ------------------------------------------------------------------ */

const OUTPUT_PREF_KEY = 'varanasi.audioOutput';

const audioOutput = {
  /** 'context' | 'element' | 'unsupported' */
  support: 'unsupported',
  deviceId: '',
  sinkNode: null,
  element: null,

  detectSupport() {
    if (typeof AudioContext !== 'undefined'
        && typeof AudioContext.prototype.setSinkId === 'function') {
      this.support = 'context';
    } else if (typeof HTMLMediaElement !== 'undefined'
        && typeof HTMLMediaElement.prototype.setSinkId === 'function') {
      this.support = 'element';
    } else {
      this.support = 'unsupported';
    }
    return this.support;
  },

  /**
   * Devices are only labelled after microphone permission has been granted
   * once — before that the browser returns blank labels to prevent
   * fingerprinting, so the list is not useful yet.
   */
  async list() {
    if (!navigator.mediaDevices?.enumerateDevices) return [];
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      return devices.filter((d) => d.kind === 'audiooutput');
    } catch {
      return [];
    }
  },

  /** Heuristic: does any output look like a headset? Labels vary by OS. */
  looksLikeHeadphones(label) {
    return /head(set|phone)|earphone|earbud|airpod|buds|hands[- ]?free/i.test(label || '');
  },

  /**
   * Build the graph for the chosen device, returning the node the player
   * should write into (or null to use ctx.destination directly).
   */
  async attach(ctx, deviceId) {
    this.detach();
    this.deviceId = deviceId || '';
    if (!deviceId) return null;               // system default

    if (this.support === 'context') {
      try {
        await ctx.setSinkId(deviceId);
        return null;                          // Web Audio now targets it
      } catch (err) {
        console.warn('AudioContext.setSinkId failed:', err.message);
      }
    }

    if (this.support === 'element') {
      try {
        const node = ctx.createMediaStreamDestination();
        const el = new Audio();
        el.srcObject = node.stream;
        await el.setSinkId(deviceId);
        await el.play();
        this.sinkNode = node;
        this.element = el;
        return node;
      } catch (err) {
        console.warn('HTMLMediaElement.setSinkId failed:', err.message);
        this.detach();
      }
    }

    return null;                              // fall back to the default output
  },

  detach() {
    if (this.element) {
      try { this.element.pause(); this.element.srcObject = null; } catch { /* noop */ }
    }
    this.element = null;
    this.sinkNode = null;
  },
};

/* --- theme ------------------------------------------------------------ */

const THEME_KEY = 'varanasi.theme';

function applyTheme(dark, persist) {
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  const toggle = $('#switch');
  if (toggle) toggle.checked = dark;
  if (persist) {
    try { localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light'); } catch { /* ignore */ }
  }
}

function initTheme() {
  const toggle = $('#switch');
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch { /* ignore */ }

  const media = window.matchMedia('(prefers-color-scheme: dark)');
  applyTheme(saved ? saved === 'dark' : media.matches, false);

  if (toggle) {
    toggle.addEventListener('change', () => applyTheme(toggle.checked, true));
  }
  // Follow the operating system until the user states a preference.
  media.addEventListener?.('change', (event) => {
    let stored = null;
    try { stored = localStorage.getItem(THEME_KEY); } catch { /* ignore */ }
    if (!stored) applyTheme(event.matches, false);
  });
}

/* --- microphone selection -------------------------------------------- */

const MIC_PREF_KEY = 'varanasi.micDeviceId';
const LANG_PREF_KEY = 'varanasi.language';
const AUDIO_SETUP_KEY = 'varanasi.audioSetup';

/**
 * Fill the picker with the real input devices. Labels are only exposed once
 * permission has been granted at least once, so this is worth re-running
 * after a session starts.
 */
async function refreshMicList() {
  if (!el.micSelect || !navigator.mediaDevices?.enumerateDevices) return;
  let devices;
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch {
    return;
  }
  const inputs = devices.filter((d) => d.kind === 'audioinput' && d.deviceId !== 'communications');
  const previous = el.micSelect.value || localStorage.getItem(MIC_PREF_KEY) || '';

  el.micSelect.innerHTML = '';
  const fallback = document.createElement('option');
  fallback.value = '';
  fallback.textContent = 'Default microphone';
  el.micSelect.appendChild(fallback);

  inputs.forEach((device, index) => {
    if (device.deviceId === 'default') return;
    const option = document.createElement('option');
    option.value = device.deviceId;
    const label = device.label || `Microphone ${index + 1}`;
    /*
     * Flag the Bluetooth A2DP endpoint. Bluetooth cannot carry high-quality
     * audio and a microphone simultaneously: the "Headphones (X)" endpoint is
     * playback only and captures pure silence, while "Headset (X Hands-Free)"
     * has a working microphone at reduced playback quality. Naming that here
     * saves the caller from choosing a device that can never work.
     */
    const a2dpOnly = /headphone/i.test(label) && !/hands[- ]?free|headset/i.test(label);
    option.textContent = a2dpOnly ? `${label} — no microphone` : label;
    option.dataset.a2dpOnly = a2dpOnly ? 'true' : 'false';
    el.micSelect.appendChild(option);
  });

  if (previous && el.micSelect.querySelector(`option[value="${CSS.escape(previous)}"]`)) {
    el.micSelect.value = previous;
  }
}

/**
 * Force the capture pipeline to actually run.
 *
 * Chromium on Windows sometimes hands back a MediaStreamAudioSourceNode that
 * emits pure silence even though the track is live and unmuted. Attaching the
 * same stream to a muted <audio> element makes the browser pull audio through
 * the pipeline, which is the long-standing workaround. The element is muted
 * and never added to the layout, so it cannot be heard or seen and cannot
 * feed the speakers.
 */
function keepStreamAwake(stream) {
  try {
    const sink = new Audio();
    sink.muted = true;
    sink.srcObject = stream;
    // Autoplay of a muted element is always permitted.
    sink.play().catch(() => {});
    return sink;
  } catch {
    return null;
  }
}

/**
 * Record a few seconds with MediaRecorder and report the true peak.
 *
 * This is the only measurement in the app that does NOT go through
 * createMediaStreamSource(). Every other check — the capture worklet, the
 * live meter, the session analyser — hangs off that one node, so if IT is the
 * thing returning silence they all agree and all mislead. MediaRecorder takes
 * a different path out of the same track, so disagreement between the two
 * localises the fault precisely:
 *
 *   both silent      -> the device really is delivering nothing
 *   recorder has audio, Web Audio silent -> the browser's Web Audio capture
 *                                           path is broken, not the hardware
 */
async function recordPeak(stream, ms = 4000) {
  if (typeof MediaRecorder === 'undefined') return null;
  let recorder;
  try {
    recorder = new MediaRecorder(stream);
  } catch {
    return null;
  }

  const chunks = [];
  recorder.ondataavailable = (event) => {
    if (event.data && event.data.size) chunks.push(event.data);
  };

  const finished = new Promise((resolve) => { recorder.onstop = resolve; });
  recorder.start();
  await new Promise((r) => setTimeout(r, ms));
  try { recorder.stop(); } catch { /* already stopped */ }
  await finished;

  if (!chunks.length) return null;
  const blob = new Blob(chunks);
  const bytes = await blob.arrayBuffer();

  // decodeAudioData is a separate code path from live capture.
  const decodeCtx = new (window.AudioContext || window.webkitAudioContext)();
  try {
    const buffer = await decodeCtx.decodeAudioData(bytes);
    let peak = 0;
    for (let c = 0; c < buffer.numberOfChannels; c += 1) {
      const data = buffer.getChannelData(c);
      for (let i = 0; i < data.length; i += 1) {
        const a = data[i] < 0 ? -data[i] : data[i];
        if (a > peak) peak = a;
      }
    }
    return { peak, bytes: blob.size, seconds: buffer.duration };
  } catch {
    return { peak: null, bytes: blob.size, seconds: null };
  } finally {
    decodeCtx.close().catch(() => {});
  }
}

/**
 * Microphone check that does not open a voice session.
 *
 * Runs the Web Audio meter and a MediaRecorder capture side by side, because
 * those two paths can disagree — and which one is silent is the whole answer.
 */
async function testMicrophone() {
  const button = $('#mic-test-button');
  const meter = $('#mic-meter');
  const fill = $('#mic-meter-fill');

  if (app.micTest) {                       // already running — stop early
    app.micTest.stop();
    return;
  }

  button.textContent = 'Stop';
  meter.hidden = false;
  setMicHint('Say something…', null);

  let stream;
  let ctx;
  try {
    const audio = { echoCancellation: false, noiseSuppression: false, autoGainControl: true };
    const chosen = el.micSelect && el.micSelect.value;
    if (chosen) audio.deviceId = { exact: chosen };
    stream = await navigator.mediaDevices.getUserMedia({ audio });
    refreshMicList();
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === 'suspended') await ctx.resume();
  } catch (err) {
    setMicHint(`Could not open that microphone: ${err.message}`, 'bad');
    button.textContent = 'Test';
    meter.hidden = true;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    return;
  }

  const sink = keepStreamAwake(stream);
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  source.connect(analyser);
  const samples = new Float32Array(analyser.fftSize);

  let best = 0;
  let raf = null;
  let stopped = false;
  const started = Date.now();

  const cleanup = () => {
    cancelAnimationFrame(raf);
    clearTimeout(app.micTest?.timeout);
    app.micTest = null;
    stream.getTracks().forEach((t) => t.stop());
    ctx.close().catch(() => {});
    if (sink) { sink.pause(); sink.srcObject = null; }
    button.textContent = 'Test';
    meter.hidden = true;
  };

  const stop = () => {
    if (stopped) return;
    stopped = true;
    cleanup();
    if (best >= 0.01) {
      setMicHint(`Microphone works — peak level ${(best * 100).toFixed(0)}%.`, 'good');
    }
  };

  const tick = () => {
    analyser.getFloatTimeDomainData(samples);
    let peak = 0;
    for (let i = 0; i < samples.length; i += 1) {
      const a = samples[i] < 0 ? -samples[i] : samples[i];
      if (a > peak) peak = a;
    }
    if (peak > best) best = peak;
    // Square-root curve: quiet speech still moves the bar visibly.
    fill.style.width = `${Math.min(100, Math.sqrt(peak) * 140)}%`;
    if (Date.now() - started > 1500 && best < 0.01) {
      setMicHint('Keep talking — checking a second way…', null);
    }
    raf = requestAnimationFrame(tick);
  };

  app.micTest = { stop, timeout: setTimeout(stop, 10000) };
  tick();

  // Run the independent recorder check alongside the live meter.
  const recorded = await recordPeak(stream, 4000);
  if (stopped) return;

  console.log('[DIAG] mic test — webAudioPeak:', best.toFixed(4), '| recorder:', recorded);

  if (best >= 0.01) {
    stop();
    return;
  }

  stopped = true;
  cleanup();

  if (recorded && recorded.peak !== null && recorded.peak >= 0.01) {
    // The device is fine; the browser's Web Audio capture path is not.
    setMicHint(
      `Your microphone IS working (recorded peak ${(recorded.peak * 100).toFixed(0)}%), but this ` +
      "browser's audio capture is returning silence. Fully quit Edge and reopen it, " +
      'or try this page in Chrome or Firefox.',
      'bad'
    );
  } else {
    setMicHint(
      'No sound reached the browser at all. Pick a different microphone above, then ' +
      'check Windows Settings → System → Sound → Input and test it there.',
      'bad'
    );
  }
}

/* ------------------------------------------------------------------ *
 * Connection timing
 *
 * Every stage of bringing a session up is marked, so a slow connection can
 * be attributed to a stage instead of guessed at. Kept in production: the
 * cost is a few timestamps, and the alternative is debugging latency blind.
 * ------------------------------------------------------------------ */

const timings = {
  marks: {},
  t0: 0,

  start() {
    this.marks = {};
    this.t0 = performance.now();
  },

  mark(name) {
    if (!this.t0) return;
    this.marks[name] = Math.round(performance.now() - this.t0);
  },

  report(label) {
    if (!this.t0) return null;
    const entries = Object.entries(this.marks);
    let previous = 0;
    const steps = entries.map(([name, at]) => {
      const step = at - previous;
      previous = at;
      return `${name}=${step}ms`;
    });
    const total = entries.length ? entries[entries.length - 1][1] : 0;
    console.log(`[TIMING] ${label}: ${steps.join('  ')}  TOTAL=${total}ms`);
    return { ...this.marks, total };
  },
};

/**
 * Watch for a microphone that is delivering literal digital silence.
 *
 * The commonest cause is a Bluetooth headset. Bluetooth cannot do
 * high-quality audio and a microphone at the same time: A2DP gives good
 * playback and NO microphone, HFP gives a microphone and poor playback.
 * Windows exposes them as separate endpoints — "Headphones (X)" for A2DP and
 * "Headset (X Hands-Free)" for HFP. Capturing from the A2DP endpoint yields
 * an endless stream of zeros, and the page used to sit there saying
 * "Connected — Arin is ready" while nothing could ever be heard.
 *
 * This says what happened and offers the devices that would actually work.
 */
function startSilenceWatchdog() {
  stopSilenceWatchdog();
  app.sawRealAudio = false;
  app.silenceWatchdog = setTimeout(async () => {
    if (app.sawRealAudio || !isActive()) return;

    const devices = await navigator.mediaDevices.enumerateDevices().catch(() => []);
    const inputs = devices.filter(
      (d) => d.kind === 'audioinput' && d.deviceId && d.deviceId !== 'communications'
    );
    const current = inputs.find((d) => d.deviceId === (el.micSelect && el.micSelect.value));
    const currentLabel = (current && current.label) || 'the selected microphone';

    const alternatives = inputs.filter((d) => d.deviceId !== (el.micSelect && el.micSelect.value));
    // An A2DP endpoint ("Headphones (X)") has no microphone at all, so it can
    // never be the answer. A built-in array is preferred over a hands-free
    // profile, because hands-free drags playback quality down with it.
    const capable = alternatives.filter(
      (d) => !(/headphone/i.test(d.label || '') && !/hands[- ]?free|headset/i.test(d.label || ''))
    );
    const builtIn = capable.find((d) => /array|internal|built[- ]?in|realtek|cam/i.test(d.label || ''));
    const handsFree = capable.find((d) => /hands[- ]?free/i.test(d.label || ''));
    const suggestion = builtIn || handsFree || capable[0];

    /*
     * Switch away from a dead microphone automatically, once.
     *
     * Telling the caller to pick a different device only helps if they read
     * the message and know which one to choose. The page already knows: a
     * Bluetooth A2DP endpoint can never capture, so prefer anything else,
     * and prefer a built-in array over a hands-free profile because
     * hands-free drags playback quality down with it.
     */
    if (!app.autoSwitchTried && suggestion) {
      app.autoSwitchTried = true;
      const label = suggestion.label || 'another microphone';
      showError(
        `No sound was reaching the browser from ${currentLabel}, so Arin is `
        + `switching to "${label}" and reconnecting.`
      );
      setMicHint(`Switched to ${label}.`, null);
      if (el.micSelect) {
        el.micSelect.value = suggestion.deviceId;
        try { localStorage.setItem(MIC_PREF_KEY, suggestion.deviceId); } catch { /* ignore */ }
      }
      app.intentionalClose = true;
      app.autoSwitching = true;
      await teardown();
      // teardown() releases resources but leaves app.state as it was, and
      // isActive() treats any state but idle/error as live — so without this
      // the restart below would early-return and the switch would silently
      // do nothing.
      setState('idle');
      setTimeout(() => { app.autoSwitching = false; startVoiceSession(); }, 400);
      return;
    }

    let message = `No sound at all is reaching the browser from ${currentLabel}. `;

    if (/bluetooth|headphone/i.test(currentLabel)) {
      message +=
        'Bluetooth cannot provide high-quality audio and a microphone at the same '
        + 'time, so a headset used for listening often has no working microphone. ';
    }
    if (suggestion && suggestion.label) {
      message += `Selecting "${suggestion.label}" did not help either. `;
    }
    message +=
      'Check Windows Settings → System → Sound → Input: test the microphone '
      + 'there, make sure it is not muted, and check the mic-mute key on your keyboard.';

    showError(message);
    setMicHint('No audio from any microphone — this is a Windows or hardware issue.', 'bad');
  }, SILENCE_WATCHDOG_MS);
}

function stopSilenceWatchdog() {
  if (app.silenceWatchdog) {
    clearTimeout(app.silenceWatchdog);
    app.silenceWatchdog = null;
  }
}

/** Populate the output picker and say plainly what this browser can do. */
async function refreshOutputDevices() {
  if (!el.outputSelect) return;

  const support = audioOutput.detectSupport();
  const devices = await audioOutput.list();
  const labelled = devices.filter((d) => d.label);
  const headphones = labelled.filter((d) => audioOutput.looksLikeHeadphones(d.label));

  const previous = el.outputSelect.value
    || (() => { try { return localStorage.getItem(OUTPUT_PREF_KEY) || ''; } catch { return ''; } })();

  el.outputSelect.innerHTML = '';
  const fallback = document.createElement('option');
  fallback.value = '';
  fallback.textContent = 'System default';
  el.outputSelect.appendChild(fallback);

  devices.forEach((device, index) => {
    if (device.deviceId === 'default' || device.deviceId === 'communications') return;
    const option = document.createElement('option');
    option.value = device.deviceId;
    option.textContent = device.label || `Output ${index + 1}`;
    el.outputSelect.appendChild(option);
  });

  if (previous && el.outputSelect.querySelector(`option[value="${CSS.escape(previous)}"]`)) {
    el.outputSelect.value = previous;
  }

  const unsupported = support === 'unsupported';
  el.outputSelect.disabled = unsupported;

  if (unsupported) {
    el.outputStatus.textContent = 'This browser cannot choose an output device.';
    el.outputStatus.className = 'output-status output-status--warn';
    el.outputNote.textContent =
      'Arin will play through whatever your system has selected. Set your headphones '
      + 'as the default output device in your operating system instead.';
    return;
  }

  if (!labelled.length) {
    el.outputStatus.textContent = 'Device names appear after you allow the microphone once.';
    el.outputStatus.className = 'output-status';
    el.outputNote.textContent = 'Press Start Voice Assistant, then come back to pick an output.';
    return;
  }

  if (headphones.length) {
    el.outputStatus.textContent = `🎧 Headphones detected — ${headphones[0].label}`;
    el.outputStatus.className = 'output-status output-status--good';
  } else {
    el.outputStatus.textContent = '🔈 No headphones detected — using a speaker.';
    el.outputStatus.className = 'output-status output-status--warn';
  }
  el.outputNote.textContent =
    support === 'context'
      ? "Arin's voice is routed to the device you pick here."
      : "Arin's voice is routed through a media element to the device you pick here.";
}

/** Play a short sample so the caller can confirm they hear Arin. */
async function testVoiceOutput() {
  const button = $('#test-voice-button');
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Playing…';
  let ctx;
  try {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === 'suspended') await ctx.resume();
    const sink = await audioOutput.attach(ctx, el.outputSelect ? el.outputSelect.value : '');
    const player = new AudioPlayer(ctx, sink);

    const sample = app.config && app.config.hindi_voice_available
      ? 'नमस्ते, मैं आरिन हूँ। क्या आप मुझे सुन पा रहे हैं?'
      : 'Hello, this is Arin. Can you hear me clearly?';
    const body = await apiPost('/api/speech/hindi', { text: sample });
    player.enqueue(base64ToInt16(body.audio));
    await new Promise((r) => setTimeout(r, (body.seconds + 0.6) * 1000));
    setMicHint('If you did not hear that, pick a different output device above.', null);
  } catch (err) {
    setMicHint(`Could not play the test: ${err.message}`, 'bad');
  } finally {
    audioOutput.detach();
    if (ctx) ctx.close().catch(() => {});
    button.disabled = false;
    button.textContent = original;
  }
}

function setMicHint(message, tone) {
  if (!el.micLevelHint) return;
  if (!message) {
    el.micLevelHint.hidden = true;
    return;
  }
  el.micLevelHint.textContent = message;
  el.micLevelHint.hidden = false;
  el.micLevelHint.className = `mic-picker__hint${tone ? ` mic-picker__hint--${tone}` : ''}`;
}

function setStatus(key) {
  const state = STATUS[key] || STATUS.idle;
  el.statusText.textContent = state.text;
  el.statusDot.className = `status-dot status-dot--${state.dot}`;
  el.micButton.className = `mic-button ${state.mic}`.trim();
  if (app.emergencyActive) el.micButton.classList.add('is-emergency');

  const active = key !== 'idle' && key !== 'error';
  el.startButtonText.textContent = active ? 'End Voice Assistant' : 'Start Voice Assistant';
  el.startButton.classList.toggle('btn--primary', !active);
  el.startButton.classList.toggle('btn--danger-soft', active);
  el.micButtonLabel.textContent = active ? 'End voice assistant' : 'Start voice assistant';
  el.micButton.setAttribute('aria-pressed', String(active));
}

function showError(message) {
  el.errorBox.textContent = message;
  el.errorBox.hidden = false;
  el.reconnectButton.hidden = false;
}

function clearError() {
  el.errorBox.hidden = true;
  el.errorBox.textContent = '';
  el.reconnectButton.hidden = true;
}

function showToolActivity(label) {
  el.toolActivity.textContent = label;
  el.toolActivity.hidden = false;
}

function hideToolActivity() {
  el.toolActivity.hidden = true;
}

/* ------------------------------------------------------------------ *
 * Transcript
 * ------------------------------------------------------------------ */

const transcriptState = { userPartial: null, agentPartial: null };

function addBubble(kind, text, { partial = false } = {}) {
  if (el.transcriptEmpty) { el.transcriptEmpty.remove(); el.transcriptEmpty = null; }

  const node = document.createElement('div');
  node.className = `bubble bubble--${kind}${partial ? ' bubble--partial' : ''}`;

  if (kind === 'user' || kind === 'agent') {
    const who = document.createElement('span');
    who.className = 'bubble__who';
    who.textContent = kind === 'user' ? 'You' : 'AI';
    node.appendChild(who);
    const body = document.createElement('span');
    body.className = 'bubble__text';
    body.textContent = text;
    node.appendChild(body);
  } else {
    node.textContent = text;
  }

  el.transcript.appendChild(node);
  el.transcript.scrollTop = el.transcript.scrollHeight;
  return node;
}

function updateBubbleText(node, text) {
  if (!node) return;
  const body = node.querySelector('.bubble__text');
  if (body) body.textContent = text;
  else node.textContent = text;
  el.transcript.scrollTop = el.transcript.scrollHeight;
}

function finalizeBubble(node, text) {
  if (!node) return;
  node.classList.remove('bubble--partial');
  if (text !== undefined) updateBubbleText(node, text);
}

function clearTranscript() {
  el.transcript.innerHTML = '';
  transcriptState.userPartial = null;
  transcriptState.agentPartial = null;
  const empty = document.createElement('p');
  empty.className = 'transcript__empty';
  empty.id = 'transcript-empty';
  empty.innerHTML =
    'Your conversation will appear here. Try saying: ' +
    '<em>“I want to book an appointment with a cardiologist tomorrow.”</em> or ' +
    '<em>“मुझे कल डॉक्टर से मिलना है।”</em>';
  el.transcript.appendChild(empty);
  el.transcriptEmpty = empty;
}

/* ------------------------------------------------------------------ *
 * Emergency
 * ------------------------------------------------------------------ */

function showEmergency(payload) {
  app.emergencyActive = true;

  el.emergencyHeadline.textContent = payload.headline || 'EMERGENCY — SEEK IMMEDIATE MEDICAL HELP';
  el.emergencyList.innerHTML = '';
  (payload.instructions || []).forEach((line) => {
    const li = document.createElement('li');
    li.textContent = line;
    el.emergencyList.appendChild(li);
  });

  el.emergencyNote.textContent =
    payload.dispatch_notice ||
    'This assistant cannot send an ambulance. No help has been dispatched — please call.';

  el.emergencyBanner.hidden = false;
  el.micButton.classList.add('is-emergency');
  window.scrollTo({ top: 0, behavior: 'smooth' });

  addBubble('emergency', '🚨 Emergency workflow activated — seek immediate medical help.');
}

function hideEmergency() {
  app.emergencyActive = false;
  el.emergencyBanner.hidden = true;
  el.micButton.classList.remove('is-emergency');
}

async function scanForEmergency(text) {
  if (!text || text.trim().length < 3) return;
  try {
    const result = await apiPost('/api/emergency', {
      transcript: text,
      session_id: app.sessionId || undefined,
      source: 'voice',
    });
    if (result.is_emergency) showEmergency(result);
  } catch {
    /* A failed scan must never break the conversation. */
  }
}

async function triggerEmergencyButton() {
  try {
    const result = await apiPost('/api/emergency', {
      source: 'button',
      session_id: app.sessionId || undefined,
    });
    showEmergency(result);
  } catch (err) {
    showError(`Could not load emergency guidance: ${err.message}`);
  }
}

/* ------------------------------------------------------------------ *
 * Audio playback — sequential PCM16 queue with interrupt support
 * ------------------------------------------------------------------ */

class AudioPlayer {
  /**
   * @param {AudioContext} context
   * @param {MediaStreamAudioDestinationNode|null} sinkNode
   *   When routing to a chosen output device, audio goes here instead of
   *   ctx.destination so an <audio> element can carry it to that device.
   *   Output routing only — the microphone graph is untouched.
   */
  constructor(context, sinkNode = null) {
    this.ctx = context;
    this.cursor = 0;
    this.lastEnqueueAt = 0;   // when audio last arrived, for the echo guard
    this.sources = new Set();
    this.pending = [];
    this.pendingSamples = 0;
    this.pendingTimer = null;
    this.gain = context.createGain();
    this.gain.connect(sinkNode || context.destination);
  }

  /**
   * Queue audio for playback.
   *
   * Incoming chunks are tiny — AssemblyAI streams 10 ms at a time, 1197 of
   * them in a single measured reply. Scheduling each one individually made
   * the voice choppy: whenever a burst arrived late the write cursor fell
   * behind the clock and a gap was stitched into the speech. Measured over
   * one reply that produced 51 gaps totalling 3654 ms of injected silence,
   * with the worst arrival 251 ms late.
   *
   * So chunks are coalesced into larger buffers before being scheduled, and
   * the jitter buffer is deep enough to absorb a late burst rather than
   * punch a hole in the sentence.
   */
  enqueue(int16) {
    if (!int16.length) return;
    this.lastEnqueueAt = Date.now();

    this.pending.push(int16);
    this.pendingSamples += int16.length;

    if (this.pendingSamples >= COALESCE_SAMPLES) {
      this.flushPending();
      return;
    }
    // A partial buffer must not wait forever, or the tail of a sentence
    // would never be spoken.
    if (!this.pendingTimer) {
      this.pendingTimer = setTimeout(() => this.flushPending(), COALESCE_MAX_WAIT_MS);
    }
  }

  /** Concatenate what has accumulated and schedule it as one buffer. */
  flushPending() {
    clearTimeout(this.pendingTimer);
    this.pendingTimer = null;
    if (!this.pendingSamples) return;

    const merged = new Float32Array(this.pendingSamples);
    let offset = 0;
    for (const chunk of this.pending) {
      for (let i = 0; i < chunk.length; i += 1) merged[offset + i] = chunk[i] / 32768;
      offset += chunk.length;
    }
    this.pending = [];
    this.pendingSamples = 0;

    const buffer = this.ctx.createBuffer(1, merged.length, SAMPLE_RATE);
    buffer.copyToChannel(merged, 0);

    const source = this.ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gain);

    /*
     * Continue exactly where the last buffer ended, unless playback has
     * genuinely run dry.
     *
     * `Math.max(now + LEAD, cursor)` looks right but inserts a gap every time
     * the buffer depth merely dips below the lead: the cursor is still in the
     * future, yet the next buffer gets pushed out to now + LEAD, punching a
     * hole of tens of milliseconds into the middle of a word. Measured, that
     * produced 13 micro-gaps of 10-59 ms in a single reply — audible as the
     * voice breaking up.
     *
     * The lead is a STARTUP cushion, so it applies only when there is nothing
     * already scheduled to follow.
     */
    const now = this.ctx.currentTime;
    const startAt = this.cursor > now
      ? this.cursor                              // seamless continuation
      : now + PLAYBACK_LEAD_SECONDS;             // true underrun: rebuild cushion
    source.start(startAt);
    this.cursor = startAt + buffer.duration;

    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
  }

  /** Stop everything already scheduled — used when the caller interrupts. */
  flush() {
    clearTimeout(this.pendingTimer);
    this.pendingTimer = null;
    this.pending = [];
    this.pendingSamples = 0;
    this.sources.forEach((source) => {
      try { source.stop(); } catch { /* already finished */ }
    });
    this.sources.clear();
    this.cursor = 0;
  }

  get isPlaying() {
    return this.ctx.currentTime < this.cursor - 0.05;
  }
}

/* ------------------------------------------------------------------ *
 * Microphone capture worklet (inlined; no extra file to serve)
 * ------------------------------------------------------------------ */

const WORKLET_SOURCE = `
class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.chunkSize = (options.processorOptions && options.processorOptions.chunkSize) || 1200;
    this.buffer = new Float32Array(this.chunkSize);
    this.offset = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i += 1) {
      this.buffer[this.offset] = channel[i];
      this.offset += 1;

      if (this.offset === this.chunkSize) {
        const pcm = new Int16Array(this.chunkSize);
        let peak = 0;
        for (let j = 0; j < this.chunkSize; j += 1) {
          let sample = this.buffer[j];
          if (sample > 1) sample = 1; else if (sample < -1) sample = -1;
          const abs = sample < 0 ? -sample : sample;
          if (abs > peak) peak = abs;
          pcm[j] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
        }
        this.port.postMessage({ pcm, peak }, [pcm.buffer]);
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor('pcm-capture', PCMCaptureProcessor);
`;

/* ------------------------------------------------------------------ *
 * Voice session
 * ------------------------------------------------------------------ */

const app = {
  ws: null,
  audioContext: null,
  player: null,
  mediaStream: null,
  workletNode: null,
  scriptNode: null,
  sourceNode: null,
  sessionId: null,
  state: 'idle',
  emergencyActive: false,
  agentTurnActive: false,
  userSpeaking: false,
  captureRate: SAMPLE_RATE,
  pendingToolResults: [],
  pendingTimer: null,
  analyserTimer: null,
  micTest: null,
  streamSink: null,
  silenceWatchdog: null,
  connecting: false,
  autoSwitchTried: false,
  autoSwitching: false,
  connectTimeout: null,
  sawRealAudio: false,
  hindiSpeaker: null,
  speakerMode: true,   // half-duplex unless headphones are selected
  config: null,
  intentionalClose: false,
};

function isActive() {
  return app.state !== 'idle' && app.state !== 'error';
}

function setState(next) {
  app.state = next;
  setStatus(next);
}

/* --- connect -------------------------------------------------------- */

async function startVoiceSession() {
  /*
   * ONE SESSION, ALWAYS.
   *
   * isActive() only becomes true once the socket is open, so rapid clicks
   * during the connect window each began their own session: several
   * microphone streams, several sockets, several tokens burned. The
   * connecting flag closes that window, and the button is disabled for the
   * duration so the UI matches the rule.
   */
  if (isActive() || app.connecting) return;
  app.connecting = true;
  el.startButton.disabled = true;
  el.micButton.disabled = true;

  timings.start();
  clearError();
  if (!app.autoSwitching) app.autoSwitchTried = false;
  setState('connecting');
  app.intentionalClose = false;

  // Never leave the UI stuck on "Connecting…".
  clearTimeout(app.connectTimeout);
  app.connectTimeout = setTimeout(() => {
    if (isActive()) return;
    showError(
      'Voice connection is taking too long. Please check your network and press Reconnect.'
    );
    setState('error');
    teardown();
  }, CONNECT_TIMEOUT_MS);

  let tokenPromise = null;

  /*
   * Step 1 — microphone permission FIRST.
   *
   * Tokens are single-use and rate limited. Minting one before the microphone
   * is secured means every permission failure burns a token, and a handful of
   * retries trips the limiter into 429s that look like a server fault. Ask for
   * the hardware first; only mint a token once we know we can actually use it.
   */
  try {
    /*
     * On speakers the agent's own voice reaches the microphone, so echo
     * cancellation is essential. On headphones there is no echo path, and
     * leaving AEC on only costs sensitivity — it behaves as a noise gate and
     * can duck a softly spoken caller.
     */
    app.speakerMode = !(el.audioSetup && el.audioSetup.value === 'headphones');
    const audio = {
      echoCancellation: app.speakerMode,
      noiseSuppression: false,
      autoGainControl: true,
    };
    // An explicitly chosen device wins over the operating system default,
    // which is often the wrong microphone on laptops with a headset paired.
    const chosen = el.micSelect && el.micSelect.value;
    if (chosen) audio.deviceId = { exact: chosen };

    /*
     * The token request does NOT depend on the microphone, so start it now
     * and await it later. Previously these ran back to back: the browser
     * finished the permission prompt and device open, and only then did the
     * round trip to our server and on to AssemblyAI begin. Overlapping them
     * removes the whole token latency from the critical path, because it
     * completes while the audio device is still opening.
     *
     * The promise is created before the await below, and the rejection is
     * absorbed here so a token failure cannot surface as an unhandled
     * rejection while the microphone prompt is still open; the real error is
     * handled where the value is consumed.
     */
    const lang = (el.langSelect && el.langSelect.value) || 'auto';
    tokenPromise = apiGet(`/api/voice-token?lang=${encodeURIComponent(lang)}`);
    tokenPromise.catch(() => {});

    app.mediaStream = await navigator.mediaDevices.getUserMedia({ audio });
    timings.mark('microphone');
    // Labels are only readable after permission is granted at least once.
    refreshMicList();
    refreshOutputDevices();
  } catch (err) {
    setState('error');
    const name = err && err.name;
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      showError(
        'Microphone access was blocked. Allow the microphone for this site in your ' +
        'browser’s address-bar permissions, then press Reconnect. On some browsers the ' +
        'microphone only works on localhost or over HTTPS.'
      );
    } else if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
      showError('No microphone was found. Connect a microphone and press Reconnect.');
    } else if (name === 'NotReadableError') {
      showError('Your microphone is in use by another application. Close it and press Reconnect.');
    } else {
      showError(`Microphone could not be started: ${err.message}`);
    }
    return;
  }

  // Step 2 — collect the token that has been in flight since step 1.
  // The permanent AssemblyAI key never leaves the server.
  let credentials;
  try {
    credentials = await tokenPromise;
    timings.mark('token');
  } catch (err) {
    setState('error');
    showError(
      `Could not start the voice assistant: ${err.message} ` +
      'If this is a fresh install, check that the AssemblyAI key is configured in the ' +
      'server’s .env file and restart the server.'
    );
    await teardown();
    return;
  }

  // Step 3 — audio graph.
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    /*
     * Deliberately NOT forcing { sampleRate: 24000 }.
     *
     * When the context is forced to a rate the audio device does not natively
     * run at (Windows shared-mode WASAPI is almost always 48000), Chromium has
     * to resample the microphone into the context — and that path can hand
     * back a silent MediaStreamAudioSourceNode. The session then looks healthy
     * (session.ready, audio chunks going out) but no speech is ever recognised,
     * because every chunk is digital silence.
     *
     * Run the context at the device's native rate instead and do the 24 kHz
     * conversion ourselves in resampleInt16(), which is exact and observable.
     */
    app.audioContext = new AudioContextClass();
    if (app.audioContext.state === 'suspended') await app.audioContext.resume();
    app.captureRate = app.audioContext.sampleRate;
    // Output routing is independent of capture: the caller can speak into
    // the laptop microphone while hearing Arin in a headset.
    const sinkNode = await audioOutput.attach(
      app.audioContext, el.outputSelect ? el.outputSelect.value : ''
    );
    app.player = new AudioPlayer(app.audioContext, sinkNode);
    timings.mark('audioGraph');
    // Hindi sessions are voiced by Sarvam through our own server, because
    // AssemblyAI has no Hindi voice. Only engage it when the server actually
    // has a Sarvam key, otherwise fall back to AssemblyAI's romanised speech.
    const wantsHindi = (el.langSelect && el.langSelect.value) === 'hi';
    app.hindiSpeaker =
      wantsHindi && app.config && app.config.hindi_voice_available
        ? new HindiSpeaker(app.player)
        : null;
  } catch (err) {
    setState('error');
    showError(`Audio could not be initialised: ${err.message}`);
    await teardown();
    return;
  }

  // Step 4 — open the WebSocket with the temporary token.
  const wsUrl = new URL(credentials.websocket_url);
  wsUrl.searchParams.set('token', credentials.token);

  let socket;
  try {
    socket = new WebSocket(wsUrl.toString());
  } catch (err) {
    setState('error');
    showError(`Could not open a voice connection: ${err.message}`);
    await teardown();
    return;
  }
  app.ws = socket;
  socket.binaryType = 'arraybuffer';

  const sessionConfig = credentials.session_config;
  // The token is single-use and short-lived; drop our reference to it now.
  credentials.token = null;

  socket.onopen = () => {
    timings.mark('socketOpen');
    console.log('[DIAG] ws.onopen — sending session.update');
    // The docs require session.update as the first message, before session.ready.
    send({ type: 'session.update', session: sessionConfig });
  };

  socket.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.type === 'session.error' || message.type === 'error') {
      console.log('[DIAG] <<', message.type, JSON.stringify(message));
    } else if (message.type !== 'reply.audio') {
      console.log('[DIAG] <<', message.type);
    }
    handleServerEvent(message);
  };

  socket.onerror = () => {
    if (!app.intentionalClose) {
      showError('The voice connection failed. Press Reconnect to try again.');
    }
  };

  socket.onclose = (event) => {
    console.log('[DIAG] ws.onclose — code:', event.code, 'reason:', event.reason || '(none)');
    const wasActive = isActive();
    teardown();
    setState('idle');
    if (app.intentionalClose) {
      addBubble('system', 'Voice session ended.');
      return;
    }
    if (wasActive) {
      addBubble('system', 'Voice session disconnected.');
      const reason = event.reason ? ` (${event.reason})` : '';
      showError(
        `The voice session ended unexpectedly${reason}. Press Reconnect to start a new session.`
      );
    }
  };
}

function send(payload) {
  if (app.ws && app.ws.readyState === WebSocket.OPEN) {
    app.ws.send(JSON.stringify(payload));
    return true;
  }
  return false;
}

/* --- microphone streaming ------------------------------------------- */

async function startMicrophoneStream() {
  const ctx = app.audioContext;
  // Must happen before createMediaStreamSource: on Windows, Chromium can
  // otherwise hand back a source node that emits nothing but silence.
  app.streamSink = keepStreamAwake(app.mediaStream);

  app.sourceNode = ctx.createMediaStreamSource(app.mediaStream);

  const rate = ctx.sampleRate;
  // ~50 ms of audio at the rate this device ACTUALLY runs at.
  const chunkSamples = Math.round(rate * CHUNK_SECONDS);

  console.log(
    '[DIAG] ctx.sampleRate:', rate,
    rate === SAMPLE_RATE ? '(matches 24000, no resampling)' : `(RESAMPLING ${rate} -> ${SAMPLE_RATE})`,
    '| chunkSamples:', chunkSamples
  );

  // Which input devices does this machine actually expose?
  if (navigator.mediaDevices.enumerateDevices) {
    navigator.mediaDevices.enumerateDevices().then((devices) => {
      const inputs = devices.filter((d) => d.kind === 'audioinput');
      console.log('[DIAG] audio input devices:', inputs.length);
      inputs.forEach((d, i) => console.log(`[DIAG]   [${i}] ${d.label || '(no label)'} — deviceId: ${d.deviceId}`));
    });
  }

  /*
   * Independent check on the RAW stream, bypassing the capture worklet. If the
   * analyser also reads zero, the MediaStream itself is silent and the fault is
   * outside this page entirely (OS mute, device privacy, wrong input).
   */
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  app.sourceNode.connect(analyser);
  const probe = new Float32Array(analyser.fftSize);
  app.analyserTimer = setInterval(() => {
    analyser.getFloatTimeDomainData(probe);
    let peak = 0;
    for (let i = 0; i < probe.length; i += 1) {
      const a = probe[i] < 0 ? -probe[i] : probe[i];
      if (a > peak) peak = a;
    }
    console.log(`[DIAG] RAW stream peak (analyser, pre-worklet): ${peak.toFixed(4)}`);
  }, 1000);

  const track = app.mediaStream.getAudioTracks()[0];
  if (track) {
    console.log(
      '[DIAG] mic track —', JSON.stringify({
        label: track.label,
        enabled: track.enabled,
        muted: track.muted,
        readyState: track.readyState,
        settings: track.getSettings(),
      })
    );
  }

  // Rolling input level. If this stays at 0.000 while you speak, the operating
  // system is handing us a silent stream and nothing else can work.
  let windowPeak = 0;
  let lastLevelLog = 0;
  const reportLevel = (peak) => {
    if (peak > windowPeak) windowPeak = peak;
    const now = Date.now();
    if (now - lastLevelLog >= 1000) {
      lastLevelLog = now;
      console.log(
        `[DIAG] mic level (1s peak): ${windowPeak.toFixed(3)}`,
        windowPeak < 0.005 ? '<-- SILENT' : ''
      );
      if (windowPeak < 0.005) {
        setMicHint('No sound from this microphone. Pick a different one above.', 'bad');
      } else {
        setMicHint(`Hearing you — input level ${(windowPeak * 100).toFixed(0)}%`, 'good');
      }
      windowPeak = 0;
    }
  };

  let loggedFirstChunk = false;
  // Reused so a gated chunk costs no allocation.
  let silence = null;

  /*
   * SOFTWARE AUTOMATIC GAIN.
   *
   * Some laptop microphone arrays deliver a signal far below what speech
   * recognition expects. Measured on one HP OMEN array, the loudest spoken
   * word peaked at 589 of 32767 — about -35 dBFS, roughly seventeen times
   * quieter than normal speech at 0.1 to 0.5 of full scale. The browser's own
   * autoGainControl did not lift it. Recognition then fails silently: audio
   * arrives, but voice activity detection never considers it speech.
   *
   * So measure what this microphone actually produces and scale it up. The
   * peak decays slowly, so gain follows the caller's voice rather than
   * pumping on every syllable, and it is floored at 1 so a healthy
   * microphone is left completely alone.
   */
  const AGC_TARGET_PEAK = 8192;      // 0.25 of full scale
  const AGC_MAX_GAIN = 24;
  const AGC_DECAY = 0.97;
  let agcPeak = 0;
  let loudChunks = 0;
  let noiseFloor = 300;
  let lastGainLogged = 0;

  const measurePeak = (buf) => {
    let peak = 0;
    for (let i = 0; i < buf.length; i += 1) {
      const a = buf[i] < 0 ? -buf[i] : buf[i];
      if (a > peak) peak = a;
    }
    return peak;
  };

  const amplify = (buf, gain) => {
    if (gain <= 1.01) return buf;
    const out = new Int16Array(buf.length);
    for (let i = 0; i < buf.length; i += 1) {
      const v = buf[i] * gain;
      out[i] = v > 32767 ? 32767 : v < -32768 ? -32768 : v;
    }
    return out;
  };

  const pushChunk = (int16) => {
    if (!app.ws || app.ws.readyState !== WebSocket.OPEN) return;

    /*
     * ECHO SUPPRESSION WITH BARGE-IN, ON SPEAKERS.
     *
     * Echo cancellation alone is not enough on a laptop: the agent's voice
     * still leaks into the microphone and the recogniser hears the agent
     * talking over the caller. The symptom is precise — with headphones the
     * caller is understood, without them they are not, because only then is
     * there a speaker-to-microphone path.
     *
     * But muting outright is worse. The agent's reply can be thirty seconds
     * of scheduled audio, and a caller who starts talking during it — which
     * is what people naturally do — is ignored for the whole of it.
     *
     * So suppress by LEVEL, not by time. Residual echo that survives the
     * browser's canceller is quiet; someone speaking near the microphone is
     * not. Below the threshold we send silence (the stream must stay
     * continuous or turn detection reads it as a stalled connection); above
     * it we send the real audio, so the caller can interrupt.
     */
    const peak = measurePeak(int16);
    if (peak > 60) app.sawRealAudio = true;   // anything above dead silence

    // Follow the loudest recent sound, and separately track how quiet this
    // microphone gets, so both gain and the gate adapt to the hardware.
    agcPeak = Math.max(peak, agcPeak * AGC_DECAY);
    if (peak < noiseFloor) noiseFloor = noiseFloor * 0.9 + peak * 0.1;
    else noiseFloor = noiseFloor * 0.999 + peak * 0.001;

    const gain = Math.min(
      AGC_MAX_GAIN, Math.max(1, AGC_TARGET_PEAK / Math.max(agcPeak, 1))
    );

    if (!agentIsAudible(MIC_GATE_TAIL_MS)) loudChunks = 0;

    if (app.speakerMode && agentIsAudible(MIC_GATE_TAIL_MS)) {
      /*
       * The barge-in threshold must be RELATIVE to this microphone, not a
       * fixed number. A fixed 3000 sat five times above the loudest word a
       * quiet array produced, so on that hardware the caller was silenced
       * every time Arin spoke. Six times the noise floor separates speech
       * from room tone on both loud and quiet microphones.
       */
      const bargeIn = Math.max(noiseFloor * 6, BARGE_IN_FLOOR);

      /*
       * A single loud chunk is not an interruption.
       *
       * The floor collapses toward zero on a quiet microphone, so one
       * keyboard tap or breath used to clear it — and because barge-in also
       * calls hindiSpeaker.cancel(), which bumps the generation counter,
       * every Hindi sentence still being synthesised was discarded for good.
       * English recovered because AssemblyAI keeps streaming; Hindi could
       * not, so it simply went quiet mid-reply.
       *
       * Real speech sustains. Require it across several consecutive chunks
       * (~200 ms) before cutting Arin off.
       */
      loudChunks = peak >= bargeIn ? loudChunks + 1 : 0;
      if (loudChunks < BARGE_IN_CHUNKS) {
        const wanted = Math.floor(int16.length / (rate / SAMPLE_RATE));
        if (!silence || silence.length !== wanted) silence = new Int16Array(wanted);
        send({ type: 'input.audio', audio: int16ToBase64(silence) });
        return;
      }
      // Loud enough to be the caller, not leakage: stop the agent and let
      // this through, so the interruption is heard from its first word.
      if (app.player) app.player.flush();
      if (app.hindiSpeaker) app.hindiSpeaker.cancel();
    }

    // Amplify before resampling so the interpolation works on the louder
    // signal, then resample to the 24 kHz the API expects.
    const outgoing = resampleInt16(amplify(int16, gain), rate, SAMPLE_RATE);

    if (!loggedFirstChunk) {
      loggedFirstChunk = true;
      console.log(
        '[DIAG] first input.audio chunk — captured samples:', int16.length,
        '| sent samples:', outgoing.length, '| bytes:', outgoing.byteLength
      );
    }
    // Report gain occasionally: a persistently high figure means the
    // microphone is very quiet, which is worth knowing.
    if (gain > 1.5 && Date.now() - lastGainLogged > 3000) {
      lastGainLogged = Date.now();
      console.log(
        `[DIAG] mic gain x${gain.toFixed(1)} (raw peak ${peak}, noise floor ${Math.round(noiseFloor)})`
      );
    }
    send({ type: 'input.audio', audio: int16ToBase64(outgoing) });
  };

  if (ctx.audioWorklet) {
    try {
      const blob = new Blob([WORKLET_SOURCE], { type: 'application/javascript' });
      const url = URL.createObjectURL(blob);
      await ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);

      app.workletNode = new AudioWorkletNode(ctx, 'pcm-capture', {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        processorOptions: { chunkSize: chunkSamples },
      });
      app.workletNode.port.onmessage = (event) => {
        reportLevel(event.data.peak || 0);
        pushChunk(event.data.pcm);
      };
      app.sourceNode.connect(app.workletNode);
      const silentSink = ctx.createGain();
      silentSink.gain.value = 0;
      app.workletNode.connect(silentSink);
      silentSink.connect(ctx.destination);
      return;
    } catch {
      /* Fall through to the ScriptProcessor path below. */
    }
  }

  // Fallback for browsers without AudioWorklet. Deprecated but widely supported.
  const processor = ctx.createScriptProcessor(4096, 1, 1);
  app.scriptNode = processor;
  processor.onaudioprocess = (event) => {
    const input = event.inputBuffer.getChannelData(0);
    const pcm = new Int16Array(input.length);
    let peak = 0;
    for (let i = 0; i < input.length; i += 1) {
      const clamped = Math.max(-1, Math.min(1, input[i]));
      const abs = clamped < 0 ? -clamped : clamped;
      if (abs > peak) peak = abs;
      pcm[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }
    reportLevel(peak);
    pushChunk(pcm);
  };
  app.sourceNode.connect(processor);
  // A muted sink keeps the ScriptProcessor running without audible feedback.
  const sink = ctx.createGain();
  sink.gain.value = 0;
  processor.connect(sink);
  sink.connect(ctx.destination);
}

/* --- server events --------------------------------------------------- */

function handleServerEvent(message) {
  switch (message.type) {

    case 'session.ready': {
      timings.mark('sessionReady');
      timings.report('connect');
      clearTimeout(app.connectTimeout);
      app.connectTimeout = null;
      // The connect attempt succeeded, so release the lock here as well as in
      // teardown — teardown does not run on the happy path, and leaving the
      // button disabled would strand the caller with no way to end the call.
      app.connecting = false;
      if (el.startButton) el.startButton.disabled = false;
      if (el.micButton) el.micButton.disabled = false;
      startSilenceWatchdog();
      app.sessionId = message.session_id || null;
      setState('connected');
      clearError();
      addBubble('system', 'Connected to Varanasi Hospital AI. You can start speaking.');
      startMicrophoneStream().catch((err) => {
        showError(`Microphone streaming failed: ${err.message}`);
      });
      break;
    }

    case 'session.updated':
      break;

    case 'input.speech.started':
      app.userSpeaking = true;
      /*
       * Voice activity alone is NOT proof the caller interrupted. On laptop
       * speakers the agent's own voice leaks into the microphone and trips
       * this event, so flushing here unconditionally makes the agent cut
       * itself off mid-sentence, over and over.
       *
       * The guard must be anchored to when audio is actually AUDIBLE, not to
       * reply.started. Hindi is voiced by Sarvam over HTTP, so speech begins
       * a round-trip after the turn starts — a guard measured from
       * reply.started has already expired by then, and at session start
       * replyStartedAt is 0, so any room noise discarded the whole greeting
       * before it had even been synthesised.
       *
       * A genuine interruption still gets through via transcript.user.delta
       * below, where real WORDS were recognised.
       */
      if (app.player && !agentIsAudible()) {
        app.player.flush();
      }
      setState('listening');
      break;

    case 'input.speech.stopped':
      app.userSpeaking = false;
      // The caller has stopped and the reply has not begun: that gap is
      // Arin thinking, and saying so beats a silent, apparently dead UI.
      if (!app.agentTurnActive && isActive()) setState('thinking');
      flushPendingToolResults();
      break;

    case 'transcript.user.delta': {
      // `text` is the full transcript so far, not an increment.
      const text = message.text || '';
      if (!text) break;
      // Recognised words mean a real person is talking, not speaker bleed.
      // This is the trustworthy interruption signal, so flush playback here.
      if (app.player && app.agentTurnActive) {
        app.player.flush();
        if (app.hindiSpeaker) app.hindiSpeaker.cancel();
      }
      if (!transcriptState.userPartial) {
        transcriptState.userPartial = addBubble('user', text, { partial: true });
      } else {
        updateBubbleText(transcriptState.userPartial, text);
      }
      break;
    }

    case 'transcript.user': {
      const text = message.text || '';
      if (transcriptState.userPartial) {
        finalizeBubble(transcriptState.userPartial, text);
        transcriptState.userPartial = null;
      } else if (text) {
        addBubble('user', text);
      }
      // Independent keyword safety net, in addition to the agent's own judgement.
      scanForEmergency(text);
      break;
    }

    case 'reply.started':
      timings.mark('replyStarted');
      app.agentTurnActive = true;
      setState('speaking');
      break;

    case 'reply.audio': {
      // In a Hindi session AssemblyAI's English voice is not used at all;
      // Sarvam speaks the reply instead, driven by transcript.agent.delta.
      if (app.hindiSpeaker) break;
      const encoded = message.data || message.audio;
      if (!encoded || !app.player) break;
      try {
        app.player.enqueue(base64ToInt16(encoded));
      } catch {
        /* A malformed chunk should not end the call. */
      }
      break;
    }

    case 'transcript.agent.delta': {
      const word = message.delta || '';
      if (!word) break;
      if (app.hindiSpeaker) {
        const separator = /^[\s.,!?;:।]/.test(word) ? '' : ' ';
        app.hindiSpeaker.push(separator + word);
      }
      if (!transcriptState.agentPartial) {
        transcriptState.agentPartial = addBubble('agent', word, { partial: true });
      } else {
        const body = transcriptState.agentPartial.querySelector('.bubble__text');
        const separator = /^[\s.,!?;:।]/.test(word) ? '' : ' ';
        updateBubbleText(
          transcriptState.agentPartial,
          (body ? body.textContent : '') + separator + word
        );
      }
      break;
    }

    case 'transcript.agent': {
      const text = message.text || '';
      if (transcriptState.agentPartial) {
        finalizeBubble(transcriptState.agentPartial, text || undefined);
        transcriptState.agentPartial = null;
      } else if (text) {
        addBubble('agent', text);
      }
      break;
    }

    case 'reply.done':
      app.agentTurnActive = false;
      if (app.hindiSpeaker) app.hindiSpeaker.flush();
      if (transcriptState.agentPartial) {
        finalizeBubble(transcriptState.agentPartial);
        transcriptState.agentPartial = null;
      }
      setState(isActive() ? 'connected' : 'idle');
      // The docs: send tool.result when reply.done is the latest event.
      flushPendingToolResults();
      break;

    case 'tool.call':
      handleToolCall(message);
      break;

    case 'session.error':
    case 'error': {
      const detail = message.message || message.code || 'Unknown voice service error';
      showError(`Voice service error: ${detail}`);
      break;
    }

    case 'session.ended':
      app.intentionalClose = true;
      break;

    default:
      break;
  }
}

/* --- tools ----------------------------------------------------------- */

const TOOL_LABELS = {
  search_doctors: 'Looking up doctors…',
  get_doctor_details: 'Fetching doctor details…',
  check_appointment_availability: 'Checking availability…',
  book_appointment: 'Booking the appointment…',
  reschedule_appointment: 'Rescheduling…',
  cancel_appointment: 'Cancelling…',
  get_appointment_details: 'Finding the appointment…',
  get_department_information: 'Looking up the department…',
  get_hospital_information: 'Fetching hospital information…',
  emergency_assistance: 'Starting emergency workflow…',
  human_handoff: 'Contacting hospital staff…',
};

async function handleToolCall(message) {
  const { call_id: callId, name } = message;
  console.log('[DIAG] tool.call:', name, '| call_id:', callId, '| arguments:', JSON.stringify(message.arguments || {}));
  showToolActivity(TOOL_LABELS[name] || `Running ${name}…`);

  let result;
  try {
    // The browser is only a relay. The server decides what every tool does.
    const response = await apiPost('/api/tools/execute', {
      name,
      arguments: message.arguments || {},
      call_id: callId,
      session_id: app.sessionId || undefined,
    });
    result = response.result;
  } catch (err) {
    result = {
      success: false,
      message:
        'The hospital system could not be reached. Do not claim anything was ' +
        'done. Offer to connect the caller with hospital staff.',
      error: String(err.message).slice(0, 200),
    };
  }

  hideToolActivity();

  if (name === 'emergency_assistance' && result && result.is_emergency) {
    showEmergency(result);
  }
  if (name === 'book_appointment' && result && result.success) {
    loadAppointments();
  }
  if ((name === 'cancel_appointment' || name === 'reschedule_appointment') && result && result.success) {
    loadAppointments();
  }

  queueToolResult({ type: 'tool.result', call_id: callId, result: JSON.stringify(result) });
}

function queueToolResult(payload) {
  app.pendingToolResults.push(payload);

  if (!app.agentTurnActive && !app.userSpeaking) {
    flushPendingToolResults();
    return;
  }
  // Safety valve: never let a stuck turn strand a tool result forever.
  if (!app.pendingTimer) {
    app.pendingTimer = setTimeout(() => {
      app.pendingTimer = null;
      flushPendingToolResults(true);
    }, TOOL_RESULT_MAX_HOLD_MS);
  }
}

function flushPendingToolResults(force = false) {
  if (!app.pendingToolResults.length) return;
  if (!force && (app.agentTurnActive || app.userSpeaking)) return;

  if (app.pendingTimer) {
    clearTimeout(app.pendingTimer);
    app.pendingTimer = null;
  }
  const queued = app.pendingToolResults.splice(0);
  queued.forEach((payload) => {
    console.log(
      '[DIAG] >> tool.result | call_id:', payload.call_id,
      '| result typeof:', typeof payload.result,
      '| forced:', force,
      '| result:', String(payload.result).slice(0, 200)
    );
    send(payload);
  });
}

/* --- teardown -------------------------------------------------------- */

function endVoiceSession() {
  if (!isActive() && !app.ws) return;
  app.intentionalClose = true;
  // The docs: send session.end before closing, or the session stays billable.
  send({ type: 'session.end' });
  setTimeout(() => {
    if (app.ws && app.ws.readyState === WebSocket.OPEN) app.ws.close(1000, 'client ended');
  }, 220);
}

async function teardown() {
  if (app.pendingTimer) { clearTimeout(app.pendingTimer); app.pendingTimer = null; }
  if (app.analyserTimer) { clearInterval(app.analyserTimer); app.analyserTimer = null; }
  if (app.connectTimeout) { clearTimeout(app.connectTimeout); app.connectTimeout = null; }
  stopSilenceWatchdog();
  app.connecting = false;
  if (el.startButton) el.startButton.disabled = false;
  if (el.micButton) el.micButton.disabled = false;
  if (app.streamSink) {
    try { app.streamSink.pause(); app.streamSink.srcObject = null; } catch { /* noop */ }
    app.streamSink = null;
  }
  app.pendingToolResults = [];
  app.agentTurnActive = false;
  app.userSpeaking = false;
  app.sessionId = null;
  hideToolActivity();

  if (app.hindiSpeaker) { app.hindiSpeaker.cancel(); app.hindiSpeaker = null; }
  audioOutput.detach();
  if (app.player) { app.player.flush(); app.player = null; }

  if (app.workletNode) {
    try { app.workletNode.port.onmessage = null; app.workletNode.disconnect(); } catch { /* noop */ }
    app.workletNode = null;
  }
  if (app.scriptNode) {
    try { app.scriptNode.onaudioprocess = null; app.scriptNode.disconnect(); } catch { /* noop */ }
    app.scriptNode = null;
  }
  if (app.sourceNode) {
    try { app.sourceNode.disconnect(); } catch { /* noop */ }
    app.sourceNode = null;
  }
  if (app.mediaStream) {
    app.mediaStream.getTracks().forEach((track) => track.stop());
    app.mediaStream = null;
  }
  if (app.audioContext) {
    try { await app.audioContext.close(); } catch { /* noop */ }
    app.audioContext = null;
  }
  if (app.ws) {
    app.ws.onopen = app.ws.onmessage = app.ws.onerror = app.ws.onclose = null;
    if (app.ws.readyState === WebSocket.OPEN || app.ws.readyState === WebSocket.CONNECTING) {
      try { app.ws.close(); } catch { /* noop */ }
    }
    app.ws = null;
  }

  transcriptState.userPartial = null;
  transcriptState.agentPartial = null;
}

function toggleVoiceSession() {
  if (isActive()) endVoiceSession();
  else startVoiceSession();
}

// The docs ask for a synchronous session.end on page navigation.
window.addEventListener('pagehide', () => {
  if (app.ws && app.ws.readyState === WebSocket.OPEN) {
    try {
      app.ws.send(JSON.stringify({ type: 'session.end' }));
      app.ws.close();
    } catch { /* nothing more we can do while unloading */ }
  }
});

/* ==================================================================== *
 * Non-voice UI — quick actions, booking form, lookups
 * ==================================================================== */

const cache = { doctors: [], departments: [] };
let lastFocused = null;

function openModal(id) {
  lastFocused = document.activeElement;
  const modal = document.getElementById(id);
  if (!modal) return;
  modal.hidden = false;
  document.body.style.overflow = 'hidden';
  const focusable = modal.querySelector(
    'input:not([type="hidden"]), select, textarea, button:not(.modal__close)'
  );
  (focusable || modal.querySelector('.modal__close')).focus();
}

function closeModal() {
  $$('.modal').forEach((modal) => { modal.hidden = true; });
  document.body.style.overflow = '';
  if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
}

/* Keep Tab inside an open dialog. */
document.addEventListener('keydown', (event) => {
  const open = $$('.modal').find((m) => !m.hidden);
  if (!open) return;

  if (event.key === 'Escape') { closeModal(); return; }
  if (event.key !== 'Tab') return;

  const items = Array.from(
    open.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea')
  ).filter((node) => node.offsetParent !== null);
  if (!items.length) return;

  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first.focus();
  }
});

/* --- reference data --------------------------------------------------- */

async function loadReferenceData() {
  try {
    const [doctorsRes, departmentsRes, hospitalRes] = await Promise.all([
      apiGet('/api/doctors'),
      apiGet('/api/departments'),
      apiGet('/api/hospital'),
    ]);
    cache.doctors = doctorsRes.doctors || [];
    cache.departments = departmentsRes.departments || [];
    populateDepartmentSelect();
    populateDoctorSelect();
    renderHospitalInfo(hospitalRes.hospital);
  } catch (err) {
    showError(`Could not load hospital data: ${err.message}`);
  }
}

function renderHospitalInfo(info) {
  if (!info) return;
  const rows = [
    ['Emergency', `${info.emergency} — walk-in`],
    ['Outpatient', info.outpatient],
    ['Visiting', info.visiting_hours],
    ['Pharmacy', info.pharmacy_hours],
    ['Assistant', info.voice_assistant],
  ];
  el.hospitalInfo.innerHTML = rows
    .map(([term, value]) =>
      `<div class="info-list__row"><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`)
    .join('');
}

function populateDepartmentSelect() {
  const select = $('#bk-department');
  if (!select) return;
  select.innerHTML = '<option value="">Any department</option>';
  cache.departments.forEach((dept) => {
    const option = document.createElement('option');
    option.value = dept.name;
    option.textContent = dept.name;
    select.appendChild(option);
  });
}

function populateDoctorSelect(department = '') {
  const select = $('#bk-doctor');
  if (!select) return;
  const list = department
    ? cache.doctors.filter((d) => d.department === department)
    : cache.doctors;

  select.innerHTML = '<option value="">Select a doctor</option>';
  list
    .filter((d) => !d.walk_in_only)
    .forEach((doctor) => {
      const option = document.createElement('option');
      option.value = doctor.id;
      option.textContent = `${doctor.name} — ${doctor.specialization}`;
      select.appendChild(option);
    });
}

/* --- availability ----------------------------------------------------- */

async function refreshSlots() {
  const doctorId = $('#bk-doctor').value;
  const date = $('#bk-date').value;
  const select = $('#bk-time');

  if (!doctorId || !date) {
    select.innerHTML = '<option value="">Choose a doctor and date</option>';
    select.disabled = true;
    return;
  }

  select.disabled = true;
  select.innerHTML = '<option value="">Checking availability…</option>';

  try {
    const result = await apiPost('/api/appointments/check', { date, doctor_id: doctorId });
    const entry = result.data && result.data.doctors && result.data.doctors[0];
    const slots = (entry && entry.available_slots) || [];

    if (!slots.length) {
      const note = (entry && entry.note) || 'No free slots on this date.';
      select.innerHTML = `<option value="">${escapeHtml(note)}</option>`;
      select.disabled = true;
      return;
    }

    select.innerHTML = '<option value="">Select a time</option>';
    slots.forEach((slot) => {
      const option = document.createElement('option');
      option.value = slot;
      option.textContent = to12Hour(slot);
      select.appendChild(option);
    });
    select.disabled = false;
  } catch (err) {
    select.innerHTML = `<option value="">${escapeHtml(err.message)}</option>`;
    select.disabled = true;
  }
}

/* --- booking form ------------------------------------------------------ */

async function submitBooking(event) {
  event.preventDefault();
  const form = event.target;
  const submit = $('#booking-submit');
  const box = $('#booking-result');

  if (!form.reportValidity()) return;

  submit.disabled = true;
  submit.textContent = 'Booking…';
  box.hidden = true;

  try {
    const result = await apiPost('/api/appointments/book', {
      patient_name: $('#bk-name').value.trim(),
      patient_phone: $('#bk-phone').value.trim(),
      doctor_id: $('#bk-doctor').value,
      date: $('#bk-date').value,
      time: $('#bk-time').value,
    });

    box.hidden = false;
    if (result.success) {
      const appt = result.data.appointment;
      box.className = 'result-box result-box--ok';
      box.innerHTML =
        `<strong>✅ Appointment confirmed</strong>` +
        `${escapeHtml(appt.doctor_name)} · ${escapeHtml(appt.department)}<br>` +
        `${escapeHtml(formatDate(appt.date))} at ${escapeHtml(to12Hour(appt.time))}<br>` +
        `Reference: <code>${escapeHtml(appt.appointment_id)}</code>`;
      // Read the number off the confirmed record before the form is cleared.
      const bookedPhone = appt.patient_phone || myPhone;
      form.reset();
      $('#bk-time').innerHTML = '<option value="">Choose a doctor and date</option>';
      loadAppointments(bookedPhone);
    } else {
      box.className = 'result-box result-box--error';
      box.innerHTML = `<strong>Not booked</strong>${escapeHtml(result.message)}`;
    }
  } catch (err) {
    box.hidden = false;
    box.className = 'result-box result-box--error';
    box.innerHTML = `<strong>Not booked</strong>${escapeHtml(err.message)}`;
  } finally {
    submit.disabled = false;
    submit.textContent = 'Confirm booking';
  }
}

/* --- doctor search ------------------------------------------------------ */

function renderDoctors(list, container) {
  if (!list.length) {
    container.innerHTML = '<p class="muted">No matching doctor found in the hospital records.</p>';
    return;
  }
  container.innerHTML = list.map((doctor) => `
    <article class="result-card">
      <div class="result-card__title">${escapeHtml(doctor.name)}</div>
      <div class="result-card__sub">${escapeHtml(doctor.specialization)} · ${escapeHtml(doctor.department)}</div>
      <div class="result-card__meta">
        ${doctor.qualification ? `${escapeHtml(doctor.qualification)}<br>` : ''}
        <strong>Days:</strong> ${escapeHtml(doctor.available_days.join(', '))}<br>
        <strong>Timings:</strong> ${escapeHtml(doctor.timings)}
        ${doctor.consultation_fee_inr ? `<br><strong>Fee:</strong> ₹${escapeHtml(doctor.consultation_fee_inr)}` : ''}
        ${doctor.room ? `<br><strong>Room:</strong> ${escapeHtml(doctor.room)}` : ''}
      </div>
      ${(doctor.languages || []).map((l) => `<span class="tag">${escapeHtml(l)}</span>`).join('')}
      ${doctor.walk_in_only ? '<span class="tag tag--walkin">Walk-in only</span>' : ''}
    </article>
  `).join('');
}

let searchTimer = null;

async function searchDoctors(term) {
  const container = $('#doctor-results');
  if (!term || !term.trim()) {
    renderDoctors(cache.doctors, container);
    return;
  }
  try {
    const result = await apiGet(`/api/doctors?q=${encodeURIComponent(term.trim())}`);
    renderDoctors(result.doctors || [], container);
  } catch (err) {
    container.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
}

function renderDepartments() {
  const container = $('#department-results');
  container.innerHTML = cache.departments.map((dept) => {
    const doctors = cache.doctors.filter((d) => d.department === dept.name);
    return `
      <article class="result-card">
        <div class="result-card__title">${escapeHtml(dept.name)}</div>
        <div class="result-card__sub">${escapeHtml(dept.timings)} · ${escapeHtml(dept.floor || '')}</div>
        <div class="result-card__meta">
          ${escapeHtml(dept.description)}
          ${doctors.length ? `<br><strong>Doctors:</strong> ${escapeHtml(doctors.map((d) => d.name).join(', '))}` : ''}
        </div>
        ${dept.walk_in_only ? '<span class="tag tag--walkin">Walk-in only</span>' : ''}
      </article>
    `;
  }).join('');
}

/* --- appointments panel -------------------------------------------------- */

/**
 * Show the caller their OWN appointments, looked up by phone number.
 *
 * This panel used to call /api/appointments, which returned the hospital's
 * entire register — every patient's name, phone number and visit time — to
 * anyone who opened the page. It now goes through the verified lookup, so a
 * caller sees only the bookings made with the number they can produce.
 */
let myPhone = '';

async function loadAppointments(phone) {
  // Handlers may call this with an Event, so only trust a real string.
  if (typeof phone === 'string' && phone.trim()) myPhone = phone.trim();
  const number = myPhone;
  if (!number) {
    el.appointmentsList.innerHTML =
      '<p class="muted small">Enter your phone number above to see your appointments.</p>';
    return;
  }

  el.appointmentsList.innerHTML = '<p class="muted small">Looking…</p>';
  try {
    const result = await apiPost('/api/appointments/lookup', { patient_phone: number });
    if (!result.success) {
      el.appointmentsList.innerHTML =
        `<p class="muted small">${escapeHtml(result.message)}</p>`;
      return;
    }
    const data = result.data || {};
    const list = data.appointments || (data.appointment ? [data.appointment] : []);
    if (!list.length) {
      el.appointmentsList.innerHTML =
        '<p class="muted small">No appointments found for that number.</p>';
      return;
    }
    el.appointmentsList.innerHTML = list.map((appt) => `
      <div class="appt${appt.status === 'cancelled' ? ' appt--cancelled' : ''}">
        <div class="appt__top">
          <span class="appt__doctor">${escapeHtml(appt.doctor_name)}</span>
          <span class="appt__ref">${escapeHtml(appt.appointment_id)}</span>
        </div>
        <div class="appt__meta">
          ${escapeHtml(appt.department)} · ${escapeHtml(formatDate(appt.date))} at ${escapeHtml(to12Hour(appt.time))}<br>
          ${escapeHtml(appt.patient_name)}
        </div>
        <span class="appt__status${appt.status === 'cancelled' ? ' appt__status--cancelled' : ''}">
          ${escapeHtml(appt.status)}
        </span>
      </div>
    `).join('');
  } catch (err) {
    el.appointmentsList.innerHTML = `<p class="muted small">${escapeHtml(err.message)}</p>`;
  }
}

/* --- lookup -------------------------------------------------------------- */

async function submitLookup(event) {
  event.preventDefault();
  const box = $('#lookup-result');
  const reference = $('#lk-reference').value.trim();
  const phone = $('#lk-phone').value.trim();

  if (!reference && !phone) {
    box.hidden = false;
    box.className = 'result-box result-box--error';
    box.textContent = 'Enter an appointment reference or the phone number used for booking.';
    return;
  }

  try {
    const result = await apiPost('/api/appointments/lookup', {
      appointment_id: reference || null,
      patient_phone: phone || null,
    });
    box.hidden = false;
    if (!result.success) {
      box.className = 'result-box result-box--error';
      box.textContent = result.message;
      return;
    }
    const found = result.data.appointments || [result.data.appointment];
    box.className = 'result-box result-box--ok';
    box.innerHTML = found.map((appt) =>
      `<strong>${escapeHtml(appt.doctor_name)} · ${escapeHtml(appt.status)}</strong>` +
      `${escapeHtml(formatDate(appt.date))} at ${escapeHtml(to12Hour(appt.time))}<br>` +
      `Reference: <code>${escapeHtml(appt.appointment_id)}</code>`
    ).join('<hr style="border:0;border-top:1px solid currentColor;opacity:.2;margin:.6rem 0">');
  } catch (err) {
    box.hidden = false;
    box.className = 'result-box result-box--error';
    box.textContent = err.message;
  }
}

/* --- handoff -------------------------------------------------------------- */

async function requestHandoff(reason = 'patient_request') {
  const box = $('#handoff-result');
  openModal('modal-handoff');
  box.className = 'result-box';
  box.innerHTML = '<p>Requesting a hospital staff member…</p>';

  try {
    const result = await apiPost('/api/handoff', {
      reason,
      session_id: app.sessionId || undefined,
    });
    box.className = 'result-box result-box--ok';
    box.innerHTML =
      `<strong>${escapeHtml(result.message)}</strong>` +
      `Reference: <code>${escapeHtml(result.handoff_id)}</code><br>` +
      `Queue position: ${escapeHtml(result.queue_position)} · ` +
      `estimated wait ${escapeHtml(result.estimated_wait_minutes)} min` +
      `<p class="small" style="margin-top:.7rem;opacity:.85">${escapeHtml(result.notice)}</p>`;
    addBubble('system', `Escalated to hospital staff — ${result.handoff_id} (demo).`);
  } catch (err) {
    box.className = 'result-box result-box--error';
    box.textContent = err.message;
  }
}

/* ==================================================================== *
 * Wiring
 * ==================================================================== */

const ACTIONS = {
  'toggle-voice': toggleVoiceSession,
  reconnect: () => { clearError(); startVoiceSession(); },
  'test-mic': testMicrophone,
  'test-voice': testVoiceOutput,
  'clear-transcript': clearTranscript,
  'close-modal': closeModal,
  emergency: triggerEmergencyButton,
  'dismiss-emergency': hideEmergency,
  'handoff-emergency': () => requestHandoff('emergency'),
  handoff: () => requestHandoff('patient_request'),
  'open-booking': () => {
    $('#booking-result').hidden = true;
    openModal('modal-booking');
  },
  'open-doctors': () => {
    renderDoctors(cache.doctors, $('#doctor-results'));
    openModal('modal-doctors');
  },
  'open-departments': () => { renderDepartments(); openModal('modal-departments'); },
  'open-my-appointments': () => {
    $('#lookup-result').hidden = true;
    openModal('modal-lookup');
  },
};

document.addEventListener('click', (event) => {
  const trigger = event.target.closest('[data-action]');
  if (!trigger) return;
  const handler = ACTIONS[trigger.dataset.action];
  if (handler) { event.preventDefault(); handler(); }
});

el.micButton.addEventListener('click', toggleVoiceSession);

$('#booking-form').addEventListener('submit', submitBooking);
$('#lookup-form').addEventListener('submit', submitLookup);

$('#bk-department').addEventListener('change', (event) => {
  populateDoctorSelect(event.target.value);
  refreshSlots();
});
$('#bk-doctor').addEventListener('change', refreshSlots);
$('#bk-date').addEventListener('change', refreshSlots);

$('#doc-search').addEventListener('input', (event) => {
  clearTimeout(searchTimer);
  const term = event.target.value;
  searchTimer = setTimeout(() => searchDoctors(term), 220);
});

/* --- init ---------------------------------------------------------------- */

async function init() {
  setStatus('idle');

  const dateInput = $('#bk-date');
  const today = new Date();
  const iso = (d) => d.toISOString().slice(0, 10);
  dateInput.min = iso(today);
  const horizon = new Date(today);
  horizon.setDate(horizon.getDate() + 60);
  dateInput.max = iso(horizon);

  await loadReferenceData();
  loadAppointments();          // renders the "enter your number" prompt

  const myForm = $('#my-appts-form');
  if (myForm) {
    myForm.addEventListener('submit', (event) => {
      event.preventDefault();
      loadAppointments($('#my-phone').value);
    });
  }

  initTheme();

  if (el.langSelect) {
    try {
      const savedLang = localStorage.getItem(LANG_PREF_KEY);
      if (savedLang) el.langSelect.value = savedLang;
    } catch { /* ignore */ }
    el.langSelect.addEventListener('change', () => {
      try { localStorage.setItem(LANG_PREF_KEY, el.langSelect.value); } catch { /* ignore */ }
      if (isActive()) {
        showError('Language changed. Press End, then Start again to apply it.');
      }
    });
  }

  if (el.audioSetup) {
    try {
      const saved = localStorage.getItem(AUDIO_SETUP_KEY);
      if (saved) el.audioSetup.value = saved;
    } catch { /* ignore */ }

    const describeSetup = () => {
      const headphones = el.audioSetup.value === 'headphones';
      el.audioSetupNote.textContent = headphones
        ? 'Full duplex — you can interrupt the assistant mid-sentence.'
        : 'The microphone pauses while the assistant speaks, so it does not hear itself.';
    };
    describeSetup();

    el.audioSetup.addEventListener('change', () => {
      try { localStorage.setItem(AUDIO_SETUP_KEY, el.audioSetup.value); } catch { /* ignore */ }
      describeSetup();
      if (isActive()) {
        showError('Audio setup changed. Press End, then Start again to apply it.');
      }
    });
  }

  refreshOutputDevices();
  if (el.outputSelect) {
    el.outputSelect.addEventListener('change', () => {
      try { localStorage.setItem(OUTPUT_PREF_KEY, el.outputSelect.value); } catch { /* ignore */ }
      if (isActive()) {
        showError('Output device changed. Press End, then Start again to apply it.');
      }
    });
  }
  if (navigator.mediaDevices) {
    navigator.mediaDevices.addEventListener?.('devicechange', refreshOutputDevices);
  }

  refreshMicList();
  if (el.micSelect) {
    el.micSelect.addEventListener('change', () => {
      localStorage.setItem(MIC_PREF_KEY, el.micSelect.value);
      setMicHint(
        isActive() ? 'Press End, then Start again to switch microphone.' : '',
        'bad'
      );
    });
  }
  if (navigator.mediaDevices) {
    navigator.mediaDevices.addEventListener?.('devicechange', refreshMicList);
  }

  try {
    const config = await apiGet('/api/config');
    app.config = config;
    const hindiNote = $('#hindi-voice-note');
    if (hindiNote) {
      hindiNote.textContent = config.hindi_voice_available
        ? 'Hindi is spoken by a native Hindi voice.'
        : 'Hindi is spoken by an English-accent voice in Roman letters.';
    }
    if (config.spoken_language_notice) {
      el.langNote.textContent = `ℹ️ ${config.spoken_language_notice}`;
      el.langNote.hidden = false;
    }
  } catch { /* the page works without it */ }

  try {
    const health = await apiGet('/api/health');
    if (!health.voice_ready) {
      showError(
        'The voice assistant is not configured yet — the server has no AssemblyAI key. ' +
        'Everything else on this page works. Copy .env.example to .env, add your key and restart.'
      );
      el.reconnectButton.hidden = true;
    }
  } catch { /* handled elsewhere */ }
}

init();
