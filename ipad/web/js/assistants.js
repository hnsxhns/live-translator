// Claude workers: live translation, chat answers and key takeaways (same prompts as the desktop app).
import { claudeStream, friendlyError, shortError, LONG_TIMEOUT, warmUp } from "./claude.js";

export const SUMMARY_EVERY = 60_000;      // refresh key takeaways this often (ms); was 25 s: 2.4x the Claude calls
export const SUMMARY_MODEL = "claude-sonnet-5-5";   // takeaways are short notes: Sonnet writes them as well, cheaper

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

  /** Warm the connection (start, and listening again after more than 2 minutes). */
  warm() {
    this.warmedAt = Date.now();
    const key = this.getKey();
    if (key) warmUp(key, this.model);
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
    // re-translate when Whisper corrected words (e.g. "right down" -> "write down"), not for case or
    // punctuation only; one word in a short sentence matters, one in a long sentence less so (Windows 1.9.5)
    if (ratio(norm(drafts), norm(text)) >= 0.97) {          // draft translation is good: keep it
      ids.forEach((u, i) => { this.en[u] = i === 0 ? text : ""; });
      return;
    }
    const first = ids[0];
    for (const u of ids) this.gen[u] = (this.gen[u] || 0) + 1;
    if (ids.length === 1) {
      // the draft's translation stays on screen until the corrected one is ready, then it is swapped in
      this.en[first] = text;
      this._submit(first, this.gen[first], text, false);
      return;
    }
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

  _submit(uid, g, text, stream = true) {
    this.waiting.push([uid, g, text, stream]);
    this._next();
  }

  _next() {
    while (this.active < 4 && this.waiting.length) {
      const [uid, g, text, stream] = this.waiting.shift();
      this.active++;
      this._translate(uid, g, text, stream).finally(() => { this.active--; this._next(); });
    }
  }

  _context(uid) {
    const prev = Object.keys(this.en).map(Number).filter((u) => u < uid && this.en[u]).sort((a, b) => a - b);
    const earlier = prev.slice(-4).map((u) => this.en[u]).join(" ");
    const lastVi = prev.length ? this.vi[prev[prev.length - 1]] || "" : "";
    return [earlier, lastVi];
  }

  async _translate(uid, g, text, stream = true) {
    const [ctx, lastVi] = this._context(uid);
    let prompt = ctx ? `Earlier speech (context only, already translated):\n${ctx}\n\n` : "";
    if (lastVi) prompt += `Its Vietnamese translation ended with:\n...${lastVi.slice(-200)}\n\n`;
    prompt += `NEW segment to translate:\n${text}`;
    let out = "";
    try {
      out = await claudeStream(this.getKey(), {
        system: Translator.SYSTEM, prompt, effort: "low", model: this.model, thinking: this.thinking, maxTokens: 1500,
        timeoutMs: 20_000,
        onPiece: stream ? (p) => this.emit("vi_piece", [uid, g, p]) : undefined,
      });
    } catch (e) {
      if (stream) this.emit("vi_piece", [uid, g, `[chưa dịch được: ${shortError(e)}]`]);   // says why (Windows 1.9.11)
      this.emit("error", friendlyError(e));      // (a failed correction keeps the draft's translation)
    }
    if (this.gen[uid] !== g) return;
    if (!stream && out.trim()) this.emit("vi_replace", [uid, g, out.trim()]);   // swapped in at once, no flicker
    if (stream || out.trim()) this.vi[uid] = out.trim();
  }
}

