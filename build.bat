@echo off
rem Builds the portable app dist\LiveTranslatorML_v<version>.exe - ONE file to copy to other PCs.
rem (Multilingual offline edition: no Learning menu; language packs are downloaded inside the app.)
rem First run unpacks it once to %%LOCALAPPDATA%%\LiveTranslatorML\v<version> (~20 s); later runs open in ~3 s.
rem Also leaves dist\LiveTranslatorML_v<version>\ (app folder) and a .zip of it.
rem The version comes from APP_VERSION in app.py. Bundled: English speech (Whisper base.en, Vosk) + voiceprints.
rem Optional: set BUNDLE_PACKS=nllb-600m qwen2.5-1.5b ...  to also put downloaded packs (models\<id>) inside the
rem build, for PCs that will never have internet (the exe gets that much bigger).
cd /d "%~dp0"
setlocal enabledelayedexpansion
for /f tokens^=2^ delims^=^" %%v in ('findstr /b /c:"APP_VERSION" app.py') do set VER=%%v
if "%VER%"=="" (echo Could not read APP_VERSION from app.py & exit /b 1)
set APPNAME=LiveTranslatorML
set NAME=%APPNAME%_v%VER%
echo Building %NAME% ...
set ADD=--add-data "models\base.en;models\base.en" --add-data "models\vosk-en-small;models\vosk-en-small" --add-data "models\spk-onnx;models\spk-onnx"
for %%p in (%BUNDLE_PACKS%) do set ADD=!ADD! --add-data "models\%%p;models\%%p"
if exist vendor\ant.exe set ADD=%ADD% --add-binary "vendor\ant.exe;vendor"
.venv\Scripts\python -m PyInstaller --noconfirm --clean --onedir --windowed --name %NAME% ^
  --contents-directory app_files ^
  %ADD% --collect-all ctranslate2 --collect-all vosk --collect-all sherpa_onnx --hidden-import native_edit ^
  --hidden-import local_ai --hidden-import pdf_export --collect-all pymupdf --collect-all tokenizers ^
  --collect-data faster_whisper ^
  --exclude-module onnxruntime --exclude-module torch --exclude-module webview --exclude-module learning app.py || exit /b 1
rem Single-file exe (launcher + app zip) and a plain zip of the same app
.venv\Scripts\python launcher\pack.py %VER% %APPNAME% || exit /b 1
echo Done: dist\%NAME%.exe  (one file - copy this to other PCs)
