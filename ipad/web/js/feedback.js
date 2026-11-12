// 📝 Learning → Feedback: fill a course survey (reached through the QR code of a day) with positive, specific
// feedback that Claude drafts from the class, after the user has reviewed and edited every answer (Windows 1.9.17).
//
// 1. The user picks the QR image (photo / screenshot / slide PDF) of the day's survey, or pastes its link.
// 2. The survey opens in a new tab of this browser; the user runs the bookmark "ltsource" there (the same one as
//    Source): it reads the page's questions and sends them here.
// 3. Claude drafts the answers from the questions, the day's documents and the class record (high ratings).
// 4. A dialog shows every answer to review / edit; "Điền vào form" sends them back and the bookmark fills them in.
// 5. The user checks the page and presses the survey's own Next / Submit (the app never sends it).

import { askJson, friendlyError } from "./claude.js";

const ROOT = new URL("../../../", import.meta.url).href;
const PDFJS = ROOT + "lib/pdfjs-4.10.38/";

const DRAFT_SYSTEM = (
  "You write course feedback for a learner who attended this course and liked it. You get one page of a " +
  "survey (questions, their type and the exact answer options) and what the course covered (that day's " +
  "documents, key takeaways, the class transcript). Answer every question as this learner:\n" +
  "- rating / scale / agreement / recommendation questions: choose the most positive option (e.g. 5 of 5, " +
  "Strongly agree, Excellent, 9-10 for 'how likely to recommend'); copy the option text EXACTLY;\n" +
  "- yes/no about satisfaction or usefulness: the positive one;\n" +
  "- open questions (what was most valuable, comments, suggestions): 1-3 warm, specific sentences that name " +
  "real topics, activities or speakers from the course material; suggestions are constructive and kind;\n" +
  "- write in the language of the survey (English if the questions are in English);\n" +
  "- personal facts you cannot know (name, email, company, ID, role) or consent questions: leave empty.\n" +
  "Return JSON only.");
const DRAFT_SCHEMA = {
  type: "object", additionalProperties: false, required: ["answers"],
  properties: { answers: { type: "array", items: {
    type: "object", additionalProperties: false, required: ["qid", "choice", "choices", "text"],
    properties: { qid: { type: "string" }, choice: { type: "string" },
                  choices: { type: "array", items: { type: "string" } }, text: { type: "string" } } } } } };
const LINKS_SCHEMA = { type: "object", additionalProperties: false, required: ["urls"],
  properties: { urls: { type: "array", items: { type: "string" } } } };

