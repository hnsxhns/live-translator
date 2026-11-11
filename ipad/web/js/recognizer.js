// Live captions in two layers (same design as the desktop app):
//  1. Vosk (streaming) turns mic audio into words immediately -> grey "live" text.
//  2. When an utterance ends (pause, or cut at a short pause after UTT_SOFT seconds), Whisper
//     re-transcribes its audio for accurate, punctuated text, which replaces the draft.
// Each utterance also gets a voiceprint -> speaker id (SpeakerTracker).

import { SpeakerTracker } from "./speakers.js";
import { VOSK_URL } from "./models.js";

export const SR = 16000;
const FRAME = 480;              // 30 ms frames
const UTT_SOFT = 3.0;           // after this long, cut the utterance at the next short pause (s)
const UTT_HARD = 7.0;           // always cut by this length (s)
/** Whisper in the browser marks silence/noise with tags such as "[BLANK_AUDIO]", "[Music]", "(applause)":
 *  they are not words (an empty result keeps Vosk's text). */
export const noSpeechTags = (t) => String(t || "")
  .replace(/\[[^\]]*\]|\((?:music|applause|laughter|laughs|silence|inaudible|noise|blank[^)]*|static|coughs?)\)|\*[^*]*\*/gi, " ")
  .replace(/\s+/g, " ").trim();
/** Phrases Whisper is known to invent on short or unclear audio (learned from video subtitles). */
export const HALLUCINATION = /thank(s| you) for watching|please (like and )?subscribe|subtitles? by|see you (in the )?next (video|time)|amara\.org|transcribed by/i;
const WHISPER_BACKLOG = 3;
const WHISPER_LIMIT = 15000;   // ms for one utterance; longer = Whisper is stuck      // if Whisper falls further behind, older utterances keep their draft text

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

/** Which browser on the iPad (they all use Safari's engine, but each has its own mic permission). */
export function browserInfo() {
  const ua = navigator.userAgent;
  if (window.LT_NATIVE) return { name: "app TestFlight", settings: "Cài đặt → Live Translator → Micro" };
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const name = /CriOS/.test(ua) ? "Chrome" : /EdgiOS/.test(ua) ? "Edge" : /FxiOS/.test(ua) ? "Firefox" : "Safari";
  return { name: standalone ? `app ở Màn hình chính (${name})` : name,
           settings: `Cài đặt → ${name} → Micro (bật), và trong ${name}: cho phép trang này dùng micro` };
}

/** The ways the mic can be captured, tried in turn when no sound comes in (and the one that works is kept):
 *  "raw" = no echo cancelling (best for speech far away), "plain" = iPad's own defaults (some iPads and the
 *  Chrome app only give sound this way); rate = an audio context at the mic's own sample rate. */
export const WAYS = [
  { cons: "raw", rate: false, script: false },
  { cons: "plain", rate: true, script: false },
  { cons: "plain", rate: true, script: true },
  { cons: "raw", rate: true, script: true },
];
export const WAY_KEY = "lt_micway";
const savedWay = () => { try { const w = +localStorage.getItem(WAY_KEY); return w > 0 && w < WAYS.length ? w : 0; } catch { return 0; } };
const MIC_RAW = { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true };
const within = (p, ms) => Promise.race([Promise.resolve(p).catch(() => {}), new Promise((r) => setTimeout(r, ms))]);

/** A running AudioContext: the given one if it can run, else a new one (iPad allows that while the mic is
 *  open). resume() is never awaited without a time limit: it can hang while iPad has the audio "interrupted". */
