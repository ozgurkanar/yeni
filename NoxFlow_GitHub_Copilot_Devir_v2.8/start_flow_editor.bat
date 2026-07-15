@echo off
cd /d "%~dp0"
set "PYTHONPATH=%CD%"
py -3 -m tools.flow_editor
if errorlevel 1 pause
