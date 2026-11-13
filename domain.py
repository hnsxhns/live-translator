"""Specialist documents and the glossary built from them, all on this PC.

The user adds documents of their field (PDF, Word, PowerPoint, Excel, CSV, text). This module
  * reads their text (no internet, no extra libraries beyond PyMuPDF for PDF),
  * finds the field's terms (word groups that come back often and are not everyday words),
  * pairs terms with translations: from glossary tables (2 columns), from documents that give the original
    term in brackets ("học tăng cường (reinforcement learning)"), from suggestions of the local model, or typed
    by the user,
  * gives the app what it needs: words for the speech recognizer's prompt, term -> translation pairs for the
    translator, and a search over the documents for the Claude panel.
"""
import csv
import html
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from xml.etree import ElementTree

SUPPORTED = (".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".tsv", ".txt", ".md", ".srt", ".vtt", ".html", ".htm")
TABLE_TYPES = (".csv", ".tsv", ".xlsx")
NO_SPACE_LANGS = {"zh", "ja", "th"}
MINE_LANGS_SKIP = {"zh", "ja", "th", "ko"}          # term mining needs words separated by spaces

# ---------------------------------------------------------------- reading files


def _decode(raw):
    for enc in ("utf-8-sig", "utf-16", "cp1258", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _xml_paragraphs(xml, para_tag, text_tag):
    out = []
    for p in ElementTree.fromstring(xml).iter(para_tag):
        line = "".join(t.text or "" for t in p.iter(text_tag)).strip()
        if line:
            out.append(line)
    return out


def _xlsx_rows(path):
    """Rows of every sheet of an .xlsx (cell texts), without extra libraries."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).iter(ns + "si"):
            shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
    rows = []
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    for name in sheets:
        for row in ElementTree.fromstring(z.read(name)).iter(ns + "row"):
            cells = []
            for c in row.iter(ns + "c"):
                v = c.find(ns + "v")
                if c.get("t") == "s" and v is not None:
                    cells.append(shared[int(v.text)] if v.text and int(v.text) < len(shared) else "")
                elif c.get("t") == "inlineStr":
                    cells.append("".join(t.text or "" for t in c.iter(ns + "t")))
                else:
                    cells.append(v.text if v is not None and v.text else "")
            if any(x.strip() for x in cells):
                rows.append(cells)
    return rows


def table_rows(path):
    """Rows of a CSV / TSV / Excel file."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        return _xlsx_rows(path)
    text = _decode(open(path, "rb").read())
    delim = "\t" if ext == ".tsv" or text.count("\t") > text.count(",") else ","
    if delim == "," and text.count(";") > text.count(","):
        delim = ";"
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(x.strip() for x in r)]


def extract_text(path):
    """Plain text of a document. Raises ValueError with a Vietnamese reason when it cannot be read."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            import pymupdf
            with pymupdf.open(path) as doc:
                return "\n".join(page.get_text() for page in doc)
        if ext == ".docx":
            w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            z = zipfile.ZipFile(path)
            parts = ["word/document.xml"] + sorted(n for n in z.namelist() if re.match(r"word/(footnotes|endnotes)\.xml$", n))
            return "\n".join(line for n in parts for line in _xml_paragraphs(z.read(n), w + "p", w + "t"))
        if ext == ".pptx":
            a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
            z = zipfile.ZipFile(path)
            slides = sorted((n for n in z.namelist() if re.match(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml$", n)),
                            key=lambda n: (("notes" in n), int(re.findall(r"\d+", n)[-1])))
            return "\n".join(line for n in slides for line in _xml_paragraphs(z.read(n), a + "p", a + "t"))
        if ext in TABLE_TYPES:
            return "\n".join("\t".join(c.strip() for c in r) for r in table_rows(path))
        if ext in (".html", ".htm"):
            t = _decode(open(path, "rb").read())
            t = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", t)
            t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", t)
            return html.unescape(re.sub(r"<[^>]+>", " ", t))
        if ext in (".srt", ".vtt"):
            t = _decode(open(path, "rb").read())
            lines = [ln for ln in t.splitlines() if ln.strip() and not re.match(r"^\s*(\d+|WEBVTT.*|[\d:.,]+\s*-->.*)\s*$", ln)]
            return "\n".join(lines)
        return _decode(open(path, "rb").read())
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"không đọc được file ({str(e)[:120]})") from e


# ---------------------------------------------------------------- language of a text

_VI_CHARS = set("ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ")
STOP = {
    "en": set("""a about above after again against all also am an and any are as at be because been before being below
        between both but by can could did do does doing down during each few for from further had has have having he her
        here hers him his how i if in into is it its itself just me more most my no nor not now of off on once only or
        other our ours out over own same she should so some such than that the their theirs them then there these they
        this those through to too under until up very was we were what when where which while who whom why will with
        would you your yours let lets like one two three first second new also may might must shall us via per e g i e
        etc using used use uses well get gets got make makes made many much way ways thing things""".split()),
    "fr": set("le la les un une des du de et en est que qui dans pour par sur au aux pas ne plus ce cette ces il elle "
              "nous vous ils elles on se sa son ses leur avec sont été être".split()),
    "de": set("der die das und ist nicht ein eine einer den dem des zu mit auf für von im in sich auch es sie er wir "
              "ihr sind war wird werden dass als bei oder aus nach wie".split()),
    "es": set("el la los las un una unos de del y en que es por para con no se su sus al lo como más pero son fue ser "
              "este esta estos".split()),
    "pt": set("o a os as um uma de do da dos das e em que é por para com não se seu sua ao no na como mais mas são foi "
              "ser este esta".split()),
    "it": set("il lo la i gli le un una di del della e in che è per con non si suo sua al come più ma sono".split()),
    "nl": set("de het een en van in is dat op te zijn met voor niet aan er ook als door maar om".split()),
    "pl": set("i w na z do się nie jest to że o a jak po za od tak ale co".split()),
    "tr": set("ve bir bu da de için ile çok daha gibi ne olarak olan ama".split()),
    "id": set("dan yang di ke dari ini itu untuk dengan pada adalah tidak dalam akan juga atau".split()),
    "vi": set("và là của có được cho các những một trong này với không người đã để khi thì từ đến về như cũng nhưng "
              "sẽ ra vào theo nên vì hay hoặc rất nhiều nó họ chúng ta tôi bạn".split()),
    "ru": set("и в не на что с по как это он она они а к из у же за от для о до".split()),
    "uk": set("і в не на що з по як це він вона вони а до від для у за".split()),
}
# everyday English words that are not terms even when they come back often
COMMON_EN = set("""people time year years day days week weeks today example examples part parts number numbers point
    points case cases fact idea ideas problem problems question questions answer answers result results work works
    working world life group groups system systems information data way people place places end start next last good
    great best better small large big high low long short important simple different same able need needs want wants
    look looks see seen know known think thinks give gives given take takes taken find finds found show shows shown
    call called try tries keep keeps lot lots kind kinds sort sorts actually really basically very quite always never
    often sometimes usually instead something nothing anything everything someone everyone anyone thanks thank please
    hello welcome morning everyone okay right going come comes talk talked talking say says said tell told chapter
    page pages section sections figure table slide slides note notes course class lecture student students teacher""".split())


def guess_lang(text, prefer=()):
    """Language code of a text, from its script, Vietnamese letters, and common words. `prefer` breaks ties."""
    t = text[:20000]
    counts = {
        "ja": len(re.findall(r"[぀-ヿ]", t)),
        "zh": len(re.findall(r"[一-鿿]", t)),
        "ko": len(re.findall(r"[가-힯]", t)),
        "ru": len(re.findall(r"[Ѐ-ӿ]", t)),
        "ar": len(re.findall(r"[؀-ۿ]", t)),
        "hi": len(re.findall(r"[ऀ-ॿ]", t)),
        "th": len(re.findall(r"[฀-๿]", t)),
        "latin": len(re.findall(r"[A-Za-zÀ-ỹ]", t)),
    }
    if counts["ja"] and counts["ja"] * 5 > counts["zh"] * 0.2:
        counts["ja"] += counts["zh"]
    script = max(counts, key=counts.get)
    if counts[script] == 0:
        return prefer[0] if prefer else "en"
    if script == "ru":
        return "uk" if re.search(r"[іїєґ]", t.lower()) else "ru"
    if script != "latin":
        return script
    low = t.lower()
    vi = sum(1 for ch in low if ch in _VI_CHARS)
    if vi > max(3, counts["latin"] * 0.03):
        return "vi"
    words = re.findall(r"[a-zà-ÿąćęłńóśźżçğıöşü']+", low)
    if not words:
        return prefer[0] if prefer else "en"
    best, score = "en", -1.0
    for lang, sw in STOP.items():
        if lang in ("vi", "ru", "uk"):
            continue
        s = sum(1 for w in words if w in sw) / len(words) + (0.01 if lang in prefer else 0)
        if s > score:
            best, score = lang, s
    return best


def short_lang(phrase, fallback="en"):
    """Language of a short phrase (a term): script and Vietnamese letters only (too short for common words)."""
    if any(ch in _VI_CHARS for ch in phrase.lower()):
        return "vi"
    g = guess_lang(phrase)
    if g in ("ja", "zh", "ko", "ru", "uk", "ar", "hi", "th"):
        return g
    return fallback


# ---------------------------------------------------------------- finding terms

_SENT = re.compile(r"[.!?;:\n•·|()\[\]{}\"“”]+")
_WORD = re.compile(r"[^\W\d_][\w'’\-]*[^\W_]|[^\W\d_]")


def mine_terms(text, lang, max_terms=300):
    """[(term, count, example sentence)] of the field's terms in a text of language `lang` (space-separated
    languages only). Terms = word groups of 1-4 words that come back, do not start or end with a small word,
    and are not everyday words; a group that only ever appears inside a longer term is left out."""
    if lang in MINE_LANGS_SKIP:
        return []
    stop = STOP.get(lang, set())
    common = COMMON_EN if lang == "en" else set()
    counts, example, cases = {}, {}, {}
    inner_ok = {"of", "and", "for", "de", "du", "des", "der", "von", "di", "da", "do", "và", "của"}
    for sent in _SENT.split(text):
        words = _WORD.findall(sent)
        if len(words) < 1 or len(words) > 120:
            continue
        low = [w.lower().replace("’", "'") for w in words]
        for n in (1, 2, 3, 4):
            for i in range(len(low) - n + 1):
                g = low[i:i + n]
                if g[0] in stop or g[-1] in stop or any(len(x) < 2 for x in g):
                    continue
                if n == 1 and (g[0] in common or len(g[0]) < 4) and not words[i].isupper():
                    continue
                if n > 2 and any(x in stop and x not in inner_ok for x in g[1:-1]):
                    continue
                key = " ".join(g)
                counts[key] = counts.get(key, 0) + 1
                if key not in example:
                    example[key] = sent.strip()[:220]
                orig = " ".join(words[i:i + n])
                c = cases.setdefault(key, {})
                if i > 0 or orig.isupper():             # the first word of a sentence is capitalised anyway
                    c[orig] = c.get(orig, 0) + 1
    def shown(k):
        c = cases.get(k) or {}
        return max(c, key=c.get) if c else k

    keep = {}
    for k, c in counts.items():
        n = k.count(" ") + 1
        acronym = shown(k).isupper() and 2 <= len(k) <= 8
        if c >= (2 if (n == 2 and len(text) < 20000) or acronym else 3):
            keep[k] = c
    # a shorter group that (almost) always appears inside one longer kept term is not a term by itself
    for k in sorted(keep, key=lambda x: -x.count(" ")):
        if k.count(" ") == 0:
            continue
        ws = k.split()
        for a in range(len(ws)):
            for b in range(a + 1, len(ws) + 1):
                sub = " ".join(ws[a:b])
                if sub != k and sub in keep and keep[sub] <= keep[k] * 1.15:
                    keep[sub] = -1
    out = []
    for k, c in keep.items():
        if c <= 0:
            continue
        n = k.count(" ") + 1
        out.append((shown(k), c, example[k], c * (1 + 0.6 * (n - 1))))
    out.sort(key=lambda x: -x[3])
    return [(t, c, e) for t, c, e, _ in out[:max_terms]]


# ---------------------------------------------------------------- term pairs found in documents

_PAREN = re.compile(r"([^()\n]{2,120})\(\s*([^()\n]{2,70}?)\s*\)")
_CUT = re.compile(r"[,.;:!?–—\"“”'‘’/]|\s-\s")
_VI_LEAD = set("các những một là của và về gọi được hay hoặc với cho trong bằng theo thuật ngữ khái niệm tiếng anh "
               "còn có dùng sử dụng giữa từ khi nếu thì này đó mà để tức nghĩa như".split())


_FUNC_LEAD = set("với và của các những một là cho trong bằng theo về từ khi như the a an of".split())


def _bigrams(s):
    s = re.sub(r"\s+", " ", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _dice(a, b):
    x, y = _bigrams(a), _bigrams(b)
    return 2 * len(x & y) / (len(x) + len(y)) if x and y else 0.0


def bracket_pairs(text, translate=None):
    """[(term, term_lang, translation, translation_lang)] from 'phrase (term in another language)' or the other
    way round. `translate([texts], sl, tl) -> [translations]` (the offline translator) finds where the phrase
    before the bracket starts (the right phrase translates back into the term); without it the length of the
    term decides."""
    out, seen, seen_inner = [], set(), set()
    for m in _PAREN.finditer(text):
        before, inner = m.group(1), m.group(2).strip()
        if re.fullmatch(r"[\d\s.,%/\-–]+|[a-zA-Z]|e\.g\.?|i\.e\.?|etc\.?|see .*|xem .*|tr\.\s*\d+|p\.\s*\d+", inner, re.I):
            continue
        inner = re.split(r"[,;]| or | hoặc | hay ", inner)[0].strip()
        if not inner or len(inner.split()) > 6:
            continue
        if inner.lower() in seen_inner:              # a term given again later in the document
            continue
        seen_inner.add(inner.lower())
        il = short_lang(inner, "en")
        seg = _CUT.split(before)[-1].strip()
        words = seg.split()
        if not words:
            continue
        ol = short_lang(seg, "en")
        if il == ol:
            continue
        content = [w for w in inner.split() if w.lower() not in STOP.get(il, ())] or inner.split()
        n_in = len(content) if il not in NO_SPACE_LANGS else max(1, len(inner) // 2)
        target_len = 2.0 * n_in if ol == "vi" else n_in
        if ol in NO_SPACE_LANGS:
            raw = [seg[-k:] for k in range(1, min(len(seg), 12) + 1)]
        else:
            raw = [" ".join(words[-k:]) for k in range(max(1, int(target_len) - 2), min(len(words), int(target_len) + 2) + 1)]
        cands = []
        for c in raw:                                 # each ending, and the same without leading small words
            cw = c.split()
            while len(cw) > 1 and cw[0].lower() in _VI_LEAD:
                cw = cw[1:]
            for x in (c, " ".join(cw)):
                if x and x not in cands:
                    cands.append(x)
        if not cands:
            continue
        guess, back = "", {}
        if translate and len(seen_inner) <= 300:      # one batch each way: the term, and every candidate back
            guess = translate([inner], il, ol)[0]
            back = dict(zip(cands, translate(cands, ol, il)))

        def score(c):
            ln = len(c) if ol in NO_SPACE_LANGS else len(c.split())
            sc = -abs(ln - target_len) * 0.1 - (0.3 if c.split()[0].lower() in _VI_LEAD else 0)
            if back:                                  # the right phrase translates back into the term
                sc += _dice(back.get(c, ""), inner) * 2.5 + _dice(c, guess)
            return sc
        best = max(cands, key=score).strip()
        bw = best.split()
        while len(bw) > 1 and bw[0].lower() in _FUNC_LEAD:     # "với học có giám sát" -> "học có giám sát"
            bw = bw[1:]
        best = " ".join(bw)
        if not best or len(best) < 2:
            continue
        if best[:1].isupper() and best[1:2].islower() and inner[:1].islower():   # capital of a sentence start
            best = best[0].lower() + best[1:]
        key = (inner.lower(), best.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((inner, il, best, ol))
    return out


HEADER_WORDS = {"english", "tiếng anh", "tiếng việt", "vietnamese", "term", "terms", "thuật ngữ", "translation",
                "bản dịch", "nghĩa", "source", "target", "word", "từ", "meaning", "definition", "français", "deutsch",
                "español", "中文", "日本語", "한국어", "русский", "en", "vi", "fr", "de", "es", "zh", "ja", "ko"}


def table_pairs(rows):
    """[(term, lang, translation, lang)] from a glossary table: two columns in different languages (a header
    row, if any, is skipped because both of its cells are in the same language)."""
    if not rows:
        return []
    width = max(len(r) for r in rows)
    if width < 2:
        return []
    # the two columns whose languages differ the most often
    langs = [[short_lang(r[i].strip(), "en") if i < len(r) and r[i].strip() else None for i in range(width)]
             for r in rows]
    best, best_n = None, 0
    for a in range(width):
        for b in range(a + 1, width):
            n = sum(1 for L in langs if L[a] and L[b] and L[a] != L[b])
            if n > best_n:
                best, best_n = (a, b), n
    if not best:                                    # same script both sides (e.g. English / French)
        best = (0, 1)
    a, b = best
    out = []
    for i, (r, L) in enumerate(zip(rows, langs)):
        if len(r) <= b or not r[a].strip() or not r[b].strip():
            continue
        if i == 0 and (r[a].strip().lower() in HEADER_WORDS or r[b].strip().lower() in HEADER_WORDS):
            continue
        if L[a] == L[b] and best_n:                 # header or a row in one language
            continue
        out.append((r[a].strip(), L[a] or "en", r[b].strip(), L[b] or "en"))
    return out


# ---------------------------------------------------------------- the library (documents + glossary)


class DomainLibrary:
    """The user's specialist documents and the glossary, kept in <data>/domain."""

    def __init__(self, folder):
        self.dir = folder
        self.files = os.path.join(folder, "files")
        self.texts = os.path.join(folder, "text")
        self.path = os.path.join(folder, "domain.json")
        self.lock = threading.RLock()
        self.docs, self.terms, self.pairs, self.ignored = [], {}, [], set()
        try:
            d = json.load(open(self.path, encoding="utf-8"))
            self.docs = d.get("docs", [])
            self.terms = d.get("terms", {})
            self.pairs = d.get("pairs", [])
            self.ignored = set(d.get("ignored", []))
        except (OSError, ValueError):
            pass
        self.version = 0                       # bumped at every change (the app re-reads the glossary)

    def save(self):
        with self.lock:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.path + ".tmp"
            json.dump({"docs": self.docs, "terms": self.terms, "pairs": self.pairs, "ignored": sorted(self.ignored)},
                      open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            os.replace(tmp, self.path)
            self.version += 1

    # ---- documents
    def add_file(self, path, translate=None, progress=None):
        """Copy a document in, read it, find its terms and term pairs. Returns the document's record."""
        progress = progress or (lambda msg: None)
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        if ext not in SUPPORTED:
            raise ValueError("loại file chưa hỗ trợ")
        did = uuid.uuid4().hex[:10]
        os.makedirs(self.files, exist_ok=True)
        os.makedirs(self.texts, exist_ok=True)
        dst = os.path.join(self.files, f"{did}_{name}")
        shutil.copy2(path, dst)
        progress(f"Đang đọc {name}...")
        text = extract_text(dst)
        open(os.path.join(self.texts, did + ".txt"), "w", encoding="utf-8").write(text)
        lang = guess_lang(text)
        doc = {"id": did, "name": name, "file": os.path.basename(dst), "lang": lang, "chars": len(text),
               "added": time.strftime("%Y-%m-%d %H:%M"), "terms": 0, "pairs": 0}
        pairs = []
        if ext in TABLE_TYPES:
            progress(f"Đang đọc bảng thuật ngữ {name}...")
            pairs = [(s, sl, t, tl, "bảng") for s, sl, t, tl in table_pairs(table_rows(dst))]
        progress(f"Đang tìm cặp thuật ngữ trong {name}...")
        pairs += [(s, sl, t, tl, "tài liệu") for s, sl, t, tl in bracket_pairs(text, translate)]
        progress(f"Đang tìm thuật ngữ trong {name}...")
        mined = mine_terms(text, lang) if ext not in TABLE_TYPES else []
        with self.lock:
            for term, count, ex in mined:
                key = f"{lang}|{term.lower()}"
                t = self.terms.get(key) or {"term": term, "lang": lang, "count": 0, "docs": [], "example": ex}
                t["count"] += count
                if did not in t["docs"]:
                    t["docs"].append(did)
                self.terms[key] = t
            added = 0
            for s, sl, t, tl, origin in pairs:
                if self._find_pair(s, sl, tl) is None:
                    self.pairs.append({"s": s, "sl": sl, "t": t, "tl": tl, "origin": origin, "on": True, "doc": did})
                    added += 1
            doc["terms"], doc["pairs"] = len(mined), added
            self.docs.append(doc)
            self.save()
        return doc

    def remove_doc(self, did):
        with self.lock:
            doc = next((d for d in self.docs if d["id"] == did), None)
            if not doc:
                return
            self.docs.remove(doc)
            for key in list(self.terms):
                t = self.terms[key]
                if did in t["docs"]:
                    t["docs"].remove(did)
                    if not t["docs"]:
                        del self.terms[key]
            self.pairs = [p for p in self.pairs if p.get("doc") != did or p.get("origin") == "tự nhập"]
            for f in (os.path.join(self.files, doc["file"]), os.path.join(self.texts, did + ".txt")):
                try:
                    os.remove(f)
                except OSError:
                    pass
            self.save()

    # ---- glossary
    def _find_pair(self, s, sl, tl):
        s = s.lower()
        for i, p in enumerate(self.pairs):
            if p["sl"] == sl and p["tl"] == tl and p["s"].lower() == s:
                return i
            if p["sl"] == tl and p["tl"] == sl and p["t"].lower() == s:
                return i
        return None

    def set_pair(self, s, sl, t, tl, origin="tự nhập"):
        """Add or change the translation of a term (sl -> tl)."""
        s, t = s.strip(), t.strip()
        if not s:
            return
        with self.lock:
            i = self._find_pair(s, sl, tl)
            if not t:
                if i is not None:
                    del self.pairs[i]
            elif i is None:
                self.pairs.append({"s": s, "sl": sl, "t": t, "tl": tl, "origin": origin, "on": True, "doc": None})
            else:
                self.pairs[i] = {**self.pairs[i], "s": s, "sl": sl, "t": t, "tl": tl, "origin": origin, "on": True}
            self.ignored.discard(f"{sl}|{s.lower()}")
            self.save()

    def toggle(self, s, sl, tl):
        with self.lock:
            i = self._find_pair(s, sl, tl)
            if i is not None:
                self.pairs[i]["on"] = not self.pairs[i].get("on", True)
            else:                                   # a found term without translation: used / not used
                k = f"{sl}|{s.lower()}"
                self.ignored.symmetric_difference_update({k})
            self.save()

    def delete(self, s, sl, tl):
        with self.lock:
            i = self._find_pair(s, sl, tl)
            if i is not None:
                del self.pairs[i]
            self.ignored.add(f"{sl}|{s.lower()}")
            self.save()

    def rows(self, src, tgt):
        """What the glossary window shows for src -> tgt: [(term, translation, origin, count, on)], pairs first
        (both directions of a stored pair are usable), then found terms that have no translation yet."""
        with self.lock:
            out, have = [], set()
            for p in self.pairs:
                if p["sl"] == src and p["tl"] == tgt:
                    s, t = p["s"], p["t"]
                elif p["sl"] == tgt and p["tl"] == src:
                    s, t = p["t"], p["s"]
                else:
                    continue
                if s.lower() in have:
                    continue
                have.add(s.lower())
                cnt = (self.terms.get(f"{src}|{s.lower()}") or {}).get("count", 0)
                out.append((s, t, p.get("origin", ""), cnt, p.get("on", True)))
            for key, t in sorted(self.terms.items(), key=lambda kv: -kv[1]["count"]):
                if t["lang"] != src or t["term"].lower() in have:
                    continue
                out.append((t["term"], "", "tìm thấy", t["count"], key not in self.ignored))
            return out

    def pairs_for(self, src, tgt):
        """[(term, translation)] in use for src -> tgt."""
        return [(s, t) for s, t, _, _, on in self.rows(src, tgt) if on and t]

    def prompt_terms(self, lang, max_chars=220):
        """The field's words for the speech recognizer's prompt (most frequent first)."""
        with self.lock:
            cands = [(t["count"] + 5, t["term"]) for k, t in self.terms.items()
                     if t["lang"] == lang and k not in self.ignored]
            for p in self.pairs:
                if p.get("on", True):
                    if p["sl"] == lang:
                        cands.append((3, p["s"]))
                    elif p["tl"] == lang:
                        cands.append((3, p["t"]))
        out, seen, n = [], set(), 0
        for _, term in sorted(cands, key=lambda x: -x[0]):
            if term.lower() in seen:
                continue
            if n + len(term) + 2 > max_chars:
                break
            seen.add(term.lower())
            out.append(term)
            n += len(term) + 2
        return ", ".join(out)

    def missing_translation(self, src, tgt, limit=60):
        """Found terms of src without a translation into tgt, most frequent first, with an example sentence."""
        with self.lock:
            out = []
            for s, t, origin, cnt, on in self.rows(src, tgt):
                if not t and on:
                    ex = (self.terms.get(f"{src}|{s.lower()}") or {}).get("example", "")
                    out.append((s, ex))
                if len(out) >= limit:
                    break
            return out

    def search(self, query, max_chars=12000):
        """Passages of the documents that mention the query words (for the Claude panel)."""
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]
        if not words:
            return ""
        hits = []
        for d in self.docs:
            try:
                text = open(os.path.join(self.texts, d["id"] + ".txt"), encoding="utf-8").read()
            except OSError:
                continue
            paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[A-ZÀ-Ỹ•\-\d])", text) if p.strip()]
            for p in paras:
                low = p.lower()
                score = sum(low.count(w) for w in words) + 3 * (query.lower() in low)
                if score:
                    hits.append((score, d["name"], p[:1500]))
        hits.sort(key=lambda h: -h[0])
        out, n = [], 0
        for _, name, p in hits:
            block = f"[{name}]\n{p}\n"
            if n + len(block) > max_chars:
                break
            out.append(block)
            n += len(block)
        return "\n".join(out)


