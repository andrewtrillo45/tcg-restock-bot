@echo off
REM ---- Walmart helper: runs ONLY the Walmart check from this PC (home IP gets through Walmart's bot wall). ----
REM First time: 1) install Python 3.11+ from python.org (tick "Add to PATH")
REM             2) copy .env.example to .env and fill in POKEMON_WEBHOOK / YUGIOH_WEBHOOK / FUNKO_WEBHOOK
REM             3) double-click this file once to test (window shows the scan summary)
REM             4) run install-walmart-helper.bat to make Windows run it every 10 minutes
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
set SOURCES=walmart
set STATE_DIR=state-walmart
.venv\Scripts\python scan_once.py
