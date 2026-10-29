@echo off
setlocal
cd /d "%~dp0"
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
start "" /min "..\.venv\Scripts\pythonw.exe" "server.py" --host 127.0.0.1 --port 7100
powershell -NoProfile -Command "$ok=$false; 1..20 | ForEach-Object { try { $r=Invoke-RestMethod 'http://127.0.0.1:7100/api/health' -TimeoutSec 1; if ($r.ok) { $ok=$true; return } } catch {}; Start-Sleep -Milliseconds 300 }; if (-not $ok) { exit 1 }"
if errorlevel 1 exit /b 1
start "" "%EDGE%" --app="http://127.0.0.1:7100/"
