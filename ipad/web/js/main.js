// Live English -> Vietnamese translator for iPad (web app). Same features as the desktop app v1.6.2:
//  - English pane: live transcript from the microphone (Vosk live words, Whisper corrections, offline)
//  - Vietnamese pane: live translation (Claude), each row lined up with its English row
//  - Key takeaways (Claude), Claude chat panel with file attachments, speakers by voice, Backup/Restore
import { APP_VERSION, BUILD, NOTES } from "./version.js";
import { checkKey, friendlyError } from "./claude.js";
import { splitSentences } from "./text.js";
import { Recognizer } from "./recognizer.js";
import { ensureFiles, hasWebGPU, modelFiles } from "./models.js";
import { Translator, Chatter, Summarizer, TRANSLATE_PRESETS, DEFAULT_PRESET } from "./assistants.js";
import { fileToBlock, ACCEPT } from "./files.js";
import * as upd from "./updates.js";
import { LearningUI, shareBlob, takeawaysPdf } from "./learning.js";

// Running inside the TestFlight app (ipad/ios): files ship in the app, updates come from TestFlight.
const NATIVE = !!(window.LT_NATIVE && window.webkit?.messageHandlers?.lt);
const LABEL = NATIVE ? `v${window.LT_NATIVE.version} (${window.LT_NATIVE.build})` : upd.BUILD_LABEL;

const TEACHER_MINUTES = 10;   // whoever has spoken this long becomes "Teacher" (several -> Teacher 1, 2...)
// soft, muted colors: steel blue, terracotta, moss green, dusty purple, ochre, teal, plum
const SPK_COLORS = ["#2f5d8a", "#9a5235", "#3f7a4f", "#6c5190", "#8a6d1f", "#2e7676", "#86476a"];

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

// ---------------- settings (shared by every installed build) ----------------
const CFG = "lt_config";
function loadConfig() {
  try { return JSON.parse(localStorage.getItem(CFG) || "{}"); } catch { return {}; }
}
function saveConfig(cfg) {
  try { localStorage.setItem(CFG, JSON.stringify(cfg)); } catch { /* private mode */ }
}

const errorLog = [];          // recent errors, sent with feedback
function logError(msg) {
  errorLog.push(`${new Date().toISOString().slice(11, 19)} ${msg}`);
  if (errorLog.length > 30) errorLog.shift();
}
window.addEventListener("error", (e) => logError(String(e.message)));
window.addEventListener("unhandledrejection", (e) => logError(String(e.reason?.message || e.reason)));

// =====================================================================
// App
// =====================================================================
class App {
  constructor() {
    this.apiKey = "";
    this.ready = false;
    this.on = false;
    this.follow = true;            // auto-scroll to the newest text (off while the user reads above)
    this.rows = new Map();         // uid -> row {uid, sid, en, draft, vi, viGen, viFilled, enEl, viEl}
    this.order = [];               // uids in speaking order
    this.transcript = [];          // [[sid, text]]
    this.uidSpk = {};
    this.spkTalk = {};
    this.spkCustom = {};
    this.teachers = [];
    this.spkAlias = {};
    this.lastSummary = "";
    this.chatLog = [];             // [{q, files, a}] for backup
    this.chatBusy = false;
    this.attachments = [];         // [[name, block]]
    this.panelSide = null;
    this.unread = false;
    this.split = 0.75;             // EN/VI share of the height (Key takeaways = the rest)

    const emit = (kind, val) => this.handle(kind, val);
    const key = () => this.apiKey;
    this.recognizer = new Recognizer(emit, (ids, text) => {
      this.transcript.push([this.recognizer.uttSpk[ids[0]] || 1, text]);
      this.handle("final", [ids, text]);
    });
    this.translator = new Translator(key, emit, loadConfig().translate_preset || DEFAULT_PRESET);
    this.chatter = new Chatter(key, emit, () => this.chatContext());
    this.summarizer = null;
    this.takeFollow = true;        // takeaways pane shows the newest points unless the user scrolls up
    this.learning = new LearningUI(this);
    this.chatter.getLibrary = () => this.learning.catalogText();
    this.buildUI();
  }

  // ---------------- login ----------------
  showLogin() {
    $("login").hidden = false;
    $("main").hidden = true;
    const cfg = loadConfig();
    $("key").value = cfg.api_key || "";
    $("remember").checked = cfg.remember !== false;
    $("loginVersion").textContent = LABEL;
    $("loginBtn").onclick = () => this.loginKey();
    $("key").onkeydown = (e) => { if (e.key === "Enter") this.loginKey(); };
    // a key pasted by any means (long-press → Paste too) logs in at once
    $("key").addEventListener("paste", () => setTimeout(() => this.keyEntered(), 0));
    $("pasteKey").onclick = () => this.pasteKey();
    $("getKey").addEventListener("click", () => {
      $("loginStatus").textContent = "Sau khi copy key ở trang Claude, quay lại đây và bấm 📋 Dán.";
      $("loginStatus").className = "status";
    });
    const guide = (open) => {
      $("guide").hidden = !open;
      $("guideBtn").setAttribute("aria-expanded", String(open));
      $("guideBtn").textContent = open ? "❓ Ẩn hướng dẫn" : "❓ Hướng dẫn chi tiết";
      if (open) $("guide").scrollIntoView({ behavior: "smooth", block: "nearest" });
    };
    $("guideBtn").onclick = () => guide($("guide").hidden);
    $("guideClose").onclick = () => guide(false);
    if (cfg.remember && cfg.api_key) {
      this.loginBusy(true, "Đang tự động đăng nhập...");
      checkKey(cfg.api_key).then(() => this.loginOk(cfg.api_key))
        .catch(() => this.loginFail("Phiên đăng nhập đã lưu không còn dùng được. Hãy đăng nhập lại."));
    }
  }

