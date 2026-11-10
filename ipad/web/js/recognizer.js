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
const WHISPER_BACKLOG = 3;      // if Whisper falls further behind, older utterances keep their draft text

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

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

  call(type, audio) {
    const id = this.next++;
    const copy = Float32Array.from(audio);
    return new Promise((resolve, reject) => {
      this.waiting.set(id, { resolve, reject });
      this.worker.postMessage({ type, id, audio: copy }, [copy.buffer]);
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
    await Promise.all([spkReady, whisperReady]);
  }

  /** ctx: an AudioContext created during the user's tap (iPad only allows audio started by a tap). */
  async start(ctx) {
    this.emit("status", "Đang nghe... (nói tiếng Anh)");
    this.ctx = ctx;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true },
    });
    if (!this.workletAdded) {
      await this.ctx.audioWorklet.addModule(new URL("capture-worklet.js", import.meta.url));
      this.workletAdded = true;
    }
    this.src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "lt-capture");
    this.src.connect(this.node);
    // iOS only runs a worklet that is connected to the output; keep it silent
    this.mute = this.ctx.createGain();
    this.mute.gain.value = 0;
    this.node.connect(this.mute).connect(this.ctx.destination);
    await this.ctx.resume();

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
    this.running = true;
    this.node.port.onmessage = (ev) => this._onAudio(ev.data);
    for (const t of this.stream.getAudioTracks()) {
      t.onended = () => { if (this.running) this.emit("mic_lost", null); };
    }
  }

  async stop() {
    if (!this.running) return;
    this.running = false;
    try {
      this.rec.retrieveFinalResult();          // the last words still become an utterance
      await new Promise((r) => setTimeout(r, 600));
      this.rec.remove();
    } catch { /* recognizer already gone */ }
    try { this.node.port.onmessage = null; this.src.disconnect(); this.node.disconnect(); } catch { /* */ }
    for (const t of this.stream?.getTracks() || []) t.stop();
    try { this.mute.disconnect(); await this.ctx.suspend(); } catch { /* */ }
    this.emit("status", "Đã tắt.");
  }

  /** iPad stopped the mic (app went to background, call...): start it again. */
  async restart() {
    this.running = false;
    try { this.rec?.remove(); } catch { /* */ }
    try { this.node.port.onmessage = null; this.src.disconnect(); this.node.disconnect(); this.mute.disconnect(); } catch { /* */ }
    for (const t of this.stream?.getTracks() || []) t.stop();
    await this.ctx.resume();
    if (this.ctx.state !== "running") throw new Error("audio suspended");
    await this.start(this.ctx);
  }

  _onAudio(block) {
    if (!this.running) return;
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
        try { text = (await this.whisper.call("run", item.audio)).text; } catch { text = ""; }
      }
      const nW = text.split(/\s+/).filter(Boolean).length, nD = item.draft.split(/\s+/).filter(Boolean).length;
      // empty, or far longer than what Vosk heard (Whisper sometimes invents text on cut-off audio)
      if (!text.replace(/[ .,!?\-'"]/g, "") || nW > 2 * nD + 3) text = cap(item.draft);
      this._final(item.id, text);
    }
    this.whisperBusy = false;
  }

  _final(id, text) {
    this.context = (this.context + " " + text).slice(-400);
    this.onFinal([id], text);
  }
}
