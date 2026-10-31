@echo off
cd /d "%~dp0"
start "" pythonw server.py --seed
timeout /t 2 /nobreak >nul
start "" "C://Program Files (x86)//Microsoft//Edge//Application//msedge.exe" --app=http://127.0.0.1:8790/ --window-size=1280,860
