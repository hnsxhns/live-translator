"""Learning menu for the Claude panel: a study library that Claude learns from.

  Folder       sync a study folder: new files are uploaded to Claude (Files API) and catalogued
               (title, description, course day, exercises). Files with the same content (e.g. the same
               PDF downloaded twice as "x.pdf" and "x (1).pdf") are uploaded once. Running it again
               only uploads what is new, then refreshes the catalogue.
  Agenda, Summary, Exercises, Journal, Certificate
               fill the prompt box with a Vietnamese prompt the user can edit before sending; the
               matching documents go along with it.
  Documents    the library list (date, title, file name); click a file name to open the original.

Everything is stored in <DATA_DIR>/learning (library.json + copies of the original files).
"""
import base64
import unicodedata
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote

import anthropic

A = None            # the app module (claude helpers, fonts, DATA_DIR, MODEL); set by setup()


def setup(app_module):
    global A
    A = app_module


KINDS = ["agenda", "book", "slides", "exercise", "journal_template", "certificate", "reference", "other"]
KIND_VI = {"agenda": "Agenda", "book": "Sách/bài đọc", "slides": "Slide", "exercise": "Bài tập",
           "journal_template": "Mẫu nhật ký", "certificate": "Chứng chỉ", "reference": "Tham khảo",
           "other": "Khác"}
# " (1)", " [2]", " - Copy", " - Copy (3)", "_copy", " copy 2", " - bản sao" at the end of a file name
COPY_MARK = re.compile(r"(\s*\(\d+\)|\s*\[\d+\]|\s*-\s*(copy|bản sao)(\s*\(\d+\))?|_copy\d*|\s+copy(\s*\d+)?)$", re.I)
JOURNAL_NAME = re.compile(r"journal|nh[aậ]t\s*k[yý]|nhat_ky|diary|log\s*book|reflection", re.I)


def supported_exts():
    return set(A.ATTACH_TYPES)


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_marked(name):
    return bool(COPY_MARK.search(os.path.splitext(name)[0]))


def safe_name(name):
    """A string that is safe as a Windows file name (no reserved or control characters, no line breaks)."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name)
    return re.sub(r"\s+", " ", name).strip(" .") or "file"


IS_MAC = sys.platform == "darwin"
PASTE = "⌘V" if IS_MAC else "Ctrl+V"                       # shown in hints
MULTI = "⌘ Cmd" if IS_MAC else "Ctrl"                      # multi-select key in lists


def open_path(path):
    """Open a file or folder with the system's default app."""
    if sys.platform == "win32":
        os.startfile(path)                                  # noqa: S606
    elif sys.platform == "darwin":
        r = subprocess.run(["open", path], capture_output=True, text=True, timeout=30)
        if r.returncode:                                     # e.g. whatsapp:// with WhatsApp not installed
            raise OSError(r.stderr.strip() or f"cannot open {path}")
    else:
        subprocess.Popen(["xdg-open", path])


def extract_text(path, ext):
    """(plain text, page count) of a PDF / Word / text file; ("", 0) for images."""
    if ext == ".pdf":
        import pymupdf
        with pymupdf.open(path) as d:
            return "\n\n".join(f"[Trang {i + 1}]\n{p.get_text()}" for i, p in enumerate(d)), d.page_count
    if ext == ".docx":
        return A.docx_text(path), 0
    if ext in A.TEXT_TYPES:
        raw = open(path, "rb").read()
        for enc in ("utf-8-sig", "utf-16", "cp1258", "latin-1"):
            try:
                return raw.decode(enc), 0
            except UnicodeDecodeError:
                continue
    return "", 0


def parse_json(text, required=None):
    """The JSON object in Claude's answer. The answer can hold more than one (e.g. when the request switched to
    a fallback model part-way and the answer started again, or text around it): every complete object is read
    and the last one is used — json.loads on the whole text fails with "Extra data" in that case."""
    text = re.sub(r"```(?:json)?", "", text.strip())
    dec, found = json.JSONDecoder(), []
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found.append(obj)
        i = text.find("{", end)
    if not found:
        raise ValueError("Claude không trả về JSON hợp lệ")
    if required:                                    # the last object that has the fields we asked for
        good = [o for o in found if all(k in o for k in required)]
        if good:
            return good[-1]
    return found[-1]


def long_client(client):
    """The same login with more time: these requests carry whole documents."""
    return client.with_options(timeout=A.LONG_TIMEOUT, max_retries=2)


def ask_json(client, system, content, schema, effort="low", max_tokens=16000):
    """One Claude call that must answer with JSON matching `schema` (structured output when the
    request accepts it, otherwise plain JSON text)."""
    client = long_client(client)
    messages = [{"role": "user", "content": content}]
    last = None
    for use_format in (True, False):
        for use_fallback in (True, False):
            cfg = {"effort": effort}
            if use_format:
                cfg["format"] = {"type": "json_schema", "schema": schema}
            extra = {"output_config": cfg}
            headers = None
            if use_fallback:
                extra["fallbacks"] = "default"
                headers = A.FALLBACK_HEADERS
            try:
                with client.messages.stream(model=A.MODEL, max_tokens=max_tokens, system=system,
                                            messages=messages, extra_body=extra,
                                            extra_headers=headers) as stream:
                    text = "".join(stream.text_stream)
                return parse_json(text, schema.get("required"))
            except anthropic.BadRequestError as e:
                last = e
            except (ValueError, json.JSONDecodeError) as e:
                last = e
                break
    raise last


# =====================================================================
# The library (index + file copies)
# =====================================================================
class Library:
    def __init__(self):
        self.dir = os.path.join(A.DATA_DIR, "learning")
        for sub in ("files", "text", "journals", "certificates"):
            os.makedirs(os.path.join(self.dir, sub), exist_ok=True)
        self.path = os.path.join(self.dir, "library.json")
        self.lock = threading.RLock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:  # noqa: BLE001
            self.data = {}
        for k, v in (("docs", {}), ("folder", ""), ("synced", ""), ("certificates", [])):
            self.data.setdefault(k, v)

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    # ---- queries ----
    @staticmethod
    def _key(d):
        return (d.get("day") or 9999, d.get("date") or "", (d.get("title") or d["name"]).lower())

    def docs(self):
        with self.lock:
            return sorted(self.data["docs"].values(), key=self._key)

    def get(self, doc_id):
        return self.data["docs"].get(doc_id)

    def file_path(self, doc):
        return os.path.join(self.dir, "files", doc["stored"])

    def text(self, doc):
        p = os.path.join(self.dir, "text", doc["id"] + ".txt")
        try:
            return open(p, encoding="utf-8").read()
        except OSError:
            return ""

    def days(self):
        """[(day, date, [docs])] for the course days found (day 0 = whole course, not listed)."""
        out = {}
        for d in self.docs():
            if (d.get("day") or 0) > 0:
                out.setdefault(d["day"], []).append(d)
        return [(day, next((x.get("date") for x in ds if x.get("date")), ""), ds)
                for day, ds in sorted(out.items())]

    def exercises(self):
        out = []
        for d in self.docs():
            for ex in d.get("exercises") or []:
                out.append({"day": d.get("day") or 0, "title": ex.get("title", ""),
                            "summary": ex.get("summary", ""), "location": ex.get("location", ""), "doc": d})
        return out

    def templates(self):
        """Journal template PDFs (catalogued as such, or named like one)."""
        docs = [d for d in self.docs() if d["ext"] == ".pdf"]
        hit = [d for d in docs if d.get("kind") == "journal_template"]
        return hit or [d for d in docs if JOURNAL_NAME.search(unicodedata.normalize("NFC", d["name"]))]

    def catalog_text(self):
        """Short description of the library for Claude's context."""
        docs = self.docs()
        if not docs:
            return ""
        lines = []
        for d in docs:
            day = f"Buổi {d['day']}" if d.get("day") else "Cả khóa"
            date = f" ({fmt_date(d['date'])})" if d.get("date") else ""
            ex = f" · {len(d['exercises'])} bài tập" if d.get("exercises") else ""
            lines.append(f"- [{d['id']}] {day}{date} · {KIND_VI.get(d.get('kind'), 'Khác')} · "
                         f"{d.get('title') or d['name']} — file {d['name']}{ex}. {d.get('description', '')}")
        return "\n".join(lines)

    # ---- content blocks for Claude ----
    def block(self, doc):
        """Content block for one document (uploaded file id when available, else the file inline)."""
        ext, title = doc["ext"], (doc.get("title") or doc["name"])[:250]
        fid = doc.get("file_id")
        if fid:
            if ext in A.IMAGE_TYPES:
                return {"type": "image", "source": {"type": "file", "file_id": fid}}
            return {"type": "document", "source": {"type": "file", "file_id": fid}, "title": title}
        path = self.file_path(doc)
        if ext in A.IMAGE_TYPES or (ext == ".pdf" and doc.get("size", 0) <= 20 * 2**20
                                    and doc.get("pages", 0) <= 500):
            try:
                return A.file_to_block(path)
            except ValueError:
                pass
        text = self.text(doc) or f"(không đọc được nội dung file {doc['name']})"
        return {"type": "text", "text": f'<file name="{doc["name"]}" title="{title}">\n{text}\n</file>'}


def out_folder(lib, fallback):
    """Where an exported file goes: <Output Folder>/Outputs if set, else the library's own folder."""
    d = A.output_dir("Outputs") if hasattr(A, "output_dir") else None
    if not d:
        d = os.path.join(lib.dir, fallback)
        os.makedirs(d, exist_ok=True)
    return d


def course_terms(lib, max_chars=260):
    """Words of this course Whisper should expect: acronyms and capitalised terms that keep coming back in the
    library's documents, and the document titles. A short comma list (Whisper reads ~200 tokens of prompt)."""
    from collections import Counter
    counts = Counter()
    for d in lib.docs():
        text = lib.text(d)[:200_000]
        for m in re.finditer(r"\b[A-Z]{2,6}s?\b", text):                   # ABT, KPI, KPIs, LLM...
            counts[m.group(0)] += 1
        for m in re.finditer(r"\b(?:[A-Z][a-z]+(?:[- ](?:of |and |to |the )?[A-Z][a-z]+){1,3})\b", text):
            counts[m.group(0)] += 2                                        # multi-word Capitalised Terms
    common = {"the", "this", "that", "part", "prompt", "table", "day", "and", "for", "with", "you", "your", "in",
              "on", "at", "to", "of", "a", "an", "is", "it", "we", "our", "step", "note", "use", "all", "each", "not"}
    def ok(t):
        first = t.split()[0].lower()
        return first not in common and not (t.isupper() and t.lower() in common | {"our", "are", "can", "will", "how"})
    terms = [t for t, n in counts.most_common(80) if n >= 3 and ok(t)]
    out = ""
    for t in terms:
        if len(out) + len(t) + 2 > max_chars:
            break
        out += (", " if out else "") + t
    return (out + ".") if out else ""


def fmt_date(iso):
    try:
        return time.strftime("%d/%m/%Y", time.strptime(iso, "%Y-%m-%d"))
    except (ValueError, TypeError):
        return iso or ""


# =====================================================================
# Folder sync: scan -> remove duplicates -> copy -> upload -> catalogue
# =====================================================================
CATALOG_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "description", "kind", "day", "date", "exercises"],
    "properties": {
        "title": {"type": "string"}, "description": {"type": "string"},
        "kind": {"type": "string", "enum": KINDS}, "day": {"type": "integer"}, "date": {"type": "string"},
        "exercises": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["title", "summary", "location"],
            "properties": {"title": {"type": "string"}, "summary": {"type": "string"},
                           "location": {"type": "string"}}}},
    },
}
CATALOG_SYSTEM = (
    "You catalogue course/study materials for a Vietnamese learner. Read the document and return JSON only. "
    "title: a short clear title (keep the document's own title if it has one). "
    "description: 1-2 Vietnamese sentences on what it contains. "
    "kind: agenda (course schedule/programme), book (textbook, chapter, reading), slides (lecture slides), "
    "exercise (exercise sheet/assignment), journal_template (a blank journal/log/reflection form the learner "
    "fills in after a session), certificate, reference, other. "
    "day: the course day/session number it belongs to, from the content, the file name or the folder names "
    "(e.g. 'Day 2', 'Ngày 3', 'Buổi 4', 'Session 5', 'Module 1'); 0 if unknown or for the whole course. "
    "date: the session date as YYYY-MM-DD if stated, else an empty string. "
    "exercises: every exercise, assignment, practice task or question set the learner must do that appears in "
    "the document (title as written, one-sentence Vietnamese summary, location such as page/section); "
    "an empty list if there are none.")
DAYS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["docs"],
    "properties": {"docs": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "day", "date"],
        "properties": {"id": {"type": "string"}, "day": {"type": "integer"}, "date": {"type": "string"}}}}},
}
DAYS_SYSTEM = (
    "You organise a course library into sessions (days). For each document you get its folder path, title, "
    "kind and a first guess of its day number and date. Return JSON with a consistent day number for every id "
    "(1, 2, 3... in course order; 0 for whole-course material such as the agenda or a journal template) and "
    "its date (YYYY-MM-DD or empty). Use the agenda text, when given, to match days and dates.")


class Syncer:
    def __init__(self, lib, client, progress):
        self.lib, self.client, self.progress = lib, client, progress
        self.files_api = True

    def run(self, folder):
        lib, exts = self.lib, supported_exts()
        self.progress("Đang quét thư mục...")
        found = []
        own = os.path.abspath(lib.dir).lower()
        for dp, dns, fns in os.walk(folder):
            dns[:] = [d for d in dns if not d.startswith(".") and os.path.abspath(os.path.join(dp, d)).lower() != own]
            for fn in fns:
                if fn.startswith(("~$", ".")):
                    continue
                if os.path.splitext(fn)[1].lower() in exts:
                    found.append(os.path.join(dp, fn))
        groups = {}
        for i, p in enumerate(found):
            if i % 10 == 0:
                self.progress(f"Đang kiểm tra trùng lặp {i + 1}/{len(found)}...")
            try:
                groups.setdefault(file_sha(p), []).append(p)
            except OSError:
                pass
        dup = sum(len(v) - 1 for v in groups.values())
        new, known, problems = [], 0, []
        today = time.strftime("%Y-%m-%d")
        for sha, paths in groups.items():
            # keep the "cleanest" name: no "(1)"/"- Copy" mark, shortest, oldest
            paths.sort(key=lambda p: (copy_marked(os.path.basename(p)), len(os.path.basename(p)), os.path.getmtime(p)))
            rels = [os.path.relpath(p, folder) for p in paths]
            doc_id = sha[:12]
            with lib.lock:
                old = lib.data["docs"].get(doc_id)
                if old:
                    known += 1
                    old["sources"] = sorted(set(old.get("sources", [])) | set(rels))
                    continue
            src = paths[0]
            name = unicodedata.normalize("NFC", os.path.basename(src))
            ext = os.path.splitext(name)[1].lower()
            stored = f"{doc_id}_{safe_name(name)}"
            try:
                shutil.copy2(src, os.path.join(lib.dir, "files", stored))
                text, pages = extract_text(src, ext)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{name}: {e}")
                continue
            with open(os.path.join(lib.dir, "text", doc_id + ".txt"), "w", encoding="utf-8") as f:
                f.write(text)
            doc = {"id": doc_id, "sha": sha, "name": name, "ext": ext, "stored": stored, "rel": unicodedata.normalize("NFC", rels[0]),
                   "sources": [unicodedata.normalize("NFC", r) for r in rels], "size": os.path.getsize(src), "pages": pages, "added": today,
                   "mtime": time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(src))),
                   "file_id": "", "title": os.path.splitext(name)[0], "description": "", "kind": "other",
                   "day": 0, "date": "", "exercises": [], "catalogued": False}
            with lib.lock:
                lib.data["docs"][doc_id] = doc
            new.append(doc)
        with lib.lock:
            lib.data["folder"] = folder
        lib.save()

        # upload everything that has no file id yet (new files, or ones that failed last time)
        todo = [d for d in lib.docs() if not d.get("file_id")]
        for i, d in enumerate(todo):
            if not self.files_api:
                break
            self.progress(f"Đang upload lên Claude {i + 1}/{len(todo)}: {d['name']}")
            try:
                self.upload(d)
            except (anthropic.PermissionDeniedError, anthropic.NotFoundError, anthropic.AuthenticationError) as e:
                self.files_api = False          # this login can't use the Files API: send files inline instead
                problems.append(f"Upload file không được hỗ trợ với tài khoản này ({type(e).__name__}); "
                                "tài liệu sẽ được gửi kèm trực tiếp khi hỏi.")
            except Exception as e:  # noqa: BLE001
                problems.append(f"{d['name']}: upload lỗi ({A.friendly_error(e)})")
        lib.save()

        # catalogue (new files and any that failed before), a few at a time
        todo = [d for d in lib.docs() if not d.get("catalogued")]
        if todo:
            from concurrent.futures import ThreadPoolExecutor
            done = [0]

            def one(d):
                try:
                    self.catalogue(d)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{d['name']}: chưa phân loại được ({A.friendly_error(e)})")
                done[0] += 1
                self.progress(f"Claude đang đọc và phân loại tài liệu {done[0]}/{len(todo)}...")
            self.progress(f"Claude đang đọc và phân loại tài liệu 0/{len(todo)}...")
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(one, todo))
            lib.save()
        if new or todo:
            self.progress("Đang sắp xếp tài liệu theo buổi học...")
            try:
                self.arrange_days()
            except Exception as e:  # noqa: BLE001
                problems.append(f"Chưa sắp xếp lại được theo buổi ({A.friendly_error(e)})")
        with lib.lock:
            lib.data["synced"] = time.strftime("%Y-%m-%d %H:%M")
        lib.save()
        return {"found": len(found), "new": len(new), "known": known, "dup": dup, "problems": problems,
                "total": len(lib.data["docs"])}

    def upload(self, doc):
        ext, path = doc["ext"], self.lib.file_path(doc)
        if ext in A.IMAGE_TYPES:
            if doc["size"] > 5 * 2**20:
                return                                          # too big for Claude; skipped
            name, data, mime = doc["name"], open(path, "rb").read(), A.IMAGE_TYPES[ext]
        elif ext == ".pdf":
            name, data, mime = doc["name"], open(path, "rb").read(), "application/pdf"
        else:                                                   # Word / text -> its plain text
            name = os.path.splitext(doc["name"])[0] + ".txt"
            data, mime = self.lib.text(doc).encode("utf-8"), "text/plain"
        f = long_client(self.client).files.upload(file=(name, data, mime))
        with self.lib.lock:
            doc["file_id"] = f.id

    def catalogue(self, doc):
        try:
            block = self.lib.block(doc)
            info = f"Đường dẫn trong thư mục học: {doc['rel']}\nNgày sửa file: {doc['mtime']}"
            res = ask_json(self.client, CATALOG_SYSTEM, [block, {"type": "text", "text": info}], CATALOG_SCHEMA)
        except anthropic.BadRequestError:
            # e.g. a PDF over the page limit: catalogue it from its text (first part is enough for this)
            text = self.lib.text(doc)[:150_000]
            info = f"Đường dẫn: {doc['rel']}\nNgày sửa file: {doc['mtime']}\n\n<file name=\"{doc['name']}\">\n{text}\n</file>"
            res = ask_json(self.client, CATALOG_SYSTEM, info, CATALOG_SCHEMA)
        with self.lib.lock:
            doc.update(title=res.get("title") or doc["title"], description=res.get("description", ""),
                       kind=res.get("kind") if res.get("kind") in KINDS else "other",
                       day=max(0, int(res.get("day") or 0)), date=res.get("date") or "",
                       exercises=res.get("exercises") or [], catalogued=True)

    def arrange_days(self):
        docs = self.lib.docs()
        if len(docs) < 2:
            return
        listing = [{"id": d["id"], "path": d["rel"], "title": d.get("title"), "kind": d.get("kind"),
                    "day": d.get("day"), "date": d.get("date")} for d in docs]
        agenda = "\n\n".join(self.lib.text(d)[:20_000] for d in docs if d.get("kind") == "agenda")
        prompt = "Documents:\n" + json.dumps(listing, ensure_ascii=False, indent=0)
        if agenda:
            prompt += f"\n\nAgenda text:\n{agenda}"
        res = ask_json(self.client, DAYS_SYSTEM, prompt, DAYS_SCHEMA)
        with self.lib.lock:
            for r in res.get("docs", []):
                d = self.lib.data["docs"].get(r.get("id"))
                if d:
                    d["day"] = max(0, int(r.get("day") or 0))
                    d["date"] = r.get("date") or d.get("date", "")


