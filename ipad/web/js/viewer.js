// Opening a file on the iPad (Learning: documents, outputs, certificates...).
//
//  1. Files the app can show (PDF, Word .docx, pictures, text, audio, video) open AT ONCE in a full-screen
//     viewer inside the app: no "open with" question. The viewer has a button for other apps (Books, Files...).
//  2. Other files (Excel, PowerPoint, EPUB, Pages...) go straight to the iPad's app list. The file is given
//     its real type, so iPad lists every app that can open it (Books for PDF/EPUB, Word/Pages for Word...).

import { unzipEntry } from "./files.js";

const ROOT = new URL("../../../", import.meta.url).href;
const PDFJS = ROOT + "lib/pdfjs-4.10.38/";

const extOf = (name) => { const i = String(name).lastIndexOf("."); return i >= 0 ? name.slice(i + 1).toLowerCase() : ""; };

/** File type by extension: iPad picks the apps of its list from it (a file without a type gets few or none). */
export const MIME = {
  pdf: "application/pdf", epub: "application/epub+zip",
  doc: "application/msword", docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xls: "application/vnd.ms-excel", xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ppt: "application/vnd.ms-powerpoint", pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  pages: "application/vnd.apple.pages", numbers: "application/vnd.apple.numbers", key: "application/vnd.apple.keynote",
  odt: "application/vnd.oasis.opendocument.text", ods: "application/vnd.oasis.opendocument.spreadsheet",
  odp: "application/vnd.oasis.opendocument.presentation", rtf: "application/rtf",
  txt: "text/plain", md: "text/markdown", csv: "text/csv", tsv: "text/tab-separated-values", json: "application/json",
  xml: "application/xml", html: "text/html", htm: "text/html", srt: "text/plain", vtt: "text/vtt", log: "text/plain",
  yaml: "text/plain", yml: "text/plain",
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", heic: "image/heic",
  heif: "image/heif", svg: "image/svg+xml", bmp: "image/bmp", tif: "image/tiff", tiff: "image/tiff",
  mp3: "audio/mpeg", m4a: "audio/mp4", aac: "audio/aac", wav: "audio/wav", ogg: "audio/ogg",
  mp4: "video/mp4", m4v: "video/mp4", mov: "video/quicktime", webm: "video/webm",
  zip: "application/zip",
};

/** The real type of a file: its own if it has a useful one, else from the extension. */
export function mimeOf(name, type = "") {
  if (type && type !== "application/octet-stream") return type;
  return MIME[extOf(name)] || "application/octet-stream";
}

/** A copy of the file that carries its name and real type (for the viewer and the iPad app list). */
export function typedFile(blob, name) {
  const type = mimeOf(name, blob?.type);
  return blob instanceof File && blob.name === name && blob.type === type ? blob : new File([blob], name, { type });
}

const TEXT_EXT = new Set(["txt", "md", "csv", "tsv", "json", "xml", "log", "srt", "vtt", "yaml", "yml", "ini", "py", "js",
  "ts", "java", "cs", "cpp", "c", "h", "go", "rb", "php", "sql"]);

/** How the app itself shows this file, or "" when only another app can. */
export function viewKind(name, type = "") {
  const e = extOf(name), t = mimeOf(name, type);
  if (e === "pdf" || t === "application/pdf") return "pdf";
  if (e === "docx") return "docx";
  if (e === "html" || e === "htm") return "html";
  if (TEXT_EXT.has(e) || t.startsWith("text/")) return "text";
  if (t.startsWith("image/") && e !== "tif" && e !== "tiff") return "image";
  if (t.startsWith("audio/")) return "audio";
  if (t.startsWith("video/")) return "video";
  return "";
}

