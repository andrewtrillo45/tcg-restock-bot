@echo off
REM Runs the bot on Windows. First time: copy .env.example to .env and fill it in.
cd /d "%~dp0"
python -m pip install -r requirements.txt
python bot.py
pause