/** Answers the user's questions about the session (multi-turn), streaming into the chat pane. */
const BACKGROUND = "Bối cảnh (buổi học / thư viện tài liệu):\n";   // the first message of every question

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
    "when tutoring an exercise, guide one step at a time and wait for the user. " +
    "You have tools: read_library_documents reads the full text of library documents by their [id] " +
    "(already stored by the app, nothing is uploaded again) - use it when a question needs what a " +
    "document says (summaries, details, quotes); the catalogue descriptions are enough for an overview. " +
    "read_class_record reads the whole class record (English transcript, Vietnamese translation, all " +
    "key takeaways), optionally only the parts that mention a word - use it when the question is about " +
    "something earlier than the recent part you were given.");

  // Claude reads library documents / the class record itself when a question needs them (Windows 1.9.17)
  static TOOLS = [
    { name: "read_library_documents",
      description: "Full text of documents in the user's Learning library, by their ids from the catalogue " +
                   "(e.g. \"a1b2c3d4e5f6\"). At most 5 per call.",
      input_schema: { type: "object", properties: { ids: { type: "array", items: { type: "string" } } },
                      required: ["ids"] } },
    { name: "read_class_record",
      description: "The class record kept by the app: part = 'english' (transcript with speakers), " +
                   "'vietnamese' (translation), 'both' (English and Vietnamese line by line) or 'takeaways' " +
                   "(all key takeaways with times). Optional search: only the lines that mention it (with a " +
                   "little context). Without search: the most recent part (up to max_chars).",
      input_schema: { type: "object", properties: {
        part: { type: "string", enum: ["english", "vietnamese", "both", "takeaways"] },
        search: { type: "string" }, max_chars: { type: "integer" } }, required: ["part"] } },
  ];

  constructor(getKey, emit, getContext) {
    this.getKey = getKey;
    this.emit = emit;
    this.getContext = getContext;   // -> [english, vietnamese end, takeaways, listening now]
    this.history = [];              // past turns, kept exactly as sent (cache hits on the next question)
    this.queue = [];
    this.busy = false;
    this.getLibrary = null;         // -> catalogue text of the Learning library ("" if empty)
    this.toolHandler = null;        // async (name, input) -> text (library documents, class record)
  }

  /** Identity of an attached document / image block (null for normal text). */
  static docKey(b) {
    if (b.type === "document" || b.type === "image") {
      const src = b.source || {};
      if (src.file_id) return "file:" + src.file_id;
      const d = String(src.data || src.url || "");
      return "data:" + d.length + ":" + d.slice(0, 64) + d.slice(-64);
    }
    if (b.type === "text" && (b.text || "").startsWith("<file ")) return "text:" + b.text.length + ":" + b.text.slice(0, 200);
    return null;
  }

  /** Each document is sent once per request: one attached earlier in the conversation (e.g. the exercise file
   *  at every step) is replaced by a one-line note instead of being sent again. */
  static withoutRepeats(messages) {
    const seen = new Set();
    return messages.map((m) => {
      if (m.role !== "user" || !Array.isArray(m.content)) return m;
      const content = m.content.map((b) => {
        const k = Chatter.docKey(b);
        if (k && seen.has(k)) {
          const name = b.title || (/<file name="([^"]*)"/.exec(b.text || "") || [])[1] || "đính kèm";
          return { type: "text", text: `(tài liệu ${name} đã gửi ở trên)` };
        }
        if (k) seen.add(k);
        // the background block keeps its cache point (it is the part worth caching); others are re-set later
        if (b.type === "text" && (b.text || "").startsWith(BACKGROUND)) return b;
        const { cache_control, ...rest } = b;
        return rest;
      });
      return { ...m, content };
    });
  }

  /** Cache point at the end of the newest question: the next question (e.g. the next exercise step) reads the
   *  documents and the conversation so far from Claude's cache, at a tenth of the price. */
  static withCache(messages) {
    const out = [...messages];
    const m = out[out.length - 1];
    if (m && m.role === "user") {
      const content = Array.isArray(m.content) ? [...m.content] : [{ type: "text", text: m.content }];
      content[content.length - 1] = { ...content[content.length - 1], cache_control: { type: "ephemeral" } };
      out[out.length - 1] = { ...m, content };
    }
    return out;
  }

  /** The last turns, starting at a question (never in the middle of a tool call). */
  recent(n = 20) {
    const h = this.history;
    let start = Math.max(0, h.length - n);
    while (start < h.length) {
      const c = h[start].content;
      if (h[start].role === "user" && !(Array.isArray(c) && c[0]?.type === "tool_result")) break;
      start++;
    }
    return h.slice(start);
  }

  ask(question, files, withSession = true) {
    this.queue.push([question, files, withSession]);
    if (!this.busy) this._run();
  }

  /** Ask; when Claude wants library documents or the class record, give them and continue.
   *  -> [answer text, the tool messages added to the conversation] */
  async answer(messages, useTools) {
    const convo = [...messages], added = [];
    let text = "";
    for (let round = 0; round < 5; round++) {
      const r = await claudeStream(this.getKey(), {
        system: Chatter.SYSTEM, prompt: Chatter.withCache(Chatter.withoutRepeats(convo)), effort: "medium",
        maxTokens: 16000, timeoutMs: LONG_TIMEOUT, retries: 2, tools: useTools ? Chatter.TOOLS : null, full: true,
        onPiece: (p) => this.emit("chat_piece", p) });
      text = r.text;
      const uses = r.content.filter((b) => b.type === "tool_use");
      if (!uses.length || r.stopReason !== "tool_use") return [text, added];
      const results = [];
      for (const u of uses) {
        const what = u.name === "read_library_documents" ? `tài liệu ${(u.input.ids || []).join(", ")}`
          : `bản ghi buổi học (${u.input.part || ""}${u.input.search ? `, tìm “${u.input.search}”` : ""})`;
        this.emit("status", `📖 Claude đang đọc ${what}...`);
        let out;
        try { out = (await this.toolHandler(u.name, u.input)) || "(trống)"; } catch (e) { out = `Không đọc được: ${e.message || e}`; }
        results.push({ type: "tool_result", tool_use_id: u.id, content: [{ type: "text", text: out }] });
      }
      if (text) this.emit("chat_piece", "\n");
      const step = [{ role: "assistant", content: r.content }, { role: "user", content: results }];
      convo.push(...step);
      added.push(...step);
    }
    return [text, added];
  }

  async _run() {
    this.busy = true;
    while (this.queue.length) {
      const [question, files, withSession] = this.queue.shift();
      // The background (library catalogue, and the class record when needed) goes FIRST, as its own message:
      // while it does not change (after class, working on exercises) Claude reads it from its cache at a tenth
      // of the price instead of paying for it again on every question (Windows 1.9.11).
      const library = this.getLibrary ? await this.getLibrary() : "";
      let context = "", live = false;
      if (withSession) {
        const [en, vi, summary, on] = this.getContext();
        live = !!on;
        context = `<transcript_english>\n${en || "(chưa có)"}\n</transcript_english>\n` +
          `<translation_vietnamese_end>\n${vi || "(chưa có)"}\n</translation_vietnamese_end>\n` +
          `<key_takeaways>\n${summary || "(chưa có)"}\n</key_takeaways>`;
      }
      if (library) context = `<learning_library>\n${library}\n</learning_library>\n` + context;
      let background = [];
      if (context) {
        const block = { type: "text", text: BACKGROUND + context };
        if (!live) block.cache_control = { type: "ephemeral" };    // while listening it changes every few seconds
        background = [{ role: "user", content: [block] }, { role: "assistant", content: "Đã nắm bối cảnh." }];
      }
      // Files go before the text, and stay in the history so follow-up questions can refer to them.
      const newQ = { role: "user", content: [...files, { type: "text", text: `Câu hỏi của tôi: ${question}` }] };
      let answer = "", added = [];
      try {
        [answer, added] = await this.answer([...background, ...this.recent(), newQ], withSession && !!this.toolHandler);
      } catch (e) {
        this.emit("chat_error", `[${friendlyError(e)}]`);
      }
      // kept exactly as sent (with any documents Claude read), so the next question reads it from the cache
      if (answer.trim()) this.history.push(newQ, ...added, { role: "assistant", content: answer.trim() });
      this.emit("chat_end", null);
    }
    this.busy = false;
  }
}

