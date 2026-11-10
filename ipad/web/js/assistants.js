// Claude workers: live translation, chat answers and key takeaways (same prompts as the desktop app).
import { claudeStream, friendlyError, LONG_TIMEOUT } from "./claude.js";

export const SUMMARY_EVERY = 25_000;      // refresh key takeaways this often (ms)

// Translation speed presets. Thinking is switched off for speed where the model allows.
export const TRANSLATE_PRESETS = {
  "Nhanh (Sonnet 5.5)": ["claude-sonnet-5-5", { type: "between_tools" }],
  "Chất lượng cao (Opus 5.5)": ["claude-opus-5-5", null],
};
export const DEFAULT_PRESET = "Nhanh (Sonnet 5.5)";

// ---- difflib.SequenceMatcher(None, a, b).ratio() for short word lists ----
function longestMatch(a, b, alo, ahi, blo, bhi) {
  let best = [alo, blo, 0];
  let prev = new Map();
  for (let i = alo; i < ahi; i++) {
    const cur = new Map();
    for (let j = blo; j < bhi; j++) {
      if (a[i] !== b[j]) continue;
      const k = (prev.get(j - 1) || 0) + 1;
      cur.set(j, k);
      if (k > best[2]) best = [i - k + 1, j - k + 1, k];
    }
    prev = cur;
  }
  return best;
}

export function ratio(a, b) {
  let matches = 0;
  const stack = [[0, a.length, 0, b.length]];
  while (stack.length) {
    const [alo, ahi, blo, bhi] = stack.pop();
    const [i, j, k] = longestMatch(a, b, alo, ahi, blo, bhi);
    if (!k) continue;
    matches += k;
    if (alo < i && blo < j) stack.push([alo, i, blo, j]);
    if (i + k < ahi && j + k < bhi) stack.push([i + k, ahi, j + k, bhi]);
  }
  const t = a.length + b.length;
  return t ? (2 * matches) / t : 1;
}

/**
 * Translates each spoken segment as soon as the speaker pauses. The fast Vosk draft is translated
 * immediately; when Whisper's accurate text arrives, the segment is re-translated only if the words
 * differ noticeably. Up to 4 segments translate at the same time, each in its own row.
 */
export class Translator {
  static SYSTEM = ("You are a live interpreter. Translate the NEW English speech segment into natural, fluent " +
    "Vietnamese. It comes from automatic speech recognition: it may lack punctuation, contain " +
    "recognition errors, or be part of a longer sentence that continues in the next segment. Silently " +
    "fix obvious recognition errors and translate it so it reads naturally after the earlier part. " +
    "Never repeat words that the earlier Vietnamese already covers. " +
    "Output ONLY the Vietnamese translation of the NEW segment: no notes, no quotes, no English.");

  constructor(getKey, emit, preset = DEFAULT_PRESET) {
    this.getKey = getKey;
    this.emit = emit;
    this.en = {};       // uid -> English text currently translated for that slot
    this.vi = {};       // uid -> finished Vietnamese text
    this.gen = {};      // uid -> generation (a re-translation makes older output stale)
    this.active = 0;
    this.waiting = [];
    this.setPreset(preset);
  }

  setPreset(name) {
    this.preset = name in TRANSLATE_PRESETS ? name : DEFAULT_PRESET;
    [this.model, this.thinking] = TRANSLATE_PRESETS[this.preset];
  }

  /** [[english, vietnamese]] in speaking order (context for chat). */
  get history() {
    return Object.keys(this.vi).map(Number).sort((a, b) => a - b)
      .filter((u) => u in this.en).map((u) => [this.en[u], this.vi[u]]);
  }

  draft(uid, text) {
    this.en[uid] = text;
    this.gen[uid] = 1;
    this.emit("vi_slot", uid);
    this._submit(uid, 1, text);
  }

