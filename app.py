"""Live Translator (multilingual, offline): pick the language you hear and the language to translate into,
then press ON:
  - left pane:   live transcript from the microphone (Vosk + Whisper, on this PC)
  - right pane:  live translation (NLLB-200, on this PC)
  - bottom pane: running key takeaways (Qwen2.5, on this PC)
Only the Claude panel (questions about the session) uses the internet and a Claude login.
"""

import hashlib
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

# The portable .exe has no console: sys.stdout/stderr are None there, and libraries that print progress
# (the model download's progress bar) crash with "'NoneType' object has no attribute 'write'".
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

os.environ.setdefault("HF_HUB_OFFLINE", "1")        # models are local files: never ask the internet for them

import anthropic
import numpy as np

import local_ai
import pdf_export
from local_ai import AUTO, LANGS, lang_name

# ---------------- Settings ----------------
APP_NAME = "LiveTranslatorML"     # multilingual offline edition (own data folder, own build name)
APP_VERSION = "2.0.0"             # shown in the title bar and used in the portable build's name
MODEL = os.environ.get("TRANSLATE_MODEL", "claude-opus-5-5")
# The login client waits at most 20 s for data. Chat sends whole documents, and Claude can read/think longer than that before the first word: give them more time.
LONG_TIMEOUT = anthropic.Timeout(300.0, connect=15.0)
# Live words come from Vosk (streaming); Whisper base.en then corrects each finished utterance.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
SAMPLE_RATE = 16000
FRAME = 480                      # 30 ms frames
VOSK_MODEL = "vosk-en-small"      # streaming model for live words (fast: ~0.2 s per 1 s audio)
UTT_SOFT = 3.0                   # after this long, cut the utterance at the next short pause (s)
UTT_HARD = 7.0                   # always cut by this length (s)
QUIET_ADD = float(os.environ.get("LT_QUIET_ADD") or 0.0005)   # pause = below 2x room noise + this; was 0.002,
                                 # which a far voice (no microphone) never rose above: every sentence was cut
UTT_MIN = 1.8                    # a piece Vosk ends before this is joined with what follows, unless a real
                                 # pause (>= 0.6 s) comes: far voices have short gaps mid-sentence
# phrases Whisper is known to invent on short or unclear audio (learned from video subtitles)
HALLUCINATION = re.compile(r"thank(s| you) for watching|please (like and )?subscribe|subtitles? by|"
                           r"see you (in the )?next (video|time)|amara\.org|transcribed by|"
                           # the same habit in other languages (YouTube subtitle credits it learned from)
                           r"subscribe cho kênh|ghiền mì gõ|đăng ký kênh|hẹn gặp lại các bạn trong|"
                           r"字幕|ご視聴ありがとうございました|チャンネル登録|請不吝點贊|订阅|"
                           r"sous-titr|untertitel|subtítulos|sottotitoli|продолжение следует|редактор субтитров", re.I)
# what Whisper says to plain noise when nobody spoke (dropped if nothing else was heard)
NOISE_GUESS = re.compile(r"(you|thank you|thanks|bye|okay|ok|uh|um|hmm|so|cảm ơn|cảm ơn các bạn|"
                         r"merci|danke|gracias|obrigado|grazie|спасибо|谢谢|ありがとう|감사합니다)[.!?。]?")
TEACHER_MINUTES = 10             # whoever has spoken this long becomes "Teacher" (several -> Teacher 1, 2...)
TEACHER_EARLY_MINUTES = 3        # ...or sooner: the main speaker (half of all speech so far) after this long
SUMMARY_EVERY = 60.0             # write new key takeaways this often (s), on this PC
# Folder next to the .exe (portable) or next to app.py.
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


def stable_data_dir():
    """Where settings, the downloaded language packs and the last session live: one folder for every version
    of this edition (%LOCALAPPDATA%\\LiveTranslatorML\\data). The first run takes the Claude login and the
    Output Folder from the classic Live Translator, if it is on this PC."""
    base = os.environ.get("LOCALAPPDATA") if getattr(sys, "frozen", False) and sys.platform == "win32" else ""
    if not base:
        return APP_DIR
    d = os.path.join(base, APP_NAME, "data")
    os.makedirs(d, exist_ok=True)
    old = os.path.join(LEGACY_DATA, "config.json") if LEGACY_DATA else ""
    if not os.path.exists(os.path.join(d, "config.json")) and old and os.path.exists(old):
        try:
            cfg = json.load(open(old, encoding="utf-8"))
            keep = {k: cfg[k] for k in ("remember", "method", "api_key", "output_folder") if k in cfg}
            with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
                json.dump(keep, f)
        except Exception:  # noqa: BLE001
            pass
    return d


# the classic Live Translator's data folder: its login is reused once, its downloaded small.en model always
LEGACY_DATA = (os.path.join(os.environ["LOCALAPPDATA"], "LiveTranslator", "data")
               if os.environ.get("LOCALAPPDATA") and sys.platform == "win32" else "")
DATA_DIR = stable_data_dir()         # settings, language packs (DATA_DIR/models), last session
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
AUTOSAVE_FILE = "session_autosave.json"     # in DATA_DIR: the session on screen, reopened at the next start
UI_FONT = "Segoe UI"
# language packs: downloaded into DATA_DIR/models; bundled ones are inside the .exe / next to app.py
STORE = local_ai.Store(DATA_DIR, (getattr(sys, "_MEIPASS", None), APP_DIR, LEGACY_DATA))


def whisper_model_path():
    """The bundled English Whisper model (works offline)."""
    return STORE.path(WHISPER_MODEL) or WHISPER_MODEL


# Listening quality (top bar). "Cao" uses a bigger Whisper model (more accurate on hard voices, slower):
# small.en for English, medium for the other languages; downloaded once (📦 Gói ngôn ngữ) the first time.
QUALITY_FAST, QUALITY_HIGH = "Nhanh", "Cao"


def whisper_pack(lang, quality):
    """Pack id of the Whisper model for a source language (code or AUTO) and a listening quality."""
    if lang == "en":
        return "small.en" if quality == QUALITY_HIGH else WHISPER_MODEL
    if lang in local_ai.FAST_ASR:                   # Fast = the fast recognizer; Cao = Whisper small on top
        return "whisper-small"
    return "whisper-medium" if quality == QUALITY_HIGH else "whisper-small"


def free_memory_mb():
    """Memory Windows can still hand out (MB): the smaller of free RAM+page file (commit) and free RAM x 2.
    None when unknown (other systems)."""
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total_phys", ctypes.c_ulonglong),
                        ("avail_phys", ctypes.c_ulonglong), ("total_pf", ctypes.c_ulonglong),
                        ("avail_pf", ctypes.c_ulonglong), ("total_v", ctypes.c_ulonglong),
                        ("avail_v", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]
        m = MS()
        m.len = ctypes.sizeof(m)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        return min(m.avail_pf, m.avail_phys * 2) / 2**20
    except Exception:  # noqa: BLE001
        return None


# Cao needs ~0.4-1.5 GB more (peak more while loading). When Windows runs out of memory the app crashes inside
# Tk (seen on 12/11: another program used 11 GB), so Cao is only started with room to spare, and is turned off
# again by itself if free memory gets low while it runs.
HQ_START_MB = 1500
HQ_KEEP_MB = 700


VOSK_QUIET_TARGET = float(os.environ.get("LT_VOSK_TARGET") or 0.02)   # speech below this (RMS) is raised for Vosk


class AudioCleaner:
    """Cleans mic audio for Whisper, block by block as it streams in.

    1. High-pass filter (~100 Hz): removes hum, fan/air-conditioner rumble and desk knocks, which carry no
       speech but mask it.
    2. Level boost: a quiet or far-away speaker is raised towards normal speech loudness (up to 10x), with
       a slow, smooth gain so words don't pump; background noise alone is never boosted more than speech.
    """
    TAPS = 513                         # FIR length: 32 ms, steep enough to cut 50/60 Hz hum, keeps voices
    TARGET = 0.08                      # speech loudness (RMS) the recognisers see
    MAX_GAIN = 10.0

    def __init__(self, cutoff=100.0, sr=SAMPLE_RATE, highpass=True, target=None):
        n = np.arange(self.TAPS) - (self.TAPS - 1) / 2
        low = np.sinc(2 * cutoff / sr * n) * np.hamming(self.TAPS)
        low /= low.sum()
        self.fir = -low
        self.fir[(self.TAPS - 1) // 2] += 1.0          # high-pass = all-pass minus low-pass
        self.target = target or self.TARGET
        if not highpass:                               # level boost only (Vosk: the filter hurt it in noise)
            self.fir = np.zeros(self.TAPS)
            self.fir[(self.TAPS - 1) // 2] = 1.0
        self.tail = np.zeros(self.TAPS - 1, np.float32)
        self.gain = 1.0
        self.level = None                              # recent speech loudness (after filtering)
        self.hist = []                                 # recent 30 ms loudness values -> noise floor

    def process(self, block):
        x = np.concatenate([self.tail, block.astype(np.float32)])
        self.tail = x[-(self.TAPS - 1):]
        y = np.convolve(x, self.fir, mode="valid").astype(np.float32)
        n = len(y) // FRAME
        if not n:
            return y * self.gain
        rms = np.sqrt((y[: n * FRAME].reshape(n, FRAME) ** 2).mean(axis=1))
        self.hist.extend(rms.tolist())
        del self.hist[:-500]
        floor = float(np.percentile(self.hist, 20)) if len(self.hist) > 30 else 0.003
        speech = rms[rms > max(floor * 3.0, 0.002)]
        start = self.gain
        if len(speech):
            v = float(np.sqrt((speech ** 2).mean()))
            self.level = v if self.level is None else self.level + (v - self.level) * (0.5 if v > self.level else 0.15)
            want = min(self.MAX_GAIN, max(1.0, self.target / max(self.level, 1e-5)))
            want = min(want, max(1.0, 0.02 / max(floor, 1e-5)))  # noise stays quiet (floor x gain <= 0.02)
            self.gain += (want - self.gain) * (0.6 if want < self.gain else 0.2)   # turn down faster than up
        ramp = np.linspace(start, self.gain, len(y), dtype=np.float32)              # no clicks
        return np.clip(y * ramp, -1.0, 1.0)

# Server-side refusal fallback (routes to another model instead of failing).
FALLBACK_HEADERS = {"anthropic-beta": "server-side-fallback-2026-07-01"}


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def output_dir(sub):
    """<Output Folder>/<sub> ("Backup" or "Outputs"), created if missing; None if no Output Folder is set."""
    root = load_config().get("output_folder")
    if not root or not os.path.isdir(root):
        return None
    d = os.path.join(root, sub)
    os.makedirs(d, exist_ok=True)
    return d


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
            p = os.path.join(base, "vendor", "ant.exe")
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

        tk.Label(frm, text="Đăng nhập bằng tài khoản Claude", font=("Segoe UI", 15, "bold"), bg=bg).pack(anchor="w")
        tk.Label(frm, text="Trang đăng nhập chính thức của Claude sẽ mở trong trình duyệt.",
                 font=("Segoe UI", 9), fg="#555", bg=bg).pack(anchor="w", pady=(4, 14))

        style = dict(font=("Segoe UI", 11), relief="solid", bd=1, cursor="hand2", anchor="center")
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
                 bg="#fff4e5", font=("Segoe UI", 9), justify="left").pack(anchor="w")
        row = tk.Frame(self.code_frame, bg="#fff4e5")
        row.pack(fill="x", pady=(6, 0))
        self.code_var = tk.StringVar()
        self.code_entry = tk.Entry(row, textvariable=self.code_var, font=("Consolas", 11))
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
                                font=("Segoe UI", 9))
        self.divider.pack(pady=(16, 8))

        self.key_var = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY") or load_config().get("api_key", ""))
        self.entry = tk.Entry(frm, textvariable=self.key_var, show="•", font=("Segoe UI", 11))
        self.entry.pack(fill="x", ipady=4)
        self.entry.bind("<Return>", lambda e: self.login_key())

        self.remember = tk.BooleanVar(value=load_config().get("remember", True))
        tk.Checkbutton(frm, text="Ghi nhớ đăng nhập (lần sau tự vào thẳng, không cần đăng nhập lại)",
                       variable=self.remember, bg=bg, wraplength=400, justify="left").pack(anchor="w", pady=(4, 0))

        self.btn = tk.Button(frm, text="Đăng nhập bằng API key", font=("Segoe UI", 11, "bold"), bg="#d97757",
                             fg="white", activebackground="#c4643f", relief="flat", command=self.login_key)
        self.btn.pack(fill="x", pady=(8, 4), ipady=3)

        self.status = tk.Label(frm, text="", font=("Segoe UI", 9), fg="#c0392b", wraplength=420,
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
            self.root.after(0, self._fail, "Thiếu công cụ đăng nhập Claude (vendor\\ant.exe). "
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
_PRELOAD = {"spk": None, "error": None, "done": threading.Event()}
_MODELS, _MODELS_LOCK = {}, threading.Lock()      # loaded speech models by pack id (switching back is instant)


def model_dir(name):
    return STORE.path(name)


def whisper_threads():
    return max(2, min(6, (os.cpu_count() or 4) - 2))


def get_whisper(pid):
    """A loaded Whisper model (pack id), loaded once. Raises FileNotFoundError when the pack is not on this PC."""
    with _MODELS_LOCK:
        if pid in _MODELS:
            return _MODELS[pid]
    path = STORE.path(pid)
    if not path:
        raise FileNotFoundError(pid)
    from faster_whisper import WhisperModel
    m = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=whisper_threads())
    list(m.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), language="en")[0])  # warm-up
    with _MODELS_LOCK:
        _MODELS[pid] = m
    return m


def vosk_pack(lang):
    if lang == "en":
        return VOSK_MODEL
    return f"vosk-{lang}" if f"vosk-{lang}" in local_ai.PACKS else None


def get_vosk(lang):
    """The live-words model for a language, or None (not installed, no Vosk model for it, or AUTO)."""
    pid = vosk_pack(lang)
    if not pid:
        return None
    with _MODELS_LOCK:
        if pid in _MODELS:
            return _MODELS[pid]
    path = STORE.path(pid)
    if not path:
        return None
    import vosk
    vosk.SetLogLevel(-1)
    m = vosk.Model(path)
    with _MODELS_LOCK:
        _MODELS[pid] = m
    return m