/** Apps to look for in the iPad list, by file type (said under the button: the list itself is the iPad's). */
export function appHint(name) {
  const e = extOf(name);
  if (e === "pdf") return "Sách (Books), Tệp, Adobe Acrobat, GoodNotes, Notability, PDF Expert, Google Drive…";
  if (e === "epub") return "Sách (Books), Google Play Sách, Kindle…";
  if (["doc", "docx", "odt", "rtf", "pages"].includes(e)) return "Microsoft Word, Pages, Google Tài liệu, WPS Office, Tệp…";
  if (["xls", "xlsx", "ods", "csv", "numbers"].includes(e)) return "Microsoft Excel, Numbers, Google Trang tính, WPS Office, Tệp…";
  if (["ppt", "pptx", "odp", "key"].includes(e)) return "Microsoft PowerPoint, Keynote, Google Trang trình bày, WPS Office, Tệp…";
  if (MIME[e]?.startsWith("image/")) return "Ảnh, Tệp, GoodNotes, Notability…";
  if (MIME[e]?.startsWith("audio/") || MIME[e]?.startsWith("video/")) return "Tệp, VLC, Ảnh…";
  if (e === "zip") return "Tệp (tự giải nén), Documents by Readdle…";
  return "Tệp, Google Drive, OneDrive, Documents by Readdle…";
}

const NATIVE = typeof window !== "undefined" && window.LT_NATIVE && window.webkit?.messageHandlers?.lt;

async function nativeShare(blob, name, text) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  window.webkit.messageHandlers.lt.postMessage({ type: "share", name, b64: btoa(s), text: text || "" });
  return true;
}

/** The iPad app list (share sheet) with the file's real type. true = shown, false = cancelled,
 *  "gesture" = iPad wants a fresh tap first (the caller shows a button), downloads as a last resort. */
export async function shareFile(blob, name, text, { download = true } = {}) {
  if (NATIVE) return nativeShare(blob, name, text);
  const file = typedFile(blob, name);
  if (navigator.canShare?.({ files: [file] })) {
    try {
      await navigator.share({ files: [file], ...(text ? { text } : {}) });   // Books, Files, Mail, Word...
      return true;
    } catch (e) {
      if (e?.name === "AbortError") return false;
      if (e?.name === "NotAllowedError" && !download) return "gesture";      // the tap was too long ago
    }
  }
  if (!download) return "gesture";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(file);
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 60_000);
  return true;
}

let pdfjsP = null;
function pdfjs() {
  pdfjsP = pdfjsP || import(PDFJS + "pdf.min.mjs").then((m) => {
    m.GlobalWorkerOptions.workerSrc = PDFJS + "pdf.worker.min.mjs";
    return m;
  });
  return pdfjsP;
}

const el = (tag, cls = "", text = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};
const fmtSize = (n) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);

/** Opens a file the fastest way (see the top of this file). */
export function openFile(blob, name) {
  const file = typedFile(blob, name);
  const kind = viewKind(name, file.type);
  if (kind) { new Viewer(file, kind).show(); return; }
  // nothing to show here: straight to the iPad app list; if iPad wants a fresh tap, a card with the button
  shareFile(file, name, "", { download: false }).then((r) => { if (r === "gesture") new Viewer(file, "").show(); });
}

class Viewer {
  constructor(file, kind) {
    this.file = file;
    this.kind = kind;
    this.urls = [];
  }

  url() {
    const u = URL.createObjectURL(this.file);
    this.urls.push(u);
    return u;
  }

  show() {
    const d = this.dlg = el("dialog", "viewer");
    const bar = el("div", "vBar");
    const close = el("button", "vClose", "✕");
    close.title = "Đóng";
    close.setAttribute("aria-label", "Đóng");
    close.onclick = () => d.close();
    const title = el("div", "vTitle");
    title.append(el("b", "", this.file.name), el("span", "", fmtSize(this.file.size)));
    this.tools = el("div", "vTools");
    const other = el("button", "vOpen", this.kind === "pdf" ? "Mở bằng Sách / app khác" : "Mở bằng app khác");
    other.title = appHint(this.file.name);
    other.onclick = () => shareFile(this.file, this.file.name);   // a tap of its own: the list always shows
    bar.append(close, title, this.tools, other);
    this.body = el("div", "vBody");
    d.append(bar, this.body);
    d.addEventListener("close", () => this.dispose());
    d.addEventListener("cancel", () => d.close());
    document.body.append(d);
    d.showModal();
    close.focus({ preventScroll: true });
    this.render().catch((e) => this.fail(e));
  }