  final(ids, text) {
    const norm = (s) => s.toLowerCase().replace(/[^a-z0-9' ]/g, " ").split(/\s+/).filter(Boolean);
    const drafts = ids.map((u) => this.en[u] || "").join(" ");
    if (ratio(norm(drafts), norm(text)) >= 0.85) {          // draft translation is good: keep it
      ids.forEach((u, i) => { this.en[u] = i === 0 ? text : ""; });
      return;
    }
    const first = ids[0];
    for (const u of ids) this.gen[u] = (this.gen[u] || 0) + 1;
    for (const u of ids.slice(1)) {                          // merged segments: translate into the first slot
      delete this.en[u];
      delete this.vi[u];
      this.emit("vi_drop", u);
    }
    this.en[first] = text;
    delete this.vi[first];
    const g = this.gen[first];
    this.emit("vi_reset", [first, g]);
    this._submit(first, g, text);
  }

  _submit(uid, g, text) {
    this.waiting.push([uid, g, text]);
    this._next();
  }

  _next() {
    while (this.active < 4 && this.waiting.length) {
      const [uid, g, text] = this.waiting.shift();
      this.active++;
      this._translate(uid, g, text).finally(() => { this.active--; this._next(); });
    }
  }

  _context(uid) {
    const prev = Object.keys(this.en).map(Number).filter((u) => u < uid && this.en[u]).sort((a, b) => a - b);
    const earlier = prev.slice(-4).map((u) => this.en[u]).join(" ");
    const lastVi = prev.length ? this.vi[prev[prev.length - 1]] || "" : "";
    return [earlier, lastVi];
  }

  async _translate(uid, g, text) {
    const [ctx, lastVi] = this._context(uid);
    let prompt = ctx ? `Earlier speech (context only, already translated):\n${ctx}\n\n` : "";
    if (lastVi) prompt += `Its Vietnamese translation ended with:\n...${lastVi.slice(-200)}\n\n`;
    prompt += `NEW segment to translate:\n${text}`;
    let out = "";
    try {
      out = await claudeStream(this.getKey(), {
        system: Translator.SYSTEM, prompt, effort: "low", model: this.model, thinking: this.thinking, maxTokens: 1500,
        timeoutMs: 20_000,
        onPiece: (p) => this.emit("vi_piece", [uid, g, p]),
      });
    } catch (e) {
      this.emit("vi_piece", [uid, g, "[chưa dịch được đoạn này]"]);
      this.emit("error", friendlyError(e));
    }
    if (this.gen[uid] === g) this.vi[uid] = out.trim();
  }
}

/** Answers the user's questions about the session (multi-turn), streaming into the chat pane. */
export class Chatter {
  static SYSTEM = ("You are Claude, helping a Vietnamese user who is listening to a live English talk or " +
    "conversation. The app gives you the speech-recognition transcript so far (it may contain " +
    "recognition errors), its Vietnamese translation, and the current key takeaways. Answer the " +
    "user's questions about it, or any other question they ask. Reply in Vietnamese unless the user " +
    "writes in another language or asks otherwise. Be clear and concise; plain text, short bullet " +
    "points when helpful (no Markdown headings or tables). If the transcript doesn't contain the " +
    "answer, say so and answer from general knowledge if appropriate. The user may also have a " +
    "study library (course documents uploaded from the Learning menu): its catalogue is given in " +
    "<learning_library>, and documents from it may be attached to a question. Use them when relevant; " +
    "when tutoring an exercise, guide one step at a time and wait for the user.");

  constructor(getKey, emit, getContext) {
    this.getKey = getKey;
    this.emit = emit;
    this.getContext = getContext;
    this.history = [];          // past turns
    this.queue = [];
    this.busy = false;
    this.getLibrary = null;     // -> catalogue text of the Learning library ("" if empty)
  }

  /** Mark the newest attached file as a cache point, so follow-up questions about the same documents
   *  (e.g. step-by-step exercise help) don't pay for them again. */
  static withCache(messages) {
    const out = [...messages];
    for (let i = out.length - 1; i >= 0; i--) {
      const m = out[i];
      if (m.role !== "user" || !Array.isArray(m.content)) continue;
      const idx = m.content.map((b, j) => (b.type !== "text" || (b.text || "").startsWith("<file ") ? j : -1))
        .filter((j) => j >= 0);
      if (idx.length) {
        const content = [...m.content];
        const k = idx[idx.length - 1];
        content[k] = { ...content[k], cache_control: { type: "ephemeral" } };
        out[i] = { ...m, content };
        break;
      }
    }
    return out;
  }

  ask(question, files) {
    this.queue.push([question, files]);
    if (!this.busy) this._run();
  }

  async _run() {
    this.busy = true;
    while (this.queue.length) {
      const [question, files] = this.queue.shift();
      const [en, vi, summary] = this.getContext();
      const context = `<transcript_english>\n${en || "(chưa có)"}\n</transcript_english>\n` +
        `<translation_vietnamese>\n${vi || "(chưa có)"}\n</translation_vietnamese>\n` +
        `<key_takeaways>\n${summary || "(chưa có)"}\n</key_takeaways>`;
      const library = this.getLibrary ? await this.getLibrary() : "";
      const ctx = library ? `${context}\n<learning_library>\n${library}\n</learning_library>` : context;
      // The latest session context rides on the newest question, so earlier turns stay unchanged.
      // Files go before the text, and stay in the history so follow-up questions can refer to them.
      const messages = [...this.history.slice(-20),
        { role: "user", content: [...files, { type: "text", text: `${ctx}\n\nCâu hỏi của tôi: ${question}` }] }];
      let answer = "";
      try {
        answer = await claudeStream(this.getKey(), { system: Chatter.SYSTEM, prompt: Chatter.withCache(messages),
                                                     effort: "medium", maxTokens: 16000, timeoutMs: LONG_TIMEOUT, retries: 2,
                                                     onPiece: (p) => this.emit("chat_piece", p) });
      } catch (e) {
        this.emit("chat_error", `[${friendlyError(e)}]`);
      }
      if (answer.trim()) {
        this.history.push({ role: "user", content: [...files, { type: "text", text: question }] },
                          { role: "assistant", content: answer.trim() });
      }
      this.emit("chat_end", null);
    }
    this.busy = false;
  }
}

/** Running key takeaways. Every round only the speech that is new since the last round is turned into new
 *  bullet points, which are ADDED to the pane (earlier takeaways are never removed). */
export class Summarizer {
  static SYSTEM = ("You keep running KEY TAKEAWAYS in Vietnamese for a live talk/conversation. You get the takeaways " +
    "already written, the end of the earlier transcript for context, and the NEW part of the English " +
    "transcript (from speech recognition; it may contain errors). Write only takeaways that the NEW " +
    "part adds and that are not already in the existing takeaways: 1-5 short bullet points starting " +
    "with '• ', most important first. If the new part adds nothing worth noting, output exactly NONE. " +
    "Output only the bullets (or NONE).");

  constructor(getKey, emit, getTranscript, getTakeaways) {
    this.getKey = getKey;
    this.emit = emit;
    this.getTranscript = getTranscript;   // (start, end) -> [end index, transcript text of those entries]
    this.getTakeaways = getTakeaways;
    this.doneN = 0;                       // transcript entries already covered
    this.final = false;                   // set when listening stops: cover even a short remainder
    this.busy = false;
    this.timer = setInterval(() => this.kick(), SUMMARY_EVERY);
  }

  async kick() {
    if (this.busy) return;
    const final = this.final;
    this.final = false;
    const [n, fresh] = this.getTranscript(this.doneN, null);
    if (fresh.split(/\s+/).filter(Boolean).length < (final ? 4 : 15)) return;
    this.busy = true;
    try {
      const [, before] = this.getTranscript(0, this.doneN);
      const prompt = `<existing_takeaways>\n${this.getTakeaways() || "(none yet)"}\n</existing_takeaways>\n` +
        `<earlier_transcript_end>\n${before.slice(-3000) || "(none)"}\n</earlier_transcript_end>\n` +
        `<new_transcript>\n${fresh}\n</new_transcript>`;
      // short timeout: a stuck request must not stop the takeaways for long
      const out = (await claudeStream(this.getKey(), { system: Summarizer.SYSTEM, prompt, effort: "medium",
                                                      timeoutMs: 60_000 })).trim();
      this.doneN = n;
      if (out && out.toUpperCase().replace(/^[ .]+|[ .]+$/g, "") !== "NONE") this.emit("summary", out);
    } catch (e) {
      this.emit("error", friendlyError(e));   // doneN unchanged -> try again next round
    }
    this.busy = false;
  }
}
