// 🌐 Source for course sites that need a login (Windows 1.9.9 opens its own login browser; a web app can't, and
// can't read another site's cookies). The iPad way: a bookmark "📥 Live Translator" (bookmarklet).
//
//   1. Learning → Source opens the materials page in a new tab (that tab's opener is this app).
//   2. The user signs in there as usual (Safari/Chrome keeps the login), opens the materials page and taps the
//      bookmark. Its code runs inside that signed-in page: it collects the document links (also pluginfile /
//      forcedownload links without an extension, and frames), downloads them with the login, then
//   3. sends them to this app with postMessage, only to this app's origin. If the tab lost its opener (some
//      sign-in pages cut it), a button in the page opens the app and sends them.
//   4. The app shows what came in, asks once, skips files it already has, and runs the usual sync (upload for
//      Claude, catalogue).

import { ROOT } from "./updates.js";

/** The bookmark's code: one line, no "%" (iOS bookmarks treat it as an escape). app: the app's root URL. */
export function bookmarklet(app = ROOT) {
  /* eslint-disable */
  const fn = async function (APP) {
    const ORIGIN = new URL(APP).origin;
    if (window.__ltSrc) return;
    window.__ltSrc = 1;
    const EXT = /\.(pdf|docx|txt|md|csv|tsv|srt|vtt|png|jpe?g|gif|webp)$/i;
    const LIKELY = /download|export|attachment|file|resource|material|document|\/d\/|pluginfile|forcedownload/i;
    const bar = document.createElement("div");
    bar.style.cssText = "position:fixed;left:12px;right:12px;bottom:18px;margin:auto;max-width:560px;z-index:2147483647;" +
      "background:#1f1e1c;color:#fff;font:15px -apple-system,Segoe UI,sans-serif;padding:12px 14px;border-radius:14px;" +
      "box-shadow:0 8px 28px rgba(0,0,0,.35);line-height:1.4";
    const msg = document.createElement("div");
    bar.appendChild(msg);
    document.body.appendChild(bar);
    const say = (t) => { msg.textContent = "📥 Live Translator: " + t; };
    const done = (t) => { say(t); window.__ltSrc = 0; setTimeout(() => bar.remove(), 8000); };
    const seen = new Set(), links = [];
    const collect = (d) => {
      for (const a of d.querySelectorAll("a[href]")) {
        const h = String(a.href || "").split("#")[0];
        if (!/^https?:/i.test(h) || seen.has(h)) continue;
        seen.add(h);
        const dl = a.getAttribute("download") || "";
        let path = h;
        try { path = decodeURIComponent(new URL(h).pathname); } catch (e) { /* keep */ }
        if (EXT.test(path) || EXT.test(dl) || LIKELY.test(h)) {
          links.push({ url: h, text: (a.innerText || a.title || a.getAttribute("aria-label") || "").trim().slice(0, 200), dl });
        }
      }
      for (const f of d.querySelectorAll("iframe,frame")) {
        try { if (f.contentDocument) collect(f.contentDocument); } catch (e) { /* other site */ }
      }
    };
    collect(document);
    if (!links.length) { done("trang này không có link tài liệu. Hãy mở trang chứa tài liệu (Materials) rồi chạm lại."); return; }
    const files = [], pages = [], fails = [];
    let k = 0;
    for (const l of links.slice(0, 120)) {
      say("đang tải " + (++k) + "/" + Math.min(links.length, 120) + "...");
      try {
        const r = await fetch(l.url, { credentials: "include" });
        if (!r.ok) { fails.push((l.text || l.url) + " (HTTP " + r.status + ")"); continue; }
        const type = (r.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
        const cd = r.headers.get("content-disposition") || "";
        if (type === "text/html" && !/filename/i.test(cd)) { pages.push(l.url); continue; }
        const m = /filename\*=(?:UTF-8'')?([^;]+)|filename="?([^";]+)"?/i.exec(cd);
        let name = m ? (m[1] || m[2]) : (l.dl || "");
        if (!name) { try { name = new URL(r.url).pathname.split("/").pop(); } catch (e) { name = ""; } }
        try { name = decodeURIComponent(name); } catch (e) { /* keep */ }
        const buf = await r.arrayBuffer();
        if (buf.byteLength > 200 * 1048576) { fails.push((l.text || name) + " (quá lớn)"); continue; }
        files.push({ url: l.url, name: String(name || "").trim(), text: l.text, type, buf });
      } catch (e) { fails.push(l.text || l.url); }
    }
    if (!files.length) { done("không tải được tài liệu nào" + (fails.length ? " (" + fails.length + " lỗi)" : "") + "."); return; }
    const data = { type: "lt-files", url: location.href, title: document.title, files, pages, fails };
    const send = (w) => w.postMessage(data, ORIGIN, files.map((f) => f.buf));
    if (window.opener && !window.opener.closed) {
      try { send(window.opener); done("đã gửi " + files.length + " tài liệu. Quay lại tab Live Translator để thêm vào thư viện."); return; }
      catch (e) { /* opener gone: the button below */ }
    }
    say("đã tải " + files.length + " tài liệu.");
    const b = document.createElement("button");
    b.textContent = "📤 Mở Live Translator và gửi";
    b.style.cssText = "display:block;margin-top:10px;width:auto;left:0;right:0;box-sizing:border-box;padding:12px 18px;border:0;border-radius:10px;" +
      "background:#c96442;color:#fff;font:600 16px -apple-system,Segoe UI,sans-serif";
    bar.appendChild(b);
    b.onclick = () => {
      const w = window.open(APP, "_blank");
      if (!w) { say("hãy cho phép mở cửa sổ mới (pop-up) rồi bấm lại."); return; }
      b.remove();
      say("đang chờ app mở...");
      const h = (e) => {
        if (e.source !== w || e.origin !== ORIGIN || !e.data || e.data.type !== "lt-ready") return;
        removeEventListener("message", h);
        send(w);
        done("đã gửi " + files.length + " tài liệu cho app.");
      };
      addEventListener("message", h);
    };
  };
  /* eslint-enable */
  const code = fn.toString().replace(/\n\s*/g, " ");
  if (code.includes("%")) throw new Error("bookmarklet must not contain a percent sign");
  return `javascript:(${code})(${JSON.stringify(app)});void 0`;
}

