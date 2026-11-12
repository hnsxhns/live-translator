"""Learning > Feedback: fill a course survey (reached through the QR code of a day) with positive, specific
feedback that Claude drafts from the class, after the user has reviewed and edited every answer.

1. The user uploads (or pastes) the QR image of the day's survey, or pastes its link.
2. The survey opens in a browser window of the app (same login as Source) which reads each page's questions.
3. Claude drafts the answers from the questions, the day's documents and the class record (high ratings).
4. A dialog shows every answer to review / edit; "Điền vào form" fills them in on the page.
5. The user checks the page and presses the survey's own Next / Submit (the app never sends it).
"""
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

A = None     # the app module (set by learning.setup through start())
L = None     # the Learning object


def qr_urls(image):
    """Links in the QR code(s) of a PIL image."""
    import zxingcpp
    img = image.convert("RGB")
    out = []
    for scale in (1, 2):                       # small QR in a big screenshot: try it larger too
        im = img if scale == 1 else img.resize((img.width * 2, img.height * 2))
        for r in zxingcpp.read_barcodes(im):
            t = (r.text or "").strip()
            if t and t not in out:
                out.append(t)
        if out:
            break
    return out


DRAFT_SYSTEM = (
    "You write course feedback for a learner who attended this course and liked it. You get one page of a "
    "survey (questions, their type and the exact answer options) and what the course covered (that day's "
    "documents, key takeaways, the class transcript). Answer every question as this learner:\n"
    "- rating / scale / agreement / recommendation questions: choose the most positive option (e.g. 5 of 5, "
    "Strongly agree, Excellent, 9-10 for 'how likely to recommend'); copy the option text EXACTLY;\n"
    "- yes/no about satisfaction or usefulness: the positive one;\n"
    "- open questions (what was most valuable, comments, suggestions): 1-3 warm, specific sentences that name "
    "real topics, activities or speakers from the course material; suggestions are constructive and kind;\n"
    "- write in the language of the survey (English if the questions are in English);\n"
    "- personal facts you cannot know (name, email, company, ID, role) or consent questions: leave empty.\n"
    "Return JSON only.")
DRAFT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["answers"],
    "properties": {"answers": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["qid", "choice", "choices", "text"],
        "properties": {"qid": {"type": "string"}, "choice": {"type": "string"},
                       "choices": {"type": "array", "items": {"type": "string"}}, "text": {"type": "string"}}}}}}


def course_context(day):
    """What the survey answers are based on: the day's documents (titles, descriptions, start of the text),
    the key takeaways and the end of the transcript."""
    lib = L.lib
    docs = [d for d in lib.docs() if not day or d.get("day") in (day, 0)]
    parts, total = [], 0
    for d in docs[:25]:
        body = (lib.text(d) or "")[:2500]
        total += len(body)
        if total > 40000:
            body = ""
        parts.append(f"<document day=\"{d.get('day') or ''}\" title=\"{d.get('title') or d['name']}\">\n"
                     f"{d.get('description', '')}\n{body}\n</document>")
    mw = L.mw
    take = (mw.last_summary or "")[-12000:]
    en = mw.transcript_text()[-12000:] if hasattr(mw, "transcript_text") else ""
    return ("<course_documents>\n" + "\n".join(parts) + "\n</course_documents>\n"
            f"<key_takeaways>\n{take or '(none)'}\n</key_takeaways>\n"
            f"<transcript_end>\n{en or '(none)'}\n</transcript_end>")


def draft(page, day):
    """Claude's answers for one survey page -> {qid: answer}."""
    import learning
    qs = page.get("questions") or []
    prompt = (course_context(day) + "\n\n<survey_page title=\"" + (page.get("title") or "") + "\">\n" +
              json.dumps(qs, ensure_ascii=False, indent=1) + "\n</survey_page>")
    res = learning.ask_json(L.mw.client, DRAFT_SYSTEM, prompt, DRAFT_SCHEMA, effort="low", max_tokens=8000)
    return {a.get("qid"): a for a in (res or {}).get("answers", [])}


# ---------------------------------------------------------------- finding the survey link
URL_RE = None


