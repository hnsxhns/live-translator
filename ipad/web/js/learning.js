// 🎓 Learning menu for the Claude panel: a study library that Claude learns from (port of learning.py, v1.8.0).
//
//   Folder       pick the study folder/files: new files are uploaded to Claude (Files API) and catalogued
//                (title, description, course day, exercises). Files with the same content (e.g. "x.pdf" and
//                "x (1).pdf") are uploaded once. Picking again only uploads what is new.
//   Agenda, Summary, Exercises, Journal, Certificate
//                fill the prompt box with a Vietnamese prompt the user can edit before sending; the matching
//                documents go along with it.
//   Documents    the library list (date, title, file name); tap a file name to open the original.
//
// Everything is stored on the iPad in IndexedDB "lt-learning" (library + copies of the original files).
import { askJson, uploadFile, ClaudeError, friendlyError } from "./claude.js";
import { fileToBlock, docxText } from "./files.js";

const ROOT = new URL("../../../", import.meta.url).href;
const PDFJS = ROOT + "lib/pdfjs-4.10.38/";

export const KINDS = ["agenda", "book", "slides", "exercise", "journal_template", "certificate", "reference", "other"];
const KIND_VI = { agenda: "Agenda", book: "Sách/bài đọc", slides: "Slide", exercise: "Bài tập",
  journal_template: "Mẫu nhật ký", certificate: "Chứng chỉ", reference: "Tham khảo", other: "Khác" };
// " (1)", " [2]", " - Copy", " - Copy (3)", "_copy", " copy 2", " - bản sao" at the end of a file name
const COPY_MARK = /(\s*\(\d+\)|\s*\[\d+\]|\s*-\s*(copy|bản sao)(\s*\(\d+\))?|_copy\d*|\s+copy(\s*\d+)?)$/i;
const JOURNAL_NAME = /journal|nh[aậ]t\s*k[yý]|nhat_ky|diary|log\s*book|reflection/i;
const IMAGE_EXT = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp"]);
const TEXT_EXT = new Set([".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".log", ".srt", ".vtt", ".yaml",
  ".yml", ".py", ".js", ".ts", ".java", ".cs", ".cpp", ".c", ".h", ".go", ".rb", ".php", ".sql", ".ini"]);
const SUPPORTED = new Set([...IMAGE_EXT, ...TEXT_EXT, ".pdf", ".docx"]);
const MB = 2 ** 20;