# =====================================================================
# Journal: fill a PDF template
# =====================================================================
JOURNAL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["fields", "placements", "preview"],
    "properties": {
        "fields": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "value"],
            "properties": {"name": {"type": "string"}, "value": {"type": "string"}}}},
        "placements": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["line_id", "position", "label", "text"],
            "properties": {"line_id": {"type": "integer"}, "position": {"type": "string", "enum": ["right", "below"]},
                           "label": {"type": "string"}, "text": {"type": "string"}}}},
        "preview": {"type": "string"},
    },
}
JOURNAL_SYSTEM = (
    "You fill in a learner's session journal (nhật ký buổi học) using a PDF template, the course documents of "
    "that session and what the teacher said (speech transcript and Vietnamese translation, if any). Write in "
    "the template's language, first person as the learner, concrete and concise, sized to fit the blank space. "
    "Return JSON only:\n"
    "- fields: if the template has form fields (listed), a value for each field to fill (exact field name; "
    "for checkboxes use 'yes' or 'no'). Otherwise an empty list.\n"
    "- placements: if the template has no form fields, one entry per blank to fill: line_id = the id of the "
    "printed label/question line; position = 'right' when the blank continues on the same line (e.g. "
    "'Date: ........'), or 'below' when the answer goes in the empty space under that line; label = the exact "
    "text on that line that comes before the blank (empty if not needed); text = the answer only (do not "
    "repeat the label). Use each line's bbox (x0, y0, x1, y1 in points) and the gap to the next line to judge "
    "how much fits. Leave signature fields empty. Otherwise an empty list.\n"
    "- preview: the whole filled journal as readable plain text (labels and answers) to show the learner.")


def font_file():
    for p in (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial Unicode.ttf",
              "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"):
        if os.path.exists(p):
            return p
    return None


def template_layout(path):
    """Form fields and printed text lines (with positions) of a PDF template."""
    import pymupdf
    fields, lines = [], []
    with pymupdf.open(path) as doc:
        sizes = [[round(p.rect.width), round(p.rect.height)] for p in doc]
        for pno, page in enumerate(doc):
            for w in page.widgets() or []:
                fields.append({"name": w.field_name, "type": w.field_type_string, "page": pno,
                               "bbox": [round(x, 1) for x in w.rect]})
            for b in page.get_text("dict")["blocks"]:
                if b.get("type") != 0:
                    continue
                for ln in b["lines"]:
                    t = "".join(s["text"] for s in ln["spans"]).strip()
                    if t:
                        lines.append({"id": len(lines), "page": pno, "bbox": [round(x, 1) for x in ln["bbox"]],
                                      "text": t, "size": round(ln["spans"][0]["size"], 1)})
    return fields, lines, sizes


def _write_fit(page, rect, text, fontname, max_size=11.0, min_size=6.0):
    """Write text into rect, shrinking the font until it fits. True if written."""
    size = max_size
    while size >= min_size:
        shape = page.new_shape()
        rc = shape.insert_textbox(rect, text, fontsize=size, fontname=fontname, color=(0.05, 0.15, 0.55))
        if rc >= 0:
            shape.commit()
            return True
        size -= 0.5
    return False


def fill_template(src, out, values, placements, lines):
    """Write the answers into a copy of the template PDF."""
    import pymupdf
    doc = pymupdf.open(src)
    ff = font_file()
    fontname = "vnfont" if ff else "helv"
    if ff:
        for page in doc:
            page.insert_font(fontname=fontname, fontfile=ff)
    vals = {v["name"]: v["value"] for v in values if v.get("value", "").strip()}
    for page in doc:
        widgets = list(page.widgets() or [])
        for w in widgets:
            if w.field_name not in vals:
                continue
            v = vals[w.field_name]
            if w.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
                w.field_value = w.on_state() if v.strip().lower() in ("yes", "true", "x", "1", "có", "on") else "Off"
                w.update()
            else:
                # Vietnamese letters need a real font: draw the text where the field is (flattened)
                rect = pymupdf.Rect(w.rect)
                page.delete_widget(w)
                inner = rect + (2, 1, -2, -1)
                _write_fit(page, inner, v, fontname, max_size=min(11.0, max(6.0, rect.height * 0.7)))
    by_id = {ln["id"]: ln for ln in lines}
    for pl in placements:
        ln = by_id.get(pl.get("line_id"))
        text = (pl.get("text") or "").strip()
        if not ln or not text:
            continue
        page = doc[ln["page"]]
        x0, y0, x1, y1 = ln["bbox"]
        same_page = [o for o in lines if o["page"] == ln["page"] and o["id"] != ln["id"]]
        right_edge = page.rect.x1 - 28
        max_size = min(11.0, max(7.0, ln["size"]))
        if pl.get("position") == "right":
            start = x1 + 3
            label = (pl.get("label") or "").strip()
            if label:
                hits = page.search_for(label, clip=pymupdf.Rect(x0 - 1, y0 - 1, x1 + 1, y1 + 1))
                if hits:
                    start = hits[0].x1 + 3
            for o in same_page:                      # another label further right on the same row
                ov = min(y1, o["bbox"][3]) - max(y0, o["bbox"][1])
                if ov > 0.5 * (y1 - y0) and o["bbox"][0] > start + 5:
                    right_edge = min(right_edge, o["bbox"][0] - 4)
            rect = pymupdf.Rect(start, y0 - 1, right_edge, y1 + 2)
            if _write_fit(page, rect, text, fontname, max_size, 7.0):
                continue
            below = [o["bbox"][1] for o in same_page if o["bbox"][1] >= y1 - 0.5
                     and o["bbox"][0] < right_edge and o["bbox"][2] > start]
            rect = pymupdf.Rect(start, y0 - 1, right_edge, (min(below) - 1) if below else page.rect.y1 - 28)
            _write_fit(page, rect, text, fontname, max_size)
        else:
            left = x0
            below = [o["bbox"][1] for o in same_page if o["bbox"][1] >= y1 - 0.5
                     and o["bbox"][0] < right_edge and o["bbox"][2] > left]
            bottom = (min(below) - 1) if below else page.rect.y1 - 28
            if bottom - y1 < 12:
                bottom = y1 + 40
            _write_fit(page, pymupdf.Rect(left, y1 + 1, right_edge, bottom), text, fontname, max_size)
    doc.save(out, garbage=3, deflate=True)
    doc.close()


def make_journal(client, lib, template, day_docs, session_text, instruction):
    """Ask Claude for the journal content and write it into the template. Returns (pdf path, preview)."""
    src = lib.file_path(template)
    fields, lines, sizes = template_layout(src)
    content = [A.file_to_block(src)]
    content += [lib.block(d) for d in day_docs if d["id"] != template["id"]]
    layout = {"page_sizes": sizes, "form_fields": fields,
              "lines": [{k: ln[k] for k in ("id", "page", "bbox", "text")} for ln in lines]}
    content.append({"type": "text", "text": (
        f"Template layout (from the PDF):\n{json.dumps(layout, ensure_ascii=False)}\n\n"
        f"Session content from the app:\n{session_text or '(không có)'}\n\n"
        f"Today: {time.strftime('%Y-%m-%d')}\n\nYêu cầu của người học: {instruction}")})
    res = ask_json(client, JOURNAL_SYSTEM, content, JOURNAL_SCHEMA, effort="medium", max_tokens=32000)
    stem = os.path.splitext(template["name"])[0]
    out = os.path.join(out_folder(lib, "journals"), f"NhatKy_{safe_name(stem)}_{time.strftime('%Y%m%d_%H%M')}.pdf")
    fill_template(src, out, res.get("fields", []), res.get("placements", []), lines)
    return out, res.get("preview", "")


# =====================================================================
# Sharing (certificate)
# =====================================================================
def copy_file_to_clipboard(path):
    """Put the file on the Windows clipboard (as a file, and as a picture for images), so Ctrl+V in
    WhatsApp / Outlook / Gmail attaches it. False if not possible."""
    if IS_MAC:
        return _mac_copy_file(path)
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    k32, u32 = ctypes.WinDLL("kernel32"), ctypes.WinDLL("user32")
    k32.GlobalAlloc.argtypes, k32.GlobalAlloc.restype = [wintypes.UINT, ctypes.c_size_t], ctypes.c_void_p
    k32.GlobalLock.argtypes, k32.GlobalLock.restype = [ctypes.c_void_p], ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.argtypes, u32.SetClipboardData.restype = [wintypes.UINT, ctypes.c_void_p], ctypes.c_void_p
    items = [(15, struct.pack("<IiiII", 20, 0, 0, 0, 1)                       # CF_HDROP: DROPFILES + paths
              + (os.path.abspath(path) + "\0\0").encode("utf-16-le"))]
    if os.path.splitext(path)[1].lower() in A.IMAGE_TYPES:
        try:
            from PIL import Image
            buf = io.BytesIO()
            Image.open(path).convert("RGB").save(buf, "BMP")
            items.append((8, buf.getvalue()[14:]))                            # CF_DIB
        except Exception:  # noqa: BLE001
            pass
    for _ in range(10):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        u32.EmptyClipboard()
        for fmt, data in items:
            h = k32.GlobalAlloc(0x0042, len(data))                            # GHND
            p = k32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            u32.SetClipboardData(fmt, h)
    finally:
        u32.CloseClipboard()
    return True


def to_recycle_bin(path):
    """Move a file to the Recycle Bin (Windows) so a delete can be undone; elsewhere delete it. True if gone."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
        op = SHFILEOPSTRUCTW(None, 3, os.path.abspath(path) + "\0", None, 0x0040 | 0x0010 | 0x0004 | 0x0400,
                             False, None, None)        # FO_DELETE; ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
        ctypes.WinDLL("shell32").SHFileOperationW(ctypes.byref(op))
        return not os.path.exists(path)
    if IS_MAC:
        return _mac_trash(path)
    try:
        os.remove(path)
    except OSError:
        return False
    return True


def _mac_trash(path):
    """Mac: move the file into the user's Trash (Finder can put it back). False if not possible;
    the file is never deleted for good here."""
    path = os.path.abspath(path)
    trash = os.path.expanduser("~/.Trash")
    try:
        base, ext = os.path.splitext(os.path.basename(path))
        dst = os.path.join(trash, f"{base} {time.strftime('%Y-%m-%d %H.%M.%S')}{ext}")
        if os.path.lexists(dst):                           # (this check may be blind: name is unique anyway)
            dst = os.path.join(trash, f"{base} {time.time():.6f}{ext}")
        if os.stat(path).st_dev != os.stat(trash).st_dev:
            raise OSError("other volume")                  # don't copy across disks; let Finder do it
        os.rename(path, dst)
        return not os.path.exists(path)
    except OSError:
        pass
    try:                                                   # ask Finder (may ask once for permission)
        subprocess.run(["osascript", "-e", f'tell application "Finder" to delete (POSIX file "{_applescript_path(path)}")'],
                       capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return not os.path.exists(path)


def _applescript_path(path):
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _mac_copy_file(path):
    """Mac: images go on the clipboard as a picture (⌘V pastes it into WhatsApp / Mail),
    other files (PDF) as a file reference."""
    path = os.path.abspath(path)
    script = f'set the clipboard to (POSIX file "{_applescript_path(path)}")'
    if os.path.splitext(path)[1].lower() in A.IMAGE_TYPES:
        try:
            import tempfile
            from PIL import Image
            tmp = os.path.join(tempfile.gettempdir(), "livetranslator_clip.png")
            Image.open(path).convert("RGB").save(tmp, "PNG")
            script = f'set the clipboard to (read (POSIX file "{_applescript_path(tmp)}") as «class PNGf»)'
        except Exception:  # noqa: BLE001
            pass
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def mapi_send(path, subject, body):
    """Open the default mail app's 'new message' window with the file attached (Windows Simple MAPI).
    Returns True when the mail app accepted it."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import c_ulong, c_void_p, c_wchar_p, POINTER

    class Recip(ctypes.Structure):
        _fields_ = [("ulReserved", c_ulong), ("ulRecipClass", c_ulong), ("lpszName", c_wchar_p),
                    ("lpszAddress", c_wchar_p), ("ulEIDSize", c_ulong), ("lpEntryID", c_void_p)]

    class FileDesc(ctypes.Structure):
        _fields_ = [("ulReserved", c_ulong), ("flFlags", c_ulong), ("nPosition", c_ulong),
                    ("lpszPathName", c_wchar_p), ("lpszFileName", c_wchar_p), ("lpFileType", c_void_p)]

    class Message(ctypes.Structure):
        _fields_ = [("ulReserved", c_ulong), ("lpszSubject", c_wchar_p), ("lpszNoteText", c_wchar_p),
                    ("lpszMessageType", c_wchar_p), ("lpszDateReceived", c_wchar_p),
                    ("lpszConversationID", c_wchar_p), ("flFlags", c_ulong), ("lpOriginator", POINTER(Recip)),
                    ("nRecipCount", c_ulong), ("lpRecips", POINTER(Recip)), ("nFileCount", c_ulong),
                    ("lpFiles", POINTER(FileDesc))]
    try:
        mapi = ctypes.WinDLL("mapi32")
        send = mapi.MAPISendMailW
    except (OSError, AttributeError):
        return False
    send.argtypes = [ctypes.c_size_t, ctypes.c_size_t, POINTER(Message), c_ulong, c_ulong]
    send.restype = c_ulong
    fd = FileDesc(0, 0, 0xFFFFFFFF, os.path.abspath(path), os.path.basename(path), None)
    msg = Message(0, subject, body, None, None, None, 0, None, 0, None, 1, ctypes.pointer(fd))
    try:
        rc = send(0, 0, ctypes.byref(msg), 0x1 | 0x8, 0)        # MAPI_LOGON_UI | MAPI_DIALOG
    except OSError:
        return False
    return rc in (0, 1)                                          # success, or the user cancelled


# =====================================================================
# Exercises: big exercises with steps (read from all files), step plans, solution PDF
# =====================================================================
_STR = {"type": "string"}
STEPS_SCHEMA = {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["title", "instruction"],
    "properties": {"title": _STR, "instruction": _STR}}}
BIGEX_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["exercises"],
    "properties": {"exercises": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["title", "goal", "location", "steps"],
        "properties": {"title": _STR, "goal": _STR, "location": _STR, "steps": STEPS_SCHEMA}}}}}
MERGE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["exercises"],
    "properties": {"exercises": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["title", "goal", "day", "doc_ids", "steps"],
        "properties": {"title": _STR, "goal": _STR, "day": {"type": "integer"},
                       "doc_ids": {"type": "array", "items": _STR}, "steps": STEPS_SCHEMA}}}}}
# plans used to solve an exercise: each step keeps the course's own prompt (verbatim) with its blanks marked as
# slots, so the app can fill them itself (a choice the learner makes, an earlier step's result, or the learner)
SLOT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name", "kind", "label", "from_step"],
               "properties": {"name": _STR, "kind": {"type": "string", "enum": ["choice", "previous", "learner"]},
                              "label": _STR, "from_step": {"type": "integer"}}}
STEPS2_SCHEMA = {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["title", "instruction", "prompt_template", "slots"],
    "properties": {"title": _STR, "instruction": _STR, "prompt_template": _STR,
                   "slots": {"type": "array", "items": SLOT_SCHEMA}}}}
PARAMS_SCHEMA = {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["name", "question", "options"],
    "properties": {"name": _STR, "question": _STR, "options": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["label", "values"],
        "properties": {"label": _STR, "values": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["slot", "text"],
            "properties": {"slot": _STR, "text": _STR}}}}}}}}}
PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["params", "steps"],
               "properties": {"params": PARAMS_SCHEMA, "steps": STEPS2_SCHEMA}}
PROMPT_GUIDE = (
    " For every step also give prompt_template and slots. prompt_template: when the document gives a ready "
    "prompt for that step, copy it VERBATIM, replacing each blank the learner would have to fill or paste (e.g. "
    "'[insert your table's challenge list from the Appendix]', '[YOUR PROBLEM]') with {{slot_name}}; when the "
    "prompt relies on an earlier step's result (e.g. 'the list of sub-problems from Stage 1', 'the workflow we "
    "designed'), append at its end a line such as '\n\nKết quả bước trước:\n{{slot_name}}' so that result "
    "can be pasted in automatically. Use an empty string when the step has no ready prompt. slots: one entry "
    "per {{slot_name}} used: kind 'choice' when the value is data the document already contains and the learner "
    "only has to pick (e.g. their table's list in the appendix), 'previous' when it is the result of an earlier "
    "step (from_step = that step's number, counting every step of the plan from 1 in order), 'learner' when only "
    "the learner can give it (from_step 0); label = a short Vietnamese description. Reuse the same slot name "
    "for the same value in every step (e.g. your_problem). params: the questions to ask the learner once to "
    "pick such data (e.g. 'Bạn ngồi bàn nào hôm qua?'); give every option (e.g. each table) with a short label "
    "(e.g. 'Table 3') and, for each choice slot it fills, the full text copied from the document. Empty list "
    "if there is nothing to choose.")
BIGEX_SYSTEM = (
    "You read a course document and list its substantial exercises, assignments, labs or projects "
    "(bài tập lớn) — tasks the learner has to carry out, especially ones whose instructions go through "
    "concrete steps. Return JSON only. For each exercise: title (as written in the document), goal (one "
    "Vietnamese sentence), location (pages/sections), and steps in order: follow the document's own steps "
    "exactly when it gives them (title + instruction in Vietnamese, keeping technical terms, commands, "
    "numbers and names from the document); if it only states the task, derive 3-8 clear steps from it. "
    "Skip trivial one-line questions and reading-only items. Empty list if there are none.")
MERGE_SYSTEM = (
    "You get exercises found file by file in a course folder. Return JSON with the list of distinct big "
    "exercises: merge entries that are the same exercise, or parts of one exercise spread over several files "
    "(keep every doc id, combine the steps in the right order without duplicates). Keep titles, goals and "
    "step texts as given (Vietnamese). day = the course day (0 if unknown). Order by day, then by course order.")
PLAN_SYSTEM = (
    "Break the given exercise from the attached course document into concrete steps the learner can work "
    "through one at a time, following the document's own instructions and order (3-8 steps for one exercise; "
    "when asked to follow the whole file, one step per exercise or instruction block, in the file's order, up "
    "to 15 steps). Return JSON only: steps with "
    "a short title and a Vietnamese instruction (keep technical terms, commands and values from the document)."
    + PROMPT_GUIDE)


WHOLE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["goal", "params", "exercises"],
    "properties": {"goal": _STR, "params": PARAMS_SCHEMA, "exercises": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["title", "goal", "steps"],
        "properties": {"title": _STR, "goal": _STR, "steps": STEPS2_SCHEMA}}}}}
WHOLE_SYSTEM = (
    "The attached course file may be one big problem made of many sub-exercises that build on each other. "
    "Return JSON only: goal = the overall goal of the whole file (one Vietnamese sentence); exercises = every "
    "sub-exercise in the file's own order (title as written, goal in one Vietnamese sentence, and its concrete "
    "steps: follow the file's own steps exactly when it gives them, otherwise 2-6 clear steps; Vietnamese, "
    "keeping technical terms, commands, numbers and names from the file). Note in a step's instruction when it "
    "uses the result of an earlier sub-exercise. Skip reading-only parts." + PROMPT_GUIDE)


def parts_to_steps(parts):
    """[sub-exercise with steps] -> one list of steps, each knowing its sub-exercise (part)."""
    parts = [p for p in parts if p.get("steps")]
    steps = []
    for k, p in enumerate(parts, 1):
        for j, st in enumerate(p["steps"], 1):
            steps.append({"title": st["title"], "instruction": st["instruction"], "part": k, "parts": len(parts),
                          "part_title": p["title"], "part_goal": p.get("goal", ""), "pstep": j,
                          "psteps": len(p["steps"]), "prompt_template": st.get("prompt_template", ""),
                          "slots": st.get("slots", [])})
    return steps


def known_file_plan(doc):
    """A whole-file plan that needs no Claude call: saved from before, or built from the sub-exercises
    (with steps) that the folder scan already found in this file. None if Claude must read the file."""
    fp = doc.get("fileplan")
    if fp and fp.get("v") == 2:          # plans from older versions lack the course prompts: make a new one
        return fp["goal"], fp["steps"], fp.get("params", [])
    return None


def file_exercise(doc, goal, steps, params=None):
    """The whole file as one exercise (sub-exercise by sub-exercise)."""
    return {"title": doc.get("title") or doc["name"], "goal": goal, "day": doc.get("day") or 0,
            "doc_ids": [doc["id"]], "steps": steps, "whole_file": True, "params": params or []}


def single_exercise(doc, title, goal, steps, params=None):
    return {"title": title, "goal": goal, "day": doc.get("day") or 0, "doc_ids": [doc["id"]], "steps": steps,
            "params": params or []}


