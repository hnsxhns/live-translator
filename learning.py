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
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "file"


def open_path(path):
    """Open a file or folder with the system's default app."""
    if sys.platform == "win32":
        os.startfile(path)                                  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
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


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise ValueError("Claude không trả về JSON")
    return json.loads(text[a:b + 1])


def ask_json(client, system, content, schema, effort="low", max_tokens=16000):
    """One Claude call that must answer with JSON matching `schema` (structured output when the
    request accepts it, otherwise plain JSON text)."""
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
                return parse_json(text)
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
        return hit or [d for d in docs if JOURNAL_NAME.search(d["name"])]

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
            name = os.path.basename(src)
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
            doc = {"id": doc_id, "sha": sha, "name": name, "ext": ext, "stored": stored, "rel": rels[0],
                   "sources": rels, "size": os.path.getsize(src), "pages": pages, "added": today,
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
        f = self.client.files.upload(file=(name, data, mime))
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
    out = os.path.join(lib.dir, "journals", f"{safe_name(stem)}_{time.strftime('%Y%m%d_%H%M')}.pdf")
    fill_template(src, out, res.get("fields", []), res.get("placements", []), lines)
    return out, res.get("preview", "")


# =====================================================================
# Sharing (certificate)
# =====================================================================
def copy_file_to_clipboard(path):
    """Put the file on the Windows clipboard (as a file, and as a picture for images), so Ctrl+V in
    WhatsApp / Outlook / Gmail attaches it. False if not possible."""
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
PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["steps"],
               "properties": {"steps": STEPS_SCHEMA}}
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
    "Break the given exercise from the attached course document into 3-8 concrete steps the learner can work "
    "through one at a time, following the document's own instructions and order. Return JSON only: steps with "
    "a short title and a Vietnamese instruction (keep technical terms, commands and values from the document).")


def _ask_doc(lib, client, system, doc, info, schema, effort="medium"):
    """ask_json with one library document attached (falls back to its text if the file is refused)."""
    try:
        return ask_json(client, system, [lib.block(doc), {"type": "text", "text": info}], schema, effort=effort)
    except anthropic.BadRequestError:
        text = lib.text(doc)[:300_000]
        return ask_json(client, system, f"{info}\n\n<file name=\"{doc['name']}\">\n{text}\n</file>", schema,
                        effort=effort)


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
    info = (f"Exercise: {ex['title']}\nSummary: {ex.get('summary', '')}\nLocation: {ex.get('location', '')}\n"
            f"File: {doc['rel']}")
    return _ask_doc(lib, client, PLAN_SYSTEM, doc, info, PLAN_SCHEMA).get("steps") or []


def pdf_fonts():
    """(folder, regular file, bold file) of a font with Vietnamese letters."""
    for folder, reg, bold in ((r"C:\Windows\Fonts", "arial.ttf", "arialbd.ttf"),
                              (r"C:\Windows\Fonts", "segoeui.ttf", "segoeuib.ttf"),
                              ("/System/Library/Fonts/Supplemental", "Arial.ttf", "Arial Bold.ttf"),
                              ("/Library/Fonts", "Arial Unicode.ttf", "Arial Unicode.ttf")):
        if os.path.exists(os.path.join(folder, reg)):
            return folder, reg, bold if os.path.exists(os.path.join(folder, bold)) else reg
    return None


def text_html(text):
    """Claude's plain text (light Markdown) -> simple HTML paragraphs."""
    import html as _html
    out = []
    for para in re.split(r"\n\s*\n", (text or "").strip()):
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


