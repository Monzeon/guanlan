@echo off
rem Full regression suite. Usage: tools\run_tests.cmd
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
python -m unittest discover -s tests -v
set RC=%ERRORLEVEL%
if "%RC%"=="0" (echo [guanlan] tests OK) else (echo [guanlan] tests FAILED, exit code %RC%)
exit /b %RC%