  dispose() {
    this.closed = true;
    this.io?.disconnect();
    try { this.pdf?.destroy(); } catch { /* */ }
    for (const u of this.urls) URL.revokeObjectURL(u);
    this.dlg.remove();
  }

  fail(e) {
    console.error("viewer", e);
    this.card(`Không xem được file này trong app (${e?.message || e}).`);
  }

  /** No preview here: the file, and the button for the iPad app list. */
  card(why = "") {
    const c = el("div", "vCard");
    c.append(el("div", "vIcon", "📄"), el("b", "", this.file.name), el("p", "muted", why ||
      "Loại file này cần app khác để mở. Chạm nút dưới đây rồi chọn app trong danh sách của iPad " +
      "(kéo sang phải hoặc bấm \"Thêm\" để xem đủ các app)."));
    const b = el("button", "primary vBig", "Mở bằng app…");
    b.onclick = () => shareFile(this.file, this.file.name);
    c.append(b, el("p", "vHint", "Gợi ý: " + appHint(this.file.name)));
    this.body.replaceChildren(c);
    this.tools.replaceChildren();
  }

  async render() {
    switch (this.kind) {
      case "pdf": return this.renderPdf();
      case "docx": return this.renderDocx();
      case "text": return this.renderText();
      case "html": {
        const f = el("iframe", "vFrame");
        f.sandbox = "";                                  // no scripts, nothing reaches the app
        f.srcdoc = await this.file.text();
        this.body.replaceChildren(f);
        return;
      }
      case "image": {
        const img = el("img", "vImg");
        img.alt = this.file.name;
        img.onerror = () => this.card("iPad không hiển thị được ảnh này.");
        img.src = this.url();
        this.body.replaceChildren(img);
        this.zoomable(img, (z) => { img.style.width = `${z * 100}%`; img.style.maxWidth = z > 1 ? "none" : ""; });
        return;
      }
      case "audio":
      case "video": {
        const m = el(this.kind, "vMedia");
        m.controls = true;
        m.playsInline = true;
        m.onerror = () => this.card("iPad không phát được file này trong app.");
        m.src = this.url();
        this.body.replaceChildren(m);
        return;
      }
      default: this.card();
    }
  }

