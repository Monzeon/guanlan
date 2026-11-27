@echo off
rem Guanlan console launcher. Prefer the packaged exe (self-contained).
rem Desktop shortcut uses launch.vbs instead (no console window).
rem
rem   start.cmd           start
rem   start.cmd stop      stop background service
rem   start.cmd status    show status
cd /d "%~dp0"

if /i "%~1"=="stop" goto :stop
if /i "%~1"=="status" goto :status

rem Prefer dist\<folder>\<exe>; fall back to source mode entry.py.
for /d %%D in ("%~dp0dist\*") do (
  for %%F in ("%%D\*.exe") do (
    start "" "%%F"
    exit /b 0
  )
)

pythonw.exe "%~dp0entry.py"
exit /b 0

:stop
python.exe "%~dp0entry.py" --stop
exit /b

:status
python.exe "%~dp0entry.py" --status
exit /b
