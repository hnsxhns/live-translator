"""End-to-end test with a fake microphone: speech from a WAV file goes through the real app window
(recognition -> translation -> key takeaways, all on this PC, no internet needed).

usage: python tests/e2e_test.py <speech.wav> <src lang> <tgt lang> [screenshot.png] [--offline]
  --offline: block every network connection inside the app process (proves nothing needs the internet)
Prints what each pane shows, the delays, and PASS/FAIL checks. Exit code 0 = all checks passed.
"""
import os
import sys
import threading
import time
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
WAV, SRC, TGT = sys.argv[1], sys.argv[2], sys.argv[3]
SHOT = next((a for a in sys.argv[4:] if a.endswith(".png")), None)
OFFLINE = "--offline" in sys.argv

if OFFLINE:                                       # any attempt to reach the network fails loudly
    import socket
    NET_TRIES = []

    def _blocked(*a, **k):
        NET_TRIES.append(a[1] if len(a) > 1 else a)
        raise OSError("network blocked by the offline test")
    socket.socket.connect = _blocked
    socket.create_connection = _blocked


def load_wav(path):
    with wave.open(path) as w:
        sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        data = np.frombuffer(w.readframes(n), dtype=np.int16 if sw == 2 else np.int8).astype(np.float32)
    data = data.reshape(-1, ch).mean(axis=1) / (32768.0 if sw == 2 else 128.0)
    if sr != 16000:
        t = np.arange(0, len(data) / sr, 1 / 16000)
        data = np.interp(t, np.arange(len(data)) / sr, data).astype(np.float32)
    return data


speech = load_wav(WAV)
rng = np.random.default_rng(1)
audio = np.concatenate([np.zeros(16000), speech * 0.7, np.zeros(16000 * 4)]).astype(np.float32)
audio += rng.normal(0, 0.002, len(audio)).astype(np.float32)        # a quiet room, not digital silence
SPEECH_END = (16000 + len(speech)) / 16000


class FakeStream:
    """sounddevice.InputStream that plays the WAV in real time, then quiet room noise."""
    def __init__(self, samplerate, channels, dtype, blocksize, callback):
        self.cb, self.bs = callback, blocksize
        self.stop = threading.Event()

    def __enter__(self):
        FakeStream.t0 = time.time()

        def run():
            i = 0
            while not self.stop.is_set():
                block = audio[i:i + self.bs] if i < len(audio) else rng.normal(0, 0.002, self.bs).astype(np.float32)
                if len(block) < self.bs:
                    block = np.concatenate([block, np.zeros(self.bs - len(block), np.float32)])
                self.cb(block.reshape(-1, 1), self.bs, None, None)
                i += self.bs
                time.sleep(self.bs / 16000)
        threading.Thread(target=run, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.stop.set()


import sounddevice  # noqa: E402
sounddevice.InputStream = FakeStream
import tkinter as tk  # noqa: E402
from tkinter import messagebox  # noqa: E402

import app  # noqa: E402

ASKED = []
messagebox.askyesno = lambda title, msg, **k: (ASKED.append(title), False)[1]
messagebox.showwarning = lambda title, msg, **k: ASKED.append(title)
messagebox.showinfo = lambda title, msg, **k: ASKED.append(title)
if os.path.exists(os.path.join(app.DATA_DIR, app.AUTOSAVE_FILE)):
    os.remove(os.path.join(app.DATA_DIR, app.AUTOSAVE_FILE))

root = tk.Tk()
threading.Thread(target=app.preload_models, args=(SRC,), daemon=True).start()
mw = app.MainWindow(root, None)
events = {"draft": {}, "vi": {}, "final": {}}
_orig_draft, _orig_piece, _orig_fin = mw._add_draft, mw._vi_piece, mw._finalize


def rel():
    return time.time() - getattr(FakeStream, "t0", time.time())


mw._add_draft = lambda uid, text: (events["draft"].setdefault(uid, rel()), _orig_draft(uid, text))
mw._vi_piece = lambda uid, g, text: (events["vi"].setdefault(uid, rel()), _orig_piece(uid, g, text))
mw._finalize = lambda ids, text: ([events["final"].setdefault(u, rel()) for u in ids], _orig_fin(ids, text))
def window_png(win, path):
    """Picture of the window itself (PrintWindow: works even when the screen is locked or covered)."""
    import ctypes
    from ctypes import wintypes
    from PIL import Image
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = u.GetAncestor(win.winfo_id(), 2)
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = u.GetWindowDC(hwnd)
    mem = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    g.SelectObject(mem, bmp)
    u.PrintWindow(hwnd, mem, 2)

    class BIH(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("w", ctypes.c_int32), ("h", ctypes.c_int32),
                    ("planes", ctypes.c_uint16), ("bits", ctypes.c_uint16), ("comp", ctypes.c_uint32),
                    ("isize", ctypes.c_uint32), ("xp", ctypes.c_int32), ("yp", ctypes.c_int32),
                    ("used", ctypes.c_uint32), ("imp", ctypes.c_uint32)]
    bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bih), 0)
    Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB").save(path)
    g.DeleteObject(bmp)
    g.DeleteDC(mem)
    u.ReleaseDC(hwnd, hdc)


