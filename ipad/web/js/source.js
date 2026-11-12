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
export function bookmarklet(app = ROOT, mode = "") {
  /* eslint-disable */
  const fn = async function (APP, MODE) {
    const ORIGIN = new URL(APP).origin, VERSION = 3;
    /* a second tap on the same page runs again with what the first one learned (tab clicks, the screen before) */
    if (window.__ltAgent) { window.__ltAgent.run(MODE); return; }
    const A = window.__ltAgent = { busy: false };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const KEY = "lt_src_clicks:" + location.origin + location.pathname;
    /* ---------- the bar: folds to a small tab ("—") and can be dragged with a finger ---------- */
    const bar = document.createElement("div");
    bar.style.cssText = "position:fixed;right:12px;bottom:18px;z-index:2147483647;background:#1f1e1c;color:#fff;max-width:440px;" +
      "font:15px -apple-system,Segoe UI,sans-serif;padding:12px 14px;border-radius:14px;box-shadow:0 8px 28px rgba(0,0,0,.35);" +
      "line-height:1.4;touch-action:none";
    const fold = document.createElement("span");
    fold.textContent = "—";
    fold.style.cssText = "float:right;margin:-6px -6px 0 10px;padding:2px 8px;font-weight:700;color:#cbd5e1;cursor:pointer";
    const msg = document.createElement("div"), btns = document.createElement("div");
    btns.style.cssText = "display:flex;gap:8px;flex-wrap:wrap;margin-top:8px";
    bar.append(fold, msg, btns);
    document.body.appendChild(bar);
    fold.onclick = (ev) => {
      ev.stopPropagation();
      const small = bar.dataset.small !== "1";
      bar.dataset.small = small ? "1" : "0";
      msg.style.display = btns.style.display = small ? "none" : "";
      fold.textContent = small ? "LT ▸" : "—";
    };
    let drag = null;
    const down = (x, y, t) => { if (t !== bar && t !== msg) return; const r = bar.getBoundingClientRect(); drag = { dx: x - r.left, dy: y - r.top }; };
    const move = (x, y) => {
      if (!drag) return;
      bar.style.right = "auto"; bar.style.bottom = "auto";
      bar.style.left = Math.max(0, Math.min(innerWidth - 60, x - drag.dx)) + "px";
      bar.style.top = Math.max(0, Math.min(innerHeight - 30, y - drag.dy)) + "px";
    };
    bar.addEventListener("touchstart", (e) => down(e.touches[0].clientX, e.touches[0].clientY, e.target), { passive: true });
    bar.addEventListener("mousedown", (e) => down(e.clientX, e.clientY, e.target));
    addEventListener("touchmove", (e) => { if (drag) { e.preventDefault(); move(e.touches[0].clientX, e.touches[0].clientY); } }, { passive: false });
    addEventListener("mousemove", (e) => move(e.clientX, e.clientY));
    addEventListener("touchend", () => { drag = null; });
    addEventListener("mouseup", () => { drag = null; });
    const say = (t) => { msg.textContent = "📥 Live Translator: " + t; };
    const button = (text, onclick, color) => {
      const b = document.createElement("button");
      b.textContent = text;
      b.style.cssText = "padding:9px 14px;border:0;border-radius:10px;color:#fff;font:600 15px -apple-system,Segoe UI,sans-serif;" +
        "background:" + (color || "#c96442");
      b.onclick = (ev) => { ev.stopPropagation(); onclick(); };
      btns.appendChild(b);
      return b;
    };
    /* ---------- the page: every element, also inside components (shadow DOM) and same-site frames ---------- */
    const all = (root, out) => {
      for (const e of root.querySelectorAll("*")) {
        out.push(e);
        if (e.shadowRoot) all(e.shadowRoot, out);
        if (e.tagName === "IFRAME" || e.tagName === "FRAME") { try { if (e.contentDocument) all(e.contentDocument, out); } catch (x) { } }
      }
      return out;
    };
    const mine = (e) => e === bar || bar.contains(e);
    const visible = (e) => {
      if (!e || !e.getClientRects || !e.getClientRects().length) return false;
      try { const s = getComputedStyle(e); return s.visibility !== "hidden" && s.display !== "none"; } catch (x) { return true; }
    };
    const label = (e) => (e.innerText || e.textContent || e.getAttribute("title") || e.getAttribute("aria-label") ||
      e.getAttribute("data-label") || "").replace(/\s+/g, " ").trim().slice(0, 200);
    const txt = (e) => ((e && (e.innerText || e.textContent)) || "").replace(/\s+/g, " ").trim();
    const parentOf = (e) => { const p = e.parentNode; if (p && p.host && p.nodeType === 11) return p.host; return p && p.nodeType === 1 ? p : null; };
    const inChrome = (e) => {
      for (let p = e; p; p = parentOf(p)) {
        const role = ((p.getAttribute && p.getAttribute("role")) || "").toLowerCase();
        if (/^(HEADER|FOOTER|NAV)$/.test(p.tagName) || /^(banner|contentinfo|navigation|menubar)$/.test(role)) return true;
      }
      return false;
    };
    const isLogin = () => /\/(s\/)?login\b|sign-?in|\/sso\b|saml|oauth|\/auth\b|\/idp\//i.test(location.pathname) ||
      all(document, []).some((e) => e.tagName === "INPUT" && (e.type || "").toLowerCase() === "password" && visible(e));
    /* where the materials are: the panel of the open tab, else the whole page */
    const scope = () => {
      const t = all(document, []).filter((e) => (e.getAttribute("role") || "").toLowerCase() === "tab" && !mine(e))
        .map((e) => ({ el: e, text: label(e), selected: e.getAttribute("aria-selected") === "true" }));
      const sel = t.find((x) => x.selected && visible(x.el));
      const panels = [];
      if (sel) {
        const id = sel.el.getAttribute("aria-controls"), root = sel.el.getRootNode();
        let p = id && root.getElementById ? root.getElementById(id) : null;
        if (!p && id) p = document.getElementById(id);
        if (p && visible(p)) panels.push(p);
        if (!panels.length) panels.push(...all(document, []).filter((e) => (e.getAttribute("role") || "").toLowerCase() === "tabpanel" && visible(e) && !mine(e)));
      }
      return { panels, tab: sel ? sel.text : "" };
    };
    /* Salesforce file ids (ContentDocument 069..., ContentVersion 068...) found in attributes */
    const SFID = /\b(069|068)[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?\b/;
    const FILE = /\.(pdf|docx?|pptx?|xlsx?|txt|md|csv|png|jpe?g|zip)\b/i;
    const sfPrefix = (location.pathname.match(/^(.*?)\/s\//) || [null, ""])[1];
    /* what the page shows now: real links and Salesforce file ids, only from visible elements, never the site's
       header/footer/menus unless inside the open tab's panel (Windows 1.9.17) */
    const items = (sc) => {
      const out = [], seen = {}, roots = sc && sc.panels.length ? sc.panels : [document];
      for (const root of roots) {
        for (const e of [root].concat(all(root, []))) {
          if (!e.getAttribute || mine(e)) continue;
          const raw = e.tagName === "A" ? (e.getAttribute("href") || "") : "";
          if (raw && raw !== "#" && !/^javascript:/i.test(raw) && /^https?:/i.test(e.href)) {
            if (seen[e.href] !== undefined || !visible(e) || (!(sc && sc.panels.length) && inChrome(e))) continue;
            seen[e.href] = out.length;
            out.push({ key: e.href, href: e.href, text: label(e), dl: e.getAttribute("download") || "" });
          }
          const attrs = e.attributes || [];
          let took = false;
          for (let j = 0; j < attrs.length && !took; j++) {
            const v = attrs[j].value || "";
            if (attrs[j].name === "href" || !/^https?:\/\//i.test(v) || !(/download|servlet|\/sfc\//i.test(v) || FILE.test(v))) continue;
            took = true;
            if (!visible(e)) break;
            const t = label(e).slice(0, 120);
            if (seen[v] !== undefined) { if (t && (e.tagName === "A" || !out[seen[v]].text)) out[seen[v]].text = t; break; }
            seen[v] = out.length;
            out.push({ key: v, href: v, text: t, dl: "" });
          }
          if (took) continue;
          for (let j = 0; j < attrs.length; j++) {
            if (attrs[j].name === "href" && e.tagName === "A" && seen[e.href] !== undefined) continue;
            const m = SFID.exec(attrs[j].value || "");
            if (!m) continue;
            const id = m[0], k = "sf:" + id.slice(0, 15);
            if (seen[k] !== undefined || !visible(e)) break;
            seen[k] = out.length;
            out.push({ key: k, href: location.origin + sfPrefix + "/s/" + (id.slice(0, 3) === "068" ? "contentversion/" : "contentdocument/") + id,
                       text: label(e).slice(0, 120), dl: "" });
            break;
          }
        }
      }
      return out;
    };
    const keys = (list) => { const s = {}; for (const x of list) s[x.key] = 1; return s; };
    /* a Salesforce preview page -> the file itself; 068 = a file version, 069 = the file */
    const sfDownload = (h) => {
      const m = /^(https?:\/\/[^\/]+)(\/[^?#]*?)?\/s\/(contentdocument|contentversion)\/((?:069|068)\w{12,15})/.exec(h);
      return m ? m[1] + (m[2] || "") + "/sfc/servlet.shepherd/" + (m[4].slice(0, 3) === "068" ? "version" : "document") +
        "/download/" + m[4] + "?operationContext=S1" : h;
    };
    const EXT = /\.(pdf|docx|txt|md|csv|tsv|srt|vtt|png|jpe?g|gif|webp)$/i;
    const LIKELY = /download|export|attachment|file|resource|material|document|\/d\/|pluginfile|forcedownload|servlet\.shepherd|contentdocument|contentversion|\/sfc\//i;
    const docOf = (x) => {
      const h = sfDownload(String(x.href).split("#")[0]);
      let path = h;
      try { path = decodeURIComponent(new URL(h).pathname); } catch (e) { }
      return EXT.test(path) || EXT.test(x.dl) || LIKELY.test(h) ? { url: h, text: x.text, dl: x.dl } : null;
    };
    /* the tabs the user clicks (e.g. "Materials"): replayed next time; just before such a click, note the screen:
       what appears after it is that tab's content */
    let before = null;
    A.clicks = [];
    const tabLike = (e) => {
      const role = (e.getAttribute("role") || "").toLowerCase();
      if (role === "tab" || e.tagName === "BUTTON" || role === "button" || role === "menuitem") return true;
      if (e.tagName !== "A") return false;
      const h = e.getAttribute("href") || "";
      return !h || h === "#" || /^javascript:/i.test(h) || (e.href && e.href.split("#")[0] === location.href.split("#")[0]);
    };
    document.addEventListener("click", (ev) => {
      for (const e of (ev.composedPath ? ev.composedPath() : [ev.target])) {
        if (!e || !e.getAttribute) continue;
        if (mine(e)) return;
        if (tabLike(e)) {
          const t = label(e), last = A.clicks[A.clicks.length - 1];
          if (last && last.text === t && last.tag === e.tagName) return;
          if (t && t.length <= 60) {
            A.clicks.push({ text: t, role: (e.getAttribute("role") || "").toLowerCase(), tag: e.tagName });
            if (A.clicks.length > 3) A.clicks.shift();
            before = keys(items(null));
          }
          return;
        }
      }
    }, true);
    const MAT = /^(course\s+)?materials?$|^tài liệu( học)?$|^resources$|^course files$/i;
    const clickable = (e) => { const r = (e.getAttribute("role") || "").toLowerCase(); return e.tagName === "BUTTON" || e.tagName === "A" || r === "tab" || r === "button" || r === "menuitem"; };
    const find = (c) => {
      let loose = null;
      for (const e of all(document, [])) {
        if (mine(e) || !e.getAttribute) continue;
        const role = (e.getAttribute("role") || "").toLowerCase(), text = label(e);
        const match = c.text === "__auto__" ? MAT.test(text) : text.toLowerCase() === c.text.toLowerCase();
        if (!match || !visible(e)) continue;
        if (c.text === "__auto__" ? clickable(e) || e.tagName === "LI" : (c.role ? role === c.role : e.tagName === c.tag)) return e;
        if (!loose) loose = e;
      }
      if (!loose) return null;
      const inner = [loose].concat(all(loose, [])).filter((x) => x.getAttribute && clickable(x) && visible(x));
      if (inner.length) return inner[0];
      for (let p = parentOf(loose); p; p = parentOf(p)) if (clickable(p)) return p;
      return loose;
    };
    /* pages built by scripts: wait until the page stops changing */
    const settle = async () => {
      let last = -1, stable = 0;
      for (let i = 0; i < 30 && stable < 3; i++) { await sleep(500); const n = all(document, []).length; stable = n === last ? stable + 1 : 0; last = n; }
    };
    /* ---------- sending to the app (its own origin only) ---------- */
    const send = (data, transfer, after) => {
      if (window.opener && !window.opener.closed) {
        try { window.opener.postMessage(data, ORIGIN, transfer); after(true); return; } catch (e) { }
      }
      btns.replaceChildren();
      button("📤 Mở Live Translator và gửi", () => {
        const w = window.open(APP, "_blank");
        if (!w) { say("hãy cho phép mở cửa sổ mới (pop-up) rồi bấm lại."); return; }
        say("đang chờ app mở...");
        const h = (e) => {
          if (e.source !== w || e.origin !== ORIGIN || !e.data || e.data.type !== "lt-ready") return;
          removeEventListener("message", h);
          A.app = w;
          btns.replaceChildren();
          w.postMessage(data, ORIGIN, transfer);
          after(false);
        };
        addEventListener("message", h);
      });
    };
    /* ---------- surveys (Learning → Feedback): read the questions, fill in the answers the user approved ---------- */
    const labelFor = (e) => {
      const root = e.getRootNode ? e.getRootNode() : document;
      if (e.id) { try { const l = (root.querySelector ? root : document).querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) return l; } catch (x) { } }
      for (let p = e; p; p = parentOf(p)) if (p.tagName === "LABEL") return p;
      return null;
    };
    const optionText = (e) => {
      const l = labelFor(e);
      let t = l && txt(l);
      if (t) return t;
      t = e.getAttribute("aria-label") || e.getAttribute("title");
      if (t) return t.trim();
      const root = e.getRootNode ? e.getRootNode() : document;
      t = (e.getAttribute("aria-labelledby") || "").split(/\s+/).map((id) => { const n = id && root.getElementById ? root.getElementById(id) : null; return n ? txt(n) : ""; })
        .filter(Boolean).join(" ");
      if (t) return t;
      let cell = null;
      for (let p = e; p; p = parentOf(p)) if (p.tagName === "TD" || p.tagName === "TH") { cell = p; break; }
      if (cell) {
        let table = cell; while (table && table.tagName !== "TABLE") table = parentOf(table);
        const head = table && (table.querySelector("thead tr") || table.querySelector("tr"));
        if (head && head !== cell.parentNode && head.children[cell.cellIndex]) { t = txt(head.children[cell.cellIndex]); if (t) return t; }
      }
      for (let q = parentOf(e), k = 0; q && k < 3; q = parentOf(q), k++) { const s = txt(q); if (s && s.length <= 60) return s; }
      return e.value || "";
    };
    const isControl = (e) => {
      if (mine(e)) return false;
      if (e.tagName === "TEXTAREA" || e.tagName === "SELECT") return true;
      if (e.tagName !== "INPUT") return false;
      return /^(radio|checkbox|text|email|number|range|tel|url)$/.test((e.type || "text").toLowerCase());
    };
    const shown = (e) => visible(e) || visible(labelFor(e)) || visible(parentOf(e));
    const readQuestions = () => {
      const els = all(document, []).filter((e) => isControl(e) && !e.disabled && shown(e));
      const groups = [], byName = {};
      for (const e of els) {
        const t = (e.type || "").toLowerCase();
        if ((t === "radio" || t === "checkbox") && e.name) {
          const k = t + ":" + e.name;
          if (byName[k] === undefined) { byName[k] = groups.length; groups.push({ type: t, els: [] }); }
          groups[byName[k]].els.push(e);
        } else {
          groups.push({ type: e.tagName === "TEXTAREA" ? "textarea" : e.tagName === "SELECT" ? "select" :
                        (t === "range" ? "range" : (t === "radio" || t === "checkbox") ? t : "text"), els: [e] });
        }
      }
      const inGroup = new Map();
      groups.forEach((g, i) => g.els.forEach((e) => inGroup.set(e, i)));
      return groups.map((g, i) => {
        let box = g.els[0], up = parentOf(box);
        while (up) {
          const other = all(up, []).filter(isControl).some((x) => { const gi = inGroup.get(x); return gi !== undefined && gi !== i; });
          if (other) break;
          box = up; up = parentOf(up);
        }
        let options = [];
        if (g.type === "radio" || g.type === "checkbox") options = g.els.map(optionText);
        if (g.type === "select") options = Array.prototype.map.call(g.els[0].options, (o) => txt(o)).filter((o) => o && !/^(-+|select|chọn)/i.test(o));
        const choice = g.type === "radio" || g.type === "checkbox";
        const skip = choice ? g.els.map(labelFor).filter(Boolean).concat(g.els) : g.els.slice();
        const parts = [];
        (function walk(n) {
          if (n.nodeType === 3) { parts.push(n.nodeValue); return; }
          if (n.nodeType !== 1 || /^(SCRIPT|STYLE|SELECT|OPTION|TEXTAREA)$/.test(n.tagName)) return;
          if (skip.indexOf(n) >= 0 || mine(n)) return;
          const kids = n.shadowRoot ? n.shadowRoot.childNodes : n.childNodes;
          for (let k = 0; k < kids.length; k++) walk(kids[k]);
        })(box);
        let question = parts.join(" ").replace(/\s+/g, " ").trim();
        const e0 = g.els[0], named = [];
        (e0.getAttribute("aria-labelledby") || "").split(/\s+/).forEach((id) => {
          const r = e0.getRootNode ? e0.getRootNode() : document, n = id && r.getElementById ? r.getElementById(id) : null;
          if (n && !choice) named.push(txt(n));
        });
        if (!choice && labelFor(e0)) named.push(txt(labelFor(e0)));
        named.forEach((t) => { if (t && question.indexOf(t) < 0) question = t + " " + question; });
        question = question.replace(/^(.{12,}?)\s+\1(?=\s|$)/, "$1").trim();
        if (!question || question.length < 3) question = e0.getAttribute("aria-label") || e0.placeholder || e0.name || "(câu hỏi)";
        const qid = "q" + (i + 1);
        g.els.forEach((e) => e.setAttribute("data-lt-q", qid));
        return { qid, type: g.type, question: question.slice(0, 600), options: options.slice(0, 40) };
      });
    };
    const setValue = (e, v) => {
      const proto = e.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : e.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
      const d = Object.getOwnPropertyDescriptor(proto, "value");
      if (d && d.set) d.set.call(e, v); else e.value = v;
      e.dispatchEvent(new Event("input", { bubbles: true }));
      e.dispatchEvent(new Event("change", { bubbles: true }));
    };
    const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();
    const fill = (answers) => {
      if (!all(document, []).some((e) => e.getAttribute && e.getAttribute("data-lt-q"))) readQuestions();
      const els = all(document, []);
      let n = 0;
      for (const a of answers || []) {
        const mine2 = els.filter((e) => e.getAttribute && e.getAttribute("data-lt-q") === a.qid);
        if (!mine2.length) continue;
        const e0 = mine2[0], t = (e0.type || "").toLowerCase();
        if (t === "radio" || t === "checkbox") {
          for (const w of (t === "radio" ? [a.choice] : (a.choices || []))) {
            if (!w) continue;
            const pick = mine2.find((e) => norm(optionText(e)) === norm(w)) || mine2.find((e) => norm(optionText(e)).indexOf(norm(w)) >= 0);
            if (pick && !pick.checked) { pick.click(); if (!pick.checked) { const l = labelFor(pick); if (l) l.click(); } n++; }
            if (pick) (labelFor(pick) || pick).style.outline = "2px solid #16a34a";
          }
        } else if (e0.tagName === "SELECT") {
          const o = Array.prototype.find.call(e0.options, (x) => norm(txt(x)) === norm(a.choice));
          if (o) { setValue(e0, o.value); e0.style.outline = "2px solid #16a34a"; n++; }
        } else if (a.text) {
          setValue(e0, a.text); e0.style.outline = "2px solid #16a34a"; n++;
        }
      }
      return n;
    };
    addEventListener("message", (e) => {
      if (e.origin !== ORIGIN || !e.data || e.data.type !== "lt-fill") return;
      const n = fill(e.data.answers);
      say("✓ đã điền " + n + " câu (viền xanh). Kiểm tra, rồi tự bấm Next / Submit của trang.");
      btns.replaceChildren();
      button("📝 Đọc câu hỏi trang tiếp", () => survey(), "#2f6db3");
    });
    const survey = () => {
      const questions = readQuestions();
      if (!questions.length) { say("không thấy câu hỏi nào trên trang này."); return; }
      say("đã đọc " + questions.length + " câu hỏi — quay lại tab Live Translator: Claude soạn câu trả lời, bạn duyệt, rồi app điền vào đây.");
      btns.replaceChildren();
      send({ type: "lt-survey", v: VERSION, url: location.href, title: document.title, questions }, [], () => {
        btns.replaceChildren();
        button("📝 Đọc lại câu hỏi", () => survey(), "#2f6db3");
      });
    };
    /* ---------- course materials ---------- */
    const materials = async () => {
      if (A.busy) return;
      A.busy = true;
      try {
        if (isLogin()) { say("đây là trang đăng nhập — hãy đăng nhập, mở phần tài liệu rồi chạy lại dấu trang."); return; }
        let sc = scope(), list = items(sc).map(docOf).filter(Boolean);
        if (!list.length) { say("đang chờ trang tải xong..."); await settle(); sc = scope(); list = items(sc).map(docOf).filter(Boolean); }
        if (!list.length) {                    /* open the materials part: the saved clicks, else a "Materials" tab */
          let saved = [];
          try { saved = JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { saved = []; }
          for (const c of (saved.length ? saved : [{ text: "__auto__" }])) {
            let el = null;
            for (let i = 0; i < 12 && !el; i++) { el = find(c); if (!el) await sleep(500); }
            if (el) { say("đang mở “" + label(el) + "”..."); before = keys(items(null)); try { el.click(); } catch (e) { } await settle(); }
          }
          sc = scope();
        }
        let found = items(sc);
        if (!sc.panels.length && before) {     /* no standard tab panel: what the last tab click added */
          const added = found.filter((x) => !before[x.key]);
          if (added.length) found = added;
        } else if (!sc.panels.length) {        /* the whole page: this site's links and download addresses only */
          found = found.filter((x) => { try { return new URL(x.href).hostname === location.hostname || /\/sfc\/|servlet\.shepherd|\/download/i.test(x.href); } catch (e) { return false; } });
        }
        list = found.map(docOf).filter(Boolean);
        if (!list.length) { say("chưa thấy tài liệu. Hãy mở phần tài liệu (bấm tab Materials...) rồi chạy lại dấu trang — lần sau nó tự mở."); return; }
        if (A.clicks.length) { try { localStorage.setItem(KEY, JSON.stringify(A.clicks.slice(-3))); } catch (e) { } }
        const files = [], pages = [], fails = [];
        let k = 0;
        for (const l of list.slice(0, 120)) {
          say("đang tải " + (++k) + "/" + Math.min(list.length, 120) + "...");
          try {
            const r = await fetch(l.url, { credentials: "include" });
            if (!r.ok) { fails.push((l.text || l.url) + " (HTTP " + r.status + ")"); continue; }
            const type = (r.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
            const cd = r.headers.get("content-disposition") || "";
            if (type === "text/html" && !/filename/i.test(cd)) { pages.push(l.url); continue; }
            const m = /filename\*=(?:UTF-8'')?([^;]+)|filename="?([^";]+)"?/i.exec(cd);
            let name = m ? (m[1] || m[2]) : (l.dl || "");
            if (!name) { try { name = new URL(r.url).pathname.split("/").pop(); } catch (e) { name = ""; } }
            try { name = decodeURIComponent(name); } catch (e) { }
            const buf = await r.arrayBuffer();
            if (buf.byteLength > 200 * 1048576) { fails.push((l.text || name) + " (quá lớn)"); continue; }
            files.push({ url: l.url, name: String(name || "").trim(), text: l.text, type, buf });
          } catch (e) { fails.push((l.text || l.url) + " (trang không cho tải file này từ đây — tải tay rồi dùng 📁 Folder)"); }
        }
        if (!files.length) { say("không tải được tài liệu nào" + (fails.length ? ": " + fails.slice(0, 2).join("; ") : "") + "."); return; }
        say("đã tải " + files.length + " tài liệu.");
        send({ type: "lt-files", v: VERSION, url: location.href, title: document.title, tab: sc.tab, files, pages, fails },
             files.map((f) => f.buf), (opener) => say("đã gửi " + files.length + " tài liệu" +
               (opener ? ". Quay lại tab Live Translator để thêm vào thư viện." : " cho app.")));
      } finally {
        A.busy = false;
      }
    };
    /* ---------- what to do on this page ---------- */
    A.run = async (mode) => {
      bar.style.display = "";
      btns.replaceChildren();
      if (isLogin()) { say("đây là trang đăng nhập — hãy đăng nhập, rồi chạy lại dấu trang."); return; }
      if (mode === "survey") {                 /* the Feedback bookmark (ltfeedback): always the survey */
        if (readQuestions().length) { survey(); return; }
        say("đang chờ trang khảo sát tải xong...");
        await settle();
        if (readQuestions().length) survey();
        else say("không thấy câu hỏi nào trên trang này — mở trang khảo sát (sau khi đăng nhập nếu cần) rồi chạy lại dấu trang ltfeedback.");
        return;
      }
      const qs = readQuestions().length, docs = items(scope()).map(docOf).filter(Boolean).length;
      if (qs && docs) {                        /* both: the user says which */
        say("trang này có " + qs + " câu hỏi và " + docs + " tài liệu:");
        button("📝 Điền khảo sát", () => survey(), "#2f6db3");
        button("📥 Tải tài liệu", () => materials());
      } else if (qs >= 2) survey();
      else materials();
    };
    A.run(MODE);
  };
  /* eslint-enable */
  // one line, comments dropped (shorter to paste into a bookmark)
  const code = fn.toString().replace(/\/\*[^*]*\*\//g, "").replace(/\n\s*/g, " ").replace(/ {2,}/g, " ");
  if (code.includes("%")) throw new Error("bookmarklet must not contain a percent sign");
  return `javascript:(${code})(${JSON.stringify(app)},${JSON.stringify(mode)});void 0`;
}

/** The bookmark of each place in the app: "ltsource" (materials, also surveys) and "ltfeedback" (surveys). */
export const BOOKMARKS = {
  source: { name: "ltsource", mode: "", title: "📥 Cài dấu trang Live Translator (Source)" },
  feedback: { name: "ltfeedback", mode: "survey", title: "📝 Cài dấu trang Feedback (ltfeedback)" },
};

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
    if (d && d.type === "lt-survey" && Array.isArray(d.questions)) {     // a survey page (Learning → Feedback)
      await this.app.learning.ready;
      if (e.origin === location.origin && /\/bm-test(-feedback)?\.html/.test(String(d.url || ""))) {   // the test form: it works
        this.app.learning.bookmarkWorks(d.questions.length, "feedback");
        return;
      }
      this.app.learning.feedback.receive(e.source, e.origin, d);
      return;
    }
    if (!d || d.type !== "lt-files" || !Array.isArray(d.files)) return;
    const L = this.app.learning;
    await L.ready;
    // the test page (Learning → Source → Dấu trang → Thử): only say that the bookmark works in this browser
    if (e.origin === location.origin && /\/bm-test\.html(\?|#|$)/.test(String(d.url || ""))) {
      L.bookmarkWorks(d.files.length);
      return;
    }
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
    if ((d.v || 1) < BOOKMARK_VERSION) {
      L.chatNote("📥 Có bản dấu trang mới (tự mở tab Materials, đọc được trang Salesforce): vào Learning → 🌐 Source → " +
                 "📥 Cập nhật dấu trang, bấm Copy mã rồi dán đè vào dấu trang “📥 Live Translator” cũ.");
    }
    const ok = await L.confirmSource(host, d.title || d.url, fresh.map((f) => L.sourceName(f)), already,
                                     files.length - usable.length);
    if (!ok) return;
    L.importSource(d, fresh);
  }
}

/** Version of the bookmark's code (the bookmark sends it; an older one is asked to be replaced). */
export const BOOKMARK_VERSION = 3;