/** Running key takeaways. Every round only the speech that is new since the last round is turned into new
 *  bullet points, which are ADDED to the pane (earlier takeaways are never removed). */
export class Summarizer {
  // The transcript comes from live speech recognition, so some words are misheard (worst for people far from
  // the mic). Takeaways are notes to learn from: they state what was said, cleanly, and never talk about the
  // recognition itself (earlier versions put "(chưa rõ do lỗi nhận dạng)" in ~40% of bullets). Windows 1.9.8.
  static SYSTEM = ("You keep running KEY TAKEAWAYS in Vietnamese for a live talk/conversation (often a course). You " +
    "get the takeaways already written, the end of the earlier transcript for context, the course's " +
    "own terms when known, and the NEW part of the English transcript. The transcript comes from live " +
    "speech recognition: some words are misheard and sentences may be split. Read it as the speaker " +
    "meant it: silently correct obvious mishearings from context and the course terms. Write only " +
    "takeaways that the NEW part adds and that are not already in the existing takeaways: 1-5 short " +
    "bullet points starting with '• ', most important first. Write each bullet as a clean, confident " +
    "note of the idea. Never mention speech recognition, transcription errors, missing or unclear " +
    "words, unknown names or results; never add remarks like '(chưa rõ)', '(có thể là ...)' or " +
    "'(do lỗi nhận dạng)'. If part of the speech is too garbled to understand, leave it out. Small " +
    "talk, logistics and side chatter are not takeaways. If the new part adds nothing worth " +
    "noting, output exactly NONE. Output only the bullets (or NONE).");

  constructor(getKey, emit, getTranscript, getTakeaways) {
    this.getKey = getKey;
    this.emit = emit;
    this.getTranscript = getTranscript;   // (start, end) -> [end index, transcript text of those entries]
    this.getTakeaways = getTakeaways;
    this.getTerms = () => "";             // course vocabulary (set by the app: the same words Whisper is given)
    this.doneN = 0;                      // transcript entries already covered
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
      const terms = this.getTerms();
      // the recent takeaways are enough to avoid repeats (the whole history grew to 40k+ characters per round in
      // a long class: slower, costlier, and the old style got copied)
      const prompt = (terms ? `<course_terms>\n${terms}\n</course_terms>\n` : "") +
        `<existing_takeaways>\n${(this.getTakeaways() || "").slice(-6000) || "(none yet)"}\n</existing_takeaways>\n` +
        `<earlier_transcript_end>\n${before.slice(-3000) || "(none)"}\n</earlier_transcript_end>\n` +
        `<new_transcript>\n${fresh}\n</new_transcript>`;
      // short timeout: a stuck request must not stop the takeaways for long
      const out = (await claudeStream(this.getKey(), { system: Summarizer.SYSTEM, prompt, effort: "medium", model: SUMMARY_MODEL,
                                                      timeoutMs: 60_000 })).trim();
      this.doneN = n;
      if (out && out.toUpperCase().replace(/^[ .]+|[ .]+$/g, "") !== "NONE") this.emit("summary", out);
    } catch (e) {
      this.emit("error", friendlyError(e));   // doneN unchanged -> try again next round
    }
    this.busy = false;
  }
}
