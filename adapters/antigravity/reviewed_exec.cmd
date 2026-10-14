@echo off
"%~dp0.venv\Scripts\python.exe" "%~dp0approved_exec.py" %*
exit /b %errorlevel%