def exercise_key(ex):
    """Identifies an exercise (its files, title and steps): saved progress belongs to it."""
    raw = json.dumps([ex.get("title"), sorted(ex.get("doc_ids", [])), [s["title"] for s in ex.get("steps", [])]],
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def plan_file(lib, client, doc, known):
    """Plan a whole file: (overall goal, [steps]) where each step knows its sub-exercise (part). Saved."""
    info = f"File: {doc['rel']}\nSub-exercises already noticed in it: " + ("; ".join(known) or "(none)")
    res = _ask_doc(lib, client, WHOLE_SYSTEM, doc, info, WHOLE_SCHEMA, effort="low", max_tokens=32000)
    goal, steps = res.get("goal", ""), parts_to_steps(res.get("exercises") or [])
    params = res.get("params") or []
    if steps:
        with lib.lock:
            doc["fileplan"] = {"v": 2, "goal": goal, "steps": steps, "params": params}
        lib.save()
    return goal, steps, params


def _ask_doc(lib, client, system, doc, info, schema, effort="medium", max_tokens=16000):
    """ask_json with one library document attached (falls back to its text if the file is refused)."""
    try:
        return ask_json(client, system, [lib.block(doc), {"type": "text", "text": info}], schema, effort=effort,
                        max_tokens=max_tokens)
    except anthropic.BadRequestError:
        text = lib.text(doc)[:300_000]
        return ask_json(client, system, f"{info}\n\n<file name=\"{doc['name']}\">\n{text}\n</file>", schema,
                        effort=effort, max_tokens=max_tokens)


def scan_exercises(lib, client, progress, full=False):
    """Read the files, find the big exercises with their steps and save the list in the library.
    Only files not read before are read again, unless `full`. Returns (number of exercises, problems)."""
    docs = [d for d in lib.docs() if d.get("kind") not in ("journal_template", "certificate")]
    todo = docs if full else [d for d in docs if "bigex" not in d]
    problems, done = [], [0]

    def one(d):
        try:
            res = _ask_doc(lib, client, BIGEX_SYSTEM, d, f"File: {d['rel']} (buổi {d.get('day') or '?'})",
                           BIGEX_SCHEMA)
            with lib.lock:
                d["bigex"] = res.get("exercises") or []
                d.pop("fileplan", None)
                d.pop("plans", None)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{d['name']}: {A.friendly_error(e)}")
        done[0] += 1
        progress(f"Claude đang đọc bài tập trong tài liệu {done[0]}/{len(todo)}...")
    if todo:
        progress(f"Claude đang đọc bài tập trong tài liệu 0/{len(todo)}...")
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(one, todo))
        lib.save()
    entries = [{"doc_id": d["id"], "doc_title": d.get("title") or d["name"], "day": d.get("day") or 0, **ex}
               for d in docs for ex in d.get("bigex") or []]
    if len(entries) > 1:
        progress("Đang gộp và sắp xếp danh sách bài tập lớn...")
        merged = ask_json(client, MERGE_SYSTEM, json.dumps(entries, ensure_ascii=False), MERGE_SCHEMA,
                          effort="medium")["exercises"]
    else:
        merged = [{"title": e["title"], "goal": e["goal"], "day": e["day"], "doc_ids": [e["doc_id"]],
                   "steps": e["steps"]} for e in entries]
    out = []
    for i, ex in enumerate(merged):
        ids = [x for x in ex.get("doc_ids", []) if lib.get(x)]
        if ex.get("steps"):
            out.append({**ex, "doc_ids": ids, "id": f"bx{i + 1}"})
    with lib.lock:
        lib.data["big_exercises"] = out
        lib.data["big_exercises_updated"] = time.strftime("%Y-%m-%d %H:%M")
    lib.save()
    return len(out), problems


def plan_steps(lib, client, ex):
    """Steps for an exercise from the per-document list (it has none yet)."""
    doc = ex["doc"]
    saved = (doc.get("plans") or {}).get(ex["title"])
    if isinstance(saved, dict) and saved.get("v") == 2:
        return saved["steps"], saved.get("params", [])
    info = (f"Exercise: {ex['title']}\nSummary: {ex.get('summary', '')}\nLocation: {ex.get('location', '')}\n"
            f"File: {doc['rel']}")
    res = _ask_doc(lib, client, PLAN_SYSTEM, doc, info, PLAN_SCHEMA, effort="low", max_tokens=32000)
    steps, params = res.get("steps") or [], res.get("params") or []
    if steps:
        with lib.lock:
            doc.setdefault("plans", {})[ex["title"]] = {"v": 2, "steps": steps, "params": params}
        lib.save()
    return steps, params


def pdf_fonts():
    """(folder, regular file, bold file) of a font with Vietnamese letters."""
    for folder, reg, bold in ((r"C:\Windows\Fonts", "arial.ttf", "arialbd.ttf"),
                              (r"C:\Windows\Fonts", "segoeui.ttf", "segoeuib.ttf"),
                              ("/System/Library/Fonts/Supplemental", "Arial.ttf", "Arial Bold.ttf"),
                              ("/Library/Fonts", "Arial Unicode.ttf", "Arial Unicode.ttf")):
        if os.path.exists(os.path.join(folder, reg)):
            return folder, reg, bold if os.path.exists(os.path.join(folder, bold)) else reg
    return None


NO_EMOJI = re.compile("[\U00010000-\U0010FFFF☀-➿️‍]")    # the PDF font has no emoji


def _paras(text):
    """Plain text (light Markdown) -> HTML paragraphs."""
    import html as _html
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        if not para.strip():
            continue
        lines = []
        for ln in para.split("\n"):
            ln = _html.escape(ln.rstrip())
            ln = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", ln)
            m = re.match(r"#{1,6}\s+(.*)", ln)
            if m:
                ln = f"<b>{m.group(1)}</b>"
            ln = re.sub(r"^(\s*)[-*]\s+", r"\1• ", ln)
            lines.append(ln)
        out.append("<p>" + "<br/>".join(lines) + "</p>")
    return "\n".join(out)


def text_html(text):
    """Claude's answer -> HTML; ready-to-use prompts go in a box headed "Prompt:"."""
    import html as _html
    text = text or ""
    out, pos = [], 0
    for m in PROMPT_BLOCK.finditer(text):
        out.append(_paras(text[pos:m.start()]))
        body = "<br/>".join(_html.escape(ln) for ln in m.group(1).strip().split("\n"))
        out.append(f"<p class='plabel'><b>Prompt:</b></p><p class='prompt'>{body}</p>")
        pos = m.end()
    out.append(_paras(text[pos:]))
    return "\n".join(x for x in out if x)


def write_pdf(out, body_html):
    """Write an A4 PDF from simple HTML (with a font that has Vietnamese letters)."""
    import pymupdf
    fonts = pdf_fonts()
    body_html = NO_EMOJI.sub("", body_html)
    css = (".prompt {background-color: #f4efe8; border: 1px solid #d9cfc4; padding: 6pt; font-size: 10pt;} "
           ".plabel {margin-bottom: 0;} "
           "body {font-size: 11pt; line-height: 1.35;} h1 {font-size: 17pt; color: #8a3f22;} "
           "h2 {font-size: 13.5pt; color: #8a3f22; margin-top: 14pt;} h3 {font-size: 12pt; margin-top: 10pt;} "
           ".meta {color: #666; font-size: 9.5pt;} .instr {color: #444; font-style: italic;} p {margin: 4pt 0;}")
    archive = None
    if fonts:
        folder, reg, bold = fonts
        archive = pymupdf.Archive(folder)
        css = (f"@font-face {{font-family: vn; src: url(\"{reg}\");}} "
               f"@font-face {{font-family: vn; src: url(\"{bold}\"); font-weight: bold;}} "
               "* {font-family: vn;} ") + css
    story = pymupdf.Story(html=f"<body>{body_html}</body>", user_css=css, archive=archive)
    writer = pymupdf.DocumentWriter(out)
    page = pymupdf.paper_rect("a4")
    where = page + (50, 50, -50, -50)
    more = True
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


def takeaways_pdf(out, history, title="Ý chính / Key takeaways"):
    """The whole key-takeaways history (blocks headed "[HH:MM]") as a PDF."""
    import html as _html
    parts = [f"<h1>{_html.escape(title)}</h1>",
             f"<p class='meta'>Xuất ngày {time.strftime('%d/%m/%Y %H:%M')}</p>"]
    for block in re.split(r"\n\s*\n(?=\[\d\d:\d\d\]\n)", history.strip()):
        m = re.match(r"\[(\d\d:\d\d)\]\n", block)
        if m:
            parts.append(f"<h3>{m.group(1)}</h3>")
            block = block[m.end():]
        parts.append(text_html(block))
    write_pdf(out, "\n".join(parts))
    return out


EXERCISE_RULES = (
    "<exercise_rules>\n"
    "You guide the learner through ONE step of a course exercise. Be very brief: no long explanations.\n"
    "1. If this step needs information only the learner can give (their own data, choices or parameters, or the "
    "blanks of a template from the course book) and the message does not contain it yet, only ask for those "
    "items as a short numbered list and end with the line [[CẦN NHẬP: item 1 | item 2]]. Do nothing else.\n"
    "2. Otherwise do the step. If the message contains 'PROMPT BÀI TẬP', that is the course's own prompt with "
    "the learner's data already filled in: answer it fully, exactly as the exercise asks (this is the step's "
    "result) — complete, but without padding. Otherwise give the result in a few lines. If the step needs a "
    "prompt for an AI tool, give the complete prompt between a line '----- PROMPT -----' and a line "
    "'----- HẾT PROMPT -----' (copy the course book's template when it has one, filled in with the learner's "
    "information).\n"
    "2b. Then give the essential reusable result of this step (e.g. the final structured list, the themes, the "
    "plan, the problem statement) between a line '----- KẾT QUẢ GỌN -----' and a line '----- HẾT KẾT QUẢ -----': "
    "as compact as possible but complete enough to be pasted into a later prompt.\n"
    "3. Then one line: '➡ Mang sang bước sau: ...' with only what the next step needs from this result (very "
    "short). Skip it at the last step.\n"
    "4. If the next step (see <next_step>) will need input from the learner (information, a plan, a prompt to "
    "write...), " + "{draft}" + "\n"
    "5. Finish with the line [[XONG BƯỚC]].\n"
    "Reply in Vietnamese.\n"
    "</exercise_rules>")
# how Claude prepares the learner's next input: as complete as possible, blanks only for what is unknown
DRAFT_RULE = (
    "prepare that input for them as completely as possible, between a line '----- MẪU BƯỚC SAU -----' and a "
    "line '----- HẾT MẪU -----': copy the course book's template for that step when there is one; fill in "
    "everything already known — from the book, from the results of earlier steps in this conversation, and "
    "from what the learner already gave; leave ONLY what is truly unknown as [cần điền: short description]. "
    "Then end with the line [[CẦN NHẬP TIẾP: item 1 | item 2]] listing just those unknown items (in Vietnamese).")
EXERCISE_RULES = EXERCISE_RULES.replace("{draft}", DRAFT_RULE)
OVERVIEW_RULES = (
    "<overview_rules>\n"
    "Summarize the exercise in 2-3 short lines (task, goal, expected result) — just enough to understand it. "
    "Do not start any step. If the first step (see <next_step>) will need input from the learner, "
    + DRAFT_RULE + " Reply in Vietnamese.\n"
    "</overview_rules>")
DRAFT_BLOCK = re.compile(r"-{3,} *MẪU BƯỚC SAU *-{3,} *\n(.*?)\n *-{3,} *HẾT MẪU *-{3,}", re.S | re.I)
DRAFT_START_TK = r"-{3,} *MẪU BƯỚC SAU *-{3,}"
DRAFT_END_TK = r"-{3,} *HẾT MẪU *-{3,}"
BLANK = re.compile(r"\[cần điền[^\]]*\]", re.I)
BLANK_LABEL = re.compile(r"\[cần điền:?\s*([^\]]*)\]", re.I)
OUTPUT_BLOCK = re.compile(r"-{3,} *KẾT QUẢ GỌN *-{3,} *\n(.*?)\n *-{3,} *HẾT KẾT QUẢ *-{3,}", re.S | re.I)
OUTPUT_START_TK = r"-{3,} *KẾT QUẢ GỌN *-{3,}"
OUTPUT_END_TK = r"-{3,} *HẾT KẾT QUẢ *-{3,}"
SLOT_MARK = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
MARKER = re.compile(r"\[\[\s*(?:CẦN NHẬP|CAN NHAP|XONG BƯỚC|XONG BUOC)[^\]]*\]\]", re.I)
MARKER_TK = r"\[\[ *(CẦN NHẬP|CAN NHAP|XONG BƯỚC|XONG BUOC)[^]]*\]\]"      # the same, for Tk's text search
NEEDS_INPUT = re.compile(r"\[\[\s*(?:CẦN NHẬP|CAN NHAP)(?!\s*TI)\s*:?\s*([^\]]*)\]\]", re.I)
NEXT_INPUT = re.compile(r"\[\[\s*(?:CẦN NHẬP TIẾP|CAN NHAP TIEP)\s*:?\s*([^\]]*)\]\]", re.I)
CARRY = re.compile(r"(?:➡\s*)?Mang sang bước sau\s*:\s*(.+)", re.I)
DONE = re.compile(r"\[\[\s*(?:XONG BƯỚC|XONG BUOC)\s*\]\]", re.I)
PROMPT_BLOCK = re.compile(r"-{3,} *PROMPT *-{3,} *\n(.*?)\n *-{3,} *HẾT PROMPT *-{3,}", re.S | re.I)
PROMPT_END_TK = r"-{3,} *HẾT PROMPT *-{3,}"


def solution_pdf(lib, session, summary):
    """The exercise solution (summary + every step) as a PDF in learning/solutions. Returns its path."""
    import html as _html
    ex = session["ex"]
    docs = [lib.get(i) for i in ex.get("doc_ids", []) if lib.get(i)]
    meta = [f"Ngày {time.strftime('%d/%m/%Y')}"]
    if ex.get("day"):
        meta.append(f"Buổi {ex['day']}")
    if docs:
        meta.append("Tài liệu: " + ", ".join(d.get("title") or d["name"] for d in docs))
    parts = [f"<h1>Lời giải bài tập: {_html.escape(ex['title'])}</h1>",
             f"<p class='meta'>{_html.escape(' · '.join(meta))}</p>"]
    if ex.get("goal"):
        parts.append(f"<p><b>Mục tiêu:</b> {_html.escape(ex['goal'])}</p>")
    if session.get("overview"):
        parts += ["<h2>Tóm tắt bài tập</h2>", text_html(session["overview"])]
    parts += ["<h2>Tổng kết lời giải</h2>", text_html(summary), "<h2>Chi tiết từng bước</h2>"]
    part = last_i = None
    for item in session["log"]:
        st, i = item["step"], item.get("i")
        if st.get("part") and st["part"] != part:
            part = st["part"]
            parts.append(f"<h2>Bài {part}/{st['parts']}: {_html.escape(st['part_title'])}</h2>")
            if st.get("part_goal"):
                parts.append(f"<p class='instr'>{_html.escape(st['part_goal'])}</p>")
        if i != last_i:
            last_i = i
            label = f"Bước {st['pstep']}/{st['psteps']}" if st.get("part") else f"Bước {(i or 0) + 1}"
            parts.append(f"<h3>{label}: {_html.escape(st.get('title', ''))}</h3>")
            if st.get("instruction"):
                parts.append(f"<p class='instr'>{_html.escape(st['instruction'])}</p>")
        if item.get("prompt"):                                     # the course prompt used, as filled in
            body = "<br/>".join(_html.escape(ln) for ln in item["prompt"].split("\n"))
            parts.append(f"<p class='plabel'><b>Prompt đã dùng:</b></p><p class='prompt'>{body}</p>")
        if item.get("input"):
            filled = item["q"]
            m = re.search(r"thông tin của tôi[^\n]*\n", item["q"], re.I)      # only what the learner sent
            if m:
                filled = item["q"][m.end():]
            parts.append(f"<p class='instr'><b>Thông tin của học viên:</b><br/>"
                         f"{_html.escape(filled.strip()).replace(chr(10), '<br/>')}</p>")
        parts.append(text_html(item["a"]))
    out = os.path.join(out_folder(lib, "solutions"),
                       f"LoiGiai_{safe_name(ex['title'])[:50]}_{time.strftime('%Y%m%d_%H%M')}.pdf")
    write_pdf(out, "\n".join(parts))
    return out


# =====================================================================
# Source: a web page that lists the course materials; new files on it are downloaded into the study folder
# =====================================================================
SOURCE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LiveTranslator"
SOURCE_MAX_BYTES = 200 * 2**20                       # skip files larger than this
# documents only (web pages, scripts and data files on the page are not course materials)
SOURCE_EXTS = {".pdf", ".docx", ".txt", ".md", ".csv", ".tsv", ".srt", ".vtt", ".png", ".jpg", ".jpeg", ".gif",
               ".webp"}
MIME_EXT = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
            "image/webp": ".webp", "text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}


def _http(url, method="GET", timeout=30, headers=None, opener=None):
    import urllib.request
    req = urllib.request.Request(url, method=method, headers={"User-Agent": SOURCE_UA, "Accept": "*/*",
                                                              **(headers or {})})
    if opener is not None:                                  # signed in: cookies of every site, kept on redirects
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout, context=_mac_ssl())   # noqa: S310  (http/https only)


def _mac_ssl():
    """python.org Python on macOS does not use the system's certificates: without this every https
    download fails with CERTIFICATE_VERIFY_FAILED (certifi is bundled with the Claude SDK). None elsewhere."""
    if not IS_MAC:
        return None
    try:
        import ssl
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return None


def login_opener(page):
    """A downloader that behaves like the signed-in browser window: it starts with all the window's cookies
    (course site, its file server, its sign-in service) and keeps the new ones set while a download hops
    between them (e.g. exec.mit.edu -> mitexeced.file.force.com)."""
    import http.cookiejar
    import urllib.request
    jar = http.cookiejar.CookieJar()
    for c in page.get("cookies") or []:
        dom = c.get("domain") or ""
        jar.set_cookie(http.cookiejar.Cookie(
            0, c["name"], c["value"], None, False, dom, bool(dom), dom.startswith("."), c.get("path") or "/", True,
            bool(c.get("secure")), None, False, None, None, {"HttpOnly": None} if c.get("httponly") else {}))
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if _mac_ssl() is not None:
        handlers.append(urllib.request.HTTPSHandler(context=_mac_ssl()))
    op = urllib.request.build_opener(*handlers)
    op.addheaders = [("User-Agent", page.get("ua") or SOURCE_UA), ("Referer", page.get("url") or "")]
    return op


def page_links(url):
    """[(absolute url, link text)] of every link on the page."""
    from html.parser import HTMLParser
    from urllib.parse import urljoin

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.out, self._href, self._text = [], None, []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self._href, self._text = dict(attrs).get("href"), []

        def handle_data(self, data):
            if self._href is not None:
                self._text.append(data)

        def handle_endtag(self, tag):
            if tag == "a" and self._href:
                self.out.append((self._href, " ".join("".join(self._text).split())))
                self._href = None
    with _http(url) as r:
        base = r.geturl()
        raw = r.read(10 * 2**20)
        charset = r.headers.get_content_charset() or "utf-8"
    p = Links()
    p.feed(raw.decode(charset, errors="replace"))
    return [(urljoin(base, h.strip()), t) for h, t in p.out if h and not h.strip().startswith(("#", "javascript:",
                                                                                              "mailto:"))]


def doc_links(url, exts):
    """Links on the page that point to a document the library can use (by file extension, or for links that
    look like downloads, by the type the server reports). Returns [(url, link text, extension)]."""
    from urllib.parse import unquote, urlparse
    out, seen, checked = [], set(), 0
    for link, text in page_links(url):
        p = urlparse(link)
        if p.scheme not in ("http", "https"):
            continue
        key = link.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        ext = os.path.splitext(unquote(p.path))[1].lower()
        if ext in exts:
            out.append((key, text, ext))
        elif checked < 25 and re.search(r"download|export|attachment|file|/d/", link, re.I):
            checked += 1                                    # ask the server what it is (a few at most)
            try:
                with _http(key, "HEAD", timeout=15) as r:
                    ext = MIME_EXT.get((r.headers.get_content_type() or "").lower())
                if ext and ext in exts:
                    out.append((key, text, ext))
            except Exception:  # noqa: BLE001
                pass
    return out


def cookie_header(cookies, url):
    """'Cookie' header value for url from the login browser's cookies (domain and path must match)."""
    from urllib.parse import urlparse
    p = urlparse(url)
    host, path = (p.hostname or "").lower(), p.path or "/"
    out = []
    for c in cookies or []:
        dom = (c.get("domain") or "").lower().lstrip(".")
        if dom and not (host == dom or host.endswith("." + dom)):
            continue
        if not path.startswith(c.get("path") or "/"):
            continue
        out.append(f"{c['name']}={c['value']}")
    return "; ".join(out)


def login_headers(page, url):
    """Headers that make a download look like it comes from the signed-in browser window."""
    h = {"Referer": page.get("url") or url}
    if page.get("ua"):
        h["User-Agent"] = page["ua"]
    ck = cookie_header(page.get("cookies"), url)
    if ck:
        h["Cookie"] = ck
    return h