const extOf = (name) => { const i = name.lastIndexOf("."); return i >= 0 ? name.slice(i).toLowerCase() : ""; };
const stem = (name) => { const i = name.lastIndexOf("."); return i > 0 ? name.slice(0, i) : name; };
const copyMarked = (name) => COPY_MARK.test(stem(name));
// a string that is safe as a file name: no reserved or control characters, no line breaks
const safeName = (name) => String(name).replace(/[\\/:*?"<>|\x00-\x1f]+/g, "_").replace(/\s+/g, " ")
  .replace(/^[ .]+|[ .]+$/g, "") || "file";
// titles shown in prompts and labels: one line, no repeated spaces
const clean = (t) => String(t || "").replace(/\s+/g, " ").trim();
const pad = (n) => String(n).padStart(2, "0");
const isoDate = (d = new Date()) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const stamp = (d = new Date()) => `${isoDate(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
export function fmtDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso || "";
}
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

// ---------------- IndexedDB ----------------
let dbp = null;
function db() {
  dbp = dbp || new Promise((resolve, reject) => {
    const r = indexedDB.open("lt-learning", 1);
    r.onupgradeneeded = () => { for (const s of ["meta", "files", "text"]) r.result.createObjectStore(s); };
    r.onsuccess = () => {
      // another copy of the app (newer build, second tab) needs the database: let it go instead of blocking it
      r.result.onversionchange = () => { r.result.close(); dbp = null; };
      resolve(r.result);
    };
    r.onerror = () => reject(r.error);
  });
  return dbp;
}
async function idb(store, mode, fn) {
  const d = await db();
  return new Promise((resolve, reject) => {
    const tx = d.transaction(store, mode);
    const req = fn(tx.objectStore(store));
    tx.oncomplete = () => resolve(req?.result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}
const idbGet = (store, key) => idb(store, "readonly", (s) => s.get(key));
const idbPut = (store, key, val) => idb(store, "readwrite", (s) => s.put(val, key));
const idbDel = (store, key) => idb(store, "readwrite", (s) => s.delete(key));

// ---------------- PDF helpers (pdf.js, loaded only when needed) ----------------
let pdfjsP = null;
function pdfjs() {
  pdfjsP = pdfjsP || import(PDFJS + "pdf.min.mjs").then((m) => {
    m.GlobalWorkerOptions.workerSrc = PDFJS + "pdf.worker.min.mjs";
    return m;
  });
  return pdfjsP;
}

async function openPdf(buf) {
  const lib = await pdfjs();
  return lib.getDocument({ data: new Uint8Array(buf.slice(0)), isEvalSupported: false }).promise;
}

/** (plain text, page count) of a PDF / Word / text file; ["", 0] for images. */
async function extractText(file, ext) {
  if (ext === ".pdf") {
    const pdf = await openPdf(await file.arrayBuffer());
    const parts = [];
    for (let i = 1; i <= pdf.numPages; i++) {
      const tc = await (await pdf.getPage(i)).getTextContent();
      parts.push(`[Trang ${i}]\n` + tc.items.map((it) => it.str + (it.hasEOL ? "\n" : " ")).join(""));
    }
    const n = pdf.numPages;
    pdf.destroy();
    return [parts.join("\n\n"), n];
  }
  if (ext === ".docx") return [await docxText(file), 0];
  if (TEXT_EXT.has(ext)) {
    const raw = new Uint8Array(await file.arrayBuffer());
    for (const enc of ["utf-8", "utf-16le", "windows-1258"]) {
      try { return [new TextDecoder(enc, { fatal: true }).decode(raw).replace(/^﻿/, ""), 0]; } catch { /* next */ }
    }
    return [new TextDecoder("latin1").decode(raw), 0];
  }
  return ["", 0];
}

async function sha256(file) {
  const h = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// =====================================================================
// The library (index + file copies)
// =====================================================================
export class Library {
  async load() {
    this.data = (await idbGet("meta", "library").catch(() => null)) || {};
    for (const [k, v] of [["docs", {}], ["folder", ""], ["synced", ""], ["certificates", []]]) {
      if (this.data[k] === undefined) this.data[k] = v;
    }
    return this;
  }

  save() {
    return idbPut("meta", "library", this.data);
  }

  static key(d) {
    return [d.day || 9999, d.date || "", (d.title || d.name).toLowerCase()];
  }

  docs() {
    return Object.values(this.data.docs).sort((a, b) => {
      const x = Library.key(a), y = Library.key(b);
      for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
      return 0;
    });
  }

  get(id) { return this.data.docs[id]; }

  async file(doc) {
    const blob = await idbGet("files", doc.id);
    if (!blob) throw new Error(`thiếu file ${doc.name}`);
    return new File([blob], doc.name, { type: blob.type });
  }

  async text(doc) { return (await idbGet("text", doc.id)) || ""; }

  /** [[day, date, docs]] for the course days found (day 0 = whole course, not listed). */
  days() {
    const out = new Map();
    for (const d of this.docs()) if ((d.day || 0) > 0) (out.get(d.day) || out.set(d.day, []).get(d.day)).push(d);
    return [...out.entries()].sort((a, b) => a[0] - b[0])
      .map(([day, ds]) => [day, (ds.find((x) => x.date) || {}).date || "", ds]);
  }

  exercises() {
    const out = [];
    for (const d of this.docs()) {
      for (const ex of d.exercises || []) {
        out.push({ day: d.day || 0, title: ex.title || "", summary: ex.summary || "", location: ex.location || "", doc: d });
      }
    }
    return out;
  }

  /** Journal template PDFs (catalogued as such, or named like one). */
  templates() {
    const docs = this.docs().filter((d) => d.ext === ".pdf");
    const hit = docs.filter((d) => d.kind === "journal_template");
    return hit.length ? hit : docs.filter((d) => JOURNAL_NAME.test(d.name));
  }

  /** Short description of the library for Claude's context. */
  catalogText() {
    return this.docs().map((d) => {
      const day = d.day ? `Buổi ${d.day}` : "Cả khóa";
      const date = d.date ? ` (${fmtDate(d.date)})` : "";
      const ex = d.exercises?.length ? ` · ${d.exercises.length} bài tập` : "";
      return `- [${d.id}] ${day}${date} · ${KIND_VI[d.kind] || "Khác"} · ${d.title || d.name} — file ${d.name}${ex}. ${d.description || ""}`;
    }).join("\n");
  }

  /** Content block for one document (uploaded file id when available, else the file inline). */
  async block(doc) {
    const ext = doc.ext, title = (doc.title || doc.name).slice(0, 250);
    if (doc.file_id) {
      if (IMAGE_EXT.has(ext)) return { type: "image", source: { type: "file", file_id: doc.file_id } };
      return { type: "document", source: { type: "file", file_id: doc.file_id }, title };
    }
    if (IMAGE_EXT.has(ext) || (ext === ".pdf" && (doc.size || 0) <= 20 * MB && (doc.pages || 0) <= 500)) {
      try { return await fileToBlock(await this.file(doc)); } catch { /* fall back to text */ }
    }
    const text = (await this.text(doc)) || `(không đọc được nội dung file ${doc.name})`;
    return { type: "text", text: `<file name="${doc.name}" title="${title}">\n${text}\n</file>` };
  }
}

// =====================================================================
// Folder sync: scan -> remove duplicates -> copy -> upload -> catalogue
// =====================================================================
const CATALOG_SCHEMA = {
  type: "object", additionalProperties: false,
  required: ["title", "description", "kind", "day", "date", "exercises"],
  properties: {
    title: { type: "string" }, description: { type: "string" },
    kind: { type: "string", enum: KINDS }, day: { type: "integer" }, date: { type: "string" },
    exercises: { type: "array", items: {
      type: "object", additionalProperties: false, required: ["title", "summary", "location"],
      properties: { title: { type: "string" }, summary: { type: "string" }, location: { type: "string" } } } },
  },
};
const CATALOG_SYSTEM = (
  "You catalogue course/study materials for a Vietnamese learner. Read the document and return JSON only. " +
  "title: a short clear title (keep the document's own title if it has one). " +
  "description: 1-2 Vietnamese sentences on what it contains. " +
  "kind: agenda (course schedule/programme), book (textbook, chapter, reading), slides (lecture slides), " +
  "exercise (exercise sheet/assignment), journal_template (a blank journal/log/reflection form the learner " +
  "fills in after a session), certificate, reference, other. " +
  "day: the course day/session number it belongs to, from the content, the file name or the folder names " +
  "(e.g. 'Day 2', 'Ngày 3', 'Buổi 4', 'Session 5', 'Module 1'); 0 if unknown or for the whole course. " +
  "date: the session date as YYYY-MM-DD if stated, else an empty string. " +
  "exercises: every exercise, assignment, practice task or question set the learner must do that appears in " +
  "the document (title as written, one-sentence Vietnamese summary, location such as page/section); " +
  "an empty list if there are none.");
const DAYS_SCHEMA = {
  type: "object", additionalProperties: false, required: ["docs"],
  properties: { docs: { type: "array", items: {
    type: "object", additionalProperties: false, required: ["id", "day", "date"],
    properties: { id: { type: "string" }, day: { type: "integer" }, date: { type: "string" } } } } },
};
const DAYS_SYSTEM = (
  "You organise a course library into sessions (days). For each document you get its folder path, title, " +
  "kind and a first guess of its day number and date. Return JSON with a consistent day number for every id " +
  "(1, 2, 3... in course order; 0 for whole-course material such as the agenda or a journal template) and " +
  "its date (YYYY-MM-DD or empty). Use the agenda text, when given, to match days and dates.");

class Syncer {
  constructor(lib, apiKey, progress) {
    this.lib = lib;
    this.key = apiKey;
    this.progress = progress;
    this.filesApi = true;
  }

  /** files: the File objects the user picked (null = only finish uploads/cataloguing still missing). */
  async run(files) {
    const lib = this.lib;
    const problems = [];
    let found = [], dup = 0, known = 0;
    const fresh = [];
    if (files) {
      this.progress("Đang quét tài liệu...");
      found = files.filter((f) => !f.name.startsWith("~$") && !f.name.startsWith(".") && SUPPORTED.has(extOf(f.name)));
      const groups = new Map();
      for (let i = 0; i < found.length; i++) {
        if (i % 10 === 0) this.progress(`Đang kiểm tra trùng lặp ${i + 1}/${found.length}...`);
        try {
          const h = await sha256(found[i]);
          (groups.get(h) || groups.set(h, []).get(h)).push(found[i]);
        } catch { /* unreadable */ }
      }
      for (const v of groups.values()) dup += v.length - 1;
      const today = isoDate();
      for (const [sha, list] of groups) {
        // keep the "cleanest" name: no "(1)"/"- Copy" mark, shortest, oldest
        list.sort((a, b) => (copyMarked(a.name) - copyMarked(b.name)) || (a.name.length - b.name.length) ||
                            (a.lastModified - b.lastModified));
        const rels = list.map((f) => f.webkitRelativePath || f.name);
        const id = sha.slice(0, 12);
        const old = lib.data.docs[id];
        if (old) {
          known++;
          old.sources = [...new Set([...(old.sources || []), ...rels])].sort();
          continue;
        }
        const src = list[0];
        const ext = extOf(src.name);
        let text = "", pages = 0;
        try {
          await idbPut("files", id, src.slice(0, src.size, src.type || "application/octet-stream"));
          [text, pages] = await extractText(src, ext);
        } catch (e) {
          problems.push(`${src.name}: ${e.message || e}`);
          continue;
        }
        await idbPut("text", id, text);
        const doc = { id, sha, name: src.name, ext, stored: `${id}_${safeName(src.name)}`, rel: rels[0], sources: rels,
          size: src.size, pages, added: today, mtime: isoDate(new Date(src.lastModified || Date.now())), file_id: "",
          title: stem(src.name), description: "", kind: "other", day: 0, date: "", exercises: [], catalogued: false };
        lib.data.docs[id] = doc;
        fresh.push(doc);
      }
      const top = found[0]?.webkitRelativePath?.split("/")[0];
      lib.data.folder = top || `${found.length} file đã chọn`;
      await lib.save();
    }

    // upload everything that has no file id yet (new files, or ones that failed last time)
    let todo = lib.docs().filter((d) => !d.file_id);
    for (let i = 0; i < todo.length && this.filesApi; i++) {
      const d = todo[i];
      this.progress(`Đang upload lên Claude ${i + 1}/${todo.length}: ${d.name}`);
      try {
        await this.upload(d);
      } catch (e) {
        if (e instanceof ClaudeError && [401, 403, 404].includes(e.status)) {
          this.filesApi = false;          // this login can't use the Files API: send files inline instead
          problems.push(`Upload file không được hỗ trợ với tài khoản này (${e.status}); tài liệu sẽ được gửi kèm trực tiếp khi hỏi.`);
        } else {
          problems.push(`${d.name}: upload lỗi (${friendlyError(e)})`);
        }
      }
    }
    await lib.save();

    // catalogue (new files and any that failed before), a few at a time
    todo = lib.docs().filter((d) => !d.catalogued);
    if (todo.length) {
      let done = 0;
      this.progress(`Claude đang đọc và phân loại tài liệu 0/${todo.length}...`);
      const queue = [...todo];
      const worker = async () => {
        for (let d = queue.shift(); d; d = queue.shift()) {
          try { await this.catalogue(d); } catch (e) { problems.push(`${d.name}: chưa phân loại được (${friendlyError(e)})`); }
          done++;
          this.progress(`Claude đang đọc và phân loại tài liệu ${done}/${todo.length}...`);
        }
      };
      await Promise.all([worker(), worker(), worker()]);
      await lib.save();
    }
    if (fresh.length || todo.length) {
      this.progress("Đang sắp xếp tài liệu theo buổi học...");
      try { await this.arrangeDays(); } catch (e) { problems.push(`Chưa sắp xếp lại được theo buổi (${friendlyError(e)})`); }
    }
    lib.data.synced = stamp();
    await lib.save();
    return { found: found.length, new: fresh.length, known, dup, problems, total: Object.keys(lib.data.docs).length };
  }

  async upload(doc) {
    const file = await this.lib.file(doc);
    let blob, name;
    if (IMAGE_EXT.has(doc.ext)) {
      if (doc.size > 5 * MB) return;                      // too big for Claude; skipped
      blob = file; name = doc.name;
    } else if (doc.ext === ".pdf") {
      blob = new Blob([file], { type: "application/pdf" }); name = doc.name;
    } else {                                              // Word / text -> its plain text
      blob = new Blob([await this.lib.text(doc)], { type: "text/plain" }); name = stem(doc.name) + ".txt";
    }
    doc.file_id = await uploadFile(this.key, blob, name);
  }

  async catalogue(doc) {
    let res;
    try {
      const block = await this.lib.block(doc);
      const info = `Đường dẫn trong thư mục học: ${doc.rel}\nNgày sửa file: ${doc.mtime}`;
      res = await askJson(this.key, CATALOG_SYSTEM, [block, { type: "text", text: info }], CATALOG_SCHEMA);
    } catch (e) {
      if (!(e instanceof ClaudeError && e.status === 400)) throw e;
      // e.g. a PDF over the page limit: catalogue it from its text (first part is enough for this)
      const text = (await this.lib.text(doc)).slice(0, 150_000);
      const info = `Đường dẫn: ${doc.rel}\nNgày sửa file: ${doc.mtime}\n\n<file name="${doc.name}">\n${text}\n</file>`;
      res = await askJson(this.key, CATALOG_SYSTEM, info, CATALOG_SCHEMA);
    }
    Object.assign(doc, { title: clean(res.title) || doc.title, description: res.description || "",
      kind: KINDS.includes(res.kind) ? res.kind : "other", day: Math.max(0, parseInt(res.day, 10) || 0),
      date: res.date || "", exercises: res.exercises || [], catalogued: true });
  }

  async arrangeDays() {
    const docs = this.lib.docs();
    if (docs.length < 2) return;
    const listing = docs.map((d) => ({ id: d.id, path: d.rel, title: d.title, kind: d.kind, day: d.day, date: d.date }));
    const agendas = [];
    for (const d of docs) if (d.kind === "agenda") agendas.push((await this.lib.text(d)).slice(0, 20_000));
    let prompt = "Documents:\n[\n" + listing.map((x) => JSON.stringify(x)).join(",\n") + "\n]";
    if (agendas.length) prompt += `\n\nAgenda text:\n${agendas.join("\n\n")}`;
    const res = await askJson(this.key, DAYS_SYSTEM, prompt, DAYS_SCHEMA);
    for (const r of res.docs || []) {
      const d = this.lib.data.docs[r.id];
      if (d) {
        d.day = Math.max(0, parseInt(r.day, 10) || 0);
        d.date = r.date || d.date || "";
      }
    }
  }
}

// =====================================================================
// Journal: fill a PDF template
// =====================================================================
const JOURNAL_SCHEMA = {
  type: "object", additionalProperties: false, required: ["fields", "placements", "preview"],
  properties: {
    fields: { type: "array", items: {
      type: "object", additionalProperties: false, required: ["name", "value"],
      properties: { name: { type: "string" }, value: { type: "string" } } } },
    placements: { type: "array", items: {
      type: "object", additionalProperties: false, required: ["line_id", "position", "label", "text"],
      properties: { line_id: { type: "integer" }, position: { type: "string", enum: ["right", "below"] },
                    label: { type: "string" }, text: { type: "string" } } } },
    preview: { type: "string" },
  },
};
const JOURNAL_SYSTEM = (
  "You fill in a learner's session journal (nhật ký buổi học) using a PDF template, the course documents of " +
  "that session and what the teacher said (speech transcript and Vietnamese translation, if any). Write in " +
  "the template's language, first person as the learner, concrete and concise, sized to fit the blank space. " +
  "Return JSON only:\n" +
  "- fields: if the template has form fields (listed), a value for each field to fill (exact field name; " +
  "for checkboxes use 'yes' or 'no'). Otherwise an empty list.\n" +
  "- placements: if the template has no form fields, one entry per blank to fill: line_id = the id of the " +
  "printed label/question line; position = 'right' when the blank continues on the same line (e.g. " +
  "'Date: ........'), or 'below' when the answer goes in the empty space under that line; label = the exact " +
  "text on that line that comes before the blank (empty if not needed); text = the answer only (do not " +
  "repeat the label). Use each line's bbox (x0, y0, x1, y1 in points) and the gap to the next line to judge " +
  "how much fits. Leave signature fields empty. Otherwise an empty list.\n" +
  "- preview: the whole filled journal as readable plain text (labels and answers) to show the learner.");

/** Form fields and printed text lines (with positions, top-left origin like the desktop app) of a PDF. */
export async function templateLayout(buf) {
  const pdf = await openPdf(buf);
  const fields = [], lines = [], sizes = [], views = [];
  for (let p = 0; p < pdf.numPages; p++) {
    const page = await pdf.getPage(p + 1);
    const [vx, vy, vx1, vy1] = page.view;
    const H = vy1 - vy;
    sizes.push([Math.round(vx1 - vx), Math.round(H)]);
    views.push([vx, vy, vx1 - vx, H]);
    const r1 = (v) => Math.round(v * 10) / 10;
    for (const a of await page.getAnnotations()) {
      if (a.subtype !== "Widget" || !a.fieldName) continue;
      const type = a.fieldType === "Btn" ? (a.checkBox ? "CheckBox" : a.radioButton ? "RadioButton" : "Button")
        : a.fieldType === "Ch" ? "ComboBox" : "Text";
      const [x0, y0, x1, y1] = a.rect;
      fields.push({ name: a.fieldName, type, page: p, bbox: [r1(x0 - vx), r1(H - (y1 - vy)), r1(x1 - vx), r1(H - (y0 - vy))] });
    }
    const items = (await page.getTextContent()).items.filter((it) => it.str && it.str.trim())
      .map((it) => {
        const size = Math.hypot(it.transform[2], it.transform[3]) || it.height || 10;
        return { str: it.str, x: it.transform[4] - vx, base: it.transform[5] - vy, w: it.width, size };
      })
      .sort((a, b) => (Math.abs(a.base - b.base) > 1 ? b.base - a.base : a.x - b.x));
    let cur = null;
    const flush = () => {
      if (!cur) return;
      const text = cur.text.trim();
      if (text) {
        lines.push({ id: lines.length, page: p, text, size: r1(cur.size),
          bbox: [r1(cur.x0), r1(H - cur.base - cur.size * 0.8), r1(cur.x1), r1(H - cur.base + cur.size * 0.25)] });
      }
      cur = null;
    };
    for (const it of items) {
      const sameRow = cur && Math.abs(it.base - cur.base) < cur.size * 0.5;
      const gap = cur ? it.x - cur.x1 : 0;
      if (sameRow && gap < cur.size * 1.5) {
        cur.text += (gap > cur.size * 0.15 && !cur.text.endsWith(" ") ? " " : "") + it.str;
        cur.x1 = Math.max(cur.x1, it.x + it.w);
        cur.size = Math.max(cur.size, it.size);
      } else {
        flush();
        cur = { text: it.str, x0: it.x, x1: it.x + it.w, base: it.base, size: it.size };
      }
    }
    flush();
  }
  pdf.destroy();
  return { fields, lines, sizes, views };
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error("không tải được " + src));
    document.head.append(s);
  });
}

let pdfLibP = null;
function pdfLib() {
  pdfLibP = pdfLibP || Promise.all([loadScript(ROOT + "lib/pdf-lib-1.17.1/pdf-lib.min.js"),
                                    loadScript(ROOT + "lib/pdf-lib-1.17.1/fontkit.umd.min.js")]).then(() => window.PDFLib);
  return pdfLibP;
}

/** Write the answers into a copy of the template PDF. Returns the new PDF bytes. */
export async function fillTemplate(buf, values, placements, layout) {
  const L = await pdfLib();
  const doc = await L.PDFDocument.load(buf, { ignoreEncryption: true });
  doc.registerFontkit(window.fontkit);
  const fontBytes = await (await fetch(ROOT + "lib/pdf-lib-1.17.1/DejaVuSans.ttf")).arrayBuffer();
  const font = await doc.embedFont(fontBytes, { subset: true });   // Vietnamese letters need a real font
  const pages = doc.getPages();
  const color = L.rgb(0.05, 0.15, 0.55);

  const wrap = (text, size, width) => {
    const out = [];
    for (const para of text.split("\n")) {
      let line = "";
      for (const word of para.split(/\s+/).filter(Boolean)) {
        const t = line ? line + " " + word : word;
        if (font.widthOfTextAtSize(t, size) <= width) { line = t; continue; }
        if (line) out.push(line);
        line = word;
        while (font.widthOfTextAtSize(line, size) > width && line.length > 1) {   // a very long word
          let k = line.length - 1;
          while (k > 1 && font.widthOfTextAtSize(line.slice(0, k), size) > width) k--;
          out.push(line.slice(0, k));
          line = line.slice(k);
        }
      }
      out.push(line);
    }
    return out;
  };
  // Write text into a top-left-origin rect, shrinking the font until it fits. True if written.
  const writeFit = (pno, [x0, y0, x1, y1], text, maxSize = 11, minSize = 6) => {
    const [vx, vy, , H] = layout.views[pno];
    const width = x1 - x0, height = y1 - y0;
    for (let size = maxSize; size >= minSize; size -= 0.5) {
      const lines = wrap(text, size, width);
      const lh = size * 1.18;
      if (lines.length * lh > height + 0.5) continue;
      lines.forEach((t, i) => pages[pno].drawText(t, { x: vx + x0, y: vy + H - y0 - size - i * lh, size, font, color }));
      return true;
    }
    return false;
  };

  // form fields
  const vals = Object.fromEntries(values.filter((v) => (v.value || "").trim()).map((v) => [v.name, v.value]));
  const form = doc.getForm();
  const pageOf = (widget) => {
    const ref = widget.P();
    let i = pages.findIndex((p) => ref && p.ref === ref);
    if (i < 0) i = pages.findIndex((p) => (p.node.Annots()?.asArray() || []).some((a) => doc.context.lookup(a) === widget.dict));
    return i;
  };
  for (const field of form.getFields()) {
    const name = field.getName();
    if (!(name in vals)) continue;
    const v = vals[name];
    const yes = ["yes", "true", "x", "1", "có", "on"].includes(v.trim().toLowerCase());
    if (field instanceof L.PDFCheckBox) { yes ? field.check() : field.uncheck(); continue; }
    if (field instanceof L.PDFRadioGroup) { if (yes && field.getOptions()[0]) field.select(field.getOptions()[0]); continue; }
    const widgets = field.acroField.getWidgets();
    const spots = widgets.map((w) => [pageOf(w), w.getRectangle()]);
    // take the widgets off every page first (pdf-lib leaves them in /Annots when a widget has no /P)
    const refs = widgets.map((w) => doc.context.getObjectRef(w.dict)).filter(Boolean);
    for (const p of pages) {
      const annots = p.node.Annots();
      if (!annots) continue;
      for (let i = annots.size() - 1; i >= 0; i--) if (refs.some((r) => r === annots.get(i))) annots.remove(i);
    }
    form.removeField(field);                               // draw the text where the field was (flattened)
    for (const [pno, r] of spots) {
      if (pno < 0) continue;
      const [vx, vy, , H] = layout.views[pno];
      const top = H - (r.y - vy) - r.height, left = r.x - vx;
      writeFit(pno, [left + 2, top + 1, left + r.width - 2, top + r.height - 1], v, Math.min(11, Math.max(6, r.height * 0.7)));
    }
  }

  // text placed next to / under the template's printed lines
  const byId = new Map(layout.lines.map((l) => [l.id, l]));
  for (const pl of placements) {
    const ln = byId.get(pl.line_id);
    const text = (pl.text || "").trim();
    if (!ln || !text) continue;
    const [x0, y0, x1, y1] = ln.bbox;
    const samePage = layout.lines.filter((o) => o.page === ln.page && o.id !== ln.id);
    const [, , W, H] = layout.views[ln.page];
    let rightEdge = W - 28;
    const maxSize = Math.min(11, Math.max(7, ln.size));
    if (pl.position === "right") {
      let start = x1 + 3;
      const label = (pl.label || "").trim();
      const at = label ? ln.text.indexOf(label) : -1;
      if (at >= 0) {                                       // where the label ends, by real glyph widths
        const full = font.widthOfTextAtSize(ln.text, 10) || 1;
        start = x0 + ((x1 - x0) * font.widthOfTextAtSize(ln.text.slice(0, at + label.length), 10)) / full + 3;
      }
      for (const o of samePage) {                          // another label further right on the same row
        const ov = Math.min(y1, o.bbox[3]) - Math.max(y0, o.bbox[1]);
        if (ov > 0.5 * (y1 - y0) && o.bbox[0] > start + 5) rightEdge = Math.min(rightEdge, o.bbox[0] - 4);
      }
      if (writeFit(ln.page, [start, y0 - 1, rightEdge, y1 + 2], text, maxSize, 7)) continue;
      const below = samePage.filter((o) => o.bbox[1] >= y1 - 0.5 && o.bbox[0] < rightEdge && o.bbox[2] > start).map((o) => o.bbox[1]);
      writeFit(ln.page, [start, y0 - 1, rightEdge, below.length ? Math.min(...below) - 1 : H - 28], text, maxSize);
    } else {
      const below = samePage.filter((o) => o.bbox[1] >= y1 - 0.5 && o.bbox[0] < rightEdge && o.bbox[2] > x0).map((o) => o.bbox[1]);
      let bottom = below.length ? Math.min(...below) - 1 : H - 28;
      if (bottom - y1 < 12) bottom = y1 + 40;
      writeFit(ln.page, [x0, y1 + 1, rightEdge, bottom], text, maxSize);
    }
  }
  return doc.save({ updateFieldAppearances: false });
}

/** Ask Claude for the journal content and write it into the template. Returns {name, blob, preview}. */
async function makeJournal(apiKey, lib, template, dayDocs, sessionText, instruction) {
  const tfile = await lib.file(template);
  const buf = await tfile.arrayBuffer();
  const layout = await templateLayout(buf);
  const content = [await fileToBlock(tfile)];
  for (const d of dayDocs) if (d.id !== template.id) content.push(await lib.block(d));
  const info = { page_sizes: layout.sizes, form_fields: layout.fields,
                 lines: layout.lines.map(({ id, page, bbox, text }) => ({ id, page, bbox, text })) };
  content.push({ type: "text", text:
    `Template layout (from the PDF):\n${JSON.stringify(info)}\n\n` +
    `Session content from the app:\n${sessionText || "(không có)"}\n\n` +
    `Today: ${isoDate()}\n\nYêu cầu của người học: ${instruction}` });
  const res = await askJson(apiKey, JOURNAL_SYSTEM, content, JOURNAL_SCHEMA, { effort: "medium", maxTokens: 32000 });
  const d = new Date();
  const name = `NhatKy_${safeName(stem(template.name))}_${isoDate(d).replaceAll("-", "")}_${pad(d.getHours())}${pad(d.getMinutes())}.pdf`;
  const bytes = await fillTemplate(buf, res.fields || [], res.placements || [], layout);
  const blob = new Blob([bytes], { type: "application/pdf" });
  return { name, blob, preview: res.preview || "" };
}

// =====================================================================
// Exercises: big exercises with steps (read from all files), step plans, solution PDF (v1.8.1+)
// =====================================================================
const STR = { type: "string" };
const STEPS_SCHEMA = { type: "array", items: {
  type: "object", additionalProperties: false, required: ["title", "instruction"],
  properties: { title: STR, instruction: STR } } };
const BIGEX_SCHEMA = {
  type: "object", additionalProperties: false, required: ["exercises"],
  properties: { exercises: { type: "array", items: {
    type: "object", additionalProperties: false, required: ["title", "goal", "location", "steps"],
    properties: { title: STR, goal: STR, location: STR, steps: STEPS_SCHEMA } } } } };
const MERGE_SCHEMA = {
  type: "object", additionalProperties: false, required: ["exercises"],
  properties: { exercises: { type: "array", items: {
    type: "object", additionalProperties: false, required: ["title", "goal", "day", "doc_ids", "steps"],
    properties: { title: STR, goal: STR, day: { type: "integer" }, doc_ids: { type: "array", items: STR }, steps: STEPS_SCHEMA } } } } };
const PLAN_SCHEMA = { type: "object", additionalProperties: false, required: ["steps"], properties: { steps: STEPS_SCHEMA } };
const WHOLE_SCHEMA = {
  type: "object", additionalProperties: false, required: ["goal", "exercises"],
  properties: { goal: STR, exercises: { type: "array", items: {
    type: "object", additionalProperties: false, required: ["title", "goal", "steps"],
    properties: { title: STR, goal: STR, steps: STEPS_SCHEMA } } } } };
const BIGEX_SYSTEM = (
  "You read a course document and list its substantial exercises, assignments, labs or projects " +
  "(bài tập lớn) — tasks the learner has to carry out, especially ones whose instructions go through " +
  "concrete steps. Return JSON only. For each exercise: title (as written in the document), goal (one " +
  "Vietnamese sentence), location (pages/sections), and steps in order: follow the document's own steps " +
  "exactly when it gives them (title + instruction in Vietnamese, keeping technical terms, commands, " +
  "numbers and names from the document); if it only states the task, derive 3-8 clear steps from it. " +
  "Skip trivial one-line questions and reading-only items. Empty list if there are none.");
const MERGE_SYSTEM = (
  "You get exercises found file by file in a course folder. Return JSON with the list of distinct big " +
  "exercises: merge entries that are the same exercise, or parts of one exercise spread over several files " +
  "(keep every doc id, combine the steps in the right order without duplicates). Keep titles, goals and " +
  "step texts as given (Vietnamese). day = the course day (0 if unknown). Order by day, then by course order.");
const PLAN_SYSTEM = (
  "Break the given exercise from the attached course document into concrete steps the learner can work " +
  "through one at a time, following the document's own instructions and order (3-8 steps for one exercise; " +
  "when asked to follow the whole file, one step per exercise or instruction block, in the file's order, up " +
  "to 15 steps). Return JSON only: steps with " +
  "a short title and a Vietnamese instruction (keep technical terms, commands and values from the document).");
const WHOLE_SYSTEM = (
  "The attached course file may be one big problem made of many sub-exercises that build on each other. " +
  "Return JSON only: goal = the overall goal of the whole file (one Vietnamese sentence); exercises = every " +
  "sub-exercise in the file's own order (title as written, goal in one Vietnamese sentence, and its concrete " +
  "steps: follow the file's own steps exactly when it gives them, otherwise 2-6 clear steps; Vietnamese, " +
  "keeping technical terms, commands, numbers and names from the file). Note in a step's instruction when it " +
  "uses the result of an earlier sub-exercise. Skip reading-only parts.");

/** askJson with one library document attached (falls back to its text if the file is refused). */
async function askDoc(lib, key, system, doc, info, schema, effort = "medium") {
  try {
    return await askJson(key, system, [await lib.block(doc), { type: "text", text: info }], schema, { effort });
  } catch (e) {
    if (!(e instanceof ClaudeError && e.status === 400)) throw e;
    const text = (await lib.text(doc)).slice(0, 300_000);
    return askJson(key, system, `${info}\n\n<file name="${doc.name}">\n${text}\n</file>`, schema, { effort });
  }
}

/** [sub-exercise with steps] -> one list of steps, each knowing its sub-exercise (part). */
export function partsToSteps(parts) {
  const ps = (parts || []).filter((p) => p.steps?.length);
  const steps = [];
  ps.forEach((p, k) => p.steps.forEach((st, j) => steps.push({ title: st.title, instruction: st.instruction,
    part: k + 1, parts: ps.length, part_title: p.title, part_goal: p.goal || "", pstep: j + 1, psteps: p.steps.length })));
  return steps;
}

/** A whole-file plan that needs no Claude call: saved from before, or built from the sub-exercises (with steps)
 *  that the folder scan already found in this file. null if Claude must read the file. */
export function knownFilePlan(doc) {
  if (doc.fileplan) return [doc.fileplan.goal, doc.fileplan.steps];
  const found = doc.bigex || [];
  if (found.length && found.every((p) => p.steps?.length)) return [doc.description || "", partsToSteps(found)];
  return null;
}

/** Plan a whole file: [overall goal, steps] where each step knows its sub-exercise (part). Saved in the library. */
export async function planFile(lib, key, doc, known) {
  const info = `File: ${doc.rel}\nSub-exercises already noticed in it: ` + (known.join("; ") || "(none)");
  const res = await askDoc(lib, key, WHOLE_SYSTEM, doc, info, WHOLE_SCHEMA, "low");
  const goal = res.goal || "", steps = partsToSteps(res.exercises);
  if (steps.length) {
    doc.fileplan = { goal, steps };
    await lib.save();
  }
  return [goal, steps];
}

/** Read the files, find the big exercises with their steps and save the list in the library.
 *  Only files not read before are read again, unless `full`. Returns [number of exercises, problems]. */
export async function scanExercises(lib, key, progress, full = false) {
  const docs = lib.docs().filter((d) => !["journal_template", "certificate"].includes(d.kind));
  const todo = full ? docs : docs.filter((d) => !("bigex" in d));
  const problems = [];
  if (todo.length) {
    let done = 0;
    progress(`Claude đang đọc bài tập trong tài liệu 0/${todo.length}...`);
    const queue = [...todo];
    const worker = async () => {
      for (let d = queue.shift(); d; d = queue.shift()) {
        try {
          const res = await askDoc(lib, key, BIGEX_SYSTEM, d, `File: ${d.rel} (buổi ${d.day || "?"})`, BIGEX_SCHEMA);
          d.bigex = res.exercises || [];
          delete d.fileplan;                       // the file was read again: plans made from it are redone
          delete d.plans;
        } catch (e) {
          problems.push(`${d.name}: ${friendlyError(e)}`);
        }
        done++;
        progress(`Claude đang đọc bài tập trong tài liệu ${done}/${todo.length}...`);
      }
    };
    await Promise.all([worker(), worker(), worker()]);
    await lib.save();
  }
  const entries = docs.flatMap((d) => (d.bigex || []).map((ex) =>
    ({ doc_id: d.id, doc_title: d.title || d.name, day: d.day || 0, ...ex })));
  let merged;
  if (entries.length > 1) {
    progress("Đang gộp và sắp xếp danh sách bài tập lớn...");
    merged = (await askJson(key, MERGE_SYSTEM, JSON.stringify(entries), MERGE_SCHEMA, { effort: "medium" })).exercises || [];
  } else {
    merged = entries.map((e) => ({ title: e.title, goal: e.goal, day: e.day, doc_ids: [e.doc_id], steps: e.steps }));
  }
  const out = [];
  merged.forEach((ex, i) => {
    const ids = (ex.doc_ids || []).filter((x) => lib.get(x));
    if (ex.steps?.length) out.push({ ...ex, doc_ids: ids, id: `bx${i + 1}` });
  });
  lib.data.big_exercises = out;
  lib.data.big_exercises_updated = stamp();
  await lib.save();
  return [out.length, problems];
}

/** Steps for an exercise from the per-document list (it has none yet). Saved, so the next start is instant. */
export async function planSteps(lib, key, ex) {
  const doc = ex.doc;
  const saved = (doc.plans || {})[ex.title];
  if (saved) return saved;
  const info = `Exercise: ${ex.title}\nSummary: ${ex.summary || ""}\nLocation: ${ex.location || ""}\nFile: ${doc.rel}`;
  const steps = (await askDoc(lib, key, PLAN_SYSTEM, doc, info, PLAN_SCHEMA, "low")).steps || [];
  if (steps.length) {
    (doc.plans = doc.plans || {})[ex.title] = steps;
    await lib.save();
  }
  return steps;
}

// ---- guided exercises: rules for Claude and the markers it writes (v1.8.5) ----
export const EXERCISE_RULES = (
  "<exercise_rules>\n" +
  "You guide the learner through ONE step of a course exercise. Be very brief: no long explanations.\n" +
  "1. If this step needs information only the learner can give (their own data, choices or parameters, or the " +
  "blanks of a template from the course book) and the message does not contain it yet, only ask for those " +
  "items as a short numbered list and end with the line [[CẦN NHẬP: item 1 | item 2]]. Do nothing else.\n" +
  "2. Otherwise do the step and give the result in a few lines. If the step needs a prompt for an AI tool, give " +
  "the complete prompt between a line '----- PROMPT -----' and a line '----- HẾT PROMPT -----' (copy the " +
  "course book's template when it has one, filled in with the learner's information).\n" +
  "3. Then one line: '➡ Mang sang bước sau: ...' with only what the next step needs from this result (very " +
  "short). Skip it at the last step.\n" +
  "4. If the next step (see <next_step>) will need information from the learner, end with the line " +
  "[[CẦN NHẬP TIẾP: item 1 | item 2]] (item names in Vietnamese).\n" +
  "5. Finish with the line [[XONG BƯỚC]].\n" +
  "Reply in Vietnamese.\n" +
  "</exercise_rules>");
export const OVERVIEW_RULES = (
  "<overview_rules>\n" +
  "Summarize the exercise in 2-3 short lines (task, goal, expected result) — just enough to understand it. " +
  "Do not start any step. If the first step (see <next_step>) will need information from the learner, end " +
  "with the line [[CẦN NHẬP TIẾP: item 1 | item 2]] (item names in Vietnamese). Reply in Vietnamese.\n" +
  "</overview_rules>");
const MARKER = /\[\[\s*(?:CẦN NHẬP|CAN NHAP|XONG BƯỚC|XONG BUOC)[^\]]*\]\]/gi;
const NEEDS_INPUT = /\[\[\s*(?:CẦN NHẬP|CAN NHAP)(?!\s*TI)\s*:?\s*([^\]]*)\]\]/i;
const NEXT_INPUT = /\[\[\s*(?:CẦN NHẬP TIẾP|CAN NHAP TIEP)\s*:?\s*([^\]]*)\]\]/i;
const CARRY = /(?:➡\s*)?Mang sang bước sau\s*:\s*(.+)/i;
const DONE = /\[\[\s*(?:XONG BƯỚC|XONG BUOC)\s*\]\]/i;
const PROMPT_BLOCK = /-{3,} *PROMPT *-{3,} *\n([\s\S]*?)\n *-{3,} *HẾT PROMPT *-{3,}/gi;
export const stripMarkers = (t) => String(t || "").replace(MARKER, "").replace(/[ \t]+\n/g, "\n").trim();
const NO_EMOJI = /[\u{10000}-\u{10FFFF}☀-➿️‍]/gu;    // the PDF font has no emoji
const fields = (m) => (m ? m[1].split("|").map((f) => f.trim()).filter(Boolean) : null);

/** A short stable id for some text (saved answers and exercise progress belong to it). */
export function hashKey(text) {
  let h1 = 0x811c9dc5, h2 = 0x01000193;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 0x01000193) >>> 0;
    h2 = Math.imul(h2 ^ c, 0x5bd1e995) >>> 0;
  }
  return h1.toString(16).padStart(8, "0") + h2.toString(16).padStart(8, "0");
}

/** The whole file as one exercise (sub-exercise by sub-exercise). */
export const fileExercise = (doc, goal, steps) => ({ title: clean(doc.title || doc.name), goal, day: doc.day || 0,
  doc_ids: [doc.id], steps, whole_file: true });
export const singleExercise = (doc, title, goal, steps) => ({ title, goal, day: doc.day || 0, doc_ids: [doc.id], steps });
/** Identifies an exercise (its files, title and steps): saved progress belongs to it. */
export const exerciseKey = (ex) => hashKey(JSON.stringify([clean(ex.title), [...(ex.doc_ids || [])].sort(),
  (ex.steps || []).map((s) => clean(s.title))]));

/** Paragraphs of Claude's plain text (light Markdown) -> blocks of [text, bold] runs. */
function paraBlocks(text) {
  return (text || "").trim().split(/\n\s*\n/).filter((p) => p.trim()).map((para) => {
    const runs = [];
    para.split("\n").forEach((line, k) => {
      if (k) runs.push(["\n", false]);
      let ln = line.replace(/\s+$/, "");
      const h = /^#{1,6}\s+(.*)/.exec(ln);
      if (h) { runs.push([h[1].replace(/\*\*/g, ""), true]); return; }
      ln = ln.replace(/^(\s*)[-*]\s+/, "$1• ");
      ln.split(/(\*\*.+?\*\*)/).forEach((part) => {
        if (!part) return;
        const b = /^\*\*(.+)\*\*$/.exec(part);
        runs.push(b ? [b[1], true] : [part, false]);
      });
    });
    return { type: "p", runs };
  });
}

/** Claude's answer -> PDF blocks; ready-to-use prompts go in a shaded box headed "Prompt:". */
export function textBlocks(text) {
  const t = stripMarkers(text);
  const out = [];
  let pos = 0;
  for (const m of t.matchAll(PROMPT_BLOCK)) {
    out.push(...paraBlocks(t.slice(pos, m.index)));
    out.push({ type: "plabel", runs: [["Prompt:", true]] });
    out.push({ type: "prompt", runs: m[1].trim().split("\n").flatMap((ln, k) => (k ? [["\n", false], [ln, false]] : [[ln, false]])) });
    pos = m.index + m[0].length;
  }
  out.push(...paraBlocks(t.slice(pos)));
  return out;
}

/** A4 PDF from simple blocks ({type: h1|h2|h3|meta|instr|p|plabel|prompt, runs: [[text, bold]]}), Vietnamese font. */
export async function writePdf(blocks) {
  const L = await pdfLib();
  const doc = await L.PDFDocument.create();
  doc.registerFontkit(window.fontkit);
  const get = async (f) => (await fetch(ROOT + "lib/pdf-lib-1.17.1/" + f)).arrayBuffer();
  const regular = await doc.embedFont(await get("DejaVuSans.ttf"), { subset: true });
  const bold = await doc.embedFont(await get("DejaVuSans-Bold.ttf"), { subset: true });
  const rgb = (h) => L.rgb(((h >> 16) & 255) / 255, ((h >> 8) & 255) / 255, (h & 255) / 255);
  const ST = { h1: { size: 17, color: 0x8a3f22, bold: true, before: 0, after: 6 },
               h2: { size: 13.5, color: 0x8a3f22, bold: true, before: 14, after: 4 },
               h3: { size: 12, color: 0x1f1f1f, bold: true, before: 10, after: 3 },
               meta: { size: 9.5, color: 0x666666, before: 2, after: 4 },
               instr: { size: 10.5, color: 0x444444, before: 2, after: 4 },
               plabel: { size: 11, color: 0x1f1f1f, before: 4, after: 1 },
               prompt: { size: 10, color: 0x1f1f1f, before: 2, after: 6, box: true },
               p: { size: 11, color: 0x1f1f1f, before: 4, after: 4 } };
  const W = 595.28, H = 841.89, M = 50;
  let page = doc.addPage([W, H]);
  let y = H - M;
  for (const b of blocks) {
    const st = ST[b.type] || ST.p;
    const size = st.size, lh = size * 1.35, pad = st.box ? 6 : 0, maxW = W - 2 * M - 2 * pad;
    y -= st.before;
    // words with their font; "\n" forces a new line
    const words = [];
    for (const [raw, isBold] of b.runs) {
      const font = isBold || st.bold ? bold : regular;
      if (raw === "\n") { words.push({ br: true }); continue; }
      const text = raw.replace(NO_EMOJI, "");
      for (const w of text.split(/(\s+)/)) if (w) words.push({ text: w, font, space: /^\s+$/.test(w) });
    }
    const lines = [[]];
    let x = 0;
    for (const w of words) {
      if (w.br) { lines.push([]); x = 0; continue; }
      let width = w.font.widthOfTextAtSize(w.text, size);
      if (w.space) { if (x > 0) { lines[lines.length - 1].push({ ...w, text: " ", x }); x += w.font.widthOfTextAtSize(" ", size); } continue; }
      if (x + width > maxW && x > 0) { lines.push([]); x = 0; }
      let t = w.text;
      while (width > maxW) {                                  // a very long word (URL, path)
        let k = t.length - 1;
        while (k > 1 && w.font.widthOfTextAtSize(t.slice(0, k), size) > maxW) k--;
        lines[lines.length - 1].push({ ...w, text: t.slice(0, k), x: 0 });
        lines.push([]);
        t = t.slice(k);
        width = w.font.widthOfTextAtSize(t, size);
      }
      lines[lines.length - 1].push({ ...w, text: t, x });
      x += width;
    }
    // a shaded box (ready-to-use prompt) is drawn piece by piece, one piece per page it spans
    let boxTop = y;
    const closeBox = (bottom) => {
      if (!st.box) return;
      page.drawRectangle({ x: M, y: bottom, width: W - 2 * M, height: boxTop - bottom,
                           color: rgb(0xf4efe8), borderColor: rgb(0xd9cfc4), borderWidth: 0.8 });
    };
    const drawn = [];
    if (st.box) y -= pad;
    for (const line of lines) {
      if (y - lh < M) {
        closeBox(y - pad);
        for (const f of drawn) f();
        drawn.length = 0;
        page = doc.addPage([W, H]);
        y = H - M;
        boxTop = y;
        if (st.box) y -= pad;
      }
      y -= lh;
      const yy = y, pg = page;
      for (const w of line) drawn.push(() => pg.drawText(w.text, { x: M + pad + w.x, y: yy + (lh - size) / 2, size, font: w.font, color: rgb(st.color) }));
    }
    if (st.box) y -= pad;
    closeBox(y);
    for (const f of drawn) f();                             // text on top of the box
    y -= st.after;
  }
  return doc.save();
}

/** The whole key-takeaways history (blocks headed "[HH:MM]") as a PDF. Returns {name, blob}. */
export async function takeawaysPdf(history, title = "Ý chính / Key takeaways") {
  const d = new Date();
  const when = `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const blocks = [{ type: "h1", runs: [[title, false]] }, { type: "meta", runs: [[`Xuất ngày ${when}`, false]] }];
  for (let block of history.trim().split(/\n\s*\n(?=\[\d\d:\d\d\]\n)/)) {
    const t = /^\[(\d\d:\d\d)\]\n/.exec(block);
    if (t) {
      blocks.push({ type: "h3", runs: [[t[1], false]] });
      block = block.slice(t[0].length);
    }
    blocks.push(...textBlocks(block));
  }
  const name = `KeyTakeaways_${isoDate(d).replaceAll("-", "")}_${pad(d.getHours())}${pad(d.getMinutes())}.pdf`;
  return { name, blob: new Blob([await writePdf(blocks)], { type: "application/pdf" }) };
}

/** The exercise solution (overview, summary, every step with the learner's inputs) as a PDF. Returns {name, blob}. */
export async function solutionPdf(lib, session, summary) {
  const ex = session.ex;
  const docs = (ex.doc_ids || []).map((i) => lib.get(i)).filter(Boolean);
  const d = new Date();
  const meta = [`Ngày ${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`];
  if (ex.day) meta.push(`Buổi ${ex.day}`);
  if (docs.length) meta.push("Tài liệu: " + docs.map((x) => clean(x.title || x.name)).join(", "));
  const one = (type, text, isBold = false) => ({ type, runs: [[text, isBold]] });
  const blocks = [one("h1", `Lời giải bài tập: ${ex.title}`), one("meta", meta.join(" · "))];
  if (ex.goal) blocks.push({ type: "p", runs: [["Mục tiêu: ", true], [ex.goal, false]] });
  if (session.overview) blocks.push(one("h2", "Tóm tắt bài tập"), ...textBlocks(session.overview));
  blocks.push(one("h2", "Tổng kết lời giải"), ...textBlocks(summary), one("h2", "Chi tiết từng bước"));
  let part = null, lastI = null;
  for (const item of session.log) {
    const st = item.step, i = item.i;
    if (st.part && st.part !== part) {
      part = st.part;
      blocks.push(one("h2", `Bài ${part}/${st.parts}: ${st.part_title}`));
      if (st.part_goal) blocks.push(one("instr", st.part_goal));
    }
    if (i !== lastI) {                                     // a step answered over several turns gets one heading
      lastI = i;
      const label = st.part ? `Bước ${st.pstep}/${st.psteps}` : `Bước ${(i || 0) + 1}`;
      blocks.push(one("h3", `${label}: ${st.title || ""}`));
      if (st.instruction) blocks.push(one("instr", st.instruction));
    }
    if (item.input) {
      let filled = item.q;
      const at = filled.toLowerCase().indexOf("thông tin của tôi:");      // only the lines the learner filled in
      if (at >= 0) filled = filled.slice(at + "thông tin của tôi:".length);
      blocks.push({ type: "instr", runs: [["Thông tin của học viên:", true], ["\n", false],
        ...filled.trim().split("\n").flatMap((ln, k) => (k ? [["\n", false], [ln, false]] : [[ln, false]]))] });
    }
    blocks.push(...textBlocks(item.a));
  }
  const bytes = await writePdf(blocks);
  const name = `LoiGiai_${safeName(ex.title).slice(0, 50)}_${isoDate(d).replaceAll("-", "")}_${pad(d.getHours())}${pad(d.getMinutes())}.pdf`;
  const blob = new Blob([bytes], { type: "application/pdf" });
  return { name, blob };
}

// ---------------- open / save / share a file on the iPad ----------------
// Inside the TestFlight app: files go to the iOS share sheet (Save to Files, Mail, WhatsApp, AirDrop...)
const NATIVE = typeof window !== "undefined" && window.LT_NATIVE && window.webkit?.messageHandlers?.lt;
async function nativeShare(blob, name, text) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  window.webkit.messageHandlers.lt.postMessage({ type: "share", name, b64: btoa(s), text: text || "" });
  return true;
}