# ---------------------------------------------------------------- suggestions from the local model

SUGGEST_SYSTEM = ("You translate technical terms of a field from {sl} into {tl}, using the standard {tl} terminology "
                  "of that field. Keep names and acronyms. Copy each {sl} term, then => and the {tl} term.")


def suggest(llm, items, sl_name, tl_name, topic="", examples=()):
    """{term: translation} suggested by the local instruction model for [(term, example sentence)]. Pairs the
    glossary already has are shown as examples (a small model follows examples far better than rules)."""
    user = (f"Field: {topic}\n" if topic else "")
    if examples:
        user += "Examples:\n" + "\n".join(f"{a} => {b}" for a, b in examples) + "\n\n"
    user += "Translate:\n" + "\n".join(t for t, _ in items)
    out = llm.chat(SUGGEST_SYSTEM.format(sl=sl_name, tl=tl_name), user, max_new=40 + 25 * len(items))
    got = {}
    wanted = {t.lower(): t for t, _ in items}
    lines = [ln for ln in out.splitlines() if ln.strip()]
    for ln in lines:
        m = re.match(r"^\s*[-•*\d.)]*\s*(.+?)\s*(=>|->|→|=)\s*(.+?)\s*$", ln)
        if not m:
            continue
        a, b = m.group(1).strip(" \"'`"), m.group(3).strip(" \"'`")
        if a.lower() in wanted and b and b.lower() != a.lower() and len(b) < 80:
            got[wanted[a.lower()]] = b
    if not got and len(lines) == len(items):          # translations only, one per line, in order
        for (t, _), ln in zip(items, lines):
            b = re.sub(r"^\s*[-•*\d.)]+\s*", "", ln).strip(" \"'`")
            if b and b.lower() != t.lower() and len(b) < 80:
                got[t] = b
    return got