def urls_in_text(text):
    """http(s) links, and short links printed without http (bit.ly/..., forms.gle/..., mit.edu/...)."""
    import re
    global URL_RE
    if URL_RE is None:
        URL_RE = re.compile(r"(https?://[^\s<>\"')\]]+|\b(?:[a-z0-9-]+\.)+(?:com|org|edu|ly|gle|io|co|me|net|us)"
                            r"/[^\s<>\"')\]]+)", re.I)
    out = []
    for m in URL_RE.findall(text or ""):
        u = m.rstrip(".,;:")
        if not u.lower().startswith("http"):
            u = "https://" + u
        if u not in out:
            out.append(u)
    return out


def urls_in_pdf(path, max_pages=60):
    """QR codes on the pages, then link annotations and links printed in the text."""
    import fitz
    from PIL import Image
    qr, other = [], []
    doc = fitz.open(path)
    for page in list(doc)[:max_pages]:
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        qr += [u for u in qr_urls(img) if u not in qr]
        for l in page.get_links():
            u = l.get("uri") or ""
            if u.startswith("http") and u not in other:
                other.append(u)
        other += [u for u in urls_in_text(page.get_text()) if u not in other]
    return qr + [u for u in other if u not in qr]


def urls_printed_in_image(img):
    """A link printed as text in the picture (e.g. next to the QR code), read by Claude."""
    import base64
    import io
    import learning
    buf = io.BytesIO()
    im = img.convert("RGB")
    im.thumbnail((1600, 1600))
    im.save(buf, "PNG")
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(buf.getvalue()).decode()}}
    res = learning.ask_json(L.mw.client, "Read every web link (URL) printed in the image, exactly as written "
                            "(short links like bit.ly/abc or forms.gle/xyz too). JSON only.",
                            [block, {"type": "text", "text": "Links in this image?"}],
                            {"type": "object", "additionalProperties": False, "required": ["urls"],
                             "properties": {"urls": {"type": "array", "items": {"type": "string"}}}},
                            effort="low", max_tokens=1000)
    return urls_in_text(" ".join((res or {}).get("urls", [])))