async function runningContext(ctx, rate = 0) {
  if (ctx && rate && ctx.sampleRate !== rate && ctx.state !== "closed") {   // one at the mic's own rate, if iPad lets it run
    let f = null;
    try { f = new AudioContext({ sampleRate: rate }); } catch { /* not supported */ }
    if (f) {
      if (f.state !== "running") await within(f.resume(), 1500);
      if (f.state === "running") { within(ctx.close(), 500); return f; }
      within(f.close(), 500);
    }
  }
  if (ctx && ctx.state !== "closed") {
    if (ctx.state !== "running") await within(ctx.resume(), 1500);
    if (ctx.state === "running") return ctx;
    within(ctx.close(), 500);
  }
  let fresh;
  try { fresh = rate ? new AudioContext({ sampleRate: rate }) : new AudioContext(); } catch { fresh = new AudioContext(); }
  if (fresh.state !== "running") await within(fresh.resume(), 1500);
  if (fresh.state === "running") return fresh;
  within(fresh.close(), 500);
  const e = new Error("iPad cần chạm vào màn hình để bật âm thanh");
  e.code = "needTap";
  throw e;
}

/** Main-thread version of capture-worklet.js (for the ScriptProcessor fallback): -> 16 kHz blocks of 100 ms. */
export function downsampler(rate, out) {
  const ratio = rate / SR;
  let acc = [], pos = 0, buf = new Float32Array(1600), n = 0;
  return (ch) => {
    for (let i = 0; i < ch.length; i++) acc.push(ch[i]);
    while (pos + ratio <= acc.length) {
      const a = Math.floor(pos), b = Math.max(a + 1, Math.floor(pos + ratio));
      let s = 0;
      for (let k = a; k < b; k++) s += acc[k];
      buf[n++] = s / (b - a);
      pos += ratio;
      if (n === buf.length) { out(buf); buf = new Float32Array(1600); n = 0; }
    }
    const drop = Math.floor(pos);
    if (drop > 0) { acc = acc.slice(drop); pos -= drop; }
  };
}

class WorkerClient {
  constructor(url) {
    this.worker = new Worker(url, { type: "module" });
    this.waiting = new Map();
    this.next = 1;
    this.worker.onmessage = (ev) => {
      const m = ev.data;
      const key = m.type === "ready" || (m.type === "error" && m.id === undefined) ? "load" : m.id;
      const w = this.waiting.get(key);
      if (!w) return;
      this.waiting.delete(key);
      m.type === "error" ? w.reject(new Error(m.error)) : w.resolve(m);
    };
    this.worker.onerror = (e) => {
      for (const w of this.waiting.values()) w.reject(new Error(e.message || "worker error"));
      this.waiting.clear();
    };
  }

  load(extra = {}) {
    return new Promise((resolve, reject) => {
      this.waiting.set("load", { resolve, reject });
      this.worker.postMessage({ type: "load", ...extra });
    });
  }

  call(type, audio, extra = {}) {
    const id = this.next++;
    const copy = Float32Array.from(audio);
    return new Promise((resolve, reject) => {
      this.waiting.set(id, { resolve, reject });
      this.worker.postMessage({ type, id, audio: copy, ...extra }, [copy.buffer]);
    });
  }
}

export class Recognizer {
  constructor(emit, onFinal) {
    this.emit = emit;                    // (kind, value) -> UI, like the desktop's ui_q
    this.onFinal = onFinal;              // (ids, text)
    this.speakers = new SpeakerTracker();
    this.uttSpk = {};                    // utterance id -> speaker id
    this.uttId = 0;
    this.context = "";
    this.glossary = "";                  // course vocabulary for Whisper (from the Learning library)
    this.running = false;
    this.vosk = null;
    this.spk = null;
    this.whisper = null;
    this.whisperMode = "";
    this.queue = [];
    this.whisperBusy = false;
    this.spkChain = Promise.resolve();
  }

