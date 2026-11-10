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
const safeName = (name) => name.replace(/[\\/:*?"<>|]+/g, "_").trim() || "file";
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
    r.onsuccess = () => resolve(r.result);
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
    Object.assign(doc, { title: res.title || doc.title, description: res.description || "",
      kind: KINDS.includes(res.kind) ? res.kind : "other", day: Math.max(0, parseInt(res.day, 10) || 0),
      date: res.date || "", exercises: res.exercises || [], catalogued: true });
  }

  async arrangeDays() {
    const docs = this.lib.docs();
    if (docs.length < 2) return;
    const listing = docs.map((d) => ({ id: d.id, path: d.rel, title: d.title, kind: d.kind, day: d.day, date: d.date }));
    const agendas = [];
    for (const d of docs) if (d.kind === "agenda") agendas.push((await this.lib.text(d)).slice(0, 20_000));
    let prompt = "Documents:\n" + JSON.stringify(listing);
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
  const name = `${safeName(stem(template.name))}_${isoDate(d).replaceAll("-", "")}_${pad(d.getHours())}${pad(d.getMinutes())}.pdf`;
  const bytes = await fillTemplate(buf, res.fields || [], res.placements || [], layout);
  const blob = new Blob([bytes], { type: "application/pdf" });
  await idbPut("files", "journal/" + name, blob);
  return { name, blob, preview: res.preview || "" };
}

// ---------------- open / save / share a file on the iPad ----------------
export function openBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const w = window.open(url, "_blank");
  if (!w) shareBlob(blob, name);                         // pop-up blocked (home-screen app): share sheet
  setTimeout(() => URL.revokeObjectURL(url), 120_000);
}

