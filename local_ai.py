"""Everything that runs on this PC without internet: the language list, the model packs (downloaded once per
language, then used offline), local translation (NLLB-200) and local key takeaways (Qwen2.5 Instruct).

Packs live in <DATA_DIR>/models/<pack id>/ (one copy for every app version). A PC without internet can get
them by copying that folder from another PC (the 📦 Gói ngôn ngữ window opens it).

Engines (all through ctranslate2, already used by Whisper - no PyTorch, no internet, no public LLM):
  speech      Vosk (live words) + Whisper (accurate text); English models are bundled in the .exe;
              Vietnamese: Zipformer (k2-fsa, through sherpa-onnx) - 15x faster than Whisper small, as accurate
  translate   EnViT5 (int8) for English <-> Vietnamese; NLLB-200 distilled 600M (int8) for every other pair
  takeaways   Qwen2.5 Instruct 1.5B (int8) - or 3B for better notes on a strong PC
"""
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile

# ---------------------------------------------------------------------------------------------------------
# Languages: code -> (name shown, Whisper code, NLLB code, Vosk small model zip name or None)
# Main languages only: each has Whisper + NLLB support; most also have a small Vosk model for live words.
# ---------------------------------------------------------------------------------------------------------
LANGS = {
    "en": ("English", "en", "eng_Latn", "bundled"),
    "vi": ("Tiếng Việt", "vi", "vie_Latn", None),       # (Vosk vn 0.4 was slower than real time: Zipformer)
    "zh": ("中文 (Chinese)", "zh", "zho_Hans", "vosk-model-small-cn-0.22"),
    "ja": ("日本語 (Japanese)", "ja", "jpn_Jpan", "vosk-model-small-ja-0.22"),
    "ko": ("한국어 (Korean)", "ko", "kor_Hang", "vosk-model-small-ko-0.22"),
    "fr": ("Français", "fr", "fra_Latn", "vosk-model-small-fr-0.22"),
    "de": ("Deutsch", "de", "deu_Latn", "vosk-model-small-de-0.15"),
    "es": ("Español", "es", "spa_Latn", "vosk-model-small-es-0.42"),
    "pt": ("Português", "pt", "por_Latn", "vosk-model-small-pt-0.3"),
    "it": ("Italiano", "it", "ita_Latn", "vosk-model-small-it-0.22"),
    "ru": ("Русский (Russian)", "ru", "rus_Cyrl", "vosk-model-small-ru-0.22"),
    "uk": ("Українська (Ukrainian)", "uk", "ukr_Cyrl", "vosk-model-small-uk-v3-small"),
    "pl": ("Polski", "pl", "pol_Latn", "vosk-model-small-pl-0.22"),
    "nl": ("Nederlands", "nl", "nld_Latn", "vosk-model-small-nl-0.22"),
    "tr": ("Türkçe", "tr", "tur_Latn", "vosk-model-small-tr-0.3"),
    "hi": ("हिन्दी (Hindi)", "hi", "hin_Deva", "vosk-model-small-hi-0.22"),
    "ar": ("العربية (Arabic)", "ar", "arb_Arab", "vosk-model-small-ar-0.3"),
    "th": ("ไทย (Thai)", "th", "tha_Thai", None),
    "id": ("Bahasa Indonesia", "id", "ind_Latn", None),
}
AUTO = "auto"                                   # source language: let Whisper tell, utterance by utterance
NO_SPACES = {"zh", "ja", "th"}                  # scripts written without spaces between words
LANG_NAMES_EN = {"en": "English", "vi": "Vietnamese", "zh": "Simplified Chinese", "ja": "Japanese",
                 "ko": "Korean", "fr": "French", "de": "German", "es": "Spanish", "pt": "Portuguese",
                 "it": "Italian", "ru": "Russian", "uk": "Ukrainian", "pl": "Polish", "nl": "Dutch",
                 "tr": "Turkish", "hi": "Hindi", "ar": "Arabic", "th": "Thai", "id": "Indonesian"}


def lang_name(code):
    if code == AUTO:
        return "Tự nhận diện"
    return LANGS.get(code, (code,))[0]