def get_fast_asr(pid):
    """The fast offline recognizer of a language (pack id), loaded once."""
    with _MODELS_LOCK:
        if pid in _MODELS:
            return _MODELS[pid]
    path = STORE.path(pid)
    if not path:
        raise FileNotFoundError(pid)
    m = local_ai.FastASR(path, threads=4)
    m.decode(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))                 # warm-up
    with _MODELS_LOCK:
        _MODELS[pid] = m
    return m


def unload_whisper(pid):
    with _MODELS_LOCK:
        _MODELS.pop(pid, None)


def preload_models(lang=None):
    """Load the speech models of the chosen language in the background as soon as the app starts."""
    lang = lang or load_config().get("src_lang", "en")
    try:
        _PRELOAD["spk"] = load_voiceprint_model()                      # who is speaking
        if lang in local_ai.FAST_ASR and STORE.have(local_ai.FAST_ASR[lang]):
            get_fast_asr(local_ai.FAST_ASR[lang])
        else:
            get_vosk(lang)
            get_whisper(whisper_pack(lang, QUALITY_FAST))
    except Exception as e:  # noqa: BLE001  (a missing pack is asked for when ON is pressed)
        _PRELOAD["error"] = e
    _PRELOAD["done"].set()


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
    """Live captions in two layers, in the language chosen in the top bar.

    1. Vosk (streaming) turns mic audio into words immediately -> grey "live" text, updated
       several times a second while the person is speaking. (Languages without a Vosk model, and
       "Tự nhận diện", skip this layer: utterances are cut at pauses by loudness alone.)
    2. When the utterance ends (a short pause), Whisper re-transcribes its audio for accurate,
       punctuated text, which replaces the grey draft and is what gets translated.
    """

    def __init__(self, ui_q, on_final, lang="en"):
        super().__init__(daemon=True)
        self.ui_q = ui_q
        self.on_final = on_final            # called with (utterance ids, final text, language code)
        self.audio_q = queue.Queue()
        self.whisper_q = queue.Queue()
        self.running = threading.Event()
        self.lang = lang                    # language code, or AUTO (Whisper tells, utterance by utterance)
        self.loaded = None                  # (language, quality) the loaded models are for
        self.vosk_model = None
        self.whisper = None
        self.whisper_pid = None
        self.context = ""
        self.utt_id = 0
        self.spk_model = None
        self.speakers = SpeakerTracker()
        self.utt_spk = {}                   # utterance id -> speaker id
        self.utt_lang = {}                  # utterance id -> language it was spoken in
        self.quality = QUALITY_FAST         # top-bar choice; "Cao" uses whisper_hq when it is loaded
        self.whisper_hq = None
        self.hq_lang = None
        self.fast_asr = None                # fast recognizer of the language (Vietnamese), if it has one
        self.catchup = None                 # small Whisper used while the main one is behind

    def set_language(self, lang):
        """New source language (UI thread): the models are swapped before listening starts again."""
        if lang != self.lang:
            self.lang = lang
            self.context = ""
            self.whisper_hq, self.hq_lang = None, None

    def load_model(self):
        """Load the models of the chosen language (recognizer thread). Raises FileNotFoundError(pack id)."""
        if not _PRELOAD["done"].is_set():
            self.ui_q.put(("status", "Đang chuẩn bị nhận diện giọng nói (vài giây)..."))
        _PRELOAD["done"].wait()
        self.spk_model = _PRELOAD["spk"]
        lang = self.lang
        if self.loaded != lang:
            self.ui_q.put(("status", f"Đang nạp mô hình nghe {lang_name(lang)}..."))
            fast = local_ai.FAST_ASR.get(lang)
            self.fast_asr = get_fast_asr(fast) if fast and STORE.have(fast) else None
            if self.fast_asr is not None:                 # Whisper only for "Cao"
                self.whisper, self.whisper_pid, self.vosk_model = None, None, None
            else:
                pid = whisper_pack(lang, QUALITY_FAST)
                if fast and not STORE.have(pid):
                    raise FileNotFoundError(fast)
                self.whisper = get_whisper(pid)
                self.whisper_pid = pid
                self.vosk_model = get_vosk(lang)
            self.catchup = (get_whisper("whisper-base") if self.whisper_pid == "whisper-small"
                            and STORE.have("whisper-base") else None)
            self.loaded = lang
        self.ui_q.put(("ready", None))

    def _mic_cb(self, indata, frames, t, status):
        self.audio_q.put(indata[:, 0].copy())

    def load_hq(self):
        """Load the high-quality Whisper model of the current language (worker thread). True when ready."""
        if self.whisper_hq is None or self.hq_lang != self.lang:
            lang = self.lang
            pid = whisper_pack(lang, QUALITY_HIGH)
            if not STORE.have(pid):
                return False
            self.whisper_hq = get_whisper(pid)
            self.hq_lang = lang
        return True

    def pick_model(self):
        """High quality when chosen and loaded, unless utterances are piling up (then the fast model catches
        up, so captions never fall far behind the speaker)."""
        if self.quality == QUALITY_HIGH and self.whisper_hq is not None and self.hq_lang == self.lang:
            free = free_memory_mb()
            if free is not None and free < HQ_KEEP_MB:        # the PC is running out of memory: give it back
                unload_whisper(whisper_pack(self.hq_lang, QUALITY_HIGH))
                self.whisper_hq = None
                self.quality = QUALITY_FAST
                import gc
                gc.collect()
                self.ui_q.put(("quality_off", f"Máy sắp hết bộ nhớ (còn {free:.0f} MB) — đã tắt chất lượng nghe "
                                              f"Cao, chuyển về Nhanh để app không bị dừng."))
                return self.whisper
            if self.whisper_q.qsize() < 2:
                return self.whisper_hq
        if self.catchup is not None and self.whisper_q.qsize() >= 2:     # behind: the smaller model catches up
            return self.catchup
        return self.whisper

    def transcribe(self, audio, model=None, lang=None):
        """(text, language code) of one utterance."""
        model = model or self.pick_model()
        lang = lang or self.lang
        peak = float(np.max(np.abs(audio))) or 1.0
        audio = audio * min(8.0, 0.6 / peak)          # auto-gain: quiet mics are boosted (max 8x)
        # base.en: beam 5 (noisy room 1.8% -> 0.6% word errors); the bigger models: beam 1 was as accurate
        # in tests, and faster
        beam = 5 if self.whisper_pid == WHISPER_MODEL and model is self.whisper else 1
        kw = dict(beam_size=beam, vad_filter=False, condition_on_previous_text=False, without_timestamps=True)
        if lang == AUTO:
            segs, info = model.transcribe(audio, language=None, **kw)
            segs = list(segs)
            got = info.language
            if got not in LANGS:                      # outside the app's languages: the likeliest one inside
                probs = [(c, p) for c, p in (info.all_language_probs or []) if c in LANGS]
                got = max(probs, key=lambda x: x[1])[0] if probs else "en"
                segs, _ = model.transcribe(audio, language=got, **kw)
            lang = got
        else:
            segs, _ = model.transcribe(audio, language=LANGS[lang][1],
                                       initial_prompt=self.context[-150:] or None, **kw)
        out = []
        for s in segs:
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            out.append(s.text.strip())
        sep = "" if lang in local_ai.NO_SPACES else " "
        return sep.join(out).strip(), lang

    def run(self):
        import sounddevice as sd
        threading.Thread(target=self._whisper_worker, daemon=True).start()
        first = True
        while True:
            if not first:
                self.running.wait()
            first = False
            try:
                self.load_model()
            except FileNotFoundError as e:
                self.ui_q.put(("need_packs", str(e)))
                self.running.clear()
                self.ui_q.put(("force_off", None))
                continue
            except Exception as e:  # noqa: BLE001
                self.ui_q.put(("error", f"Không tải được mô hình nhận diện giọng nói: {e}"))
                self.running.clear()
                self.ui_q.put(("force_off", None))
                continue
            self.running.wait()
            if self.loaded != self.lang:                 # language changed while waiting: load it first
                continue
            try:
                self._listen(sd)
            except Exception as e:  # noqa: BLE001
                self.ui_q.put(("error", f"Lỗi micro: {e}"))
                self.running.clear()
                self.ui_q.put(("force_off", None))

    def _listen(self, sd):
        import vosk
        lang = self.lang
        self.ui_q.put(("status", f"Đang nghe... ({lang_name(lang)})"))
        while not self.audio_q.empty():
            self.audio_q.get_nowait()
        rec = vosk.KaldiRecognizer(self.vosk_model, SAMPLE_RATE) if self.vosk_model is not None else None
        utt = []                       # audio of the current utterance
        utt_len = 0
        voiced = 0.0                   # how long this utterance was clearly louder than the room
        held = ""                      # words of a too-short piece, waiting to be joined with the rest
        last_partial = ""
        rms_hist = []                  # recent 30 ms loudness values -> background-noise floor
        cleaner = AudioCleaner()       # hum/rumble filter + level boost for quiet or far speakers (Whisper)
        booster = AudioCleaner(highpass=False, target=VOSK_QUIET_TARGET)   # for Vosk: only a QUIET voice is
        # raised (a far voice at 1/40 of a close one was silence to it); a normal voice reaches Vosk unchanged
        quiet = 0.0                    # how long it has been quiet (s)
        result = lambda r: local_ai.tidy(json.loads(r).get("text", ""), lang)
        slow = 0                       # how many times in a row Vosk was > 1.5 s behind the speaker
        next_partial = 0.0             # fast recognizer: when to re-read the utterance for live words
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=FRAME * 5, callback=self._mic_cb):
            while self.running.is_set() and self.lang == lang:
                try:
                    blocks = [self.audio_q.get(timeout=0.1)]
                except queue.Empty:
                    continue
                while not self.audio_q.empty():          # take everything waiting (catch up)
                    blocks.append(self.audio_q.get_nowait())
                block = np.concatenate(blocks)
                if rec is not None:
                    slow = slow + 1 if len(block) > 1.5 * SAMPLE_RATE else max(0, slow - 1)
                    if slow >= 3:                         # this PC is too slow for live words in this language
                        rec, held, last_partial = None, "", ""
                        self.ui_q.put(("status", f"Đang nghe... ({lang_name(lang)}) — máy xử lý chậm nên tắt chữ "
                                                 f"hiện ngay; câu hiện ra khi người nói ngừng."))
                # Whisper gets the cleaned audio (far speaker: 9.9% -> 5.8% word errors); Vosk keeps the raw
                # audio it was trained on (cleaning made its live words slightly worse in noise)
                utt.append((block, cleaner.process(block)))
                utt_len += len(block)

                # breath pauses: Vosk only ends an utterance after a long silence, so we also cut
                # at short pauses once the utterance is long enough -> accurate text comes sooner
                n = len(block) // FRAME
                rms = np.sqrt((block[: n * FRAME].reshape(n, FRAME) ** 2).mean(axis=1)) if n else np.zeros(0)
                rms_hist.extend(rms.tolist())
                del rms_hist[:-400]
                floor = float(np.percentile(rms_hist, 20)) if len(rms_hist) > 30 else 0.004
                for v in rms:
                    quiet = quiet + FRAME / SAMPLE_RATE if v < floor * 2.0 + QUIET_ADD else 0.0
                    if v > floor * 3.0 + QUIET_ADD * 0.5:
                        voiced += FRAME / SAMPLE_RATE
                secs = utt_len / SAMPLE_RATE

                if rec is None:
                    # no live-words model: the utterance ends at a pause, judged by loudness alone
                    if voiced < 0.15 and secs > 1.0:          # nothing said yet: keep only the last 0.5 s
                        keep, kept = [], 0
                        for u in reversed(utt):
                            keep.insert(0, u)
                            kept += len(u[0])
                            if kept >= SAMPLE_RATE // 2:
                                break
                        utt, utt_len, voiced = keep, kept, 0.0
                        continue
                    if ((voiced >= 0.4 and quiet >= 0.6 and secs >= UTT_MIN) or (secs >= UTT_SOFT and quiet >= 0.25)
                            or secs >= UTT_HARD):
                        self._end_utterance({"text": ""}, utt, voiced)
                        utt, utt_len, voiced, last_partial = [], 0, 0.0, ""
                        self.ui_q.put(("partial", ""))
                    elif voiced >= 0.3 and quiet < 0.3:
                        if self.fast_asr is not None and time.time() >= next_partial:
                            t0 = time.time()               # live words: re-read the utterance so far
                            words = self.fast_asr.decode(np.concatenate([u[1] for u in utt]))
                            next_partial = time.time() + max(0.5, 2 * (time.time() - t0))
                            if words and words != last_partial:
                                last_partial = words
                                self.ui_q.put(("partial", words))
                        elif self.fast_asr is None:
                            self.ui_q.put(("partial", "🎤 …"))
                    continue

                pcm16 = (booster.process(block) * 32767).astype(np.int16).tobytes()
                if rec.AcceptWaveform(pcm16):
                    words = (held + " " + result(rec.Result())).strip()
                    if secs < UTT_MIN and words:          # too short for a sentence: keep listening
                        held = words
                    else:
                        self._end_utterance({"text": words}, utt, voiced)
                        utt, utt_len, last_partial, voiced, held = [], 0, "", 0.0, ""
                # (no earlier cut for short sentences: speakers hesitate ~0.4-0.5 s mid-sentence, and cutting
                # there split sentences into fragments that were recognised and translated worse)
                elif (secs >= UTT_SOFT and quiet >= 0.2) or secs >= UTT_HARD or (held and quiet >= 0.6):
                    words = (held + " " + result(rec.FinalResult())).strip()
                    self._end_utterance({"text": words}, utt, voiced)
                    utt, utt_len, last_partial, voiced, held = [], 0, "", 0.0, ""
                else:
                    partial = (held + " " + local_ai.tidy(json.loads(rec.PartialResult()).get("partial", ""),
                                                          lang)).strip()
                    if partial != last_partial:
                        last_partial = partial
                        self.ui_q.put(("partial", partial))
            last = (held + " " + result(rec.FinalResult())).strip() if rec is not None else ""
            self._end_utterance({"text": last}, utt, voiced)
        self.whisper_q.put(None)                  # flush marker: stop -> translate the rest
        if not self.running.is_set():
            self.ui_q.put(("status", "Đã tắt."))

    def _end_utterance(self, result, utt, voiced=1.0):
        draft = local_ai.tidy(result.get("text", "").strip(), self.lang)
        if not utt:
            return
        raw = np.concatenate([u[0] for u in utt])       # as recorded: voiceprints, Whisper small.en
        audio = np.concatenate([u[1] for u in utt])     # cleaned: Whisper base.en
        if self.fast_asr is not None and len(audio) >= 0.4 * SAMPLE_RATE and voiced >= 0.3:
            try:
                draft = self.fast_asr.decode(audio)       # the whole utterance, accurate and instant
            except Exception:  # noqa: BLE001
                draft = ""
        if draft in ("", "the", "huh", "uh", "um"):
            # Vosk heard no words. A quiet or far voice (no microphone) can still be speech that Whisper hears
            # well: let it decide when the boosted audio has voice-loud parts; real silence is skipped.
            # (voiced: seconds clearly louder than the room's own noise, measured on the audio as recorded)
            if len(audio) < 0.6 * SAMPLE_RATE or voiced < 0.4:
                self.ui_q.put(("partial", ""))
                return
            draft = ""
        self.utt_id += 1
        self.utt_lang[self.utt_id] = self.lang
        vec = None
        if self.spk_model is not None and len(raw) > SAMPLE_RATE // 2:
            try:
                vec = self.spk_model(raw)
            except Exception:  # noqa: BLE001
                vec = None
        sid = self.speakers.assign(vec, len(audio) / SAMPLE_RATE)
        self.utt_spk[self.utt_id] = sid
        self.ui_q.put(("speaker", (self.utt_id, sid, dict(self.speakers.talk), dict(self.speakers.merged))))
        if draft:                                       # (no draft: the row appears when Whisper has text)
            self.ui_q.put(("draft", (self.utt_id, draft)))
        self.whisper_q.put((self.utt_id, draft, audio, sid, raw, self.lang))

    def _whisper_worker(self):
        while True:
            item = self.whisper_q.get()
            if item is None:
                self.ui_q.put(("flush", None))
                continue
            uid, draft, lang = item[0], item[1], item[5]
            model = self.pick_model()
            if self.fast_asr is not None and (model is None or model is not self.whisper_hq):
                if draft:                               # the fast recognizer's text is the final text
                    self.context = (self.context + " " + draft)[-400:]
                    self.on_final([uid], draft, lang)
                continue
            items = [item]
            # The multilingual Whisper models cost several seconds per call on a slow PC, whatever the length
            # (audio is padded to 30 s): when utterances are waiting, the next ones from the same speaker are
            # read in the same call and become one row. (English base.en keeps one row per utterance.)
            if model is not self.whisper or self.whisper_pid != WHISPER_MODEL:
                total = len(item[2])
                while not self.whisper_q.empty():
                    nxt = self.whisper_q.queue[0]
                    if nxt is None or nxt[3] != item[3] or nxt[5] != lang or total + len(nxt[2]) > 25 * SAMPLE_RATE:
                        break
                    items.append(self.whisper_q.get())
                    total += len(nxt[2])
            ids = [x[0] for x in items]
            gap = np.zeros(SAMPLE_RATE // 5, dtype=np.float32)
            # the cleaned audio helps base.en (far speaker 9.9% -> 5.8% word errors) but not small.en, which
            # did slightly better on the audio as recorded
            audio = np.concatenate([p for x in items for p in (x[4] if model is self.whisper_hq else x[2], gap)])
            draft = " ".join(x[1] for x in items if x[1]).strip()
            try:
                text, lang = self.transcribe(audio, model, lang)
            except Exception:  # noqa: BLE001
                text = ""
            text = self.choose_text(draft, text, len(audio) / SAMPLE_RATE, lang)
            if not text:                                # nothing heard after all (no draft, no Whisper text)
                continue
            for u in ids:
                self.utt_lang[u] = lang
            if not items[0][1]:                         # no live words: the row starts with Whisper's text
                self.ui_q.put(("draft", (ids[0], text)))
            self.context = (self.context + " " + text)[-400:]
            self.on_final(ids, text, lang)

    @staticmethod
    def choose_text(draft, text, secs, lang="en"):
        """The text a finished utterance shows: Whisper's, unless it is clearly invented (a known phrase, or more
        words than anyone says in that time); then Vosk's live draft. Before 1.9.20 a Whisper text much longer
        than the draft was thrown away - exactly what happens with a far speaker, where Vosk hears little."""
        cap = lambda s: s[:1].upper() + s[1:]
        d = draft.strip()
        clean = text.strip(" .,!?-'\"。、！？")
        n_w, n_d = local_ai.speech_units(text), local_ai.speech_units(d)
        if not clean:
            return cap(d)
        if HALLUCINATION.search(text) and not HALLUCINATION.search(d):
            return cap(d)
        if n_w > 4.5 * secs + 3:                        # faster than anyone speaks: invented
            return cap(d)
        if not d:                                       # only Whisper heard it: drop its usual noise guesses
            if n_w <= 2 and NOISE_GUESS.fullmatch(clean.lower()):
                return ""
            return text
        shared = len(set(local_ai.words(text)) & set(local_ai.words(d))) / max(1, len(set(local_ai.words(d))))
        if n_d <= 3 and n_w <= 4 and shared < 0.5:     # a short guess that disagrees with what Vosk heard
            return cap(d)
        return text


# =====================================================================
# Claude workers: translation + key takeaways
# =====================================================================
SENTENCE_GAP = re.compile(r"[.!?…](\s+)(?=\S)|[。！？](\s*)(?=\S)")   # the space after a sentence end
ABBREV = {"mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "no", "fig", "ông", "bà", "tp"}


def sentence_gaps(text):
    """Spaces that separate two sentences (next word capitalized, not after an abbreviation)."""
    for m in SENTENCE_GAP.finditer(text):
        if m.group(1) is None:                     # 。！？: always a sentence end
            yield _Gap(m.start(2), m.end(2))
            continue
        nxt = text[m.end(1):m.end(1) + 1]
        word = re.findall(r"[\w.]+$", text[:m.start(1) - 1])
        # (a letter without upper/lower case - Thai, Arabic, Hindi... - also starts a sentence)
        if ((nxt.isupper() or nxt.isdigit() or nxt in "\"'“(" or (nxt.isalpha() and nxt == nxt.lower() == nxt.upper()))
                and not (word and word[0].lower() in ABBREV)):
            yield _Gap(m.start(1), m.end(1))


class _Gap:
    """The whitespace between two sentences (start(1)/end(1) like a regex match)."""
    def __init__(self, a, b):
        self.a, self.b = a, b

    def start(self, _=1):
        return self.a

    def end(self, _=1):
        return self.b


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
    if isinstance(e, anthropic.APITimeoutError) or "timed out" in s:
        return "Claude phản hồi quá lâu nên hết thời gian chờ. Hãy bấm gửi lại."
    if isinstance(e, anthropic.APIConnectionError):
        return "Mất kết nối tới Claude. Kiểm tra mạng."
    return f"Lỗi Claude: {str(e)[:160]}"


class Translator:
    """Translates each spoken segment on this PC (NLLB-200) as soon as the speaker pauses.

    * The fast Vosk draft of a segment is translated immediately (no waiting for Whisper). When Whisper's
      accurate text arrives, the segment is re-translated only if the words differ noticeably.
    * Each segment has its own slot in the translation pane, so the text always appears in speaking order.
    * Source language per segment (it can change with "Tự nhận diện"); the target is the top-bar choice.
    """

    def __init__(self, ui_q, src="en", tgt="vi"):
        from concurrent.futures import ThreadPoolExecutor
        self.ui_q = ui_q
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.lock = threading.Lock()
        self.en = {}        # uid -> source text currently translated for that slot ("en": kept for old backups)
        self.vi = {}        # uid -> finished translation
        self.gen = {}       # uid -> generation (a re-translation makes older output stale)
        self.lang = {}      # uid -> source language of that segment
        self.src, self.tgt = src, tgt
        self.mt = None
        self.mt_error = None
        self._loading = threading.Lock()

    def set_languages(self, src, tgt):
        self.src, self.tgt = src, tgt

    @property
    def history(self):
        """[(source, translation)] in speaking order (used as context for chat)."""
        with self.lock:
            return [(self.en[u], self.vi[u]) for u in sorted(self.vi) if u in self.en]

    def warm(self):
        """Load the translation model in the background, so the first sentence is not slower."""
        self.pool.submit(self._model)

    def _model(self):
        with self._loading:
            if self.mt is None:
                path = STORE.path("nllb-600m")
                if not path:
                    raise FileNotFoundError("nllb-600m")
                self.mt = local_ai.NLLB(path, threads=local_ai.free_threads() + 1)
                self.mt.translate("Hello.", "en", "vi")          # warm-up
            return self.mt

    # called on the UI thread
    def draft(self, uid, text, lang):
        with self.lock:
            self.en[uid] = text
            self.gen[uid] = 1
            self.lang[uid] = lang
        self.ui_q.put(("vi_slot", uid))
        self.pool.submit(self._translate, uid, 1, text)

    def final(self, ids, text, lang):
        """Whisper's accurate text for one or more segments."""
        with self.lock:
            drafts = " ".join(self.en.get(u, "") for u in ids)
            # re-translate whenever Whisper's text differs: the local model translates much better with
            # punctuation and capitals (the live draft has neither), and it costs no credit
            same = self.lang.get(ids[0]) == lang and drafts.split() == text.split()
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
            self.lang[first] = lang
            g = self.gen[first]
        # the draft's translation stays on screen until the corrected one is ready, then it is swapped in
        self.pool.submit(self._translate, first, g, text, False)

    def _translate(self, uid, g, text, stream=True):
        with self.lock:
            if self.gen.get(uid) != g:                 # a newer text of this row is already waiting
                return
            src = self.lang.get(uid, self.src)
        tgt = self.tgt
        out = ""
        try:
            if src == AUTO:                            # (only before Whisper said which language it is)
                out = text
            elif src == tgt:
                out = text                             # same language: nothing to translate
            elif stream:                               # words appear as they are decoded (like live captions)
                out = self._model().stream(text, src, tgt, lambda p: self.ui_q.put(("vi_piece", (uid, g, p))))
                out = out or "—"
            else:
                out = self._model().translate(text, src, tgt)
            self.mt_error = None
            if src in (AUTO, tgt):
                self.ui_q.put(("vi_piece" if stream else "vi_replace", (uid, g, out.strip() or "—")))
            elif not stream:
                self.ui_q.put(("vi_replace", (uid, g, out.strip() or "—")))
            elif out == "—":
                self.ui_q.put(("vi_piece", (uid, g, out)))
        except FileNotFoundError:
            self.ui_q.put(("vi_piece" if stream else "vi_replace", (uid, g, "[chưa có gói Dịch offline]")))
            if not self.mt_error:
                self.mt_error = "missing"
                self.ui_q.put(("need_packs", "nllb-600m"))
        except Exception as e:  # noqa: BLE001
            self.ui_q.put(("vi_piece" if stream else "vi_replace", (uid, g, "[chưa dịch được]")))
            self.ui_q.put(("error", f"Lỗi mô hình dịch: {str(e)[:160]}"))
        with self.lock:
            if self.gen.get(uid) == g:
                self.vi[uid] = out.strip()


BACKGROUND_HEAD = "Bối cảnh (phiên nghe):\n"   # starts the chat's cached background block


class Chatter(threading.Thread):
    """Answers the user's questions about the session (multi-turn), streaming into the chat pane. The only part
    of the app that uses the internet (Claude)."""
    SYSTEM = ("You are Claude, helping a user who is listening to a live talk or conversation and reading it "
              "translated by the app. The app gives you the speech-recognition transcript so far in the language "
              "spoken (it may contain recognition errors), the end of its translation, and the current key "
              "takeaways (both written on the user's PC by small offline models, so they may be rough). Answer "
              "the user's questions about it, or any other question they ask. Reply in the language the user "
              "writes in (by default the translation language). Be clear and concise; plain text, short bullet "
              "points when helpful (no Markdown headings or tables). If the transcript doesn't contain the "
              "answer, say so and answer from general knowledge if appropriate. "
              "You have a tool: read_class_record reads the whole session record (original transcript, "
              "translation, all key takeaways), optionally only the parts that mention a word - use it when the "
              "question is about something earlier than the recent part you were given.")
    TOOLS = [
        {"name": "read_class_record",
         "description": "The session record kept by the app: part = 'english' (original transcript with speakers, "
                        "in the language spoken), 'vietnamese' (translation), 'both' (original and translation "
                        "line by line) or 'takeaways' (all key takeaways with times). Optional search: only the "
                        "lines that mention it (with a little context). Without search: the most recent part (up "
                        "to max_chars).",
         "input_schema": {"type": "object", "properties": {
             "part": {"type": "string", "enum": ["english", "vietnamese", "both", "takeaways"]},
             "search": {"type": "string"}, "max_chars": {"type": "integer"}}, "required": ["part"]}},
    ]

    def __init__(self, client, ui_q, get_context):
        super().__init__(daemon=True)
        self.client = None
        self.set_client(client)
        self.ui_q = ui_q
        self.get_context = get_context
        self.q = queue.Queue()
        self.history = []          # past turns: [{"role": ..., "content": ...}, ...]
        self.tool_handler = None   # (name, input) -> text, set by the main window (the session record)

    def set_client(self, client):
        """Signed in (or out: None). Questions may carry documents: give them time."""
        self.client = client.with_options(timeout=LONG_TIMEOUT, max_retries=2) if client else None

    @staticmethod
    def doc_key(b):
        """Identity of an attached document/image block (None for normal text)."""
        t = b.get("type")
        if t in ("document", "image"):
            src = b.get("source") or {}
            if src.get("file_id"):
                return "file:" + src["file_id"]
            data = src.get("data") or src.get("url") or ""
            return "data:" + hashlib.sha1(str(data).encode("utf-8", "ignore")).hexdigest()
        if t == "text" and b.get("text", "").startswith("<file "):
            return "text:" + hashlib.sha1(b["text"].encode("utf-8", "ignore")).hexdigest()
        return None

    @classmethod
    def without_repeats(cls, messages):
        """Each document is sent once per request: a document already attached earlier in the conversation
        (e.g. the exercise file at every step) is replaced by a one-line note instead of being sent again."""
        seen, out = set(), []
        for m in messages:
            if m["role"] != "user" or not isinstance(m["content"], list):
                out.append(m)
                continue
            content = []
            for b in m["content"]:
                k = cls.doc_key(b)
                if k and k in seen:
                    name = b.get("title") or (re.match(r'<file name="([^"]*)"', b.get("text", "")) or [None, ""])[1]
                    content.append({"type": "text", "text": f"(tài liệu {name or 'đính kèm'} đã gửi ở trên)"})
                    continue
                if k:
                    seen.add(k)
                keep = b.get("type") == "text" and b.get("text", "").startswith(BACKGROUND_HEAD)
                # old cache points go (only the newest question gets one), but the background block keeps its
                # own: it is the big part that stays the same from question to question
                content.append(b if keep else {key: v for key, v in b.items() if key != "cache_control"})
            out.append({**m, "content": content})
        return out

    @staticmethod
    def _with_cache(messages):
        """Cache point at the end of the newest question: the next question (e.g. the next exercise step)
        then reads the documents and the conversation so far from Claude's cache, at a tenth of the price,
        instead of paying for them again."""
        out = list(messages)
        m = out[-1]
        if m["role"] == "user":
            content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            content = list(content)
            content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
            out[-1] = {**m, "content": content}
        return out

    def _recent(self, n=20):
        """The last turns of the conversation, starting at a question (never in the middle of a tool call)."""
        h = self.history
        start = max(0, len(h) - n)
        while start < len(h):
            m = h[start]
            c = m["content"]
            if m["role"] == "user" and not (isinstance(c, list) and c and c[0].get("type") == "tool_result"):
                break
            start += 1
        return h[start:]

    def _stream(self, convo, use_tools, on_piece):
        """One request (streamed into the pane). Returns (final message or None, text)."""
        for use_fallback in (True, False):
            extra = {"output_config": {"effort": "medium"}}
            headers = None
            if use_fallback:
                extra["fallbacks"] = "default"
                headers = FALLBACK_HEADERS
            text = ""
            kw = dict(model=MODEL, max_tokens=16000, system=self.SYSTEM,
                      messages=self._with_cache(self.without_repeats(convo)), extra_body=extra,
                      extra_headers=headers)
            if use_tools:
                kw["tools"] = self.TOOLS
            try:
                with self.client.messages.stream(**kw) as st:
                    for piece in st.text_stream:
                        text += piece
                        on_piece(piece)
                    final = st.get_final_message() if hasattr(st, "get_final_message") else None
                return final, text
            except anthropic.BadRequestError as e:
                if not use_fallback or text or "fallback" not in str(e).lower():
                    raise
        return None, ""

    def _answer(self, messages, use_tools):
        """Ask, and when Claude wants to read library documents or the class record, give them and continue.
        Returns (final answer text, the tool messages added to the conversation)."""
        convo, added = list(messages), []
        on_piece = lambda p: self.ui_q.put(("chat_piece", p))
        for _ in range(5):
            final, text = self._stream(convo, use_tools, on_piece)
            uses = [b for b in (getattr(final, "content", None) or []) if getattr(b, "type", "") == "tool_use"]
            if not uses or getattr(final, "stop_reason", "") != "tool_use":
                return text, added
            asst = {"role": "assistant", "content": [
                {"type": "text", "text": b.text} if b.type == "text" else
                {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                for b in final.content if (b.type == "text" and b.text) or b.type == "tool_use"]}
            results = []
            for u in uses:
                what = (f"bản ghi phiên nghe ({u.input.get('part', '')}"
                        f"{', tìm “' + u.input['search'] + '”' if u.input.get('search') else ''})")
                self.ui_q.put(("status", f"📖 Claude đang đọc {what}..."))
                try:
                    out = self.tool_handler(u.name, u.input) or "(trống)"
                except Exception as e:  # noqa: BLE001
                    out = f"Không đọc được: {e}"
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": [{"type": "text", "text": out}]})
            if text:
                on_piece("\n")
            step = [asst, {"role": "user", "content": results}]
            convo += step
            added += step
        return text, added

    def run(self):
        while True:
            item = self.q.get()
            question, files = item[0], item[1]     # files: content blocks (images, PDFs, text)
            with_session = item[2] if len(item) > 2 else True
            # The background (the session transcript) goes FIRST, as its own message: while it does not change
            # (after the talk) Claude reads it from its cache at a tenth of the price instead of paying for it
            # again on every question.
            context, live = "", False
            if with_session:
                en, vi, summary, live, langs = self.get_context()
                context = (f"<languages>{langs}</languages>\n"
                           f"<transcript_original>\n{en or '(chưa có)'}\n</transcript_original>\n"
                           f"<translation_end>\n{vi or '(chưa có)'}\n</translation_end>\n"
                           f"<key_takeaways>\n{summary or '(chưa có)'}\n</key_takeaways>")
            background = []
            if context:
                block = {"type": "text", "text": BACKGROUND_HEAD + context}
                if not live:                       # while listening it changes every few seconds: no cache
                    block["cache_control"] = {"type": "ephemeral"}
                background = [{"role": "user", "content": [block]},
                              {"role": "assistant", "content": "Đã nắm bối cảnh."}]
            # Files go before the text, and stay in the history so follow-up questions can refer to them.
            new_q = {"role": "user", "content": files + [{"type": "text", "text": f"Câu hỏi của tôi: {question}"}]}
            messages = background + self._recent() + [new_q]
            answer, added = "", []
            try:
                answer, added = self._answer(messages, use_tools=with_session and self.tool_handler is not None)
            except Exception as e:  # noqa: BLE001
                self.ui_q.put(("chat_error", f"[{friendly_error(e)}]"))
            if answer.strip():
                # kept exactly as sent (with any documents Claude read), so the next question starts with the
                # same text and reads all of it from the cache
                self.history += [new_q] + added + [{"role": "assistant", "content": answer.strip()}]
            self.ui_q.put(("chat_end", None))


class Summarizer(threading.Thread):
    """Running key takeaways, written on this PC by a small instruction model (Qwen2.5). Every round only the
    speech that is new since the last round is turned into new bullet points, which are ADDED to the pane
    (earlier takeaways are never removed). The model reads the TRANSLATION (in the language the notes are
    written in): a small model writes much better notes in the language it reads."""

    def __init__(self, ui_q, get_transcript, get_takeaways):
        super().__init__(daemon=True)
        self.ui_q = ui_q
        self.get_transcript = get_transcript      # (start, end) -> (end, text in the notes' language)
        self.get_takeaways = get_takeaways
        self.kick = threading.Event()
        self.done_n = 0                           # transcript entries already covered
        self.final = False                        # set when listening stops: cover even a short remainder
        self.pack = local_ai.LLM_PACKS[0]         # which model (⚙ menu)
        self.lang = "vi"                          # language of the notes
        self.llm, self.llm_pack = None, None
        self.warned = False

    def _model(self):
        if self.llm is None or self.llm_pack != self.pack:
            path = STORE.path(self.pack)
            if not path:
                raise FileNotFoundError(self.pack)
            self.llm = None
            free = free_memory_mb()
            need = 2200 if "1.5b" in self.pack else 4000
            if free is not None and free < need:
                raise MemoryError(f"máy còn {free:.0f} MB bộ nhớ trống, cần khoảng {need} MB")
            self.ui_q.put(("busy", ("takeaways", "Đang nạp mô hình Key takeaways...")))
            try:
                self.llm = local_ai.LocalLLM(path, threads=local_ai.free_threads())
            finally:
                self.ui_q.put(("busy", ("takeaways", None)))
            self.llm_pack = self.pack
        return self.llm

    def run(self):
        while True:
            self.kick.wait(timeout=SUMMARY_EVERY)
            self.kick.clear()
            final, self.final = self.final, False
            try:
                n, new = self.get_transcript(self.done_n, None)
                if local_ai.speech_units(new) < (4 if final else 15):
                    continue
                llm = self._model()
                lang = local_ai.LANG_NAMES_EN.get(self.lang, "English")
                prompt = (f"Existing takeaways:\n{(self.get_takeaways() or '')[-1500:] or '(none yet)'}\n\n"
                          f"NEW part of the transcript:\n{new[-6000:]}\n\n"
                          f"Write the new key takeaways in {lang}.")
                self.ui_q.put(("busy", ("takeaways", "Đang viết Key takeaways trên máy...")))
                try:
                    out = llm.chat(local_ai.TAKEAWAY_SYSTEM.format(lang=lang), prompt, max_new=350)
                finally:
                    self.ui_q.put(("busy", ("takeaways", None)))
                self.done_n = n
                bullets = local_ai.clean_bullets(out)
                if bullets:
                    self.ui_q.put(("summary", bullets))
                self.warned = False
            except FileNotFoundError:
                if not self.warned:
                    self.warned = True
                    self.ui_q.put(("error", "Chưa có gói Key takeaways offline — bấm 📦 Gói ngôn ngữ để tải "
                                            "(một lần, sau đó chạy không cần internet)."))
            except Exception as e:  # noqa: BLE001  (done_n unchanged -> try again next round)
                if not self.warned:
                    self.warned = True
                    self.ui_q.put(("error", f"Key takeaways: {str(e)[:160]}"))


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
        self._dl_queue, self._dl_then, self._dl_current = [], [], None    # language packs being downloaded
        self._dl_running, self._dl_cancel, self._dl_progress = False, threading.Event(), 0
        self._start_after_packs = False

        self.root_title = f"Live Translator v{APP_VERSION} — đa ngôn ngữ, offline"
        root.title(self.root_title)
        root.resizable(True, True)
        w, h = 1280, 800
        root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{max(0, (root.winfo_screenheight() - h) // 2 - 30)}")
        root.minsize(700, 450)

        text_font = tkfont.Font(family="Segoe UI", size=13)
        head_font = ("Segoe UI", 10, "bold")

        # top bar
        top = tk.Frame(root, bg="#2b2b2b", height=40)
        top.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.status = tk.Label(top, text="Nhấn ON để bắt đầu.", bg="#2b2b2b", fg="#ddd", font=("Segoe UI", 10))
        self.status.pack(side="left", padx=12, pady=6)
        # "working on it" line: shown while something takes time (restore, Claude reading files...)
        self._busy, self._busy_tick = {}, 0
        self.busy_lbl = tk.Label(top, text="", bg="#2b2b2b", fg="#ffd479", font=("Segoe UI", 10))
        root.after(120, self._busy_spin)
        self.toggle = tk.Button(top, text="OFF", width=7, font=("Segoe UI", 10, "bold"), relief="flat",
                                bg="#777", fg="white", activebackground="#555", command=self.toggle_onoff)
        self.toggle.pack(side="right", padx=10, pady=5)
        cfg = load_config()
        menu_style = dict(font=("Segoe UI", 9), bg="#3a3a3a", fg="#ddd", activebackground="#4a4a4a",
                          activeforeground="white", relief="flat", highlightthickness=0, bd=0)
        # ⚙ menu: language packs, takeaways model, Output Folder, Claude login
        self.gear = tk.Menubutton(top, text="⚙", font=("Segoe UI Emoji", 11), relief="flat", bg="#2b2b2b",
                                  fg="#ddd", activebackground="#444", activeforeground="white", cursor="hand2")
        self.gear_menu = tk.Menu(self.gear, tearoff=0, font=("Segoe UI", 10), postcommand=self._fill_gear_menu)
        self.gear["menu"] = self.gear_menu
        self.gear.pack(side="right", pady=5)
        tk.Button(top, text="📦 Gói ngôn ngữ", command=self.open_packs, font=("Segoe UI", 9), relief="flat",
                  bg="#3a3a3a", fg="#ddd", activebackground="#4a4a4a", activeforeground="white", cursor="hand2",
                  padx=8).pack(side="right", padx=(0, 6), pady=5)
        # languages: what is spoken -> what to translate into (remembered); every one runs on this PC
        self.src_lang = cfg.get("src_lang", "en") if cfg.get("src_lang", "en") in LANGS or             cfg.get("src_lang") == AUTO else "en"
        self.tgt_lang = cfg.get("tgt_lang", "vi") if cfg.get("tgt_lang", "vi") in LANGS else "vi"
        self.tgt_var = tk.StringVar(value=lang_name(self.tgt_lang))
        tm = tk.OptionMenu(top, self.tgt_var, *[lang_name(c) for c in LANGS],
                           command=lambda name: self.set_languages(tgt=self._code(name)))
        tm.config(**menu_style)
        tm["menu"].config(font=("Segoe UI", 9))
        tm.pack(side="right", padx=(0, 10), pady=5)
        tk.Label(top, text="→ Dịch sang:", bg="#2b2b2b", fg="#aaa", font=("Segoe UI", 9)).pack(side="right")
        self.src_var = tk.StringVar(value=lang_name(self.src_lang))
        sm = tk.OptionMenu(top, self.src_var, *[lang_name(c) for c in [AUTO] + list(LANGS)],
                           command=lambda name: self.set_languages(src=self._code(name)))
        sm.config(**menu_style)
        sm["menu"].config(font=("Segoe UI", 9))
        sm.pack(side="right", padx=(0, 6), pady=5)
        tk.Label(top, text="Nghe:", bg="#2b2b2b", fg="#aaa", font=("Segoe UI", 9)).pack(side="right")
        # listening quality: Nhanh = Whisper base.en / small, Cao = small.en / medium (downloaded once, slower)
        self.quality_var = tk.StringVar(value=load_config().get("listen_quality", QUALITY_FAST))
        if self.quality_var.get() not in (QUALITY_FAST, QUALITY_HIGH):
            self.quality_var.set(QUALITY_FAST)
        qm = tk.OptionMenu(top, self.quality_var, QUALITY_FAST, QUALITY_HIGH, command=self._set_quality)
        qm.config(font=("Segoe UI", 9), bg="#3a3a3a", fg="#ddd", activebackground="#4a4a4a",
                  activeforeground="white", relief="flat", highlightthickness=0, bd=0)
        qm["menu"].config(font=("Segoe UI", 9))
        qm.pack(side="right", padx=(0, 10), pady=5)
        tk.Label(top, text="Chất lượng:", bg="#2b2b2b", fg="#aaa", font=("Segoe UI", 9)).pack(side="right")
        bstyle = dict(font=("Segoe UI", 9), relief="flat", bg="#3a3a3a", fg="#ddd", activebackground="#4a4a4a",
                      activeforeground="white", cursor="hand2", padx=8)
        tk.Button(top, text="📂 Restore", command=self.restore_session, **bstyle).pack(side="right", padx=(0, 12), pady=5)
        tk.Button(top, text="💾 Backup", command=self.backup_session, **bstyle).pack(side="right", padx=(0, 4), pady=5)
        tk.Button(top, text="🆕 Phiên mới", command=self.new_session, **bstyle).pack(side="right", padx=(0, 4), pady=5)

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
            head = tk.Label(f, text=title, font=head_font, bg="#f0f0f0", anchor="w", padx=8)
            head.pack(fill="x")
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
            t.head = head
            return t

        # top: spoken language | translation, same size, one shared scrollbar (they scroll together)
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
        tk.Button(self.bottom.master, text="📄 Xuất PDF", font=("Segoe UI", 8), relief="flat", bd=0, padx=6,
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
        self.bottom.tag_configure("ttime", foreground="#999", font=("Segoe UI", 9))
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
        self.send_btn = tk.Button(row, text="Gửi ➤", font=("Segoe UI", 10, "bold"), bg="#d97757", fg="white",
                                  activebackground="#c4643f", relief="flat", cursor="hand2", command=self.send_prompt)
        self.send_btn.pack(side="right", fill="y", padx=6, pady=6)
        self.attach_btn = tk.Button(row, text="📎", font=("Segoe UI Emoji", 13), bg="white", relief="flat",
                                    activebackground="#eee", cursor="hand2", command=self.attach_files)
        self.attach_btn.pack(side="right", fill="y", pady=6)
        # Native Windows text box: works with every Vietnamese input method (Tk's Text does not)
        host = tk.Frame(row, bg="white", height=60)
        host.pack(side="left", fill="both", expand=True)
        try:
            from native_edit import NativeEdit
            self.prompt = NativeEdit(host, self.send_prompt)
        except Exception:  # noqa: BLE001  (not Windows, or the control could not be created)
            host.destroy()
            self.prompt = tk.Text(row, wrap="word", font=("Segoe UI", 12), bd=0, padx=10, pady=6, height=3)
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
        btn = dict(font=("Segoe UI", 9, "bold"), relief="flat", bd=0, fg="white",
                   activeforeground="white", cursor="hand2")
        self.panel_btns = {
            "left": tk.Button(strip_l, command=lambda: self.toggle_panel("left"), **btn),
            "right": tk.Button(strip, command=lambda: self.toggle_panel("right"), **btn),
        }
        for b in self.panel_btns.values():
            b.place(relx=0.5, rely=0.5, anchor="center", width=26, height=230)
        root.bind_all("<Control-k>", lambda e: self.toggle_panel("right"))
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

        self.recognizer = Recognizer(self.ui_q, self._on_english, self.src_lang)
        self.translator = Translator(self.ui_q, self.src_lang, self.tgt_lang)
        if self.src_lang != self.tgt_lang and STORE.have("nllb-600m"):
            self.translator.warm()
        self.summarizer = Summarizer(self.ui_q, self.transcript_range, lambda: self.last_summary)
        self.summarizer.lang = self.tgt_lang
        self.summarizer.pack = cfg.get("takeaways_pack") if cfg.get("takeaways_pack") in local_ai.LLM_PACKS else             local_ai.LLM_PACKS[0]
        self.chatter = Chatter(client, self.ui_q, self._chat_context)
        self.recognizer.start()
        self.summarizer.start()
        self.chatter.tool_handler = self._chat_tool              # Claude reads the session record
        self._set_pane_titles()
        if self.quality_var.get() == QUALITY_HIGH:
            self._set_quality(QUALITY_HIGH, startup=True)
        self.chatter.start()
        self.root.after(50, self._poll)
        # the session survives closing the app: reopened now, saved every minute and when the window closes
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(400, self._reopen_last)
        self.root.after(60_000, self._autosave_tick)
        self.root.after(300, self._auto_login)            # Claude panel only; everything else works offline
        self.root.after(1500, self._first_run_packs)

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
        if self.follow and not getattr(self, "_restoring", False):
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
        if not self.attachments:
            self.attach_bar.pack_forget()
            return
        for i, (name, _) in enumerate(self.attachments):
            chip = tk.Frame(self.attach_bar, bg="#f3e3dc")
            chip.pack(side="left", padx=(6, 0), pady=(4, 0))
            short = name if len(name) <= 28 else name[:25] + "..."
            tk.Label(chip, text="📎 " + short, bg="#f3e3dc", font=("Segoe UI", 9)).pack(side="left", padx=(6, 2))
            tk.Button(chip, text="✕", bg="#f3e3dc", relief="flat", bd=0, font=("Segoe UI", 8), cursor="hand2",
                      command=lambda i=i: (self.attachments.pop(i), self._refresh_attach_bar())).pack(side="left",
                                                                                                padx=(0, 4))
        self.attach_bar.pack(fill="x", before=self.prompt_row)

    def send_prompt(self):
        q = self.prompt.get("1.0", "end").strip()
        files = self.attachments
        if (not q and not files) or self.chat_busy:
            return
        if self.chatter.client is None:              # Claude needs a login (and internet): ask for it now
            self.login_claude(then=self.send_prompt)
            return
        if not q:
            q = "Hãy xem (các) file đính kèm và tóm tắt nội dung chính."
        self._chat_failed = False
        self.prompt.delete("1.0", "end")
        self.attachments = []
        self._refresh_attach_bar()
        self.chat_busy = True
        self.busy("chat", "Claude đang trả lời...")                  # cleared when the answer ends
        self.send_btn.config(state="disabled", text="...")
        w = self.chat
        w.configure(state="normal")
        if w.index("end-1c") != "1.0":
            w.insert("end", "\n\n")
        w.insert("end", "Bạn: ", "you")
        w.insert("end", q + "\n")
        if files:
            w.insert("end", "📎 " + ", ".join(n for n, _ in files) + "\n", "file")
        w.insert("end", "Claude: ", "who")
        w.see("end")
        w.configure(state="disabled")
        self.chat_log.append({"q": q, "files": [n for n, _ in files], "a": ""})
        self.chatter.q.put((q, [b for _, b in files], True))

    # ---- Claude login: only the Claude panel needs it (asked the first time a question is sent) ----
    def _auto_login(self):
        """Remembered login: checked quietly in the background (no internet -> the panel asks later)."""
        cfg = load_config()
        if not cfg.get("remember") or not cfg.get("method"):
            return

        def work():
            try:
                if cfg["method"] == "key" and cfg.get("api_key"):
                    c = anthropic.Anthropic(api_key=cfg["api_key"], max_retries=1, timeout=20.0)
                else:
                    c = profile_client()
                c.models.list(limit=1)
                self.ui_q.put(("call", lambda: self._logged_in(c, quiet=True)))
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=work, daemon=True).start()

    def _logged_in(self, client, quiet=False):
        self.client = client
        self.chatter.set_client(client)
        if not quiet:
            self.status.config(text="✓ Đã đăng nhập Claude.", fg="#7ee2a8")

    def login_claude(self, then=None):
        """The login window (Claude account or API key), over the main window."""
        if getattr(self, "_login_win", None) and self._login_win.winfo_exists():
            self._login_win.lift()
            return
        top = self._login_win = tk.Toplevel(self.root)
        top.transient(self.root)
        top.configure(bg="#faf9f5")

        def ok(client):
            top.destroy()
            self._logged_in(client)
            if then:
                self.root.after(50, then)
        LoginWindow(top, ok).try_auto_login()
        top.grab_set()

    def logout(self):
        """Forget the saved Claude login (the rest of the app keeps working offline)."""
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
        self.client = None
        self.chatter.set_client(None)
        self.status.config(text="Đã đăng xuất Claude. Nghe, dịch và Key takeaways vẫn chạy trên máy.", fg="#ddd")

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
            "src_lang": self.src_lang, "tgt_lang": self.tgt_lang,
            "rows": rows,
            "speakers": {"custom": self.spk_custom, "teachers": self.teachers, "talk": self.spk_talk,
                         "alias": self.spk_alias, "voices": {str(k): v.tolist() for k, v in sp.sums.items()},
                         "next_id": sp.next_id},
            "transcript": self.transcript,
            "takeaways": self.last_summary,
            "chat": self.chat_log,
            "chat_history": [m if isinstance(m["content"], str) else
                             {"role": m["role"], "content": " ".join(self._saved_block(b) for b in m["content"])}
                             for m in self.chatter.history],
        }

    def backup_session(self):
        from tkinter import filedialog, messagebox
        if self.restoring():
            return
        default = f"LiveTranslator_backup_{time.strftime('%Y%m%d_%H%M')}.json"
        path = filedialog.asksaveasfilename(parent=self.root, title="Lưu backup", defaultextension=".json",
                                            initialfile=default, initialdir=output_dir("Backup"),
                                            filetypes=[("Live Translator backup", "*.json")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._session_data(), f, ensure_ascii=False, indent=1)
            self.status.config(text=f"✓ Đã backup: {os.path.basename(path)}", fg="#7ee2a8")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Backup lỗi", str(e), parent=self.root)

    # ---- the session survives closing the app: saved every minute and on close, reopened at the next start ----
    def _autosave(self, force=False):
        """Save the session on screen to DATA_DIR (only when something changed). Never while a restore is
        still filling the panes (that would save half a session)."""
        if self.restoring():
            return
        sig = (len(self.uid_spk), len(self.last_summary), len(self.chat_log),
               sum(len(v or "") for v in self.translator.vi.values()),
               len(self.chat_log[-1].get("a", "")) if self.chat_log else 0)
        if not force and sig == getattr(self, "_autosave_sig", None):
            return
        path = os.path.join(DATA_DIR, AUTOSAVE_FILE)
        try:
            data = self._session_data()
            if not (data["rows"] or data["takeaways"] or data["chat"]):
                if os.path.exists(path):
                    os.remove(path)                   # an empty session: nothing to reopen
            else:
                with open(path + ".part", "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(path + ".part", path)
            self._autosave_sig = sig
        except Exception:  # noqa: BLE001
            pass

    def _autosave_tick(self):
        self._autosave()
        self.root.after(60_000, self._autosave_tick)

    def _on_close(self):
        if self.on:
            self.toggle_onoff()
        self._autosave(force=True)
        self.root.destroy()

    def _reopen_last(self):
        """At start: bring back the session that was on screen when the app was closed (its translations,
        takeaways and answers are not asked from Claude again)."""
        path = os.path.join(DATA_DIR, AUTOSAVE_FILE)
        try:
            data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else None
        except Exception:  # noqa: BLE001
            data = None
        if not data or data.get("app") != "LiveTranslator" or not (data.get("rows") or data.get("takeaways")
                                                                    or data.get("chat")):
            return

        def done(problems):
            self._autosave_sig = None
            self.status.config(text=f"↺ Đã mở lại phiên trước ({len(data.get('rows', []))} dòng, lưu lúc "
                                    f"{data.get('saved', '?')}). Bấm 🆕 Phiên mới để bắt đầu buổi khác.",
                               fg="#7ee2a8" if not problems else "#ffd479")
        self.load_session(data, done)

    def new_session(self):
        """Start an empty session; the current one is kept as a backup file first."""
        from tkinter import messagebox
        if self.restoring():
            return
        data = self._session_data()
        if data["rows"] or data["takeaways"] or data["chat"]:
            if not messagebox.askyesno("Phiên mới", "Lưu phiên hiện tại vào Backup rồi bắt đầu một phiên trống?",
                                       parent=self.root):
                return
            folder = output_dir("Backup") or os.path.join(DATA_DIR, "backups")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"LiveTranslator_backup_{time.strftime('%Y%m%d_%H%M%S')}.json")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("Không lưu được backup", str(e), parent=self.root)
                return
        if self.on:
            self.toggle_onoff()
        self.load_session({"app": "LiveTranslator", "rows": [], "transcript": [], "takeaways": "", "chat": [],
                           "chat_history": [], "speakers": {}},
                          lambda p: (self._autosave(force=True),
                                     self.status.config(text="🆕 Phiên mới — nhấn ON để bắt đầu.", fg="#ddd")))

    def restoring(self):
        """True while a restore is still putting rows in or lining them up (tells the user to wait)."""
        if getattr(self, "_restoring", False) or getattr(self, "_align_job", None):
            self.status.config(text="⏳ Đang khôi phục bản ghi — chờ chút cho xong rồi thử lại.", fg="#ffd479")
            return True
        return False

    def restore_session(self):
        from tkinter import filedialog, messagebox
        if self.restoring():
            return
        path = filedialog.askopenfilename(parent=self.root, title="Mở file backup", initialdir=output_dir("Backup"),
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
        t0 = time.time()

        def finished(problems):
            n_take = len(re.findall(r"^\[\d\d:\d\d\]$", self.last_summary, re.M)) or \
                (1 if self.last_summary.strip() else 0)
            self.status.config(text=f"✓ Đã restore: {os.path.basename(path)} ({len(data.get('rows', []))} dòng, "
                                    f"{n_take} mốc Key takeaways, {time.time() - t0:.0f}s)",
                               fg="#7ee2a8" if not problems else "#ffd479")
            if problems:
                messagebox.showwarning("Restore chưa đầy đủ", "Một số phần chưa khôi phục được (các phần khác vẫn "
                                       "đã được khôi phục):\n\n" + "\n".join("• " + p for p in problems),
                                       parent=self.root)
        self.load_session(data, finished)

    def load_session(self, data, on_done=None):
        """Rebuild every pane from backup data (also restores voices so new speech matches old speakers).
        Each part is restored on its own, Key takeaways first: if one part fails the others are still restored.
        The EN/VI rows come in progressively (the app stays usable); on_done(problems) is called at the end."""
        for w in (self.left, self.right, self.bottom, self.chat):
            w.configure(state="normal")
            w.delete("1.0", "end")
            old = [t for t in w.tag_names()
                   if (t[:1] in ("b", "u", "v") and t[1:].isdigit()) or t.startswith(("pad", "vsep"))]
            if old:
                w.tag_delete(*old)                         # one call (thousands of row tags)
            w.configure(state="disabled")
        problems = []
        for name, fn in (("Key takeaways", self._restore_takeaways), ("người nói", self._restore_speakers),
                         ("khung Claude", self._restore_chat)):
            try:
                fn(data)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{name}: {e}")
        self.root.after_idle(lambda: (self.chat.see("end"), self.bottom.see("end")))

        def rows_done(row_problems):
            self.summarizer.done_n = len(self.transcript)  # what is restored is not summarized again
            if on_done:
                on_done(problems + row_problems)
        try:
            self._restore_rows(data, rows_done)
        except Exception as e:  # noqa: BLE001
            self._restoring = False
            self.done("restore")
            rows_done([f"bản ghi tiếng Anh/tiếng Việt: {e}"])
        return problems

    def _restore_takeaways(self, data):
        self.last_summary = data.get("takeaways") or ""
        self.bottom.configure(state="normal")
        try:
            for i, part in enumerate(re.split(r"\n\n(?=\[\d\d:\d\d\]\n)", self.last_summary)
                                     if self.last_summary else []):
                if i:
                    self.bottom.insert("end", "\n\n")
                m = re.match(r"(\[\d\d:\d\d\]\n)", part)
                if m:
                    self.bottom.insert("end", m.group(1), "ttime")
                    part = part[m.end():]
                self.bottom.insert("end", part)
        finally:
            self.bottom.configure(state="disabled")
        self.take_follow = True

    def _restore_speakers(self, data):
        sp = data.get("speakers", {})
        conv = lambda d: {int(k): v for k, v in (d or {}).items()}
        self.spk_custom = conv(sp.get("custom"))
        self.teachers = [int(t) for t in sp.get("teachers", [])]
        self.spk_talk = {int(k): float(v) for k, v in (sp.get("talk") or {}).items()}
        self.spk_alias = {int(k): int(v) for k, v in (sp.get("alias") or {}).items()}
        tr = self.recognizer.speakers
        tr.sums = {int(k): np.asarray(v, dtype=np.float32) for k, v in (sp.get("voices") or {}).items()}
        tr.talk = dict(self.spk_talk)
        tr.merged = dict(self.spk_alias)
        tr.next_id = int(sp.get("next_id", max(tr.sums, default=0) + 1))
        tr.last = None

    RESTORE_CHUNK = 120                  # rows put in per step; the window redraws (and stays usable) in between
    ALIGN_CHUNK = 150

    def _restore_rows(self, data, on_done=None):
        """Rebuild the EN/VI rows quickly and progressively: the final text of a batch of rows is put in with one
        insert per pane (no draft -> final, no "…" -> translation edits), the window redraws between batches
        (text appears while loading and the app stays usable), and rows are lined up afterwards from the
        newest one up. on_done(problems) is called at the end."""
        self.spk_shown, self.last_spk = {}, {"en": None, "vi": None}
        self.uid_spk, self.vi_gen, self.vi_filled = {}, {}, {}
        self.translator.en, self.translator.vi, self.translator.gen = {}, {}, {}
        self.transcript = [tuple(t) for t in data.get("transcript", [])]
        rows = [r for r in data.get("rows", []) if "uid" in r]
        # new speech continues after the last restored row (set first, so it is right even if something fails)
        self.recognizer.utt_id = max([int(r["uid"]) for r in rows], default=self.recognizer.utt_id)
        self._restoring = True
        self._restore_job = {"rows": rows, "i": 0, "on_done": on_done, "problems": [], "tags": set()}
        self.busy("restore", f"Đang khôi phục bản ghi 0/{len(rows)} dòng...")
        self._restore_step()

    def _row_segments(self, pane, uid, sid, text, job):
        """The text pieces (with their tags) of one row, exactly as the live path leaves a finished row."""
        w = self.left if pane == "en" else self.right
        blk, sid = f"b{uid}", self._res(sid)
        if (pane, sid) not in job["tags"]:                     # color tags: configure once per speaker
            self._spk_tags(w, sid)
            job["tags"].add((pane, sid))
        segs = []
        if self.last_spk[pane] != sid:
            self.last_spk[pane] = sid
            if job.get(f"nonempty_{pane}"):
                segs += ["\n", (blk,)]                         # blank line between speakers
            name = self.speaker_name(sid)
            self.spk_shown[sid] = name
            segs += [name + ": ", ("lbl", f"lbl{sid}", blk)]
        tags = (f"spk{sid}", blk) if pane == "en" else (f"v{uid}", f"spk{sid}", blk)
        segs += [text, tags, "\n", (blk, f"pad{uid}")]
        job[f"nonempty_{pane}"] = True
        return segs

    def _restore_step(self):
        job = getattr(self, "_restore_job", None)
        if not job:
            return
        rows, i = job["rows"], job["i"]
        try:
            if i == 0:
                job["nonempty_en"] = self.left.index("end-1c") != "1.0"
                job["nonempty_vi"] = self.right.index("end-1c") != "1.0"
            en, vi = [], []
            for r in rows[i:i + self.RESTORE_CHUNK]:
                uid, sid = int(r["uid"]), int(r.get("speaker", 1))
                self.uid_spk[uid] = sid
                self.recognizer.utt_spk[uid] = sid
                e, v = (r.get("en") or "").strip(), (r.get("vi") or "").strip()
                self.translator.en[uid], self.translator.vi[uid], self.translator.gen[uid] = e, v, 1
                self.vi_gen[uid], self.vi_filled[uid] = 1, True
                en += self._row_segments("en", uid, sid, split_sentences(e), job)
                vi += self._row_segments("vi", uid, sid, split_sentences(v) if v else "—", job)
            for w, segs in ((self.left, en), (self.right, vi)):
                if segs:
                    w.configure(state="normal")
                    w.insert("end", *segs)
                    w.configure(state="disabled")
                    w.see("end")                               # the text is seen coming in
            job["i"] = i = min(len(rows), i + self.RESTORE_CHUNK)
        except Exception as e:  # noqa: BLE001
            job["problems"].append(f"bản ghi tiếng Anh/tiếng Việt (dòng {i + 1}): {e}")
            job["i"] = i = len(rows)
        if i < len(rows):
            self.busy("restore", f"Đang khôi phục bản ghi {i}/{len(rows)} dòng...")
            self.root.update_idletasks()                       # redraw now, so the rows are seen coming in
            self.root.after(1, self._restore_step)
            return
        # all rows are in: line them up, newest first (what is on screen), a batch per step
        self._restoring = False
        self._restore_job = None
        job["align"] = [int(r["uid"]) for r in reversed(rows)]
        self._align_job = job
        self._align_step()

    def _align_step(self):
        job = getattr(self, "_align_job", None)
        if not job:
            return
        todo = job["align"]
        batch, job["align"] = todo[:self.ALIGN_CHUNK], todo[self.ALIGN_CHUNK:]
        try:
            self._align_uids(batch)
        except Exception as e:  # noqa: BLE001
            job["problems"].append(f"căn hàng: {e}")
            job["align"] = []
        if job["align"]:
            done = len(job["rows"]) - len(job["align"])
            self.busy("restore", f"Đang căn thẳng hàng tiếng Anh/tiếng Việt {done}/{len(job['rows'])}...")
            self.root.update_idletasks()
            self.root.after(1, self._align_step)
            return
        self._align_job = None
        self.done("restore")
        self.left.see("end")
        self._sync_from(self.left, self.right)
        if job.get("on_done"):
            job["on_done"](job["problems"])

    def _align_uids(self, uids):
        """Give each row the same height in both panes (padding under the shorter one)."""
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

    # ---- "working on it" indicator: shown while something takes time, gone when it is done ----
    def busy(self, key, text):
        self._busy[key] = text
        self._show_busy()

    def done(self, key):
        self._busy.pop(key, None)
        self._show_busy()

    def _show_busy(self):
        if not self._busy:
            self.busy_lbl.config(text="")
            self.busy_lbl.pack_forget()
            return
        dots = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self._busy_tick % 10]
        self.busy_lbl.config(text=f"{dots} " + "   ·   ".join(self._busy.values()))
        if not self.busy_lbl.winfo_ismapped():
            self.busy_lbl.pack(side="left", padx=(4, 8), pady=6, after=self.status)

    def _busy_spin(self):
        self._busy_tick += 1
        if self._busy:
            self._show_busy()
        self.root.after(120, self._busy_spin)

    def _restore_chat(self, data):
        self.chat_log = list(data.get("chat", []))
        w = self.chat
        w.configure(state="normal")
        try:
            for i, turn in enumerate(self.chat_log):
                if i:
                    w.insert("end", "\n\n")
                w.insert("end", "Bạn: ", "you")
                w.insert("end", turn.get("q", "") + "\n")
                if turn.get("files"):
                    w.insert("end", "📎 " + ", ".join(turn["files"]) + "\n", "file")
                w.insert("end", "Claude: ", "who")
                w.insert("end", turn.get("a", ""))
        finally:
            w.configure(state="disabled")
        self.chatter.history = list(data.get("chat_history", []))

    def choose_output_folder(self):
        """Output Folder: backups go to its Backup folder, exported files to its Outputs folder."""
        from tkinter import filedialog, messagebox
        cfg = load_config()
        folder = filedialog.askdirectory(parent=self.root, title="Chọn Output Folder (nơi lưu backup và kết quả)",
                                         initialdir=cfg.get("output_folder") or None)
        if not folder:
            return
        try:
            for sub in ("Backup", "Outputs"):
                os.makedirs(os.path.join(folder, sub), exist_ok=True)
        except OSError as e:
            messagebox.showerror("Output Folder", f"Không tạo được thư mục con: {e}", parent=self.root)
            return
        cfg["output_folder"] = folder
        save_config(cfg)
        self.status.config(text=f"✓ Output Folder: {folder}  (Backup: file backup · Outputs: PDF Key takeaways)",
                           fg="#7ee2a8")

    def export_takeaways(self):
        from tkinter import filedialog, messagebox
        if not self.last_summary.strip():
            messagebox.showinfo("Key takeaways", "Chưa có ý chính nào để xuất.", parent=self.root)
            return
        name = f"KeyTakeaways_{time.strftime('%Y%m%d_%H%M')}.pdf"
        outputs = output_dir("Outputs")
        if outputs:                                # saved straight into <Output Folder>/Outputs
            path = os.path.join(outputs, name)
        else:
            path = filedialog.asksaveasfilename(parent=self.root, title="Xuất Key takeaways ra PDF",
                                                defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
                                                initialfile=name)
            if not path:
                return
        try:
            pdf_export.takeaways_pdf(path, self.last_summary)
            self.status.config(text=f"✓ Đã xuất PDF: {os.path.basename(path)}", fg="#7ee2a8")
            pdf_export.open_path(path)
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

    @staticmethod
    def _saved_block(b):
        """How one message block is kept in the saved session (short text; files and documents are read again
        from the library when needed, not stored twice)."""
        t = b.get("type")
        if t == "text":
            return b.get("text", "")
        if t == "tool_use":
            return f"(Claude đọc: {b.get('name')} {json.dumps(b.get('input', {}), ensure_ascii=False)})"
        if t == "tool_result":
            return "(nội dung đã đọc — đọc lại khi cần)"
        return f"[file đính kèm: {b.get('title', t)}]"

    def _chat_tool(self, name, inp):
        """What Claude asked to read (called from the chat thread): the whole session record, optionally
        searched."""
        if name == "read_class_record":
            part = inp.get("part") or "english"
            limit = max(2000, min(int(inp.get("max_chars") or 30_000), 100_000))
            if part == "takeaways":
                text = self.last_summary
            elif part == "english":
                text = self.transcript_text()
            else:
                pairs = list(self.translator.history)
                text = "\n".join(v for _, v in pairs) if part == "vietnamese" else \
                    "\n".join(f"EN: {e}\nVI: {v}" for e, v in pairs)
            if not text.strip():
                return "(chưa có nội dung)"
            term = (inp.get("search") or "").strip().lower()
            if term:
                lines = text.splitlines()
                keep = sorted({j for i, l in enumerate(lines) if term in l.lower()
                               for j in range(max(0, i - 1), min(len(lines), i + 2))})
                if not keep:
                    return f"(không có dòng nào nhắc tới “{inp.get('search')}”)"
                text = "\n".join(lines[j] for j in keep)
                return text[:limit] + ("\n…(còn nữa)" if len(text) > limit else "")
            return ("…(phần trước còn nữa)\n" if len(text) > limit else "") + text[-limit:]
        return "(không rõ công cụ)"

    def _chat_context(self):
        """What Claude knows about the session when answering (read from the UI thread's data). The Vietnamese
        translation repeats the English, so only its end is sent (it would cost as much again)."""
        en = self.transcript_text()[-40000:]
        vi = "\n".join(v for _, v in self.translator.history)[-3000:]
        langs = f"spoken: {lang_name(self.src_lang)}; translated into: {lang_name(self.tgt_lang)}"
        return en, vi, self.last_summary[-8000:], self.on, langs

    # ---- events ----
    def _on_english(self, ids, text, lang):
        # called from the Whisper thread with the accurate text for one or more utterances
        self.transcript.append((self.recognizer.utt_spk.get(ids[0], 1), text, ids[0]))
        self.ui_q.put(("final", (ids, text, lang)))

    def transcript_range(self, start=0, end=None):
        """(end index, text) of transcript entries start..end, in the TRANSLATION language where the row is
        already translated (used by the running takeaways, written in that language)."""
        items = list(self.transcript)
        end = len(items) if end is None else end
        vi = dict(self.translator.vi)
        rows = [(t[0], (vi.get(t[2]) or t[1]) if len(t) > 2 else t[1]) for t in items[start:end]]
        # no speaker names: the small takeaways model copied "Người nói 1:" into its notes
        lines, last = [], None
        for sid, text in rows:
            if sid != last or not lines:
                lines.append(text)
                last = sid
            else:
                lines[-1] += ("" if self.tgt_lang in local_ai.NO_SPACES else " ") + text
        return end, "\n".join(lines)

    def transcript_text(self):
        """English transcript with speaker names, e.g. 'Teacher: ...' (used by chat)."""
        return self._transcript_lines(list(self.transcript))

    def _transcript_lines(self, items):
        lines, last = [], None
        for sid, text, *_ in items:
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
            if not rng:                              # empty text: nothing to split (and no range to read)
                return
            body = w.get(rng[0], rng[1])
            for m in reversed(list(sentence_gaps(body))):
                a = w.index(f"{rng[0]} + {m.start(1)}c")
                b = w.index(f"{rng[0]} + {m.end(1)}c")
                w.delete(a, b)
                w.insert(a, "\n", (f"v{uid}", spk_tag, f"b{uid}"))
        self._vi_edit(put)
        self._schedule_align(uid)

    def _vi_replace(self, uid, g, text):
        """A corrected translation replaces the row's text in one go (no "…" in between)."""
        if g < self.vi_gen.get(uid, 0):
            return
        self.vi_gen[uid] = g
        self.vi_filled[uid] = False
        self._vi_piece(uid, g, text)

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
        if getattr(self, "_align_all_job", None):         # <Configure> can fire while the window is still being built
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
        self._align_uids(uids)
        self._follow()

    def _set_quality(self, name, startup=False):
        """Listening quality from the top bar. "Cao" downloads Whisper small.en once (~480 MB, asks first) and
        loads it in the background. The menu always shows the mode that is really running: while Cao is being
        prepared it reads "Cao: 37%" / "Cao: đang nạp" and listening stays on Nhanh until Cao is ready."""
        cfg = load_config()
        cfg["listen_quality"] = name
        save_config(cfg)
        rec = self.recognizer
        self._quality_wanted = name
        if name == QUALITY_FAST:
            rec.quality = QUALITY_FAST
            self.quality_var.set(QUALITY_FAST)
            self.ui_q.put(("status", "Chất lượng nghe: Nhanh (đang dùng)"))
            return
        if rec.whisper_hq is not None and rec.hq_lang == self.src_lang:
            rec.quality = QUALITY_HIGH
            self.quality_var.set(QUALITY_HIGH)
            self.ui_q.put(("status", "Chất lượng nghe: Cao (đang dùng)"))
            return
        if getattr(self, "_quality_job", False):         # already downloading/loading: just keep showing progress
            self.quality_var.set(self._quality_label)
            return
        free = free_memory_mb()
        if free is not None and free < HQ_START_MB:
            self._quality_back()
            msg = (f"Máy đang thiếu bộ nhớ (còn khoảng {free:.0f} MB) nên chưa bật được chất lượng nghe Cao — "
                   f"vẫn dùng Nhanh. Hãy đóng bớt chương trình khác rồi chọn lại Cao.")
            if startup:
                self.ui_q.put(("error", msg))
            else:
                from tkinter import messagebox
                messagebox.showwarning("Chất lượng nghe: Cao", msg, parent=self.root)
            return
        hq_pid = whisper_pack(self.src_lang, QUALITY_HIGH)
        need_download = not STORE.have(hq_pid)
        if need_download:
            from tkinter import messagebox
            pack = local_ai.PACKS[hq_pid]
            if startup or not messagebox.askyesno(
                    "Chất lượng nghe: Cao",
                    f"Chế độ Cao dùng mô hình nhận diện lớn hơn ({pack.title}): nghe chính xác hơn với giọng "
                    "khó, người nói xa, phòng ồn, nhưng chậm hơn vài giây mỗi câu.\n\n"
                    f"Cần tải mô hình một lần (khoảng {local_ai.human_mb(pack.size_mb)}), sau đó chạy không cần "
                    "internet. Tải ngay?", parent=self.root):
                self._quality_back()
                return
        self._quality_job = True

        def show(label, busy_text):                      # UI thread: menu label + busy line together
            self._quality_label = label
            if self._quality_wanted == QUALITY_HIGH:
                self.quality_var.set(label)
            self.busy("quality", busy_text + " Trong lúc chờ vẫn nghe ở chế độ Nhanh.")
        show("Cao: 0%" if need_download else "Cao: đang nạp",
             "Đang tải mô hình nghe chất lượng cao... 0%." if need_download
             else "Đang nạp mô hình nghe chất lượng cao...")

        def work():
            try:
                if need_download:
                    shown = [-1]

                    def progress(done, total):          # "37% (180/486 MB)", updated each 1%
                        pct = int(done * 100 / total) if total else 0
                        if pct != shown[0]:
                            shown[0] = pct
                            text = (f"Đang tải mô hình nghe chất lượng cao... {pct}% "
                                    f"({done / 1e6:.0f}/{total / 1e6:.0f} MB).")
                            self.ui_q.put(("call", lambda t=text, l=f"Cao: {pct}%": show(l, t)))
                    STORE.download(hq_pid, progress)
                    self.ui_q.put(("call", lambda: show("Cao: đang nạp", "Đang nạp mô hình nghe chất lượng cao...")))
                ok = rec.load_hq()
                err = None if ok else "không tìm thấy mô hình sau khi tải"
            except Exception as e:  # noqa: BLE001
                err = str(e)

            def finish():
                self._quality_job = False
                self.done("quality")
                if err:
                    self.ui_q.put(("error", f"Không bật được chất lượng nghe Cao (vẫn dùng Nhanh): {err}"))
                    self._quality_back()
                elif self._quality_wanted == QUALITY_HIGH:     # still chosen -> switch now
                    rec.quality = QUALITY_HIGH
                    self.quality_var.set(QUALITY_HIGH)
                    self.ui_q.put(("status", "Chất lượng nghe: Cao (đã sẵn sàng, đang dùng)"))
            self.ui_q.put(("call", finish))
        threading.Thread(target=work, daemon=True).start()

    def _quality_back(self):
        self._quality_wanted = QUALITY_FAST
        self.quality_var.set(QUALITY_FAST)
        self.recognizer.quality = QUALITY_FAST
        cfg = load_config()
        cfg["listen_quality"] = QUALITY_FAST
        save_config(cfg)

    # ---- languages and language packs (all run on this PC; packs are downloaded once) ----
    @staticmethod
    def _code(name):
        if name == lang_name(AUTO):
            return AUTO
        return next((c for c in LANGS if lang_name(c) == name), "en")

    def _set_pane_titles(self):
        src, tgt = self.src_lang, self.tgt_lang
        self.left.head.config(text="Lời nói (tự nhận diện ngôn ngữ)" if src == AUTO else
                              f"{lang_name(src)} (nhận diện giọng nói)")
        self.right.head.config(text=f"{lang_name(tgt)} (bản dịch trên máy)" if src != tgt else
                               f"{lang_name(tgt)} (cùng ngôn ngữ, không cần dịch)")
        self.root.title(f"Live Translator v{APP_VERSION} — {lang_name(src)} → {lang_name(tgt)} (offline)")

    def set_languages(self, src=None, tgt=None):
        """New language choice from the top bar (remembered). Listening goes on with the new models."""
        src = self.src_lang if src is None else src
        tgt = self.tgt_lang if tgt is None else tgt
        if tgt == AUTO:
            tgt = "vi"
        changed_src = src != self.src_lang
        self.src_lang, self.tgt_lang = src, tgt
        self.src_var.set(lang_name(src))
        self.tgt_var.set(lang_name(tgt))
        cfg = load_config()
        cfg["src_lang"], cfg["tgt_lang"] = src, tgt
        save_config(cfg)
        self.translator.set_languages(src, tgt)
        self.translator.mt_error = None
        self.summarizer.lang = tgt
        self._set_pane_titles()
        was_on = self.on
        if self.missing_packs():
            if was_on:
                self.toggle_onoff()                       # stop until the new language's packs are here
            self.recognizer.set_language(src)
            self.ensure_packs(start=was_on)
            return
        self.recognizer.set_language(src)
        if changed_src and self.quality_var.get() == QUALITY_HIGH:
            self.recognizer.quality = QUALITY_FAST
            self._set_quality(QUALITY_HIGH)
        if src != tgt and STORE.have("nllb-600m"):
            self.translator.warm()
        self.status.config(text=f"Nghe {lang_name(src)} → dịch sang {lang_name(tgt)}"
                                + (" (đang nạp mô hình...)" if was_on and changed_src else ""), fg="#ddd")

    def needed_packs(self):
        """(packs needed to listen and translate, the takeaways pack) for the current choice."""
        need = local_ai.Store.needed(self.src_lang, self.tgt_lang)
        need = [p for p in need if p not in ("small.en", "whisper-medium")]      # Cao is asked on its own
        return need, self.summarizer.pack

    def missing_packs(self):
        """Packs without which listening/translation cannot start (Vosk only adds live words: optional)."""
        need, _ = self.needed_packs()
        return [p for p in STORE.missing(need) if not p.startswith("vosk-")]

    def ensure_packs(self, start=False):
        """Ask to download what the current choice needs (listening, translation, takeaways, live words);
        `start`: press ON by itself when they are here."""
        from tkinter import messagebox
        need, take = self.needed_packs()
        todo = STORE.missing(need + [take])
        todo = [p for p in todo if p not in self._dl_queue and p != self._dl_current]
        if not todo:
            if self._dl_current or self._dl_queue:       # already downloading: start when it is done
                if start:
                    self._dl_then.append(self._start_when_ready)
                return
            if start and not self.on:
                self.toggle_onoff()
            return
        lines = "\n".join(f"• {local_ai.PACKS[p].title} — {local_ai.human_mb(local_ai.PACKS[p].size_mb)}"
                          for p in todo)
        total = sum(local_ai.PACKS[p].size_mb for p in todo)
        if not messagebox.askyesno(
                "Tải gói ngôn ngữ",
                f"Nghe {lang_name(self.src_lang)} → dịch sang {lang_name(self.tgt_lang)} cần các gói sau "
                f"(tổng khoảng {local_ai.human_mb(total)}):\n\n{lines}\n\n"
                "Tải một lần (cần internet lúc tải), sau đó nhận diện, dịch và Key takeaways chạy hoàn toàn trên "
                "máy, không cần internet. Tải ngay?", parent=self.root):
            if self.missing_packs():
                self.status.config(text="Chưa có đủ gói ngôn ngữ cho lựa chọn này — bấm 📦 Gói ngôn ngữ để tải.",
                                   fg="#ffd479")
            elif start and not self.on:
                self.toggle_onoff()                       # only the takeaways pack is missing: listen anyway
            return
        self.download_packs(todo, then=self._start_when_ready if start else None)

    def _start_when_ready(self):
        if not self.on and not self.missing_packs():
            self.toggle_onoff()

    def _first_run_packs(self):
        """At start: if the chosen languages still need packs, offer them once per language pair."""
        need, take = self.needed_packs()
        if not STORE.missing(need + [take]):
            return
        cfg = load_config()
        key = f"{self.src_lang}>{self.tgt_lang}"
        if key in cfg.get("packs_offered", []):
            self.status.config(text="Một số gói ngôn ngữ chưa được tải — bấm 📦 Gói ngôn ngữ (hoặc ON) để tải.",
                               fg="#ffd479")
            return
        cfg["packs_offered"] = cfg.get("packs_offered", []) + [key]
        save_config(cfg)
        self.ensure_packs()

    def download_packs(self, pids, then=None):
        """Download packs one after another in the background (progress in the top bar and the 📦 window)."""
        for p in pids:
            if p not in self._dl_queue and p != self._dl_current and not STORE.have(p):
                self._dl_queue.append(p)
        if then:
            self._dl_then.append(then)
        self._refresh_packs_win()
        if self._dl_running:
            return
        self._dl_running = True
        self._dl_cancel.clear()

        def work():
            errors = []
            while self._dl_queue:
                pid = self._dl_queue.pop(0)
                self._dl_current = pid
                pack = local_ai.PACKS[pid]
                t0, shown = time.time(), [-1]

                def progress(done, total, pack=pack, t0=t0, shown=shown):
                    pct = int(done * 100 / total) if total else 0
                    if pct != shown[0]:
                        shown[0] = pct
                        self._dl_progress = pct
                        text = (f"Đang tải {pack.title}: {pct}% ({done / 1e6:.0f}/{total / 1e6:.0f} MB"
                                f"{local_ai.eta(t0, done, total)})" + (f" · còn {len(self._dl_queue)} gói nữa"
                                                                      if self._dl_queue else ""))
                        self.ui_q.put(("call", lambda t=text: (self.busy("packs", t), self._refresh_packs_win())))
                try:
                    STORE.download(pid, progress, self._dl_cancel)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{pack.title}: {e}")
                    if self._dl_cancel.is_set():
                        self._dl_queue.clear()
                self._dl_current, self._dl_progress = None, 0
                self.ui_q.put(("call", self._refresh_packs_win))

            def finish():
                from tkinter import messagebox
                self._dl_running = False
                self.done("packs")
                thens, self._dl_then = self._dl_then, []
                self.recognizer.loaded = None if not self.on else self.recognizer.loaded   # new models next ON
                self._refresh_packs_win()
                if errors:
                    messagebox.showwarning("Tải gói ngôn ngữ", "Chưa tải xong:\n\n" + "\n".join(errors) +
                                           "\n\nKiểm tra internet rồi tải lại (phần đã tải được giữ lại).",
                                           parent=self.root)
                else:
                    self.status.config(text="✓ Đã tải xong gói ngôn ngữ — chạy được không cần internet.",
                                       fg="#7ee2a8")
                for fn in thens:
                    fn()
            self.ui_q.put(("call", finish))
        threading.Thread(target=work, daemon=True).start()

    def set_takeaways_pack(self, pid):
        self.summarizer.pack = pid
        cfg = load_config()
        cfg["takeaways_pack"] = pid
        save_config(cfg)
        if not STORE.have(pid):
            self.ensure_packs()

    def _fill_gear_menu(self):
        m = self.gear_menu
        m.delete(0, "end")
        m.add_command(label="📦  Gói ngôn ngữ (offline)...", command=self.open_packs)
        m.add_separator()
        m.add_command(label="Key takeaways viết bằng:", state="disabled")
        for pid in local_ai.LLM_PACKS:
            title = local_ai.PACKS[pid].title.split("— ", 1)[-1]
            mark = "●  " if self.summarizer.pack == pid else "     "
            m.add_command(label=mark + title + ("" if STORE.have(pid) else "  (chưa tải)"),
                          command=lambda p=pid: self.set_takeaways_pack(p))
        m.add_separator()
        m.add_command(label="📂  Output Folder (nơi lưu backup, PDF)...", command=self.choose_output_folder)
        m.add_separator()
        if self.chatter.client is not None:
            m.add_command(label="Đăng xuất Claude", command=self.logout)
        else:
            m.add_command(label="Đăng nhập Claude (chỉ khung hỏi Claude cần internet)...",
                          command=self.login_claude)

    def open_packs(self):
        """The 📦 window: every pack, whether it is on this PC, download / delete, open the folder."""
        from tkinter import ttk
        w = getattr(self, "_packs_win", None)
        if w and w.winfo_exists():
            w.lift()
            self._refresh_packs_win()
            return
        w = self._packs_win = tk.Toplevel(self.root)
        w.title("Gói ngôn ngữ — chạy offline")
        w.geometry("760x560")
        w.transient(self.root)
        bg = "#faf9f5"
        w.configure(bg=bg)
        tk.Label(w, text="Gói ngôn ngữ (chạy hoàn toàn trên máy)", font=(UI_FONT, 13, "bold"), bg=bg).pack(
            anchor="w", padx=14, pady=(12, 0))
        tk.Label(w, text="Mỗi gói tải một lần (cần internet lúc tải); sau đó nghe, dịch và Key takeaways chạy "
                         "không cần internet. Chỉ tải các ngôn ngữ bạn dùng. Máy không có internet: chép thư mục "
                         "gói từ máy khác vào đúng chỗ.", font=(UI_FONT, 9), fg="#555", bg=bg, wraplength=720,
                 justify="left").pack(anchor="w", padx=14, pady=(2, 6))
        self._packs_need = tk.Label(w, text="", font=(UI_FONT, 9, "bold"), fg="#1a5fb4", bg=bg, wraplength=720,
                                    justify="left")
        self._packs_need.pack(anchor="w", padx=14)
        frame = tk.Frame(w, bg=bg)
        frame.pack(fill="both", expand=True, padx=14, pady=6)
        tree = self._packs_tree = ttk.Treeview(frame, columns=("size", "state"), show="tree headings",
                                               selectmode="extended")
        tree.heading("#0", text="Gói")
        tree.heading("size", text="Dung lượng")
        tree.heading("state", text="Trạng thái")
        tree.column("#0", width=470)
        tree.column("size", width=90, anchor="e")
        tree.column("state", width=150)
        sb = tk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        groups = {}
        for g in ("Dịch", "Ý chính", "Nghe"):
            groups[g] = tree.insert("", "end", text={"Dịch": "Dịch", "Ý chính": "Key takeaways",
                                                     "Nghe": "Nhận diện giọng nói"}[g], open=True)
        tree.insert(groups["Nghe"], "end", iid="__en", text="Nghe tiếng Anh — Whisper base.en + Vosk (có sẵn trong app)",
                    values=("", "✓ có sẵn"))
        for pid, p in local_ai.PACKS.items():
            tree.insert(groups[p.group], "end", iid=pid, text=p.title, values=(local_ai.human_mb(p.size_mb), ""))
        btns = tk.Frame(w, bg=bg)
        btns.pack(fill="x", padx=14, pady=(0, 12))
        style = dict(font=(UI_FONT, 9), relief="flat", cursor="hand2", padx=10, pady=4)

        def selected():
            return [i for i in tree.selection() if i in local_ai.PACKS]

        def delete():
            from tkinter import messagebox
            ids = [i for i in selected() if STORE.have(i) and os.path.isdir(os.path.join(STORE.dir, i))]
            if not ids:
                return
            if self.on:
                messagebox.showinfo("Xoá gói", "Hãy tắt nghe (OFF) trước khi xoá gói.", parent=w)
                return
            if messagebox.askyesno("Xoá gói", "Xoá khỏi máy (tải lại được khi cần):\n\n" +
                                   "\n".join("• " + local_ai.PACKS[i].title for i in ids), parent=w):
                for i in ids:
                    unload_whisper(i)
                    STORE.remove(i)
                if "nllb-600m" in ids:
                    self.translator.mt = None
                if self.summarizer.llm_pack in ids:
                    self.summarizer.llm, self.summarizer.llm_pack = None, None
                self.recognizer.loaded = None
                self._refresh_packs_win()

        tk.Button(btns, text="⬇ Tải đủ cho lựa chọn hiện tại", bg="#27ae60", fg="white", activebackground="#1e8449",
                  command=lambda: self.ensure_packs(), **style).pack(side="left")
        tk.Button(btns, text="⬇ Tải gói đã chọn", bg="#3a7bd5", fg="white", activebackground="#2f66b3",
                  command=lambda: self.download_packs(selected()), **style).pack(side="left", padx=6)
        tk.Button(btns, text="🗑 Xoá gói đã chọn", bg="#eee", command=delete, **style).pack(side="left")
        tk.Button(btns, text="⏹ Dừng tải", bg="#eee", command=self._dl_cancel.set, **style).pack(side="left", padx=6)
        tk.Button(btns, text="📂 Mở thư mục gói", bg="#eee",
                  command=lambda: (os.makedirs(STORE.dir, exist_ok=True), pdf_export.open_path(STORE.dir)),
                  **style).pack(side="right")
        self._refresh_packs_win()

    def _refresh_packs_win(self):
        w = getattr(self, "_packs_win", None)
        if not (w and w.winfo_exists()):
            return
        tree = self._packs_tree
        need, take = self.needed_packs()
        need_all = need + [take]
        for pid in local_ai.PACKS:
            if pid == self._dl_current:
                state = f"⬇ đang tải {self._dl_progress}%"
            elif pid in self._dl_queue:
                state = "⏳ chờ tải"
            elif STORE.have(pid):
                state = "✓ đã có"
            else:
                state = "cần cho lựa chọn này" if pid in need_all else "chưa tải"
            tree.set(pid, "state", state)
            tree.item(pid, tags=("need",) if pid in need_all and not STORE.have(pid) else ())
        tree.tag_configure("need", foreground="#c0392b")
        miss = STORE.missing(need_all)
        self._packs_need.config(
            text=f"Lựa chọn hiện tại: nghe {lang_name(self.src_lang)} → dịch sang {lang_name(self.tgt_lang)}: " +
                 ("✓ đủ gói, chạy được không cần internet." if not miss else
                  "còn thiếu " + ", ".join(local_ai.PACKS[p].title.split(" — ")[0] for p in miss) + "."),
            fg="#27ae60" if not miss else "#c0392b")

    def toggle_onoff(self):
        if not self.on and self.restoring():          # don't start listening in the middle of a restore
            return
        if not self.on and self.missing_packs():
            self.ensure_packs(start=True)             # download first (asks), then starts by itself
            return
        self.on = not self.on
        if self.on:
            self.toggle.config(text="ON", bg="#27ae60", activebackground="#1e8449")
            self.recognizer.running.set()
            if self.src_lang != self.tgt_lang:
                self.translator.warm()
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
                    total = sum(talk.values()) or 1.0
                    for s, secs in talk.items():
                        # Teacher (or Teacher 2, 3...): spoke 10 min, or is clearly the main speaker after 3 min
                        # (half of all speech) so the label shows early in a new class
                        main = secs >= TEACHER_EARLY_MINUTES * 60 and secs >= 0.5 * total
                        if (secs >= TEACHER_MINUTES * 60 or main) and s not in self.teachers:
                            self.teachers.append(s)
                    self._relabel()
                elif kind == "draft":
                    self._add_draft(*val)
                    lang = self.recognizer.utt_lang.get(val[0], self.src_lang)
                    self.translator.draft(*val, lang)    # translate right away, don't wait for Whisper
                elif kind == "final":
                    ids, text, lang = val
                    self._finalize(ids, text)
                    self.translator.final(ids, text, lang)   # re-translates only if the words changed
                elif kind == "flush":
                    pass
                elif kind == "vi_slot":
                    self._vi_slot(val)
                elif kind == "vi_piece":
                    self._vi_piece(*val)
                elif kind == "vi_reset":
                    self._vi_reset(*val)
                elif kind == "vi_replace":
                    self._vi_replace(*val)
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
                    self.done("chat")
                    self.send_btn.config(state="normal", text="Gửi ➤")
                    if self.panel_open:
                        self.prompt.focus_set()
                    else:                       # answered while the panel was closed -> show a hint
                        self.unread = True
                        self._refresh_panel_btn()
                elif kind == "summary":
                    self._add_takeaways(val)
                elif kind == "ready":
                    self.ready = True
                    if not self.on and not self._busy.get("packs"):
                        self.status.config(text="✓ Sẵn sàng. Nhấn ON để bắt đầu nghe.", fg="#7ee2a8")
                elif kind == "status":
                    self.status.config(text=val, fg="#ddd")
                elif kind == "error":
                    self.status.config(text=val, fg="#ff7b7b")
                elif kind == "call":                # run a function on the UI thread
                    val()
                elif kind == "quality_off":         # Cao was turned off for lack of memory
                    self._quality_back()
                    self.status.config(text=val, fg="#ffd479")
                elif kind == "need_packs":          # a model this choice needs is not on this PC yet
                    if self.on or val == "nllb-600m":
                        self.ensure_packs(start=self.on or self._start_after_packs)
                    else:
                        self.status.config(text="Chưa có gói ngôn ngữ cho lựa chọn này — bấm 📦 Gói ngôn ngữ "
                                                "hoặc ON để tải.", fg="#ffd479")
                elif kind == "busy":
                    key, text = val
                    self.busy(key, text) if text else self.done(key)
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
    """`LiveTranslatorML.exe --selftest` -> checks the bundled speech models and every language pack on this PC
    (a sample translation and takeaways, all offline), writes selftest.txt next to the app."""
    lines = []
    try:
        t = time.time()
        preload_models("en")
        if _PRELOAD["error"]:
            raise _PRELOAD["error"]
        lines.append(f"OK v{APP_VERSION} English speech models loaded in {time.time() - t:.1f}s")
        lines.append(f"whisper: {whisper_model_path()}\nvosk: {model_dir(VOSK_MODEL)}\n"
                     f"voiceprints: {_PRELOAD['spk'] is not None}\n"
                     f"native input box: {__import__('importlib').util.find_spec('native_edit') is not None}\n"
                     f"ant: {find_ant()}\npdf: {__import__('importlib').util.find_spec('pymupdf') is not None}\n"
                     f"packs folder: {STORE.dir}\n"
                     f"packs here: {', '.join(p for p in local_ai.PACKS if STORE.have(p)) or '(none)'}")
        if STORE.have("nllb-600m"):
            t = time.time()
            mt = local_ai.NLLB(STORE.path("nllb-600m"), threads=4)
            lines.append(f"translation model loaded in {time.time() - t:.1f}s")
            for src, tgt, text in (("en", "vi", "Today we will talk about how neural networks learn from data."),
                                   ("vi", "en", "Hôm nay chúng ta sẽ nói về cách mạng nơ-ron học từ dữ liệu."),
                                   ("en", "ja", "The meeting starts at nine. Please bring your laptop."),
                                   ("zh", "vi", "我们今天讨论机器学习。")):
                t = time.time()
                out = mt.translate(text, src, tgt)
                lines.append(f"translate {src}->{tgt} ({time.time() - t:.2f}s): {out}")
        for pid in local_ai.LLM_PACKS:
            if STORE.have(pid):
                t = time.time()
                llm = local_ai.LocalLLM(STORE.path(pid), threads=local_ai.free_threads())
                lines.append(f"takeaways model {pid} loaded in {time.time() - t:.1f}s")
                t = time.time()
                text = ("Giảng viên: Hôm nay chúng ta học về học máy. Học máy là cách máy tính học quy luật từ dữ "
                        "liệu thay vì được lập trình từng bước. Có ba loại chính: học có giám sát, học không giám "
                        "sát và học tăng cường. Bài tập về nhà nộp vào thứ sáu.")
                out = llm.chat(local_ai.TAKEAWAY_SYSTEM.format(lang="Vietnamese"),
                               f"Existing takeaways:\n(none yet)\n\nNEW part of the transcript:\n{text}\n\n"
                               f"Write the new key takeaways in Vietnamese.", max_new=300)
                lines.append(f"takeaways ({time.time() - t:.1f}s):\n{local_ai.clean_bullets(out)}")
        if STORE.have("asr-vi"):
            t = time.time()
            asr = get_fast_asr("asr-vi")
            lines.append(f"Vietnamese recognizer loaded in {time.time() - t:.1f}s, silence -> "
                         f"'{asr.decode(np.zeros(SAMPLE_RATE, dtype=np.float32))}'")
        for pid in ("whisper-small", "whisper-medium", "small.en"):
            if STORE.have(pid):
                t = time.time()
                get_whisper(pid)
                lines.append(f"speech model {pid} loaded in {time.time() - t:.1f}s")
        msg = "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        msg = "\n".join(lines + [f"FAIL {e!r}"])
    with open(os.path.join(APP_DIR, "selftest.txt"), "w", encoding="utf-8") as f:
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

    # no login screen: listening, translation and takeaways run offline; the Claude panel asks for a login
    threading.Thread(target=preload_models, daemon=True).start()
    MainWindow(root, None)
    root.mainloop()


if __name__ == "__main__":
    main()