# ---------------------------------------------------------------- the user's choice of survey
def start(learning_obj):
    """Menu entry: the survey from a QR image (file / clipboard), a slide PDF, or a link; then open it."""
    global L, A
    L = learning_obj
    A = sys.modules["learning"].A
    top = L._dialog("Feedback — khảo sát buổi học", "680x380")
    tk.Label(top, text="Khảo sát của buổi học: chọn ảnh/slide có mã QR hoặc link, dán ảnh/link, hoặc gõ link:",
             font=(A.UI_FONT, 10, "bold"), bg="white").pack(anchor="w", padx=14, pady=(14, 4))
    url = tk.StringVar()
    found = tk.StringVar(value="")
    row = tk.Frame(top, bg="white")
    row.pack(fill="x", padx=14, pady=4)
    st = dict(font=(A.UI_FONT, 10), relief="flat", cursor="hand2", padx=10, pady=3, bg="#eef2ff")
    links = []

    def use(urls, where):
        new = [u for u in urls if u.lower().startswith(("http://", "https://")) and u not in links]
        links.extend(new)
        box["values"] = links
        if not links:
            found.set(f"Không thấy mã QR hay link nào trong {where}.")
            return
        if new:
            url.set(new[0])
        found.set(f"✓ {len(new)} link từ {where}" + (" — chọn link đúng trong danh sách bên dưới." if len(links) > 1
                                                     else "."))

    def from_images(images, where):
        """QR codes first; a picture with no QR may show the link as text: Claude reads it."""
        urls = []
        for img in images:
            urls += qr_urls(img)
        if urls:
            return use(urls, where)
        found.set("Không có mã QR — Claude đang đọc link in trong ảnh...")

        def work():
            got, err = [], None
            try:
                for img in images:
                    got += urls_printed_in_image(img)
            except Exception as e:  # noqa: BLE001
                err = A.friendly_error(e) if hasattr(A, "friendly_error") else str(e)
            top.after(0, lambda: found.set(f"Không đọc được link trong ảnh: {err}") if err else use(got, where))
        threading.Thread(target=work, daemon=True).start()

    def from_files(paths, where):
        from PIL import Image
        images, urls = [], []
        for p in paths:
            try:
                if p.lower().endswith(".pdf"):
                    urls += urls_in_pdf(p)
                else:
                    images.append(Image.open(p))
            except Exception as e:  # noqa: BLE001
                found.set(f"Không mở được {os.path.basename(p)}: {e}")
        if urls:
            use(urls, where)
        if images:
            from_images(images, where)

    def pick():
        paths = filedialog.askopenfilenames(parent=top, title="Chọn ảnh mã QR hoặc slide PDF",
                                            filetypes=[("Ảnh hoặc PDF", "*.png *.jpg *.jpeg *.gif *.bmp *.webp *.pdf"),
                                                       ("Tất cả", "*.*")])
        if paths:
            from_files(paths, "file đã chọn")

    def paste():
        try:
            from PIL import ImageGrab
            clip = ImageGrab.grabclipboard()
        except Exception:  # noqa: BLE001
            clip = None
        if isinstance(clip, list):                  # copied files
            return from_files(clip, "file trong clipboard")
        if clip is not None:
            return from_images([clip], "ảnh trong clipboard")
        try:
            text = top.clipboard_get()
        except tk.TclError:
            text = ""
        if text.strip():
            return use(urls_in_text(text), "chữ trong clipboard")
        found.set("Clipboard trống — chụp màn hình mã QR / link (Win+Shift+S) hoặc copy link, rồi bấm lại.")

    tk.Button(row, text="📷 Chọn ảnh QR / slide PDF...", command=pick, **st).pack(side="left")
    tk.Button(row, text="📋 Dán ảnh hoặc link (clipboard)", command=paste, **st).pack(side="left", padx=8)
    tk.Label(top, textvariable=found, bg="white", fg="#16a34a", font=(A.UI_FONT, 9), wraplength=640,
             justify="left").pack(anchor="w", padx=14, pady=(2, 6))
    tk.Label(top, text="Link khảo sát (chọn trong danh sách, hoặc gõ / dán vào đây):", bg="white",
             font=(A.UI_FONT, 9)).pack(anchor="w", padx=14)
    box = ttk.Combobox(top, textvariable=url, values=links, font=(A.UI_FONT, 10))
    box.pack(fill="x", padx=14, pady=(2, 8))
    days = sorted({d.get("day") for d in L.lib.docs() if d.get("day")})
    tk.Label(top, text="Khảo sát của buổi (để Claude dựa vào tài liệu buổi đó):", bg="white",
             font=(A.UI_FONT, 9)).pack(anchor="w", padx=14)
    choices = ["Cả khóa"] + [f"Buổi {d}" for d in days]
    day_var = tk.StringVar(value=choices[-1] if days else choices[0])
    ttk.Combobox(top, textvariable=day_var, values=choices, state="readonly", width=20).pack(anchor="w", padx=14)

    def ok():
        typed = url.get().strip()
        u = typed if typed.lower().startswith(("http://", "https://")) else \
            (urls_in_text(typed) or [""])[0]
        if not u:
            messagebox.showinfo("Feedback", "Hãy chọn/dán ảnh mã QR, slide, hoặc dán link khảo sát.", parent=top)
            return
        d = day_var.get()
        top.destroy()
        open_survey(u, int(d.split()[-1]) if d.startswith("Buổi") else 0)
    L._buttons(top, ok, ok="Mở khảo sát")


