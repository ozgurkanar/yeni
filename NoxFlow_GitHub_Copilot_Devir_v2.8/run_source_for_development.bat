@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3.12 -m venv .venv 2>nul || py -3.11 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements_runtime.txt
python runtime_panel.py