export function openBlob(blob, name) {
  if (NATIVE) { nativeShare(blob, name); return; }
  const url = URL.createObjectURL(blob);
  const w = window.open(url, "_blank");
  if (!w) shareBlob(blob, name);                         // pop-up blocked (home-screen app): share sheet
  setTimeout(() => URL.revokeObjectURL(url), 120_000);
}

export async function shareBlob(blob, name, text) {
  if (NATIVE) return nativeShare(blob, name, text);
  const file = new File([blob], name, { type: blob.type || "application/octet-stream" });
  try {
    if (navigator.canShare?.({ files: [file] })) {
      await navigator.share({ files: [file], ...(text ? { text } : {}) });   // "Lưu vào Tệp", Mail, WhatsApp...
      return true;
    }
  } catch (e) {
    if (e?.name === "AbortError") return false;
  }
  const a = el("a");
  a.href = URL.createObjectURL(file);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 60_000);
  return true;
}

// =====================================================================
// UI
// =====================================================================
export class LearningUI {
  /** app: the main App (prompt box, chat pane, sheet dialog, status). */
  constructor(app) {
    this.app = app;
    this.lib = new Library();
    this.ready = this.lib.load();
    this.job = null;                 // documents/instructions that go with the next prompt
    this.syncing = false;
    this.session = null;             // the exercise being solved step by step
    this.sentJob = null;             // the job that went with the last sent prompt
    this.scanning = false;           // reading the folder for big exercises
  }