# ---------------------------------------------------------------- the survey window and the review loop
def open_survey(url, day):
    import learning
    io_dir = os.path.join(A.DATA_DIR, "learning", f"feedback_{int(time.time())}")
    os.makedirs(io_dir, exist_ok=True)
    profile = learning.source_profile()                        # the same login as the Source window
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--feedback-browser", url, io_dir, profile]
    else:
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(A.__file__)), "app.py"),
               "--feedback-browser", url, io_dir, profile]
    L.chat_note("Đã mở khảo sát trong cửa sổ Feedback. Mỗi trang câu hỏi: Claude soạn câu trả lời (điểm tích cực "
                "cao, nội dung theo buổi học) → bạn duyệt/sửa trong hộp thoại → app điền vào trang → bạn kiểm tra và "
                "tự bấm Next/Submit của khảo sát.")

    def work():
        if not learning.wait_profile_free(profile):
            L.later(lambda: L.chat_note("Cửa sổ Source/Feedback trước vẫn đang mở — hãy đóng nó rồi thử lại."))
            return
        proc = subprocess.Popen(cmd)
        seen = set()
        answered = {}       # page (its questions) -> the answers the user approved: refilled if the page reloads
        waiting = {}        # page -> answer files to write when the open review dialog is closed
        while True:
            for name in sorted(os.listdir(io_dir), key=lambda x: (len(x), x)):
                if not (name.startswith("q_") and name.endswith(".json")) or name in seen:
                    continue
                seen.add(name)
                n = name[2:-5]
                try:
                    page = json.load(open(os.path.join(io_dir, name), encoding="utf-8"))
                except ValueError:
                    seen.discard(name)
                    continue
                out = os.path.join(io_dir, f"a_{n}.json")
                sig = json.dumps([q.get("question") for q in page.get("questions") or []], ensure_ascii=False)
                if sig in answered:                    # the same page again (it reloaded): fill it again
                    write_answers([out], answered[sig])
                    continue
                if sig in waiting:                     # its dialog is already open: one dialog per page
                    waiting[sig].append(out)
                    continue
                waiting[sig] = [out]
                L.later(lambda: L.mw.busy("feedback", "Claude đang soạn câu trả lời khảo sát..."))
                try:
                    ans = draft(page, day)
                    err = None
                except Exception as e:  # noqa: BLE001
                    ans, err = {}, A.friendly_error(e) if hasattr(A, "friendly_error") else str(e)
                ans = {**positive_defaults(page.get("questions") or []), **ans}   # ratings filled even without Claude

                def approved(answers, s=sig):
                    answered[s] = answers
                    write_answers(waiting.pop(s, []), answers)
                L.later(lambda p=page, a=ans, e=err, cb=approved: (L.mw.done("feedback"), review(p, a, cb, e)))
            if proc.poll() is not None:
                L.later(lambda: (L.mw.done("feedback"), L.chat_note("Đã đóng cửa sổ Feedback.")))
                return
            time.sleep(0.5)
    threading.Thread(target=work, daemon=True).start()


def write_answers(paths, answers):
    for path in paths:
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(answers, f, ensure_ascii=False)
        os.replace(tmp, path)


POSITIVE = [(r"\bexcellent\b|\bextremely (likely|satisfied|useful|valuable)\b|\bstrongly agree\b|\boutstanding\b", 100),
            (r"\bvery (good|likely|satisfied|useful|valuable|well)\b|\bagree\b(?!.*dis)", 80),
            (r"\bgood\b|\bsatisfied\b|\blikely\b|\buseful\b|\byes\b", 60)]


def positive_defaults(questions):
    """Without Claude: the most positive option of every rating / agreement question (highest number, or
    the words like Excellent / Strongly agree); text fields stay empty for the user (or Claude)."""
    import re
    out = {}
    for q in questions:
        opts = q.get("options") or []
        if q.get("type") not in ("radio", "select") or not opts:
            continue
        def score(o):
            low = o.lower()
            if re.search(r"\b(not|dis|poor|un|never|no)\b|disagree|unlikely|dissatisfied", low):
                return -1
            for pat, s in POSITIVE:
                if re.search(pat, low):
                    return s
            m = re.match(r"\s*(\d+)", o)
            return int(m.group(1)) if m else 0
        best = max(opts, key=lambda o: (score(o), opts.index(o)))
        if score(best) > 0 or all(re.match(r"\s*\d+", o) for o in opts):
            out[q["qid"]] = {"qid": q["qid"], "choice": best, "choices": [], "text": ""}
    return out


