@echo off
setlocal
cd /d "%~dp0"
call :resolve_python
if not defined PYTHON_CMD goto :python_missing

if not exist "%~dp0superparser_modular\.venv\Scripts\python.exe" (
  %PYTHON_CMD% -m venv "%~dp0superparser_modular\.venv"
  if errorlevel 1 exit /b %errorlevel%
)
set "PYTHON_CMD="%~dp0superparser_modular\.venv\Scripts\python.exe""

%PYTHON_CMD% -m pip install --require-hashes -r requirements.lock
if errorlevel 1 exit /b %errorlevel%

%PYTHON_CMD% -m pip install --no-deps -e superparser_modular
if errorlevel 1 exit /b %errorlevel%

cd /d "%~dp0frontend"
npm.cmd ci
if errorlevel 1 exit /b %errorlevel%
npm.cmd run build
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
pause
exit /b 1