  // ---- menu ----
  async showMenu() {
    await this.ready;
    if (!this.app.panelSide) this.app.togglePanel("right");
    const n = Object.keys(this.lib.data.docs).length;
    const body = el("div", "lmenu");
    body.append(el("p", "muted", n ? `Thư viện: ${n} tài liệu · đồng bộ ${this.lib.data.synced || "-"}`
                                   : "Chưa có tài liệu — chọn Folder để bắt đầu"));
    const item = (label, fn, enabled = true, cls = "lmItem") => {
      const b = el("button", cls, label);
      b.disabled = !enabled;
      b.onclick = () => { this.app.closeSheet(); fn(); };
      body.append(b);
    };
    item("📁  Folder — " + (this.syncing ? "đang đồng bộ..." : "chọn thư mục học"), () => this.folder(), !this.syncing);
    if (this.lib.data.folder) item("🔄  Refresh — tìm file mới, cập nhật dữ liệu", () => this.refresh(), !this.syncing);
    if (this.cachedAgenda()) {
      item("🗓  Agenda — chương trình học (đã lưu, mở ngay)", () => this.agenda(), n > 0);
      item("🔄  Tạo lại Agenda", () => this.agenda(true), n > 0, "lmItem sub");
    } else {
      item("🗓  Agenda — chương trình học", () => this.agenda(), n > 0);
    }
    item("📚  Documents — danh sách tài liệu", () => this.documents(), n > 0);
    item("📝  Summary — tổng kết buổi học", () => this.summary(), n > 0);
    item("✏  Exercises — bài tập, giải theo từng bước", () => this.exercises(), n > 0);
    if (this.session) {
      const ex = this.session.ex;
      item(`⏹  Dừng bài "${ex.title.slice(0, 30)}" (bước ${this.session.i + 1}/${ex.steps.length})`,
           () => this.stopSession(), true, "lmItem sub");
    }
    item("📓  Journal — điền nhật ký buổi học (PDF)", () => this.journal(), n > 0);
    item("🏆  Certificate — chứng chỉ & chia sẻ", () => this.certificate());
    body.append(el("hr", "lsep"));
    const outs = (this.lib.data.outputs || []).length;
    item(`📋  List Outputs — các file đã tạo${outs ? ` (${outs})` : ""}`, () => this.listOutputs());
    this.app.sheet("🎓 Learning", body);
  }