# ---------------------------------------------------------------------------------------------------------
# Model packs
# ---------------------------------------------------------------------------------------------------------
HF = "https://huggingface.co/{repo}/resolve/main/{file}"
VOSK_URL = "https://alphacephei.com/vosk/models/{name}.zip"
SHERPA_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{name}.tar.bz2"
# languages with a fast offline recognizer (whole utterance in ~0.05x its length): it gives the live words
# (re-read every ~0.6 s while someone speaks) and the final text; Whisper is then only the "Cao" option
FAST_ASR = {"vi": "asr-vi"}


class Pack:
    def __init__(self, pid, title, size_mb, repo=None, files=None, zip_url=None, check="model.bin", group=""):
        self.id, self.title, self.size_mb = pid, title, size_mb
        self.repo, self.files, self.zip_url, self.check, self.group = repo, files or [], zip_url, check, group


WHISPER_FILES = ["config.json", "tokenizer.json", "vocabulary.txt", "model.bin"]    # model.bin last: marks done
PACKS = {}


def _add(p):
    PACKS[p.id] = p


_add(Pack("nllb-600m", "Dịch offline — NLLB-200 (mọi cặp ngôn ngữ)", 630, "JustFrederik/nllb-200-distilled-600M-ct2-int8",
          ["config.json", "shared_vocabulary.txt", "tokenizer.json", "model.bin"], group="Dịch"))
_add(Pack("envit5", "Dịch Anh ↔ Việt — EnViT5 (hay hơn và nhanh gấp 3 lần NLLB)", 280,
          "luigi000/envit5-translation-ct2-int8",
          ["config.json", "shared_vocabulary.json", "spiece.model", "model.bin"], group="Dịch"))
_add(Pack("qwen2.5-1.5b", "Key takeaways offline — Qwen2.5 1.5B (nhanh)", 1560, "jncraton/Qwen2.5-1.5B-Instruct-ct2-int8",
          ["config.json", "vocabulary.json", "tokenizer.json", "tokenizer_config.json", "model.bin"], group="Ý chính"))
_add(Pack("qwen2.5-3b", "Key takeaways offline — Qwen2.5 3B (hay hơn, cần máy mạnh)", 3100,
          "jncraton/Qwen2.5-3B-Instruct-ct2-int8",
          ["config.json", "vocabulary.json", "tokenizer.json", "tokenizer_config.json", "model.bin"], group="Ý chính"))
_add(Pack("asr-vi", "Nghe tiếng Việt — Zipformer (nhanh, chính xác)", 75,
          zip_url=SHERPA_URL.format(name="sherpa-onnx-zipformer-vi-int8-2025-04-20"), check="tokens.txt",
          group="Nghe"))
_add(Pack("whisper-base", "Nghe đa ngôn ngữ — Whisper base (nhanh, cho máy yếu)", 145,
          "Systran/faster-whisper-base", WHISPER_FILES, group="Nghe"))
_add(Pack("whisper-small", "Nghe đa ngôn ngữ — Whisper small (mọi ngôn ngữ trừ tiếng Anh)", 485,
          "Systran/faster-whisper-small", WHISPER_FILES, group="Nghe"))
_add(Pack("whisper-medium", "Nghe đa ngôn ngữ chất lượng Cao — Whisper medium (chậm hơn)", 1530,
          "Systran/faster-whisper-medium", WHISPER_FILES, group="Nghe"))
_add(Pack("small.en", "Nghe tiếng Anh chất lượng Cao — Whisper small.en", 485,
          "Systran/faster-whisper-small.en", WHISPER_FILES, group="Nghe"))
for _code, (_name, _w, _n, _vosk) in LANGS.items():
    if _vosk and _vosk != "bundled":
        _add(Pack(f"vosk-{_code}", f"Chữ hiện ngay khi nói — {_name} (Vosk)", 0, zip_url=VOSK_URL.format(name=_vosk),
                  check="am", group="Nghe"))
VOSK_SIZES = {"zh": 42, "ja": 48, "ko": 83, "fr": 40, "de": 44, "es": 38, "pt": 31, "it": 47, "ru": 44,
              "uk": 137, "pl": 51, "nl": 39, "tr": 35, "hi": 42, "ar": 100}
for _c, _s in VOSK_SIZES.items():
    PACKS[f"vosk-{_c}"].size_mb = _s

LLM_PACKS = ["qwen2.5-1.5b", "qwen2.5-3b"]
MT_PACKS = ["envit5", "nllb-600m"]           # translation models, the better one for a pair first
EN_VI = {"en", "vi"}


