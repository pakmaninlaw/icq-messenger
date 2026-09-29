@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -m pip install -q -r requirements.txt
start "" pyw -3 client_desktop.py