def probe_doc(url, headers, opener=None):
    """For a link without a file extension: what does the server send? Returns (ext, file name) for a document,
    None for a web page. Reads only the headers."""
    import email.message
    with _http(url, timeout=20, headers=None if opener else headers, opener=opener) as r:
        ctype = (r.headers.get_content_type() or "").lower()
        name = ""
        cd = r.headers.get("Content-Disposition")
        if cd:
            msg = email.message.Message()
            msg["content-disposition"] = cd
            name = msg.get_filename() or ""
    ext = os.path.splitext(name)[1].lower() or MIME_EXT.get(ctype, "")
    if ctype == "text/html" and not name:
        return None
    return (ext, name) if ext else None


def source_profile():
    """Browser profile of the Source window (keeps the site login). One folder for every app version, so an
    update does not ask for the login again."""
    if os.environ.get("LT_SOURCE_PROFILE"):              # tests: never share the user's own login
        return os.path.normpath(os.environ["LT_SOURCE_PROFILE"])
    base = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else ""
    if base:
        return os.path.join(base, "LiveTranslator", "source_browser")
    return os.path.join(A.DATA_DIR, "learning", "source_browser")


LOGIN_PATH = re.compile(r"/(s/)?login\b|sign-?in|/sso\b|saml|oauth|/auth\b|/idp/", re.I)


def is_login_url(url):
    from urllib.parse import urlparse
    return bool(LOGIN_PATH.search(urlparse(url).path or ""))


def fix_source_url(url):
    """A sign-in page saved as the Source page -> the page it leads to (its startURL/return parameter)."""
    from urllib.parse import parse_qs, unquote, urljoin, urlparse
    if not is_login_url(url):
        return url
    q = parse_qs(urlparse(url).query)
    for k in ("startURL", "starturl", "returnUrl", "return_to", "redirect", "next", "RelayState"):
        if q.get(k):
            return urljoin(url, unquote(q[k][0]))
    return url


def source_log(msg):
    """A line in learning/source_autolog.txt (with the Source window's own notes): what the app did, step by
    step, so a problem on a real site can be found afterwards."""
    try:
        path = os.path.join(A.DATA_DIR, "learning", "source_autolog.txt")
        old = open(path, encoding="utf-8").read()[-30000:] if os.path.exists(path) else ""
        with open(path, "w", encoding="utf-8") as f:
            f.write(old + time.strftime("%H:%M:%S ") + "[app] " + str(msg)[:3000] + "\n")
    except Exception:  # noqa: BLE001
        pass


