@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Устанавливаю библиотеки (один раз)...
py -3 -m pip install -q -r requirements.txt
py -3 server.py
pause
