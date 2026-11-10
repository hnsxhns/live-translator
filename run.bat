@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  echo Setting up for first run...
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
start "" .venv\Scripts\pythonw.exe app.py
