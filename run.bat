@echo off
cd /d %~dp0
.venv\Scripts\python run.py >> data\run-%date:~0,4%%date:~5,2%%date:~8,2%.log 2>&1