  /** Load the speech models (files must already be in the cache). Whisper/voiceprints are optional. */
  async load({ gpu, useWhisper, native = false }) {
    const base = new URL("./", import.meta.url);
    const spk = new WorkerClient(new URL("speaker-worker.js", base));
    const spkReady = spk.load().then(() => { this.spk = spk; }).catch((e) => console.warn("voiceprints off:", e));
    let whisperReady = Promise.resolve();
    if (useWhisper) {
      const w = new WorkerClient(new URL("whisper-worker.js", base));
      whisperReady = w.load({ gpu, native }).then((m) => { this.whisper = w; this.whisperMode = m.mode; })
        .catch((e) => {
          console.warn("whisper off:", e);
          this.emit("status", "Whisper không chạy được trên máy này — dùng chữ của Vosk.");
        });
    }
    if (!window.Vosk?.createModel) throw new Error("thiếu thư viện Vosk");
    this.vosk = await window.Vosk.createModel(VOSK_URL, -1);
    // never wait for ever: a Whisper that does not load on this browser must not stop the listening
    // (it joins later if it does finish)
    await within(Promise.all([spkReady, whisperReady]), 25000);
  }

  // ---------------- microphone ----------------
  // iPad pitfalls handled here (the cause of "nothing is heard"):
  //  - ctx.resume() can hang for ever while the context is "interrupted" (after the mic permission prompt,
  //    a call, another app): never wait on it without a time limit, make a new context instead;
  //  - a context made before the mic was opened sometimes gets only zeros from the mic: checked after
  //    start (watchdog) and fixed by opening the mic and the context again, then by the older
  //    ScriptProcessor capture;
  //  - an AudioWorklet module belongs to one context: added again for each new context.

  /** ctx: an AudioContext created during the user's tap (iPad only allows audio started by a tap). */
  async start(ctx) {
    if (this.stopping) await this.stopping;      // OFF then ON quickly: let the old stop finish first
    this.emit("status", "Đang mở micro...");
    this.fixes = 0;
    this.way = savedWay();
    await this._open(ctx);
    this.rec = new this.vosk.KaldiRecognizer(SR);
    this.rec.setWords(true);
    this.rec.on("result", (m) => this._onResult(m.result));
    this.rec.on("partialresult", (m) => {
      const p = m.result?.partial || "";
      if (p !== this.lastPartial) {
        this.lastPartial = p;
        this.emit("partial", p);
      }
    });
    this.sent = 0;                 // samples sent to Vosk since this recognizer started
    this.cut = 0;                  // sample index where the current utterance starts
    this.chunks = [];              // [{start, data}] audio not yet given to an utterance
    this.rmsHist = [];
    this.quiet = 0;
    this.cutAsked = false;
    this.lastPartial = "";
    this.paused = false;
    this.running = true;
    this.emit("status", "Đang nghe... (nói tiếng Anh)");
    this._watch();
  }

