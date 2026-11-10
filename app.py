"""Live English -> Vietnamese translator.

Login with an Anthropic (Claude) API key, then press ON to:
  - left pane:   live English transcript from the microphone (Whisper, offline)
  - right pane:  live Vietnamese translation (Claude)
  - bottom pane: running Vietnamese key takeaways (Claude)
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

import anthropic
import numpy as np

import learning

# ---------------- Settings ----------------
APP_VERSION = "1.8.3.2"            # shown in the title bar and used in the portable build's name
MODEL = os.environ.get("TRANSLATE_MODEL", "claude-opus-5-5")
# Live words come from Vosk (streaming); Whisper base.en then corrects each finished utterance.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
SAMPLE_RATE = 16000
FRAME = 480                      # 30 ms frames
VOSK_MODEL = "vosk-en-small"      # streaming model for live words (fast: ~0.2 s per 1 s audio)
UTT_SOFT = 3.0                   # after this long, cut the utterance at the next short pause (s)
UTT_HARD = 7.0                   # always cut by this length (s)
TEACHER_MINUTES = 10             # whoever has spoken this long becomes "Teacher" (several -> Teacher 1, 2...)
SUMMARY_EVERY = 25.0             # refresh key takeaways this often (seconds)
# Folder next to the .exe (portable) or next to app.py.
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
IS_MAC = sys.platform == "darwin"
# Where settings are saved. On Mac the .app bundle is read-only (and signed), so use the user's
# Application Support folder there; on Windows keep config.json next to the .exe (portable).
if IS_MAC and getattr(sys, "frozen", False):
    DATA_DIR = os.path.join(os.path.expanduser("~/Library/Application Support"), "LiveTranslator")
    os.makedirs(DATA_DIR, exist_ok=True)
else:
    DATA_DIR = APP_DIR
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
# Fonts: Segoe UI / Consolas only exist on Windows
UI_FONT = "Helvetica Neue" if IS_MAC else "Segoe UI"
MONO_FONT = "Menlo" if IS_MAC else "Consolas"
EMOJI_FONT = "Apple Color Emoji" if IS_MAC else "Segoe UI Emoji"


class _FlatButton(tk.Label):
    """Mac only: a Label that behaves like tk.Button. macOS draws tk.Button natively and ignores
    bg/activebackground, so white-on-orange buttons became unreadable and the ON/OFF colors vanished."""
    _SKIP = ("command", "overrelief", "default", "repeatdelay", "repeatinterval")

    def __init__(self, master=None, cnf=None, **kw):
        kw = {**(cnf or {}), **kw}
        self._command = kw.pop("command", None)
        for k in self._SKIP:
            kw.pop(k, None)
        kw.setdefault("cursor", "hand2")
        kw.setdefault("padx", 6)
        kw.setdefault("pady", 2)
        kw.setdefault("activebackground", kw.get("bg", kw.get("background", "#e6e6e6")))
        kw.setdefault("activeforeground", kw.get("fg", kw.get("foreground", "black")))
        super().__init__(master, **kw)
        self.bind("<Enter>", lambda e: self._on() and tk.Label.configure(self, state="active"))
        self.bind("<Leave>", lambda e: self._on() and tk.Label.configure(self, state="normal"))
        self.bind("<ButtonRelease-1>", self._release)

    def _on(self):
        return str(tk.Label.cget(self, "state")) != "disabled"

    def _release(self, e):
        if self._on() and 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height():
            self.invoke()

    def invoke(self):
        if self._command:
            return self._command()

    def configure(self, cnf=None, **kw):
        if cnf is None and not kw:
            return super().configure()
        kw = {**(cnf or {}), **kw}
        if "command" in kw:
            self._command = kw.pop("command")
        for k in self._SKIP:
            kw.pop(k, None)
        if kw:
            return super().configure(**kw)

    config = configure

    def cget(self, key):
        return self._command if key == "command" else super().cget(key)


if IS_MAC:
    tk.Button = _FlatButton


def whisper_model_path():
    """Use the Whisper model bundled inside the .exe / models folder if present (works offline)."""
    for base in (getattr(sys, "_MEIPASS", None), APP_DIR):
        if base:
            p = os.path.join(base, "models", WHISPER_MODEL)
            if os.path.isdir(p):
                return p
    return WHISPER_MODEL

# Server-side refusal fallback (routes to another model instead of failing).
FALLBACK_HEADERS = {"anthropic-beta": "server-side-fallback-2026-07-01"}


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass


# =====================================================================
# Login screen
# =====================================================================
def find_ant():
    """Locate Anthropic's official `ant` CLI (bundled in vendor/ or on PATH)."""
    for base in (getattr(sys, "_MEIPASS", None), APP_DIR):
        if base:
            p = os.path.join(base, "vendor", "ant.exe" if sys.platform == "win32" else "ant")
            if os.path.exists(p):
                return p
    return shutil.which("ant")