def write_pdf(out, body_html):
    """Write an A4 PDF from simple HTML (with a font that has Vietnamese letters)."""
    import pymupdf
    fonts = pdf_fonts()
    css = ("body {font-size: 11pt; line-height: 1.35;} h1 {font-size: 17pt; color: #8a3f22;} "
           "h2 {font-size: 13.5pt; color: #8a3f22; margin-top: 14pt;} h3 {font-size: 12pt; margin-top: 10pt;} "
           ".meta {color: #666; font-size: 9.5pt;} .instr {color: #444; font-style: italic;} p {margin: 4pt 0;}")
    archive = None
    if fonts:
        folder, reg, bold = fonts
        archive = pymupdf.Archive(folder)
        css = (f"@font-face {{font-family: vn; src: url({reg});}} "
               f"@font-face {{font-family: vn; src: url({bold}); font-weight: bold;}} "
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
    parts += ["<h2>Tổng kết lời giải</h2>", text_html(summary), "<h2>Chi tiết từng bước</h2>"]
    for k, item in enumerate(session["log"], 1):
        st = item["step"]
        parts.append(f"<h3>Bước {k}: {_html.escape(st.get('title', ''))}</h3>")
        if st.get("instruction"):
            parts.append(f"<p class='instr'>{_html.escape(st['instruction'])}</p>")
        parts.append(text_html(item["a"]))
    os.makedirs(os.path.join(lib.dir, "solutions"), exist_ok=True)
    out = os.path.join(lib.dir, "solutions",
                       f"LoiGiai_{safe_name(ex['title'])[:50]}_{time.strftime('%Y%m%d_%H%M')}.pdf")
    write_pdf(out, "\n".join(parts))
    return out


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
                      ("đồng bộ thư mục học" if n else "chọn thư mục học")),
                      command=self.folder, state="disabled" if self.syncing else "normal")
        m.add_command(label="🗓  Agenda — chương trình học", command=self.agenda, state=need)
        m.add_command(label="📚  Documents — danh sách tài liệu", command=self.documents, state=need)
        m.add_command(label="📝  Summary — tổng kết buổi học", command=self.summary, state=need)
        m.add_command(label="✏  Exercises — bài tập, giải theo từng bước", command=self.exercises, state=need)
        if self.session:
            ex = self.session["ex"]
            m.add_command(label=f"      ⏹  Dừng bài \"{ex['title'][:30]}\" (bước {self.session['i'] + 1}/{len(ex['steps'])})",
                          command=self.stop_session)
        m.add_command(label="📓  Journal — điền nhật ký buổi học (PDF)", command=self.journal, state=need)
        m.add_command(label="🏆  Certificate — chứng chỉ & chia sẻ", command=self.certificate)
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
        self.job = job
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
        last = self.lib.data.get("folder") or ""
        if last and os.path.isdir(last):
            ans = messagebox.askyesnocancel(
                "Learning — Folder",
                f"Đồng bộ lại thư mục đã chọn?\n\n{last}\n\nCó: tìm tài liệu mới và upload, làm mới dữ liệu.\n"
                "Không: chọn thư mục khác.", parent=self.root)
            if ans is None:
                return
            folder = last if ans else filedialog.askdirectory(parent=self.root, title="Chọn thư mục học",
                                                              initialdir=last)
        else:
            folder = filedialog.askdirectory(parent=self.root, title="Chọn thư mục học tập để Claude học")
        if not folder:
            return
        self.syncing = True
        self.chat_note(f"Bắt đầu đồng bộ thư mục {folder}")

        def progress(msg):
            self.mw.ui_q.put(("status", "🎓 " + msg))

        def work():
            try:
                r = Syncer(self.lib, self.mw.client, progress).run(folder)
                msg = (f"Đồng bộ xong: {r['found']} file trong thư mục, {r['new']} tài liệu mới đã upload, "
                       f"{r['known']} đã có từ trước, {r['dup']} bản trùng nội dung đã bỏ qua. "
                       f"Thư viện có {r['total']} tài liệu.")
                if r["problems"]:
                    msg += "\nLưu ý:\n" + "\n".join("• " + p for p in r["problems"][:12])
                self.later(lambda: self.chat_note(msg))
                self.mw.ui_q.put(("status", f"🎓 Đồng bộ xong: +{r['new']} tài liệu mới"))
            except Exception as e:  # noqa: BLE001
                self.later(lambda: self.chat_note(f"Đồng bộ lỗi: {A.friendly_error(e)}", "err"))
                self.mw.ui_q.put(("error", f"🎓 Đồng bộ lỗi: {A.friendly_error(e)}"))
            finally:
                self.syncing = False
        threading.Thread(target=work, daemon=True).start()

    # ---- Agenda ----
    def agenda(self):
        ids = [d["id"] for d in self.lib.docs() if d.get("kind") == "agenda"]
        src = "tài liệu Agenda đính kèm" if ids else "danh mục tài liệu đã upload"
        self.set_prompt(
            f"Hãy in ra Agenda khóa học thật gọn dựa trên {src}: mỗi buổi đúng 1 dòng theo dạng "
            "\"Buổi N · dd/mm · chủ đề chính — bài tập (nếu có)\". Không giải thích, không mô tả tài liệu, "
            "không chia mục con. Dòng cuối: tổng số buổi và thời gian khóa học.",
            {"kind": "agenda", "label": f"Agenda · {len(ids)} tài liệu" if ids else "Agenda · danh mục",
             "doc_ids": ids})

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
        tk.Label(top, text="File sách dùng để tổng kết (giữ Ctrl hoặc Shift để chọn nhiều file):", font=bold,
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
            f"Summary · {when + ' · ' if when else ''}{'Sách' if m == 'book' else 'Sách + thầy giảng'} · {len(ids)} file")})

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
        mode = tk.StringVar(value="big")
        tk.Radiobutton(head, text="Bài tập lớn (đọc toàn bộ thư mục, theo các bước)", value="big", variable=mode,
                       bg="white", font=bold).pack(side="left")
        tk.Radiobutton(head, text="Bài tập trong từng tài liệu", value="doc", variable=mode, bg="white",
                       font=(A.UI_FONT, 10)).pack(side="left", padx=14)
        status = tk.Label(top, text="", bg="white", fg="#666", font=(A.UI_FONT, 9), anchor="w")
        status.pack(fill="x", padx=12)
        tree = ttk.Treeview(top, columns=("day", "title", "steps", "docs"), show="headings", height=10)
        for col, txt, w in (("day", "Buổi", 50), ("title", "Bài tập", 380), ("steps", "Số bước", 70),
                            ("docs", "Tài liệu", 330)):
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor="w", stretch=col in ("title", "docs"))
        tree.pack(fill="both", expand=True, padx=12, pady=4)
        detail = tk.Text(top, height=8, wrap="word", font=(A.UI_FONT, 9), bd=1, relief="solid", bg="#fbf8f6")
        detail.pack(fill="x", padx=12)
        btns = tk.Frame(top, bg="white")
        btns.pack(fill="x", padx=12, pady=8)
        full = tk.BooleanVar(value=False)
        items = []
        busy = {"scan": False}

        def alive():
            return top.winfo_exists()

        def show_status():
            if mode.get() == "big":
                upd = self.lib.data.get("big_exercises_updated")
                status.config(text=f"{len(items)} bài tập lớn · cập nhật {upd}" if upd else
                              "Chưa có danh sách — nhấn 🔄 Refresh để Claude đọc toàn bộ thư mục.")
            else:
                status.config(text=f"{len(items)} bài tập tìm thấy khi đồng bộ (Claude sẽ chia bước khi bắt đầu).")

        def fill(*_):
            tree.delete(*tree.get_children())
            items.clear()
            if mode.get() == "big":
                for ex in self.lib.data.get("big_exercises", []):
                    docs = [self.lib.get(i) for i in ex.get("doc_ids", []) if self.lib.get(i)]
                    items.append(ex)
                    tree.insert("", "end", iid=str(len(items) - 1), values=(
                        ex.get("day") or "-", ex["title"], len(ex["steps"]),
                        ", ".join(d.get("title") or d["name"] for d in docs)))
            else:
                for ex in self.lib.exercises():
                    items.append(ex)
                    tree.insert("", "end", iid=str(len(items) - 1), values=(
                        ex["day"] or "-", ex["title"], "-", ex["doc"].get("title") or ex["doc"]["name"]))
            detail.delete("1.0", "end")
            show_status()
            refresh_btn.config(state="normal" if mode.get() == "big" and not busy["scan"] else "disabled")

        def on_select(*_):
            detail.delete("1.0", "end")
            if not tree.selection():
                return
            ex = items[int(tree.selection()[0])]
            if mode.get() == "big":
                detail.insert("end", (ex.get("goal") or "") + "\n\n" + "\n".join(
                    f"{k}. {s['title']} — {s['instruction']}" for k, s in enumerate(ex["steps"], 1)))
            else:
                detail.insert("end", f"{ex['summary']}\n({ex['location']})")

        def refresh():
            busy["scan"] = True
            refresh_btn.config(state="disabled")

            def progress(msg):
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
                    self.later(lambda: alive() and fill())
            threading.Thread(target=work, daemon=True).start()

        def start():
            if not tree.selection():
                return
            ex = items[int(tree.selection()[0])]
            if mode.get() == "big":
                top.destroy()
                self.start_session(ex)
                return
            status.config(text="⏳ Claude đang chia bài tập thành các bước...")
            start_btn.config(state="disabled")

            def work():
                try:
                    steps = plan_steps(self.lib, self.mw.client, ex)
                    if not steps:
                        raise ValueError("Claude không trả về các bước")
                    sess = {"title": ex["title"], "goal": ex.get("summary", ""), "day": ex["day"],
                            "doc_ids": [ex["doc"]["id"]], "steps": steps}
                    self.later(lambda: (alive() and top.destroy(), self.start_session(sess)))
                except Exception as e:  # noqa: BLE001
                    why = A.friendly_error(e) if isinstance(e, anthropic.APIError) else str(e)
                    self.later(lambda: alive() and (status.config(text="Lỗi: " + why), start_btn.config(state="normal")))
            threading.Thread(target=work, daemon=True).start()

        st = dict(font=(A.UI_FONT, 10), relief="flat", cursor="hand2", padx=12, pady=3)
        refresh_btn = tk.Button(btns, text="🔄 Refresh danh sách", command=refresh, bg="#eee", **st)
        refresh_btn.pack(side="left")
        tk.Checkbutton(btns, text="Đọc lại toàn bộ file", variable=full, bg="white",
                       font=(A.UI_FONT, 9)).pack(side="left", padx=6)
        tk.Button(btns, text="Đóng", command=top.destroy, bg="#eee", **st).pack(side="right")
        start_btn = tk.Button(btns, text="▶ Bắt đầu bài tập", command=start, bg="#d97757", fg="white",
                              activebackground="#c4643f", **st)
        start_btn.pack(side="right", padx=6)
        tree.bind("<<TreeviewSelect>>", on_select)
        tree.bind("<Double-1>", lambda e: start())
        mode.trace_add("write", fill)
        fill()
        if not self.lib.data.get("big_exercises_updated"):
            refresh()                          # first time: read the folder right away

    # ---- guided exercise: one prompt per step, then the solution summary + PDF ----
    def start_session(self, ex):
        self.session = {"ex": ex, "i": 0, "log": []}
        self.chat_note(f"Bắt đầu bài tập \"{ex['title']}\" — {len(ex['steps'])} bước:\n" + "\n".join(
            f"  {k}. {s['title']}" for k, s in enumerate(ex["steps"], 1)))
        self.prompt_step()

    def stop_session(self):
        if self.session:
            self.chat_note(f"Đã dừng bài tập \"{self.session['ex']['title']}\".")
        self.session = None
        if self.job and self.job["kind"] in ("exstep", "exsummary"):
            self.job = None
            self.mw.prompt.delete("1.0", "end")
            self.mw._refresh_attach_bar()

    def prompt_step(self):
        s = self.session
        ex, i = s["ex"], s["i"]
        n, step = len(ex["steps"]), ex["steps"][i]
        text = (f"Bài tập \"{ex['title']}\" — Bước {i + 1}/{n}: {step['title']}.\n{step['instruction']}\n"
                "Hãy thực hiện bước này: giải thích ngắn gọn cần làm gì và vì sao, làm/giải chi tiết bước này "
                "dựa trên tài liệu, rồi tóm tắt kết quả của bước. Chỉ làm bước này, chưa sang bước sau.")
        # the documents go with the first step, and again now and then so they stay in Claude's recent history
        ids = ex.get("doc_ids", []) if i % 6 == 0 else []
        self.set_prompt(text, {"kind": "exstep", "step": i, "doc_ids": ids,
                               "label": f"Bài tập · Bước {i + 1}/{n} · {ex['title'][:30]}"})

    def summary_prompt(self, send=True):
        ex = self.session["ex"]
        self.set_prompt(
            f"Đã xong tất cả {len(ex['steps'])} bước của bài tập \"{ex['title']}\". Hãy viết phần tổng kết lời giải: "
            "đề bài và mục tiêu, kết quả cuối cùng, tóm tắt lời giải theo từng bước (mỗi bước 1-3 dòng), các kiến "
            "thức quan trọng cần nhớ và lỗi thường gặp.",
            {"kind": "exsummary", "label": f"Tổng kết lời giải · {ex['title'][:30]}"})
        if send:
            self.root.after(300, self.mw.send_prompt)      # sent right away

    def on_chat_end(self, turn, ok):
        """Called by the app when Claude finished answering (ok = no error)."""
        job, self.sent_job = self.sent_job, None
        s = self.session
        if not s:
            return
        steps = s["ex"]["steps"]
        answer = (turn or {}).get("a", "").strip()
        kind = job["kind"] if job else None
        if kind == "exstep" and job.get("step") == s["i"] and ok and answer:
            s["log"].append({"step": steps[s["i"]], "q": turn.get("q", ""), "a": answer})
            s["i"] += 1
            if s["i"] < len(steps):
                self.prompt_step()                          # next step's prompt, ready to send
            else:
                self.summary_prompt()
        elif kind == "exsummary" and ok and answer:
            try:
                out = solution_pdf(self.lib, s, answer)
                self.chat_note(f"Hoàn thành bài tập \"{s['ex']['title']}\". File PDF lời giải:\n")
                self.file_links(out, "Lưu lời giải")
            except Exception as e:  # noqa: BLE001
                self.chat_note(f"Chưa tạo được file PDF lời giải: {e}", "err")
            self.session = None
        elif s["i"] < len(steps):
            self.prompt_step()                              # a side question or an error: same step again
        else:
            self.summary_prompt(send=False)

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

        def ask():
            d = selected()
            if not d:
                return
            self.set_prompt(f"Hãy giới thiệu tài liệu \"{d.get('title') or d['name']}\": nội dung chính, "
                            "các ý quan trọng và nó liên quan thế nào đến khóa học.",
                            {"kind": "doc", "label": f"Tài liệu · {(d.get('title') or d['name'])[:40]}",
                             "doc_ids": [d["id"]]})
        bstyle = dict(font=(A.UI_FONT, 10), relief="flat", cursor="hand2", padx=12, pady=3)
        tk.Button(btns, text="Đóng", command=top.destroy, bg="#eee", **bstyle).pack(side="right")
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
                                "rồi chọn Folder để đồng bộ lại.", parent=self.root)
            return
        pick = self.pick_day("Journal — chọn buổi học")
        if not pick:
            return
        day, date, docs, _ = pick
        template = temps[0]
        when = (f"Buổi {day}" + (f" (ngày {fmt_date(date)})" if date else "")) if day else \
            f"buổi học hôm nay ({time.strftime('%d/%m/%Y')})"
        self.set_prompt(
            f"Hãy điền nhật ký buổi học cho {when} theo đúng mẫu \"{template['name']}\", dựa trên tài liệu của "
            "buổi này và nội dung thầy giảng trên màn hình. Viết ngắn gọn, cụ thể, đúng từng mục trong mẫu, "
            "rồi trả lại file PDF đã điền để tải về.",
            {"kind": "journal", "label": f"Journal · {when} · mẫu {template['name'][:30]}",
             "doc_ids": [d["id"] for d in docs if d["id"] != template["id"]], "template": template["id"]})

    def run_journal(self, instruction, job):
        """Called by the app when the Journal prompt is sent: fill the PDF in the background."""
        template = self.lib.get(job["template"])
        day_docs = [self.lib.get(i) for i in job.get("doc_ids", []) if self.lib.get(i)]
        en, vi, summary = self.mw._chat_context()
        session = (f"<key_takeaways>\n{summary}\n</key_takeaways>\n<transcript_english>\n{en}\n</transcript_english>\n"
                   f"<translation_vietnamese>\n{vi}\n</translation_vietnamese>") if (en or vi or summary) else ""
        q = self.mw.ui_q
        q.put(("chat_piece", "đang đọc mẫu nhật ký và điền thông tin...\n"))

        def work():
            try:
                out, preview = make_journal(self.mw.client, self.lib, template, day_docs, session, instruction)
                q.put(("chat_piece", (preview.strip() + "\n") if preview else ""))
                self.later(lambda: self.file_links(out))
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
                        self.mw.ui_q.put(("status", "🎓 Đã copy ảnh chứng chỉ — nhấn Ctrl+V trong thư để đính kèm."))
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
                                + ("\nẢnh chứng chỉ đã được copy: nhấn Ctrl+V trong ô chat để gửi kèm ảnh." if copied else ""),
                                parent=top)

        def copy_img():
            c = current()
            if c and copy_file_to_clipboard(path_of(c)):
                self.mw.status.config(text="✓ Đã copy ảnh chứng chỉ (Ctrl+V để dán)", fg="#7ee2a8")

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
