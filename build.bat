@echo off
rem Builds the portable app dist\LiveTranslator_v<version>.exe - ONE file to copy to other PCs.
rem First run unpacks it once to %%LOCALAPPDATA%%\LiveTranslator\v<version> (~20 s); later runs open in ~3 s.
rem Also leaves dist\LiveTranslator_v<version>\ (app folder) and a .zip of it.
rem The version comes from APP_VERSION in app.py.
cd /d "%~dp0"
for /f tokens^=2^ delims^=^" %%v in ('findstr /b /c:"APP_VERSION" app.py') do set VER=%%v
if "%VER%"=="" (echo Could not read APP_VERSION from app.py & exit /b 1)
set NAME=LiveTranslator_v%VER%
echo Building %NAME% ...
set ADD=--add-data "models\base.en;models\base.en" --add-data "models\vosk-en-small;models\vosk-en-small" --add-data "models\spk-onnx;models\spk-onnx"
if exist vendor\ant.exe set ADD=%ADD% --add-binary "vendor\ant.exe;vendor"
.venv\Scripts\python -m PyInstaller --noconfirm --clean --onedir --windowed --name %NAME% ^
  --contents-directory app_files ^
  %ADD% --collect-all ctranslate2 --collect-all vosk --collect-all sherpa_onnx --hidden-import native_edit --hidden-import learning --collect-all pymupdf ^
  --hidden-import source_browser --collect-all webview --collect-all pythonnet --collect-all clr_loader --hidden-import clr ^
  --collect-data faster_whisper ^
  --exclude-module onnxruntime --exclude-module torch app.py || exit /b 1
rem Single-file exe (launcher + app zip) and a plain zip of the same app
.venv\Scripts\python launcher\pack.py %VER% || exit /b 1
echo Done: dist\%NAME%.exe  (one file - copy this to other PCs)