def review(page, drafted, on_done, error=None):
    """Every question with Claude's answer, editable; "Điền vào form" writes the approved answers."""
    qs = page.get("questions") or []
    top = L._dialog("Feedback — duyệt câu trả lời trước khi điền", "820x660", modal=False)
    head = tk.Frame(top, bg="white")
    head.pack(fill="x", padx=14, pady=(10, 4))
    tk.Label(head, text=(page.get("title") or "Khảo sát") + f" — {len(qs)} câu hỏi",
             font=(A.UI_FONT, 11, "bold"), bg="white").pack(anchor="w")
    tk.Label(head, text=("⚠ Claude chưa soạn được: " + error + " — bạn có thể tự điền.") if error else
             "Claude đã soạn sẵn (điểm tích cực cao, theo nội dung buổi học). Sửa bất cứ câu nào, rồi bấm "
             "“Điền vào form”. App không tự gửi: bạn bấm Next/Submit trên trang.",
             fg="#b45309" if error else "#555", bg="white", font=(A.UI_FONT, 9), wraplength=780,
             justify="left").pack(anchor="w")
    wrap = tk.Frame(top, bg="white")
    wrap.pack(fill="both", expand=True, padx=8)
    canvas = tk.Canvas(wrap, bg="white", highlightthickness=0)
    sb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
    body = tk.Frame(canvas, bg="white")
    body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=body, anchor="nw", width=780)
    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
    getters = {}
    for i, q in enumerate(qs, 1):
        a = drafted.get(q["qid"], {})
        box = tk.Frame(body, bg="white")
        box.pack(fill="x", padx=6, pady=(8, 2))
        tk.Label(box, text=f"{i}. {q['question']}", bg="white", font=(A.UI_FONT, 10, "bold"), wraplength=740,
                 justify="left").pack(anchor="w")
        t = q.get("type")
        if t in ("radio", "select") and q.get("options"):
            var = tk.StringVar(value=a.get("choice") if a.get("choice") in q["options"] else "")
            ttk.Combobox(box, textvariable=var, values=[""] + q["options"], state="readonly",
                         width=70).pack(anchor="w", pady=2)
            getters[q["qid"]] = lambda v=var: {"choice": v.get()}
        elif t == "checkbox" and q.get("options"):
            lb = tk.Listbox(box, selectmode="multiple", height=min(8, len(q["options"])), exportselection=False,
                            font=(A.UI_FONT, 9), width=80)
            for j, o in enumerate(q["options"]):
                lb.insert("end", o)
                if o in (a.get("choices") or []):
                    lb.selection_set(j)
            lb.pack(anchor="w", pady=2)
            getters[q["qid"]] = lambda l=lb, o=q["options"]: {"choices": [o[j] for j in l.curselection()]}
        else:
            txt = tk.Text(box, height=4 if t == "textarea" else 1, wrap="word", font=(A.UI_FONT, 10), width=90)
            txt.insert("1.0", a.get("text") or a.get("choice") or "")
            txt.pack(anchor="w", pady=2)
            getters[q["qid"]] = lambda w=txt: {"text": w.get("1.0", "end-1c").strip()}

    def write(answers):
        on_done(answers)
        canvas.unbind_all("<MouseWheel>")
        top.destroy()

    def ok():
        write([{"qid": qid, "choice": "", "choices": [], "text": "", **g()} for qid, g in getters.items()])
        L.chat_note(f"Feedback: đã điền {len(getters)} câu vào trang — hãy kiểm tra rồi bấm Next/Submit của khảo sát.")

    row = tk.Frame(top, bg="white")
    row.pack(side="bottom", fill="x", padx=12, pady=10)
    st = dict(font=(A.UI_FONT, 10), relief="flat", cursor="hand2", padx=14, pady=3)
    tk.Button(row, text="Bỏ qua trang này", command=lambda: write([]), bg="#eee", **st).pack(side="right")
    tk.Button(row, text="✍ Điền vào form", command=ok, bg="#d97757", fg="white", activebackground="#c4643f",
              **st).pack(side="right", padx=6)
    top.protocol("WM_DELETE_WINDOW", lambda: write([]))
    # in front of the survey window (another program), so it is seen at once
    top.lift()
    top.attributes("-topmost", True)
    top.focus_force()
    top.after(1500, lambda: top.winfo_exists() and top.attributes("-topmost", False))
    L.chat_note("Feedback: hộp duyệt câu trả lời đã mở — sửa nếu cần rồi bấm “Điền vào form”.")
