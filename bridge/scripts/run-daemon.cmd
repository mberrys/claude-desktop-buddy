@echo off
REM Starts the bridge daemon. Put a shortcut to this in shell:startup to
REM have it come up with Windows.
setlocal
cd /d "%~dp0.."
python -m cursor_buddy daemon %*
