@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe (
  echo Birinchi ishga tushirish: virtual muhit yaratilmoqda...
  py -3 -m venv .venv || python -m venv .venv
)
echo Kutubxonalar tekshirilmoqda...
.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
.venv\Scripts\python.exe bot.py
echo.
echo Bot to'xtadi. Yuqoridagi xabarni o'qing.
pause