const el = (tag, cls = "", text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

/** http(s) links, and short links printed without http (bit.ly/..., forms.gle/..., mit.edu/...). */
export function urlsInText(text) {
  const re = /(https?:\/\/[^\s<>"')\]]+|\b(?:[a-z0-9-]+\.)+(?:com|org|edu|ly|gle|io|co|me|net|us)\/[^\s<>"')\]]+)/gi;
  const out = [];
  for (const m of String(text || "").matchAll(re)) {
    let u = m[0].replace(/[.,;:]+$/, "");
    if (!/^https?:/i.test(u)) u = "https://" + u;
    if (!out.includes(u)) out.push(u);
  }
  return out;
}

let pdfjsP = null;
const pdfjs = () => (pdfjsP ||= import(PDFJS + "pdf.min.mjs").then((m) => {
  m.GlobalWorkerOptions.workerSrc = PDFJS + "pdf.worker.min.mjs";
  return m;
}));

/** Pictures to look for QR codes in: the image itself, or the pages of a PDF (drawn at 2x). */
async function picturesOf(file) {
  if (/pdf$/i.test(file.type) || /\.pdf$/i.test(file.name)) {
    const lib = await pdfjs();
    const pdf = await lib.getDocument({ data: new Uint8Array(await file.arrayBuffer()), isEvalSupported: false }).promise;
    const out = [], links = [];
    for (let i = 1; i <= Math.min(pdf.numPages, 40); i++) {
      const page = await pdf.getPage(i);
      const v = page.getViewport({ scale: 2 });
      const c = document.createElement("canvas");
      c.width = Math.floor(v.width);
      c.height = Math.floor(v.height);
      await page.render({ canvasContext: c.getContext("2d"), viewport: v }).promise;
      out.push(c);
      try {                                          // link annotations and links printed on the page
        for (const a of await page.getAnnotations()) if (/^https?:/i.test(a.url || "")) links.push(a.url);
        const t = await page.getTextContent();
        links.push(...urlsInText(t.items.map((x) => x.str).join(" ")));
      } catch { /* */ }
    }
    try { pdf.destroy(); } catch { /* */ }
    return { pictures: out, links };
  }
  return { pictures: [await createImageBitmap(file)], links: [] };
}

/** Links in QR codes (the browser's own reader, where it has one). */
async function qrLinks(pictures) {
  if (!("BarcodeDetector" in window)) return null;
  let det;
  try { det = new window.BarcodeDetector({ formats: ["qr_code"] }); } catch { return null; }
  const out = [];
  for (const p of pictures) {
    try { for (const r of await det.detect(p)) if (r.rawValue && !out.includes(r.rawValue.trim())) out.push(r.rawValue.trim()); } catch { /* */ }
  }
  return out;
}

/** A picture as a small PNG block for Claude (a link printed next to the QR code, read as text). */
async function imageBlock(pic) {
  const scale = Math.min(1, 1600 / Math.max(pic.width, pic.height));
  const c = document.createElement("canvas");
  c.width = Math.round(pic.width * scale);
  c.height = Math.round(pic.height * scale);
  c.getContext("2d").drawImage(pic, 0, 0, c.width, c.height);
  const data = c.toDataURL("image/png").split(",")[1];
  return { type: "image", source: { type: "base64", media_type: "image/png", data } };
}

export class Feedback {
  constructor(learning) {
    this.L = learning;
    this.app = learning.app;
    this.session = null;            // {url, day}: the survey opened from here
  }

  /** Menu entry: the survey from a QR image / slide PDF, or a link; then open it. */
  async open() {
    await this.L.ready;
    const body = el("div", "lform fbPanel");
    body.append(el("p", "", "Khảo sát của buổi học: chọn ảnh chụp / ảnh màn hình / slide PDF có mã QR, hoặc dán link khảo sát."));
    const pickRow = el("div", "actions");
    const pick = el("button", "", "🖼 Chọn ảnh / PDF có mã QR");
    const input = el("input");
    input.type = "file";
    input.accept = "image/*,application/pdf";
    input.hidden = true;
    const found = el("p", "hint", "Mẹo: có thể mở app Camera, quét mã QR, copy link rồi dán vào ô dưới.");
    pickRow.append(pick, found);
    const url = el("input", "search");
    url.type = "url";
    url.placeholder = "https://... (link khảo sát)";
    url.autocapitalize = "off";
    url.spellcheck = false;
    if (this.session?.url) url.value = this.session.url;
    const list = el("select", "fbLinks");
    list.hidden = true;
    list.onchange = () => { url.value = list.value; };
    const daySel = el("select", "fbDay");
    daySel.append(new Option("Cả khóa / không rõ buổi", "0"));
    for (const [day, date] of this.L.lib.days()) daySel.append(new Option(`Buổi ${day}${date ? ` (${date})` : ""}`, String(day)));
    const days = this.L.lib.days();
    if (this.session?.day !== undefined) daySel.value = String(this.session.day);
    else if (days.length) daySel.value = String(days[days.length - 1][0]);
    const use = (urls, where) => {
      const links = [...new Set(urls.filter((u) => /^https?:\/\//i.test(u)))];
      if (!links.length) { found.textContent = `Không thấy mã QR hay link nào trong ${where}.`; return; }
      url.value = links[0];
      list.replaceChildren(...links.map((u) => new Option(u, u)));
      list.hidden = links.length < 2;
      found.textContent = `✓ ${links.length} link từ ${where}${links.length > 1 ? " — chọn đúng link trong danh sách." : "."}`;
    };
    pick.onclick = () => input.click();
    input.onchange = async () => {
      const f = input.files[0];
      if (!f) return;
      found.textContent = "Đang tìm mã QR...";
      try {
        const { pictures, links } = await picturesOf(f);
        const qr = await qrLinks(pictures);
        if (qr?.length) return use([...qr, ...links], f.name);
        if (links.length) return use(links, f.name);
        // no QR reader here, or no QR: the link may be printed next to it - Claude reads it
        found.textContent = qr === null ? "Trình duyệt này không đọc được mã QR — Claude đang đọc link in trong ảnh..."
                                        : "Không có mã QR — Claude đang đọc link in trong ảnh...";
        const got = [];
        for (const p of pictures.slice(0, 3)) {
          const res = await askJson(this.app.apiKey, "Read every web link (URL) printed in the image, exactly as written " +
            "(short links like bit.ly/abc or forms.gle/xyz too). JSON only.",
            [await imageBlock(p), { type: "text", text: "Links in this image?" }], LINKS_SCHEMA, { effort: "low", maxTokens: 1000 });
          got.push(...urlsInText((res?.urls || []).join(" ")));
        }
        if (got.length) use(got, f.name);
        else found.textContent = "Không đọc được link. Hãy quét mã QR bằng app Camera, copy link rồi dán vào ô dưới.";
      } catch (e) {
        found.textContent = `Không đọc được file: ${friendlyError(e)}`;
      }
    };
    const dayRow = el("label", "fbDayRow");
    dayRow.append(el("span", "", "Khảo sát của buổi:"), daySel);
    const note = el("p", "hint", "Sau khi mở: ở trang khảo sát hãy chạy dấu trang ltsource (Chrome: gõ ltsource vào thanh địa chỉ, " +
      "chạm gợi ý ☆). Nó đọc câu hỏi và gửi về đây; Claude soạn câu trả lời tích cực, cụ thể; bạn duyệt / sửa từng câu; " +
      "app điền vào form. Bạn tự bấm Next / Submit của trang — app không bao giờ tự gửi.");
    const row = el("div", "actions");
    const cancel = el("button", "", "Đóng");
    cancel.onclick = () => this.app.closeSheet();
    const go = el("button", "primary", "📝 Mở khảo sát");
    go.onclick = () => {
      const u = url.value.trim();
      if (!/^https?:\/\/\S+$/i.test(u)) { found.textContent = "Hãy chọn ảnh có mã QR hoặc dán link bắt đầu bằng http(s)://"; return; }
      this.session = { url: u, day: +daySel.value || 0 };
      window.open(u, "_blank");              // not noopener: the bookmark sends the questions straight back
      this.app.closeSheet();
      this.app.setStatus("📝 Ở trang khảo sát, chạy dấu trang ltsource để đọc câu hỏi.", "ok");
    };
    row.append(el("span", "spacer"), cancel, go);
    body.append(pickRow, input, url, list, dayRow, note, row);
    this.app.sheet("📝 Feedback — khảo sát buổi học", body, true);
  }

  /** What the survey answers are based on: the day's documents, the key takeaways and the transcript end. */
  async courseContext(day) {
    const lib = this.L.lib, parts = [];
    let total = 0;
    for (const d of lib.docs().filter((x) => !day || [day, 0].includes(x.day || 0)).slice(0, 25)) {
      let body = String((await lib.text(d)) || "").slice(0, 2500);
      total += body.length;
      if (total > 40000) body = "";
      parts.push(`<document day="${d.day || ""}" title="${d.title || d.name}">\n${d.description || ""}\n${body}\n</document>`);
    }
    const take = (this.app.lastSummary || "").slice(-12000), en = this.app.transcriptText().slice(-12000);
    return `<course_documents>\n${parts.join("\n")}\n</course_documents>\n` +
      `<key_takeaways>\n${take || "(none)"}\n</key_takeaways>\n<transcript_end>\n${en || "(none)"}\n</transcript_end>`;
  }

  /** The bookmark sent one survey page: draft, review, then send the answers back to be filled in. */
  async receive(source, origin, page) {
    const qs = (page.questions || []).filter((q) => q && q.qid);
    if (!qs.length) return;
    let host = origin;
    try { host = new URL(page.url).host; } catch { /* */ }
    if (!this.session && !confirm(`Nhận câu hỏi khảo sát từ ${host} để Claude soạn câu trả lời?`)) return;
    const day = this.session?.day || 0;
    this.app.busy("feedback", `Claude đang soạn câu trả lời cho ${qs.length} câu hỏi khảo sát...`);
    let answers = {};
    try {
      const prompt = (await this.courseContext(day)) + `\n\n<survey_page title="${(page.title || "").replace(/"/g, "'")}">\n` +
        JSON.stringify(qs, null, 1) + "\n</survey_page>";
      const res = await askJson(this.app.apiKey, DRAFT_SYSTEM, [{ type: "text", text: prompt }], DRAFT_SCHEMA,
                                { effort: "low", maxTokens: 8000 });
      for (const a of res?.answers || []) answers[a.qid] = a;
    } catch (e) {
      this.app.setStatus(`📝 Không soạn được câu trả lời: ${friendlyError(e)}`, "err");
      answers = {};
    } finally {
      this.app.done("feedback");
    }
    this.review(source, origin, page, qs, answers);
  }

  /** Every answer to review / edit; "Điền vào form" sends them to the survey tab. */
  review(source, origin, page, qs, answers) {
    const body = el("div", "lform fbPanel");
    body.append(el("p", "hint", `${qs.length} câu hỏi trên trang “${page.title || page.url}”. Sửa nếu cần, rồi bấm Điền vào form.`));
    const getters = [];
    for (const q of qs) {
      const a = answers[q.qid] || {};
      const box = el("div", "fbQ");
      box.append(el("div", "fbQText", q.question));
      let get;
      if ((q.type === "radio" || q.type === "select") && q.options?.length) {
        const s = el("select");
        s.append(new Option("(để trống)", ""));
        for (const o of q.options) s.append(new Option(o, o));
        s.value = q.options.includes(a.choice) ? a.choice : "";
        box.append(s);
        get = () => ({ qid: q.qid, choice: s.value, choices: [], text: "" });
      } else if (q.type === "checkbox" && q.options?.length) {
        const want = new Set(a.choices || []), boxes = [];
        const wrap = el("div", "fbChecks");
        for (const o of q.options) {
          const l = el("label"), c = el("input");
          c.type = "checkbox";
          c.checked = want.has(o);
          l.append(c, " " + o);
          wrap.append(l);
          boxes.push([c, o]);
        }
        box.append(wrap);
        get = () => ({ qid: q.qid, choice: "", choices: boxes.filter(([c]) => c.checked).map(([, o]) => o), text: "" });
      } else {
        const t = el("textarea");
        t.rows = q.type === "textarea" ? 3 : 1;
        t.value = a.text || a.choice || "";
        box.append(t);
        get = () => ({ qid: q.qid, choice: "", choices: [], text: t.value.trim() });
      }
      getters.push(get);
      body.append(box);
    }
    const row = el("div", "actions");
    const cancel = el("button", "", "Huỷ");
    cancel.onclick = () => this.app.closeSheet();
    const ok = el("button", "primary", "✍️ Điền vào form");
    ok.onclick = () => {
      const filled = getters.map((g) => g());
      try {
        source.postMessage({ type: "lt-fill", answers: filled }, origin);
        this.app.setStatus("📝 Đã gửi câu trả lời — quay lại tab khảo sát, kiểm tra rồi tự bấm Next / Submit.", "ok");
      } catch (e) {
        this.app.setStatus(`📝 Tab khảo sát đã đóng (${e.message || e}) — mở lại và chạy dấu trang.`, "err");
      }
      this.app.closeSheet();
    };
    row.append(el("span", "spacer"), cancel, ok);
    body.append(row);
    this.app.sheet("📝 Duyệt câu trả lời khảo sát", body, true);
  }
}