def profile_client():
    """Client that uses the signed-in Claude account profile (SDK refreshes the token itself)."""
    env = {k: os.environ.pop(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN") if k in os.environ}
    try:
        return anthropic.Anthropic(max_retries=1, timeout=20.0)
    finally:
        os.environ.update(env)


class LoginWindow:
    def __init__(self, root, on_success):
        self.root = root
        self.on_success = on_success
        root.title(f"Đăng nhập - Live Translator v{APP_VERSION}")
        root.geometry("480x470")
        root.resizable(False, False)

        frm = tk.Frame(root, padx=28, pady=22, bg="#faf9f5")
        frm.pack(fill="both", expand=True)
        bg = "#faf9f5"

        tk.Label(frm, text="Đăng nhập bằng tài khoản Claude", font=(UI_FONT, 15, "bold"), bg=bg).pack(anchor="w")
        tk.Label(frm, text="Trang đăng nhập chính thức của Claude sẽ mở trong trình duyệt.",
                 font=(UI_FONT, 9), fg="#555", bg=bg).pack(anchor="w", pady=(4, 14))

        style = dict(font=(UI_FONT, 11), relief="solid", bd=1, cursor="hand2", anchor="center")
        self.btn_google = tk.Button(frm, text="G   Tiếp tục với Google", bg="white", activebackground="#eee",
                                    command=self.login_account, **style)
        self.btn_google.pack(fill="x", ipady=6)
        self.btn_email = tk.Button(frm, text="✉   Tiếp tục với email", bg="white", activebackground="#eee",
                                   command=self.login_account, **style)
        self.btn_email.pack(fill="x", ipady=6, pady=(8, 0))

        # Shown while the browser login is in progress: paste the code the web page displays.
        self.code_frame = tk.Frame(frm, bg="#fff4e5", padx=10, pady=8, bd=1, relief="solid")
        tk.Label(self.code_frame, text="Sau khi đăng nhập trên web, nếu trang hiển thị một MÃ (code),\n"
                                       "hãy copy và dán mã đó vào đây rồi bấm Xác nhận:",
                 bg="#fff4e5", font=(UI_FONT, 9), justify="left").pack(anchor="w")
        row = tk.Frame(self.code_frame, bg="#fff4e5")
        row.pack(fill="x", pady=(6, 0))
        self.code_var = tk.StringVar()
        self.code_entry = tk.Entry(row, textvariable=self.code_var, font=(MONO_FONT, 11))
        self.code_entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.code_entry.bind("<Return>", lambda e: self.submit_code())
        tk.Button(row, text="Xác nhận", bg="#d97757", fg="white", relief="flat",
                  command=self.submit_code).pack(side="left", padx=(6, 0), ipadx=6)
        links = tk.Frame(self.code_frame, bg="#fff4e5")
        links.pack(fill="x", pady=(6, 0))
        tk.Button(links, text="Mở lại trang đăng nhập", relief="flat", bg="#fff4e5", fg="#1a5fb4",
                  cursor="hand2", command=self.reopen_browser).pack(side="left")
        tk.Button(links, text="Huỷ", relief="flat", bg="#fff4e5", fg="#c0392b",
                  cursor="hand2", command=self.cancel_login).pack(side="right")
        self.proc = None
        self.cancelled = False
        self.attempt = 0
        self.code_try = 0

        self.divider = tk.Label(frm, text="──────────  hoặc dùng API key  ──────────", fg="#999", bg=bg,
                                font=(UI_FONT, 9))
        self.divider.pack(pady=(16, 8))

        self.key_var = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY") or load_config().get("api_key", ""))
        self.entry = tk.Entry(frm, textvariable=self.key_var, show="•", font=(UI_FONT, 11))
        self.entry.pack(fill="x", ipady=4)
        self.entry.bind("<Return>", lambda e: self.login_key())

        self.remember = tk.BooleanVar(value=load_config().get("remember", True))
        tk.Checkbutton(frm, text="Ghi nhớ đăng nhập (lần sau tự vào thẳng, không cần đăng nhập lại)",
                       variable=self.remember, bg=bg, wraplength=400, justify="left").pack(anchor="w", pady=(4, 0))

        self.btn = tk.Button(frm, text="Đăng nhập bằng API key", font=(UI_FONT, 11, "bold"), bg="#d97757",
                             fg="white", activebackground="#c4643f", relief="flat", command=self.login_key)
        self.btn.pack(fill="x", pady=(8, 4), ipady=3)

        self.status = tk.Label(frm, text="", font=(UI_FONT, 9), fg="#c0392b", wraplength=420,
                               justify="left", bg=bg)
        self.status.pack(anchor="w")

    def _busy(self, on, msg=""):
        st = "disabled" if on else "normal"
        for b in (self.btn, self.btn_google, self.btn_email):
            b.config(state=st)
        if msg:
            self.status.config(text=msg, fg="#555")

    # ---- Claude account (Google / email) via official OAuth login ----
    def login_account(self):
        self.attempt += 1          # each login attempt has its own id; older threads stop quietly
        self.cancelled = False
        self._busy(True, "Đang mở trang đăng nhập Claude trong trình duyệt... hãy chọn Google hoặc email.")
        threading.Thread(target=self._account_flow, args=(self.attempt,), daemon=True).start()

    def _stale(self, my_attempt):
        return self.cancelled or my_attempt != self.attempt

    def _show_code_box(self, show):
        if show:
            self.code_var.set("")
            self.code_frame.pack(fill="x", pady=(10, 0), before=self.divider)
            self.root.geometry("480x600")
            self.code_entry.focus_set()
        else:
            self.code_frame.pack_forget()
            self.root.geometry("480x470")

    def submit_code(self):
        code = self.code_var.get().strip()
        if not code or not self.proc or self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.write(code + "\n")
            self.proc.stdin.flush()
            self.status.config(text="Đang xác nhận mã...", fg="#555")
            self.code_try = getattr(self, "code_try", 0) + 1
            self.root.after(12000, self._code_hint, self.code_try)
        except Exception as e:  # noqa: BLE001
            self.status.config(text=f"Không gửi được mã: {e}", fg="#c0392b")

    def _code_hint(self, attempt):
        # ant silently ignores a code from an older login attempt; tell the user what to do.
        # After that ant stops reading codes, so start a fresh login attempt automatically.
        if (attempt == self.code_try and self.proc and self.proc.poll() is None
                and self.status["text"] == "Đang xác nhận mã..."):
            self.restart_login("Mã đó thuộc lần đăng nhập cũ. Đã mở trang đăng nhập MỚI, "
                               "hãy đăng nhập lại và dán mã mới.")

    def restart_login(self, note=None):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.login_account()
        if note:
            self.status.config(text=note, fg="#c0392b")

    def reopen_browser(self):
        self.restart_login("Đã mở trang đăng nhập mới. Hãy dùng mã từ trang mới này.")

    def cancel_login(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self._show_code_box(False)
        self._fail("Đã huỷ đăng nhập.")

    def _account_flow(self, my_attempt):
        # Already signed in earlier? Reuse it without opening the browser.
        try:
            c = profile_client()
            c.models.list(limit=1)
            self.root.after(0, self._ok, c)
            return
        except Exception:
            pass
        ant = find_ant()
        if not ant:
            self.root.after(0, self._fail, "Thiếu công cụ đăng nhập Claude (vendor/ant). "
                                           "Hãy dùng API key bên dưới.")
            return
        try:
            # ant finishes either by itself (browser -> localhost callback) or after we feed it the
            # code shown on the web page through stdin.
            proc = subprocess.Popen(
                [ant, "auth", "login"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.proc = proc
            self.root.after(0, self._show_code_box, True)
            log = []
            for line in proc.stdout:
                if self._stale(my_attempt):
                    break
                log.append(line)
                s = line.strip()
                detail = s.removeprefix("Code:").strip()
                if (s.startswith("Code:") or "✗" in s or "error" in s.lower()) and detail:
                    # ant rejected the pasted code but keeps waiting -> let the user try again
                    if "invalid" in detail.lower():
                        msg = "Mã không hợp lệ hoặc đã hết hạn. Bấm 'Mở lại trang đăng nhập' để lấy mã mới."
                    else:
                        msg = f"Lỗi: {detail[:200]}"
                    self.root.after(0, lambda m=msg: (self.status.config(text=m, fg="#c0392b"),
                                                      self.code_var.set("")))
                elif "Logged in" in s:
                    self.root.after(0, lambda: self.status.config(text="Đăng nhập thành công!", fg="#27ae60"))
            rc = proc.wait()
            if self._stale(my_attempt):
                return
            self.root.after(0, self._show_code_box, False)
            if rc != 0 and "invalid" in "".join(log).lower():
                self.root.after(0, self.restart_login, "Mã không hợp lệ hoặc đã hết hạn. Đã mở trang "
                                                       "đăng nhập mới, hãy dán mã mới.")
                return
            if rc != 0:
                tail = "".join(log).strip().splitlines()
                msg = tail[-1] if tail else "đăng nhập bị huỷ"
                raise RuntimeError(msg)
            self.root.after(0, lambda: self.status.config(text="Đăng nhập thành công, đang kiểm tra...",
                                                          fg="#27ae60"))
            c = profile_client()
            c.models.list(limit=1)
            self.root.after(0, self._ok, c)
        except anthropic.AuthenticationError:
            self.root.after(0, self._fail, "Đăng nhập thất bại: tài khoản không hợp lệ.")
        except anthropic.PermissionDeniedError:
            self.root.after(0, self._fail, "Tài khoản chưa có quyền dùng API (cần tổ chức Console có credit).")
        except Exception as e:  # noqa: BLE001
            if not self._stale(my_attempt):
                self.root.after(0, self._fail, f"Đăng nhập thất bại: {e}")

    # ---- API key ----
    def login_key(self):
        key = self.key_var.get().strip()
        if not key:
            self.status.config(text="Vui lòng nhập API key.", fg="#c0392b")
            return
        self._busy(True, "Đang kiểm tra tài khoản...")
        threading.Thread(target=self._check, args=(key,), daemon=True).start()

    def _check(self, key):
        try:
            client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=20.0)
            client.models.list(limit=1)
            self.root.after(0, self._ok, client, key)
        except anthropic.AuthenticationError:
            self.root.after(0, self._fail, "Đăng nhập thất bại: API key không hợp lệ.")
        except anthropic.PermissionDeniedError:
            self.root.after(0, self._fail, "Đăng nhập thất bại: tài khoản không có quyền truy cập.")
        except anthropic.APIConnectionError:
            self.root.after(0, self._fail, "Không kết nối được tới Claude. Kiểm tra mạng.")
        except Exception as e:  # noqa: BLE001
            self.root.after(0, self._fail, f"Đăng nhập thất bại: {e}")

    def _fail(self, msg):
        self._busy(False)
        self.status.config(text=msg, fg="#c0392b")

    def _ok(self, client, key=None):
        """Login worked (key=None means Claude account login). Save the 'remember me' choice."""
        cfg = load_config()
        cfg["remember"] = bool(self.remember.get())
        cfg["method"] = "key" if key else "account"
        if key and self.remember.get():
            cfg["api_key"] = key
        else:
            cfg.pop("api_key", None)
        save_config(cfg)
        self.on_success(client)

    def try_auto_login(self):
        """Remembered login: validate it in the background and skip this screen if it still works."""
        cfg = load_config()
        if not cfg.get("remember") or not cfg.get("method"):
            return
        self._busy(True, "Đang tự động đăng nhập...")

        def work():
            try:
                if cfg["method"] == "key" and cfg.get("api_key"):
                    c = anthropic.Anthropic(api_key=cfg["api_key"], max_retries=1, timeout=20.0)
                else:
                    c = profile_client()
                c.models.list(limit=1)
                self.root.after(0, self.on_success, c)
            except Exception:  # noqa: BLE001
                self.root.after(0, self._fail, "Phiên đăng nhập đã lưu không còn dùng được. Hãy đăng nhập lại.")
        threading.Thread(target=work, daemon=True).start()


# =====================================================================
# Speech recognition (Whisper, local)
# =====================================================================
_PRELOAD = {"whisper": None, "vosk": None, "spk": None, "error": None, "done": threading.Event()}


def model_dir(name):
    for base in (getattr(sys, "_MEIPASS", None), APP_DIR):
        if base:
            p = os.path.join(base, "models", name)
            if os.path.isdir(p):
                return p
    return None


def preload_models():
    """Load both speech models in the background as soon as the app starts (during login)."""
    try:
        import vosk
        vosk.SetLogLevel(-1)
        _PRELOAD["vosk"] = vosk.Model(model_dir(VOSK_MODEL))
        _PRELOAD["spk"] = load_voiceprint_model()                      # who is speaking
        from faster_whisper import WhisperModel
        m = WhisperModel(whisper_model_path(), device="cpu", compute_type="int8",
                         cpu_threads=max(2, min(6, (os.cpu_count() or 4) - 2)))
        list(m.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), language="en")[0])  # warm-up
        _PRELOAD["whisper"] = m
    except Exception as e:  # noqa: BLE001
        _PRELOAD["error"] = e
    _PRELOAD["done"].set()


def get_models():
    _PRELOAD["done"].wait()
    if _PRELOAD["error"]:
        raise _PRELOAD["error"]
    return _PRELOAD["vosk"], _PRELOAD["whisper"]


SPK_MODEL_FILE = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"   # CAM++ trained on VoxCeleb


def load_voiceprint_model():
    """CAM++ speaker-embedding model via sherpa-onnx (offline). Returns a function audio -> unit vector."""
    try:
        import sherpa_onnx
        path = os.path.join(model_dir("spk-onnx") or "", SPK_MODEL_FILE)
        ex = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=path, num_threads=2))
    except Exception:  # noqa: BLE001
        return None

    def window(x):
        st = ex.create_stream()
        st.accept_waveform(SAMPLE_RATE, np.ascontiguousarray(x, dtype=np.float32))
        st.input_finished()
        e = np.array(ex.compute(st), dtype=np.float32)
        return e / (np.linalg.norm(e) or 1.0)

    def embed(audio):
        # This model gives reliable voiceprints only for exactly-2-second inputs (measured on real
        # voices), so: 2 s windows, 1 s apart, averaged. Short audio is looped up to 2 s.
        W = 2 * SAMPLE_RATE
        x = audio
        if len(x) < W:
            x = np.tile(x, int(np.ceil(W / max(1, len(x)))))[:W]
        vs = [window(x[i:i + W]) for i in range(0, len(x) - W + 1, SAMPLE_RATE)][:8]
        m = np.mean(vs, axis=0)
        return m / (np.linalg.norm(m) or 1.0)
    return embed


class SpeakerTracker:
    """Groups utterances by voice (CAM++ voiceprints), online. Any number of speakers.

    Each utterance's voiceprint is compared with the average voiceprint of every speaker heard so
    far; close enough -> same person, otherwise a new speaker. Short utterances give unreliable
    voiceprints, so they never create a new speaker. When two groups later turn out to have the
    same voice (one person was split in two early on), they are merged: `merged` maps the old id
    to the id it joined, so the window can give it the same name and color.
    """
    SAME = 0.36          # similarity to a speaker's average voice to count as that speaker
    MERGE = 0.50         # two speakers' average voices this similar -> the same person
    # (tuned on real voices with classroom echo/noise: ~97% right with 3 people, ~91-94% with 5-8)
    MIN_SECONDS = 1.2    # shorter utterances are too short to start a new speaker
    SMALL = 8.0          # a "speaker" with less talk than this (seconds) is still uncertain
    MERGE_SMALL = 0.50   # (tried a lower bar for uncertain speakers: it merged real students away)

    def __init__(self):
        self.sums = {}      # speaker id -> weighted sum of voiceprints
        self.talk = {}      # speaker id -> seconds spoken
        self.merged = {}    # old speaker id -> speaker id it was merged into
        self.last = None
        self.next_id = 1

    def resolve(self, sid):
        while sid in self.merged:
            sid = self.merged[sid]
        return sid

    @staticmethod
    def _unit(v):
        return v / (np.linalg.norm(v) or 1.0)

    def assign(self, vec, seconds):
        sid = self.resolve(self.last) if self.last else None
        if vec is not None and len(vec):
            v = np.asarray(vec, dtype=np.float32)
            best, best_sim = None, -1.0
            for s, total in self.sums.items():
                sim = float(v @ self._unit(total))
                if sim > best_sim:
                    best, best_sim = s, sim
            if best is not None and (best_sim >= self.SAME or seconds < self.MIN_SECONDS):
                sid = best
            elif best is None or seconds >= self.MIN_SECONDS:
                sid = self.next_id
                self.next_id += 1
                self.sums[sid] = np.zeros_like(v)
            self.sums[sid] = self.sums[sid] + v * min(seconds, 6.0)   # longer speech counts more
            self._merge_similar()
            sid = self.resolve(sid)
        if sid is None:                         # no voiceprint at all
            sid = self.next_id
            self.next_id += 1
            self.sums[sid] = np.zeros(512, dtype=np.float32)
        self.talk[sid] = self.talk.get(sid, 0.0) + seconds
        self.last = sid
        return sid

    def _merge_similar(self):
        while True:
            ids = sorted(self.sums)
            pair, best = None, -1.0
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    sim = float(self._unit(self.sums[a]) @ self._unit(self.sums[b]))
                    small = min(self.talk.get(a, 0), self.talk.get(b, 0)) < self.SMALL
                    need = self.MERGE_SMALL if small else self.MERGE
                    if sim >= need and (pair is None or sim > best):
                        pair, best = (a, b), sim
            if not pair:
                return
            a, b = pair
            keep, gone = (a, b) if self.talk.get(a, 0) >= self.talk.get(b, 0) else (b, a)
            self.sums[keep] = self.sums[keep] + self.sums.pop(gone)
            self.talk[keep] = self.talk.get(keep, 0) + self.talk.pop(gone, 0)
            self.merged[gone] = keep