def mt_packs(src, tgt):
    """Translation packs a choice needs: English <-> Vietnamese has its own model (EnViT5); every other pair,
    and speech in any language ("auto"), uses NLLB-200."""
    if {src, tgt} == EN_VI:
        return ["envit5"]
    return (["envit5"] if src == AUTO and tgt in EN_VI else []) + ["nllb-600m"]


def mt_pack_for(src, tgt, have):
    """Which installed translation pack to use for src -> tgt (have(pid) -> bool), else None."""
    if {src, tgt} == EN_VI and have("envit5"):
        return "envit5"
    return "nllb-600m" if have("nllb-600m") else None
BUNDLED = {"base.en", "vosk-en-small", "spk-onnx"}      # inside the .exe


class Store:
    """Finds and downloads packs. `roots`: folders that may hold a models/ folder, the writable one first."""

    def __init__(self, data_dir, read_only_roots=()):
        self.dir = os.path.join(data_dir, "models")
        self.roots = [self.dir] + [os.path.join(r, "models") for r in read_only_roots if r]
        self.lock = threading.Lock()
        self.jobs = {}                         # pack id -> {"done": bytes, "total": bytes, "error": str|None}

    def path(self, pid):
        """Folder of an installed pack (or bundled model), else None."""
        pack = PACKS.get(pid)
        check = pack.check if pack else ""
        # a cut download leaves a model file much smaller than the real one: that pack is not installed
        min_bytes = int(pack.size_mb * 0.8e6) if pack and pack.repo and check == "model.bin" else 0
        for r in self.roots:
            p = os.path.join(r, pid)
            f = os.path.join(p, check) if check else ""
            if os.path.isdir(p) and (not check or (os.path.exists(f) and os.path.getsize(f) >= min_bytes)):
                return p
        return None

    def have(self, pid):
        return self.path(pid) is not None

    # ---- which packs a choice needs ----
    @staticmethod
    def needed(src, tgt, quality_high=False, takeaways=None):
        """Pack ids needed for listening to `src` and translating into `tgt` (+ the takeaways model)."""
        out = []
        if src == "en":
            out.append("small.en" if quality_high else None)
        elif src in FAST_ASR:
            out.append(FAST_ASR[src])
            out.append("whisper-small" if quality_high else None)
        else:
            out.append("whisper-medium" if quality_high else "whisper-small")
            out.append("whisper-base")                  # catches up when the PC falls behind
            if src != AUTO and f"vosk-{src}" in PACKS:
                out.append(f"vosk-{src}")
        if src != tgt:
            out.extend(mt_packs(src, tgt))
        if takeaways:
            out.append(takeaways)
        return [p for p in out if p]

    def missing(self, pids):
        return [p for p in pids if not self.have(p)]

    # ---- download ----
    def download(self, pid, progress=None, cancel=None):
        """Download one pack into <data>/models/<pid> (files written as .part, renamed when whole, so a cut
        download is never used and is resumed next time). progress(done_bytes, total_bytes)."""
        p = PACKS[pid]
        dst = os.path.join(self.dir, pid)
        os.makedirs(dst, exist_ok=True)
        progress = progress or (lambda d, t: None)
        if p.zip_url:
            self._download_zip(p, dst, progress, cancel)
            return dst
        sizes = {}
        for f in p.files:
            req = urllib.request.Request(HF.format(repo=p.repo, file=f), method="HEAD", headers=_UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                sizes[f] = int(r.headers.get("Content-Length") or 0)
        total, done = sum(sizes.values()), 0
        progress(0, total)
        for f in p.files:
            final = os.path.join(dst, f)
            if os.path.isfile(final) and (not sizes[f] or os.path.getsize(final) == sizes[f]):
                done += sizes[f]
                continue
            done = self._fetch(HF.format(repo=p.repo, file=f), final, done, total, progress, cancel)
        for f in p.files:                          # every file whole, or the pack is not finished
            final = os.path.join(dst, f)
            if sizes[f] and os.path.getsize(final) != sizes[f]:
                os.remove(final)
                raise OSError(f"tải {f} chưa đủ ({os.path.getsize(final) if os.path.exists(final) else 0} / "
                              f"{sizes[f]} byte), hãy tải lại")
        progress(total, total)
        return dst

    def _fetch(self, url, final, done, total, progress, cancel):
        """Download url -> final, resuming after a dropped connection (big files on a slow line often stall
        once or twice). Returns the new byte count."""
        for attempt in range(6):
            try:
                return self._fetch_once(url, final, done, total, progress, cancel)
            except (OSError, urllib.error.URLError) as e:
                if (cancel and cancel.is_set()) or attempt == 5:
                    raise
                if isinstance(e, urllib.error.HTTPError) and e.code in (403, 404, 416):
                    raise
                time.sleep(2 + attempt * 3)
        return done

    def _fetch_once(self, url, final, done, total, progress, cancel):
        part = final + ".part"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        headers = dict(_UA)
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            if have and r.status != 206:             # server ignored the range: start over
                have = 0
            # the whole file's size: the server may close the connection early without an error
            whole = 0
            rng = r.headers.get("Content-Range") or ""
            if "/" in rng and rng.rsplit("/", 1)[1].strip().isdigit():
                whole = int(rng.rsplit("/", 1)[1])
            elif r.headers.get("Content-Length"):
                whole = have + int(r.headers["Content-Length"])
            done += have
            with open(part, "ab" if have else "wb") as out:
                while True:
                    if cancel and cancel.is_set():
                        raise RuntimeError("đã huỷ")
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    progress(done, total)
        if whole and os.path.getsize(part) < whole:          # cut short: the retry resumes from here
            raise OSError(f"connection closed at {os.path.getsize(part)} of {whole} bytes")
        os.replace(part, final)
        return done

    def _download_zip(self, p, dst, progress, cancel):
        tmp = dst + (".tar.bz2" if p.zip_url.endswith(".tar.bz2") else ".zip")
        req = urllib.request.Request(p.zip_url, method="HEAD", headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            total = int(r.headers.get("Content-Length") or 0)
        if not (os.path.exists(tmp) and os.path.getsize(tmp) == total):
            self._fetch(p.zip_url, tmp, 0, total, progress, cancel)
        if tmp.endswith(".tar.bz2"):
            self._untar(tmp, dst)
            os.remove(tmp)
            progress(total, total)
            return
        # the zip holds one top folder (vosk-model-small-xx-0.yy/...): its content goes straight into dst
        with zipfile.ZipFile(tmp) as z:
            for info in z.infolist():
                parts = info.filename.replace("\\", "/").split("/", 1)
                if len(parts) < 2 or not parts[1] or info.is_dir():
                    continue
                out = os.path.normpath(os.path.join(dst, parts[1]))
                if not out.startswith(os.path.normpath(dst) + os.sep):      # never write outside the pack
                    continue
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with z.open(info) as src, open(out, "wb") as f:
                    shutil.copyfileobj(src, f)
        os.remove(tmp)
        progress(total, total)

    @staticmethod
    def _untar(tmp, dst):
        """A sherpa-onnx model archive: its top folder's files go into dst (not the sample recordings)."""
        import tarfile
        with tarfile.open(tmp, "r:bz2") as t:
            for m in t.getmembers():
                parts = m.name.replace("\\", "/").split("/", 1)
                if len(parts) < 2 or not m.isfile() or "test_wavs" in parts[1]:
                    continue
                out = os.path.normpath(os.path.join(dst, parts[1]))
                if not out.startswith(os.path.normpath(dst) + os.sep):
                    continue
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with t.extractfile(m) as src, open(out, "wb") as f:
                    shutil.copyfileobj(src, f)

    def remove(self, pid):
        p = os.path.join(self.dir, pid)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)

    def size_on_disk_mb(self, pid):
        p = self.path(pid)
        if not p:
            return 0
        n = 0
        for dp, _, files in os.walk(p):
            n += sum(os.path.getsize(os.path.join(dp, f)) for f in files)
        return n / 1e6


_UA = {"User-Agent": "LiveTranslator"}


# ---------------------------------------------------------------------------------------------------------
# Text helpers for scripts without spaces, and for comparing a live draft with the accurate text
# ---------------------------------------------------------------------------------------------------------
_CJK = r"぀-ヿ㐀-䶿一-鿿豈-﫿฀-๿"
_CJK_GAP = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")


def tidy(text, lang):
    """Vosk writes Chinese/Japanese/Thai with a space between words: join them as the language is written."""
    if lang in NO_SPACES:
        return _CJK_GAP.sub("", text)
    return text


def words(text):
    """Comparable units of a text in any language: words, or single characters for scripts without spaces."""
    t = re.sub(r"[^\w']+", " ", text.lower())
    out = []
    for w in t.split():
        if re.search(rf"[{_CJK}]", w):
            out.extend(ch for ch in w if not ch.isspace())
        else:
            out.append(w)
    return out


def speech_units(text):
    """About how many words were said (a Chinese/Japanese/Thai character is ~1/2 word)."""
    n = 0
    for w in words(text):
        n += 0.5 if re.match(rf"[{_CJK}]", w) else 1
    return n


# ---------------------------------------------------------------------------------------------------------
# Fast offline recognizer (sherpa-onnx transducer): Vietnamese
# ---------------------------------------------------------------------------------------------------------
class FastASR:
    def __init__(self, path, threads=4):
        import glob
        import sherpa_onnx

        def one(pattern):
            found = sorted(glob.glob(os.path.join(path, pattern)))
            if not found:
                raise FileNotFoundError(os.path.join(path, pattern))
            return found[0]
        self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=one("encoder*.onnx"), decoder=one("decoder*.onnx"), joiner=one("joiner*.onnx"),
            tokens=one("tokens.txt"), num_threads=threads, decoding_method="greedy_search")
        self.lock = threading.Lock()

    def decode(self, audio, sr=16000):
        """Text of one piece of speech, written normally ("xin chào các bạn", not "XIN CHÀO CÁC BẠN")."""
        import numpy as np
        with self.lock:
            st = self.rec.create_stream()
            st.accept_waveform(sr, np.ascontiguousarray(audio, dtype=np.float32))
            self.rec.decode_stream(st)
            text = st.result.text.strip()
        text = text.lower()
        return text[:1].upper() + text[1:]