# ---------------------------------------------------------------- using the glossary while translating


class TermMatcher:
    """Finds glossary terms in a sentence (longest first, any case, English plurals too)."""

    def __init__(self, pairs, src):
        self.src = src
        self.map = {}
        for s, t in pairs:
            if s.strip() and t.strip():
                self.map.setdefault(s.strip().lower(), t.strip())
        self.rx = None
        if self.map:
            alts = "|".join(re.escape(k) for k in sorted(self.map, key=len, reverse=True))
            plural = r"(?:e?s)?" if src == "en" else ""
            if src in NO_SPACE_LANGS:
                self.rx = re.compile(f"(?:{alts})", re.I)
            else:
                self.rx = re.compile(rf"(?<![\w-])(?:{alts}){plural}(?![\w-])", re.I)

    def __bool__(self):
        return self.rx is not None

    def lookup(self, found):
        k = found.lower()
        if k in self.map:
            return self.map[k]
        if self.src == "en":
            if k.endswith("es") and k[:-2] in self.map:
                return self.map[k[:-2]]
            if k.endswith("s") and k[:-1] in self.map:
                return self.map[k[:-1]]
        return None

    def mask(self, text):
        """(text with each term replaced by X1, X2..., [translations]) and the code-switched text (each term
        replaced by its translation), the fallback when the translator loses a placeholder."""
        vals = []

        def ph(m):
            t = self.lookup(m.group(0))
            if t is None:
                return m.group(0)
            vals.append(t)
            return f"X{len(vals)}"
        masked = self.rx.sub(ph, text) if self.rx else text
        switched = self.rx.sub(lambda m: self.lookup(m.group(0)) or m.group(0), text) if self.rx else text
        return masked, vals, switched