state = {"phase": "wait"}
T0 = time.time()


def step():
    if state["phase"] == "wait" and time.time() - T0 > 1.5:
        mw.set_languages(SRC, TGT)
        state["phase"] = "ready"
    elif state["phase"] == "ready" and mw.ready:
        mw.toggle_onoff()
        state["phase"], state["on"] = "on", time.time()
    elif state["phase"] == "on" and time.time() - state["on"] > SPEECH_END + 8:
        mw.toggle_onoff()                           # OFF -> final takeaways for the last words
        state["phase"], state["off"] = "off", time.time()
    elif state["phase"] == "off" and (mw.last_summary or time.time() - state["off"] > 150):
        if SHOT:
            try:
                root.update()
                window_png(root, SHOT)
            except Exception as e:  # noqa: BLE001
                print("screenshot failed:", e)
        state["phase"] = "done"
        state["left"] = mw.left.get("1.0", "end").strip()
        state["right"] = mw.right.get("1.0", "end").strip()
        root.after(200, root.destroy)
        return
    root.after(200, step)


root.after(500, step)
root.mainloop()

left, right = state.get("left", ""), state.get("right", "")
take = mw.last_summary.strip()
print("==== source pane ====\n" + left)
print("==== translation pane ====\n" + right)
print("==== key takeaways ====\n" + take)
print("==== delays (s after the fake mic started; speech ends at %.1f) ====" % SPEECH_END)
for uid in sorted(events["draft"]):
    print(f"row {uid}: words {events['draft'].get(uid, 0):.1f}  translation starts {events['vi'].get(uid, 0):.1f}  "
          f"accurate text {events['final'].get(uid, 0):.1f}")
lags = [events["vi"][u] - events["draft"][u] for u in events["draft"] if u in events["vi"]]
checks = [
    ("speech recognised", len(left) > 20),
    ("translated", len(right) > 20 and "[chưa" not in right),
    ("translation is not the source text", SRC == TGT or right[:40] != left[:40]),
    ("translation starts < 1.5 s after the row's words appear", bool(lags) and max(lags) < 1.5),
    ("key takeaways written", take.count("•") >= 1),
    ("no dialog popped up", not ASKED),
]
if OFFLINE:
    checks.append(("no network connection attempted", not NET_TRIES))
print("==== checks ====")
for name, ok in checks:
    print(("PASS " if ok else "FAIL ") + name)
if ASKED:
    print("dialogs:", ASKED)
if OFFLINE and NET_TRIES:
    print("network tries:", NET_TRIES[:5])
sys.exit(0 if all(ok for _, ok in checks) else 1)