# ---------------------------------------------------------------------------------------------------------
# Local translation: NLLB-200 through ctranslate2
# ---------------------------------------------------------------------------------------------------------
class CT2MT:
    """A translation model run by CTranslate2 on this PC. Subclasses say how text becomes tokens (encode),
    tokens become text (decode) and which language pairs they know (supports)."""
    MAX_TOKENS = 200                 # trained on sentences: longer input is split first
    beam = 2

    def __init__(self, path, threads=4):
        import ctranslate2
        self.model = ctranslate2.Translator(path, device="cpu", compute_type="int8", inter_threads=1,
                                            intra_threads=threads)
        self.lock = threading.Lock()

    # ---- what a subclass provides
    def supports(self, src, tgt):
        return src in LANGS and tgt in LANGS

    def encode(self, text, src, tgt):
        """(source tokens, target prefix tokens)"""
        raise NotImplementedError

    def decode(self, tokens):
        raise NotImplementedError

    def skip(self, token):
        return token == "</s>" or (token.startswith("<") and token.endswith(">"))

    # ---- shared
    def _run(self, texts, src, tgt, max_len=256):
        enc = [self.encode(t, src, tgt) for t in texts]
        with self.lock:
            res = self.model.translate_batch([e[0] for e in enc], target_prefix=[e[1] or None for e in enc],
                                             beam_size=self.beam, max_decoding_length=max_len,
                                             repetition_penalty=1.1, no_repeat_ngram_size=4, max_batch_size=16)
        out = []
        for (_, prefix), r in zip(enc, res):
            toks = r.hypotheses[0][len(prefix):] if prefix and r.hypotheses[0][:len(prefix)] == prefix else \
                r.hypotheses[0]
            out.append(self.decode([t for t in toks if not self.skip(t)]).strip())
        return out

    def translate(self, text, src, tgt, sentences=None, matcher=None):
        """Translate `text` (a segment of speech) from language code src into tgt. Long segments are cut into
        sentences, translated together in one batch, and joined again. matcher (domain.TermMatcher): glossary
        terms are kept out of the model's hands (placeholders) and put back translated as the glossary says."""
        text = text.strip()
        if not text or src == tgt:
            return text
        if matcher:
            return self._translate_terms(text, src, tgt, matcher)
        parts = sentences or split_for_mt(text, src)
        joiner = "" if tgt in NO_SPACES else " "
        return joiner.join(o for o in self._run(parts, src, tgt) if o)

    def translate_many(self, texts, src, tgt):
        """Short texts (terms) translated in one batch: [translation per text]."""
        if not texts or src == tgt:
            return list(texts)
        return self._run(list(texts), src, tgt, max_len=64)

    def _translate_terms(self, text, src, tgt, matcher):
        import domain
        parts = split_for_mt(text, src)
        masked = [matcher.mask(p) for p in parts]
        raws = self._run([m for m, _, _ in masked], src, tgt)
        outs = []
        for (m, vals, switched), raw in zip(masked, raws):
            if not vals:
                outs.append(raw)
                continue
            fixed, ok = domain.unmask(raw, vals)
            # the model lost or doubled a placeholder: translate the sentence with the terms already in place
            outs.append(fixed if ok else self._run([switched], src, tgt)[0])
        joiner = "" if tgt in NO_SPACES else " "
        return joiner.join(o for o in outs if o)

    def stream_terms(self, text, src, tgt, on_piece, matcher):
        """stream() with the glossary (see translate). Returns (full text, ok): ok False = a sentence had to be
        translated again after it was streamed, so the caller replaces what was shown with the full text."""
        import domain
        space = "" if tgt in NO_SPACES else " "
        full, all_ok = [], True
        for p in split_for_mt(text.strip(), src):
            m, vals, switched = matcher.mask(p)
            lead = [bool(full)]

            def emit(piece):
                if lead[0] and piece.strip():
                    piece = space + piece.lstrip()
                    lead[0] = False
                on_piece(piece)
            if not vals:
                full.append(self.stream(m, src, tgt, emit))
                continue
            um = domain.StreamUnmasker(vals, emit)
            self.stream(m, src, tgt, um.feed)
            out, ok = um.close()
            if not ok:
                out, all_ok = self._run([switched], src, tgt)[0], False
            full.append(out)
        return space.join(o for o in full if o).strip(), all_ok

    def stream(self, text, src, tgt, on_piece):
        """Like translate(), but each word is handed to on_piece(text) as soon as it is decoded (the first
        word shows ~0.2 s after the call, instead of the whole sentence after ~1 s). Returns the full text."""
        text = text.strip()
        if not text or src == tgt:
            on_piece(text)
            return text
        space = "" if tgt in NO_SPACES else " "
        full = ""
        with self.lock:
            for p in split_for_mt(text, src):
                toks, prefix = self.encode(p, src, tgt)
                first, n = True, 0
                for step in self.model.generate_tokens(toks, target_prefix=prefix or None, max_decoding_length=256,
                                                       repetition_penalty=1.1, no_repeat_ngram_size=4):
                    t = step.token
                    if n < len(prefix) and t == prefix[n]:          # the forced start (language tag, "vi:")
                        n += 1
                        continue
                    n = len(prefix)
                    if self.skip(t):
                        continue
                    piece = t.replace("▁", space)
                    if first:
                        piece = piece.lstrip()
                        if full:
                            piece = space + piece
                        first = False
                    if piece:
                        full += piece
                        on_piece(piece)
        return full.strip()