class Recognizer(threading.Thread):
    """Live captions in two layers.

    1. Vosk (streaming) turns mic audio into words immediately -> grey "live" text, updated
       several times a second while the person is speaking.
    2. When Vosk detects the end of an utterance (a short pause), Whisper re-transcribes that
       utterance's audio for accurate, punctuated text, which replaces the grey draft and is
       what gets translated. If Whisper falls behind, waiting utterances are merged into one
       call (a Whisper call costs about the same for 3 s or 20 s of audio).
    """

    def __init__(self, ui_q, on_final):
        super().__init__(daemon=True)
        self.ui_q = ui_q
        self.on_final = on_final            # called with (utterance ids, final text)
        self.audio_q = queue.Queue()
        self.whisper_q = queue.Queue()
        self.running = threading.Event()
        self.vosk_model = None
        self.whisper = None
        self.context = ""
        self.utt_id = 0
        self.spk_model = None
        self.speakers = SpeakerTracker()
        self.utt_spk = {}                   # utterance id -> speaker id

    def load_model(self):
        if self.whisper is None:
            if not _PRELOAD["done"].is_set():
                self.ui_q.put(("status", "Đang chuẩn bị nhận diện giọng nói (vài giây)..."))
            self.vosk_model, self.whisper = get_models()
            self.spk_model = _PRELOAD["spk"]
        self.ui_q.put(("ready", None))

    def _mic_cb(self, indata, frames, t, status):
        self.audio_q.put(indata[:, 0].copy())

    def transcribe(self, audio):
        peak = float(np.max(np.abs(audio))) or 1.0
        audio = audio * min(8.0, 0.6 / peak)          # auto-gain: quiet mics are boosted (max 8x)
        segs, _ = self.whisper.transcribe(
            audio, language="en", beam_size=1, vad_filter=False,
            condition_on_previous_text=False, initial_prompt=self.context[-200:] or None,
            without_timestamps=True,
        )
        out = []
        for s in segs:
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            out.append(s.text.strip())
        return " ".join(out).strip()

    def run(self):
        import sounddevice as sd
        try:
            self.load_model()
        except Exception as e:  # noqa: BLE001
            self.ui_q.put(("error", f"Không tải được mô hình nhận diện giọng nói: {e}"))
            return
        threading.Thread(target=self._whisper_worker, daemon=True).start()
        while True:
            self.running.wait()
            try:
                self._listen(sd)
            except Exception as e:  # noqa: BLE001
                self.ui_q.put(("error", f"Lỗi micro: {e}"))
                self.running.clear()
                self.ui_q.put(("force_off", None))

    def _listen(self, sd):
        import vosk
        self.ui_q.put(("status", "Đang nghe... (nói tiếng Anh)"))
        while not self.audio_q.empty():
            self.audio_q.get_nowait()
        rec = vosk.KaldiRecognizer(self.vosk_model, SAMPLE_RATE)
        utt = []                       # audio of the current utterance
        utt_len = 0
        last_partial = ""
        rms_hist = []                  # recent 30 ms loudness values -> background-noise floor
        quiet = 0.0                    # how long it has been quiet (s)
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=FRAME * 5, callback=self._mic_cb):
            while self.running.is_set():
                try:
                    blocks = [self.audio_q.get(timeout=0.1)]
                except queue.Empty:
                    continue
                while not self.audio_q.empty():          # take everything waiting (catch up)
                    blocks.append(self.audio_q.get_nowait())
                block = np.concatenate(blocks)
                utt.append(block)
                utt_len += len(block)

                # breath pauses: Vosk only ends an utterance after a long silence, so we also cut
                # at short pauses once the utterance is long enough -> accurate text comes sooner
                n = len(block) // FRAME
                rms = np.sqrt((block[: n * FRAME].reshape(n, FRAME) ** 2).mean(axis=1)) if n else np.zeros(0)
                rms_hist.extend(rms.tolist())
                del rms_hist[:-400]
                floor = float(np.percentile(rms_hist, 20)) if len(rms_hist) > 30 else 0.004
                for v in rms:
                    quiet = quiet + FRAME / SAMPLE_RATE if v < floor * 2.0 + 0.002 else 0.0
                secs = utt_len / SAMPLE_RATE

                pcm16 = (np.clip(block, -1, 1) * 32767).astype(np.int16).tobytes()
                if rec.AcceptWaveform(pcm16):
                    self._end_utterance(json.loads(rec.Result()), utt)
                    utt, utt_len, last_partial = [], 0, ""
                elif (secs >= UTT_SOFT and quiet >= 0.2) or secs >= UTT_HARD:
                    self._end_utterance(json.loads(rec.FinalResult()), utt)
                    utt, utt_len, last_partial = [], 0, ""
                else:
                    partial = json.loads(rec.PartialResult()).get("partial", "")
                    if partial != last_partial:
                        last_partial = partial
                        self.ui_q.put(("partial", partial))
            self._end_utterance(json.loads(rec.FinalResult()), utt)
        self.whisper_q.put(None)                  # flush marker: stop -> translate the rest
        self.ui_q.put(("status", "Đã tắt."))

    def _end_utterance(self, result, utt):
        draft = result.get("text", "").strip()
        if draft in ("", "the", "huh", "uh", "um"):    # Vosk heard no real words: skip Whisper
            self.ui_q.put(("partial", ""))
            return
        audio = np.concatenate(utt)
        self.utt_id += 1
        vec = None
        if self.spk_model is not None and len(audio) > SAMPLE_RATE // 2:
            try:
                vec = self.spk_model(audio)
            except Exception:  # noqa: BLE001
                vec = None
        sid = self.speakers.assign(vec, len(audio) / SAMPLE_RATE)
        self.utt_spk[self.utt_id] = sid
        self.ui_q.put(("speaker", (self.utt_id, sid, dict(self.speakers.talk), dict(self.speakers.merged))))
        self.ui_q.put(("draft", (self.utt_id, draft)))
        self.whisper_q.put((self.utt_id, draft, audio, sid))

    def _whisper_worker(self):
        while True:
            item = self.whisper_q.get()
            if item is None:
                self.ui_q.put(("flush", None))
                continue
            items = [item]          # one segment per call, so English and Vietnamese rows stay 1:1
            ids = [x[0] for x in items]
            audio = np.concatenate([x[2] for x in items])
            try:
                text = self.transcribe(audio)
            except Exception:  # noqa: BLE001
                text = ""
            draft = " ".join(x[1] for x in items)
            n_w, n_d = len(text.split()), len(draft.split())
            # empty, or far longer than what Vosk heard (Whisper sometimes invents text on cut-off
            # audio) -> use the live draft instead
            if not text.strip(" .,!?-'\"") or n_w > 2 * n_d + 3:
                text = draft[:1].upper() + draft[1:]
            self.context = (self.context + " " + text)[-400:]
            self.on_final(ids, text)


# =====================================================================
# Claude workers: translation + key takeaways
# =====================================================================
def claude_stream(client, system, prompt, effort, on_piece=None, model=None, thinking=None, max_tokens=4000):
    """Stream a Claude reply; returns the full text. Retries once without the fallback option if rejected.

    `prompt` is either the user's text or a full list of messages (for multi-turn chat).
    """
    messages = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
    for use_fallback in (True, False):
        extra = {"output_config": {"effort": effort}}
        if thinking:
            extra["thinking"] = thinking
        headers = None
        if use_fallback and not (model or MODEL).startswith("claude-haiku"):   # Haiku has no fallback
            extra["fallbacks"] = "default"
            headers = FALLBACK_HEADERS
        out = ""
        try:
            with client.messages.stream(model=model or MODEL, max_tokens=max_tokens, system=system,
                                        messages=messages,
                                        extra_body=extra, extra_headers=headers) as stream:
                for piece in stream.text_stream:
                    out += piece
                    if on_piece:
                        on_piece(piece)
            return out
        except anthropic.BadRequestError as e:
            # only the optional fallback setting is worth retrying without
            if not use_fallback or out or "fallback" not in str(e).lower():
                raise
    return ""


SENTENCE_GAP = re.compile(r"[.!?…](\s+)(?=\S)")        # the space after a sentence end
ABBREV = {"mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "no", "fig", "ông", "bà", "tp"}


def sentence_gaps(text):
    """Spaces that separate two sentences (next word capitalized, not after an abbreviation)."""
    for m in SENTENCE_GAP.finditer(text):
        nxt = text[m.end(1):m.end(1) + 1]
        word = re.findall(r"[\w.]+$", text[:m.start(1) - 1])
        if (nxt.isupper() or nxt.isdigit() or nxt in "\"'“(") and not (word and word[0].lower() in ABBREV):
            yield m


def split_sentences(text):
    """One sentence per line."""
    out, last = [], 0
    for m in sentence_gaps(text):
        out += [text[last:m.start(1)], "\n"]
        last = m.end(1)
    return "".join(out + [text[last:]])


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
TEXT_TYPES = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".log", ".srt", ".vtt", ".yaml",
              ".yml", ".py", ".js", ".ts", ".java", ".cs", ".cpp", ".c", ".h", ".go", ".rb", ".php", ".sql", ".ini"}
ATTACH_TYPES = sorted(set(IMAGE_TYPES) | TEXT_TYPES | {".pdf", ".docx"})


