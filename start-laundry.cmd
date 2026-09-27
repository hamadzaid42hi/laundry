@echo off
cd /d "%~dp0"
set "BUNDLED_PYTHON=C:\Users\hamad\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%BUNDLED_PYTHON%" (
  "%BUNDLED_PYTHON%" server.py --port 8875
) else (
  py -3 server.py --port 8875
)