class NLLB(CT2MT):
    """NLLB-200 (Meta): one model for every pair of the app's languages."""

    def __init__(self, path, threads=4):
        super().__init__(path, threads)
        from tokenizers import Tokenizer
        self.tok = Tokenizer.from_file(os.path.join(path, "tokenizer.json"))

    def _pieces(self, text):
        return self.tok.encode(text, add_special_tokens=False).tokens

    def encode(self, text, src, tgt):
        return [LANGS[src][2]] + self._pieces(text)[: self.MAX_TOKENS] + ["</s>"], [LANGS[tgt][2]]

    def decode(self, tokens):
        return self.tok.decoder.decode(tokens) if self.tok.decoder else "".join(tokens).replace("▁", " ")


class EnViT5(CT2MT):
    """EnViT5 (VietAI): English <-> Vietnamese only, trained on far more English-Vietnamese text than NLLB.
    Input "en: <text>" -> output "vi: <text>" (and the other way round)."""

    def __init__(self, path, threads=4):
        super().__init__(path, threads)
        import sentencepiece as spm
        self.sp = spm.SentencePieceProcessor(model_file=os.path.join(path, "spiece.model"))
        self.prefix = {lang: self.sp.encode(f"{lang}:", out_type=str) for lang in ("en", "vi")}

    def supports(self, src, tgt):
        return {src, tgt} == {"en", "vi"}

    def encode(self, text, src, tgt):
        return self.sp.encode(f"{src}: {text}", out_type=str)[: self.MAX_TOKENS] + ["</s>"], self.prefix[tgt]

    def decode(self, tokens):
        return re.sub(r"^\s*(vi|en)\s*:\s*", "", self.sp.decode(tokens))


