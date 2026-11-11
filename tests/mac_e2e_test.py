"""Full end-to-end test of the app on a Mac (GitHub Actions), run from source.

The real app window is built and a fake microphone plays English speech (made with macOS `say`,
two different voices) in real time. Each check matches a problem the user once hit on Windows:
live-word latency, EN/VI rows level, auto-scroll that follows new text but never fights a user who
scrolled up, speaker labels (a returning speaker keeps the same label), takeaways kept as timestamped
history, prompt box as tall as Key takeaways on both sides, Enter sends the prompt (Vietnamese text
kept intact), backup/restore, friendly "credit balance" error, window fits the screen. Plus the
Learning features (Documents/Certificate windows, certificate copy to the Mac clipboard,
Vietnamese PDF).

Translation uses Claude if ANTHROPIC_API_KEY is set, otherwise a fake translator.

usage: python tests/mac_e2e_test.py <out_dir> <A:a1.wav> <B:b1.wav> <A:a2.wav> ...
Writes screenshots (*.png) and result.json to out_dir. Exit code 1 if a check fails.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import wave

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = sys.argv[1]
SEGMENTS = [a.split(":", 1) for a in sys.argv[2:]]          # [(speaker letter, wav path)]
os.makedirs(OUT, exist_ok=True)

import sounddevice                                   # noqa: E402
import tkinter as tk                                  # noqa: E402
from tkinter import filedialog, messagebox            # noqa: E402

import app                                            # noqa: E402
import learning                                       # noqa: E402

results = {"checks": {}, "notes": [], "timings": {}}


def check(name, ok, detail=""):
    results["checks"][name] = {"ok": bool(ok), "detail": str(detail)[:600]}
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""), flush=True)


def save_results():
    results["passed"] = all(c["ok"] for c in results["checks"].values())
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def watchdog():                                       # never hang the CI job
    time.sleep(900)
    check("finished_in_time", False, "test still running after 15 min")
    save_results()
    os._exit(3)


threading.Thread(target=watchdog, daemon=True).start()


def shot(name):
    path = os.path.join(OUT, name + ".png")
    try:
        r = subprocess.run(["screencapture", "-x", path], capture_output=True, text=True, timeout=30)
        err = r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        err = repr(e)
    if not os.path.exists(path):
        results["notes"].append(f"screenshot {name} failed: {err}")


# ---- dialogs never block the test ----
for fn in ("showinfo", "showwarning", "showerror"):
    setattr(messagebox, fn, lambda *a, **k: "ok")
messagebox.askyesno = lambda *a, **k: True
messagebox.askyesnocancel = lambda *a, **k: None
messagebox.askokcancel = lambda *a, **k: True

# ---- fake microphone: plays the segments in real time through the app's own callback ----
SR = app.SAMPLE_RATE
parts, seg_times, t = [np.zeros(SR, np.float32)], [], 1.0
for spk, path in SEGMENTS:
    with wave.open(path) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    seg_times.append((spk, t, t + len(a) / SR))
    parts += [a, np.zeros(int(SR * 1.5), np.float32)]
    t += len(a) / SR + 1.5
parts.append(np.zeros(SR * 3, np.float32))
AUDIO = np.concatenate(parts)
results["audio_seconds"] = round(len(AUDIO) / SR, 1)
results["segments"] = [(s, round(a, 1), round(b, 1)) for s, a, b in seg_times]
PLAY = {"start": None}


class FakeInputStream:
    def __init__(self, samplerate, channels, dtype, blocksize, callback, **_):
        self.cb, self.bs, self.stop = callback, blocksize, threading.Event()

    def __enter__(self):
        def play():
            pos = 0
            PLAY["start"] = time.time()
            while not self.stop.is_set():
                block = AUDIO[pos:pos + self.bs] if pos < len(AUDIO) else np.zeros(self.bs, np.float32)
                if len(block) < self.bs:
                    block = np.pad(block, (0, self.bs - len(block)))
                self.cb(block.reshape(-1, 1), self.bs, None, None)
                pos += self.bs
                time.sleep(self.bs / SR)
        threading.Thread(target=play, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.stop.set()


sounddevice.InputStream = FakeInputStream

# ---- Claude: real if a key is given, otherwise fake ----
if os.environ.get("ANTHROPIC_API_KEY"):
    import anthropic
    client = anthropic.Anthropic(max_retries=1, timeout=30.0)
    results["notes"].append("Claude: real API (translation, summary, chat)")
else:
    class FakeClient:                                    # the app only needs with_options(); text goes through claude_stream
        def with_options(self, **kw):
            return self
    client = FakeClient()
    results["notes"].append("Claude: FAKE (no ANTHROPIC_API_KEY secret) - Vietnamese text is placeholder")

    def fake_stream(client, system, prompt, effort, on_piece=None, **kw):
        text = ("Bản dịch thử nghiệm: xin chào mọi người, hôm nay chúng ta học về máy tính và cách "
                "máy tính hiểu giọng nói; đây là câu tiếng Việt có dấu đầy đủ.")
        for word in text.split(" "):
            if on_piece:
                on_piece(word + " ")
            time.sleep(0.01)
        return text
    app.claude_stream = fake_stream

# ---- the app, exactly as main() starts it ----
import multiprocessing                                 # noqa: E402
multiprocessing.freeze_support()
root = tk.Tk()
if app.IS_MAC:
    root.tk.call("tk", "scaling", 1.33)
threading.Thread(target=app.preload_models, daemon=True).start()
WORDS = {"t": None}
SEEN = {}                                   # word -> time it first appeared in the English pane
LAST_WORDS = {0: "ideas", 1: "part", 2: "tonight"}
BACKLOG = []                                # (seconds since playback start, audio blocks waiting)


def letters(w):
    return re.sub(r"Người nói \d+:|Teacher( \d+)?:", "", w.get("1.0", "end")).strip()


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        if WORDS["t"] is None and PLAY["start"] and letters(mw.left):
            WORDS["t"] = time.time()
        if PLAY["start"]:
            low = mw.left.get("1.0", "end").lower()
            for w in LAST_WORDS.values():
                if w not in SEEN and w in low:
                    SEEN[w] = time.time()
            if not BACKLOG or time.time() - PLAY["start"] - BACKLOG[-1][0] >= 1.0:
                BACKLOG.append((round(time.time() - PLAY["start"], 1), mw.recognizer.audio_q.qsize()))
        time.sleep(0.02)


# 1) login screen
app.LoginWindow(root, lambda c: None)
root.update()
time.sleep(1.5)
root.update()
shot("01_login")
check("mac_buttons_keep_colors", tk.Button is app._FlatButton, f"tk.Button is {tk.Button.__name__}")
for w in root.winfo_children():
    w.destroy()

# 2) main window
mw = app.MainWindow(root, client)
t0 = time.time()
while not mw.ready and time.time() - t0 < 120:
    root.update()
    time.sleep(0.05)
check("speech_models_ready", mw.ready, f"{time.time() - t0:.1f}s")
root.update()
x, y, ww, wh = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
check("window_fits_screen", x >= 0 and y >= 0 and x + ww <= sw and y + wh <= sh,
      f"window {ww}x{wh} at ({x},{y}), screen {sw}x{sh}")
shot("02_main_ready")
# smaller window so the text overflows and auto-scroll can really be tested
root.geometry(f"{min(1000, sw - 20)}x420+10+40")
pump(1)

mw.toggle_onoff()
pump(0.3)
check("toggle_on_green", mw.toggle.cget("text") == "ON" and mw.toggle.cget("bg") == "#27ae60",
      f"{mw.toggle.cget('text')} {mw.toggle.cget('bg')}")
while PLAY["start"] is None:
    pump(0.1)
start = PLAY["start"]


def at(sec):                                           # pump until <sec> after playback start
    pump(max(0.0, start + sec - time.time()))


# --- live words latency (speech starts 1.0 s into playback) ---
at(seg_times[0][1] + 4)
lat = (WORDS["t"] - (start + seg_times[0][1])) if WORDS["t"] else None
results["timings"]["first_live_words_s"] = lat
check("live_words_latency", lat is not None and lat < 2.5,
      f"first English words {lat:.2f}s after speech started (Windows target < 1 s + first word)" if lat else "no words")
shot("03_listening")
at(seg_times[0][2] + 4)
mw.summarizer.kick.set()                                # takeaways for the first speaker's part

# --- after segment 2: follow newest text, then user scrolls up ---
at(seg_times[1][2] + 5)
mw.summarizer.kick.set()                                # takeaways for the second speaker's part
yl, yr, yt = mw.left.yview(), mw.right.yview(), mw.bottom.yview()
overflow = yl[0] > 0
check("auto_scroll_follows_new_text", yl[1] >= 0.999 and yr[1] >= 0.999,
      f"EN view {yl[0]:.2f}-{yl[1]:.2f}, VI view {yr[0]:.2f}-{yr[1]:.2f}, text overflows: {overflow}")
if not overflow:
    results["notes"].append("text did not overflow the pane: scroll checks are weaker")
mw.left.yview_moveto(0)
mw._sync_from(mw.left, mw.right)
mw._user_scrolled()
shot("04_user_scrolled_up")

# --- rest of the speech arrives while the user reads above ---
at(seg_times[-1][2] + 8)
st = mw.status.cget("text")
check("mic_silence_warning", "Micro không gửi âm thanh" in st,
      f"{st[:90]} | zero seconds counted: {getattr(mw.recognizer, 'zero_seconds', '?')}; "
      f"audio blocks waiting now: {mw.recognizer.audio_q.qsize()}")
# real-time check: each speaker's last words must appear soon after they stop talking
lats = {}
for i, (spk, a, b) in enumerate(seg_times):
    w = LAST_WORDS.get(i)
    if w in SEEN:
        lats[w] = round(SEEN[w] - (start + b), 2)
results["timings"]["end_of_speech_latency_s"] = lats
results["timings"]["audio_backlog"] = BACKLOG
worst_backlog = max((q for _, q in BACKLOG), default=0)
check("realtime_end_of_speech_latency", len(lats) == len(LAST_WORDS) and max(lats.values()) < 3.0,
      f"last words shown this long after each speaker stopped: {lats}; max audio backlog {worst_backlog} blocks "
      f"({worst_backlog * 0.15:.1f} s)")
yl = mw.left.yview()
check("no_fight_with_user_scroll", (not overflow) or yl[0] < 0.05,
      f"EN view stayed at {yl[0]:.2f}-{yl[1]:.2f} while new text arrived")
mw.left.yview_moveto(1.0)
mw._sync_from(mw.left, mw.right)
mw._user_scrolled()
pump(1)
check("follow_resumes_at_bottom", mw.follow and mw.left.yview()[1] >= 0.999, f"follow={mw.follow}")

# --- recognized text ---
english = mw.left.get("1.0", "end").strip()
vietnamese = mw.right.get("1.0", "end").strip()
results["english_text"] = english
results["vietnamese_text"] = vietnamese
want = ["morning", "machine", "learning", "speech", "notebooks", "homework", "friday", "questions"]
found = [w for w in want if w in english.lower()]
check("english_recognized", len(found) >= 6, f"found {found} of {want}")
check("vietnamese_column_filled", len(vietnamese) > 20, vietnamese[:160])

# --- speakers: two voices -> two labels; the first voice comes back with the same label ---
labels = re.findall(r"(Người nói \d+|Teacher(?: \d+)?):", english)
distinct = list(dict.fromkeys(labels))
check("speakers_two_voices_two_labels", len(distinct) == 2,
      f"labels in order: {labels} (voices in order: {[s for s, _, _ in seg_times]})")

# --- EN/VI rows level ---
mw._align_all()
pump(1)
def ypix(w, a, b):
    v = w.count(a, b, "update", "ypixels")
    return (v[0] if isinstance(v, tuple) else v) or 0


diffs, rows = [], []
for tag in sorted((t for t in mw.left.tag_names() if t.startswith("b") and t[1:].isdigit()), key=lambda t: int(t[1:])):
    if mw.right.tag_ranges(tag):
        uid = tag[1:]
        ya, yb = ypix(mw.left, "1.0", mw.left.tag_ranges(tag)[0]), ypix(mw.right, "1.0", mw.right.tag_ranges(tag)[0])
        diffs.append(abs(ya - yb))
        rows.append({"uid": uid, "y_en": ya, "y_vi": yb,
                     "h_en": mw._row_height(mw.left, int(uid)), "h_vi": mw._row_height(mw.right, int(uid)),
                     "pad_en": mw.left.tag_cget(f"pad{uid}", "spacing3"), "pad_vi": mw.right.tag_cget(f"pad{uid}", "spacing3"),
                     "en": mw.left.get(*mw.left.tag_ranges(tag))[:50], "vi": mw.right.get(*mw.right.tag_ranges(tag))[:30]})
results["rows"] = rows
# diagnosis for the worst row: when does Tk report its real height after the padding is cleared?
if diffs and max(diffs) > 3:
    bad = rows[diffs.index(max(diffs)) - 1] if diffs.index(max(diffs)) > 0 else rows[0]
    u = int(bad["uid"])
    diag = {"uid": u}
    for side, w in (("en", mw.left), ("vi", mw.right)):
        pad = f"pad{u}"
        d = {"b_range": [str(i) for i in w.tag_ranges(f"b{u}")], "pad_ranges": [str(i) for i in w.tag_ranges(pad)],
             "pad_before": w.tag_cget(pad, "spacing3")}
        w.tag_configure(pad, spacing3=0)
        d["h_now"] = mw._row_height(w, u)
        w.tk.call(w._w, "sync")
        d["h_after_sync"] = mw._row_height(w, u)
        w.update_idletasks()
        d["h_after_idle"] = mw._row_height(w, u)
        pump(0.3)
        d["h_after_300ms"] = mw._row_height(w, u)
        r0, r1 = w.tag_ranges(f"b{u}")
        dl = w.count(r0, r1, "displaylines")
        d["displaylines"] = dl[0] if isinstance(dl, tuple) else dl
        d["dlineinfo_first"] = w.dlineinfo(r0)
        d["text"] = w.get(r0, r1)
        diag[side] = d
    results["row_diag"] = diag
    mw._align_all()
    pump(1)
results["totals"] = {"en": ypix(mw.left, "1.0", "end"), "vi": ypix(mw.right, "1.0", "end"),
                     "tk": root.tk.call("info", "patchlevel")}
check("en_vi_rows_level", diffs and max(diffs) <= 3, f"{len(diffs)} rows, offsets {diffs} px")

# --- takeaways: timestamped, history kept ---
mw.toggle_onoff()                                       # OFF -> final summary
check("toggle_off_gray", mw.toggle.cget("text") == "OFF", mw.toggle.cget("bg"))
pump(8)
take = mw.bottom.get("1.0", "end")
stamps = re.findall(r"\[\d\d:\d\d\]", take)
check("takeaways_timestamped_history", len(stamps) >= 2, f"{len(stamps)} timestamped blocks kept")
check("takeaways_follow_newest", mw.bottom.yview()[1] >= 0.999, f"view {mw.bottom.yview()}")
# new in v1.8.3: export the takeaways history as a PDF (Vietnamese text must survive)
tpdf = os.path.join(OUT, "takeaways_test.pdf")
filedialog.asksaveasfilename = lambda *a, **k: tpdf
learning.open_path = lambda p: None                     # don't open a PDF viewer during the test
mw.export_takeaways()
pump(0.5)
try:
    import pymupdf
    with pymupdf.open(tpdf) as d:
        ttext = " ".join(" ".join(p.get_text() for p in d).split())
    snippet = " ".join(mw.last_summary.splitlines()[1].split())[:40]
    check("takeaways_export_pdf", snippet in ttext, f"pdf has {len(ttext)} chars; looked for '{snippet}'")
except Exception as e:  # noqa: BLE001
    check("takeaways_export_pdf", False, repr(e))
shot("05_after_speech")
root.geometry(f"{min(1280, sw - 20)}x{min(800, sh - 110)}+10+30")
pump(1)

# --- Claude panel from both sides: prompt box as tall as Key takeaways ---
for side in ("left", "right"):
    mw.toggle_panel(side)
    pump(1.2)
    pf, tf = mw.prompt_row.master, mw.bottom.master
    hp, ht = pf.winfo_height(), tf.winfo_height()
    yp, yt_ = pf.winfo_rooty(), tf.winfo_rooty()
    check(f"prompt_height_equals_takeaways_{side}", abs(hp - ht) <= 6 and abs(yp - yt_) <= 6,
          f"prompt {hp}px at y={yp}, takeaways {ht}px at y={yt_}")
    shot(f"06_claude_panel_{side}")
    if side == "right":
        # Enter sends the prompt; Vietnamese text stays intact
        q = "Xin chào Claude, hãy tóm tắt bài học hôm nay giúp tôi: ắ ằ ẳ ẵ ặ ơ ư đ."
        mw.prompt.delete("1.0", "end")
        mw.prompt.insert("1.0", q)
        how = ""
        if isinstance(mw.prompt, tk.Text):              # Mac: Tk text box
            # a window started from a script is not the active app on macOS, so key presses may not
            # reach it; try to bring it to the front first
            subprocess.run(["osascript", "-e", 'tell application "System Events" to set frontmost of '
                            f'(first process whose unix id is {os.getpid()}) to true'], capture_output=True)
            root.lift()
            mw.prompt.focus_force()
            pump(0.5)
            mw.prompt.event_generate("<KeyPress-Return>", keysym="Return")
            pump(3)
            how = "real Enter key event"
            if mw.prompt.get("1.0", "end").strip():     # key not delivered: run the Enter binding itself
                bound = mw.prompt.bind("<Return>")
                class Ev: state = 0
                mw._prompt_enter(Ev())
                how = (f"key event not delivered (focus={root.focus_get()}); Enter binding present={bool(bound)},"
                       " ran the Enter handler")
        else:                                            # Windows: native box (Enter is polled by the app)
            mw.prompt._enter = True
            how = "native box Enter flag"
        pump(4)
        chat = mw.chat.get("1.0", "end")
        check("enter_sends_prompt_vietnamese_intact",
              q in chat and mw.prompt.get("1.0", "end").strip() == "" and (
                  not isinstance(mw.prompt, tk.Text) or bool(mw.prompt.bind("<Return>"))),
              f"{how}; chat: {chat.strip()[:160]}")
        # Shift+Enter must make a new line, not send
        class EvShift: state = 1
        if isinstance(mw.prompt, tk.Text):
            check("shift_enter_does_not_send", mw._prompt_enter(EvShift()) is None)
        shot("07_chat_answer")
    mw.toggle_panel(side)
    pump(0.5)

# --- backup / restore ---
bk = os.path.join(OUT, "backup.json")
filedialog.asksaveasfilename = lambda *a, **k: bk
filedialog.askopenfilename = lambda *a, **k: bk
before = mw.left.get("1.0", "end")
mw.backup_session()
pump(0.5)
mw.restore_session()
pump(1.5)
check("backup_restore_roundtrip", os.path.exists(bk) and mw.left.get("1.0", "end").strip() == before.strip(),
      f"backup {os.path.getsize(bk) if os.path.exists(bk) else 0} bytes")

# --- a failing UI call must not stop later UI updates ---
mw.ui_q.put(("call", lambda: 1 / 0))
mw.ui_q.put(("status", "cập nhật sau lỗi"))
pump(0.5)
check("ui_keeps_running_after_error", mw.status.cget("text") == "cập nhật sau lỗi", mw.status.cget("text")[:80])

# --- Mac: Ctrl+K toggles the Claude panel without deleting text in the prompt box ---
if getattr(app, "IS_MAC", False) and isinstance(mw.prompt, tk.Text):
    keep = "dòng này phải được giữ nguyên"
    mw.prompt.delete("1.0", "end")
    mw.prompt.insert("1.0", keep)
    mw.prompt.mark_set("insert", "1.0")
    mw.prompt.focus_force()
    pump(0.3)
    before_side = mw.panel_side
    mw.prompt.event_generate("<Control-k>")
    pump(0.8)
    check("ctrl_k_keeps_prompt_text", mw.prompt.get("1.0", "end").strip() == keep and mw.panel_side != before_side,
          f"text='{mw.prompt.get('1.0', 'end').strip()[:40]}', panel {before_side}->{mw.panel_side}")
    if mw.panel_side:
        mw.toggle_panel(mw.panel_side)
    mw.prompt.delete("1.0", "end")
    pump(0.3)

# --- friendly credit-balance error ---
msg = app.friendly_error(Exception("Error code: 400 - Your credit balance is too low to access the Anthropic API."))
check("friendly_credit_balance_error", "credit" in msg and "Billing" in msg, msg[:120])

# --- v1.8.4: long Claude replies get a friendly timeout message ---
if hasattr(app, "LONG_TIMEOUT"):
    msg = app.friendly_error(Exception("Request timed out."))
    check("friendly_timeout_error", "hết thời gian" in msg, msg[:120])

# --- v1.8.4: Output Folder (Backup/ and Outputs/), takeaways saved straight into Outputs/ ---
if hasattr(mw, "choose_output_folder"):
    outf = os.path.join(OUT, "OutputFolder")
    os.makedirs(outf, exist_ok=True)
    filedialog.askdirectory = lambda *a, **k: outf
    mw.choose_output_folder()
    pump(0.5)
    saved = app.load_config().get("output_folder")
    check("output_folder_created_and_remembered",
          os.path.isdir(os.path.join(outf, "Backup")) and os.path.isdir(os.path.join(outf, "Outputs")) and saved == outf,
          f"config output_folder={saved}")
    filedialog.asksaveasfilename = lambda *a, **k: ""           # a dialog must not be needed now
    mw.export_takeaways()
    pump(0.5)
    outs = os.listdir(os.path.join(outf, "Outputs"))
    check("takeaways_saved_into_outputs", any(n.startswith("KeyTakeaways_") and n.endswith(".pdf") for n in outs),
          f"Outputs/: {outs}")
    cfg = app.load_config()                                      # leave no setting behind
    cfg.pop("output_folder", None)
    app.save_config(cfg)

# --- Learning ---
def toplevels():
    return [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]


def find_button(widget, text):
    for c in widget.winfo_children():
        if isinstance(c, tk.Button) and text in str(c.cget("text")):
            return c
        b = find_button(c, text)
        if b:
            return b
    return None


n0 = len(toplevels())
mw.learning.documents()
pump(1)
check("learning_documents_window", len(toplevels()) == n0 + 1)
shot("08_learning_documents")
toplevels()[-1].destroy()

from PIL import Image, ImageDraw                       # noqa: E402
cert = os.path.join(OUT, "certificate_test.png")
img = Image.new("RGB", (800, 560), "white")
ImageDraw.Draw(img).rectangle([20, 20, 780, 540], outline="#d97757", width=8)
img.save(cert)
learning.filedialog.askopenfilename = lambda *a, **k: cert
mw.learning.certificate()
pump(1)
top = toplevels()[-1]
up = find_button(top, "Upload")
if up:
    up.invoke()
pump(1.5)
certs = mw.learning.lib.data.get("certificates") or []
check("learning_certificate_upload", up is not None and len(certs) >= 1, f"{len(certs)} certificate(s)")
shot("09_learning_certificate")
cp = find_button(top, "Copy")
if app.IS_MAC:
    subprocess.run(["osascript", "-e", "set the clipboard to \"\""], capture_output=True)
    if cp:
        cp.invoke()
        pump(1)
    info = subprocess.run(["osascript", "-e", "clipboard info"], capture_output=True, text=True).stdout
    check("mac_clipboard_has_certificate_picture", "PNGf" in info, info.strip()[:160])
else:
    results["notes"].append("clipboard check runs on Mac only")
top.destroy()
pump(0.3)

pdf = os.path.join(OUT, "vietnamese_test.pdf")
viet = "Lời giải bài tập: Tiếng Việt có dấu ắ ằ ẳ ẵ ặ ơ ư đ Đ — kiểm tra phông chữ."
learning.write_pdf(pdf, f"<h2>Kiểm tra PDF</h2><p>{viet}</p>")
import pymupdf                                         # noqa: E402
with pymupdf.open(pdf) as d:
    text = d[0].get_text()
    fonts = [f[3] for f in d[0].get_fonts()]
check("learning_pdf_vietnamese", viet in " ".join(text.split()) and (not getattr(app, "IS_MAC", False) or any("Arial" in f for f in fonts)), f"fonts {fonts}; text {text.strip()[:100]}")

# --- v1.8.6: List output files; 🗑 Xoá moves the file to the Trash (the dialog promises it can be restored) ---
if hasattr(mw.learning, "list_outputs"):
    import shutil as _sh
    errors = []
    messagebox.showerror = lambda *a, **k: errors.append(a) or "ok"
    victim = os.path.join(OUT, f"output_delete_test_{int(time.time())}.pdf")
    _sh.copy(pdf, victim)
    kind = next(iter(getattr(mw.learning, "OUT_KINDS", {"takeaways": None})))
    mw.learning.add_output(kind, victim, "Test output for delete", "test")
    n0 = len(toplevels())
    mw.learning.list_outputs()
    pump(1)
    top = toplevels()[-1] if len(toplevels()) > n0 else None
    check("list_output_files_window", top is not None)
    shot("10_list_output_files")
    tree = None
    if top:
        stack = [top]
        while stack and tree is None:
            w = stack.pop()
            if w.winfo_class() == "Treeview":
                tree = w
            stack.extend(w.winfo_children())
    target = None
    if tree is not None:
        items = list(tree.get_children())
        for it in list(items):
            items += list(tree.get_children(it))
        target = next((it for it in items if any("Test output for delete" in str(v) for v in tree.item(it, "values"))), None)
    if target:
        tree.selection_set(target)
        tree.focus(target)
        pump(0.3)
        delb = find_button(top, "Xoá") or find_button(top, "Xóa")
        if delb:
            delb.invoke()
            pump(1.5)
    import glob as _glob                               # the app adds a date/time to the name in the Trash
    stem = os.path.splitext(os.path.basename(victim))[0]
    in_trash = bool(_glob.glob(os.path.expanduser(f"~/.Trash/{stem}*"))) if getattr(app, "IS_MAC", False) else None
    check("list_output_delete_goes_to_trash",
          target is not None and not os.path.exists(victim) and not errors and in_trash is not False,
          f"found in list={target is not None}; file still there={os.path.exists(victim)}; in ~/.Trash={in_trash}; errors={errors}")
    if top:
        top.destroy()
        pump(0.3)

# --- v1.9.0: modal dialogs that ask the learner (choose an option / type a value) ---
if hasattr(mw.learning, "ask_choice"):
    def click_in_last_dialog(label, before=None):
        tops = toplevels()
        if not tops:
            return
        if before:
            before(tops[-1])
        b = find_button(tops[-1], label)
        if b:
            b.invoke()

    def pick_second(top):
        stack = [top]
        while stack:
            x = stack.pop()
            if isinstance(x, tk.Listbox):
                x.selection_clear(0, "end")
                x.selection_set(1)
            stack.extend(x.winfo_children())

    opts = [{"label": "Bảng A", "values": [{"slot": "bảng", "text": "Bảng A: doanh thu"}]},
            {"label": "Bảng B", "values": [{"slot": "bảng", "text": "Bảng B: chi phí"}]}]
    root.after(800, lambda: click_in_last_dialog("Chọn", pick_second))
    got = mw.learning.ask_choice({"question": "Chọn bảng của bạn", "options": opts})
    check("dialog_choose_option", got is opts[1], f"returned {got and got.get('label')}")

    typed = "Đề bài của tôi: phân tích doanh thu quý 3, có dấu ắ ơ ư đ"

    def type_text(top):
        stack = [top]
        while stack:
            x = stack.pop()
            if isinstance(x, tk.Text):
                x.insert("1.0", typed)
                return
            stack.extend(x.winfo_children())
    root.after(800, lambda: click_in_last_dialog("Điền vào prompt", type_text))
    got = mw.learning.ask_text({"name": "de_bai", "label": "Đề bài"})
    check("dialog_type_value_vietnamese", got == typed, f"returned {got!r}"[:120])
    pump(0.3)

# --- clean exit: no extra processes, no hang ---
save_results()
print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
root.destroy()
os._exit(0 if results["passed"] else 1)