  // ---- prompt + job ----
  setPrompt(text, job) {
    const p = document.getElementById("prompt");
    p.value = text;
    job.prompt = text;                 // to know later whether the user edited it before sending
    this.job = job;
    this.app.refreshAttachBar();
    if (!this.app.panelSide) this.app.togglePanel("right");
    p.focus({ preventScroll: true });
  }

  takeJob() {
    const job = this.job;
    this.job = null;
    this.sentJob = job;                // remembered until Claude finishes answering (guided exercises)
    return job;
  }

  async blocks(job) {
    const out = [];
    for (const id of job.doc_ids || []) {
      const d = this.lib.get(id);
      if (d) out.push(await this.lib.block(d));
    }
    return out.concat(job.blocks || []);
  }

  catalogText() {
    return this.ready.then(() => this.lib.catalogText());
  }

  /** A line from the Learning menu in the Claude answers pane. */
  chatNote(text, err = false) {
    const w = document.getElementById("chat");
    if (w.childNodes.length) w.append("\n\n");
    w.append(el("span", "who", "🎓 Learning"), " ", err ? el("span", "err", text) : text);
    this.app.scrollEnd(w);
  }

  // ---- List Outputs: every file the app created (kept on the iPad), with clear names, sortable, tap to open ----
  static OUT_KINDS = {
    takeaways: ["📄", "Ý chính (Key takeaways)"],
    journal: ["📓", "Nhật ký buổi học"],
    solution: ["✏", "Lời giải bài tập"],
    backup: ["💾", "Backup phiên nghe"],
  };

  /** Keep a file the app made and list it in List Outputs. title: what it is; info: {day, detail}. */
  async addOutput(kind, name, blob, title, info = {}) {
    await this.ready;
    const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    const key = "out/" + id;
    await idbPut("files", key, blob);
    (this.lib.data.outputs = this.lib.data.outputs || []).push({ id, key, kind, name, title: clean(title),
      detail: clean(info.detail || ""), day: info.day || 0, created: new Date().toISOString(), size: blob.size,
      type: blob.type || "application/octet-stream" });
    await this.lib.save();
  }