_MT_SPLIT = re.compile(r"(?<=[.!?…。！？])\s*")


def split_for_mt(text, lang):
    parts = [p.strip() for p in _MT_SPLIT.split(text) if p.strip()]
    out = []
    for p in parts:                      # a very long run without full stops: cut at commas
        while len(p) > 400:
            cut = max(p.rfind(",", 0, 400), p.rfind("，", 0, 400), p.rfind("、", 0, 400))
            cut = cut if cut > 100 else 400
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    return out or [text]


# ---------------------------------------------------------------------------------------------------------
# Local key takeaways: a small instruction model (Qwen2.5) through ctranslate2
# ---------------------------------------------------------------------------------------------------------
class LocalLLM:
    def __init__(self, path, threads=4):
        import ctranslate2
        from tokenizers import Tokenizer
        self.tok = Tokenizer.from_file(os.path.join(path, "tokenizer.json"))
        self.model = ctranslate2.Generator(path, device="cpu", compute_type="int8", intra_threads=threads)
        self.lock = threading.Lock()

    def chat(self, system, user, max_new=400, temperature=0.0, yield_to=None):
        """yield_to() -> True while something more urgent needs the CPU (live translation): writing pauses
        between words until it is False again (at most 30 s in all, so takeaways still come)."""
        prompt = (f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n"
                  f"<|im_start|>assistant\n")
        toks = self.tok.encode(prompt, add_special_tokens=False).tokens
        kw = dict(max_length=max_new, include_prompt_in_result=False, end_token=["<|im_end|>", "<|endoftext|>"],
                  repetition_penalty=1.08, no_repeat_ngram_size=6)
        if temperature > 0:
            kw.update(sampling_temperature=temperature, sampling_topk=20)
        if yield_to:
            waited = [0.0]

            def step(_):
                while yield_to() and waited[0] < 30:
                    time.sleep(0.05)
                    waited[0] += 0.05
                return False
            kw["callback"] = step
        with self.lock:
            res = self.model.generate_batch([toks], **kw)
        return self.tok.decode(res[0].sequences_ids[0], skip_special_tokens=True).strip()