  /** Mic + a running context + the capture graph. Throws an error with code "needTap" if iPad wants a tap. */
  async _open(ctx) {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error(`${browserInfo().name} không cho trang này dùng micro — cập nhật iPadOS/trình duyệt, hoặc mở bằng Safari`);
    }
    const way = WAYS[this.way || 0];
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: way.cons === "plain" ? true : MIC_RAW });
    try {
      const rate = way.rate ? +(this.stream.getAudioTracks()[0]?.getSettings?.().sampleRate || 0) : 0;
      this.ctx = await runningContext(ctx, rate);
      await this._graph();
    } catch (e) {
      this._close();
      throw e;
    }
    for (const t of this.stream.getAudioTracks()) {
      t.onended = () => { if (this.running) this.emit("mic_lost", null); };
    }
    this.ctx.onstatechange = () => {             // a call, Siri, another app took the audio
      if (this.running && this.ctx.state !== "running") this.emit("mic_lost", null);
    };
  }

  async _graph() {
    const ctx = this.ctx;
    this.src = ctx.createMediaStreamSource(this.stream);
    // iOS only runs a capture node that is connected to the output; keep it silent
    this.mute = ctx.createGain();
    this.mute.gain.value = 0;
    if (ctx.audioWorklet && typeof AudioWorkletNode !== "undefined" && !WAYS[this.way || 0].script) {
      if (this.workletCtx !== ctx) {
        await ctx.audioWorklet.addModule(new URL("capture-worklet.js", import.meta.url));
        this.workletCtx = ctx;
      }
      this.node = new AudioWorkletNode(ctx, "lt-capture");
      this.node.port.onmessage = (ev) => this._onAudio(ev.data);
      this.mode = "worklet";
    } else {
      this.node = ctx.createScriptProcessor(4096, 1, 1);
      const down = downsampler(ctx.sampleRate, (block) => this._onAudio(block));
      this.node.onaudioprocess = (ev) => down(ev.inputBuffer.getChannelData(0));
      this.mode = "script";
    }
    this.src.connect(this.node);
    this.node.connect(this.mute).connect(ctx.destination);
  }

  /** Disconnect the graph and release the mic (the context and the recognizer stay). */
  _close() {
    clearTimeout(this.watchTimer);
    try { if (this.node?.port) this.node.port.onmessage = null; } catch { /* */ }
    try { if (this.node) this.node.onaudioprocess = null; } catch { /* */ }
    for (const n of [this.src, this.node, this.mute]) { try { n?.disconnect(); } catch { /* */ } }
    this.src = this.node = this.mute = null;
    if (this.ctx) this.ctx.onstatechange = null;
    for (const t of this.stream?.getTracks() || []) { t.onended = null; t.stop(); }
    this.stream = null;
  }

  /** After start: is sound really coming in? Real mic noise is never exactly 0. Fixes itself if not. */
  _watch() {
    this.blocks = 0;
    this.peak = 0;
    clearTimeout(this.watchTimer);
    this.watchTimer = setTimeout(async () => {
      if (!this.running) return;
      if (this.blocks > 0 && this.peak > 0) {                // sound comes in: this way works here, keep it
        try { localStorage.setItem(WAY_KEY, String(this.way || 0)); } catch { /* */ }
        return;
      }
      const why = this.blocks ? "chỉ nhận được im lặng" : "không nhận được âm thanh";
      this.fixes += 1;
      console.warn(`mic: ${why} (way ${this.way}, ${this.mode}, ctx ${this.ctx?.state}, ${this.ctx?.sampleRate} Hz) -> fix ${this.fixes}`);
      if (this.fixes >= WAYS.length) {                       // every way was tried
        try { localStorage.removeItem(WAY_KEY); } catch { /* */ }
        this.emit("mic_silent", why);
        return;
      }
      this.way = ((this.way || 0) + 1) % WAYS.length;        // the next way to capture
      this.emit("status", `Micro chưa thu được tiếng — đang thử cách thu âm khác (${this.fixes + 1}/${WAYS.length})...`);
      this.running = false;
      this._close();
      const old = this.ctx;
      this.ctx = null;
      try { await old?.close(); } catch { /* */ }
      this.workletCtx = null;
      this.paused = true;                                    // until the mic is open again
      this.resuming = (async () => {
        try {
          await this._open(null);                            // the mic first, then a new context
          this.paused = false;
          this.running = true;
          this.emit("ctx", this.ctx);
          this.emit("status", "Đang nghe... (nói tiếng Anh)");
          this._watch();
        } catch (e) {
          this.emit("mic_resume_failed", e);
        }
      })().finally(() => { this.resuming = null; });
    }, 3000);
  }

  stop() {
    if (!this.running && !this.paused) return this.stopping || Promise.resolve();
    this.running = false;
    this.paused = false;
    const rec = this.rec;                      // this run's own recognizer and mic, never a newer one
    this.stopping = (async () => {
      if (this.resuming) { try { await this.resuming; } catch { /* */ } }   // a mic being opened is closed too
      this.running = false;
      this.paused = false;
      try {
        rec.retrieveFinalResult();             // the last words still become an utterance
        await new Promise((r) => setTimeout(r, 600));
        rec.remove();
      } catch { /* recognizer already gone */ }
      if (this.rec === rec) this.rec = null;
      this._close();
      try { await this.ctx?.suspend(); } catch { /* */ }
      this.emit("level", 0);
      this.emit("status", "Đã tắt.");
    })().finally(() => { this.stopping = null; });
    return this.stopping;
  }

  /** Leaving the screen: the words so far become an utterance, the mic is released (iOS shows it off). */
  async pause() {
    if (this.resuming) { try { await this.resuming; } catch { /* */ } }     // screen switched while re-opening
    if (!this.running) return;
    this.running = false;
    this.paused = true;
    try { this.rec.retrieveFinalResult(); } catch { /* */ }
    this._close();
    try { await this.ctx?.suspend(); } catch { /* */ }
    this.emit("level", 0);
  }

  /** Back on the screen: open the mic again and carry on with the same recognizer.
   *  ctx: a context made during a tap, if there was one. Throws (code "needTap") when iPad wants a tap. */
  async resume(ctx = null) {
    if (this.stopping) await this.stopping;
    if (this.resuming) return this.resuming;   // one re-open at a time (two left a mic feeding audio twice)
    if (this.running || !this.paused || !this.rec) return;
    this.resuming = (async () => {
      this.fixes = 0;
      await this._open(ctx || this.ctx);
      this.paused = false;
      this.running = true;
      this._watch();
    })().finally(() => { this.resuming = null; });
    return this.resuming;
  }

  /** iPad stopped the mic while on screen (call, Siri, another app): open it again. */
  async restart() {
    if (this.running) await this.pause();
    await this.resume();
  }

  _onAudio(block) {
    if (!this.running) return;
    let sq = 0, pk = 0;
    for (let i = 0; i < block.length; i++) { const v = block[i]; sq += v * v; if (v > pk) pk = v; else if (-v > pk) pk = -v; }
    this.blocks += 1;
    if (pk > this.peak) this.peak = pk;
    this.emit("level", Math.sqrt(sq / (block.length || 1)));
    this.chunks.push({ start: this.sent, data: block });
    this.sent += block.length;
    this.rec.acceptWaveformFloat(block, SR);

    // breath pauses: Vosk only ends an utterance after a long silence, so we also cut at short
    // pauses once the utterance is long enough -> accurate text comes sooner
    const n = Math.floor(block.length / FRAME);
    for (let f = 0; f < n; f++) {
      let s = 0;
      for (let i = f * FRAME; i < (f + 1) * FRAME; i++) s += block[i] * block[i];
      this.rmsHist.push(Math.sqrt(s / FRAME));
    }
    if (this.rmsHist.length > 400) this.rmsHist.splice(0, this.rmsHist.length - 400);
    let floor = 0.004;
    if (this.rmsHist.length > 30) {
      const sorted = [...this.rmsHist].sort((a, b) => a - b);
      floor = sorted[Math.floor(0.2 * (sorted.length - 1))];
    }
    for (let k = this.rmsHist.length - n; k < this.rmsHist.length; k++) {
      this.quiet = this.rmsHist[k] < floor * 2.0 + 0.002 ? this.quiet + FRAME / SR : 0;
    }
    const secs = (this.sent - this.cut) / SR;
    if (!this.cutAsked && ((secs >= UTT_SOFT && this.quiet >= 0.2) || secs >= UTT_HARD)) {
      this.cutAsked = true;
      this.rec.retrieveFinalResult();
    }
  }

  _take(from, to) {
    const out = new Float32Array(Math.max(0, to - from));
    for (const c of this.chunks) {
      const a = Math.max(from, c.start), b = Math.min(to, c.start + c.data.length);
      if (b > a) out.set(c.data.subarray(a - c.start, b - c.start), a - from);
    }
    this.chunks = this.chunks.filter((c) => c.start + c.data.length > to);
    return out;
  }

  _onResult(result) {
    this.cutAsked = false;
    const text = (result?.text || "").trim();
    const words = result?.result || [];
    let end;
    if (words.length) end = Math.min(this.sent, Math.round((words[words.length - 1].end + 0.25) * SR));
    else end = text ? this.sent : Math.max(this.cut, this.sent - Math.round(0.3 * SR));
    end = Math.max(end, this.cut);
    const audio = this._take(this.cut, end);
    this.cut = end;
    this.lastPartial = "";
    this._endUtterance(text, audio);
  }

  _endUtterance(draft, audio) {
    if (["", "the", "huh", "uh", "um"].includes(draft)) {    // Vosk heard no real words: skip Whisper
      this.emit("partial", "");
      return;
    }
    const id = ++this.uttId;
    const seconds = audio.length / SR;
    // Show and translate the words at once with the last speaker as a first guess: the voiceprint takes
    // ~1 s in the browser, and waiting for it made the text late. The label is corrected when it is ready.
    const sp = this.speakers;
    const guess = sp.last ? sp.resolve(sp.last) : sp.nextId;
    this.uttSpk[id] = guess;
    this.emit("speaker", [id, guess, { ...sp.talk }, { ...sp.merged }]);
    this.emit("draft", [id, draft]);
    this.spkChain = this.spkChain.then(async () => {
      let vec = null;
      if (this.spk && audio.length > SR / 2) {
        try { vec = (await this.spk.call("embed", audio)).vec; } catch { vec = null; }
      }
      const sid = sp.assign(vec, seconds);
      this.uttSpk[id] = sid;
      this.emit("speaker", [id, sid, { ...sp.talk }, { ...sp.merged }]);
      this.queue.push({ id, draft, audio });     // Whisper text (and the transcript) use the real speaker
      this._pumpWhisper();
    });
  }

  async _pumpWhisper() {
    if (this.whisperBusy) return;
    this.whisperBusy = true;
    while (this.queue.length) {
      while (this.queue.length > WHISPER_BACKLOG) {
        const x = this.queue.shift();
        this._final(x.id, cap(x.draft));
      }
      const item = this.queue.shift();
      let text = "";
      if (this.whisper) {
        // never wait for ever: a Whisper stuck on this iPad (e.g. on the GPU) kept every sentence from being
        // finished and translated. Too slow twice -> Vosk text only.
        const w = this.whisper;
        let late = false;
        try {
          // the course's vocabulary + the last words heard help Whisper with names and terms (Windows 1.9.5)
          const prompt = ((this.glossary ? this.glossary + " " : "") + this.context.slice(-150)).trim();
          const r = await Promise.race([w.call("run", item.audio, { prompt }),
            new Promise((res) => setTimeout(() => { late = true; res(null); }, WHISPER_LIMIT))]);
          text = r?.text || "";
        } catch { text = ""; }
        if (late) {
          this.whisperLate = (this.whisperLate || 0) + 1;
          console.warn("whisper: no answer in", WHISPER_LIMIT, "ms", this.whisperLate);
          if (this.whisperLate >= 2 && this.whisper === w) {
            this.whisper = null;
            this.whisperMode = "";
            try { w.worker.terminate(); } catch { /* */ }
            this.emit("status", "Whisper quá chậm trên máy này — dùng chữ của Vosk (vẫn nghe và dịch bình thường).");
          }
        } else this.whisperLate = 0;
        text = noSpeechTags(text);
      }
      const nW = text.split(/\s+/).filter(Boolean).length, nD = item.draft.split(/\s+/).filter(Boolean).length;
      const words = (s) => new Set(s.toLowerCase().replace(/[^a-z0-9' ]/g, " ").split(/\s+/).filter(Boolean));
      const wd = words(item.draft), shared = [...words(text)].filter((x) => wd.has(x)).length / Math.max(1, wd.size);
      // phrases Whisper invents in silence ("thanks for watching"...), or a few words heard and Whisper wrote
      // something else (Windows 1.9.5)
      const invented = (HALLUCINATION.test(text) && !HALLUCINATION.test(item.draft)) || (nD <= 3 && shared < 0.5);
      // empty, or far longer than what Vosk heard (Whisper sometimes invents text on cut-off audio)
      if (!text.replace(/[ .,!?\-'"]/g, "") || nW > 2 * nD + 3 || invented) text = cap(item.draft);
      this._final(item.id, text);
    }
    this.whisperBusy = false;
  }

  _final(id, text) {
    this.context = (this.context + " " + text).slice(-400);
    this.onFinal([id], text);
  }
}
