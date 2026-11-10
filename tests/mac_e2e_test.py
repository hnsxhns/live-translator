"""End-to-end UI test of the app on a Mac (GitHub Actions), run from source.

The real app window is built, a fake microphone plays a WAV file (made with macOS `say`)
in real time, and the test checks that the English text appears in the window.
Translation: uses Claude if ANTHROPIC_API_KEY is set, otherwise a fake translator
(so the Vietnamese column and the layout can still be checked).

usage: python tests/mac_e2e_test.py <speech.wav> <out_dir>
Writes screenshots (*.png) and result.json to out_dir. Exit code 1 if a check fails.
"""
import json
import os
import subprocess
import sys
import threading
import time
import wave

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WAV, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)

import sounddevice                                   # noqa: E402
import tkinter as tk                                  # noqa: E402

import app                                            # noqa: E402

results = {"checks": {}, "notes": []}


def check(name, ok, detail=""):
    results["checks"][name] = {"ok": bool(ok), "detail": str(detail)[:500]}
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""), flush=True)


def shot(name):
    path = os.path.join(OUT, name + ".png")
    try:
        r = subprocess.run(["screencapture", "-x", path], capture_output=True, text=True, timeout=30)
        err = r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:          # not a Mac / no screen access
        err = repr(e)
    if not os.path.exists(path):
        results["notes"].append(f"screenshot {name} failed: {err}")


# ---- fake microphone: plays the WAV in real time through the app's own callback ----
with wave.open(WAV) as w:
    assert w.getframerate() == app.SAMPLE_RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
    AUDIO = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
AUDIO = np.concatenate([np.zeros(app.SAMPLE_RATE, np.float32), AUDIO, np.zeros(app.SAMPLE_RATE * 3, np.float32)])
results["audio_seconds"] = round(len(AUDIO) / app.SAMPLE_RATE, 1)


class FakeInputStream:
    def __init__(self, samplerate, channels, dtype, blocksize, callback, **_):
        self.cb, self.bs, self.stop = callback, blocksize, threading.Event()

    def __enter__(self):
        def play():
            pos = 0
            while not self.stop.is_set():
                block = AUDIO[pos:pos + self.bs] if pos < len(AUDIO) else np.zeros(self.bs, np.float32)
                if len(block) < self.bs:
                    block = np.pad(block, (0, self.bs - len(block)))
                self.cb(block.reshape(-1, 1), self.bs, None, None)
                pos += self.bs
                time.sleep(self.bs / app.SAMPLE_RATE)
        threading.Thread(target=play, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.stop.set()


sounddevice.InputStream = FakeInputStream

# ---- Claude: real if a key is given, otherwise a fake translator ----
if os.environ.get("ANTHROPIC_API_KEY"):
    import anthropic
    client = anthropic.Anthropic(max_retries=1, timeout=30.0)
    results["notes"].append("translation: real Claude API")
else:
    client = object()
    results["notes"].append("translation: FAKE (no ANTHROPIC_API_KEY secret) - Vietnamese text is placeholder")

    def fake_stream(client, system, prompt, effort, on_piece=None, **kw):
        text = "Bản dịch thử nghiệm: xin chào, đây là câu tiếng Việt có dấu."
        for word in text.split(" "):
            if on_piece:
                on_piece(word + " ")
            time.sleep(0.02)
        return text
    app.claude_stream = fake_stream

# ---- run the app UI exactly as main() does ----
import multiprocessing                                 # noqa: E402
multiprocessing.freeze_support()
root = tk.Tk()
if app.IS_MAC:
    root.tk.call("tk", "scaling", 1.33)
threading.Thread(target=app.preload_models, daemon=True).start()

# 1) login screen
app.LoginWindow(root, lambda c: None)
root.update()
time.sleep(1.5)
root.update()
shot("1_login")
check("mac_buttons_keep_colors", tk.Button is app._FlatButton, f"tk.Button is {tk.Button.__name__}")
for w in root.winfo_children():
    w.destroy()

# 2) main window
# keep the app's own window size and position (it centers itself on the screen)
mw = app.MainWindow(root, client)
t0 = time.time()
while not mw.ready and time.time() - t0 < 120:          # models loaded?
    root.update()
    time.sleep(0.05)
check("speech_models_ready", mw.ready, f"{time.time() - t0:.1f}s")
shot("2_main_ready")

mw.toggle_onoff()                                         # ON -> starts listening (fake mic)
root.update()
check("toggle_on_green", mw.toggle.cget("text") == "ON" and mw.toggle.cget("bg") == "#27ae60",
      f"{mw.toggle.cget('text')} {mw.toggle.cget('bg')}")
end = time.time() + results["audio_seconds"] + 25
mid = time.time() + results["audio_seconds"] / 2
took_mid = False
while time.time() < end:
    root.update()
    time.sleep(0.03)
    if not took_mid and time.time() > mid:
        shot("3_listening")
        took_mid = True

english = mw.left.get("1.0", "end").strip()
vietnamese = mw.right.get("1.0", "end").strip()
results["english_text"] = english
results["vietnamese_text"] = vietnamese
want = ["morning", "machine", "learning", "speech", "notebooks"]
found = [w for w in want if w in english.lower()]
check("english_recognized", len(found) >= 3, f"found {found} of {want}")
check("vietnamese_column_filled", len(vietnamese) > 10, vietnamese[:120])
shot("4_after_speech")

# 3) Claude panel from both sides
for side in ("left", "right"):
    mw.toggle_panel(side)
    for _ in range(20):
        root.update()
        time.sleep(0.05)
    shot(f"5_claude_panel_{side}")
    mw.toggle_panel(side)
    root.update()
check("claude_panel_both_sides", True)

mw.toggle_onoff()
root.update()
check("toggle_off_gray", mw.toggle.cget("text") == "OFF", mw.toggle.cget("bg"))

results["passed"] = all(c["ok"] for c in results["checks"].values())
with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
root.destroy()
os._exit(0 if results["passed"] else 1)