  // ---- PDF: pages drawn as they come into view, sharp at any zoom ----
  async renderPdf() {
    this.body.replaceChildren(el("p", "vLoading", "Đang mở PDF..."));
    const lib = await pdfjs();
    this.pdf = await lib.getDocument({ data: new Uint8Array(await this.file.arrayBuffer()), isEvalSupported: false }).promise;
    if (this.closed) return;
    const n = this.pdf.numPages;
    const first = await this.pdf.getPage(1);
    const vp1 = first.getViewport({ scale: 1 });
    const pages = el("div", "vPages");
    this.body.replaceChildren(pages);
    this.zoom = 1;
    const slots = [];
    for (let i = 1; i <= n; i++) {
      const s = el("div", "vPage");
      s.dataset.n = i;
      s.style.aspectRatio = `${vp1.width} / ${vp1.height}`;   // corrected when the page is drawn
      pages.append(s);
      slots.push(s);
    }
    const counter = el("span", "vCount", `1 / ${n}`);
    this.tools.replaceChildren(counter);
    const draw = async (s, force = false) => {
      if (this.closed || (s.drawn === this.zoom && !force) || s.drawing) return;
      s.drawing = true;
      try {
        const page = await this.pdf.getPage(+s.dataset.n);
        const vp = page.getViewport({ scale: 1 });
        s.style.aspectRatio = `${vp.width} / ${vp.height}`;
        const css = s.clientWidth || pages.clientWidth;
        const scale = Math.min(4, (css / vp.width) * Math.min(3, window.devicePixelRatio || 1));
        const v = page.getViewport({ scale });
        const c = document.createElement("canvas");
        c.width = Math.floor(v.width);
        c.height = Math.floor(v.height);
        await page.render({ canvasContext: c.getContext("2d"), viewport: v }).promise;
        s.replaceChildren(c);
        s.drawn = this.zoom;
      } catch (e) {
        s.textContent = `Trang ${s.dataset.n}: lỗi (${e.message || e})`;
      } finally {
        s.drawing = false;
      }
    };
    this.io = new IntersectionObserver((ents) => {
      for (const en of ents) {
        if (!en.isIntersecting) continue;
        draw(en.target);
        counter.textContent = `${en.target.dataset.n} / ${n}`;
      }
    }, { root: this.body, rootMargin: "120% 0px" });
    slots.forEach((s) => this.io.observe(s));
    this.zoomable(pages, (z) => {
      pages.style.width = `${z * 100}%`;
      clearTimeout(this.redraw);
      this.redraw = setTimeout(() => {                    // sharp again after zooming
        for (const s of slots) {
          const r = s.getBoundingClientRect(), b = this.body.getBoundingClientRect();
          if (r.bottom > b.top - b.height && r.top < b.bottom + b.height) draw(s, true);
          else s.drawn = 0;
        }
      }, 250);
    });
  }

