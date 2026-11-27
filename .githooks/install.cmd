@echo off
rem One-time setup: run the pre-commit test gate from .githooks
setlocal
cd /d "%~dp0.."
git config core.hooksPath .githooks
if errorlevel 1 goto :fail
echo hooks installed:
git config --get core.hooksPath
echo Undo with: git config --unset core.hooksPath
exit /b 0
:fail
echo could not set core.hooksPath
exit /b 1