export async function shareBlob(blob, name, text) {
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
  }

  // ---- menu ----
  async showMenu() {
    await this.ready;
    if (!this.app.panelSide) this.app.togglePanel("right");
    const n = Object.keys(this.lib.data.docs).length;
    const body = el("div", "lmenu");
    body.append(el("p", "muted", n ? `Thư viện: ${n} tài liệu · đồng bộ ${this.lib.data.synced || "-"}`
                                   : "Chưa có tài liệu — chọn Folder để bắt đầu"));
    const item = (label, fn, enabled = true) => {
      const b = el("button", "lmItem", label);
      b.disabled = !enabled;
      b.onclick = () => { this.app.closeSheet(); fn(); };
      body.append(b);
    };
    item("📁  Folder — " + (this.syncing ? "đang đồng bộ..." : n ? "đồng bộ thư mục học" : "chọn thư mục học"),
         () => this.folder(), !this.syncing);
    item("🗓  Agenda — chương trình học", () => this.agenda(), n > 0);
    item("📚  Documents — danh sách tài liệu", () => this.documents(), n > 0);
    item("📝  Summary — tổng kết buổi học", () => this.summary(), n > 0);
    item("✏️  Exercises — bài tập, hướng dẫn từng bước", () => this.exercises(), n > 0);
    item("📓  Journal — điền nhật ký buổi học (PDF)", () => this.journal(), n > 0);
    item("🏆  Certificate — chứng chỉ & chia sẻ", () => this.certificate());
    this.app.sheet("🎓 Learning", body);
  }

  // ---- prompt + job ----
  setPrompt(text, job) {
    const p = document.getElementById("prompt");
    p.value = text;
    this.job = job;
    this.app.refreshAttachBar();
    if (!this.app.panelSide) this.app.togglePanel("right");
    p.focus({ preventScroll: true });
  }

  takeJob() {
    const job = this.job;
    this.job = null;
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
    w.append(el("span", "who", "🎓 Learning: "), err ? el("span", "err", text) : text);
    this.app.scrollEnd(w);
  }

  // ---- Folder ----
  async folder() {
    await this.ready;
    const body = el("div", "lform");
    const last = this.lib.data.folder;
    body.append(el("p", "", last
      ? `Lần trước: ${last} (đồng bộ ${this.lib.data.synced || "-"}). Chọn lại thư mục hoặc các file học — file đã có sẽ tự bỏ qua, chỉ tài liệu mới được upload.`
      : "Chọn thư mục học (hoặc chọn nhiều file trong app Tệp) để Claude đọc, phân loại theo buổi và tìm bài tập."));
    const row = el("div", "actions");
    const pickFiles = el("button", "primary", "📄 Chọn file");
    const pickDir = el("button", "", "📁 Chọn cả thư mục");
    const refresh = el("button", "", "🔄 Chỉ làm mới (upload/phân loại phần còn thiếu)");
    const input = el("input");
    input.type = "file";
    input.multiple = true;
    input.hidden = true;
    input.onchange = () => { const fs = [...input.files]; input.remove(); if (fs.length) this.sync(fs); };
    pickFiles.onclick = () => { this.app.closeSheet(); input.removeAttribute("webkitdirectory"); input.click(); };
    pickDir.onclick = () => { this.app.closeSheet(); input.setAttribute("webkitdirectory", ""); input.click(); };
    refresh.onclick = () => { this.app.closeSheet(); this.sync(null); };
    document.body.append(input);
    row.append(pickFiles);
    if ("webkitdirectory" in input) row.append(pickDir);
    if (last) row.append(refresh);
    body.append(row);
    this.app.sheet("Learning — Folder", body);
  }

  async sync(files) {
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
    }
  }

  // ---- Agenda ----
  agenda() {
    const ids = this.lib.docs().filter((d) => d.kind === "agenda").map((d) => d.id);
    const src = ids.length ? "tài liệu Agenda đính kèm và danh mục tài liệu" : "danh mục tài liệu đã upload";
    this.setPrompt(
      `Hãy in ra Agenda (chương trình học) của khóa học dựa trên ${src}: liệt kê theo từng buổi/ngày ` +
      "gồm ngày học, chủ đề chính, nội dung từng phần, bài tập và tài liệu liên quan. Trình bày gọn, dễ đọc.",
      { kind: "agenda", label: ids.length ? `Agenda · ${ids.length} tài liệu` : "Agenda · danh mục", doc_ids: ids });
  }

  // ---- Summary ----
  summary() {
    this.pickDay("Summary — tổng kết buổi học", true, ([day, date, docs, mode]) => {
      const books = docs.filter((d) => !["journal_template", "certificate"].includes(d.kind));
      const when = day ? `Buổi ${day}` + (date ? ` (ngày ${fmtDate(date)})` : "") : "toàn khóa học";
      const how = mode === "book" ? "chỉ dựa trên tài liệu sách/bài học đã upload của buổi này"
        : "kết hợp tài liệu sách đã upload của buổi này với nội dung thầy giảng (bản ghi lời giảng và " +
          "bản dịch đang có trên màn hình); ghi rõ ý nào từ sách, ý nào thầy nhấn mạnh hoặc bổ sung";
      this.setPrompt(
        `Hãy tổng kết tất cả thông tin của ${when}, ${how}. Gồm: các ý chính, khái niệm quan trọng, ví dụ, ` +
        "bài tập đã giao và những điểm cần ghi nhớ.",
        { kind: "summary", label: `Summary · ${when} · ${mode === "book" ? "Sách" : "Sách + thầy giảng"} · ${books.length} tài liệu`,
          doc_ids: books.map((d) => d.id) });
    });
  }

  /** Choose the course day (and summary mode); calls done([day, date, docs, mode]). */
  pickDay(title, withMode, done) {
    const choices = this.lib.days().map(([day, date, ds]) => [day, date, ds,
      `Buổi ${day}` + (date ? ` · ${fmtDate(date)}` : "") + " · " +
      ds.slice(0, 3).map((d) => (d.title || d.name).slice(0, 40)).join(", ") + (ds.length > 3 ? " ..." : "")]);
    choices.push([0, "", this.lib.docs(), "Toàn khóa (tất cả tài liệu)"]);
    const body = el("div", "lform");
    body.append(el("b", "", "Chọn buổi học:"));
    const sel = el("select", "wide");
    choices.forEach((c, i) => sel.append(new Option(c[3], String(i))));
    sel.value = String(choices.length > 1 ? choices.length - 2 : 0);
    body.append(sel);
    let mode = "mix";
    if (withMode) {
      body.append(el("b", "", "Cách tổng kết:"));
      for (const [val, txt] of [["book", "Theo file sách đã upload"], ["mix", "Kết hợp sách + nội dung thầy giảng trên màn hình"]]) {
        const lab = el("label", "radio");
        const r = el("input");
        r.type = "radio";
        r.name = "lmode";
        r.checked = val === mode;
        r.onchange = () => { mode = val; };
        lab.append(r, " " + txt);
        body.append(lab);
      }
    }
    this.buttons(body, "Tạo prompt", () => {
      const c = choices[+sel.value];
      done([c[0], c[1], c[2], mode]);
    });
    this.app.sheet(title, body);
  }

  buttons(body, ok, onOk) {
    const row = el("div", "actions end");
    const cancel = el("button", "", "Hủy");
    cancel.onclick = () => this.app.closeSheet();
    const go = el("button", "primary", ok);
    go.onclick = () => { this.app.closeSheet(); onOk(); };
    row.append(cancel, go);
    body.append(row);
    return go;
  }

  // ---- Exercises ----
  exercises() {
    const exs = this.lib.exercises();
    if (!exs.length) {
      alert("Chưa tìm thấy bài tập nào trong các tài liệu đã upload.\nHãy thêm tài liệu bài tập vào thư mục học rồi chọn Folder để đồng bộ lại.");
      return;
    }
    const body = el("div", "lform");
    const table = el("div", "ltable");
    const desc = el("p", "muted", "Chạm để chọn một bài tập.");
    let chosen = null;
    const choose = () => {
      if (!chosen) return;
      const ex = chosen, doc = ex.doc;
      const where = ex.location ? `, ${ex.location}` : "";
      const day = ex.day ? `Buổi ${ex.day}, ` : "";
      this.setPrompt(
        `Hướng dẫn tôi làm bài tập "${ex.title}" (${day}trong tài liệu "${doc.title || doc.name}"` +
        `${where}) từng bước một. Bắt đầu bằng việc nêu lại đề bài và mục tiêu, sau đó chỉ hướng dẫn bước 1 ` +
        "và chờ tôi trả lời hoặc làm xong rồi mới sang bước tiếp theo. Không đưa ngay toàn bộ lời giải; " +
        "nếu tôi làm sai, hãy gợi ý để tôi tự sửa.",
        { kind: "exercise", label: `Exercise · ${ex.title.slice(0, 40)}`, doc_ids: [doc.id] });
    };
    table.append(el("div", "lth", "Buổi"), el("div", "lth", "Bài tập"), el("div", "lth", "Tài liệu"));
    for (const ex of exs) {
      const cells = [el("div", "ltd", String(ex.day || "-")), el("div", "ltd", ex.title), el("div", "ltd", ex.doc.title || ex.doc.name)];
      for (const c of cells) {
        c.onclick = () => {
          table.querySelectorAll(".sel").forEach((x) => x.classList.remove("sel"));
          cells.forEach((x) => x.classList.add("sel"));
          chosen = ex;
          desc.textContent = `${ex.summary}  (${ex.location})`;
        };
        c.ondblclick = () => { this.app.closeSheet(); choose(); };
      }
      table.append(...cells);
    }
    body.append(table, desc);
    this.buttons(body, "Chọn bài", choose);
    this.app.sheet("Exercises — chọn bài tập", body, true);
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
      try { openBlob(await this.lib.file(d), d.name); } catch (e) { alert("Không mở được file: " + e.message); }
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
    const row = el("div", "actions end");
    const openBtn = el("button", "", "📄 Mở file");
    openBtn.onclick = () => chosen && open(chosen);
    const ask = el("button", "primary", "💬 Hỏi Claude về tài liệu này");
    ask.onclick = () => {
      if (!chosen) return;
      const d = chosen;
      this.app.closeSheet();
      this.setPrompt(`Hãy giới thiệu tài liệu "${d.title || d.name}": nội dung chính, các ý quan trọng và nó liên quan thế nào đến khóa học.`,
        { kind: "doc", label: `Tài liệu · ${(d.title || d.name).slice(0, 40)}`, doc_ids: [d.id] });
    };
    row.append(openBtn, ask);
    body.append(q, table, desc, row);
    this.app.sheet("Documents — tài liệu đã upload", body, true);
  }

  // ---- Journal ----
  journal() {
    const temps = this.lib.templates();
    if (!temps.length) {
      alert('Chưa tìm thấy file PDF mẫu nhật ký trong các tài liệu đã upload.\nHãy đặt file mẫu (ví dụ "Journal.pdf" / "Nhat ky.pdf") vào thư mục học rồi chọn Folder để đồng bộ lại.');
      return;
    }
    this.pickDay("Journal — chọn buổi học", false, ([day, date, docs]) => {
      const template = temps[0];
      const d = new Date();
      const when = day ? `Buổi ${day}` + (date ? ` (ngày ${fmtDate(date)})` : "")
        : `buổi học hôm nay (${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()})`;
      this.setPrompt(
        `Hãy điền nhật ký buổi học cho ${when} theo đúng mẫu "${template.name}", dựa trên tài liệu của ` +
        "buổi này và nội dung thầy giảng trên màn hình. Viết ngắn gọn, cụ thể, đúng từng mục trong mẫu, " +
        "rồi trả lại file PDF đã điền để tải về.",
        { kind: "journal", label: `Journal · ${when} · mẫu ${template.name.slice(0, 30)}`,
          doc_ids: docs.filter((x) => x.id !== template.id).map((x) => x.id), template: template.id });
    });
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