def save_library(lib, why):
    """Save the library and note it (path, size) or the error in the Source log."""
    try:
        lib.save()
        source_log(f"saved library ({why}): {lib.path} {os.path.getsize(lib.path)} bytes, "
                   f"source_url={lib.data.get('source_url')}, clicks={lib.data.get('source_clicks')}")
    except Exception as e:  # noqa: BLE001
        import traceback
        source_log(f"COULD NOT SAVE library ({why}): {type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise


def save_source_debug(page, result=None):
    """What the Source window saw (tabs, panel, links; never cookies) -> learning/source_debug.json, so a wrong
    scan on a real site can be checked."""
    try:
        d = {k: page.get(k) for k in ("url", "title", "scope", "tab", "tabs", "panels", "clicks", "error")}
        d["result"] = result
        d["outline"] = page.get("outline")              # file-like things the page showed (tags, attributes)
        d["links"] = [{"href": l.get("href"), "text": l.get("text")} for l in (page.get("links") or [])[:200]]
        d["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(A.DATA_DIR, "learning", "source_debug.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass


def wait_profile_free(profile, timeout=15.0):
    """Wait until the previous Source window's browser has let go of the profile (it keeps it locked a few
    seconds after closing; a new window on a locked profile never opens)."""
    if sys.platform != "win32":
        return True
    ps = ("(Get-CimInstance Win32_Process -Filter \"name='msedgewebview2.exe'\" | Where-Object { $_.CommandLine "
          "-like '*" + os.path.normpath(profile).replace("'", "''") + "*' }).Count")
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                               timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if (r.stdout.strip() or "0") == "0":
                return True
        except Exception:  # noqa: BLE001
            return True
        time.sleep(0.5)
    return False                                    # still in use: an earlier Source window is open


def salesforce_download(link):
    """Salesforce sites (e.g. exec.mit.edu) link files to a preview page (/s/contentdocument/069...); the file
    itself is at /sfc/servlet.shepherd/document/download/069.... Other links are returned unchanged."""
    m = re.match(r"(https?://[^/]+)(/[^?#]*?)?/s/(contentdocument|contentversion)/((?:069|068)\w{12,15})", link)
    if not m:
        return link
    kind = "version" if m.group(4).startswith("068") else "document"     # 068 = a file version, 069 = the file
    return f"{m.group(1)}{m.group(2) or ''}/sfc/servlet.shepherd/{kind}/download/{m.group(4)}?operationContext=S1"


def browser_doc_links(page, exts, opener=None):
    """Documents among the links the login window collected: by file extension, by a download attribute, or
    (for links that look like downloads) by what the server sends. Returns [(url, link text, ext, file name)]."""
    from urllib.parse import unquote, urlparse
    out, seen, probed = [], set(), 0
    for l in page.get("links") or []:
        link = salesforce_download((l.get("href") or "").split("#")[0])
        p = urlparse(link)
        if p.scheme not in ("http", "https") or link in seen:
            continue
        seen.add(link)
        text = l.get("text") or ""
        dl = l.get("download") or ""
        ext = os.path.splitext(unquote(p.path))[1].lower()
        if ext in exts:
            out.append((link, text, ext, os.path.basename(unquote(p.path))))
        elif os.path.splitext(dl)[1].lower() in exts:
            out.append((link, text, os.path.splitext(dl)[1].lower(), dl))
        elif probed < 80 and (dl or re.search(r"download|export|attachment|file|resource|material|document|"
                                              r"/d/|pluginfile|forcedownload|servlet\.shepherd", link, re.I)):
            probed += 1
            try:
                got = probe_doc(link, login_headers(page, link), opener)
            except Exception:  # noqa: BLE001
                got = None
            if got and got[0] in exts:
                out.append((link, text, got[0], got[1]))
    return out


def download(url, folder, ext_hint="", headers=None, opener=None):
    """Download a file into folder (never overwriting a different file). Returns (path, sha256, newly saved).
    `headers` or `opener` (login_opener) carry the login for sites that need one."""
    import email.message
    from urllib.parse import unquote, urlparse
    os.makedirs(folder, exist_ok=True)
    with _http(url, timeout=120, headers=None if opener else headers, opener=opener) as r:
        name = ""
        cd = r.headers.get("Content-Disposition")
        if (r.headers.get_content_type() or "").lower() == "text/html" and not cd:
            # a web page instead of the file: usually the site's login page (login expired)
            raise ValueError("trang web trả về một trang thay vì file (có thể cần đăng nhập lại trong cửa sổ Source)")
        if cd:
            msg = email.message.Message()
            msg["content-disposition"] = cd
            name = msg.get_filename() or ""
        if not name:
            name = os.path.basename(unquote(urlparse(r.geturl()).path)) or "tai_lieu"
        if not os.path.splitext(name)[1]:
            name += ext_hint or MIME_EXT.get((r.headers.get_content_type() or "").lower(), "")
        name = safe_name(name)
        tmp = os.path.join(folder, f".dl_{os.getpid()}_{threading.get_ident()}.part")
        h, size = hashlib.sha256(), 0
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > SOURCE_MAX_BYTES:
                    f.close()
                    os.remove(tmp)
                    raise ValueError(f"file lớn hơn {SOURCE_MAX_BYTES // 2**20} MB")
                h.update(chunk)
                f.write(chunk)
    sha = h.hexdigest()
    path = os.path.join(folder, name)
    stem, ext = os.path.splitext(name)
    k = 1
    while os.path.exists(path):
        if file_sha(path) == sha:                           # the same file is already there
            os.remove(tmp)
            return path, sha, False
        k += 1
        path = os.path.join(folder, f"{stem} ({k}){ext}")
    os.replace(tmp, path)
    return path, sha, True


# =====================================================================
# UI
# =====================================================================
class LearningUI:
    """The 🎓 Learning button + menu next to the 📎 button in the Claude prompt box."""

    def __init__(self, mw, button):
        self.mw, self.root, self.button = mw, mw.root, button
        self.lib = Library()
        self.job = None                 # documents/instructions that go with the next prompt
        self.syncing = False
        self.session = None             # the exercise being solved step by step
        self.sent_job = None            # the job that went with the last sent prompt
        button.config(command=self.show_menu)
        self.menu = tk.Menu(self.root, tearoff=0, font=(A.UI_FONT, 10), activebackground="#d97757",
                            activeforeground="white")

    # ---- menu ----
    def show_menu(self, anchor=None):
        if not self.mw.panel_open:
            self.mw.toggle_panel("right")
            self.root.after(120, lambda: self.show_menu())      # once the prompt box is on screen
            return
        n = len(self.lib.data["docs"])
        m = self.menu
        m.delete(0, "end")
        info = (f"Thư viện: {n} tài liệu · đồng bộ {self.lib.data.get('synced') or '-'}" if n
                else "Chưa có tài liệu — chọn Folder để bắt đầu")
        m.add_command(label=info, state="disabled")
        m.add_separator()
        need = "normal" if n else "disabled"
        m.add_command(label="📁  Folder — " + ("đang đồng bộ..." if self.syncing else
                      "chọn thư mục học"),
                      command=self.folder, state="disabled" if self.syncing else "normal")
        if self.lib.data.get("folder"):
            m.add_command(label="🔄  Refresh — tìm file mới, cập nhật dữ liệu", command=self.refresh,
                          state="disabled" if self.syncing else "normal")
        src = self.lib.data.get("source_url") or ""
        host = re.sub(r"^https?://(www\.)?", "", src).split("/")[0]
        m.add_command(label="🌐  Source — " + (f"quét {host} tìm tài liệu mới" if src else
                                               "link trang web chứa tài liệu"),
                      command=self.source, state="disabled" if self.syncing else "normal")
        if src:
            m.add_command(label="      ✎  Đổi link Source", command=self.change_source,
                          state="disabled" if self.syncing else "normal")
        if self.cached_agenda():
            m.add_command(label="🗓  Agenda — chương trình học (đã lưu, mở ngay)", command=self.agenda, state=need)
            m.add_command(label="      🔄  Tạo lại Agenda", command=lambda: self.agenda(fresh=True), state=need)
        else:
            m.add_command(label="🗓  Agenda — chương trình học", command=self.agenda, state=need)
        m.add_command(label="📚  Documents — danh sách tài liệu", command=self.documents, state=need)
        m.add_command(label="📝  Summary — tổng kết buổi học", command=self.summary, state=need)
        m.add_command(label="✏  Exercises — bài tập, giải theo từng bước", command=self.exercises, state=need)
        if self.session:
            ex = self.session["ex"]
            m.add_command(label=f"      ⏹  Dừng bài \"{ex['title'][:30]}\" (bước {self.session['i'] + 1}/{len(ex['steps'])})",
                          command=self.stop_session)
        m.add_command(label="📓  Journal — điền nhật ký buổi học (PDF)", command=self.journal, state=need)
        m.add_command(label="📝  Feedback — điền khảo sát buổi học (QR)", command=self.feedback)
        m.add_command(label="🏆  Certificate — chứng chỉ & chia sẻ", command=self.certificate)
        m.add_separator()
        out = A.load_config().get("output_folder") or ""
        m.add_command(label="📂  Output Folder — " + (f"…{out[-38:]}" if len(out) > 40 else out or
                                                     "chọn nơi lưu backup & kết quả"),
                      command=self.mw.choose_output_folder)
        n_out = len(self.lib.data.get("outputs", []))
        m.add_command(label="📋  List output files — các file đã tạo" + (f" ({n_out})" if n_out else ""),
                      command=self.list_outputs)
        b = anchor or self.button
        m.update_idletasks()
        try:
            m.tk_popup(b.winfo_rootx() + b.winfo_width() - m.winfo_reqwidth(),
                       b.winfo_rooty() - m.winfo_reqheight())
        finally:
            m.grab_release()

    # ---- prompt + job ----
    def set_prompt(self, text, job):
        p = self.mw.prompt
        p.delete("1.0", "end")
        p.insert("end", text)
        job["prompt"] = text                # to know later whether the user edited it before sending
        self.job = job
        if job.get("kind") == "exstep" and BLANK.search(text):
            # information only the learner has: ask it in a small dialog, then write it into the prompt
            self.root.after(80, lambda j=job: self.fill_prompt_blanks(j))
        self.mw._refresh_attach_bar()
        if not self.mw.panel_open:
            self.mw.toggle_panel("right")
        p.focus_set()

    def take_job(self):
        job, self.job = self.job, None
        self.sent_job = job
        return job

    def blocks(self, job):
        out = []
        for i in job.get("doc_ids", []):
            d = self.lib.get(i)
            if d:
                out.append(self.lib.block(d))
        return out + job.get("blocks", [])

    def chat_note(self, text, tag=None):
        """A line from the Learning menu in the Claude answers pane (UI thread)."""
        w = self.mw.chat
        w.configure(state="normal")
        if w.index("end-1c") != "1.0":
            w.insert("end", "\n\n")
        w.insert("end", "🎓 Learning: ", "who")
        w.insert("end", text, tag or ())
        w.see("end")
        w.configure(state="disabled")

    def later(self, fn):
        """Run fn on the UI thread (from a worker thread)."""
        self.mw.ui_q.put(("call", fn))

    # ---- Folder ----
    def folder(self):
        """Choose the study folder (then sync it)."""
        last = self.lib.data.get("folder") or ""
        folder = filedialog.askdirectory(parent=self.root, title="Chọn thư mục học tập để Claude học",
                                         initialdir=last if os.path.isdir(last) else None)
        if folder:
            self.sync(folder)

    def refresh(self, on_done=None):
        """Sync the current study folder again: upload new files, refresh the catalogue."""
        last = self.lib.data.get("folder") or ""
        if not os.path.isdir(last):
            messagebox.showinfo("Refresh", "Chưa chọn thư mục học (hoặc thư mục không còn). Hãy chọn Folder.",
                                parent=self.root)
            return
        self.sync(last, on_done)

    def sync(self, folder, on_done=None):
        if self.syncing:
            return
        self.syncing = True
        self.chat_note(f"Bắt đầu đồng bộ thư mục {folder}")

        def progress(msg):
            self.mw.ui_q.put(("status", "🎓 " + msg))
            self.later(lambda: self.mw.busy("sync", msg))

        def work():
            try:
                r = Syncer(self.lib, self.mw.client, progress).run(folder)
                msg = (f"Đồng bộ xong: {r['found']} file trong thư mục, {r['new']} tài liệu mới đã upload, "
                       f"{r['known']} đã có từ trước, {r['dup']} bản trùng nội dung đã bỏ qua. "
                       f"Thư viện có {r['total']} tài liệu.")
                if r["problems"]:
                    msg += "\nLưu ý:\n" + "\n".join("• " + p for p in r["problems"][:12])
                self.later(lambda: self.chat_note(msg))
                terms = course_terms(self.lib)                     # new documents: new words to expect
                self.later(lambda: setattr(self.mw.recognizer, "glossary", terms))
                self.mw.ui_q.put(("status", f"🎓 Đồng bộ xong: +{r['new']} tài liệu mới"))
            except Exception as e:  # noqa: BLE001
                self.later(lambda: self.chat_note(f"Đồng bộ lỗi: {A.friendly_error(e)}", "err"))
                self.mw.ui_q.put(("error", f"🎓 Đồng bộ lỗi: {A.friendly_error(e)}"))
            finally:
                self.syncing = False
                self.later(lambda: self.mw.done("sync"))
                if on_done:
                    self.later(on_done)
        threading.Thread(target=work, daemon=True).start()

    # ---- Feedback: the day's survey (QR), answers drafted by Claude, reviewed by the user ----
    def feedback(self):
        import feedback
        feedback.start(self)

    # ---- Source: a web page with the course materials ----
    def source(self):
        """First time: ask for the page's link. Then (and every time after): scan it for new documents."""
        if not self.lib.data.get("source_url") and not self.ask_source_url():
            return
        self.scan_source()

    def change_source(self):
        if self.ask_source_url(self.lib.data.get("source_url", "")):
            self.scan_source()

    def ask_source_url(self, current=""):
        top = self._dialog("Source — trang web chứa tài liệu", "660x230")
        tk.Label(top, text="Link trang web chứa tài liệu khóa học:", font=(A.UI_FONT, 10, "bold"),
                 bg="white").pack(anchor="w", padx=14, pady=(14, 2))
        tk.Label(top, text="Mỗi lần bấm 🌐 Source, trang này mở trong một cửa sổ trình duyệt của app: đăng nhập "
                           "(chỉ lần đầu, app nhớ phiên), mở mục tài liệu (Materials) rồi bấm nút xanh 📥. Tài liệu "
                           "chưa có trong thư mục học được tải về đó và upload cho Claude.",
                 bg="white", fg="#666", font=(A.UI_FONT, 9), wraplength=620, justify="left").pack(anchor="w", padx=14)
        var = tk.StringVar(value=current)
        entry = tk.Entry(top, textvariable=var, font=(A.UI_FONT, 10))
        entry.pack(fill="x", padx=14, pady=10)
        entry.focus_set()
        result = []

        def ok():
            url = var.get().strip()
            if not re.match(r"https?://", url, re.I):
                messagebox.showinfo("Source", "Hãy nhập một link bắt đầu bằng http:// hoặc https://", parent=top)
                return
            result.append(url)
            top.destroy()
        self._buttons(top, ok, ok="Lưu và mở")
        top.wait_window()
        if not result:
            return None
        with self.lib.lock:
            if self.lib.data.get("source_url") != result[0]:
                self.lib.data["source_files"] = {}               # a new page: start the comparison afresh
            self.lib.data["source_url"] = result[0]
        self.lib.save()
        return result[0]

    def scan_source(self):
        """Open the Source page in the login window (the login is remembered, so usually it opens signed in);
        the user goes to the materials page and presses the button; documents not yet in the study folder are
        downloaded into it, with the login, and uploaded for Claude."""
        if self.syncing:
            self.mw.status.config(text="⏳ Đang đồng bộ — chờ xong rồi bấm Source lại.", fg="#ffd479")
            return
        folder = self.lib.data.get("folder") or ""
        if not os.path.isdir(folder):
            folder = filedialog.askdirectory(parent=self.root, title="Chọn thư mục học để lưu tài liệu tải về")
            if not folder:
                return
        url = fix_source_url(self.lib.data["source_url"])
        source_log(f"Source start: saved={self.lib.data['source_url']} open={url} "
                   f"clicks={self.lib.data.get('source_clicks')} folder={folder} library={self.lib.path}")
        if url != self.lib.data["source_url"]:               # a sign-in page was saved by v1.9.10: repair it
            with self.lib.lock:
                self.lib.data["source_url"] = url
            try:
                save_library(self.lib, "repaired link")
            except Exception:  # noqa: BLE001
                pass
        out = os.path.join(A.DATA_DIR, "learning", f"source_page_{os.getpid()}.json")
        profile = source_profile()                                       # keeps the site login
        clicks = json.dumps(self.lib.data.get("source_clicks") or [], ensure_ascii=False)
        try:
            os.remove(out)
        except OSError:
            pass
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--source-browser", url, out, profile, clicks]
        else:
            cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(A.__file__)), "app.py"),
                   "--source-browser", url, out, profile, clicks]
        self.syncing = True
        self.chat_note(f"Đã mở {url} trong cửa sổ Source. Nếu đã đăng nhập từ lần trước, app tự tải sau vài giây "
                       f"và tự đóng cửa sổ. Lần đầu: đăng nhập, mở trang chứa tài liệu (Materials), rồi bấm nút xanh "
                       f"“📥 Tải tài liệu mới từ trang này” ở góc dưới — lần sau app nhớ trang đó và làm tự động.")
        self.mw.busy("source", "Đang chờ bạn mở trang tài liệu trong cửa sổ Source...")

        def progress(msg):
            self.later(lambda: self.mw.busy("source", msg))

        def work():
            got, have_n, dup, problems, links, r = [], 0, 0, [], [], None
            page = None
            try:
                if not wait_profile_free(profile):       # the last Source window may still be closing
                    problems.append("cửa sổ Source trước vẫn đang mở — hãy đóng nó rồi bấm Source lại")
                    return
                try:
                    proc = subprocess.Popen(cmd)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"không mở được cửa sổ Source: {e}")
                    return
                proc.wait()
                if os.path.exists(out):
                    with open(out, encoding="utf-8") as f:
                        page = json.load(f)
                    os.remove(out)
                if page is None:
                    self.later(lambda: (self.mw.done("source"),
                                        self.chat_note("Đã đóng cửa sổ Source mà chưa bấm “Tải tài liệu mới” — "
                                                       "không tải gì.")))
                    return
                if page.get("error"):
                    problems.append(page["error"])
                    return
                save_source_debug(page)
                new_url = page.get("url") or url
                if is_login_url(new_url):                    # never keep or scan a sign-in page
                    problems.append("đó là trang đăng nhập — hãy đăng nhập, bấm tab Materials rồi bấm nút")
                    return
                with self.lib.lock:                          # next time: open this page, replay the clicks
                    if page.get("clicks"):
                        self.lib.data["source_clicks"] = page["clicks"]   # e.g. the "Materials" tab
                    elif page.get("scope") == "panel" and page.get("tab"):
                        self.lib.data["source_clicks"] = [{"text": page["tab"], "role": "tab", "tag": "A"}]
                    elif page.get("scope") == "page" and not [c for c in (self.lib.data.get("source_clicks") or [])
                                                              if c.get("text") != "__page__"]:
                        # no tabs: the files are on the page itself (a saved tab click is never replaced by this:
                        # one scan that found nothing must not make the app stop opening the tab)
                        self.lib.data["source_clicks"] = [{"text": "__page__"}]
                    elif new_url != url:
                        self.lib.data["source_clicks"] = []
                    self.lib.data["source_url"] = new_url
                save_library(self.lib, "page and clicks")    # the page and its clicks are kept right away
                progress("Đang tìm tài liệu trên trang...")
                if page.get("scope") == "page":              # no tab content found: this site's links, and
                    from urllib.parse import urlparse        # download addresses (e.g. its file server)
                    host = (urlparse(new_url).hostname or "").lower()
                    page["links"] = [l for l in page.get("links") or []
                                     if (urlparse(l.get("href") or "").hostname or "").lower() == host
                                     or re.search(r"/sfc/|servlet\.shepherd|/download", l.get("href") or "", re.I)]
                opener = login_opener(page)                  # signed in like the window, on every site
                links = browser_doc_links(page, SOURCE_EXTS & supported_exts(), opener)
                known = self.lib.data.setdefault("source_files", {})
                # what is already in the study folder (any sub-folder): by file name and by content
                names = set()
                for base, dirs, files in os.walk(folder):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    names.update(n.lower() for n in files)
                have = {d.get("sha") for d in self.lib.docs()}
                todo = []
                for link, text, ext, name in links:
                    k = known.get(link)
                    if (k and (k.get("name", "").lower() in names or k.get("sha") in have)) or \
                            (name and safe_name(name).lower() in names):    # in the folder, or a known duplicate
                        have_n += 1
                    else:
                        todo.append((link, text, ext))
                for k, (link, text, ext) in enumerate(todo, 1):
                    progress(f"Đang tải tài liệu mới {k}/{len(todo)} từ Source...")
                    try:
                        path, sha, created = download(link, folder, ext, login_headers(page, link), opener)
                    except Exception as e:  # noqa: BLE001
                        problems.append(f"{text or link}: {e}")
                        continue
                    with self.lib.lock:
                        known[link] = {"name": os.path.basename(path), "sha": sha,
                                       "time": time.strftime("%Y-%m-%d %H:%M")}
                    if sha in have or not created:
                        dup += 1                                 # same content as a document already there
                        if created:
                            try:
                                os.remove(path)                  # no second copy in the folder
                            except OSError:
                                pass
                    else:
                        have.add(sha)
                        got.append(os.path.basename(path))
                with self.lib.lock:
                    self.lib.data["source_checked"] = time.strftime("%Y-%m-%d %H:%M")
                self.lib.save()
                if got:                                          # upload the new files to Claude and catalogue them
                    r = Syncer(self.lib, self.mw.client, progress).run(folder)
                    problems += r["problems"]
            except Exception as e:  # noqa: BLE001
                import traceback
                source_log(f"Source error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                problems.append(A.friendly_error(e) if isinstance(e, anthropic.APIError) else f"{type(e).__name__}: {e}")
            finally:
                self.syncing = False
                source_log(f"Source end: found={len(links)} already={have_n} downloaded={got} problems={problems}")
                if page is not None:                         # what happened, for checking a real site later
                    save_source_debug(page, {"found": len(links), "already": have_n, "downloaded": got,
                                             "duplicates": dup, "problems": problems})
                if page is None and not problems:
                    return
            where = (page or {}).get("title") or (page or {}).get("url") or url
            msg = (f"Source ({where}): {len(links)} tài liệu trên trang"
                   + (f", {have_n} đã có trong thư mục học" if have_n else "") + "; "
                   + (f"{len(got)} tài liệu mới đã tải về {folder} và upload cho Claude:\n" +
                      "\n".join(f"  • {n}" for n in got) if got else "không có tài liệu mới.")
                   + (f"\n{dup} file trùng nội dung với tài liệu đã có (bỏ qua)." if dup else ""))
            if problems:
                msg += "\nLưu ý:\n" + "\n".join("• " + p for p in problems[:10])
            self.later(lambda: (self.mw.done("source"), self.chat_note(msg),
                                self.mw.status.config(text=f"🌐 Source: +{len(got)} tài liệu mới", fg="#7ee2a8")))
        threading.Thread(target=work, daemon=True).start()

    # ---- Agenda ----
    def agenda_key(self):
        """Changes when the library's content changes (files added/removed, days/dates/titles re-catalogued):
        the saved agenda is then out of date. A Refresh that finds nothing new keeps it."""
        parts = sorted(f"{d['id']}:{d.get('day')}:{d.get('date')}:{d.get('kind')}:{d.get('title')}"
                       for d in self.lib.docs())
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]

    # ---- saved answers: Agenda, "ask about this document", book-only summaries ----
    def saved_answer(self, name, key):
        """A saved answer if it is still valid for `key` (changes when what the answer depends on changes)."""
        c = (self.lib.data.get("answers") or {}).get(name)
        return c if c and c.get("key") == key and c.get("text") else None

    def save_answer(self, name, key, text):
        with self.lib.lock:
            self.lib.data.setdefault("answers", {})[name] = {"key": key, "text": text,
                                                             "time": time.strftime("%d/%m %H:%M")}
        self.lib.save()

    def show_saved(self, title, c, redo_hint):
        """Show a saved answer in the answers pane at once (no Claude call); file names stay links."""
        w = self.mw.chat
        w.configure(state="normal")
        if w.index("end-1c") != "1.0":
            w.insert("end", "\n\n")
        w.insert("end", f"🎓 {title} ", "who")
        w.insert("end", f"(đã lưu lúc {c['time']} — {redo_hint})\n", "file")
        w.mark_set("answer_start", "end-1c")
        w.mark_gravity("answer_start", "left")
        w.insert("end", c["text"])
        w.see("end")
        w.configure(state="disabled")
        try:
            self.linkify()
        except tk.TclError:
            pass
        if not self.mw.panel_open:
            self.mw.toggle_panel("right")
        # Claude can still answer follow-up questions about it
        q = f"{title} (đã lưu)"
        self.mw.chatter.history += [{"role": "user", "content": q}, {"role": "assistant", "content": c["text"]}]
        self.mw.chat_log.append({"q": q, "files": [], "a": c["text"]})

    def cached_agenda(self):
        return self.saved_answer("agenda", self.agenda_key())

    def agenda(self, fresh=False):
        cached = None if fresh else self.cached_agenda()
        if cached:
            self.show_saved("Agenda", cached, "muốn tạo lại: menu Learning → 🔄 Tạo lại Agenda")
            return
        docs = self.lib.docs()
        ids = [d["id"] for d in docs if d.get("kind") == "agenda"]
        # the first part of every document (cover page: title, teacher, date, session) helps place it in the agenda
        heads = "\n\n".join(
            f"[{d['name']}] buổi {d.get('day') or '?'} · {d.get('title') or ''}\n{self.lib.text(d)[:1200].strip()}"
            for d in docs if d.get("kind") not in ("journal_template", "certificate"))
        src = "tài liệu Agenda đính kèm, phần đầu của từng tài liệu" if ids else "phần đầu của từng tài liệu"
        self.set_prompt(
            f"Hãy lập Agenda chi tiết của khóa học dựa trên {src} và nội dung thầy giảng (nếu có), phân cấp "
            "Ngày → Buổi (sáng/chiều/tối) → Phiên. Mỗi phiên ghi: giờ (nếu có), chủ đề, tên giảng viên, và các tài "
            "liệu dùng trong phiên đó — ghi đúng tên file trong thư viện (kể cả đuôi .pdf/.docx) để bấm mở được. "
            "Trình bày đúng mẫu sau, không thêm giải thích; thông tin nào không có thì ghi \"—\":\n"
            "📅 Ngày 1 · Thứ Hai 10/11/2026\n"
            "  ▸ Buổi sáng\n"
            "    • Phiên 1 · 08:30–10:00 · Chủ đề — GV: Tên giảng viên\n"
            "        📄 Ten_file.pdf",
            {"kind": "agenda", "doc_ids": ids, "label": f"Agenda chi tiết · {len(docs)} tài liệu",
             "save_as": ("agenda", self.agenda_key()),
             "blocks": [{"type": "text", "text": f"<document_heads>\n{heads}\n</document_heads>"}] if heads else []})

    # ---- file names in Claude's answers become links ----
    def linkify(self):
        """Turn library file names in the latest answer into links that open the file."""
        w = self.mw.chat
        try:
            start = w.index("answer_start")
        except tk.TclError:
            return
        w.tag_configure("doclink", foreground="#1a5fb4", underline=True)
        w.tag_bind("doclink", "<Enter>", lambda e: w.config(cursor="hand2"))
        w.tag_bind("doclink", "<Leave>", lambda e: w.config(cursor=""))
        count = tk.IntVar()
        for d in sorted(self.lib.docs(), key=lambda d: -len(d["name"])):   # longest names first
            idx = start
            while True:
                idx = w.search(unicodedata.normalize("NFC", d["name"]), idx, stopindex="end", nocase=True,
                               count=count)
                if not idx or not count.get():
                    break
                end = f"{idx}+{count.get()}c"
                if "doclink" not in w.tag_names(idx):
                    tag = f"doc_{d['id']}"
                    w.tag_add("doclink", idx, end)
                    w.tag_add(tag, idx, end)
                    w.tag_bind(tag, "<Button-1>", lambda e, d=d: open_path(self.lib.file_path(d)))
                idx = end

    # ---- Summary ----
    def summary(self):
        """Dialog: mode (book only / book + teacher), course day, and the exact book files to use."""
        books = [d for d in self.lib.docs() if d.get("kind") not in ("journal_template", "certificate")]
        days = self.lib.days()
        top = self._dialog("Summary — tổng kết", "760x560")
        bold = (A.UI_FONT, 10, "bold")
        tk.Label(top, text="Cách tổng kết:", font=bold, bg="white").pack(anchor="w", padx=14, pady=(12, 2))
        mode = tk.StringVar(value="book")
        for val, txt in (("book", "Theo file sách đã upload (chọn file bên dưới)"),
                         ("mix", "Kết hợp sách + nội dung thầy giảng trên màn hình")):
            tk.Radiobutton(top, text=txt, value=val, variable=mode, bg="white",
                           font=(A.UI_FONT, 10)).pack(anchor="w", padx=22)
        tk.Label(top, text="Buổi học:", font=bold, bg="white").pack(anchor="w", padx=14, pady=(10, 2))
        day_labels = ["Tự chọn tài liệu (không theo buổi)"] + [
            f"Buổi {day}" + (f" · {fmt_date(date)}" if date else "") for day, date, _ in days]
        day_var = tk.StringVar(value=day_labels[-1])
        ttk.Combobox(top, textvariable=day_var, values=day_labels, state="readonly", width=50).pack(anchor="w", padx=14)
        tk.Label(top, text=f"File sách dùng để tổng kết (giữ {MULTI} hoặc Shift để chọn nhiều file):", font=bold,
                 bg="white").pack(anchor="w", padx=14, pady=(10, 2))
        tree = ttk.Treeview(top, columns=("day", "title", "file"), show="headings", selectmode="extended", height=9)
        for col, txt, w in (("day", "Buổi", 50), ("title", "Tên tài liệu", 330), ("file", "Tên file", 300)):
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor="w")
        for d in books:
            tree.insert("", "end", iid=d["id"], values=(d.get("day") or "-", d.get("title") or d["name"], d["name"]))
        tree.pack(fill="both", expand=True, padx=14)
        count = tk.Label(top, text="", bg="white", fg="#666", font=(A.UI_FONT, 9))
        count.pack(anchor="w", padx=14)
        tree.bind("<<TreeviewSelect>>", lambda e: count.config(text=f"Đã chọn {len(tree.selection())} file"))

        def day_changed(*_):
            i = day_labels.index(day_var.get())
            if i == 0:
                tree.selection_set(())
                return
            ids = [d["id"] for d in days[i - 1][2] if d["id"] in tree.get_children()]
            tree.selection_set(ids)
            if ids:
                tree.see(ids[0])
        day_var.trace_add("write", day_changed)
        day_changed()
        result = []
        fresh = tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="Tạo mới (không dùng bản tổng kết đã lưu)", variable=fresh, bg="white",
                       font=(A.UI_FONT, 9)).pack(anchor="w", padx=14)

        def ok():
            if not tree.selection():
                messagebox.showinfo("Summary", "Hãy chọn ít nhất 1 file sách.", parent=top)
                return
            result.append((day_labels.index(day_var.get()), list(tree.selection()), mode.get()))
            top.destroy()
        self._buttons(top, ok)
        top.wait_window()
        if not result:
            return
        di, ids, m = result[0]
        # a book-only summary depends only on the chosen files: reuse it (with the teacher it changes live)
        name = "summary:" + ",".join(sorted(ids)) + f":{di}"
        if m == "book" and not fresh.get():
            saved = self.saved_answer(name, "v1")
            if saved:
                self.show_saved("Summary (theo sách)", saved, "muốn tạo mới: Summary → tích \"Tạo mới\"")
                return
        chosen = [self.lib.get(i) for i in ids]
        names = "; ".join(f"\"{d.get('title') or d['name']}\" ({d['name']})" for d in chosen)
        when = ""
        if di:
            day, date, _ = days[di - 1]
            when = f"Buổi {day}" + (f" (ngày {fmt_date(date)})" if date else "")
        if m == "book":
            text = (f"Hãy tổng kết {('nội dung ' + when) if when else 'nội dung'} chỉ dựa trên các file sách sau: "
                    f"{names}. Gồm: các ý chính, khái niệm quan trọng, ví dụ, bài tập và những điểm cần ghi nhớ.")
        else:
            text = (f"Hãy tổng kết {when or 'buổi học'} kết hợp các file sách ({names}) với nội dung thầy giảng "
                    "(bản ghi lời giảng và bản dịch đang có trên màn hình); ghi rõ ý nào từ sách, ý nào thầy nhấn "
                    "mạnh hoặc bổ sung. Gồm: các ý chính, khái niệm quan trọng, ví dụ, bài tập đã giao và những "
                    "điểm cần ghi nhớ.")
        self.set_prompt(text, {"kind": "summary", "doc_ids": ids, "label": (
            f"Summary · {when + ' · ' if when else ''}{'Sách' if m == 'book' else 'Sách + thầy giảng'} · {len(ids)} file"),
            **({"save_as": (name, "v1")} if m == "book" else {})})

    def pick_day(self, title, with_mode=False):
        """Small dialog: choose the course day. Returns (day, date, docs, mode) or None."""
        days = self.lib.days()
        choices = [(day, date, ds, f"Buổi {day}" + (f" · {fmt_date(date)}" if date else "") + " · " +
                    ", ".join((d.get("title") or d["name"])[:40] for d in ds[:3]) + (" ..." if len(ds) > 3 else ""))
                   for day, date, ds in days]
        choices.append((0, "", self.lib.docs(), "Toàn khóa (tất cả tài liệu)"))
        top = self._dialog(title)
        tk.Label(top, text="Chọn buổi học:", font=(A.UI_FONT, 10, "bold"), bg="white").pack(anchor="w", padx=14, pady=(12, 4))
        var = tk.StringVar(value=choices[-2][3] if len(choices) > 1 else choices[-1][3])
        cb = ttk.Combobox(top, textvariable=var, values=[c[3] for c in choices], state="readonly", width=70)
        cb.pack(fill="x", padx=14)
        mode = tk.StringVar(value="mix")
        if with_mode:
            tk.Label(top, text="Cách tổng kết:", font=(A.UI_FONT, 10, "bold"), bg="white").pack(anchor="w", padx=14, pady=(12, 2))
            for val, txt in (("book", "Theo file sách đã upload"),
                             ("mix", "Kết hợp sách + nội dung thầy giảng trên màn hình")):
                tk.Radiobutton(top, text=txt, value=val, variable=mode, bg="white",
                               font=(A.UI_FONT, 10)).pack(anchor="w", padx=22)
        result = []
        self._buttons(top, lambda: (result.append(var.get()), top.destroy()))
        top.wait_window()
        if not result:
            return None
        c = next(c for c in choices if c[3] == result[0])
        return c[0], c[1], c[2], mode.get()

    # ---- Exercises ----
    def exercises(self):
        if self.session and not messagebox.askyesno(
                "Exercises", f"Đang làm bài \"{self.session['ex']['title']}\" (bước {self.session['i'] + 1}/"
                f"{len(self.session['ex']['steps'])}).\nDừng bài này để chọn bài khác?", parent=self.root):
            return
        self.session = None
        top = self._dialog("Exercises — bài tập", "900x600", modal=False)
        bold = (A.UI_FONT, 10, "bold")
        head = tk.Frame(top, bg="white")
        head.pack(fill="x", padx=12, pady=(10, 4))
        mode = tk.StringVar(value=self.lib.data.get("ex_mode", "big"))      # the mode used last time
        tk.Radiobutton(head, text="Bài tập lớn (đọc toàn bộ thư mục, theo các bước)", value="big", variable=mode,
                       bg="white", font=bold).pack(side="left")
        tk.Radiobutton(head, text="Theo file bài tập (chọn file cụ thể)", value="doc", variable=mode, bg="white",
                       font=(A.UI_FONT, 10)).pack(side="left", padx=14)
        status = tk.Label(top, text="", bg="white", fg="#666", font=(A.UI_FONT, 9), anchor="w")
        status.pack(fill="x", padx=12)
        tree = ttk.Treeview(top, columns=("day", "title", "steps", "docs"), show="tree headings", height=10)
        tree.column("#0", width=28, stretch=False)
        for col, txt, w in (("day", "Buổi", 50), ("title", "Bài tập", 380), ("steps", "Số bước", 70),
                            ("docs", "Tài liệu / file", 330)):
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor="w", stretch=col in ("title", "docs"))
        tree.tag_configure("file", font=(A.UI_FONT, 10, "bold"))
        tree.pack(fill="both", expand=True, padx=12, pady=4)
        detail = tk.Text(top, height=8, wrap="word", font=(A.UI_FONT, 9), bd=1, relief="solid", bg="#fbf8f6")
        detail.pack(fill="x", padx=12)
        btns = tk.Frame(top, bg="white")
        btns.pack(fill="x", padx=12, pady=8)
        full = tk.BooleanVar(value=False)
        items = {}                 # tree row id -> {"kind": "big"|"file"|"ex", ...}
        busy = {"scan": False}

        def alive():
            return top.winfo_exists()

        def file_exercises(d):
            """Exercises found in one file: the ones with steps (folder scan) first, then the others."""
            out = [dict(e) for e in d.get("bigex") or []]
            seen = {e["title"].strip().lower() for e in out}
            out += [{"title": e["title"], "goal": e.get("summary", ""), "location": e.get("location", ""), "steps": []}
                    for e in d.get("exercises") or [] if e["title"].strip().lower() not in seen]
            return out

        def show_status():
            if mode.get() == "big":
                upd = self.lib.data.get("big_exercises_updated")
                n = len(items)
                status.config(text=f"{n} bài tập lớn · cập nhật {upd}" if upd else
                              "Chưa có danh sách — nhấn 🔄 Refresh để Claude đọc toàn bộ thư mục.")
            else:
                status.config(text="Chọn dòng 📄 (cả file: làm lần lượt mọi bài con) hoặc một dòng ✏ (một bài con), "
                                   "rồi bấm ▶ Bắt đầu.")

        def fill(*_):
            tree.delete(*tree.get_children())
            items.clear()
            if mode.get() == "big":
                for k, ex in enumerate(self.lib.data.get("big_exercises", [])):
                    docs = [self.lib.get(i) for i in ex.get("doc_ids", []) if self.lib.get(i)]
                    iid = f"b{k}"
                    items[iid] = {"kind": "big", "ex": ex}
                    tree.insert("", "end", iid=iid, values=(
                        ex.get("day") or "-", ex["title"], len(ex["steps"]),
                        ", ".join(d.get("title") or d["name"] for d in docs)))
            else:
                for k, d in enumerate(self.lib.docs()):
                    if d.get("kind") in ("journal_template", "certificate", "agenda"):
                        continue
                    exs = file_exercises(d)
                    fid = f"f{k}"
                    items[fid] = {"kind": "file", "doc": d, "exs": exs}
                    # parent = the whole file, children = its sub-exercises, shown open: either can be started
                    tree.insert("", "end", iid=fid, tags=("file",), open=True, values=(
                        d.get("day") or "-", "📄 " + (d.get("title") or d["name"]),
                        f"{len(exs)} bài con" if exs else "-", d["name"]))
                    for j, ex in enumerate(exs):
                        eid = f"{fid}_{j}"
                        items[eid] = {"kind": "ex", "doc": d, "ex": ex}
                        tree.insert(fid, "end", iid=eid, values=(
                            d.get("day") or "-", f"✏ {j + 1}. {ex['title']}",
                            f"{len(ex['steps'])} bước" if ex["steps"] else "-", d["name"]))
            detail.delete("1.0", "end")
            show_status()
            refresh_btn.config(state="disabled" if busy["scan"] else "normal")

        def prepare(it):
            """Start working out the steps in the background as soon as an item is selected, so that
            ▶ Bắt đầu shows the first prompt at once (the result is saved for next time too)."""
            if it["kind"] == "file" and not known_file_plan(it["doc"]):
                d = it["doc"]
                self.prefetch(f"file:{d['id']}",
                              lambda: plan_file(self.lib, self.mw.client, d, [e["title"] for e in it["exs"]]))
            elif it["kind"] == "ex":
                d, e = it["doc"], it["ex"]
                saved = (d.get("plans") or {}).get(e["title"])
                if not (isinstance(saved, dict) and saved.get("v") == 2):
                    self.prefetch(f"ex:{d['id']}:{e['title']}", lambda: plan_steps(self.lib, self.mw.client, {
                        "title": e["title"], "summary": e.get("goal", ""), "location": e.get("location", ""),
                        "doc": d}))

        def progress_note(ex):
            saved = (self.lib.data.get("sessions") or {}).get(exercise_key(ex))
            if saved and not saved.get("done") and saved.get("i", -1) >= 0:
                return f"\n\n⏸ Đang làm dở: đã xong {saved['i']}/{len(ex['steps'])} bước — Bắt đầu sẽ làm tiếp."
            if saved and saved.get("done"):
                return "\n\n✓ Đã làm xong bài này trước đây (Bắt đầu sẽ làm lại, tóm tắt bài lấy từ bản đã lưu)."
            return ""

        def on_select(*_):
            detail.delete("1.0", "end")
            if not tree.selection():
                return
            it = items[tree.selection()[0]]
            if it["kind"] != "big":
                prepare(it)
            if it["kind"] == "file":
                d, exs = it["doc"], it["exs"]
                kp = known_file_plan(d)
                if kp:
                    detail.insert("end", progress_note(file_exercise(d, kp[0], kp[1], kp[2])).strip() + "\n")
                tree.item(tree.selection()[0], open=True)
                detail.insert("end", f"{d.get('description', '')}\n\n" + (
                    "Bài tập trong file:\n" + "\n".join(f"  • {e['title']}" for e in exs) if exs else
                    "Chưa thấy bài tập riêng trong file (nhấn 🔄 Refresh để Claude đọc kỹ file).") +
                    "\n\n▶ Bắt đầu = làm lần lượt toàn bộ bài tập/hướng dẫn trong file này.")
            else:
                ex = it["ex"]
                detail.insert("end", (ex.get("goal") or "") + (f"  ({ex['location']})" if ex.get("location") else ""))
                if ex.get("steps"):
                    detail.insert("end", "\n\n" + "\n".join(
                        f"{k}. {s['title']} — {s['instruction']}" for k, s in enumerate(ex["steps"], 1)))
                    full_ex = ex if it["kind"] == "big" else single_exercise(it["doc"], ex["title"],
                                                                              ex.get("goal", ""), ex["steps"])
                    detail.insert("end", progress_note(full_ex))
                else:
                    detail.insert("end", "\n\n(Đang chuẩn bị các bước ở chế độ nền...)")

        def refresh():
            busy["scan"] = True
            refresh_btn.config(state="disabled")

            def progress(msg):
                self.later(lambda: self.mw.busy("scan", msg))
                self.later(lambda: alive() and status.config(text="⏳ " + msg))

            def work():
                try:
                    n, problems = scan_exercises(self.lib, self.mw.client, progress, full=full.get())
                    note = f"Đã cập nhật danh sách: {n} bài tập lớn." + (
                        "\nLưu ý:\n" + "\n".join("• " + p for p in problems[:8]) if problems else "")
                    self.later(lambda: self.chat_note(note))
                except Exception as e:  # noqa: BLE001
                    why = A.friendly_error(e)
                    self.later(lambda: self.chat_note(f"Chưa cập nhật được danh sách bài tập: {why}", "err"))
                finally:
                    busy["scan"] = False
                    self.later(lambda: self.mw.done("scan"))
                    self.later(lambda: alive() and fill())
            threading.Thread(target=work, daemon=True).start()

        def start():
            if not tree.selection():
                return
            it = items[tree.selection()[0]]
            redo = restart.get()
            if it["kind"] == "big":
                top.destroy()
                self.start_session(it["ex"], restart=redo)
                return
            d = it["doc"]
            top.destroy()                  # close right away: progress shows in the Claude pane
            if it["kind"] == "file":       # the whole file: one big problem, sub-exercise by sub-exercise
                prep = f"file:{d['id']}"
                known = known_file_plan(d)
                if known:                  # steps already known (saved): the first prompt appears at once
                    self.start_session(file_exercise(d, *known), restart=redo)
                    return
                self.chat_note(f"Đang chuẩn bị các bài tập con và các bước của file \"{d['name']}\"... "
                               "Câu prompt đầu tiên sẽ hiện trong ô nhập khi xong.")

                def plan():
                    self.wait_prefetch(prep)          # it may already be on its way since the file was selected
                    known = known_file_plan(d)
                    goal, steps, params = known if known else plan_file(self.lib, self.mw.client, d,
                                                                        [e["title"] for e in it["exs"]])
                    if not steps:
                        raise ValueError("Claude không tìm thấy bài tập trong file")
                    return file_exercise(d, goal, steps, params)
            else:
                prep = f"ex:{d['id']}:{it['ex']['title']}"
                ex = {"title": it["ex"]["title"], "summary": it["ex"].get("goal", ""),
                      "location": it["ex"].get("location", ""), "doc": d}
                saved = (d.get("plans") or {}).get(ex["title"])
                if not (isinstance(saved, dict) and saved.get("v") == 2):
                    self.chat_note(f"Đang chuẩn bị bài \"{ex['title']}\" (các bước và prompt của sách)... "
                                   "Câu prompt đầu tiên sẽ hiện trong ô nhập khi xong.")

                def plan():
                    self.wait_prefetch(prep)
                    steps, params = plan_steps(self.lib, self.mw.client, ex)      # uses the saved plan if any
                    if not steps:
                        raise ValueError("Claude không trả về các bước")
                    return single_exercise(d, ex["title"], ex.get("summary", ""), steps, params)
            self.mw.status.config(text="🎓 Đang chuẩn bị bài tập...", fg="#ddd")

            def work():
                try:
                    self.later(lambda: self.mw.busy("prepare", "Claude đang đọc file và chuẩn bị các bước bài tập..."))
                    sess = plan()
                    self.later(lambda: (self.mw.status.config(text="🎓 Bài tập đã sẵn sàng", fg="#7ee2a8"),
                                        self.start_session(sess, restart=redo)))
                except Exception as e:  # noqa: BLE001
                    why = A.friendly_error(e) if isinstance(e, anthropic.APIError) or "timed out" in str(e) \
                        else str(e)
                    self.later(lambda: self.chat_note(f"Chưa chuẩn bị được bài tập: {why}", "err"))
                finally:
                    self.later(lambda: self.mw.done("prepare"))
            threading.Thread(target=work, daemon=True).start()

        st = dict(font=(A.UI_FONT, 10), relief="flat", cursor="hand2", padx=12, pady=3)
        refresh_btn = tk.Button(btns, text="🔄 Refresh danh sách", command=refresh, bg="#eee", **st)
        refresh_btn.pack(side="left")
        tk.Checkbutton(btns, text="Đọc lại toàn bộ file", variable=full, bg="white",
                       font=(A.UI_FONT, 9)).pack(side="left", padx=6)
        restart = tk.BooleanVar(value=False)
        tk.Checkbutton(btns, text="Làm lại từ đầu", variable=restart, bg="white",
                       font=(A.UI_FONT, 9)).pack(side="left", padx=6)
        tk.Button(btns, text="Đóng", command=top.destroy, bg="#eee", **st).pack(side="right")
        start_btn = tk.Button(btns, text="▶ Bắt đầu bài tập", command=start, bg="#d97757", fg="white",
                              activebackground="#c4643f", **st)
        start_btn.pack(side="right", padx=6)
        tree.bind("<<TreeviewSelect>>", on_select)
        tree.bind("<Double-1>", lambda e: start())
        mode.trace_add("write", fill)
        mode.trace_add("write", lambda *_: self.lib.data.__setitem__("ex_mode", mode.get()))
        fill()
        if not self.lib.data.get("big_exercises_updated"):
            refresh()                          # first time: read the folder right away

    # ---- guided exercise: short overview, then one prompt per step (asking the learner for inputs when needed,
    # giving ready-to-use prompts), then the solution summary + PDF ----
    # background preparation of exercise steps (started when an item is selected in the Exercises list)
    def prefetch(self, key, fn):
        jobs = self.__dict__.setdefault("_prep", {})
        if key in jobs:
            return
        done = threading.Event()
        jobs[key] = done
        self.mw.busy(f"prep:{key}", "Đang chuẩn bị bài tập ở chế độ nền (Claude đọc file)...")

        def work():
            try:
                fn()
            except Exception:  # noqa: BLE001  (▶ Bắt đầu tries again and shows the error)
                pass
            finally:
                done.set()
                jobs.pop(key, None)
                self.later(lambda: self.mw.done(f"prep:{key}"))
        threading.Thread(target=work, daemon=True).start()

    def wait_prefetch(self, key):
        done = self.__dict__.get("_prep", {}).get(key)
        if done:
            done.wait(330)

    def save_session(self, done=False):
        """Keep the exercise's progress (steps done, answers, overview) so it can be resumed later."""
        s = self.session
        if not s:
            return
        with self.lib.lock:
            self.lib.data.setdefault("sessions", {})[s["key"]] = {
                "ex": s["ex"], "i": s["i"], "log": s["log"], "overview": s.get("overview", ""),
                "awaiting": s.get("awaiting"), "carry": s.get("carry"), "next_inputs": s.get("next_inputs"),
                "next_draft": s.get("next_draft"), "choices": s.get("choices", {}),
                "choice_values": s.get("choice_values", {}), "slot_values": s.get("slot_values", {}),
                "outputs": s.get("outputs", {}),
                "done": done, "time": time.strftime("%d/%m %H:%M")}
        self.lib.save()

    def start_session(self, ex, restart=False):
        key = exercise_key(ex)
        saved = (self.lib.data.get("sessions") or {}).get(key) or {}
        steps = ex["steps"]
        if saved and not restart and not saved.get("done") and saved.get("i", -1) >= 0:
            # an exercise opened before: carry on where it stopped
            self.session = {"ex": ex, "i": min(saved["i"], len(steps)), "log": saved.get("log", []),
                            "overview": saved.get("overview", ""), "key": key, "resume": True}
            for k in ("awaiting", "carry", "next_inputs", "next_draft", "choices", "choice_values", "slot_values",
                      "outputs"):
                if saved.get(k):
                    self.session[k] = saved[k]
            k = self.session["i"]
            self.chat_note(f"Tiếp tục bài \"{ex['title']}\" (đã lưu lúc {saved.get('time', '')}): đã xong {k}/"
                           f"{len(steps)} bước. Muốn làm lại từ đầu: Exercises → tích \"Làm lại từ đầu\"."
                           + (f"\nTóm tắt bài:\n{self.session['overview']}" if self.session["overview"] else ""))
            if k >= len(steps):
                self.summary_prompt(send=False)
            elif self.session.get("awaiting"):
                self.prompt_inputs(self.session["awaiting"])
            else:
                self.prompt_step()
            return
        self.session = {"ex": ex, "i": -1, "log": [], "overview": "", "key": key,     # i = -1: the overview first
                        "choices": {}, "choice_values": {}, "slot_values": {}, "outputs": {}}
        if steps and steps[0].get("part"):
            lines = [f"  Bài {st['part']}/{st['parts']}: {st['part_title']} ({st['psteps']} bước)"
                     for st in steps if st["pstep"] == 1]
            self.chat_note(f"Bắt đầu bài toán \"{ex['title']}\" — {steps[0]['parts']} bài tập con, "
                           f"{len(steps)} bước:\n" + "\n".join(lines))
        else:
            self.chat_note(f"Bắt đầu bài tập \"{ex['title']}\" — {len(steps)} bước:\n" + "\n".join(
                f"  {k}. {s['title']}" for k, s in enumerate(steps, 1)))
        if saved.get("overview"):                          # the overview was written before: no need to ask again
            self.session.update(overview=saved["overview"], i=0)
            self.chat_note(f"Tóm tắt bài (đã lưu):\n{saved['overview']}")
            self.save_session()
            self.prompt_step()
            return
        self.save_session()
        self.prompt_step()
        self.root.after(300, self.mw.send_prompt)          # the overview is asked for right away

    def _resume_blocks(self):
        """After a resume Claude has not seen the earlier steps: give it a short recap (with the documents)."""
        s = self.session
        if not s.get("resume"):
            return []
        recap = [f"Tóm tắt bài: {s.get('overview', '')}"] + [
            f"[{self._step_name(e['i'])}: {e['step']['title']}]"
            + (f"\nThông tin của học viên: {e['q']}" if e.get("input") else "") + f"\nKết quả: {e['a'][:1500]}"
            for e in s["log"]]
        return [{"type": "text", "text": "<previous_steps>\n" + "\n\n".join(recap) + "\n</previous_steps>"}]

    def stop_session(self):
        if self.session:
            self.save_session()
            self.chat_note(f"Đã dừng bài tập \"{self.session['ex']['title']}\" — tiến độ đã được lưu, "
                           "mở lại bài này trong Exercises để làm tiếp.")
        self.session = None
        if self.job and self.job["kind"] in ("exstep", "exsummary"):
            self.job = None
            self.mw.prompt.delete("1.0", "end")
            self.mw._refresh_attach_bar()

    def _step_name(self, i):
        ex = self.session["ex"]
        st = ex["steps"][i]
        if st.get("part"):
            return f"Bài {st['part']}/{st['parts']} · Bước {st['pstep']}/{st['psteps']}"
        return f"Bước {i + 1}/{len(ex['steps'])}"

    def _next_step_block(self, i):
        """Tells Claude what the next step is, so it can say what to carry over and what to ask the learner."""
        steps = self.session["ex"]["steps"]
        if i + 1 >= len(steps):
            return {"type": "text", "text": "<next_step>(không còn — đây là bước cuối)</next_step>"}
        st = steps[i + 1]
        return {"type": "text", "text": f"<next_step>{self._step_name(i + 1)}: {st['title']} — "
                                        f"{st['instruction']}</next_step>"}

    def prompt_step(self):
        s = self.session
        ex, i = s["ex"], s["i"]
        if i < 0:                                          # a 2-3 line overview first
            self.set_prompt(
                f"Bài tập \"{ex['title']}\": tóm tắt rất ngắn (2-3 dòng) đề bài, mục tiêu và kết quả cần có.",
                {"kind": "exstep", "step": -1, "doc_ids": ex.get("doc_ids", []),
                 "label": f"Bài tập · Tóm tắt · {ex['title'][:30]}",
                 "blocks": [{"type": "text", "text": OVERVIEW_RULES}, self._next_step_block(-1)]})
            return
        step = ex["steps"][i]
        new_part = bool(step.get("part")) and step["pstep"] == 1
        head = f"{self._step_name(i)}"
        if step.get("part"):
            head = f"{step['part_title']} — {head}"
        lines = [f"{head}: {step['title']}.", step["instruction"]]
        if new_part and step.get("part_goal"):
            lines.append(f"Mục tiêu bài này: {step['part_goal']}")
        draft = s.get("next_draft")
        if step.get("prompt_template"):
            # the course's own prompt: the app fills its blanks itself (the learner's choice, earlier results,
            # values the learner gave once) - nothing to copy and paste
            self.ensure_inputs(step)
            draft = self.fill_template(step)
            lines.append("PROMPT BÀI TẬP (đã điền sẵn):")
            lines.append(draft)
        elif s.get("carry"):
            lines.append(f"Mang sang từ bước trước: {s['carry']}")
        if step.get("prompt_template"):
            pass
        elif draft:                     # Claude prepared this input: template + known values, blanks for the rest
            lines.append("Thông tin của tôi (đã điền sẵn):")
            lines.append(draft)
        elif s.get("next_inputs"):                         # Claude said this step needs the learner's information
            lines.append("Thông tin của tôi:")
            lines += [f"• {f.strip()}: [cần điền: {f.strip()}]" for f in s["next_inputs"]]
        label = (f"{self._step_name(i)} · {step['part_title'][:25]}" if step.get("part")
                 else f"Bài tập · {self._step_name(i)} · {ex['title'][:30]}")
        blanks = len(BLANK.findall(draft or ""))
        if step.get("prompt_template"):
            if blanks:
                label += f" · còn {blanks} chỗ cần điền"
                self.mw.status.config(text=f"🎓 Prompt đã điền sẵn — còn {blanks} chỗ [cần điền] trong ô nhập.",
                                      fg="#ffd479")
            else:
                self.mw.status.config(text="🎓 Prompt của bước đã được điền sẵn đầy đủ — bấm Gửi.", fg="#7ee2a8")
        elif draft or s.get("next_inputs"):
            label += f" · còn {blanks} chỗ cần điền" if draft else " · điền thông tin"
            self.mw.status.config(text=(f"🎓 Bản nháp đã điền sẵn — còn {blanks} chỗ [cần điền] trong ô nhập."
                                        if draft else "🎓 Điền thông tin vào mẫu trong ô nhập rồi gửi."),
                                  fg="#ffd479")
        # the documents go again with each new sub-exercise and now and then, so they stay in Claude's recent
        # history (the overview already sent them)
        ids = ex.get("doc_ids", []) if (i > 0 and (i % 6 == 0 or new_part)) or s.get("resume") else []
        self.set_prompt("\n".join(lines), {
            "kind": "exstep", "step": i, "doc_ids": ids, "label": label,
            "input": bool(draft or s.get("next_inputs")) and not step.get("prompt_template"),
            "course_prompt": step.get("prompt_template") and draft,
            "blocks": [{"type": "text", "text": EXERCISE_RULES}, self._next_step_block(i)] + self._resume_blocks()})

    # ---- filling the course's prompts: choices (e.g. the learner's table), earlier results, learner values ----
    def fill_template(self, step):
        """The step's course prompt with every blank filled in that the app can fill; [cần điền: ...] for the rest."""
        s = self.session
        values = {}
        for slot in step.get("slots", []):
            name, kind = slot["name"], slot.get("kind")
            val = None
            if kind == "choice":
                val = s.setdefault("choice_values", {}).get(name)
            elif kind == "previous":
                k = int(slot.get("from_step") or 0) - 1
                val = s.setdefault("outputs", {}).get(str(k))
            if val is None:
                val = s.setdefault("slot_values", {}).get(name)
            values[name] = val if val else f"[cần điền: {slot.get('label') or name}]"

        def sub(m):
            name = m.group(1)
            return values.get(name) or s.get("slot_values", {}).get(name) or f"[cần điền: {name}]"
        return SLOT_MARK.sub(sub, step["prompt_template"]).strip()

    def ensure_inputs(self, step):
        """Before a course prompt is shown: ask the learner for the choices and values it needs (once each)."""
        s = self.session
        params = s["ex"].get("params") or []
        changed = False
        for slot in step.get("slots", []):
            name, kind = slot["name"], slot.get("kind")
            if kind == "choice" and name not in s.setdefault("choice_values", {}):
                param = next((p for p in params if any(v["slot"] == name for o in p.get("options", [])
                                                       for v in o.get("values", []))), None)
                if param and param["name"] not in s.setdefault("choices", {}):
                    opt = self.ask_choice(param)
                    if opt:
                        s["choices"][param["name"]] = opt["label"]
                        for v in opt.get("values", []):
                            s["choice_values"][v["slot"]] = v["text"]
                        self.chat_note(f"{param['question']} → {opt['label']} (dữ liệu đã được điền vào prompt)")
                        changed = True
            elif kind == "learner" and not s.setdefault("slot_values", {}).get(name):
                val = self.ask_text(slot)
                if val:
                    s["slot_values"][name] = val
                    changed = True
        if changed:
            self.save_session()

    def ask_choice(self, param):
        """Ask the learner to pick one option (e.g. their table); shows what each option will fill in."""
        options = param.get("options") or []
        if not options:
            return None
        top = self._dialog("Chọn để điền vào prompt", "640x460")
        tk.Label(top, text=param.get("question") or "Chọn một mục:", font=(A.UI_FONT, 11, "bold"), bg="white",
                 wraplength=600, justify="left").pack(anchor="w", padx=14, pady=(12, 6))
        row = tk.Frame(top, bg="white")
        row.pack(fill="both", expand=True, padx=14)
        lst = tk.Listbox(row, width=18, font=(A.UI_FONT, 11), activestyle="none", exportselection=False)
        for o in options:
            lst.insert("end", o["label"])
        lst.pack(side="left", fill="y")
        prev = tk.Text(row, wrap="word", font=(A.UI_FONT, 9), bg="#fbf8f6", bd=1, relief="solid")
        prev.pack(side="left", fill="both", expand=True, padx=(8, 0))

        def show(*_):
            prev.delete("1.0", "end")
            sel = lst.curselection()
            if sel:
                for v in options[sel[0]].get("values", []):
                    prev.insert("end", f"{v['slot']}:\n{v['text']}\n\n")
        lst.bind("<<ListboxSelect>>", show)
        lst.selection_set(0)
        show()
        result = []
        self._buttons(top, lambda: (result.append(lst.curselection()), top.destroy()), ok="Chọn")
        lst.bind("<Double-1>", lambda e: (result.append(lst.curselection()), top.destroy()))
        top.wait_window()
        return options[result[0][0]] if result and result[0] else None

    def fill_prompt_blanks(self, job):
        """Ask for every [cần điền: ...] left in the exercise prompt (one short question each) and fill them in."""
        if self.job is not job:                    # the prompt was sent or replaced meanwhile
            return
        text = self.mw.prompt.get("1.0", "end").rstrip("\n")
        labels = list(dict.fromkeys(m.group(1).strip() or "thông tin" for m in BLANK_LABEL.finditer(text)))
        if not labels:
            return
        values = self.ask_fields(job.get("label", "Bài tập"), labels)
        if not values:
            return
        for m in reversed(list(BLANK_LABEL.finditer(text))):
            v = values.get(m.group(1).strip() or "thông tin")
            if v:
                text = text[:m.start()] + v + text[m.end():]
        p = self.mw.prompt
        p.delete("1.0", "end")
        p.insert("end", text)
        job["prompt"] = text
        left = len(BLANK.findall(text))
        self.mw.status.config(text=(f"🎓 Đã điền vào prompt — còn {left} chỗ [cần điền]." if left else
                                    "🎓 Đã điền đủ thông tin vào prompt — bấm Gửi."),
                              fg="#ffd479" if left else "#7ee2a8")
        p.focus_set()

    def ask_fields(self, title, labels):
        """A dialog with one short question per missing item; returns {item: answer} (empty if skipped)."""
        top = self._dialog("Thông tin cần cho bước này", f"640x{min(620, 170 + 78 * len(labels))}")
        tk.Label(top, text=title, font=(A.UI_FONT, 10, "bold"), bg="white", fg="#8a3f22", wraplength=600,
                 justify="left").pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(top, text="Trả lời ngắn gọn từng câu; app sẽ điền vào đúng chỗ trong prompt.", fg="#666",
                 bg="white", font=(A.UI_FONT, 9)).pack(anchor="w", padx=14, pady=(0, 6))
        frame = tk.Frame(top, bg="white")
        frame.pack(fill="both", expand=True, padx=14)
        boxes = {}
        for k, label in enumerate(labels):
            q = label.rstrip(" ?:") + "?"
            tk.Label(frame, text=f"{k + 1}. {q[:1].upper() + q[1:]}", bg="white", font=(A.UI_FONT, 10),
                     wraplength=590, justify="left").pack(anchor="w", pady=(4, 0))
            box = tk.Text(frame, height=2, wrap="word", font=(A.UI_FONT, 10), bd=1, relief="solid")
            box.pack(fill="x")
            boxes[label] = box
        if boxes:
            next(iter(boxes.values())).focus_set()
        result = []
        self._buttons(top, lambda: (result.append({k: b.get("1.0", "end").strip() for k, b in boxes.items()}),
                                    top.destroy()), ok="Điền vào prompt")
        top.unbind("<Return>")                         # Enter makes a new line in a box
        top.wait_window()
        return {k: v for k, v in (result[0] if result else {}).items() if v}

    def ask_text(self, slot):
        """Ask once for a value only the learner can give (e.g. their problem statement); reused in later prompts."""
        top = self._dialog("Thông tin cho prompt", "620x300")
        tk.Label(top, text=f"Nhập: {slot.get('label') or slot['name']}", font=(A.UI_FONT, 11, "bold"), bg="white",
                 wraplength=580, justify="left").pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(top, text="Chỉ cần nhập một lần — các prompt sau dùng lại tự động.", fg="#666", bg="white",
                 font=(A.UI_FONT, 9)).pack(anchor="w", padx=14)
        box = tk.Text(top, height=6, wrap="word", font=(A.UI_FONT, 11), bd=1, relief="solid")
        box.pack(fill="both", expand=True, padx=14, pady=6)
        box.focus_set()
        result = []
        self._buttons(top, lambda: (result.append(box.get("1.0", "end").strip()), top.destroy()), ok="Điền vào prompt")
        top.unbind("<Return>")                         # Enter makes a new line in the box
        top.wait_window()
        return result[0] if result and result[0] else None

    def prompt_inputs(self, fields):
        """Claude asked for the learner's own information: a fill-in form in the prompt box."""
        s = self.session
        i = s["i"]
        text = f"{self._step_name(i)} — thông tin của tôi:\n" + "\n".join(
            f"• {f.strip()}: [cần điền: {f.strip()}]" for f in fields)
        self.set_prompt(text, {"kind": "exstep", "step": i, "input": True,
                               "doc_ids": s["ex"].get("doc_ids", []) if s.get("resume") else [],
                               "blocks": [{"type": "text", "text": EXERCISE_RULES}, self._next_step_block(i)]
                               + self._resume_blocks(),
                               "label": f"{self._step_name(i)} · điền thông tin"})
        self.mw.status.config(text="🎓 Claude cần thông tin của bạn — điền vào mẫu trong ô nhập rồi gửi.",
                              fg="#ffd479")

    def summary_prompt(self, send=True):
        ex = self.session["ex"]
        self.set_prompt(
            f"Đã xong {len(ex['steps'])} bước của bài tập \"{ex['title']}\". Tổng hợp ngắn gọn (tối đa 8 dòng): kết "
            "quả cuối cùng, kết quả/prompt chính của từng "
            + ("bài tập con" if ex.get("whole_file") else "bước") + " (mỗi mục 1 dòng) và 2-3 điều cần nhớ.",
            {"kind": "exsummary", "label": f"Tổng kết lời giải · {ex['title'][:30]}",
             "blocks": self._resume_blocks()})
        if send:
            self.root.after(300, self.mw.send_prompt)      # sent right away

    def tidy_answer(self, turn):
        """Hide the status markers from the latest answer and add [📋 Copy prompt] after each prompt block."""
        w = self.mw.chat
        try:
            start = w.index("answer_start")
        except tk.TclError:
            return
        w.configure(state="normal")
        count = tk.IntVar()
        # the compact result is kept for later prompts; it need not be shown twice
        idx = w.search(OUTPUT_START_TK, start, stopindex="end", regexp=True, nocase=True, count=count)
        if idx:
            end = w.search(OUTPUT_END_TK, idx, stopindex="end", regexp=True, nocase=True, count=count)
            if end:
                w.delete(idx, f"{end}+{count.get()}c")
                w.insert(idx, "📌 (đã lưu kết quả gọn của bước này — sẽ tự điền vào prompt các bước sau)", "file")
        # the draft for the next step goes into the prompt box, not into the answers pane
        idx = w.search(DRAFT_START_TK, start, stopindex="end", regexp=True, nocase=True, count=count)
        if idx:
            end = w.search(DRAFT_END_TK, idx, stopindex="end", regexp=True, nocase=True, count=count)
            if end:
                w.delete(idx, f"{end}+{count.get()}c")
                w.insert(idx, "📝 (bản nháp cho bước sau đã được điền sẵn vào ô nhập)", "file")
        while True:
            idx = w.search(MARKER_TK, start, stopindex="end", regexp=True, nocase=True, count=count)
            if not idx or not count.get():
                break
            end = f"{idx}+{count.get()}c"
            if w.get(f"{idx} linestart", idx).strip() == "" and w.get(end, f"{end} lineend").strip() == "":
                end = f"{end} lineend +1c"                 # the marker was alone on its line
            w.delete(idx, end)
        texts = [m.group(1).strip() for m in PROMPT_BLOCK.finditer(w.get(start, "end"))]
        ends, idx = [], start
        while len(ends) < len(texts):
            idx = w.search(PROMPT_END_TK, idx, stopindex="end", regexp=True, nocase=True, count=count)
            if not idx or not count.get():
                break
            ends.append(w.index(f"{idx} lineend"))
            idx = f"{idx}+{count.get()}c"
        for text, pos in reversed(list(zip(texts, ends))):   # last first, so earlier positions stay valid
            n = getattr(self, "_copy_n", 0) + 1
            self._copy_n = n
            tag = f"copyprompt{n}"
            w.tag_configure(tag, foreground="#1a5fb4", underline=True)
            w.tag_bind(tag, "<Button-1>", lambda e, t=text: self.copy_text(t))
            w.tag_bind(tag, "<Enter>", lambda e: w.config(cursor="hand2"))
            w.tag_bind(tag, "<Leave>", lambda e: w.config(cursor=""))
            w.insert(pos, "  ")
            w.insert(f"{pos}+2c", "[📋 Copy prompt]", tag)
        w.configure(state="disabled")
        if turn:
            turn["a"] = OUTPUT_BLOCK.sub("", DRAFT_BLOCK.sub("", MARKER.sub("", turn.get("a", "")))).strip()

    def copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.mw.status.config(text=f"✓ Đã copy prompt — dán ({PASTE}) vào nơi cần dùng", fg="#7ee2a8")

    def on_chat_end(self, turn, ok):
        """Called by the app when Claude finished answering (ok = no error)."""
        job, self.sent_job = self.sent_job, None
        raw = (turn or {}).get("a", "")
        try:
            self.linkify()
            self.tidy_answer(turn)
        except tk.TclError:
            pass
        # save the answer of a Learning prompt that can be reused (only if it was sent as the app wrote it)
        if job and job.get("save_as") and ok and (turn or {}).get("a", "").strip() and \
                " ".join(turn.get("q", "").split()) == " ".join(job.get("prompt", "").split()):
            self.save_answer(job["save_as"][0], job["save_as"][1], turn["a"].strip())
        s = self.session
        if not s:
            return
        steps = s["ex"]["steps"]
        answer = (turn or {}).get("a", "").strip()
        kind = job["kind"] if job else None
        if kind == "exstep" and job.get("step") == s["i"] and ok and answer:
            s.pop("resume", None)                           # Claude has the context again from here on
            nxt = NEXT_INPUT.search(raw)
            dm = DRAFT_BLOCK.search(raw)
            if s["i"] < 0:                                  # the overview
                s["overview"] = answer
                s["next_inputs"] = [f for f in nxt.group(1).split("|") if f.strip()] if nxt else None
                s["next_draft"] = dm.group(1).strip() if dm else None
                s["i"] = 0
                self.save_session()
                self.prompt_step()
                return
            s["log"].append({"i": s["i"], "step": steps[s["i"]], "q": turn.get("q", ""), "a": answer,
                             "input": bool(job.get("input")), "prompt": job.get("course_prompt") or ""})
            # the step's compact result is what later prompts get pasted in automatically
            om = OUTPUT_BLOCK.search(raw)
            s.setdefault("outputs", {})[str(s["i"])] = (om.group(1).strip() if om else answer)[:12000]
            ask = NEEDS_INPUT.search(raw)
            if ask and not DONE.search(raw):                # Claude needs the learner's information first
                s["awaiting"] = [f for f in ask.group(1).split("|") if f.strip()] or ["thông tin"]
                self.save_session()
                self.prompt_inputs(s["awaiting"])
                return
            s.pop("awaiting", None)
            carry = CARRY.search(MARKER.sub("", raw))
            s["carry"] = carry.group(1).strip() if carry else None
            s["next_inputs"] = [f for f in nxt.group(1).split("|") if f.strip()] if nxt else None
            s["next_draft"] = dm.group(1).strip() if dm else None
            s["i"] += 1
            self.save_session()
            if s["i"] < len(steps):
                self.prompt_step()                          # next step's prompt, ready to send
            else:
                self.summary_prompt()
        elif kind == "exsummary" and ok and answer:
            try:
                out = solution_pdf(self.lib, s, answer)
                self.add_output("solution", out, f"Lời giải: {s['ex']['title']}", day=s["ex"].get("day", 0),
                                detail=f"{len(s['ex']['steps'])} bước")
                self.chat_note(f"Hoàn thành bài tập \"{s['ex']['title']}\". File PDF lời giải (cũng có trong "
                               "📋 List output files):\n")
                self.file_links(out, "Lưu lời giải")
            except Exception as e:  # noqa: BLE001
                self.chat_note(f"Chưa tạo được file PDF lời giải: {e}", "err")
            self.save_session(done=True)
            self.session = None
        elif s["i"] < 0:
            self.prompt_step()
        elif s["i"] < len(steps):
            if s.get("awaiting"):                           # still waiting for the learner's information
                self.prompt_inputs(s["awaiting"])
            else:
                self.prompt_step()                          # a side question or an error: same step again
        else:
            self.summary_prompt(send=False)

    # ---- List output files: every file the app created, with clear descriptions, sortable, open/save/delete ----
    OUT_KINDS = {"takeaways": ("📄", "Ý chính (Key takeaways)"), "journal": ("📓", "Nhật ký buổi học"),
                 "solution": ("✏", "Lời giải bài tập"), "backup": ("💾", "Backup phiên nghe")}
    OUT_PREFIX = (("KeyTakeaways_", "takeaways"), ("NhatKy_", "journal"), ("LoiGiai_", "solution"),
                  ("LiveTranslator_backup_", "backup"))
    OUT_SORTS = (("new", "Mới nhất trước"), ("old", "Cũ nhất trước"), ("kind", "Theo loại file"),
                 ("name", "Theo tên (A → Z)"), ("day", "Theo buổi học"), ("size", "Theo dung lượng"))

    def add_output(self, kind, path, title, detail="", day=0):
        """Record a file the app just created, so it shows in List output files."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        key = os.path.normcase(os.path.abspath(path))
        entry = {"path": os.path.abspath(path), "kind": kind, "title": title, "detail": detail, "day": day or 0,
                 "created": time.strftime("%Y-%m-%d %H:%M:%S"), "size": size}
        with self.lib.lock:
            outs = [o for o in self.lib.data.get("outputs", []) if os.path.normcase(o["path"]) != key]
            self.lib.data["outputs"] = outs + [entry]
        self.lib.save()

    def adopt_outputs(self):
        """Add files made earlier (or before this list existed) from the output folders; drop deleted ones."""
        folders = [os.path.join(self.lib.dir, "journals"), os.path.join(self.lib.dir, "solutions")]
        for sub in ("Outputs", "Backup"):
            d = A.output_dir(sub) if hasattr(A, "output_dir") else None
            if d:
                folders.append(d)
        with self.lib.lock:
            outs = [o for o in self.lib.data.get("outputs", []) if os.path.exists(o["path"])]
            known = {os.path.normcase(o["path"]) for o in outs}
            for folder in folders:
                try:
                    names = os.listdir(folder)
                except OSError:
                    continue
                for name in names:
                    path = os.path.join(folder, name)
                    kind = next((k for p, k in self.OUT_PREFIX if name.startswith(p)), None)
                    if not kind or os.path.normcase(path) in known or not os.path.isfile(path):
                        continue
                    what = re.sub(r"_\d{8}_\d{4}\.(pdf|json)$", "", name)
                    what = re.sub(r"^(KeyTakeaways|NhatKy_|LoiGiai_|LiveTranslator_backup)_?", "", what).replace("_", " ")
                    title = {"takeaways": "Ý chính phiên nghe", "backup": "Backup phiên nghe",
                             "journal": f"Nhật ký: {what}", "solution": f"Lời giải: {what}"}[kind]
                    st = os.stat(path)
                    outs.append({"path": path, "kind": kind, "title": title.strip(": "), "detail": "", "day": 0,
                                 "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                                 "size": st.st_size})
                    known.add(os.path.normcase(path))
            self.lib.data["outputs"] = outs
        self.lib.save()

    def list_outputs(self):
        self.adopt_outputs()
        top = self._dialog("📋 List output files — các file đã tạo", "1000x520", modal=False)
        bar = tk.Frame(top, bg="white")
        bar.pack(fill="x", padx=12, pady=(10, 4))
        sorts = dict(self.OUT_SORTS)
        kinds = {"": "Tất cả loại file", **{k: f"{i} {t}" for k, (i, t) in self.OUT_KINDS.items()}}
        sort_var = tk.StringVar(value=sorts.get(self.lib.data.get("out_sort", "new"), sorts["new"]))
        kind_var = tk.StringVar(value=kinds[""])
        tk.Label(bar, text="Sắp xếp:", bg="white", font=(A.UI_FONT, 10, "bold")).pack(side="left")
        ttk.Combobox(bar, textvariable=sort_var, values=list(sorts.values()), state="readonly",
                     width=20).pack(side="left", padx=(4, 14))
        tk.Label(bar, text="Lọc:", bg="white", font=(A.UI_FONT, 10, "bold")).pack(side="left")
        ttk.Combobox(bar, textvariable=kind_var, values=list(kinds.values()), state="readonly",
                     width=26).pack(side="left", padx=4)
        info = tk.Label(top, text="", bg="white", fg="#666", font=(A.UI_FONT, 9), anchor="w")
        info.pack(fill="x", padx=12)
        cols = (("kind", "Loại", 150), ("title", "Mô tả", 300), ("day", "Buổi", 50), ("when", "Thời gian", 125),
                ("size", "Dung lượng", 80), ("file", "Tên file", 260))
        tree = ttk.Treeview(top, columns=[c[0] for c in cols], show="tree headings")
        tree.column("#0", width=24, stretch=False)
        for col, txt, w in cols:
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor="w", stretch=col in ("title", "file"))
        tree.tag_configure("group", font=(A.UI_FONT, 10, "bold"), background="#f3ece7")
        sb = ttk.Scrollbar(top, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        btns = tk.Frame(top, bg="white")
        btns.pack(side="bottom", fill="x", padx=12, pady=8)
        sb.pack(side="right", fill="y", pady=4, padx=(0, 12))
        tree.pack(fill="both", expand=True, padx=(12, 0), pady=4)
        rows = {}

        def when(o):
            try:
                return time.strftime("%H:%M · %d/%m/%Y", time.strptime(o["created"], "%Y-%m-%d %H:%M:%S"))
            except (ValueError, KeyError):
                return ""

        def size(n):
            return f"{n / 1e6:.1f} MB" if n > 1e6 else f"{max(1, round(n / 1e3))} KB"

        def fill(*_):
            tree.delete(*tree.get_children())
            rows.clear()
            by = next(k for k, v in sorts.items() if v == sort_var.get())
            only = next(k for k, v in kinds.items() if v == kind_var.get())
            with self.lib.lock:
                self.lib.data["out_sort"] = by
            allouts = self.lib.data.get("outputs", [])
            outs = [o for o in allouts if not only or o["kind"] == only]
            keyf = {"new": lambda o: o["created"], "old": lambda o: o["created"], "kind": lambda o: o["kind"],
                    "name": lambda o: o["title"].lower(), "day": lambda o: o.get("day") or 999,
                    "size": lambda o: o["size"]}[by]
            outs.sort(key=lambda o: o["created"], reverse=True)          # newest first inside groups
            outs.sort(key=keyf, reverse=by in ("new", "size"))
            parent, group = "", None
            for n, o in enumerate(outs):
                icon, label = self.OUT_KINDS.get(o["kind"], ("📄", "File"))
                g = label if by == "kind" else (f"Buổi {o['day']}" if o.get("day") else "Không theo buổi") \
                    if by == "day" else None
                if g and g != group:
                    group = g
                    parent = tree.insert("", "end", text="", open=True, tags=("group",), values=(g, "", "", "", "", ""))
                iid = f"o{n}"
                rows[iid] = o
                tree.insert(parent if g else "", "end", iid=iid, values=(
                    f"{icon} {label}", o["title"] + (f" — {o['detail']}" if o.get("detail") else ""),
                    o.get("day") or "-", when(o), size(o["size"]), os.path.basename(o["path"])))
            info.config(text=f"{len(outs)}/{len(allouts)} file. Nhấn đúp để mở; chọn file rồi dùng các nút bên dưới."
                        if allouts else "Chưa có file nào. Các file app tạo ra (Xuất PDF ý chính, Backup, nhật ký, "
                                        "lời giải bài tập) sẽ hiện ở đây.")

        def selected():
            s = tree.selection()
            return rows.get(s[0]) if s else None

        def open_out():
            o = selected()
            if not o:
                return
            if not os.path.exists(o["path"]):
                messagebox.showinfo("List output files", "Không tìm thấy file này nữa.", parent=top)
                self.adopt_outputs()
                fill()
                return
            open_path(o["path"])

        def show_in_folder():
            o = selected()
            if o and os.path.exists(o["path"]):
                if sys.platform == "win32":
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(o["path"])])
                elif IS_MAC:
                    subprocess.Popen(["open", "-R", o["path"]])      # Finder, file selected
                else:
                    open_path(os.path.dirname(o["path"]))

        def save_copy():
            o = selected()
            if not o or not os.path.exists(o["path"]):
                return
            ext = os.path.splitext(o["path"])[1]
            dst = filedialog.asksaveasfilename(parent=top, title="Lưu bản sao", initialfile=os.path.basename(o["path"]),
                                               defaultextension=ext, filetypes=[(ext.upper().strip("."), "*" + ext)])
            if dst:
                shutil.copy2(o["path"], dst)
                self.mw.status.config(text=f"✓ Đã lưu bản sao: {os.path.basename(dst)}", fg="#7ee2a8")

        def delete():
            o = selected()
            if not o:
                return
            if not messagebox.askyesno("Xoá file", f"Xoá \"{o['title']}\"?\n\n{o['path']}\n\nFile được chuyển vào "
                                       "Thùng rác (có thể khôi phục lại).", parent=top):
                return
            if os.path.exists(o["path"]) and not to_recycle_bin(o["path"]):
                messagebox.showerror("Xoá file", "Không xoá được file này.", parent=top)
                return
            with self.lib.lock:
                self.lib.data["outputs"] = [x for x in self.lib.data.get("outputs", []) if x is not o]
            self.lib.save()
            fill()

        sort_var.trace_add("write", fill)
        kind_var.trace_add("write", fill)
        tree.bind("<Double-1>", lambda e: open_out())
        st = dict(font=(A.UI_FONT, 10), relief="flat", cursor="hand2", padx=12, pady=3)
        tk.Button(btns, text="Đóng", command=top.destroy, bg="#eee", **st).pack(side="right")
        tk.Button(btns, text="🗑 Xoá", command=delete, bg="#eee", **st).pack(side="right", padx=6)
        tk.Button(btns, text="💾 Lưu bản sao…", command=save_copy, bg="#eee", **st).pack(side="right")
        tk.Button(btns, text="📁 Mở thư mục chứa file", command=show_in_folder, bg="#eee", **st).pack(side="right", padx=6)
        tk.Button(btns, text="📄 Mở", command=open_out, bg="#d97757", fg="white", activebackground="#c4643f",
                  **st).pack(side="right")
        fill()

    # ---- Documents ----
    def documents(self):
        top = self._dialog("Documents — tài liệu đã upload", "900x480", modal=False)
        bar = tk.Frame(top, bg="white")
        bar.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(bar, text="Tìm:", bg="white", font=(A.UI_FONT, 10)).pack(side="left")
        q = tk.StringVar()
        tk.Entry(bar, textvariable=q, font=(A.UI_FONT, 10), width=40).pack(side="left", padx=6)
        tk.Label(bar, text="Nhấn vào tên file để mở file gốc", bg="white", fg="#888",
                 font=(A.UI_FONT, 9)).pack(side="right")
        tree = ttk.Treeview(top, columns=("date", "day", "title", "file"), show="headings")
        for col, txt, w in (("date", "Ngày", 90), ("day", "Buổi", 55), ("title", "Tên tài liệu", 380),
                            ("file", "Tên file", 330)):
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor="w", stretch=col in ("title", "file"))
        tree.tag_configure("odd", background="#faf6f4")
        sb = ttk.Scrollbar(top, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        desc = tk.Label(top, text="", bg="white", fg="#555", font=(A.UI_FONT, 9), wraplength=860, justify="left",
                        anchor="w")
        btns = tk.Frame(top, bg="white")
        btns.pack(side="bottom", fill="x", padx=12, pady=8)
        desc.pack(side="bottom", fill="x", padx=12)
        sb.pack(side="right", fill="y", pady=4, padx=(0, 12))
        tree.pack(fill="both", expand=True, padx=(12, 0), pady=4)
        docs = {}

        def fill(*_):
            tree.delete(*tree.get_children())
            docs.clear()
            words = q.get().lower().split()
            for d in self.lib.docs():
                hay = f"{d.get('title')} {d['name']} {d.get('description')}".lower()
                if words and not all(w in hay for w in words):
                    continue
                docs[d["id"]] = d
                tree.insert("", "end", iid=d["id"], tags=("odd",) if len(docs) % 2 else (),
                            values=(fmt_date(d.get("date") or d.get("added")), d.get("day") or "-",
                                    d.get("title") or d["name"], "📄 " + d["name"]))
        q.trace_add("write", fill)
        fill()

        def selected():
            s = tree.selection()
            return docs.get(s[0]) if s else None

        def open_doc(d=None):
            d = d or selected()
            if d:
                try:
                    open_path(self.lib.file_path(d))
                except OSError as e:
                    messagebox.showerror("Không mở được file", str(e), parent=top)
                    return
                top.destroy()                      # file is open: close the list

        def on_click(e):
            row = tree.identify_row(e.y)
            if row and tree.identify_column(e.x) == "#4":
                open_doc(docs.get(row))

        def on_move(e):
            tree.config(cursor="hand2" if tree.identify_row(e.y) and tree.identify_column(e.x) == "#4" else "")

        tree.bind("<ButtonRelease-1>", on_click)
        tree.bind("<Motion>", on_move)
        tree.bind("<Double-1>", lambda e: open_doc())
        tree.bind("<<TreeviewSelect>>", lambda e: desc.config(text=(
            lambda d: f"{KIND_VI.get(d.get('kind'), '')} · {d.get('description', '')}"
            + (f"  ·  Bài tập: {len(d['exercises'])}" if d.get("exercises") else "")
            + f"  ·  Gốc: {d['rel']}")(selected()) if selected() else ""))

        def ask(fresh=False):
            d = selected()
            if not d:
                return
            title = d.get("title") or d["name"]
            name = f"doc:{d['id']}"                # the id is the file's content hash: same file, same answer
            saved = None if fresh else self.saved_answer(name, "v1")
            top.destroy()
            if saved:
                self.show_saved(f"Tài liệu \"{title[:50]}\"", saved, "muốn hỏi lại: Documents → 🔄 Hỏi lại")
                return
            self.set_prompt(f"Hãy giới thiệu tài liệu \"{title}\": nội dung chính, "
                            "các ý quan trọng và nó liên quan thế nào đến khóa học.",
                            {"kind": "doc", "label": f"Tài liệu · {title[:40]}", "doc_ids": [d["id"]],
                             "save_as": (name, "v1")})

        def resync():
            if self.syncing:
                return
            sync_btn.config(state="disabled", text="⏳ Đang đồng bộ...")

            def done():
                if top.winfo_exists():
                    sync_btn.config(state="normal", text="🔄 Refresh")
                    fill()
            self.refresh(on_done=done)
            if not self.syncing:                   # no folder chosen
                sync_btn.config(state="normal", text="🔄 Refresh")
        bstyle = dict(font=(A.UI_FONT, 10), relief="flat", cursor="hand2", padx=12, pady=3)
        sync_btn = tk.Button(btns, text="🔄 Refresh", command=resync, bg="#eee", **bstyle)
        sync_btn.pack(side="left", padx=(0, 6))
        tk.Button(btns, text="Đóng", command=top.destroy, bg="#eee", **bstyle).pack(side="right")
        tk.Button(btns, text="🔄 Hỏi lại", command=lambda: ask(fresh=True), bg="#eee",
                  **bstyle).pack(side="right", padx=(6, 0))
        tk.Button(btns, text="💬 Hỏi Claude về tài liệu này", command=ask, bg="#d97757", fg="white",
                  activebackground="#c4643f", **bstyle).pack(side="right", padx=6)
        tk.Button(btns, text="📄 Mở file", command=open_doc, bg="#eee", **bstyle).pack(side="right")
        tk.Button(btns, text="📁 Thư mục thư viện", bg="#eee", **bstyle,
                  command=lambda: open_path(os.path.join(self.lib.dir, "files"))).pack(side="left")

    # ---- Journal ----
    def journal(self):
        temps = self.lib.templates()
        if not temps:
            messagebox.showinfo("Journal", "Chưa tìm thấy file PDF mẫu nhật ký trong thư mục đã upload.\n"
                                "Hãy đặt file mẫu (ví dụ \"Journal.pdf\" / \"Nhat ky.pdf\") vào thư mục học "
                                "rồi chọn Refresh để đồng bộ lại.", parent=self.root)
            return
        # today's course day if the library knows it, else the latest day (the user can edit the prompt)
        days = self.lib.days()
        today = time.strftime("%Y-%m-%d")
        day, date, docs = next((x for x in days if x[1] == today), days[-1] if days else (0, "", []))
        template = temps[0]
        when = (f"Buổi {day}" + (f" (ngày {fmt_date(date)})" if date else "")) if day else \
            f"buổi học hôm nay ({time.strftime('%d/%m/%Y')})"
        self.set_prompt(
            f"Hãy điền nhật ký buổi học cho {when} theo đúng mẫu \"{template['name']}\", dựa trên tài liệu của "
            "buổi này và nội dung thầy giảng trên màn hình. Viết ngắn gọn, cụ thể, đúng từng mục trong mẫu, "
            "rồi trả lại file PDF đã điền để tải về.",
            {"kind": "journal", "label": f"Journal · {when} · mẫu {template['name'][:30]}", "when": when, "day": day,
             "doc_ids": [d["id"] for d in docs if d["id"] != template["id"]], "template": template["id"]})

    def run_journal(self, instruction, job):
        """Called by the app when the Journal prompt is sent: fill the PDF in the background."""
        template = self.lib.get(job["template"])
        day_docs = [self.lib.get(i) for i in job.get("doc_ids", []) if self.lib.get(i)]
        # the English transcript and the takeaways (the Vietnamese translation would repeat it at the same cost)
        en, summary = self.mw.transcript_text()[-40000:], self.mw.last_summary[-30000:]
        session = (f"<key_takeaways>\n{summary}\n</key_takeaways>\n<transcript_english>\n{en}\n</transcript_english>"
                   if (en or summary) else "")
        q = self.mw.ui_q
        q.put(("chat_piece", "đang đọc mẫu nhật ký và điền thông tin...\n"))
        self.mw.busy("chat", "Claude đang điền nhật ký theo mẫu PDF...")      # cleared when it ends

        def work():
            try:
                out, preview = make_journal(self.mw.client, self.lib, template, day_docs, session, instruction)
                q.put(("chat_piece", (preview.strip() + "\n") if preview else ""))
                self.later(lambda: (self.file_links(out), self.add_output(
                    "journal", out, "Nhật ký: " + job.get("when", "buổi học"), day=job.get("day", 0))))
            except Exception as e:  # noqa: BLE001
                why = A.friendly_error(e) if isinstance(e, anthropic.APIError) else str(e)[:200]
                q.put(("chat_error", f"[Chưa điền được nhật ký: {why}]"))
            q.put(("chat_end", None))
        threading.Thread(target=work, daemon=True).start()

    def file_links(self, path, title="Lưu file"):
        """Clickable 'open' / 'save as' links for a file in the answers pane."""
        w = self.mw.chat
        n = getattr(self, "_link_n", 0) + 1
        self._link_n = n

        def save_as():
            dst = filedialog.asksaveasfilename(parent=self.root, title=title, defaultextension=".pdf",
                                               initialfile=os.path.basename(path), filetypes=[("PDF", "*.pdf")])
            if dst:
                shutil.copy2(path, dst)
                self.mw.status.config(text=f"✓ Đã lưu: {os.path.basename(dst)}", fg="#7ee2a8")
        w.configure(state="normal")
        w.insert("end", "📄 " + os.path.basename(path) + "   ")
        for tag, label, fn in ((f"lnkopen{n}", "[Mở file]", lambda e: open_path(path)),
                               (f"lnksave{n}", "[Tải về / Lưu…]", lambda e: save_as())):
            w.tag_configure(tag, foreground="#1a5fb4", underline=True)
            w.tag_bind(tag, "<Button-1>", fn)
            w.tag_bind(tag, "<Enter>", lambda e: w.config(cursor="hand2"))
            w.tag_bind(tag, "<Leave>", lambda e: w.config(cursor=""))
            w.insert("end", label, tag)
            w.insert("end", "  ")
        w.see("end")
        w.configure(state="disabled")

    # ---- Certificate ----
    def certificate(self):
        top = self._dialog("Certificate — chứng chỉ", "760x500", modal=False)
        left = tk.Frame(top, bg="white")
        left.pack(side="left", fill="y", padx=(12, 6), pady=12)
        right = tk.Frame(top, bg="white")
        right.pack(side="left", fill="both", expand=True, padx=(6, 12), pady=12)
        lst = tk.Listbox(left, width=26, font=(A.UI_FONT, 10), activestyle="none", exportselection=False)
        lst.pack(fill="y", expand=True)
        preview = tk.Label(right, bg="#f4f4f4", text="Chưa có chứng chỉ.\nNhấn ＋ Upload để thêm ảnh.",
                           fg="#888", font=(A.UI_FONT, 10), width=60, height=14)
        preview.pack(fill="both", expand=True)
        tk.Label(right, text="Lời chia sẻ (có thể sửa):", bg="white", font=(A.UI_FONT, 9, "bold")).pack(anchor="w", pady=(8, 0))
        caption = tk.Text(right, height=3, font=(A.UI_FONT, 10), wrap="word")
        caption.pack(fill="x")
        certs = self.lib.data["certificates"]
        state = {"img": None}

        def current():
            s = lst.curselection()
            return certs[s[0]] if s else None

        def path_of(c):
            return os.path.join(self.lib.dir, "certificates", c["stored"])

        def show(*_):
            c = current()
            if not c:
                return
            caption.delete("1.0", "end")
            caption.insert("end", c.get("caption") or
                           f"Vui mừng chia sẻ: tôi vừa nhận được chứng chỉ \"{c['title']}\"! 🎉")
            try:
                from PIL import Image, ImageTk
                img = Image.open(path_of(c))
                img.thumbnail((480, 300))
                state["img"] = ImageTk.PhotoImage(img)
                preview.config(image=state["img"], text="", width=0, height=0)
            except Exception:  # noqa: BLE001
                preview.config(image="", text=c["name"], width=60, height=14)

        def refill(select=None):
            lst.delete(0, "end")
            for c in certs:
                lst.insert("end", f"{fmt_date(c['added'])}  {c['title'][:22]}")
            if certs:
                lst.selection_set(select if select is not None else len(certs) - 1)
                show()
        lst.bind("<<ListboxSelect>>", show)

        def upload():
            p = filedialog.askopenfilename(parent=top, title="Chọn ảnh certificate",
                                           filetypes=[("Ảnh / PDF", "*.png *.jpg *.jpeg *.webp *.gif *.pdf"),
                                                      ("Tất cả", "*.*")])
            if not p:
                return
            name = os.path.basename(p)
            stored = f"{time.strftime('%Y%m%d_%H%M%S')}_{safe_name(name)}"
            shutil.copy2(p, os.path.join(self.lib.dir, "certificates", stored))
            certs.append({"name": name, "stored": stored, "title": os.path.splitext(name)[0],
                          "added": time.strftime("%Y-%m-%d"), "caption": ""})
            self.lib.save()
            refill()
            c = certs[-1]
            try:
                block = A.file_to_block(path_of(c))
            except ValueError:
                return
            self.set_prompt("Đây là chứng chỉ tôi vừa nhận được. Hãy viết giúp tôi một lời chia sẻ ngắn gọn, "
                            "tự nhiên (tiếng Việt, kèm bản tiếng Anh) để gửi email và đăng lên nhóm WhatsApp, "
                            "nêu tên khóa học và cảm ơn thầy cô, bạn học.",
                            {"kind": "certificate", "label": f"Certificate · {name[:40]}", "blocks": [block]})

        def save_caption():
            c = current()
            if c:
                c["caption"] = caption.get("1.0", "end").strip()
                self.lib.save()
            return c

        def email():
            c = save_caption()
            if not c:
                return
            subject = f"Chứng chỉ: {c['title']}"
            body = c["caption"]
            path = path_of(c)

            def work():
                if not mapi_send(path, subject, body):
                    copied = copy_file_to_clipboard(path)
                    self.later(lambda: open_path(f"mailto:?subject={quote(subject)}&body={quote(body)}"))
                    if copied:
                        self.mw.ui_q.put(("status", f"🎓 Đã copy ảnh chứng chỉ — nhấn {PASTE} trong thư để đính kèm."))
            threading.Thread(target=work, daemon=True).start()

        def whatsapp():
            c = save_caption()
            if not c:
                return
            copied = copy_file_to_clipboard(path_of(c))
            text = quote(c["caption"])
            try:
                open_path(f"whatsapp://send?text={text}")
            except OSError:
                import webbrowser
                webbrowser.open(f"https://wa.me/?text={text}")
            messagebox.showinfo("WhatsApp", "Chọn nhóm WhatsApp cần gửi."
                                + (f"\nẢnh chứng chỉ đã được copy: nhấn {PASTE} trong ô chat để gửi kèm ảnh." if copied else ""),
                                parent=top)

        def copy_img():
            c = current()
            if c and copy_file_to_clipboard(path_of(c)):
                self.mw.status.config(text=f"✓ Đã copy ảnh chứng chỉ ({PASTE} để dán)", fg="#7ee2a8")

        def remove():
            c = current()
            if c and messagebox.askyesno("Xóa", f"Xóa chứng chỉ \"{c['title']}\" khỏi danh sách?", parent=top):
                certs.remove(c)
                self.lib.save()
                preview.config(image="", text="", width=60, height=14)
                refill()
        bst = dict(font=(A.UI_FONT, 10), relief="flat", cursor="hand2", padx=10, pady=3)
        tk.Button(left, text="＋ Upload ảnh certificate", command=upload, bg="#d97757", fg="white",
                  activebackground="#c4643f", **bst).pack(fill="x", pady=(8, 2))
        tk.Button(left, text="🗑 Xóa", command=remove, bg="#eee", **bst).pack(fill="x")
        row = tk.Frame(right, bg="white")
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="Share:", bg="white", font=(A.UI_FONT, 10, "bold")).pack(side="left")
        tk.Button(row, text="📧 Email", command=email, bg="#1a5fb4", fg="white", activebackground="#154c8f",
                  **bst).pack(side="left", padx=4)
        tk.Button(row, text="💬 WhatsApp", command=whatsapp, bg="#25D366", fg="white", activebackground="#1da851",
                  **bst).pack(side="left", padx=4)
        tk.Button(row, text="📋 Copy ảnh", command=copy_img, bg="#eee", **bst).pack(side="left", padx=4)
        tk.Button(row, text="📄 Mở", command=lambda: current() and open_path(path_of(current())), bg="#eee",
                  **bst).pack(side="left", padx=4)
        refill()

    # ---- dialog helpers ----
    def _dialog(self, title, geometry=None, modal=True):
        top = tk.Toplevel(self.root, bg="white")
        top.title(title)
        top.transient(self.root)
        if geometry:
            top.geometry(geometry)
        top.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() - (top.winfo_reqwidth() if not geometry
                                                                 else int(geometry.split("x")[0])) - 60
        top.geometry(f"+{max(0, x)}+{self.root.winfo_rooty() + 80}")
        if modal:
            top.grab_set()
        top.bind("<Escape>", lambda e: top.destroy())
        return top

    def _buttons(self, top, on_ok, ok="Tạo prompt"):
        row = tk.Frame(top, bg="white")
        row.pack(side="bottom", fill="x", padx=12, pady=10)
        st = dict(font=(A.UI_FONT, 10), relief="flat", cursor="hand2", padx=14, pady=3)
        tk.Button(row, text="Hủy", command=top.destroy, bg="#eee", **st).pack(side="right")
        tk.Button(row, text=ok, command=on_ok, bg="#d97757", fg="white", activebackground="#c4643f",
                  **st).pack(side="right", padx=6)
        top.bind("<Return>", lambda e: on_ok())