TAKEAWAY_SYSTEM = (
    "You write KEY TAKEAWAYS (short notes) of a live talk, in {lang}. You get the takeaways already written and "
    "the NEW part of the transcript. Write only the important new ideas from the NEW part that are not already in "
    "the existing takeaways: 1 to 4 bullet points, each one short sentence starting with '• ', most important "
    "first. Small talk, greetings and logistics are not takeaways. Never comment on the transcript itself. If "
    "the new part has nothing important, answer exactly NONE. Answer only with the bullets (or NONE), in {lang}.")


def clean_bullets(out, max_n=5):
    """Model output -> '• ...' lines (or '' for nothing new)."""
    out = (out or "").strip()
    if not out or re.fullmatch(r"\W*none\W*", out, re.I):
        return ""
    lines = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln or re.fullmatch(r"\W*none\W*", ln, re.I):
            continue
        ln = re.sub(r"^(\d+[.)]|[-*•·●▪]|•)\s*", "", ln).strip()
        ln = re.sub(r"\*\*(.+?)\*\*", r"\1", ln)
        if len(ln) < 4 or ln.endswith(":") and len(ln) < 40:     # headings like "Key takeaways:"
            continue
        if ln not in lines:
            lines.append(ln)
    return "\n".join("• " + ln for ln in lines[:max_n])


def free_threads():
    n = os.cpu_count() or 4
    return max(1, min(4, n // 3))


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def human_mb(mb):
    return f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb:.0f} MB"


def eta(start, done, total):
    if not done or not total:
        return ""
    left = (time.time() - start) * (total - done) / done
    return f", còn ~{left / 60:.0f} phút" if left > 90 else f", còn ~{left:.0f} s"