def file_to_block(path):
    """Read a file into a Claude content block. Raises ValueError with a Vietnamese reason if unsupported."""
    import base64
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    name = os.path.basename(path)
    if ext in IMAGE_TYPES:
        if size > 5 * 2**20:
            raise ValueError("ảnh lớn hơn 5 MB")
        data = base64.standard_b64encode(open(path, "rb").read()).decode()
        return {"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[ext], "data": data}}
    if ext == ".pdf":
        if size > 20 * 2**20:
            raise ValueError("PDF lớn hơn 20 MB")
        data = base64.standard_b64encode(open(path, "rb").read()).decode()
        return {"type": "document", "title": name,
                "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    if ext == ".docx":
        text = docx_text(path)
    elif ext in TEXT_TYPES:
        raw = open(path, "rb").read()
        for enc in ("utf-8-sig", "utf-16", "cp1258", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    else:
        raise ValueError("loại file chưa hỗ trợ (dùng ảnh, PDF, Word .docx hoặc file chữ)")
    if len(text) > 400_000:
        raise ValueError("file chữ quá dài (hơn 400.000 ký tự)")
    return {"type": "text", "text": f'<file name="{name}">\n{text}\n</file>'}


def docx_text(path):
    """Plain text of a Word .docx (paragraphs and table cells), without extra libraries."""
    import zipfile
    from xml.etree import ElementTree
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        xml = zipfile.ZipFile(path).read("word/document.xml")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"không đọc được file Word ({e})")
    paras = []
    for p in ElementTree.fromstring(xml).iter(ns + "p"):
        line = "".join(t.text or "" for t in p.iter(ns + "t"))
        paras.append(line)
    return "\n".join(paras).strip()


def friendly_error(e):
    """Turn Claude API errors into a short Vietnamese explanation."""
    s = str(e).lower()
    if "credit balance" in s:
        return ("Tài khoản API hết credit ($0). Gói Ultra có $200/tháng: vào claude.ai → Settings → Billing → "
                "API credits → Link organization, rồi dùng API key/đăng nhập của đúng tổ chức đó.")
    if isinstance(e, anthropic.AuthenticationError):
        return "Phiên đăng nhập/API key không còn hợp lệ. Hãy đăng nhập lại."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "Tài khoản không có quyền dùng model này."
    if isinstance(e, anthropic.RateLimitError):
        return "Đang gửi quá nhiều yêu cầu (rate limit). Ứng dụng sẽ tự thử lại."
    if isinstance(e, anthropic.APIConnectionError):
        return "Mất kết nối tới Claude. Kiểm tra mạng."
    return f"Lỗi Claude: {str(e)[:160]}"


# Translation speed presets (shown in the top bar). Thinking is switched off for speed where the model allows.
TRANSLATE_PRESETS = {
    "Nhanh (Sonnet 5.5)": ("claude-sonnet-5-5", {"type": "between_tools"}),
    "Chất lượng cao (Opus 5.5)": ("claude-opus-5-5", None),
}
DEFAULT_PRESET = "Nhanh (Sonnet 5.5)"


class Translator:
    """Translates each spoken segment as soon as the speaker pauses.

    * The fast Vosk draft of a segment is translated immediately (no waiting for Whisper or for the
      end of a sentence). When Whisper's accurate text arrives, the segment is re-translated only if
      the words differ noticeably.
    * Several segments can be translated at the same time; each has its own slot in the Vietnamese
      pane, so the text always appears in speaking order.
    """
    SYSTEM = ("You are a live interpreter. Translate the NEW English speech segment into natural, fluent "
              "Vietnamese. It comes from automatic speech recognition: it may lack punctuation, contain "
              "recognition errors, or be part of a longer sentence that continues in the next segment. Silently "
              "fix obvious recognition errors and translate it so it reads naturally after the earlier part. "
              "Never repeat words that the earlier Vietnamese already covers. "
              "Output ONLY the Vietnamese translation of the NEW segment: no notes, no quotes, no English.")

    def __init__(self, client, ui_q, preset=DEFAULT_PRESET):
        from concurrent.futures import ThreadPoolExecutor
        self.client = client
        self.ui_q = ui_q
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.lock = threading.Lock()
        self.en = {}        # uid -> English text currently translated for that slot
        self.vi = {}        # uid -> finished Vietnamese text
        self.gen = {}       # uid -> generation (a re-translation makes older output stale)
        self.set_preset(preset)

    def set_preset(self, name):
        self.preset = name if name in TRANSLATE_PRESETS else DEFAULT_PRESET
        self.model, self.thinking = TRANSLATE_PRESETS[self.preset]

    @property
    def history(self):
        """[(english, vietnamese)] in speaking order (used as context for chat)."""
        with self.lock:
            return [(self.en[u], self.vi[u]) for u in sorted(self.vi) if u in self.en]

    # called on the UI thread
    def draft(self, uid, text):
        with self.lock:
            self.en[uid] = text
            self.gen[uid] = 1
        self.ui_q.put(("vi_slot", uid))
        self.pool.submit(self._translate, uid, 1, text)

    def final(self, ids, text):
        """Whisper's accurate text for one or more segments (merged when Whisper was busy)."""
        import difflib
        with self.lock:
            drafts = " ".join(self.en.get(u, "") for u in ids)
            norm = lambda s: re.sub(r"[^a-z0-9' ]", " ", s.lower()).split()
            same = difflib.SequenceMatcher(None, norm(drafts), norm(text)).ratio() >= 0.85
            if same:                                   # draft translation is good: keep it
                for u in ids:
                    self.en[u] = text if u == ids[0] else ""
                return
            first = ids[0]
            for u in ids:
                self.gen[u] = self.gen.get(u, 0) + 1
            for u in ids[1:]:                          # merged segments: translate into the first slot
                self.en.pop(u, None)
                self.vi.pop(u, None)
                self.ui_q.put(("vi_drop", u))
            self.en[first] = text
            self.vi.pop(first, None)
            g = self.gen[first]
        self.ui_q.put(("vi_reset", (first, g)))
        self.pool.submit(self._translate, first, g, text)

    def _context(self, uid):
        """Earlier English (last few segments) and the Vietnamese of the segment just before."""
        with self.lock:
            prev = [u for u in sorted(self.en) if u < uid and self.en[u]]
            earlier = " ".join(self.en[u] for u in prev[-4:])
            last_vi = self.vi.get(prev[-1], "") if prev else ""
        return earlier, last_vi

    def _translate(self, uid, g, text):
        ctx, last_vi = self._context(uid)
        prompt = (f"Earlier speech (context only, already translated):\n{ctx}\n\n" if ctx else "")
        if last_vi:
            prompt += f"Its Vietnamese translation ended with:\n...{last_vi[-200:]}\n\n"
        prompt += f"NEW segment to translate:\n{text}"
        out = ""
        try:
            out = claude_stream(self.client, self.SYSTEM, prompt, "low",
                                lambda p: self.ui_q.put(("vi_piece", (uid, g, p))),
                                model=self.model, thinking=self.thinking, max_tokens=1500)
        except Exception as e:  # noqa: BLE001
            self.ui_q.put(("vi_piece", (uid, g, "[chưa dịch được đoạn này]")))
            self.ui_q.put(("error", friendly_error(e)))
        with self.lock:
            if self.gen.get(uid) == g:
                self.vi[uid] = out.strip()


class Chatter(threading.Thread):
    """Answers the user's questions about the session (multi-turn), streaming into the chat pane."""
    SYSTEM = ("You are Claude, helping a Vietnamese user who is listening to a live English talk or "
              "conversation. The app gives you the speech-recognition transcript so far (it may contain "
              "recognition errors), its Vietnamese translation, and the current key takeaways. Answer the "
              "user's questions about it, or any other question they ask. Reply in Vietnamese unless the user "
              "writes in another language or asks otherwise. Be clear and concise; plain text, short bullet "
              "points when helpful (no Markdown headings or tables). If the transcript doesn't contain the "
              "answer, say so and answer from general knowledge if appropriate. The user may also have a "
              "study library (course documents uploaded from the Learning menu): its catalogue is given in "
              "<learning_library>, and documents from it may be attached to a question. Use them when relevant; "
              "when tutoring an exercise, guide one step at a time and wait for the user.")

    def __init__(self, client, ui_q, get_context):
        super().__init__(daemon=True)
        self.client = client
        self.ui_q = ui_q
        self.get_context = get_context
        self.q = queue.Queue()
        self.history = []          # past turns: [{"role": ..., "content": ...}, ...]
        self.get_library = None    # -> catalogue text of the Learning library ("" if empty)

    @staticmethod
    def _with_cache(messages):
        """Mark the newest attached file as a cache point, so follow-up questions about the same
        documents (e.g. step-by-step exercise help) don't pay for them again."""
        out = list(messages)
        for i in range(len(out) - 1, -1, -1):
            m = out[i]
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            idx = [j for j, b in enumerate(m["content"])
                   if b.get("type") != "text" or b.get("text", "").startswith("<file ")]
            if idx:
                content = list(m["content"])
                content[idx[-1]] = {**content[idx[-1]], "cache_control": {"type": "ephemeral"}}
                out[i] = {**m, "content": content}
                break
        return out

    def run(self):
        while True:
            question, files = self.q.get()        # files: content blocks (images, PDFs, text)
            en, vi, summary = self.get_context()
            context = (f"<transcript_english>\n{en or '(chưa có)'}\n</transcript_english>\n"
                       f"<translation_vietnamese>\n{vi or '(chưa có)'}\n</translation_vietnamese>\n"
                       f"<key_takeaways>\n{summary or '(chưa có)'}\n</key_takeaways>")
            library = self.get_library() if self.get_library else ""
            if library:
                context += f"\n<learning_library>\n{library}\n</learning_library>"
            # The latest session context rides on the newest question, so earlier turns stay unchanged.
            # Files go before the text, and stay in the history so follow-up questions can refer to them.
            messages = self.history[-20:] + [
                {"role": "user", "content": files + [
                    {"type": "text", "text": f"{context}\n\nCâu hỏi của tôi: {question}"}]}]
            answer = ""
            try:
                answer = claude_stream(self.client, self.SYSTEM, self._with_cache(messages), "medium",
                                       lambda p: self.ui_q.put(("chat_piece", p)), max_tokens=16000)
            except Exception as e:  # noqa: BLE001
                self.ui_q.put(("chat_error", f"[{friendly_error(e)}]"))
            if answer.strip():
                self.history += [{"role": "user", "content": files + [{"type": "text", "text": question}]},
                                 {"role": "assistant", "content": answer.strip()}]
            self.ui_q.put(("chat_end", None))


class Summarizer(threading.Thread):
    """Running key takeaways. Every round only the speech that is new since the last round is turned into
    new bullet points, which are ADDED to the pane (earlier takeaways are never removed)."""
    SYSTEM = ("You keep running KEY TAKEAWAYS in Vietnamese for a live talk/conversation. You get the takeaways "
              "already written, the end of the earlier transcript for context, and the NEW part of the English "
              "transcript (from speech recognition; it may contain errors). Write only takeaways that the NEW "
              "part adds and that are not already in the existing takeaways: 1-5 short bullet points starting "
              "with '• ', most important first. If the new part adds nothing worth noting, output exactly NONE. "
              "Output only the bullets (or NONE).")

    def __init__(self, client, ui_q, get_transcript, get_takeaways):
        super().__init__(daemon=True)
        # short timeout: a stuck request must not stop the takeaways for long
        self.client = client.with_options(timeout=120.0, max_retries=1)
        self.ui_q = ui_q
        self.get_transcript = get_transcript      # (start, end) -> transcript text of those entries
        self.get_takeaways = get_takeaways
        self.kick = threading.Event()
        self.done_n = 0                           # transcript entries already covered
        self.final = False                        # set when listening stops: cover even a short remainder

    def run(self):
        while True:
            self.kick.wait(timeout=SUMMARY_EVERY)
            self.kick.clear()
            final, self.final = self.final, False
            try:
                n, new = self.get_transcript(self.done_n, None)
                if len(new.split()) < (4 if final else 15):
                    continue
                _, before = self.get_transcript(0, self.done_n)
                prompt = (f"<existing_takeaways>\n{self.get_takeaways() or '(none yet)'}\n</existing_takeaways>\n"
                          f"<earlier_transcript_end>\n{before[-3000:] or '(none)'}\n</earlier_transcript_end>\n"
                          f"<new_transcript>\n{new}\n</new_transcript>")
                out = claude_stream(self.client, self.SYSTEM, prompt, "medium").strip()
                self.done_n = n
                if out and out.upper().strip(" .") != "NONE":
                    self.ui_q.put(("summary", out))
            except Exception as e:  # noqa: BLE001  (done_n unchanged -> try again next round)
                self.ui_q.put(("error", friendly_error(e)))


# =====================================================================
# Main window
# =====================================================================
class MainWindow:
    def __init__(self, root, client):
        self.root = root
        self.client = client
        self.ui_q = queue.Queue()
        self._syncing = False
        self._sync_dst, self._sync_quiet = None, 0.0
        self.follow = True          # auto-scroll to the newest text (off while the user reads above)
        self.transcript = []
        self.on = False
        self.ready = False
        self.vi_gen = {}            # Vietnamese slot uid -> current translation generation
        self.vi_filled = {}         # slot uid -> has real text (placeholder replaced)
        self.uid_spk = {}           # utterance id -> speaker id
        self.spk_talk = {}          # speaker id -> seconds spoken
        self.spk_custom = {}        # speaker id -> name typed by the user
        self.teachers = []          # speaker ids that passed TEACHER_MINUTES, in the order they did
        self.spk_alias = {}         # speaker id -> id it was merged into (same voice, found later)
        self.spk_shown = {}         # speaker id -> label text currently on screen
        self.last_spk = {"en": None, "vi": None}

        root.title(f"Live Translator v{APP_VERSION} — EN → VI")
        root.resizable(True, True)
        # 1280x800, but never bigger than the screen (small laptops, Mac "larger text", 150% scaling);
        # leave room for the Mac menu bar / Dock or the Windows taskbar
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1280, sw - 20), min(800, sh - (110 if IS_MAC else 80))
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 30)}")
        root.minsize(700, 450)

        text_font = tkfont.Font(family=UI_FONT, size=13)
        head_font = (UI_FONT, 10, "bold")

        # top bar
        top = tk.Frame(root, bg="#2b2b2b", height=40)
        top.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.status = tk.Label(top, text="Nhấn ON để bắt đầu.", bg="#2b2b2b", fg="#ddd", font=(UI_FONT, 10))
        self.status.pack(side="left", padx=12, pady=6)
        self.toggle = tk.Button(top, text="OFF", width=7, font=(UI_FONT, 10, "bold"), relief="flat",
                                bg="#777", fg="white", activebackground="#555", command=self.toggle_onoff)
        self.toggle.pack(side="right", padx=10, pady=5)
        tk.Button(top, text="Đăng xuất", font=(UI_FONT, 9), relief="flat", bg="#2b2b2b", fg="#bbb",
                  activebackground="#444", cursor="hand2", command=self.logout).pack(side="right", pady=5)
        # translation speed / quality choice (remembered)
        self.preset_var = tk.StringVar(value=load_config().get("translate_preset", DEFAULT_PRESET))
        if self.preset_var.get() not in TRANSLATE_PRESETS:
            self.preset_var.set(DEFAULT_PRESET)
        om = tk.OptionMenu(top, self.preset_var, *TRANSLATE_PRESETS, command=self._set_preset)
        om.config(font=(UI_FONT, 9), bg="#3a3a3a", fg="#ddd", activebackground="#4a4a4a",
                  activeforeground="white", relief="flat", highlightthickness=0, bd=0)
        om["menu"].config(font=(UI_FONT, 9))
        if IS_MAC:
            om.config(fg="black")
        om.pack(side="right", padx=(0, 10), pady=5)
        tk.Label(top, text="Dịch:", bg="#2b2b2b", fg="#aaa", font=(UI_FONT, 9)).pack(side="right")
        bstyle = dict(font=(UI_FONT, 9), relief="flat", bg="#3a3a3a", fg="#ddd", activebackground="#4a4a4a",
                      activeforeground="white", cursor="hand2", padx=8)
        tk.Button(top, text="📂 Restore", command=self.restore_session, **bstyle).pack(side="right", padx=(0, 12), pady=5)
        tk.Button(top, text="💾 Backup", command=self.backup_session, **bstyle).pack(side="right", padx=(0, 4), pady=5)

        # Layout (translation mode):
        #   [strip] | English            | Tiếng Việt          | [strip]
        #           |   (each English segment is on the same row as its translation)
        #           | Key takeaways — full width, 25% of the height    |
        # The Claude panel (answers + prompt) slides over the left or right half (full height); the
        # buttons on the two edges choose the side. Everything underneath keeps updating.
        strip_l = tk.Frame(root, bg="#2b2b2b", width=30)
        strip_l.grid(row=1, column=0, sticky="ns")
        strip_l.grid_propagate(False)
        self.body = tk.PanedWindow(root, orient="vertical", sashwidth=6, sashrelief="flat", bd=0, bg="#d6d6d6")
        self.body.grid(row=1, column=1, columnspan=2, sticky="nsew", padx=3, pady=3)
        strip = tk.Frame(root, bg="#2b2b2b", width=30)
        strip.grid(row=1, column=3, sticky="ns")
        strip.grid_propagate(False)

        def pane(parent, title, height=200, scroll=True):
            f = tk.Frame(parent, bd=1, relief="solid", bg="white")
            tk.Label(f, text=title, font=head_font, bg="#f0f0f0", anchor="w", padx=8).pack(fill="x")
            t = tk.Text(f, wrap="word", font=text_font, bd=0, padx=10, pady=8, state="disabled")
            if scroll:
                sb = tk.Scrollbar(f, command=t.yview)
                t.configure(yscrollcommand=sb.set)
                sb.pack(side="right", fill="y")
            t.pack(fill="both", expand=True)
            if isinstance(parent, tk.PanedWindow):
                parent.add(f, minsize=60, height=height)
            elif parent is not None and getattr(parent, "_grid_col", None) is not None:
                f.grid(row=0, column=parent._grid_col, sticky="nsew", padx=(0, 3) if parent._grid_col == 0 else (3, 0))
            else:
                f.pack(fill="both", expand=True)
            return t

        # top: English | Vietnamese, same size, one shared scrollbar (they scroll together)
        top_area = tk.Frame(self.body, bg="#d6d6d6")
        self.body.add(top_area, minsize=120)
        top_area.columnconfigure(0, weight=1, uniform="tv")
        top_area.columnconfigure(1, weight=1, uniform="tv")
        top_area.rowconfigure(0, weight=1)
        top_area._grid_col = 0
        self.left = pane(top_area, "English (nhận diện giọng nói)", scroll=False)
        top_area._grid_col = 1
        self.right = pane(top_area, "Tiếng Việt (bản dịch)", scroll=False)
        self.vsb = tk.Scrollbar(self.right.master, command=self._scroll_both)
        self.vsb.pack(side="right", fill="y", before=self.right)
        self.left.configure(yscrollcommand=lambda a, b: self._on_text_scroll(self.left, a, b))
        self.right.configure(yscrollcommand=lambda a, b: self._on_text_scroll(self.right, a, b))
        self.bottom = pane(self.body, "Ý chính / Key takeaways")
        # export the whole takeaways history as a PDF (button on the right of the pane's title bar)
        tk.Button(self.bottom.master, text="📄 Xuất PDF", font=(UI_FONT, 8), relief="flat", bd=0, padx=6,
                  bg="#e4e4e4", activebackground="#d0d0d0", cursor="hand2",
                  command=self.export_takeaways).place(relx=1.0, x=-4, y=2, anchor="ne")

        # Claude panel: covers the left or right half when open (everything underneath keeps updating)
        self.claude_panel = tk.PanedWindow(root, orient="vertical", sashwidth=6, sashrelief="flat",
                                           bd=0, bg="#d6d6d6")
        self.chat = pane(self.claude_panel, "Claude trả lời")
        self.left.tag_configure("partial", foreground="#9a9a9a")                  # words being spoken now
        self.left.tag_configure("draft", foreground="#7a7a7a", font=(text_font.actual("family"), 13, "italic"))
        self.chat.tag_configure("you", foreground="#1a5fb4", font=(text_font.actual("family"), 12, "bold"))
        self.chat.tag_configure("who", foreground="#d97757", font=(text_font.actual("family"), 12, "bold"))
        self.chat.tag_configure("err", foreground="#c0392b")
        self.right.tag_configure("vipend", foreground="#b0b0b0")          # "…" while translating
        # keep the takeaways scrolled to the newest lines when the pane changes size
        self.take_follow = True        # takeaways pane shows the newest points unless the user scrolls up
        self.bottom.bind("<Configure>", lambda e: self.take_follow and self.bottom.see("end"))
        self.bottom.bind("<MouseWheel>", lambda e: self.root.after_idle(self._take_scrolled))
        for wdg in self.bottom.master.winfo_children():
            if isinstance(wdg, tk.Scrollbar):
                wdg.config(command=lambda *a: (self.bottom.yview(*a), self._take_scrolled()))
        self.bottom.tag_configure("ttime", foreground="#999", font=(UI_FONT, 9))
        for w in (self.left, self.right):
            w.tag_configure("lbl", font=(text_font.actual("family"), 12, "bold"))
            w.tag_bind("lbl", "<Double-Button-1>", self._rename_speaker)
            w.tag_bind("lbl", "<Enter>", lambda e: e.widget.config(cursor="hand2"))
            w.tag_bind("lbl", "<Leave>", lambda e: e.widget.config(cursor=""))
            w.bind("<Configure>", lambda e: self._schedule_align_all())   # re-wrap -> re-align rows
            w.bind("<MouseWheel>", self._wheel)
        self.chat.tag_configure("file", foreground="#8a5a44", font=(text_font.actual("family"), 11, "italic"))

        # prompt box (like Claude's chat input): Enter = send, Shift+Enter = new line
        pf = tk.Frame(self.claude_panel, bd=1, relief="solid", bg="white")
        tk.Label(pf, text="Hỏi Claude  (Enter: gửi · Shift+Enter: xuống dòng)",
                 font=head_font, bg="#f0f0f0", anchor="w", padx=8).pack(fill="x")
        # attached files appear as small chips above the input; ✕ removes one
        self.attach_bar = tk.Frame(pf, bg="white")
        self.attachments = []          # [(display name, content block for Claude)]
        row = self.prompt_row = tk.Frame(pf, bg="white")
        row.pack(fill="both", expand=True)
        self.send_btn = tk.Button(row, text="Gửi ➤", font=(UI_FONT, 10, "bold"), bg="#d97757", fg="white",
                                  activebackground="#c4643f", relief="flat", cursor="hand2", command=self.send_prompt)
        self.send_btn.pack(side="right", fill="y", padx=6, pady=6)
        self.attach_btn = tk.Button(row, text="📎", font=(EMOJI_FONT, 13), bg="white", relief="flat",
                                    activebackground="#eee", cursor="hand2", command=self.attach_files)
        self.attach_btn.pack(side="right", fill="y", pady=6)
        # 🎓 Learning menu (study library), just left of the 📎 button
        self.learn_btn = tk.Button(row, text="🎓 Learning", font=(UI_FONT, 9, "bold"), bg="#f3e3dc", fg="#8a3f22",
                                   activebackground="#ecd2c6", relief="flat", cursor="hand2", padx=6)
        self.learn_btn.pack(side="right", fill="y", pady=10, padx=(4, 2))
        # Native Windows text box: works with every Vietnamese input method (Tk's Text does not)
        host = tk.Frame(row, bg="white", height=60)
        host.pack(side="left", fill="both", expand=True)
        try:
            from native_edit import NativeEdit
            self.prompt = NativeEdit(host, self.send_prompt)
        except Exception:  # noqa: BLE001  (not Windows, or the control could not be created)
            host.destroy()
            self.prompt = tk.Text(row, wrap="word", font=(UI_FONT, 12), bd=0, padx=10, pady=6, height=3)
            self.prompt.pack(side="left", fill="both", expand=True)
            self.prompt.bind("<Return>", self._prompt_enter)
        self.claude_panel.add(pf, minsize=70)
        self.chat_busy = False
        self.last_summary = ""
        self.chat_log = []             # [{"q": question, "files": [names], "a": answer}] for backup

        # open/close buttons on both edges, vertically centered (Ctrl+K toggles the right one)
        self.panel_side = None         # None (closed), "left" or "right"
        self.unread = False
        self.body_ratio = 0.75         # EN/VI share of the height (Key takeaways = the rest)
        self.chat_ratio = 0.80         # answers share of the Claude panel (prompt box = the rest)
        btn = dict(font=(UI_FONT, 9, "bold"), relief="flat", bd=0, fg="white",
                   activeforeground="white", cursor="hand2")
        self.panel_btns = {
            "left": tk.Button(strip_l, command=lambda: self.toggle_panel("left"), **btn),
            "right": tk.Button(strip, command=lambda: self.toggle_panel("right"), **btn),
        }
        for b in self.panel_btns.values():
            b.place(relx=0.5, rely=0.5, anchor="center", width=26, height=230)
        # Learning: quick button at the bottom of the right edge (opens the panel + the menu); Ctrl+L too
        learning.setup(sys.modules[__name__])
        self.learning = learning.LearningUI(self, self.learn_btn)
        self.edge_learn = tk.Button(strip, text="🎓" + chr(10) + chr(10).join("Learning"),
                                    command=self.learning.show_menu, bg="#8a3f22", activebackground="#6e321b",
                                    **btn)
        self.edge_learn.place(relx=0.5, rely=1.0, y=-8, anchor="s", width=26, height=150)
        root.bind_all("<Control-k>", lambda e: self.toggle_panel("right"))
        root.bind_all("<Control-l>", lambda e: self.learning.show_menu())
        if IS_MAC:
            root.bind_all("<Command-k>", lambda e: self.toggle_panel("right"))
            root.bind_all("<Command-l>", lambda e: self.learning.show_menu())
        self._refresh_panel_btn()
        # keep the pane proportions when the window is resized (a dragged divider sets a new ratio)
        self.body.bind("<Configure>", lambda e: self._place_sashes())
        self.claude_panel.bind("<Configure>", lambda e: self._place_sashes())
        self.body.bind("<ButtonRelease-1>", lambda e: (self._remember_ratio(self.body, "body_ratio"),
                                                       self._place_sashes()))
        self.claude_panel.bind("<ButtonRelease-1>", lambda e: (self._remember_ratio(self.claude_panel, "body_ratio"),
                                                               self._place_sashes()))
        self._align_pending = set()
        self._align_all_job = None
        self._syncing = False
        self._sync_dst, self._sync_quiet = None, 0.0

        root.columnconfigure(0, weight=0)
        root.columnconfigure(1, weight=1, uniform="col")
        root.columnconfigure(2, weight=1, uniform="col")
        root.columnconfigure(3, weight=0)
        root.rowconfigure(1, weight=1)
        root.after(80, self._place_sashes)
        root.after(400, self._place_sashes)      # again once the window has its final size

        self.recognizer = Recognizer(self.ui_q, self._on_english)
        self.translator = Translator(client, self.ui_q, self.preset_var.get())
        self.summarizer = Summarizer(client, self.ui_q, self.transcript_range, lambda: self.last_summary)
        self.chatter = Chatter(client, self.ui_q, self._chat_context)
        self.recognizer.start()
        self.summarizer.start()
        self.chatter.get_library = self.learning.lib.catalog_text
        self.chatter.start()
        self.root.after(50, self._poll)

    def _place_sashes(self):
        """Pane sizes by ratio: EN/VI 3/4 + takeaways 1/4; Claude answers ~4/5 + prompt box."""
        try:
            self._place_sashes_now()
        except tk.TclError:          # window is closing
            pass

    def _place_sashes_now(self):
        hb = self.body.winfo_height()
        if hb > 100:
            self.body.sash_place(0, 0, int(hb * self.body_ratio))
        hc = self.claude_panel.winfo_height()
        if hc > 100 and self.panel_side:
            # the prompt box is exactly as tall as Key takeaways (same divider height), on both sides
            self.claude_panel.sash_place(0, 0, self.body.sash_coord(0)[1])
        self.root.after_idle(lambda: self.take_follow and self.bottom.see("end"))

    def _remember_ratio(self, pw, attr):
        h = pw.winfo_height()
        if h > 100:
            setattr(self, attr, min(0.9, max(0.1, pw.sash_coord(0)[1] / h)))

    # ---- Claude panel open/close (same panel, shown over the left or the right half) ----
    def toggle_panel(self, side="right"):
        if self.panel_side == side:                 # same edge again -> close
            self.panel_side = None
            self.claude_panel.place_forget()
        else:
            self.panel_side = side
            # cover only the English (left) or Vietnamese (right) pane; Key takeaways stays visible
            # covers the whole left or right half; its prompt box lines up with Key takeaways
            self.claude_panel.place(in_=self.body, relx=0 if side == "left" else 0.5, rely=0,
                                    relwidth=0.5, relheight=1)
            self.claude_panel.lift()
            self.unread = False
            self.root.after(30, self._place_sashes)
            self.root.after(250, self._place_sashes)     # again once the panel has its final size
            self.prompt.focus_set()
        self._refresh_panel_btn()

    @property
    def panel_open(self):
        return self.panel_side is not None

    def _refresh_panel_btn(self):
        letters = chr(10).join("Claude")          # vertical label
        gap = chr(10)
        for side, b in self.panel_btns.items():
            opens = "▶" if side == "left" else "◀"     # arrow points to where the panel slides
            closes = "◀" if side == "left" else "▶"
            if self.panel_side == side:
                b.config(text=closes + gap + letters, bg="#8a8a8a", activebackground="#6f6f6f")
            elif self.unread:                        # new answer arrived while closed
                b.config(text=opens + gap + letters + gap + "●", bg="#e0662f", activebackground="#c4643f")
            else:
                b.config(text=opens + gap + letters, bg="#d97757", activebackground="#c4643f")

    # ---- English and Vietnamese scroll together ----
    # Scrolling rules: the panes follow the newest text automatically, unless the user scrolls up to
    # read (then they stay put until the user scrolls back to the bottom). The two panes are synced
    # only when the user scrolls or when we follow new text, never just because text changed.
    def _scroll_both(self, *args):                 # the shared scrollbar
        self.left.yview(*args)
        self._sync_from(self.left, self.right)
        self._user_scrolled()

    def _wheel(self, event):                       # mouse wheel over English or Vietnamese
        w = event.widget
        if IS_MAC:
            w.yview_scroll(-event.delta, "units")    # Mac: small deltas, many events (trackpad)
        else:
            w.yview_scroll(-3 if event.delta > 0 else 3, "units")
        self._sync_from(w, self.right if w is self.left else self.left)
        self._user_scrolled()
        return "break"

    def _user_scrolled(self):
        self.follow = self.left.yview()[1] >= 0.999 or self.right.yview()[1] >= 0.999

    def _on_text_scroll(self, w, first, last):
        if w is self.right:
            self.vsb.set(first, last)

    def _sync_from(self, src, dst):
        """Show in `dst` the same row (segment) that is at the top of `src`."""
        if self._syncing:
            return
        self._syncing = True
        try:
            top = src.index("@0,0")
            blk = next((t for t in src.tag_names(top) if t.startswith("b") and t[1:].isdigit()), None)
            total_dst = dst.count("1.0", "end", "ypixels")
            total_dst = total_dst[0] if isinstance(total_dst, tuple) else total_dst
            if blk and dst.tag_ranges(blk) and total_dst:
                info = src.dlineinfo(top)
                into = src.count(src.tag_ranges(blk)[0], top, "ypixels")
                into = (into[0] if isinstance(into, tuple) else into) or 0
                pady = int(str(src.cget("pady")))
                into -= ((info[1] - pady) if info else 0)  # part of the top line already scrolled off
                y = dst.count("1.0", dst.tag_ranges(blk)[0], "ypixels")
                y = (y[0] if isinstance(y, tuple) else y) or 0
                dst.yview_moveto(max(0.0, (y + into) / total_dst))
                # fine-tune to the pixel: put the row at exactly the same height in both panes
                dst.update_idletasks()
                first_uid = int(blk[1:])
                for u in range(first_uid, first_uid + 6):     # first row whose start is on screen in both
                    rs, rd = src.tag_ranges(f"b{u}"), dst.tag_ranges(f"b{u}")
                    if not (rs and rd):
                        continue
                    a, b = src.dlineinfo(rs[0]), dst.dlineinfo(rd[0])
                    if a and b:
                        if a[1] != b[1]:
                            dst.yview_scroll(b[1] - a[1], "pixels")
                        break
            else:
                dst.yview_moveto(src.yview()[0])
            if dst is self.right:
                self.vsb.set(*dst.yview())
        finally:
            self._syncing = False
            self._sync_dst, self._sync_quiet = dst, time.time() + 0.15

    def _follow(self):
        """Show the newest rows in both panes (unless the user scrolled up to read)."""
        if self.follow:
            self.left.see("end")
            self._sync_from(self.left, self.right)
            if self.right.yview()[1] < 0.999 and not self.right.dlineinfo("end-1c"):
                self.right.see("end")                # Vietnamese has a newer row than English shows

    # ---- Claude chat ----
    def _prompt_enter(self, event):
        if event.state & 0x0001:          # Shift held -> normal new line
            return None
        self.send_prompt()
        return "break"

    def attach_files(self):
        from tkinter import filedialog, messagebox
        paths = filedialog.askopenfilenames(
            parent=self.root, title="Chọn file gửi cho Claude",
            filetypes=[("Ảnh, PDF, Word, văn bản", " ".join("*" + e for e in ATTACH_TYPES)),
                       ("Tất cả", "*.*")])
        problems = []
        for p in paths:
            try:
                self.attachments.append((os.path.basename(p), file_to_block(p)))
            except ValueError as e:
                problems.append(f"{os.path.basename(p)}: {e}")
        self._refresh_attach_bar()
        if problems:
            messagebox.showwarning("Không đính kèm được", "\n".join(problems), parent=self.root)
        self.prompt.focus_set()

    def _refresh_attach_bar(self):
        for w in self.attach_bar.winfo_children():
            w.destroy()
        job = self.learning.job if getattr(self, "learning", None) else None
        if not self.attachments and not job:
            self.attach_bar.pack_forget()
            return
        if job:                                   # documents the Learning menu adds to the next prompt
            chip = tk.Frame(self.attach_bar, bg="#e6d3f5")
            chip.pack(side="left", padx=(6, 0), pady=(4, 0))
            tk.Label(chip, text="🎓 " + job["label"], bg="#e6d3f5", font=(UI_FONT, 9)).pack(side="left", padx=(6, 2))
            tk.Button(chip, text="✕", bg="#e6d3f5", relief="flat", bd=0, font=(UI_FONT, 8), cursor="hand2",
                      command=lambda: (self.learning.take_job(), self._refresh_attach_bar())).pack(side="left",
                                                                                                  padx=(0, 4))
        for i, (name, _) in enumerate(self.attachments):
            chip = tk.Frame(self.attach_bar, bg="#f3e3dc")
            chip.pack(side="left", padx=(6, 0), pady=(4, 0))
            short = name if len(name) <= 28 else name[:25] + "..."
            tk.Label(chip, text="📎 " + short, bg="#f3e3dc", font=(UI_FONT, 9)).pack(side="left", padx=(6, 2))
            tk.Button(chip, text="✕", bg="#f3e3dc", relief="flat", bd=0, font=(UI_FONT, 8), cursor="hand2",
                      command=lambda i=i: (self.attachments.pop(i), self._refresh_attach_bar())).pack(side="left",
                                                                                                padx=(0, 4))
        self.attach_bar.pack(fill="x", before=self.prompt_row)

    def send_prompt(self):
        q = self.prompt.get("1.0", "end").strip()
        files = self.attachments
        if (not q and not files) or self.chat_busy:
            return
        if not q:
            q = "Hãy xem (các) file đính kèm và tóm tắt nội dung chính."
        job = self.learning.take_job()            # documents from the Learning menu, if any
        self._chat_failed = False
        self.prompt.delete("1.0", "end")
        self.attachments = []
        self._refresh_attach_bar()
        learn_blocks = []
        if job:
            try:
                learn_blocks = self.learning.blocks(job) if job["kind"] != "journal" else []
            except Exception as e:  # noqa: BLE001
                self.status.config(text=f"🎓 Không đọc được tài liệu: {e}", fg="#ff7b7b")
        self.chat_busy = True
        self.send_btn.config(state="disabled", text="...")
        w = self.chat
        w.configure(state="normal")
        if w.index("end-1c") != "1.0":
            w.insert("end", "\n\n")
        w.insert("end", "Bạn: ", "you")
        w.insert("end", q + "\n")
        if files:
            w.insert("end", "📎 " + ", ".join(n for n, _ in files) + "\n", "file")
        if job:
            w.insert("end", "🎓 " + job["label"] + "\n", "file")
        w.insert("end", "Claude: ", "who")
        w.mark_set("answer_start", "end-1c")      # file names in this answer become links (Learning)
        w.mark_gravity("answer_start", "left")
        w.see("end")
        w.configure(state="disabled")
        names = [n for n, _ in files] + (["🎓 " + job["label"]] if job else [])
        self.chat_log.append({"q": q, "files": names, "a": ""})
        if job and job["kind"] == "journal":
            self.learning.run_journal(q, job)     # fills the PDF template instead of a normal answer
            return
        self.chatter.q.put((q, learn_blocks + [b for _, b in files]))

    # ---- Backup / Restore of the whole session ----
    def _session_data(self):
        rows = []
        for uid in sorted(self.uid_spk):
            en = self.translator.en.get(uid) or ""
            rng = self.left.tag_ranges(f"b{uid}")
            if not en and rng:                         # still a draft: take what is on screen
                en = self.left.get(rng[0], rng[1])
            rows.append({"uid": uid, "speaker": self.uid_spk[uid], "en": en.strip(),
                         "vi": (self.translator.vi.get(uid) or "").strip()})
        sp = self.recognizer.speakers
        return {
            "app": "LiveTranslator", "version": APP_VERSION, "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rows": rows,
            "speakers": {"custom": self.spk_custom, "teachers": self.teachers, "talk": self.spk_talk,
                         "alias": self.spk_alias, "voices": {str(k): v.tolist() for k, v in sp.sums.items()},
                         "next_id": sp.next_id},
            "transcript": self.transcript,
            "takeaways": self.last_summary,
            "chat": self.chat_log,
            "chat_history": [m if isinstance(m["content"], str) else
                             {"role": m["role"], "content": " ".join(
                                 b.get("text", "") if b.get("type") == "text" else f"[file đính kèm: {b.get('title', b.get('type'))}]"
                                 for b in m["content"])}
                             for m in self.chatter.history],
        }

    def backup_session(self):
        from tkinter import filedialog, messagebox
        default = f"LiveTranslator_backup_{time.strftime('%Y%m%d_%H%M')}.json"
        path = filedialog.asksaveasfilename(parent=self.root, title="Lưu backup", defaultextension=".json",
                                            initialfile=default, filetypes=[("Live Translator backup", "*.json")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._session_data(), f, ensure_ascii=False, indent=1)
            self.status.config(text=f"✓ Đã backup: {os.path.basename(path)}", fg="#7ee2a8")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Backup lỗi", str(e), parent=self.root)

    def restore_session(self):
        from tkinter import filedialog, messagebox
        path = filedialog.askopenfilename(parent=self.root, title="Mở file backup",
                                          filetypes=[("Live Translator backup", "*.json"), ("Tất cả", "*.*")])
        if not path:
            return
        try:
            data = json.load(open(path, encoding="utf-8"))
            if data.get("app") != "LiveTranslator":
                raise ValueError("đây không phải file backup của Live Translator")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Không mở được backup", str(e), parent=self.root)
            return
        if self.uid_spk and not messagebox.askyesno(
                "Restore", "Nội dung hiện tại sẽ được thay bằng nội dung trong file backup. Tiếp tục?",
                parent=self.root):
            return
        if self.on:
            self.toggle_onoff()                       # stop listening while loading
        self.load_session(data)
        self.status.config(text=f"✓ Đã restore: {os.path.basename(path)} ({len(data.get('rows', []))} dòng)",
                           fg="#7ee2a8")

    def load_session(self, data):
        """Rebuild every pane from backup data (also restores voices so new speech matches old speakers)."""
        for w in (self.left, self.right, self.bottom, self.chat):
            w.configure(state="normal")
            w.delete("1.0", "end")
            for t in w.tag_names():
                if t[:1] in ("b", "u", "v") and t[1:].isdigit() or t.startswith(("pad", "vsep")):
                    w.tag_delete(t)
            w.configure(state="disabled")
        sp = data.get("speakers", {})
        conv = lambda d: {int(k): v for k, v in (d or {}).items()}
        self.spk_custom = conv(sp.get("custom"))
        self.teachers = [int(t) for t in sp.get("teachers", [])]
        self.spk_talk = {int(k): float(v) for k, v in (sp.get("talk") or {}).items()}
        self.spk_alias = {int(k): int(v) for k, v in (sp.get("alias") or {}).items()}
        self.spk_shown, self.last_spk = {}, {"en": None, "vi": None}
        self.uid_spk, self.vi_gen, self.vi_filled = {}, {}, {}
        tr = self.recognizer.speakers
        tr.sums = {int(k): np.asarray(v, dtype=np.float32) for k, v in (sp.get("voices") or {}).items()}
        tr.talk = dict(self.spk_talk)
        tr.merged = dict(self.spk_alias)
        tr.next_id = int(sp.get("next_id", max(tr.sums, default=0) + 1))
        tr.last = None
        self.translator.en, self.translator.vi, self.translator.gen = {}, {}, {}
        rows = data.get("rows", [])
        for r in rows:
            uid, sid = int(r["uid"]), int(r["speaker"])
            self.uid_spk[uid] = sid
            self.recognizer.utt_spk[uid] = sid
            self.translator.en[uid] = r.get("en", "")
            self.translator.vi[uid] = r.get("vi", "")
            self.translator.gen[uid] = 1
            self._add_draft(uid, r.get("en", ""))
            self._finalize([uid], r.get("en", ""))
            self._vi_slot(uid)
            self._vi_piece(uid, 1, r.get("vi", "") or " ")
        self.recognizer.utt_id = max([int(r["uid"]) for r in rows], default=self.recognizer.utt_id)
        self.transcript = [tuple(t) for t in data.get("transcript", [])]
        self.last_summary = data.get("takeaways", "")
        self.bottom.configure(state="normal")
        for i, part in enumerate(re.split(r"\n\n(?=\[\d\d:\d\d\]\n)", self.last_summary) if self.last_summary else []):
            if i:
                self.bottom.insert("end", "\n\n")
            m = re.match(r"(\[\d\d:\d\d\]\n)", part)
            if m:
                self.bottom.insert("end", m.group(1), "ttime")
                part = part[m.end():]
            self.bottom.insert("end", part)
        self.bottom.configure(state="disabled")
        self.take_follow = True
        self.summarizer.done_n = len(self.transcript)
        self.chat_log = list(data.get("chat", []))
        w = self.chat
        w.configure(state="normal")
        for i, turn in enumerate(self.chat_log):
            if i:
                w.insert("end", "\n\n")
            w.insert("end", "Bạn: ", "you")
            w.insert("end", turn.get("q", "") + "\n")
            if turn.get("files"):
                w.insert("end", "📎 " + ", ".join(turn["files"]) + "\n", "file")
            w.insert("end", "Claude: ", "who")
            w.insert("end", turn.get("a", ""))
        w.configure(state="disabled")
        self.chatter.history = list(data.get("chat_history", []))
        self._schedule_align_all()
        self.root.after(200, lambda: (self.left.see("end"), self._sync_from(self.left, self.right),
                                      self.chat.see("end"), self.bottom.see("end")))

    def export_takeaways(self):
        from tkinter import filedialog, messagebox
        if not self.last_summary.strip():
            messagebox.showinfo("Key takeaways", "Chưa có ý chính nào để xuất.", parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="Xuất Key takeaways ra PDF",
                                            defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
                                            initialfile=f"KeyTakeaways_{time.strftime('%Y%m%d_%H%M')}.pdf")
        if not path:
            return
        try:
            learning.takeaways_pdf(path, self.last_summary)
            self.status.config(text=f"✓ Đã xuất PDF: {os.path.basename(path)}", fg="#7ee2a8")
            learning.open_path(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Không xuất được PDF", str(e), parent=self.root)

    def _take_scrolled(self):
        self.take_follow = self.bottom.yview()[1] >= 0.999

    def _add_takeaways(self, bullets):
        """Add new takeaways under a time stamp; the whole history stays (also in last_summary/backup)."""
        stamp = time.strftime("%H:%M")
        block = f"[{stamp}]\n{bullets.strip()}"
        self.last_summary = (self.last_summary + "\n\n" + block) if self.last_summary else block
        w = self.bottom
        w.configure(state="normal")
        if w.index("end-1c") != "1.0":
            w.insert("end", "\n\n")
        w.insert("end", f"[{stamp}]\n", "ttime")
        w.insert("end", bullets.strip())
        w.configure(state="disabled")
        if self.take_follow:
            self.root.after_idle(lambda: w.see("end"))

    def _chat_context(self):
        """What Claude knows about the session when answering (read from the UI thread's data)."""
        en = self.transcript_text()[-40000:]
        vi = "\n".join(v for _, v in self.translator.history)[-20000:]
        return en, vi, self.last_summary

    # ---- events ----
    def _on_english(self, ids, text):
        # called from the Whisper thread with the accurate text for one or more utterances
        self.transcript.append((self.recognizer.utt_spk.get(ids[0], 1), text))
        self.ui_q.put(("final", (ids, text)))

    def transcript_range(self, start=0, end=None):
        """(end index, text) of transcript entries start..end (used by the running takeaways)."""
        items = list(self.transcript)
        end = len(items) if end is None else end
        return end, self._transcript_lines(items[start:end])

    def transcript_text(self):
        """English transcript with speaker names, e.g. 'Teacher: ...' (used by chat)."""
        return self._transcript_lines(list(self.transcript))

    def _transcript_lines(self, items):
        lines, last = [], None
        for sid, text in items:
            if sid != last:
                lines.append(f"{self.speaker_name(sid)}: {text}")
                last = sid
            else:
                lines[-1] += " " + text
        return "\n".join(lines)

    # ---- speakers ----
    # soft, muted colors (readable on white, nothing bright): steel blue, terracotta, moss green,
    # dusty purple, ochre, teal, plum
    SPK_COLORS = ["#2f5d8a", "#9a5235", "#3f7a4f", "#6c5190", "#8a6d1f", "#2e7676", "#86476a"]

    def _spk_tags(self, w, sid):
        """Configure (once) the color tags for a speaker in a pane; returns (label tag, text tag)."""
        color = self.SPK_COLORS[(self._res(sid) - 1) % len(self.SPK_COLORS)]
        w.tag_configure(f"lbl{sid}", foreground=color)
        w.tag_configure(f"spk{sid}", foreground=color)
        w.tag_raise("lbl")                      # label stays bold on top of the color tags
        return f"lbl{sid}", f"spk{sid}"

    def _res(self, sid):
        """Current id of a speaker (follows merges)."""
        seen = 0
        while sid in self.spk_alias and seen < 50:
            sid, seen = self.spk_alias[sid], seen + 1
        return sid

    def _apply_merges(self, merged):
        """Two groups turned out to be one voice: the old one takes the kept one's name and color."""
        new = [g for g in merged if g not in self.spk_alias]
        self.spk_alias = dict(merged)
        for gone in new:
            keep = self._res(gone)
            if gone in self.spk_custom and keep not in self.spk_custom:
                self.spk_custom[keep] = self.spk_custom[gone]
            if gone in self.teachers:
                self.teachers = [keep if t == gone else t for t in self.teachers]
                self.teachers = list(dict.fromkeys(self.teachers))
            for w in (self.left, self.right):
                self._spk_tags(w, gone)               # recolor its tags with the kept speaker's color
        for pane in self.last_spk:
            if self.last_spk[pane] is not None:
                self.last_spk[pane] = self._res(self.last_spk[pane])
        self._relabel()

    def speaker_name(self, sid):
        """Name typed by the user, else Teacher (spoke > TEACHER_MINUTES; numbered if several),
        else 'Người nói N'. Colors stay tied to the speaker, whatever the name."""
        sid = self._res(sid)
        if sid in self.spk_custom:
            return self.spk_custom[sid]
        if sid in self.teachers:
            return "Teacher" if len(self.teachers) == 1 else f"Teacher {self.teachers.index(sid) + 1}"
        return f"Người nói {sid}"

    def _block_start(self, w, pane, uid, sid):
        """Start row `uid` on a new line; a new speaker also gets a blank line and their name.
        Everything in the row is tagged b<uid> so the two panes can be lined up row by row."""
        blk = f"b{uid}"
        sid = self._res(sid)
        if self.last_spk[pane] != sid:
            self.last_spk[pane] = sid
            tag, _ = self._spk_tags(w, sid)
            if w.index("end-1c") != "1.0":
                w.insert("end", "\n", (blk,))           # blank line between speakers
            name = self.speaker_name(sid)
            self.spk_shown[sid] = name
            w.insert("end", name + ": ", ("lbl", tag, blk))
        return blk

    def _relabel(self):
        """Update every label on screen whose name changed (new Teacher, or renamed by the user)."""
        changed = False
        for sid in list(self.spk_shown):
            name = self.speaker_name(sid)
            if name == self.spk_shown[sid]:
                continue
            self.spk_shown[sid] = name
            changed = True
            for w in (self.left, self.right):
                w.configure(state="normal")
                rng = w.tag_ranges(f"lbl{sid}")
                for i in range(len(rng) - 2, -1, -2):          # from the end, so indexes stay valid
                    a = w.index(rng[i])
                    keep = [t for t in w.tag_names(a) if t.startswith("b") and t[1:].isdigit()]
                    w.delete(rng[i], rng[i + 1])
                    w.insert(a, name + ": ", tuple(["lbl", f"lbl{sid}"] + keep))
                w.configure(state="disabled")
        if changed:
            self._schedule_align_all()

    def _rename_speaker(self, event):
        from tkinter import simpledialog
        w = event.widget
        tags = w.tag_names(f"@{event.x},{event.y}")
        sid = next((int(t[3:]) for t in tags if t.startswith("lbl") and t[3:].isdigit()), None)
        if sid is None:
            return
        name = simpledialog.askstring("Đổi tên người nói", f"Tên cho '{self.speaker_name(sid)}':",
                                      initialvalue=self.spk_custom.get(sid, ""), parent=self.root)
        if name is None:
            return
        name = name.strip()
        sid = self._res(sid)
        if name:
            self.spk_custom[sid] = name
        else:
            self.spk_custom.pop(sid, None)       # empty -> back to automatic name
        self._relabel()

    # English pane: rows of final text (speaker color) / drafts (grey italic), then the live words.
    def _set_partial(self, text):
        w = self.left
        w.configure(state="normal")
        rng = w.tag_ranges("partial")
        if rng:
            w.delete(rng[0], rng[1])
        if text:
            w.insert("end", text, "partial")
        w.configure(state="disabled")
        self._follow()

    def _add_draft(self, uid, text):
        self._set_partial("")
        w = self.left
        w.configure(state="normal")
        blk = self._block_start(w, "en", uid, self.uid_spk.get(uid, 1))
        w.insert("end", text, ("draft", f"u{uid}", blk))
        w.insert("end", "\n", (blk, f"pad{uid}"))
        w.configure(state="disabled")
        self._schedule_align(uid)
        self._follow()

    def _finalize(self, ids, text):
        w = self.left
        w.configure(state="normal")
        for uid in ids:
            rng = w.tag_ranges(f"u{uid}")
            if not rng:
                continue
            a = w.index(rng[0])
            w.delete(rng[0], rng[1])
            if uid == ids[0]:
                _, spk_tag = self._spk_tags(w, self.uid_spk.get(uid, 1))
                w.insert(a, split_sentences(text.strip()), (spk_tag, f"b{uid}"))
        w.configure(state="disabled")
        for uid in ids:
            self._schedule_align(uid)

    # Vietnamese pane: one row per English row (tag v<uid> = the translation being streamed in).
    def _vi_edit(self, fn):
        w = self.right
        w.configure(state="normal")
        fn(w)
        w.configure(state="disabled")

    def _vi_slot(self, uid):
        self.vi_gen[uid] = 1
        self.vi_filled[uid] = False
        sid = self.uid_spk.get(uid, 1)

        def add(w):
            blk = self._block_start(w, "vi", uid, sid)
            w.insert("end", "…", ("vipend", f"v{uid}", blk))
            w.insert("end", "\n", (blk, f"pad{uid}"))
        self._vi_edit(add)
        self._schedule_align(uid)
        self._follow()

    def _vi_piece(self, uid, g, text):
        if self.vi_gen.get(uid) != g:
            return                                   # output of an older translation of this row
        def put(w):
            rng = w.tag_ranges(f"v{uid}")
            if not rng:
                return
            _, spk_tag = self._spk_tags(w, self.uid_spk.get(uid, 1))
            if not self.vi_filled[uid]:             # replace the "…" placeholder
                start = w.index(rng[0])
                w.delete(rng[0], rng[1])
                w.insert(start, text.lstrip(), (f"v{uid}", spk_tag, f"b{uid}"))
                self.vi_filled[uid] = True
            else:
                w.insert(rng[1], text, (f"v{uid}", spk_tag, f"b{uid}"))
            # one sentence per line, like the English side: turn "sentence. Next" into a line break
            rng = w.tag_ranges(f"v{uid}")
            body = w.get(rng[0], rng[1])
            for m in reversed(list(sentence_gaps(body))):
                a = w.index(f"{rng[0]} + {m.start(1)}c")
                b = w.index(f"{rng[0]} + {m.end(1)}c")
                w.delete(a, b)
                w.insert(a, "\n", (f"v{uid}", spk_tag, f"b{uid}"))
        self._vi_edit(put)
        self._schedule_align(uid)

    def _vi_reset(self, uid, g):
        self.vi_gen[uid] = g
        self.vi_filled[uid] = False
        def reset(w):
            rng = w.tag_ranges(f"v{uid}")
            if rng:
                start = w.index(rng[0])
                w.delete(rng[0], rng[1])
                w.insert(start, "…", ("vipend", f"v{uid}", f"b{uid}"))
        self._vi_edit(reset)
        self._schedule_align(uid)

    def _vi_drop(self, uid):
        self.vi_gen[uid] = -1
        def drop(w):
            rng = w.tag_ranges(f"v{uid}")
            if rng:
                w.delete(rng[0], rng[1])
        self._vi_edit(drop)
        self._schedule_align(uid)

    # ---- line the rows up: pad the shorter side so each row starts at the same height ----
    def _schedule_align(self, uid):
        if not self._align_pending:
            self.root.after(15, self._run_align)
        self._align_pending.add(uid)

    def _schedule_align_all(self):
        if self._align_all_job:
            self.root.after_cancel(self._align_all_job)
        self._align_all_job = self.root.after(120, self._align_all)

    def _align_all(self):
        self._align_all_job = None
        uids = sorted(set(int(t[3:]) for t in self.left.tag_names() if t.startswith("pad") and t[3:].isdigit()))
        self._align_pending.update(uids)
        self._run_align()

    def _row_height(self, w, uid):
        rng = w.tag_ranges(f"b{uid}")
        if not rng:
            return None
        h = w.count(rng[0], rng[1], "update", "ypixels")      # top of the row -> top of the next one
        return (h[0] if isinstance(h, tuple) else h) or 0

    def _run_align(self):
        uids, self._align_pending = sorted(self._align_pending), set()
        for uid in uids:
            pad = f"pad{uid}"
            if not (self.left.tag_ranges(pad) and self.right.tag_ranges(pad)):
                continue                              # the other side of this row doesn't exist yet
            for w in (self.left, self.right):
                # Tk takes line spacing from the line's first character, so the padding tag must
                # cover the row's whole last line (not just its line break)
                rng = w.tag_ranges(f"b{uid}")
                if rng:
                    # only the CURRENT last line: when a row grew, its old last line (or the speaker
                    # label left at its start) kept the tag and the padding was added twice
                    w.tag_remove(pad, rng[0], rng[1])
                    w.tag_add(pad, w.index(f"{rng[1]} -1c linestart"), rng[1])
                w.tag_configure(pad, spacing3=0)
            hl, hr = self._row_height(self.left, uid), self._row_height(self.right, uid)
            if hl is None or hr is None:
                continue
            if hl > hr:
                self.right.tag_configure(pad, spacing3=hl - hr)
            elif hr > hl:
                self.left.tag_configure(pad, spacing3=hr - hl)
        self._follow()

    def _set_preset(self, name):
        self.translator.set_preset(name)
        cfg = load_config()
        cfg["translate_preset"] = name
        save_config(cfg)

    def logout(self):
        """Forget the saved login and restart at the login screen."""
        cfg = load_config()
        method = cfg.get("method")
        for k in ("remember", "method", "api_key"):
            cfg.pop(k, None)
        save_config(cfg)
        ant = find_ant()
        if method == "account" and ant:
            try:
                subprocess.run([ant, "auth", "logout"], capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:  # noqa: BLE001
                pass
        args = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
        subprocess.Popen(args, cwd=APP_DIR)
        self.recognizer.running.clear()
        self.root.destroy()

    def toggle_onoff(self):
        self.on = not self.on
        if self.on:
            self.toggle.config(text="ON", bg="#27ae60", activebackground="#1e8449")
            self.recognizer.running.set()
            if not self.ready:
                self.status.config(text="Đang chuẩn bị nhận diện giọng nói... sẽ tự bắt đầu nghe khi xong.",
                                   fg="#ddd")
        else:
            self.toggle.config(text="OFF", bg="#777", activebackground="#555")
            self.recognizer.running.clear()
            self.summarizer.final = True
            self.root.after(1500, self.summarizer.kick.set)  # final takeaways for the last words

    # ---- UI updates (main thread only) ----
    def _append(self, widget, text):
        widget.configure(state="normal")
        widget.insert("end", text)
        widget.see("end")
        widget.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                kind, val = self.ui_q.get_nowait()
                if kind == "partial":
                    self._set_partial(val)
                elif kind == "speaker":
                    uid, sid, talk, *rest = val
                    merged = rest[0] if rest else self.spk_alias
                    self.uid_spk[uid] = sid
                    self.spk_talk = talk
                    if merged != self.spk_alias:
                        self._apply_merges(merged)
                    for s, secs in talk.items():
                        if secs >= TEACHER_MINUTES * 60 and s not in self.teachers:
                            self.teachers.append(s)      # becomes Teacher (or Teacher 2, 3...)
                    self._relabel()
                elif kind == "draft":
                    self._add_draft(*val)
                    self.translator.draft(*val)          # translate right away, don't wait for Whisper
                elif kind == "final":
                    ids, text = val
                    self._finalize(ids, text)
                    self.translator.final(ids, text)     # re-translates only if the words changed
                elif kind == "flush":
                    pass
                elif kind == "vi_slot":
                    self._vi_slot(val)
                elif kind == "vi_piece":
                    self._vi_piece(*val)
                elif kind == "vi_reset":
                    self._vi_reset(*val)
                elif kind == "vi_drop":
                    self._vi_drop(val)
                elif kind == "chat_piece":
                    if self.chat_log:
                        self.chat_log[-1]["a"] += val
                    self._append(self.chat, val)
                elif kind == "chat_error":
                    self._chat_failed = True
                    self.chat.configure(state="normal")
                    self.chat.insert("end", val, "err")
                    self.chat.configure(state="disabled")
                elif kind == "chat_end":
                    self.chat_busy = False
                    self.send_btn.config(state="normal", text="Gửi ➤")
                    if self.panel_open:
                        self.prompt.focus_set()
                    else:                       # answered while the panel was closed -> show a hint
                        self.unread = True
                        self._refresh_panel_btn()
                    # guided exercise: put the next step's prompt in the box (or finish the solution)
                    self.learning.on_chat_end(self.chat_log[-1] if self.chat_log else None,
                                              not getattr(self, "_chat_failed", False))
                elif kind == "summary":
                    self._add_takeaways(val)
                elif kind == "ready":
                    self.ready = True
                    if not self.on:
                        self.status.config(text="✓ Sẵn sàng. Nhấn ON để bắt đầu nghe.", fg="#7ee2a8")
                elif kind == "status":
                    self.status.config(text=val, fg="#ddd")
                elif kind == "error":
                    self.status.config(text=val, fg="#ff7b7b")
                elif kind == "call":                # run a function on the UI thread (Learning menu)
                    val()
                elif kind == "force_off":
                    if self.on:
                        self.toggle_onoff()
        except queue.Empty:
            pass
        take = getattr(self.prompt, "take_enter", None)
        if take and take():
            self.send_prompt()                  # Enter pressed in the native text box
        self.root.after(30, self._poll)


def selftest():
    """`LiveTranslator.exe --selftest` -> checks the bundled speech model, writes selftest.txt."""
    try:
        t = time.time()
        preload_models()
        get_models()
        msg = (f"OK v{APP_VERSION} models loaded in {time.time() - t:.1f}s\nwhisper: {whisper_model_path()}\n"
               f"vosk: {model_dir(VOSK_MODEL)}\nvoiceprints: {_PRELOAD['spk'] is not None}\n"
               f"native input box: {__import__('importlib').util.find_spec('native_edit') is not None}\n"
               f"ant: {find_ant()}")
    except Exception as e:  # noqa: BLE001
        msg = f"FAIL {e!r}"
    with open(os.path.join(DATA_DIR, "selftest.txt"), "w", encoding="utf-8") as f:
        f.write(msg)


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except Exception:
        pass
    if IS_MAC:
        root.tk.call("tk", "scaling", 1.33)     # Mac Tk draws fonts ~25% smaller; match the Windows look

    def start_main(client):
        for w in root.winfo_children():
            w.destroy()
        root.resizable(True, True)
        MainWindow(root, client)

    threading.Thread(target=preload_models, daemon=True).start()
    LoginWindow(root, start_main).try_auto_login()
    root.mainloop()


if __name__ == "__main__":
    # Must come first. On Mac, Python's multiprocessing starts helper processes (resource tracker)
    # by running this same app file; without this each helper opened the whole app again, forever.
    import multiprocessing
    multiprocessing.freeze_support()
    main()