_PH = re.compile(r"(?<![^\W\d_])[Xx]\s?(\d{1,2})(?!\d)")


def unmask(out, vals, cap=True):
    """Put the translations back in place of X1, X2... Returns (text, ok): ok = every placeholder came back
    exactly once. cap: `out` starts a sentence (a translation put first gets a capital letter)."""
    seen = []

    def rep(m):
        i = int(m.group(1))
        if 1 <= i <= len(vals):
            seen.append(i)
            return vals[i - 1]
        return m.group(0)
    text = _PH.sub(rep, out)
    ok = sorted(seen) == list(range(1, len(vals) + 1))
    if cap and text and text[0].islower() and out[:1] in ("X", "x"):
        text = text[0].upper() + text[1:]
    return text, ok


class StreamUnmasker:
    """Streamed translation pieces -> pieces with the placeholders already replaced (a piece ending in 'X' or
    'X1' is held back until the next one shows whether a placeholder continues)."""

    def __init__(self, vals, emit):
        self.vals, self.emit, self.held, self.raw = vals, emit, "", ""
        self.started = False

    def feed(self, piece):
        self.raw += piece
        s = self.held + piece
        m = re.search(r"[Xx]\s?\d{0,2}$", s)
        if m:
            s, self.held = s[:m.start()], s[m.start():]
        else:
            self.held = ""
        if s:
            self.emit(unmask(s, self.vals, cap=not self.started)[0])
            self.started = True

    def close(self):
        if self.held:
            self.emit(unmask(self.held, self.vals, cap=not self.started)[0])
            self.held = ""
        return unmask(self.raw.strip(), self.vals)
