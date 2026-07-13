@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0logs" mkdir "%~dp0logs"

call :resolve_python
if not defined PYTHON_CMD goto :python_missing

set "PYTHONPATH=%CD%\superparser_modular\src"
cd /d "%~dp0superparser_modular"
%PYTHON_CMD% -m uvicorn pokemon_parser.api.app:app --host 127.0.0.1 --port 8000 >> "%~dp0logs\backend.log" 2>&1
exit /b %errorlevel%

:resolve_python
if exist "%~dp0superparser_modular\.venv\Scripts\python.exe" (
  set "PYTHON_CMD="%~dp0superparser_modular\.venv\Scripts\python.exe""
)
if not defined PYTHON_CMD (
  where python >nul 2>nul && set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
  where py >nul 2>nul && set "PYTHON_CMD=py -3.11"
)
if not defined PYTHON_CMD (
  where py >nul 2>nul && set "PYTHON_CMD=py -3.12"
)
exit /b 0

:python_missing
>> "%~dp0logs\backend.log" echo [%date% %time%] Python 3.11 or newer was not found. Hidden backend launch aborted.
exit /b 1