  /** Paste the key from the clipboard (iPad asks once to allow pasting). */
  async pasteKey() {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (!text) throw new Error("empty");
      $("key").value = text;
      this.keyEntered();
    } catch {
      $("key").focus();
      this.loginFail("Không đọc được bộ nhớ tạm. Chạm giữ vào ô nhập key → chọn Dán.");
    }
  }

  keyEntered() {
    const key = $("key").value.trim().replace(/\s+/g, "");
    $("key").value = key;
    if (/^sk-ant-[\w-]{20,}$/.test(key)) this.loginKey();
    else if (key) this.loginFail("Đây chưa phải API key (key bắt đầu bằng sk-ant-). Xem ❓ Hướng dẫn chi tiết.");
  }

  loginBusy(on, msg) {
    $("loginBtn").disabled = on;
    if (msg) { $("loginStatus").textContent = msg; $("loginStatus").className = "status"; }
  }

  loginFail(msg) {
    this.loginBusy(false);
    $("loginStatus").textContent = msg;
    $("loginStatus").className = "status err";
  }

  async loginKey() {
    const key = $("key").value.trim();
    if (!key) return this.loginFail("Vui lòng nhập API key.");
    this.loginBusy(true, "Đang kiểm tra tài khoản...");
    try {
      await checkKey(key);
      this.loginOk(key);
    } catch (e) {
      if (e?.status === 401) this.loginFail("Đăng nhập thất bại: API key không hợp lệ.");
      else if (e?.status === 403) this.loginFail("Đăng nhập thất bại: tài khoản không có quyền truy cập.");
      else if (e instanceof TypeError) this.loginFail("Không kết nối được tới Claude. Kiểm tra mạng.");
      else this.loginFail(`Đăng nhập thất bại: ${e.message || e}`);
    }
  }

  loginOk(key) {
    const cfg = loadConfig();
    cfg.remember = $("remember").checked;
    cfg.method = "key";
    if (cfg.remember) cfg.api_key = key; else delete cfg.api_key;
    saveConfig(cfg);
    this.apiKey = key;
    $("login").hidden = true;
    $("main").hidden = false;
    this.summarizer = this.summarizer || new Summarizer(() => this.apiKey, (k, v) => this.handle(k, v),
                                                        (a, b) => this.transcriptRange(a, b), () => this.lastSummary);
    if (this.ready) this.setStatus("✓ Sẵn sàng. Nhấn ON để bắt đầu nghe.", "ok");
    this.checkUpdates();
  }

  logout() {
    const cfg = loadConfig();
    for (const k of ["remember", "method", "api_key"]) delete cfg[k];
    saveConfig(cfg);
    location.reload();
  }

  // ---------------- speech models (start loading at once, during login) ----------------
  async prepareModels() {
    try {
      const cfg = loadConfig();
      const gpu = await hasWebGPU();
      const useWhisper = cfg.whisper !== false;
      const downloading = !NATIVE && await ensureFiles(modelFiles(gpu, useWhisper), (done, total) => {
        const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
        const msg = `Đang tải mô hình nhận diện giọng nói lần đầu: ${(done / 1e6).toFixed(0)}` +
          (total ? ` / ${(total / 1e6).toFixed(0)} MB (${pct}%)` : " MB");
        this.progress(pct, msg);
      });
      this.progress(100, downloading ? "Đã tải xong mô hình. Đang chuẩn bị..." : "Đang chuẩn bị nhận diện giọng nói (vài giây)...");
      await this.recognizer.load({ gpu, useWhisper, native: NATIVE });
      this.progress(null);
      this.handle("ready", null);
    } catch (e) {
      logError(e.message || e);
      this.progress(null);
      this.handle("error", `Không tải được mô hình nhận diện giọng nói: ${e.message || e}`);
      $("loginStatus").textContent = `Chưa tải được mô hình giọng nói (${e.message || e}). Kiểm tra mạng rồi mở lại app.`;
      $("loginStatus").className = "status err";
    }
  }

  progress(pct, msg) {
    for (const box of document.querySelectorAll(".progress")) {
      box.hidden = pct === null;
      if (pct === null) continue;
      box.querySelector(".bar").style.width = `${pct}%`;
      box.querySelector("span").textContent = msg;
    }
    if (pct !== null && this.apiKey) this.setStatus(msg);
  }

  // ---------------- UI ----------------
  buildUI() {
    const sel = $("preset");
    for (const name of Object.keys(TRANSLATE_PRESETS)) sel.append(new Option(name, name));
    sel.value = this.translator.preset;
    sel.onchange = () => {
      this.translator.setPreset(sel.value);
      const cfg = loadConfig();
      cfg.translate_preset = sel.value;
      saveConfig(cfg);
    };
    $("toggle").onclick = () => this.toggleOnOff();
    $("btnLogout").onclick = () => this.logout();
    $("btnBackup").onclick = () => this.backupSession();
    $("btnRestore").onclick = () => $("restorePick").click();
    $("restorePick").onchange = (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) this.restoreSession(f); };
    $("btnVersion").textContent = LABEL;
    $("btnVersion").onclick = () => this.showVersions();
    $("edgeL").onclick = () => this.togglePanel("left");
    $("edgeR").onclick = () => this.togglePanel("right");
    $("send").onclick = () => this.sendPrompt();
    $("learn").onclick = () => this.learning.showMenu();
    $("takePdf").onclick = () => this.exportTakeaways();
    $("edgeLearn").onclick = () => this.learning.showMenu();
    $("summary").addEventListener("scroll", () => {
      const w = $("summary");
      this.takeFollow = w.scrollTop + w.clientHeight >= w.scrollHeight - 6;
    }, { passive: true });
    $("attach").onclick = () => $("filePick").click();
    $("filePick").accept = ACCEPT;
    $("filePick").onchange = (e) => { const fs = [...e.target.files]; e.target.value = ""; this.attachFiles(fs); };
    $("prompt").onkeydown = (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.sendPrompt(); }
    };
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); this.togglePanel("right"); }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "l") { e.preventDefault(); this.learning.showMenu(); }
    });
    const panes = $("panes");
    panes.addEventListener("scroll", () => {
      this.follow = panes.scrollTop + panes.clientHeight >= panes.scrollHeight - 6;
    }, { passive: true });
    for (const d of [$("divider"), $("divider2")]) this.dragDivider(d);
    this.applySplit();
    this.refreshPanelBtn();
    document.addEventListener("visibilitychange", () => this.onVisibility());
  }

  setStatus(text, kind = "") {
    const s = $("status");
    s.textContent = text;
    s.className = "status " + kind;
  }

  dragDivider(d) {
    d.addEventListener("pointerdown", (e) => {
      d.setPointerCapture(e.pointerId);
      const box = $("center").getBoundingClientRect();
      const move = (ev) => {
        this.split = Math.min(0.9, Math.max(0.1, (ev.clientY - box.top) / box.height));
        this.applySplit();
      };
      const up = () => { d.removeEventListener("pointermove", move); d.removeEventListener("pointerup", up); };
      d.addEventListener("pointermove", move);
      d.addEventListener("pointerup", up);
    });
  }

  applySplit() {
    $("center").style.setProperty("--split", String(this.split));
    if (this.takeFollow) this.scrollEnd($("summary"));
  }

  // ---- Claude panel (same panel over the left or the right half) ----
  togglePanel(side) {
    const p = $("claude");
    if (this.panelSide === side) {
      this.panelSide = null;
      p.hidden = true;
    } else {
      this.panelSide = side;
      p.hidden = false;
      p.classList.toggle("left", side === "left");
      p.classList.toggle("right", side === "right");
      this.unread = false;
      this.scrollEnd($("chat"));
      $("prompt").focus({ preventScroll: true });
    }
    this.refreshPanelBtn();
  }

  refreshPanelBtn() {
    const letters = [..."Claude"].join("\n");
    for (const side of ["left", "right"]) {
      const b = $(side === "left" ? "edgeL" : "edgeR");
      const opens = side === "left" ? "▶" : "◀", closes = side === "left" ? "◀" : "▶";
      if (this.panelSide === side) {
        b.textContent = `${closes}\n${letters}`;
        b.className = `edge ${side} open`;
      } else if (this.unread) {
        b.textContent = `${opens}\n${letters}\n●`;
        b.className = `edge ${side} unread`;
      } else {
        b.textContent = `${opens}\n${letters}`;
        b.className = `edge ${side}`;
      }
    }
  }

  scrollEnd(w) {
    requestAnimationFrame(() => { w.scrollTop = w.scrollHeight; });
  }

  followNow() {
    if (this.follow) {
      const p = $("panes");
      requestAnimationFrame(() => { p.scrollTop = p.scrollHeight; });
    }
  }

  // ---------------- chat ----------------
  async attachFiles(files) {
    const problems = [];
    for (const f of files) {
      try {
        this.attachments.push([f.name, await fileToBlock(f)]);
      } catch (e) {
        problems.push(`${f.name}: ${e.message}`);
      }
    }
    this.refreshAttachBar();
    if (problems.length) alert("Không đính kèm được:\n" + problems.join("\n"));
  }

  refreshAttachBar() {
    const bar = $("attachBar");
    bar.replaceChildren();
    const job = this.learning?.job;
    if (job) {                                   // documents the Learning menu adds to the next prompt
      const chip = el("span", "chip learn", "🎓 " + job.label);
      const x = el("button", "x", "✕");
      x.onclick = () => { this.learning.takeJob(); this.refreshAttachBar(); };
      chip.append(x);
      bar.append(chip);
    }
    this.attachments.forEach(([name], i) => {
      const chip = el("span", "chip", "📎 " + (name.length <= 28 ? name : name.slice(0, 25) + "..."));
      const x = el("button", "x", "✕");
      x.onclick = () => { this.attachments.splice(i, 1); this.refreshAttachBar(); };
      chip.append(x);
      bar.append(chip);
    });
    bar.hidden = !this.attachments.length && !job;
  }

  async sendPrompt() {
    let q = $("prompt").value.trim();
    const files = this.attachments;
    if ((!q && !files.length) || this.chatBusy) return;
    if (!q) q = "Hãy xem (các) file đính kèm và tóm tắt nội dung chính.";
    const job = this.learning.takeJob();          // documents from the Learning menu, if any
    $("prompt").value = "";
    this.attachments = [];
    this.refreshAttachBar();
    this.chatBusy = true;
    this.chatFailed = false;
    $("send").disabled = true;
    $("send").textContent = "...";
    const names = files.map(([n]) => n).concat(job ? ["🎓 " + job.label] : []);
    this.chatTurn(q, names);
    this.chatLog.push({ q, files: names, a: "" });
    if (job && job.kind === "journal") {
      this.learning.runJournal(q, job);           // fills the PDF template instead of a normal answer
      return;
    }
    let learnBlocks = [];
    if (job) {
      try {
        learnBlocks = await this.learning.blocks(job);
      } catch (e) {
        this.setStatus(`🎓 Không đọc được tài liệu: ${e.message || e}`, "err");
      }
    }
    this.chatter.ask(q, learnBlocks.concat(files.map(([, b]) => b)));
  }

  chatTurn(q, names, answer = null) {
    const w = $("chat");
    if (w.childNodes.length) w.append("\n\n");
    w.append(el("span", "you", "Bạn: "), q + "\n");
    const plain = names.filter((n) => !n.startsWith("🎓 ")), learn = names.filter((n) => n.startsWith("🎓 "));
    if (plain.length) w.append(el("span", "file", "📎 " + plain.join(", ") + "\n"));
    for (const n of learn) w.append(el("span", "file", n + "\n"));
    w.append(el("span", "who", "Claude: "), el("span", "ansStart"));   // file names after this become links
    if (answer !== null) w.append(answer);
    this.scrollEnd(w);
  }

  chatContext() {
    return [this.transcriptText().slice(-40000),
            this.translator.history.map(([, v]) => v).join("\n").slice(-20000),
            this.lastSummary];
  }

  // ---------------- speakers ----------------
  res(sid) {
    let n = 0;
    while (sid in this.spkAlias && n++ < 50) sid = this.spkAlias[sid];
    return sid;
  }

  speakerName(sid) {
    sid = this.res(sid);
    if (sid in this.spkCustom) return this.spkCustom[sid];
    const t = this.teachers.indexOf(sid);
    if (t >= 0) return this.teachers.length === 1 ? "Teacher" : `Teacher ${t + 1}`;
    return `Người nói ${sid}`;
  }

  color(sid) {
    return SPK_COLORS[(this.res(sid) - 1 + SPK_COLORS.length * 100) % SPK_COLORS.length];
  }

  applyMerges(merged) {
    const fresh = Object.keys(merged).map(Number).filter((g) => !(g in this.spkAlias));
    this.spkAlias = Object.fromEntries(Object.entries(merged).map(([k, v]) => [+k, +v]));
    for (const gone of fresh) {
      const keep = this.res(gone);
      if (gone in this.spkCustom && !(keep in this.spkCustom)) this.spkCustom[keep] = this.spkCustom[gone];
      if (this.teachers.includes(gone)) this.teachers = [...new Set(this.teachers.map((t) => (t === gone ? keep : t)))];
    }
    this.relabel();
  }

  /** Name + color of every row (a new speaker starts with a blank line and their name). */
  relabel(from = 0) {
    let prev = from > 0 ? this.rows.get(this.order[from - 1]) : null;
    for (let i = from; i < this.order.length; i++) {
      const row = this.rows.get(this.order[i]);
      const sid = this.res(row.sid);
      const start = !prev || this.res(prev.sid) !== sid;
      const name = start ? this.speakerName(sid) + ": " : "";
      for (const c of [row.enEl, row.viEl]) {
        c.classList.toggle("spkStart", start && i > 0);
        c.style.setProperty("--spk", this.color(sid));
        const lbl = c.firstChild;
        if (lbl.textContent !== name) lbl.textContent = name;
        lbl.dataset.sid = sid;
      }
      prev = row;
    }
  }

  renameSpeaker(sid) {
    const name = prompt(`Tên cho '${this.speakerName(sid)}':`, this.spkCustom[this.res(sid)] || "");
    if (name === null) return;
    sid = this.res(sid);
    if (name.trim()) this.spkCustom[sid] = name.trim();
    else delete this.spkCustom[sid];                  // empty -> back to automatic name
    this.relabel();
  }

  /** [end index, text] of transcript entries start..end (used by the running takeaways). */
  transcriptRange(start = 0, end = null) {
    const stop = end === null ? this.transcript.length : end;
    return [stop, this.transcriptLines(this.transcript.slice(start, stop))];
  }

  transcriptText() {
    return this.transcriptLines(this.transcript);
  }

  transcriptLines(items) {
    const lines = [];
    let last = null;
    for (const [sid, text] of items) {
      if (sid !== last) { lines.push(`${this.speakerName(sid)}: ${text}`); last = sid; }
      else lines[lines.length - 1] += " " + text;
    }
    return lines.join("\n");
  }

  // ---------------- rows: English | Vietnamese ----------------
  cell(kind, uid) {
    const c = el("div", `cell ${kind}`);
    c.dataset.uid = uid;
    const lbl = el("span", "lbl");
    lbl.onclick = () => this.renameSpeaker(+lbl.dataset.sid);
    c.append(lbl, el("span", "txt"));
    return c;
  }

  row(uid) {
    let r = this.rows.get(uid);
    if (!r) {
      r = { uid, sid: this.uidSpk[uid] || 1, en: "", draft: true, vi: "", viGen: 0, viFilled: false,
            enEl: this.cell("en", uid), viEl: this.cell("vi", uid) };
      this.rows.set(uid, r);
      this.order.push(uid);
      $("rows").insertBefore(r.enEl, $("partialEn"));     // live words stay the last row
      $("rows").insertBefore(r.viEl, $("partialEn"));
      this.relabel(this.order.length - 1);
    }
    return r;
  }

  setPartial(text) {
    $("partialEn").textContent = text;
    this.followNow();
  }

  addDraft(uid, text) {
    this.setPartial("");
    const r = this.row(uid);
    r.en = text;
    r.draft = true;
    r.enEl.lastChild.textContent = text;
    r.enEl.classList.add("draft");
    this.followNow();
  }

  finalize(ids, text) {
    ids.forEach((uid, i) => {
      const r = this.rows.get(uid);
      if (!r) return;
      r.draft = false;
      r.en = i === 0 ? text : "";
      r.enEl.lastChild.textContent = i === 0 ? splitSentences(text.trim()) : "";
      r.enEl.classList.remove("draft");
    });
    this.followNow();
  }

  viSlot(uid) {
    const r = this.row(uid);
    r.viGen = 1;
    r.viFilled = false;
    r.vi = "";
    r.viEl.lastChild.textContent = "…";
    r.viEl.classList.add("pending");
    this.followNow();
  }

  viPiece(uid, g, text) {
    const r = this.rows.get(uid);
    if (!r || r.viGen !== g) return;                   // output of an older translation of this row
    r.vi = r.viFilled ? r.vi + text : text.trimStart();
    r.viFilled = true;
    r.viEl.classList.remove("pending");
    r.viEl.lastChild.textContent = splitSentences(r.vi);  // one sentence per line, like English
    this.followNow();
  }

  viReset(uid, g) {
    const r = this.rows.get(uid);
    if (!r) return;
    r.viGen = g;
    r.viFilled = false;
    r.vi = "";
    r.viEl.lastChild.textContent = "…";
    r.viEl.classList.add("pending");
  }

  viDrop(uid) {
    const r = this.rows.get(uid);
    if (!r) return;
    r.viGen = -1;
    r.vi = "";
    r.viEl.lastChild.textContent = "";
    r.viEl.classList.remove("pending");
  }

  // ---------------- events (like the desktop's _poll) ----------------
  handle(kind, val) {
    switch (kind) {
      case "partial": this.setPartial(val); break;
      case "speaker": {
        const [uid, sid, talk, merged] = val;
        this.uidSpk[uid] = sid;
        this.spkTalk = talk;
        const row = this.rows.get(uid);
        if (row && row.sid !== sid) {             // the voiceprint corrected the first guess
          row.sid = sid;
          this.relabel(this.order.indexOf(uid));
        }
        if (JSON.stringify(merged) !== JSON.stringify(this.spkAlias)) this.applyMerges(merged);
        let changed = false;
        for (const [s, secs] of Object.entries(talk)) {
          if (secs >= TEACHER_MINUTES * 60 && !this.teachers.includes(+s)) { this.teachers.push(+s); changed = true; }
        }
        if (changed) this.relabel();
        break;
      }
      case "draft": this.addDraft(...val); this.translator.draft(...val); break;
      case "final": this.finalize(...val); this.translator.final(...val); break;
      case "vi_slot": this.viSlot(val); break;
      case "vi_piece": this.viPiece(...val); break;
      case "vi_reset": this.viReset(...val); break;
      case "vi_drop": this.viDrop(val); break;
      case "chat_piece":
        if (this.chatLog.length) this.chatLog[this.chatLog.length - 1].a += val;
        $("chat").append(val);
        this.scrollEnd($("chat"));
        break;
      case "chat_error": this.chatFailed = true; $("chat").append(el("span", "err", val)); break;
      case "chat_end":
        this.chatBusy = false;
        $("send").disabled = false;
        $("send").textContent = "Gửi ➤";
        if (!this.panelSide) { this.unread = true; this.refreshPanelBtn(); }
        else $("prompt").focus({ preventScroll: true });
        // guided exercise: put the next step's prompt in the box (or finish the solution); links in the answer
        this.learning.onChatEnd(this.chatLog[this.chatLog.length - 1] || null, !this.chatFailed);
        break;
      case "summary": this.addTakeaways(val); break;
      case "ready":
        this.ready = true;
        if (this.wantOn) this.startListening();
        else if (!this.on) this.setStatus("✓ Sẵn sàng. Nhấn ON để bắt đầu nghe.", "ok");
        break;
      case "status": this.setStatus(val); break;
      case "error": logError(val); this.setStatus(val, "err"); break;
      case "mic_lost": this.onVisibility(); break;
      case "force_off": if (this.on) this.toggleOnOff(); break;
    }
  }

  /** Add new takeaways under a time stamp; the whole history stays (also in lastSummary / backup). */
  addTakeaways(bullets) {
    const d = new Date();
    const stamp = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    const block = `[${stamp}]\n${bullets.trim()}`;
    this.lastSummary = this.lastSummary ? this.lastSummary + "\n\n" + block : block;
    const w = $("summary");
    if (w.childNodes.length) w.append("\n\n");
    w.append(el("span", "ttime", `[${stamp}]\n`), bullets.trim());
    if (this.takeFollow) this.scrollEnd(w);
  }

  /** The whole takeaways history as a PDF (button on the pane's title bar). */
  async exportTakeaways() {
    if (!this.lastSummary.trim()) { alert("Chưa có ý chính nào để xuất."); return; }
    try {
      const { name, blob } = await takeawaysPdf(this.lastSummary);
      const marks = (this.lastSummary.match(/^\[\d\d:\d\d\]$/gm) || []).length;
      await this.learning.addOutput("takeaways", name, blob, "Ý chính / Key takeaways",
        { detail: `${marks || 1} mốc giờ${this.lastSummary.match(/^\[(\d\d:\d\d)\]/) ? ` · từ ${RegExp.$1}` : ""}` });
      if (await shareBlob(blob, name)) this.setStatus(`✓ Đã xuất PDF: ${name} (cũng có trong 📋 List Outputs)`, "ok");
      else this.setStatus(`✓ Đã tạo PDF: ${name} — xem trong 🎓 Learning → 📋 List Outputs`, "ok");
    } catch (e) {
      alert("Không xuất được PDF: " + (e.message || e));
    }
  }

  renderTakeaways(text) {
    const w = $("summary");
    w.replaceChildren();
    (text ? text.split(/\n\n(?=\[\d\d:\d\d\]\n)/) : []).forEach((part, i) => {
      if (i) w.append("\n\n");
      const m = /^(\[\d\d:\d\d\]\n)/.exec(part);
      if (m) {
        w.append(el("span", "ttime", m[1]));
        part = part.slice(m[1].length);
      }
      w.append(part);
    });
  }

  // ---------------- ON / OFF ----------------
  toggleOnOff() {
    this.on = !this.on;
    const b = $("toggle");
    b.textContent = this.on ? "ON" : "OFF";
    b.className = "onoff " + (this.on ? "on" : "off");
    if (this.on) {
      // iPad only allows audio that starts during a tap: create/resume it now
      this.audioCtx = this.audioCtx || new AudioContext();
      this.audioCtx.resume();
      this.keepAwake(true);
      if (this.ready) this.startListening();
      else {
        this.wantOn = true;
        this.setStatus("Đang chuẩn bị nhận diện giọng nói... sẽ tự bắt đầu nghe khi xong.");
      }
    } else {
      this.wantOn = false;
      this.keepAwake(false);
      if (this.summarizer) this.summarizer.final = true;
      this.recognizer.stop().then(() => setTimeout(() => this.summarizer?.kick(), 1500));   // takeaways for the last words
    }
  }

  async startListening() {
    this.wantOn = false;
    if (!this.on) return;
    try {
      await this.recognizer.start(this.audioCtx);
    } catch (e) {
      logError(e.message || e);
      const denied = e?.name === "NotAllowedError";
      this.handle("error", denied ? "Chưa cho phép dùng micro. Vào Cài đặt → Safari (hoặc app) → Micro, rồi bật ON lại."
                                  : `Lỗi micro: ${e.message || e}`);
      this.handle("force_off", null);
    }
  }

  async keepAwake(on) {
    if (NATIVE) window.webkit.messageHandlers.lt.postMessage({ type: "keepAwake", on });
    try {
      if (on && "wakeLock" in navigator && document.visibilityState === "visible") {
        this.wake = await navigator.wakeLock.request("screen");      // screen stays on while listening
      } else if (!on && this.wake) {
        await this.wake.release();
        this.wake = null;
      }
    } catch { /* not supported */ }
  }

  async onVisibility() {
    if (document.visibilityState !== "visible") return;
    this.checkUpdates();
    if (!this.on || !this.recognizer.running) return;
    this.keepAwake(true);
    const live = this.recognizer.stream?.getAudioTracks().some((t) => t.readyState === "live");
    if (live && this.audioCtx.state === "running") return;
    try {
      await this.recognizer.restart();
      this.setStatus("Đang nghe tiếp... (nói tiếng Anh)");
    } catch {
      this.handle("force_off", null);
      this.setStatus("iPad đã tạm dừng micro khi bạn rời app. Nhấn ON để nghe tiếp.", "err");
    }
  }

  // ---------------- Backup / Restore (same file format as the desktop app) ----------------
  sessionData() {
    const rows = [...this.order].sort((a, b) => a - b).filter((u) => u in this.uidSpk).map((uid) => {
      const r = this.rows.get(uid);
      return { uid, speaker: this.uidSpk[uid], en: (this.translator.en[uid] || r?.en || "").trim(),
               vi: (this.translator.vi[uid] || "").trim() };
    });
    const sp = this.recognizer.speakers;
    const pad = (n) => String(n).padStart(2, "0");
    const d = new Date();
    return {
      app: "LiveTranslator", version: APP_VERSION, build: BUILD, device: "iPad",
      saved: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`,
      rows,
      speakers: { custom: this.spkCustom, teachers: this.teachers, talk: this.spkTalk, alias: this.spkAlias,
                  voices: Object.fromEntries([...sp.sums].map(([k, v]) => [String(k), Array.from(v)])),
                  next_id: sp.nextId },
      transcript: this.transcript,
      takeaways: this.lastSummary,
      chat: this.chatLog,
      chat_history: this.chatter.history.map((m) => (typeof m.content === "string" ? m : {
        role: m.role,
        content: m.content.map((b) => (b.type === "text" ? b.text || "" : `[file đính kèm: ${b.title || b.type}]`)).join(" "),
      })),
    };
  }

  async backupSession() {
    const d = new Date(), pad = (n) => String(n).padStart(2, "0");
    const name = `LiveTranslator_backup_${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}.json`;
    const file = new Blob([JSON.stringify(this.sessionData(), null, 1)], { type: "application/json" });
    try {
      const data = JSON.parse(await file.text());
      await this.learning.addOutput("backup", name, file, "Backup phiên nghe",
        { detail: `${(data.rows || []).length} dòng · ${Object.keys(data.speakers?.talk || {}).length} người nói · ${(data.chat || []).length} câu hỏi Claude` });
      // iPad share sheet: "Lưu vào Tệp", AirDrop, Mail...
      if (!(await shareBlob(file, name))) return;
      this.setStatus(`✓ Đã backup: ${name}`, "ok");
    } catch (e) {
      if (e?.name !== "AbortError") alert("Backup lỗi: " + (e.message || e));
    }
  }

  async restoreSession(file) {
    let data;
    try {
      data = JSON.parse(await file.text());
      if (data.app !== "LiveTranslator") throw new Error("đây không phải file backup của Live Translator");
    } catch (e) {
      alert("Không mở được backup: " + e.message);
      return;
    }
    if (this.order.length && !confirm("Nội dung hiện tại sẽ được thay bằng nội dung trong file backup. Tiếp tục?")) return;
    if (this.on) this.toggleOnOff();                     // stop listening while loading
    this.loadSession(data);
    this.setStatus(`✓ Đã restore: ${file.name} (${(data.rows || []).length} dòng)`, "ok");
  }

  /** Rebuild every pane from backup data (also restores voices so new speech matches old speakers). */
  loadSession(data) {
    for (const r of this.rows.values()) { r.enEl.remove(); r.viEl.remove(); }
    this.rows.clear();
    this.order = [];
    const sp = data.speakers || {};
    const conv = (d, f = (v) => v) => Object.fromEntries(Object.entries(d || {}).map(([k, v]) => [+k, f(v)]));
    this.spkCustom = conv(sp.custom);
    this.teachers = (sp.teachers || []).map(Number);
    this.spkTalk = conv(sp.talk, Number);
    this.spkAlias = conv(sp.alias, Number);
    this.uidSpk = {};
    const tr = this.recognizer.speakers;
    tr.sums = new Map(Object.entries(sp.voices || {}).map(([k, v]) => [+k, Float32Array.from(v)]));
    tr.talk = { ...this.spkTalk };
    tr.merged = { ...this.spkAlias };
    tr.nextId = +(sp.next_id ?? (Math.max(0, ...tr.sums.keys()) + 1));
    tr.last = null;
    this.translator.en = {};
    this.translator.vi = {};
    this.translator.gen = {};
    const rows = data.rows || [];
    for (const r of rows) {
      const uid = +r.uid, sid = +r.speaker;
      this.uidSpk[uid] = sid;
      this.recognizer.uttSpk[uid] = sid;
      this.translator.en[uid] = r.en || "";
      this.translator.vi[uid] = r.vi || "";
      this.translator.gen[uid] = 1;
      this.addDraft(uid, r.en || "");
      this.finalize([uid], r.en || "");
      this.viSlot(uid);
      this.viPiece(uid, 1, r.vi || " ");
    }
    this.recognizer.uttId = Math.max(this.recognizer.uttId, ...rows.map((r) => +r.uid));
    this.relabel();
    this.transcript = (data.transcript || []).map((t) => [+t[0], t[1]]);
    this.lastSummary = data.takeaways || "";
    this.renderTakeaways(this.lastSummary);
    this.takeFollow = true;
    if (this.summarizer) this.summarizer.doneN = this.transcript.length;
    this.chatLog = [...(data.chat || [])];
    $("chat").replaceChildren();
    for (const t of this.chatLog) this.chatTurn(t.q || "", t.files || [], t.a || "");
    this.chatter.history = [...(data.chat_history || [])];
    this.follow = true;
    this.followNow();
    this.scrollEnd($("summary"));
  }

  // ---------------- TestFlight-style updates ----------------
  async checkUpdates() {
    if (NATIVE) return;                          // the TestFlight app tells testers about new builds itself
    const now = Date.now();
    if (this.lastCheck && now - this.lastCheck < 60_000) return;
    this.lastCheck = now;
    const data = await upd.fetchReleases();
    this.releases = data;
    const prefs = upd.getPrefs();
    const nb = upd.newerBuild(data, prefs.channel);
    const banner = $("updateBanner");
    if (nb && !prefs.pinned) {
      banner.hidden = false;
      $("updateText").textContent = `Có bản mới ${nb.version} (${nb.build})${nb.channel === "beta" ? " BETA" : ""}: ` +
        ((nb.notes || [])[0] || "");
      $("updateGo").onclick = () => this.confirmLeave() && upd.openBuild(null);
      $("updateNotes").onclick = () => this.showVersions();
    } else {
      banner.hidden = true;
    }
  }

  confirmLeave() {
    return !this.order.length ||
      confirm("Mở bản khác sẽ tải lại app và xoá nội dung đang hiển thị. Hãy Backup trước nếu cần. Tiếp tục?");
  }

  async showVersions() {
    if (NATIVE) {
      const body = el("div", "versions");
      body.append(el("p", "", `Live Translator ${LABEL} — bản cài qua TestFlight.`),
                  el("p", "muted", "Khi có bản mới, app TestFlight sẽ báo; mở TestFlight và bấm Cập nhật."));
      const ul = el("ul");
      for (const n of NOTES) ul.append(el("li", "", n));
      const fb = el("button", "", "✉ Gửi phản hồi");
      fb.onclick = () => this.showFeedback("");
      const actions = el("div", "actions");
      actions.append(fb);
      body.append(ul, actions);
      this.sheet("Phiên bản", body);
      return;
    }
    const data = this.releases || await upd.fetchReleases();
    const prefs = upd.getPrefs();
    const body = el("div", "versions");
    const chan = el("label", "row");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = prefs.channel === "beta";
    cb.onchange = () => { upd.setPrefs({ channel: cb.checked ? "beta" : "stable" }); this.lastCheck = 0; this.checkUpdates(); this.showVersions(); };
    chan.append(cb, " Nhận bản thử nghiệm (Beta) — giống TestFlight: có bản mới sớm hơn, có thể còn lỗi");
    body.append(chan);
    if (prefs.pinned) {
      const unpin = el("button", "primary", "Luôn dùng bản mới nhất");
      unpin.onclick = () => this.confirmLeave() && upd.openBuild(null);
      body.append(el("p", "muted", `Đang giữ cố định bản ${prefs.pinned}.`), unpin);
    }
    const list = upd.visible(data, prefs.channel);
    if (!list.length) list.push({ version: APP_VERSION, build: BUILD, channel: "beta", date: "", notes: NOTES });
    for (const r of list) {
      const id = upd.idOf(r);
      const item = el("div", "release");
      const head = el("div", "relHead");
      head.append(el("b", "", `${r.version} (${r.build})`));
      head.append(el("span", `badge ${r.channel}`, r.channel === "beta" ? "BETA" : "Ổn định"));
      if (r.date) head.append(el("span", "muted", r.date));
      if (id === upd.BUILD_ID) head.append(el("span", "badge current", "Đang dùng"));
      else {
        const open = el("button", "small", "Mở bản này");
        open.onclick = () => this.confirmLeave() && upd.openBuild(id);
        head.append(open);
      }
      const ul = el("ul");
      for (const n of r.notes || []) ul.append(el("li", "", n));
      item.append(head, ul);
      body.append(item);
    }
    const actions = el("div", "actions");
    const fb = el("button", "", "✉ Gửi phản hồi");
    fb.onclick = () => this.showFeedback(data.feedback_email || "");
    const inv = el("button", "", "📲 Mời người thử (QR)");
    inv.onclick = () => window.open(upd.ROOT + "install.html", "_blank");
    actions.append(fb, inv);
    body.append(actions);
    this.sheet("Phiên bản & thử nghiệm", body);
  }

  showFeedback(email) {
    const body = el("div", "feedback");
    const ta = el("textarea");
    ta.rows = 6;
    ta.placeholder = "Mô tả lỗi hoặc góp ý (đang làm gì, điều gì xảy ra, mong muốn gì)...";
    const send = el("button", "primary", "Gửi");
    const note = el("p", "muted", "Kèm theo: phiên bản, thiết bị và các lỗi gần đây (không kèm API key hay nội dung buổi nghe).");
    send.onclick = async () => {
      const info = upd.diagnostics({ whisper: this.recognizer.whisperMode || "off", recentErrors: errorLog });
      const text = `${ta.value.trim()}\n\n---\n${JSON.stringify(info, null, 1)}`;
      const subject = `Phản hồi Live Translator iPad ${upd.BUILD_ID}`;
      try {
        if (NATIVE) await shareBlob(new Blob([text], { type: "text/plain" }), "phan-hoi-live-translator.txt", text);
        else if (email) location.href = `mailto:${email}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(text)}`;
        else if (navigator.share) await navigator.share({ title: subject, text });
        else { await navigator.clipboard.writeText(text); alert("Đã copy phản hồi, hãy dán gửi cho người phát triển."); }
        $("sheet").close();
      } catch (e) {
        if (e?.name !== "AbortError") alert(e.message || e);
      }
    };
    body.append(ta, note, send);
    this.sheet("Gửi phản hồi", body);
  }

  sheet(title, body, wide = false) {
    const d = $("sheet");
    $("sheetTitle").textContent = title;
    $("sheetBody").replaceChildren(body);
    d.classList.toggle("wide", wide);
    if (!d.open) d.showModal();
  }

  closeSheet() {
    if ($("sheet").open) $("sheet").close();
  }
}

// ---------------- start ----------------
function registerSW() {
  if (NATIVE || !("serviceWorker" in navigator)) return;   // the app already has every file
  navigator.serviceWorker.register(upd.ROOT + "sw.js", { scope: upd.ROOT }).then(async (reg) => {
    // keep this build's files offline
    const files = await fetch("files.json").then((r) => r.json()).catch(() => []);
    const msg = { type: "precache", urls: files.map((f) => new URL(f, location.href).href) };
    (reg.active || reg.installing || reg.waiting)?.postMessage(msg);
    navigator.serviceWorker.ready.then((r) => r.active?.postMessage(msg));
  }).catch((e) => logError("sw: " + e.message));
}

const app = new App();
window.app = app;
$("sheetClose").onclick = () => $("sheet").close();
upd.setPrefs({ last: upd.BUILD_ID });
registerSW();
app.showLogin();
app.prepareModels();
