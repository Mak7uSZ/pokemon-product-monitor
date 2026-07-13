@echo off
setlocal
cd /d "%~dp0"
call :resolve_python
if not defined PYTHON_CMD goto :python_missing

set "PYTHONPATH=%CD%\superparser_modular\src"
cd /d "%~dp0superparser_modular"
%PYTHON_CMD% -m uvicorn pokemon_parser.api.app:app --host 127.0.0.1 --port 8000
exit /b %errorlevel%

:resolve_python
if exist "%~dp0superparser_modular\.venv\Scripts\python.exe" (
  set "PYTHON_CMD="%~dp0superparser_modular\.venv\Scripts\python.exe""
)
if not defined PYTHON_CMD (
  py -3.12 -c "import sys" >nul 2>nul && set "PYTHON_CMD=py -3.12"
)
if not defined PYTHON_CMD (
  py -3.11 -c "import sys" >nul 2>nul && set "PYTHON_CMD=py -3.11"
)
if not defined PYTHON_CMD (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PYTHON_CMD=python"
)
exit /b 0

:python_missing
echo Python 3.11 or newer was not found.
echo Install Python, then run install_dependencies.bat and try again.
pause
exit /b 1
