@echo off
rem Daily entry point for the scheduled task: collect, render, and keep a dated log.
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%d
if not "%~1"=="" set JEV_PYTHON=%~1
if "%JEV_PYTHON%"=="" set JEV_PYTHON=python
set PYTHONIOENCODING=utf-8
"%JEV_PYTHON%" collector.py >> "logs\%TODAY%.log" 2>&1
set RC=%ERRORLEVEL%
rem keep two weeks of logs
forfiles /p logs /m *.log /d -14 /c "cmd /c del @path" >nul 2>&1
exit /b %RC%