  /** − / % / + buttons and pinching with two fingers. */
  zoomable(target, apply) {
    const STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3];
    this.zoom = 1;
    const pct = el("button", "vPct", "100%");
    pct.title = "Vừa khung";
    const set = (z) => {
      z = Math.max(0.5, Math.min(3, z));
      if (Math.abs(z - this.zoom) < 0.01) return;
      const b = this.body, cx = (b.scrollLeft + b.clientWidth / 2) / b.scrollWidth, cy = (b.scrollTop + b.clientHeight / 2) / b.scrollHeight;
      this.zoom = z;
      apply(z);
      pct.textContent = `${Math.round(z * 100)}%`;
      b.scrollLeft = cx * b.scrollWidth - b.clientWidth / 2;       // keep the middle in place
      b.scrollTop = cy * b.scrollHeight - b.clientHeight / 2;
    };
    const minus = el("button", "", "−"), plus = el("button", "", "+");
    minus.setAttribute("aria-label", "Thu nhỏ");
    plus.setAttribute("aria-label", "Phóng to");
    minus.onclick = () => set([...STEPS].reverse().find((s) => s < this.zoom - 0.01) ?? 0.5);
    plus.onclick = () => set(STEPS.find((s) => s > this.zoom + 0.01) ?? 3);
    pct.onclick = () => set(1);
    const grp = el("span", "vZoom");
    grp.append(minus, pct, plus);
    this.tools.append(grp);
    // iPad pinch (Safari gesture events; touches as a fallback)
    let base = 1;
    this.body.addEventListener("gesturestart", (e) => { e.preventDefault(); base = this.zoom; });
    this.body.addEventListener("gesturechange", (e) => { e.preventDefault(); set(base * e.scale); });
    if (!("ongesturestart" in window)) {
      let d0 = 0;
      const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
      this.body.addEventListener("touchstart", (e) => { if (e.touches.length === 2) { d0 = dist(e.touches); base = this.zoom; } }, { passive: true });
      this.body.addEventListener("touchmove", (e) => {
        if (e.touches.length === 2 && d0) { e.preventDefault(); set(base * dist(e.touches) / d0); }
      }, { passive: false });
      this.body.addEventListener("touchend", () => { d0 = 0; });
    }
  }

  async renderText() {
    let text;
    const raw = new Uint8Array(await this.file.arrayBuffer());
    for (const enc of ["utf-8", "utf-16le", "windows-1258"]) {
      try { text = new TextDecoder(enc, { fatal: true }).decode(raw); break; } catch { /* next */ }
    }
    if (text === undefined) text = new TextDecoder("latin1").decode(raw);
    const pre = el("pre", "vText", text.replace(/^﻿/, ""));
    this.body.replaceChildren(pre);
    this.zoomable(pre, (z) => { pre.style.fontSize = `${15 * z}px`; });
  }

  // ---- Word .docx: headings, paragraphs (bold / italic / underline), lists, tables ----
  async renderDocx() {
    const xml = new TextDecoder().decode(await unzipEntry(await this.file.arrayBuffer(), "word/document.xml"));
    const W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    const doc = new DOMParser().parseFromString(xml, "application/xml");
    const body = doc.getElementsByTagNameNS(W, "body")[0];
    if (!body) throw new Error("file Word không có nội dung");
    const kid = (n, tag) => [...n.childNodes].find((c) => c.localName === tag);
    const val = (n) => n?.getAttributeNS(W, "val") ?? n?.getAttribute("w:val");
    const on = (rp, tag) => { const x = rp && kid(rp, tag); return !!x && !["0", "false", "none"].includes(val(x)); };
    const para = (p) => {
      const pp = kid(p, "pPr");
      const style = (val(pp && kid(pp, "pStyle")) || "").toLowerCase();
      const title = /^title$/.test(style);
      const h = title ? 0 : (/heading\s*([1-6])|^([1-6])$/.exec(style) || [])[1] || (/heading/.test(style) ? 2 : 0);
      const list = pp && kid(pp, "numPr");
      const e = el(title ? "h1" : h ? `h${Math.min(6, +h + 1)}` : list ? "li" : "p");
      const align = val(pp && kid(pp, "jc"));
      if (align === "center" || align === "right") e.style.textAlign = align;
      const runs = (n) => {
        for (const r of n.childNodes) {
          if (r.localName === "hyperlink" || r.localName === "ins" || r.localName === "smartTag") { runs(r); continue; }
          if (r.localName !== "r") continue;
          const rp = kid(r, "rPr");
          for (const t of r.childNodes) {
            let piece = null;
            if (t.localName === "t") piece = document.createTextNode(t.textContent);
            else if (t.localName === "tab") piece = document.createTextNode("\t");
            else if (t.localName === "br" || t.localName === "cr") piece = el("br");
            if (!piece) continue;
            let w = piece;
            if (on(rp, "b")) { const x = el("strong"); x.append(w); w = x; }
            if (on(rp, "i")) { const x = el("em"); x.append(w); w = x; }
            if (on(rp, "u")) { const x = el("u"); x.append(w); w = x; }
            e.append(w);
          }
        }
      };
      runs(p);
      if (!e.childNodes.length) e.append(el("br"));
      return e;
    };
    const page = el("div", "vDoc");
    let ul = null;
    const add = (node) => {
      if (node.tagName === "LI") { if (!ul) { ul = el("ul"); page.append(ul); } ul.append(node); return; }
      ul = null;
      page.append(node);
    };
    for (const n of body.childNodes) {
      if (n.localName === "p") add(para(n));
      else if (n.localName === "tbl") {
        const t = el("table");
        for (const tr of n.childNodes) {
          if (tr.localName !== "tr") continue;
          const row = el("tr");
          for (const tc of tr.childNodes) {
            if (tc.localName !== "tc") continue;
            const cell = el("td");
            for (const p of tc.childNodes) if (p.localName === "p") cell.append(para(p));
            row.append(cell);
          }
          t.append(row);
        }
        add(t);
      } else if (n.localName === "sdt") {
        const c = kid(n, "sdtContent");
        for (const p of c?.childNodes || []) if (p.localName === "p") add(para(p));
      }
    }
    this.body.replaceChildren(page);
    this.zoomable(page, (z) => { page.style.fontSize = `${16 * z}px`; });
  }
}