  /** Files made by earlier versions (journals, solutions) that are not in the list yet. */
  async adoptOldOutputs() {
    const known = new Set((this.lib.data.outputs || []).map((o) => o.key));
    const keys = await idb("files", "readonly", (s) => s.getAllKeys()).catch(() => []);
    let added = false;
    for (const key of keys || []) {
      const k = String(key);
      const kind = k.startsWith("solution/") ? "solution" : k.startsWith("journal/") ? "journal" : null;
      if (!kind || known.has(k)) continue;
      const name = k.slice(k.indexOf("/") + 1);
      const m = /_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})\.pdf$/.exec(name);
      const created = m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]).toISOString() : new Date(0).toISOString();
      const what = name.replace(/^(LoiGiai_|NhatKy_)/, "").replace(/_\d{8}_\d{4}\.pdf$/, "").replace(/_/g, " ");
      const blob = await idbGet("files", k);
      (this.lib.data.outputs = this.lib.data.outputs || []).push({ id: k, key: k, kind, name,
        title: kind === "solution" ? `Lời giải: ${what}` : `Nhật ký: ${what}`, detail: "", day: 0, created,
        size: blob?.size || 0, type: "application/pdf" });
      added = true;
    }
    if (added) await this.lib.save();
  }

  async listOutputs() {
    await this.ready;
    await this.adoptOldOutputs();
    const body = el("div", "lform");
    const bar = el("div", "actions");
    const sortSel = el("select", "wide");
    for (const [v, t] of [["new", "Mới nhất trước"], ["old", "Cũ nhất trước"], ["kind", "Theo loại file"],
                          ["name", "Theo tên (A → Z)"], ["day", "Theo buổi học"], ["size", "Theo dung lượng"]]) {
      sortSel.append(new Option(t, v));
    }
    const kindSel = el("select", "wide");
    kindSel.append(new Option("Tất cả loại file", ""));
    for (const [k, [icon, label]] of Object.entries(LearningUI.OUT_KINDS)) kindSel.append(new Option(`${icon} ${label}`, k));
    try { sortSel.value = localStorage.getItem("lt_out_sort") || "new"; } catch { /* private mode */ }
    bar.append(el("b", "", "Sắp xếp:"), sortSel, el("b", "", "Lọc:"), kindSel);
    const list = el("div", "outlist");
    const info = el("p", "muted", "");
    const fmtWhen = (iso) => {
      const d = new Date(iso);
      return d.getTime() ? `${pad(d.getHours())}:${pad(d.getMinutes())} · ${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}` : "";
    };
    const fmtSize = (n) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);
    const open = async (o) => {
      const blob = await idbGet("files", o.key);
      if (!blob) { alert("Không tìm thấy file này nữa."); return; }
      openBlob(blob, o.name);
    };
    const fill = () => {
      const outs = [...(this.lib.data.outputs || [])].filter((o) => !kindSel.value || o.kind === kindSel.value);
      const by = sortSel.value;
      const cmp = {
        new: (a, b) => b.created.localeCompare(a.created),
        old: (a, b) => a.created.localeCompare(b.created),
        kind: (a, b) => a.kind.localeCompare(b.kind) || b.created.localeCompare(a.created),
        name: (a, b) => a.title.localeCompare(b.title, "vi"),
        day: (a, b) => (a.day || 999) - (b.day || 999) || b.created.localeCompare(a.created),
        size: (a, b) => b.size - a.size,
      }[by];
      outs.sort(cmp);
      list.replaceChildren();
      let group = null;
      for (const o of outs) {
        const [icon, label] = LearningUI.OUT_KINDS[o.kind] || ["📄", "File"];
        if (by === "kind" && group !== o.kind) { group = o.kind; list.append(el("div", "outgroup", `${icon} ${label}`)); }
        if (by === "day") {
          const g = o.day ? `Buổi ${o.day}` : "Không theo buổi";
          if (group !== g) { group = g; list.append(el("div", "outgroup", g)); }
        }
        const row = el("div", "outrow");
        const main = el("div", "outmain");
        main.append(el("div", "outtitle", `${icon} ${o.title}`),
                    el("div", "outmeta", [label, o.day ? `Buổi ${o.day}` : "", fmtWhen(o.created), fmtSize(o.size)].filter(Boolean).join(" · ")),
                    ...(o.detail ? [el("div", "outmeta", o.detail)] : []),
                    el("div", "outfile", o.name));
        main.onclick = () => open(o);
        const btns = el("div", "outbtns");
        const openB = el("button", "", "Mở");
        openB.onclick = () => open(o);
        const shareB = el("button", "", "Lưu / Chia sẻ");
        shareB.onclick = async () => { const b = await idbGet("files", o.key); if (b) shareBlob(b, o.name); };
        const delB = el("button", "", "Xoá");
        delB.onclick = async () => {
          if (!confirm(`Xoá "${o.title}" khỏi danh sách và khỏi iPad?\nFile đã lưu ra ngoài (app Tệp, email...) không bị ảnh hưởng.`)) return;
          await idbDel("files", o.key).catch(() => {});
          this.lib.data.outputs = (this.lib.data.outputs || []).filter((x) => x.id !== o.id);
          await this.lib.save();
          fill();
        };
        btns.append(openB, shareB, delB);
        row.append(main, btns);
        list.append(row);
      }
      const total = (this.lib.data.outputs || []).length;
      info.textContent = total
        ? `${outs.length}/${total} file. Chạm vào một file để mở; “Lưu / Chia sẻ” để lưu vào app Tệp, gửi Mail, AirDrop...`
        : "Chưa có file nào. Các file app tạo ra (Xuất PDF ý chính, Backup, nhật ký, lời giải bài tập) sẽ hiện ở đây.";
    };
    sortSel.onchange = () => { try { localStorage.setItem("lt_out_sort", sortSel.value); } catch { /* */ } fill(); };
    kindSel.onchange = fill;
    body.append(bar, info, list);
    this.app.sheet("📋 List Outputs — các file đã tạo", body, true);
    fill();
  }

  // ---- Folder / Refresh ----
  // The desktop app reads the study folder itself; on the iPad the user picks the folder's files (Files app).
  // Picking them again finds the new ones: files already in the library are skipped (same content = same file).
  async folder(refresh = false, onDone = null) {
    await this.ready;
    const body = el("div", "lform");
    const last = this.lib.data.folder;
    body.append(el("p", "", refresh
      ? `Thư mục học: ${last} (đồng bộ ${this.lib.data.synced || "-"}). Chọn lại các file trong thư mục (app Tệp → Chọn → ` +
        "chọn tất cả): file đã có sẽ tự bỏ qua, chỉ tài liệu mới được upload và dữ liệu được làm mới."
      : "Chọn thư mục học (hoặc chọn nhiều file trong app Tệp) để Claude đọc, phân loại theo buổi và tìm bài tập."));
    const row = el("div", "actions");
    const pickFiles = el("button", "primary", "📄 Chọn file");
    const pickDir = el("button", "", "📁 Chọn cả thư mục");
    const update = el("button", "", "🔄 Chỉ cập nhật dữ liệu (không thêm file)");
    const input = el("input");
    input.type = "file";
    input.multiple = true;
    input.hidden = true;
    input.onchange = () => { const fs = [...input.files]; input.remove(); if (fs.length) this.sync(fs, onDone); };
    pickFiles.onclick = () => { this.app.closeSheet(); input.removeAttribute("webkitdirectory"); input.click(); };
    pickDir.onclick = () => { this.app.closeSheet(); input.setAttribute("webkitdirectory", ""); input.click(); };
    update.onclick = () => { this.app.closeSheet(); this.sync(null, onDone); };
    document.body.append(input);
    row.append(pickFiles);
    if ("webkitdirectory" in input) row.append(pickDir);
    if (last) row.append(update);
    body.append(row);
    this.app.sheet(refresh ? "Learning — Refresh" : "Learning — Folder", body);
  }

  refresh(onDone = null) {
    if (!this.lib.data.folder) {
      alert("Chưa chọn thư mục học (hoặc thư mục không còn). Hãy chọn Folder.");
      return;
    }
    this.folder(true, onDone);
  }

  async sync(files, onDone = null) {
    if (this.syncing) return;
    this.syncing = true;
    this.chatNote(files ? `Bắt đầu đồng bộ ${files.length} file` : "Bắt đầu làm mới thư viện");
    const progress = (msg) => this.app.setStatus("🎓 " + msg);
    try {
      const r = await new Syncer(this.lib, this.app.apiKey, progress).run(files);
      let msg = `Đồng bộ xong: ${r.found} file đã chọn, ${r.new} tài liệu mới đã upload, ${r.known} đã có từ trước, ` +
        `${r.dup} bản trùng nội dung đã bỏ qua. Thư viện có ${r.total} tài liệu.`;
      if (r.problems.length) msg += "\nLưu ý:\n" + r.problems.slice(0, 12).map((p) => "• " + p).join("\n");
      this.chatNote(msg);
      this.app.setStatus(`🎓 Đồng bộ xong: +${r.new} tài liệu mới`, "ok");
    } catch (e) {
      this.chatNote(`Đồng bộ lỗi: ${friendlyError(e)}`, true);
      this.app.setStatus(`🎓 Đồng bộ lỗi: ${friendlyError(e)}`, "err");
    } finally {
      this.syncing = false;
      onDone?.();
    }
  }

  // ---- saved answers: Agenda, "ask about this document", book-only summaries ----
  /** Changes when the library's content changes (files added/removed, days/dates/titles re-catalogued):
   *  the saved agenda is then out of date. A Refresh that finds nothing new keeps it. */
  agendaKey() {
    return hashKey(this.lib.docs().map((d) => `${d.id}:${d.day}:${d.date}:${d.kind}:${d.title}`).sort().join("\n"));
  }

  /** A saved answer if it is still valid for `key` (changes when what the answer depends on changes). */
  savedAnswer(name, key) {
    const c = (this.lib.data.answers || {})[name];
    return c && c.key === key && c.text ? c : null;
  }

  async saveAnswer(name, key, text) {
    const d = new Date();
    (this.lib.data.answers = this.lib.data.answers || {})[name] = { key, text,
      time: `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}` };
    await this.lib.save();
  }

  /** Show a saved answer in the answers pane at once (no Claude call); file names stay links. */
  showSaved(title, c, redoHint) {
    const w = document.getElementById("chat");
    if (w.childNodes.length) w.append("\n\n");
    w.append(el("span", "who", `🎓 ${title}`), " ", el("span", "file", `(đã lưu lúc ${c.time} — ${redoHint})\n`),
             el("span", "ansStart"), c.text);
    this.app.scrollEnd(w);
    try { this.linkify(); } catch { /* pane changed */ }
    if (!this.app.panelSide) this.app.togglePanel("right");
    // Claude can still answer follow-up questions about it
    const q = `${title} (đã lưu)`;
    this.app.chatter.history.push({ role: "user", content: q }, { role: "assistant", content: c.text });
    this.app.chatLog.push({ q, files: [], a: c.text });
  }

  cachedAgenda() {
    return this.lib.data ? this.savedAnswer("agenda", this.agendaKey()) : null;
  }

  // ---- Agenda ----
  async agenda(fresh = false) {
    const cached = fresh ? null : this.cachedAgenda();
    if (cached) {
      this.showSaved("Agenda", cached, "muốn tạo lại: menu Learning → 🔄 Tạo lại Agenda");
      return;
    }
    const docs = this.lib.docs();
    const ids = docs.filter((d) => d.kind === "agenda").map((d) => d.id);
    // the first part of every document (cover page: title, teacher, date, session) helps place it in the agenda
    const heads = [];
    for (const d of docs) {
      if (["journal_template", "certificate"].includes(d.kind)) continue;
      heads.push(`[${d.name}] buổi ${d.day || "?"} · ${clean(d.title)}\n${(await this.lib.text(d)).slice(0, 1200).trim()}`);
    }
    const src = ids.length ? "tài liệu Agenda đính kèm, phần đầu của từng tài liệu" : "phần đầu của từng tài liệu";
    this.setPrompt(
      `Hãy lập Agenda chi tiết của khóa học dựa trên ${src} và nội dung thầy giảng (nếu có), phân cấp ` +
      "Ngày → Buổi (sáng/chiều/tối) → Phiên. Mỗi phiên ghi: giờ (nếu có), chủ đề, tên giảng viên, và các tài " +
      "liệu dùng trong phiên đó — ghi đúng tên file trong thư viện (kể cả đuôi .pdf/.docx) để bấm mở được. " +
      "Trình bày đúng mẫu sau, không thêm giải thích; thông tin nào không có thì ghi \"—\":\n" +
      "📅 Ngày 1 · Thứ Hai 10/11/2026\n" +
      "  ▸ Buổi sáng\n" +
      "    • Phiên 1 · 08:30–10:00 · Chủ đề — GV: Tên giảng viên\n" +
      "        📄 Ten_file.pdf",
      { kind: "agenda", doc_ids: ids, label: `Agenda chi tiết · ${docs.length} tài liệu`, save_as: ["agenda", this.agendaKey()],
        blocks: heads.length ? [{ type: "text", text: `<document_heads>\n${heads.join("\n\n")}\n</document_heads>` }] : [] });
  }

  /** Library file names in Claude's latest answer become links that open the file. */
  linkify() {
    document.getElementById("chat").normalize();          // streamed pieces -> whole text, so names are not split
    const marker = [...document.querySelectorAll("#chat .ansStart")].pop();
    if (!marker) return;
    const names = this.lib.docs().map((d) => [d.name.toLowerCase(), d]).sort((a, b) => b[0].length - a[0].length);
    if (!names.length) return;
    const nodes = [];
    for (let n = marker.nextSibling; n; n = n.nextSibling) if (n.nodeType === Node.TEXT_NODE) nodes.push(n);
    for (let node of nodes) {
      for (;;) {
        const low = node.data.toLowerCase();
        let best = null;
        for (const [name, d] of names) {
          const i = low.indexOf(name);
          if (i >= 0 && (!best || i < best[0])) best = [i, name.length, d];
        }
        if (!best) break;
        const [i, len, d] = best;
        const hit = node.splitText(i);
        const rest = hit.splitText(len);
        const a = el("span", "flink", hit.data);
        a.onclick = async () => { try { openBlob(await this.lib.file(d), d.name); } catch (e) { alert(e.message); } };
        hit.replaceWith(a);
        node = rest;
      }
    }
  }

  // ---- Summary: mode, course day, and the exact book files ----
  summary() {
    const books = this.lib.docs().filter((d) => !["journal_template", "certificate"].includes(d.kind));
    const days = this.lib.days();
    const body = el("div", "lform");
    body.append(el("b", "", "Cách tổng kết:"));
    let mode = "book";
    for (const [val, txt] of [["book", "Theo file sách đã upload (chọn file bên dưới)"],
                              ["mix", "Kết hợp sách + nội dung thầy giảng trên màn hình"]]) {
      const lab = el("label", "radio");
      const r = el("input");
      r.type = "radio";
      r.name = "smode";
      r.checked = val === mode;
      r.onchange = () => { mode = val; };
      lab.append(r, " " + txt);
      body.append(lab);
    }
    body.append(el("b", "", "Buổi học:"));
    const sel = el("select", "wide");
    sel.append(new Option("Tự chọn tài liệu (không theo buổi)", "0"));
    days.forEach(([day, date], i) => sel.append(new Option(`Buổi ${day}` + (date ? ` · ${fmtDate(date)}` : ""), String(i + 1))));
    sel.value = String(days.length);
    body.append(sel, el("b", "", "File sách dùng để tổng kết (chạm để chọn nhiều file):"));
    const list = el("div", "checklist");
    const count = el("p", "muted", "");
    const boxes = new Map();
    for (const d of books) {
      const lab = el("label", "checkrow");
      const cb = el("input");
      cb.type = "checkbox";
      cb.onchange = () => { count.textContent = `Đã chọn ${[...boxes.values()].filter((b) => b.checked).length} file`; };
      lab.append(cb, el("span", "cday", d.day ? `Buổi ${d.day}` : "-"), el("span", "ctitle", clean(d.title || d.name)),
                 el("span", "cfile", d.name));
      boxes.set(d.id, cb);
      list.append(lab);
    }
    const dayChanged = () => {
      const i = +sel.value;
      const ids = new Set(i ? days[i - 1][2].map((d) => d.id) : []);
      for (const [id, cb] of boxes) cb.checked = ids.has(id);
      count.textContent = `Đã chọn ${ids.size} file`;
      list.querySelector("input:checked")?.scrollIntoView({ block: "nearest" });
    };
    sel.onchange = dayChanged;
    const freshLab = el("label", "radio");
    const fresh = el("input");
    fresh.type = "checkbox";
    freshLab.append(fresh, " Tạo mới (không dùng bản tổng kết đã lưu)");
    body.append(list, count, freshLab);
    const row = el("div", "actions end");
    const cancel = el("button", "", "Hủy");
    cancel.onclick = () => this.app.closeSheet();
    const go = el("button", "primary", "Tạo prompt");
    go.onclick = () => {
      const ids = [...boxes].filter(([, cb]) => cb.checked).map(([id]) => id);
      if (!ids.length) { alert("Hãy chọn ít nhất 1 file sách."); return; }
      this.app.closeSheet();
      const di = +sel.value;
      // a book-only summary depends only on the chosen files: reuse it (with the teacher it changes live)
      const name = "summary:" + [...ids].sort().join(",") + `:${di}`;
      if (mode === "book" && !fresh.checked) {
        const saved = this.savedAnswer(name, "v1");
        if (saved) { this.showSaved("Summary (theo sách)", saved, "muốn tạo mới: Summary → tích \"Tạo mới\""); return; }
      }
      const chosen = ids.map((i) => this.lib.get(i));
      const names = chosen.map((d) => `"${clean(d.title || d.name)}" (${d.name})`).join("; ");
      const when = di ? `Buổi ${days[di - 1][0]}` + (days[di - 1][1] ? ` (ngày ${fmtDate(days[di - 1][1])})` : "") : "";
      const text = mode === "book"
        ? `Hãy tổng kết ${when ? "nội dung " + when : "nội dung"} chỉ dựa trên các file sách sau: ${names}. ` +
          "Gồm: các ý chính, khái niệm quan trọng, ví dụ, bài tập và những điểm cần ghi nhớ."
        : `Hãy tổng kết ${when || "buổi học"} kết hợp các file sách (${names}) với nội dung thầy giảng ` +
          "(bản ghi lời giảng và bản dịch đang có trên màn hình); ghi rõ ý nào từ sách, ý nào thầy nhấn " +
          "mạnh hoặc bổ sung. Gồm: các ý chính, khái niệm quan trọng, ví dụ, bài tập đã giao và những " +
          "điểm cần ghi nhớ.";
      this.setPrompt(text, { kind: "summary", doc_ids: ids,
        label: `Summary · ${when ? when + " · " : ""}${mode === "book" ? "Sách" : "Sách + thầy giảng"} · ${ids.length} file`,
        ...(mode === "book" ? { save_as: [name, "v1"] } : {}) });
    };
    row.append(cancel, go);
    body.append(row);
    body.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.tagName !== "TEXTAREA") { e.preventDefault(); go.click(); } });
    this.app.sheet("Summary — tổng kết", body, true);
    dayChanged();
  }

  // ---- Exercises: big exercises (whole folder, with steps) or per file ----
  exercises() {
    if (this.session && !confirm(`Đang làm bài "${this.session.ex.title}" (bước ${this.session.i + 1}/` +
        `${this.session.ex.steps.length}).\nDừng bài này để chọn bài khác?`)) return;
    if (this.session) this.saveSession();
    this.session = null;
    const body = el("div", "lform");
    let mode = "big";
    const modes = el("div", "actions");
    for (const [val, txt] of [["big", "Bài tập lớn (đọc toàn bộ thư mục, theo các bước)"],
                              ["doc", "Theo file bài tập (chọn file cụ thể)"]]) {
      const lab = el("label", "radio");
      const r = el("input");
      r.type = "radio";
      r.name = "xmode";
      r.checked = val === mode;
      r.onchange = () => { mode = val; fill(); };
      lab.append(r, " " + txt);
      modes.append(lab);
    }
    const status = el("p", "muted", "");
    const list = el("div", "exlist");
    const detail = el("div", "exdetail", "");
    const check = (text) => {
      const lab = el("label", "radio");
      const cb = el("input");
      cb.type = "checkbox";
      lab.append(cb, " " + text);
      return [lab, cb];
    };
    const [fullLab, full] = check("Đọc lại toàn bộ file");
    const [restartLab, restart] = check("Làm lại từ đầu");
    const refreshBtn = el("button", "", "🔄 Refresh danh sách");
    const startBtn = el("button", "primary", "▶ Bắt đầu bài tập");
    const close = el("button", "", "Đóng");
    close.onclick = () => this.app.closeSheet();
    const row = el("div", "actions");
    row.append(refreshBtn, fullLab, restartLab, el("span", "spacer"), close, startBtn);
    body.append(modes, status, list, detail, row);
    const alive = () => body.isConnected;
    let chosen = null;

    const fileExercises = (d) => {           // exercises found in one file: with steps (folder scan) first
      const out = (d.bigex || []).map((e) => ({ ...e }));
      const seen = new Set(out.map((e) => e.title.trim().toLowerCase()));
      for (const e of d.exercises || []) {
        if (!seen.has(e.title.trim().toLowerCase())) out.push({ title: e.title, goal: e.summary || "", location: e.location || "", steps: [] });
      }
      return out;
    };
    const showStatus = () => {
      if (mode === "big") {
        const upd = this.lib.data.big_exercises_updated;
        const n = (this.lib.data.big_exercises || []).length;
        status.textContent = upd ? `${n} bài tập lớn · cập nhật ${upd}` : "Chưa có danh sách — nhấn 🔄 Refresh để Claude đọc toàn bộ thư mục.";
      } else {
        status.textContent = "Chọn một file bài tập: bấm ▶ để làm lần lượt toàn bộ bài trong file, hoặc mở file và chọn một bài cụ thể.";
      }
    };
    // start working out the steps in the background as soon as an item is selected, so that ▶ Bắt đầu shows
    // the first prompt at once (the result is saved for next time too)
    const prepare = (it) => {
      if (it.kind === "file" && it.exs.length && !knownFilePlan(it.doc)) {
        const d = it.doc;
        this.prefetch(`file:${d.id}`, () => planFile(this.lib, this.app.apiKey, d, it.exs.map((e) => e.title)));
      } else if (it.kind === "ex" && !it.ex.steps?.length) {
        const d = it.doc, e = it.ex;
        if (!(d.plans || {})[e.title]) {
          this.prefetch(`ex:${d.id}:${e.title}`, () => planSteps(this.lib, this.app.apiKey,
            { title: e.title, summary: e.goal || "", location: e.location || "", doc: d }));
        }
      }
    };
    const progressNote = (ex) => {
      const saved = (this.lib.data.sessions || {})[exerciseKey(ex)];
      if (saved && !saved.done && (saved.i ?? -1) >= 0) return `\n\n⏸ Đang làm dở: đã xong ${saved.i}/${ex.steps.length} bước — Bắt đầu sẽ làm tiếp.`;
      if (saved && saved.done) return "\n\n✓ Đã làm xong bài này trước đây (Bắt đầu sẽ làm lại, tóm tắt bài lấy từ bản đã lưu).";
      return "";
    };
    const select = (rowEl, it) => {
      list.querySelectorAll(".sel").forEach((x) => x.classList.remove("sel"));
      rowEl.classList.add("sel");
      chosen = it;
      detail.textContent = "";
      if (it.kind !== "big") prepare(it);
      if (it.kind === "file") {
        rowEl.nextSibling?.classList?.contains("exchildren") && rowEl.nextSibling.classList.add("open");
        const kp = knownFilePlan(it.doc);
        detail.textContent = (kp ? progressNote(fileExercise(it.doc, kp[0], kp[1])).trim() + "\n" : "") +
          `${it.doc.description || ""}\n\n` + (it.exs.length
          ? "Bài tập trong file:\n" + it.exs.map((e) => `  • ${e.title}`).join("\n")
          : "Chưa thấy bài tập riêng trong file (nhấn 🔄 Refresh để Claude đọc kỹ file).") +
          "\n\n▶ Bắt đầu = làm lần lượt toàn bộ bài tập/hướng dẫn trong file này.";
      } else {
        const ex = it.ex;
        let text = (ex.goal || "") + (ex.location ? `  (${ex.location})` : "");
        if (ex.steps?.length) {
          text += "\n\n" + ex.steps.map((s, k) => `${k + 1}. ${s.title} — ${s.instruction}`).join("\n");
          text += progressNote(it.kind === "big" ? ex : singleExercise(it.doc, ex.title, ex.goal || "", ex.steps));
        } else {
          text += "\n\n(Đang chuẩn bị các bước ở chế độ nền...)";
        }
        detail.textContent = text;
      }
    };
    const rowOf = (cells, it, cls = "exrow") => {
      const r = el("div", cls);
      cells.forEach((c, i) => r.append(el("span", ["xday", "xtitle", "xsteps", "xdocs"][i], c)));
      r.onclick = () => select(r, it);
      r.ondblclick = () => { select(r, it); start(); };
      return r;
    };
    const fill = () => {
      list.replaceChildren();
      chosen = null;
      detail.textContent = "";
      list.append(rowOf(["Buổi", "Bài tập", "Số bước", "Tài liệu / file"], null, "exrow head"));
      list.lastChild.onclick = null;
      list.lastChild.ondblclick = null;
      if (mode === "big") {
        for (const ex of this.lib.data.big_exercises || []) {
          const docs = (ex.doc_ids || []).map((i) => this.lib.get(i)).filter(Boolean);
          list.append(rowOf([String(ex.day || "-"), ex.title, String(ex.steps.length),
                             docs.map((d) => clean(d.title || d.name)).join(", ")], { kind: "big", ex }));
        }
      } else {
        for (const d of this.lib.docs()) {
          if (["journal_template", "certificate", "agenda"].includes(d.kind)) continue;
          const exs = fileExercises(d);
          list.append(rowOf([String(d.day || "-"), "📄 " + clean(d.title || d.name), exs.length ? `${exs.length} bài` : "-", d.name],
                            { kind: "file", doc: d, exs }, "exrow file"));
          const kids = el("div", "exchildren");
          for (const ex of exs) {
            kids.append(rowOf([String(d.day || "-"), "✏ " + ex.title, String(ex.steps.length || "-"), d.name],
                              { kind: "ex", doc: d, ex }, "exrow child"));
          }
          list.append(kids);
        }
      }
      showStatus();
      refreshBtn.disabled = this.scanning;
    };
    const refresh = async () => {
      this.scanning = true;
      refreshBtn.disabled = true;
      try {
        const [n, problems] = await scanExercises(this.lib, this.app.apiKey,
          (msg) => { if (alive()) status.textContent = "⏳ " + msg; }, full.checked);
        this.chatNote(`Đã cập nhật danh sách: ${n} bài tập lớn.` +
          (problems.length ? "\nLưu ý:\n" + problems.slice(0, 8).map((p) => "• " + p).join("\n") : ""));
      } catch (e) {
        this.chatNote(`Chưa cập nhật được danh sách bài tập: ${friendlyError(e)}`, true);
      } finally {
        this.scanning = false;
        if (alive()) fill();
      }
    };
    const start = () => {
      const it = chosen;
      if (!it) return;
      const redo = restart.checked;
      this.app.closeSheet();                 // close right away: progress shows in the Claude pane
      if (it.kind === "big") { this.startSession(it.ex, redo); return; }
      const d = it.doc;
      if (it.kind === "ex" && it.ex.steps?.length) {
        this.startSession(singleExercise(d, it.ex.title, it.ex.goal || "", it.ex.steps), redo);
        return;
      }
      let plan;
      if (it.kind === "file") {              // the whole file: one big problem, sub-exercise by sub-exercise
        const prep = `file:${d.id}`;
        const known = knownFilePlan(d);
        if (known) {                         // steps already known (saved): the first prompt appears at once
          this.startSession(fileExercise(d, known[0], known[1]), redo);
          return;
        }
        this.chatNote(`Đang chuẩn bị các bài tập con và các bước của file "${d.name}"... ` +
                      "Câu prompt đầu tiên sẽ hiện trong ô nhập khi xong.");
        plan = async () => {
          await this.waitPrefetch(prep);     // it may already be on its way since the file was selected
          const kp = knownFilePlan(d);
          const [goal, steps] = kp || await planFile(this.lib, this.app.apiKey, d, it.exs.map((e) => e.title));
          if (!steps.length) throw new Error("Claude không tìm thấy bài tập trong file");
          return fileExercise(d, goal, steps);
        };
      } else {
        const prep = `ex:${d.id}:${it.ex.title}`;
        const ex = { title: it.ex.title, summary: it.ex.goal || "", location: it.ex.location || "", doc: d };
        if (!(d.plans || {})[ex.title]) {
          this.chatNote(`Đang chia bài "${clean(ex.title)}" thành các bước... Câu prompt đầu tiên sẽ hiện trong ô nhập khi xong.`);
        }
        plan = async () => {
          await this.waitPrefetch(prep);
          const steps = await planSteps(this.lib, this.app.apiKey, ex);     // uses the saved steps if any
          if (!steps.length) throw new Error("Claude không trả về các bước");
          return singleExercise(d, ex.title, ex.summary, steps);
        };
      }
      this.app.setStatus("🎓 Đang chuẩn bị bài tập...");
      plan().then((sess) => {
        this.app.setStatus("🎓 Bài tập đã sẵn sàng", "ok");
        this.startSession(sess, redo);
      }).catch((e) => {
        const why = e instanceof ClaudeError || /timed out/i.test(String(e?.message)) ? friendlyError(e) : String(e?.message || e);
        this.chatNote(`Chưa chuẩn bị được bài tập: ${why}`, true);
      });
    };
    refreshBtn.onclick = refresh;
    startBtn.onclick = start;
    this.app.sheet("Exercises — bài tập", body, true);
    fill();
    if (!this.lib.data.big_exercises_updated && !this.scanning) refresh();    // first time: read the folder right away
  }

  // ---- background preparation of exercise steps (started when an item is selected in the Exercises list) ----
  prefetch(key, fn) {
    this.prep = this.prep || new Map();
    if (this.prep.has(key)) return;
    const p = Promise.resolve().then(fn).catch(() => {})       // ▶ Bắt đầu tries again and shows the error
      .finally(() => this.prep.delete(key));
    this.prep.set(key, p);
  }

  async waitPrefetch(key) {
    const p = this.prep?.get(key);
    if (p) await Promise.race([p, new Promise((r) => setTimeout(r, 330_000))]);
  }

  // ---- guided exercise: short overview, then one prompt per step (asking the learner for inputs when needed,
  // giving ready-to-use prompts), then the solution summary + PDF; progress is saved and can be resumed ----
  async saveSession(done = false) {
    const s = this.session;
    if (!s) return;
    const d = new Date();
    (this.lib.data.sessions = this.lib.data.sessions || {})[s.key] = {
      ex: s.ex, i: s.i, log: s.log, overview: s.overview || "", awaiting: s.awaiting || null, carry: s.carry || null,
      next_inputs: s.next_inputs || null, done,
      time: `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}` };
    await this.lib.save();
  }

  startSession(ex, restart = false) {
    ex = { ...ex, title: clean(ex.title), goal: clean(ex.goal),
           steps: ex.steps.map((x) => ({ ...x, title: clean(x.title), ...(x.part_title ? { part_title: clean(x.part_title) } : {}) })) };
    const key = exerciseKey(ex);
    const saved = (this.lib.data.sessions || {})[key] || {};
    const steps = ex.steps;
    if (saved.ex && !restart && !saved.done && (saved.i ?? -1) >= 0) {
      // an exercise opened before: carry on where it stopped
      this.session = { ex, i: Math.min(saved.i, steps.length), log: saved.log || [], overview: saved.overview || "",
                       key, resume: true };
      for (const k of ["awaiting", "carry", "next_inputs"]) if (saved[k]) this.session[k] = saved[k];
      const k = this.session.i;
      this.chatNote(`Tiếp tục bài "${ex.title}" (đã lưu lúc ${saved.time || ""}): đã xong ${k}/${steps.length} bước. ` +
                    "Muốn làm lại từ đầu: Exercises → tích \"Làm lại từ đầu\"." +
                    (this.session.overview ? `\nTóm tắt bài:\n${this.session.overview}` : ""));
      if (k >= steps.length) this.summaryPrompt(false);
      else if (this.session.awaiting) this.promptInputs(this.session.awaiting);
      else this.promptStep();
      return;
    }
    this.session = { ex, i: -1, log: [], overview: "", key };          // i = -1: the overview first
    if (steps.length && steps[0].part) {
      const lines = steps.filter((st) => st.pstep === 1).map((st) => `  Bài ${st.part}/${st.parts}: ${st.part_title} (${st.psteps} bước)`);
      this.chatNote(`Bắt đầu bài toán "${ex.title}" — ${steps[0].parts} bài tập con, ${steps.length} bước:\n` + lines.join("\n"));
    } else {
      this.chatNote(`Bắt đầu bài tập "${ex.title}" — ${steps.length} bước:\n` + steps.map((s, k) => `  ${k + 1}. ${s.title}`).join("\n"));
    }
    if (saved.overview) {                                      // the overview was written before: no need to ask again
      Object.assign(this.session, { overview: saved.overview, i: 0 });
      this.chatNote(`Tóm tắt bài (đã lưu):\n${saved.overview}`);
      this.saveSession();
      this.promptStep();
      return;
    }
    this.saveSession();
    this.promptStep();
    setTimeout(() => this.app.sendPrompt(), 300);              // the overview is asked for right away
  }

  /** After a resume Claude has not seen the earlier steps: give it a short recap (with the documents). */
  resumeBlocks() {
    const s = this.session;
    if (!s.resume) return [];
    const recap = [`Tóm tắt bài: ${s.overview || ""}`].concat(s.log.map((e) =>
      `[${this.stepName(e.i)}: ${e.step.title}]` + (e.input ? `\nThông tin của học viên: ${e.q}` : "") +
      `\nKết quả: ${e.a.slice(0, 1500)}`));
    return [{ type: "text", text: "<previous_steps>\n" + recap.join("\n\n") + "\n</previous_steps>" }];
  }

  stopSession() {
    if (this.session) {
      this.saveSession();
      this.chatNote(`Đã dừng bài tập "${this.session.ex.title}" — tiến độ đã được lưu, mở lại bài này trong Exercises để làm tiếp.`);
    }
    this.session = null;
    if (this.job && ["exstep", "exsummary"].includes(this.job.kind)) {
      this.job = null;
      document.getElementById("prompt").value = "";
      this.app.refreshAttachBar();
    }
  }

  stepName(i) {
    const ex = this.session.ex, st = ex.steps[i];
    return st.part ? `Bài ${st.part}/${st.parts} · Bước ${st.pstep}/${st.psteps}` : `Bước ${i + 1}/${ex.steps.length}`;
  }

  /** Tells Claude what the next step is, so it can say what to carry over and what to ask the learner. */
  nextStepBlock(i) {
    const steps = this.session.ex.steps;
    if (i + 1 >= steps.length) return { type: "text", text: "<next_step>(không còn — đây là bước cuối)</next_step>" };
    const st = steps[i + 1];
    return { type: "text", text: `<next_step>${this.stepName(i + 1)}: ${st.title} — ${st.instruction}</next_step>` };
  }

  promptStep() {
    const s = this.session, ex = s.ex, i = s.i;
    if (i < 0) {                                               // a 2-3 line overview first
      this.setPrompt(`Bài tập "${ex.title}": tóm tắt rất ngắn (2-3 dòng) đề bài, mục tiêu và kết quả cần có.`,
        { kind: "exstep", step: -1, doc_ids: ex.doc_ids || [], label: `Bài tập · Tóm tắt · ${ex.title.slice(0, 30)}`,
          blocks: [{ type: "text", text: OVERVIEW_RULES }, this.nextStepBlock(-1)] });
      return;
    }
    const step = ex.steps[i];
    const newPart = !!step.part && step.pstep === 1;
    const head = step.part ? `${step.part_title} — ${this.stepName(i)}` : this.stepName(i);
    const lines = [`${head}: ${step.title}.`, step.instruction];
    if (newPart && step.part_goal) lines.push(`Mục tiêu bài này: ${step.part_goal}`);
    if (s.carry) lines.push(`Mang sang từ bước trước: ${s.carry}`);
    if (s.next_inputs) {                                       // Claude said this step needs the learner's information
      lines.push("Thông tin của tôi:");
      lines.push(...s.next_inputs.map((f) => `• ${f}: `));
    }
    let label = step.part ? `${this.stepName(i)} · ${step.part_title.slice(0, 25)}` : `Bài tập · ${this.stepName(i)} · ${ex.title.slice(0, 30)}`;
    if (s.next_inputs) {
      label += " · điền thông tin";
      this.app.setStatus("🎓 Điền thông tin vào mẫu trong ô nhập rồi gửi.", "warn");
    }
    // the documents go again with each new sub-exercise and now and then, so they stay in Claude's recent
    // history (the overview already sent them)
    const ids = (i > 0 && (i % 6 === 0 || newPart)) || s.resume ? ex.doc_ids || [] : [];
    this.setPrompt(lines.join("\n"), { kind: "exstep", step: i, doc_ids: ids, label, input: !!s.next_inputs,
      blocks: [{ type: "text", text: EXERCISE_RULES }, this.nextStepBlock(i), ...this.resumeBlocks()] });
  }

  /** Claude asked for the learner's own information: a fill-in form in the prompt box. */
  promptInputs(fieldsList) {
    const s = this.session, i = s.i;
    const text = `${this.stepName(i)} — thông tin của tôi:\n` + fieldsList.map((f) => `• ${f.trim()}: `).join("\n");
    this.setPrompt(text, { kind: "exstep", step: i, input: true, doc_ids: s.resume ? s.ex.doc_ids || [] : [],
      blocks: [{ type: "text", text: EXERCISE_RULES }, this.nextStepBlock(i), ...this.resumeBlocks()],
      label: `${this.stepName(i)} · điền thông tin` });
    this.app.setStatus("🎓 Claude cần thông tin của bạn — điền vào mẫu trong ô nhập rồi gửi.", "warn");
  }

  summaryPrompt(send = true) {
    const ex = this.session.ex;
    this.setPrompt(
      `Đã xong ${ex.steps.length} bước của bài tập "${ex.title}". Tổng hợp ngắn gọn (tối đa 8 dòng): kết ` +
      "quả cuối cùng, kết quả/prompt chính của từng " + (ex.whole_file ? "bài tập con" : "bước") +
      " (mỗi mục 1 dòng) và 2-3 điều cần nhớ.",
      { kind: "exsummary", label: `Tổng kết lời giải · ${ex.title.slice(0, 30)}`, blocks: this.resumeBlocks() });
    if (send) setTimeout(() => this.app.sendPrompt(), 300);      // sent right away
  }

  /** Hide the step markers from the latest answer and add [📋 Copy prompt] after each prompt block. */
  tidyAnswer(turn) {
    const marker = [...document.querySelectorAll("#chat .ansStart")].pop();
    if (!marker) return;
    document.getElementById("chat").normalize();
    const nodes = [];
    for (let n = marker.nextSibling; n; n = n.nextSibling) if (n.nodeType === Node.TEXT_NODE) nodes.push(n);
    for (const n of nodes) {
      let t = n.data.replace(/^[ \t]*\[\[\s*(?:CẦN NHẬP|CAN NHAP|XONG BƯỚC|XONG BUOC)[^\]]*\]\][ \t]*\n?/gim, "");
      t = t.replace(MARKER, "");
      // a Copy link after the end line of each ready-to-use prompt
      const pieces = [];
      let pos = 0;
      for (const m of t.matchAll(PROMPT_BLOCK)) {
        const end = m.index + m[0].length;
        pieces.push(t.slice(pos, end));
        pieces.push({ copy: m[1].trim() });
        pos = end;
      }
      pieces.push(t.slice(pos));
      const frag = document.createDocumentFragment();
      for (const p of pieces) {
        if (typeof p === "string") { if (p) frag.append(p); continue; }
        const a = el("span", "flink", "[📋 Copy prompt]");
        a.onclick = () => this.copyText(p.copy);
        frag.append("  ", a);
      }
      n.replaceWith(frag);
    }
    if (turn) turn.a = stripMarkers(turn.a);
  }

  async copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      this.app.setStatus("✓ Đã copy prompt — dán vào nơi cần dùng", "ok");
    } catch {
      prompt("Copy prompt này:", text);                        // no clipboard access: let the user copy it
    }
  }

  /** Called by the app when Claude finished answering (ok = no error). */
  async onChatEnd(turn, ok) {
    const job = this.sentJob;
    this.sentJob = null;
    const raw = turn?.a || "";
    try { this.linkify(); this.tidyAnswer(turn); } catch { /* answer pane changed */ }
    // save the answer of a Learning prompt that can be reused (only if it was sent as the app wrote it)
    const norm = (t) => String(t || "").split(/\s+/).filter(Boolean).join(" ");
    if (job?.save_as && ok && (turn?.a || "").trim() && norm(turn.q) === norm(job.prompt)) {
      await this.saveAnswer(job.save_as[0], job.save_as[1], turn.a.trim());
    }
    const s = this.session;
    if (!s) return;
    const steps = s.ex.steps;
    const answer = (turn?.a || "").trim();
    const kind = job?.kind;
    if (kind === "exstep" && job.step === s.i && ok && answer) {
      delete s.resume;                                         // Claude has the context again from here on
      const nxt = fields(NEXT_INPUT.exec(raw));
      if (s.i < 0) {                                           // the overview
        s.overview = answer;
        s.next_inputs = nxt;
        s.i = 0;
        this.saveSession();
        this.promptStep();
        return;
      }
      s.log.push({ i: s.i, step: steps[s.i], q: turn.q || "", a: answer, input: !!job.input });
      const ask = NEEDS_INPUT.exec(raw);
      if (ask && !DONE.test(raw)) {                            // Claude needs the learner's information first
        s.awaiting = fields(ask) || ["thông tin"];
        if (!s.awaiting.length) s.awaiting = ["thông tin"];
        this.saveSession();
        this.promptInputs(s.awaiting);
        return;
      }
      delete s.awaiting;
      const carry = CARRY.exec(raw.replace(MARKER, ""));
      s.carry = carry ? carry[1].trim() : null;
      s.next_inputs = nxt;
      s.i += 1;
      this.saveSession();
      if (s.i < steps.length) this.promptStep();               // next step's prompt, ready to send
      else this.summaryPrompt();
    } else if (kind === "exsummary" && ok && answer) {
      try {
        const out = await solutionPdf(this.lib, s, answer);
        await this.addOutput("solution", out.name, out.blob, `Lời giải: ${s.ex.title}`,
          { day: s.ex.day, detail: `${s.ex.steps.length} bước${s.ex.whole_file ? " · cả file" : ""}` });
        this.chatNote(`Hoàn thành bài tập "${s.ex.title}". File PDF lời giải (cũng có trong 📋 List Outputs):\n`);
        this.fileLinks(out.name, out.blob);
      } catch (e) {
        this.chatNote(`Chưa tạo được file PDF lời giải: ${e.message || e}`, true);
      }
      await this.saveSession(true);
      this.session = null;
    } else if (s.i < 0) {
      this.promptStep();
    } else if (s.i < steps.length) {
      if (s.awaiting) this.promptInputs(s.awaiting);           // still waiting for the learner's information
      else this.promptStep();                                  // a side question or an error: same step again
    } else {
      this.summaryPrompt(false);
    }
  }

  // ---- Documents ----
  documents() {
    const body = el("div", "lform");
    const q = el("input", "search");
    q.placeholder = "Tìm tài liệu...";
    const table = el("div", "ltable docs");
    const desc = el("p", "muted", "Chạm vào tên file để mở file gốc.");
    let chosen = null;
    const open = async (d) => {
      try {
        openBlob(await this.lib.file(d), d.name);
        this.app.closeSheet();                                 // file is open: close the list
      } catch (e) {
        alert("Không mở được file: " + e.message);
      }
    };
    const fill = () => {
      table.replaceChildren(el("div", "lth", "Ngày"), el("div", "lth", "Buổi"), el("div", "lth", "Tên tài liệu"), el("div", "lth", "Tên file"));
      const words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
      for (const d of this.lib.docs()) {
        const hay = `${d.title} ${d.name} ${d.description}`.toLowerCase();
        if (words.length && !words.every((w) => hay.includes(w))) continue;
        const file = el("div", "ltd link", "📄 " + d.name);
        const cells = [el("div", "ltd", fmtDate(d.date || d.added)), el("div", "ltd", String(d.day || "-")),
                       el("div", "ltd", d.title || d.name), file];
        for (const c of cells) {
          c.onclick = () => {
            table.querySelectorAll(".sel").forEach((x) => x.classList.remove("sel"));
            cells.forEach((x) => x.classList.add("sel"));
            chosen = d;
            desc.textContent = `${KIND_VI[d.kind] || ""} · ${d.description || ""}` +
              (d.exercises?.length ? `  ·  Bài tập: ${d.exercises.length}` : "") + `  ·  Gốc: ${d.rel}`;
          };
        }
        file.onclick = (e) => { cells[0].onclick(e); open(d); };
        table.append(...cells);
      }
    };
    q.oninput = fill;
    fill();
    const row = el("div", "actions");
    const sync = el("button", "", "🔄 Refresh");
    sync.onclick = () => { this.app.closeSheet(); this.refresh(() => this.documents()); };
    const spacer = el("span", "spacer");
    const openBtn = el("button", "", "📄 Mở file");
    openBtn.onclick = () => chosen && open(chosen);
    const askAbout = (fresh) => {
      if (!chosen) return;
      const d = chosen, title = clean(d.title || d.name);
      const name = `doc:${d.id}`;                  // the id is the file's content hash: same file, same answer
      const saved = fresh ? null : this.savedAnswer(name, "v1");
      this.app.closeSheet();
      if (saved) { this.showSaved(`Tài liệu "${title.slice(0, 50)}"`, saved, "muốn hỏi lại: Documents → 🔄 Hỏi lại"); return; }
      this.setPrompt(`Hãy giới thiệu tài liệu "${title}": nội dung chính, các ý quan trọng và nó liên quan thế nào đến khóa học.`,
        { kind: "doc", label: `Tài liệu · ${title.slice(0, 40)}`, doc_ids: [d.id], save_as: [name, "v1"] });
    };
    const ask = el("button", "primary", "💬 Hỏi Claude về tài liệu này");
    ask.onclick = () => askAbout(false);
    const again = el("button", "", "🔄 Hỏi lại");
    again.onclick = () => askAbout(true);
    row.append(sync, spacer, openBtn, again, ask);
    body.append(q, table, desc, row);
    this.app.sheet("Documents — tài liệu đã upload", body, true);
  }

  // ---- Journal ----
  journal() {
    const temps = this.lib.templates();
    if (!temps.length) {
      alert('Chưa tìm thấy file PDF mẫu nhật ký trong thư mục đã upload.\nHãy đặt file mẫu (ví dụ "Journal.pdf" / "Nhat ky.pdf") vào thư mục học rồi chọn Refresh để đồng bộ lại.');
      return;
    }
    // today's course day if the library knows it, else the latest day (the user can edit the prompt)
    const days = this.lib.days();
    const today = isoDate();
    const [day, date, docs] = days.find((x) => x[1] === today) || days[days.length - 1] || [0, "", []];
    const template = temps[0];
    const d = new Date();
    const when = day ? `Buổi ${day}` + (date ? ` (ngày ${fmtDate(date)})` : "")
      : `buổi học hôm nay (${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()})`;
    this.setPrompt(
      `Hãy điền nhật ký buổi học cho ${when} theo đúng mẫu "${template.name}", dựa trên tài liệu của ` +
      "buổi này và nội dung thầy giảng trên màn hình. Viết ngắn gọn, cụ thể, đúng từng mục trong mẫu, " +
      "rồi trả lại file PDF đã điền để tải về.",
      { kind: "journal", day, label: `Journal · ${when} · mẫu ${template.name.slice(0, 30)}`,
        doc_ids: docs.filter((x) => x.id !== template.id).map((x) => x.id), template: template.id });
  }

  /** Called when the Journal prompt is sent: fill the PDF in the background. */
  async runJournal(instruction, job) {
    const emit = (k, v) => this.app.handle(k, v);
    const template = this.lib.get(job.template);
    const dayDocs = (job.doc_ids || []).map((i) => this.lib.get(i)).filter(Boolean);
    const [en, vi, summary] = this.app.chatContext();
    const session = en || vi || summary
      ? `<key_takeaways>\n${summary}\n</key_takeaways>\n<transcript_english>\n${en}\n</transcript_english>\n` +
        `<translation_vietnamese>\n${vi}\n</translation_vietnamese>` : "";
    emit("chat_piece", "đang đọc mẫu nhật ký và điền thông tin...\n");
    try {
      const out = await makeJournal(this.app.apiKey, this.lib, template, dayDocs, session, instruction);
      if (out.preview) emit("chat_piece", out.preview.trim() + "\n");
      const when = (job.label || "").replace(/^Journal · /, "").replace(/ · mẫu .*$/, "");
      await this.addOutput("journal", out.name, out.blob, `Nhật ký: ${when || "buổi học"}`,
        { day: job.day || 0, detail: `Mẫu ${template.name}` });
      emit("chat_piece", "(file cũng có trong 📋 List Outputs)\n");
      this.fileLinks(out.name, out.blob);
    } catch (e) {
      const why = e instanceof ClaudeError ? friendlyError(e) : String(e?.message || e).slice(0, 200);
      emit("chat_error", `[Chưa điền được nhật ký: ${why}]`);
    }
    emit("chat_end", null);
  }

  /** Tappable 'open' / 'save' links for a file in the answers pane. */
  fileLinks(name, blob) {
    const w = document.getElementById("chat");
    w.append("📄 " + name + "   ");
    const link = (label, fn) => {
      const a = el("span", "flink", label);
      a.onclick = fn;
      w.append(a, "  ");
    };
    link("[Mở file]", () => openBlob(blob, name));
    link("[Tải về / Lưu…]", () => shareBlob(blob, name).then((ok) => ok && this.app.setStatus(`✓ Đã lưu: ${name}`, "ok")));
    this.app.scrollEnd(w);
  }

  // ---- Certificate ----
  async certificate() {
    await this.ready;
    const certs = this.lib.data.certificates;
    const body = el("div", "lcert");
    const list = el("div", "certList");
    const right = el("div", "certRight");
    const preview = el("div", "certPreview", "Chưa có chứng chỉ.\nNhấn ＋ Upload để thêm ảnh.");
    const caption = el("textarea");
    caption.rows = 3;
    let cur = null;
    const blobOf = (c) => idbGet("files", "cert/" + c.stored);
    const show = async (c) => {
      cur = c;
      list.querySelectorAll(".sel").forEach((x) => x.classList.remove("sel"));
      list.querySelector(`[data-s="${CSS.escape(c.stored)}"]`)?.classList.add("sel");
      caption.value = c.caption || `Vui mừng chia sẻ: tôi vừa nhận được chứng chỉ "${c.title}"! 🎉`;
      const blob = await blobOf(c);
      preview.replaceChildren();
      if (blob && blob.type.startsWith("image/")) {
        const img = el("img");
        img.src = URL.createObjectURL(blob);
        preview.append(img);
      } else {
        preview.textContent = c.name;
      }
    };
    const refill = (select) => {
      list.replaceChildren();
      for (const c of certs) {
        const b = el("button", "certItem", `${fmtDate(c.added)}  ${c.title.slice(0, 22)}`);
        b.dataset.s = c.stored;
        b.onclick = () => show(c);
        list.append(b);
      }
      if (certs.length) show(select || certs[certs.length - 1]);
    };
    const saveCaption = async () => {
      if (!cur) return null;
      cur.caption = caption.value.trim();
      await this.lib.save();
      return cur;
    };
    const upload = el("button", "primary", "＋ Upload ảnh certificate");
    const input = el("input");
    input.type = "file";
    input.accept = "image/*,.pdf";
    input.hidden = true;
    input.onchange = async () => {
      const f = input.files[0];
      input.value = "";
      if (!f) return;
      const d = new Date();
      const storedName = `${isoDate(d).replaceAll("-", "")}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}_${safeName(f.name)}`;
      await idbPut("files", "cert/" + storedName, f.slice(0, f.size, f.type));
      const c = { name: f.name, stored: storedName, title: stem(f.name), added: isoDate(), caption: "" };
      certs.push(c);
      await this.lib.save();
      refill(c);
      let block;
      try { block = await fileToBlock(f); } catch { return; }
      this.app.closeSheet();
      this.setPrompt("Đây là chứng chỉ tôi vừa nhận được. Hãy viết giúp tôi một lời chia sẻ ngắn gọn, " +
        "tự nhiên (tiếng Việt, kèm bản tiếng Anh) để gửi email và đăng lên nhóm WhatsApp, " +
        "nêu tên khóa học và cảm ơn thầy cô, bạn học.",
        { kind: "certificate", label: `Certificate · ${f.name.slice(0, 40)}`, blocks: [block] });
    };
    upload.onclick = () => input.click();
    const remove = el("button", "", "🗑 Xóa");
    remove.onclick = async () => {
      if (!cur || !confirm(`Xóa chứng chỉ "${cur.title}" khỏi danh sách?`)) return;
      certs.splice(certs.indexOf(cur), 1);
      await idbDel("files", "cert/" + cur.stored).catch(() => {});
      await this.lib.save();
      cur = null;
      preview.textContent = "";
      refill();
    };
    const share = async (how) => {
      const c = await saveCaption();
      if (!c) return;
      const blob = await blobOf(c);
      if (how === "email" && !navigator.canShare) {
        location.href = `mailto:?subject=${encodeURIComponent("Chứng chỉ: " + c.title)}&body=${encodeURIComponent(c.caption)}`;
        return;
      }
      // iPad share sheet: Mail, WhatsApp, Messages... with the picture and the text
      if (blob) await shareBlob(blob, c.name, c.caption);
    };
    const copyImg = el("button", "", "📋 Copy ảnh");
    copyImg.onclick = async () => {
      if (!cur) return;
      try {
        const blob = await blobOf(cur);
        const bmp = await createImageBitmap(blob);
        const cv = el("canvas");
        cv.width = bmp.width; cv.height = bmp.height;
        cv.getContext("2d").drawImage(bmp, 0, 0);
        const png = await new Promise((r) => cv.toBlob(r, "image/png"));
        await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
        this.app.setStatus("✓ Đã copy ảnh chứng chỉ (chạm giữ → Dán để gửi kèm)", "ok");
      } catch (e) {
        alert("Không copy được ảnh: " + (e.message || e));
      }
    };
    const mail = el("button", "blue", "📧 Email");
    mail.onclick = () => share("email");
    const wa = el("button", "green", "💬 WhatsApp");
    wa.onclick = () => share("whatsapp");
    const openB = el("button", "", "📄 Mở");
    openB.onclick = async () => { if (cur) { const b = await blobOf(cur); if (b) openBlob(b, cur.name); } };
    const left = el("div", "certLeft");
    left.append(list, upload, remove, input);
    const shareRow = el("div", "actions");
    shareRow.append(el("b", "", "Share:"), mail, wa, copyImg, openB);
    right.append(preview, el("b", "", "Lời chia sẻ (có thể sửa):"), caption, shareRow);
    caption.onchange = saveCaption;
    body.append(left, right);
    this.app.sheet("Certificate — chứng chỉ", body, true);
    refill();
  }
}
