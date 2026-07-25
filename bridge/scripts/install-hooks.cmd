@echo off
REM Installs the Cursor hooks for every workspace (writes %USERPROFILE%\.cursor\hooks.json).
setlocal
cd /d "%~dp0.."
python -m cursor_buddy install --user
pause