/** Receives documents from the bookmark (see the top of this file). */
export class SourceBridge {
  constructor(app) {
    this.app = app;
    window.addEventListener("message", (e) => this.onMessage(e));
    // opened by the bookmark's button: say we are here (no data in it, so any opener may see it)
    try { if (window.opener && window.opener !== window) window.opener.postMessage({ type: "lt-ready" }, "*"); } catch { /* */ }
  }

  async onMessage(e) {
    const d = e.data;
    if (!d || d.type !== "lt-files" || !Array.isArray(d.files)) return;
    const L = this.app.learning;
    await L.ready;
    let host = e.origin;
    try { host = new URL(d.url).host; } catch { /* */ }
    const known = (L.lib.data.source_files ||= {});
    // already in the library: by link (received before) or by file name (Windows 1.9.9 checks the study folder)
    const names = new Set(L.lib.docs().map((x) => String(x.name || "").toLowerCase()));
    const files = d.files.filter((f) => f && f.buf instanceof ArrayBuffer);
    const usable = files.filter((f) => L.sourceUsable(L.sourceName(f)));          // e.g. no .zip
    const fresh = usable.filter((f) => !known[f.url] && !names.has(L.sourceName(f).toLowerCase()));
    const already = usable.length - fresh.length;
    // the user confirms: anything that can open this app could send files
    const ok = await L.confirmSource(host, d.title || d.url, fresh.map((f) => L.sourceName(f)), already,
                                     files.length - usable.length);
    if (!ok) return;
    L.importSource(d, fresh);
  }
}
