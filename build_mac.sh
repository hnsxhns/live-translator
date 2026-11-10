#!/bin/bash
# Builds the portable Mac app: dist/LiveTranslator_v<version>_mac.dmg - ONE file to copy to other Macs.
# Normally built by GitHub Actions (.github/workflows/build-mac.yml); this script also works on any Mac.
# Must be run ON A MAC (PyInstaller cannot build Mac apps from Windows).
#
#   1. Copy the whole project folder to the Mac, including models/ (and vendor/ant if you have the Mac build of `ant`).
#   2. Install Python 3.12 from python.org (it includes Tk; Homebrew Python needs `brew install python-tk@3.12`).
#   3. In Terminal:   cd <project folder>  &&  bash build_mac.sh
#
# The app is built for the Mac's own chip: build on Apple Silicon -> runs on M1/M2/M3/M4 Macs;
# build on an Intel Mac -> runs on Intel Macs.
# The version comes from APP_VERSION in app.py. Old builds in dist/ are never deleted.
set -euo pipefail
cd "$(dirname "$0")"

VER=$(grep -m1 '^APP_VERSION' app.py | cut -d'"' -f2)
[ -n "$VER" ] || { echo "Could not read APP_VERSION from app.py"; exit 1; }
NAME="LiveTranslator_v${VER}_mac"
PY="${PYTHON:-python3}"
echo "Building $NAME ($(uname -m)) ..."

for m in base.en vosk-en-small spk-onnx; do
  [ -d "models/$m" ] || { echo "Missing models/$m - copy the models folder from the Windows PC."; exit 1; }
done

# Separate Mac virtualenv (the .venv folder is the Windows one)
if [ ! -x .venv-mac/bin/python ]; then
  "$PY" -c "import tkinter" 2>/dev/null || { echo "This Python has no Tkinter. Install Python 3.12 from python.org."; exit 1; }
  "$PY" -m venv .venv-mac
  .venv-mac/bin/python -m pip install --upgrade pip
  .venv-mac/bin/python -m pip install -r requirements.txt pyinstaller
fi

ADD=(--add-data "$PWD/models/base.en:models/base.en"
     --add-data "$PWD/models/vosk-en-small:models/vosk-en-small"
     --add-data "$PWD/models/spk-onnx:models/spk-onnx")
if [ -f vendor/ant ]; then
  chmod +x vendor/ant
  ADD+=(--add-binary "$PWD/vendor/ant:vendor")
else
  echo "Note: vendor/ant not found - the 'Sign in with Claude' button will use ant from PATH if installed; API-key login always works."
fi

.venv-mac/bin/python -m PyInstaller --noconfirm --clean --onedir --windowed --name "$NAME" \
  --osx-bundle-identifier com.livetranslator.app \
  --distpath dist --workpath build/mac --specpath build/mac \
  "${ADD[@]}" --collect-all ctranslate2 --collect-all vosk --collect-all sherpa_onnx \
  --collect-data faster_whisper --exclude-module onnxruntime --exclude-module torch \
  "$PWD/app.py"

APP="dist/$NAME.app"
PLIST="$APP/Contents/Info.plist"
# macOS blocks the microphone unless the app says why it needs it
/usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string 'Live Translator nghe micro để dịch lời nói tiếng Anh sang tiếng Việt.'" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :NSMicrophoneUsageDescription 'Live Translator nghe micro để dịch lời nói tiếng Anh sang tiếng Việt.'" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string 'Live Translator'" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || true

# Info.plist changed -> re-sign (ad-hoc; no Apple developer account needed)
codesign --force --deep --sign - "$APP"

# Quick check that the bundled speech models load (stop the build if they do not)
SELFTEST="$HOME/Library/Application Support/LiveTranslator/selftest.txt"
rm -f "$SELFTEST"
"$APP/Contents/MacOS/$NAME" --selftest &
PID=$!
for _ in $(seq 300); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done   # at most 5 minutes
if kill -0 "$PID" 2>/dev/null; then
  echo "Selftest hung (still running after 5 minutes)"
  pkill -f "$APP/Contents/MacOS/" || true
  exit 1
fi
sleep 3
# The app must not leave extra copies of itself running (that happened once on Mac)
if pgrep -f "$APP/Contents/MacOS/" >/dev/null; then
  echo "Selftest left app processes running:"; pgrep -fl "$APP/Contents/MacOS/" || true
  pkill -f "$APP/Contents/MacOS/" || true
  exit 1
fi
echo "--- selftest ---"; cat "$SELFTEST" 2>/dev/null || echo "(no selftest.txt)"; echo
grep -q '^OK' "$SELFTEST" 2>/dev/null || { echo "Selftest failed"; exit 1; }
if [ -f vendor/ant ]; then   # the "Sign in with Claude" tool must be inside the app, not just on this Mac
  grep -q "^ant: .*\.app/.*vendor/ant$" "$SELFTEST" || { echo "Selftest: bundled ant not found"; exit 1; }
fi

# ONE file to copy to other Macs: a .dmg with the app and an Applications shortcut
STAGE="build/mac/dmg"
rm -rf "$STAGE" && mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Live Translator.app"
ln -s /Applications "$STAGE/Applications"
DMG="dist/$NAME.dmg"
rm -f "$DMG"
hdiutil create -volname "Live Translator $VER" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
echo "Done: $DMG  ($(du -h "$DMG" | cut -f1))"
echo "On the other Mac: open the .dmg, drag Live Translator into Applications, double-click it."
echo "First time only, if macOS blocks it: System Settings > Privacy & Security > Open Anyway."
