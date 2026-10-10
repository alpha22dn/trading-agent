@echo off
setlocal
cd /d "%~dp0"
echo.
echo === Trading Agent setup ===
echo.

rem --- Python 3.12+ (prefers the py launcher) ---
set "PY="
where py >nul 2>nul && (
  py -3.13 -c "import sys" >nul 2>nul && set "PY=py -3.13"
  if not defined PY py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.12 or 3.13 is required. Install it from https://www.python.org/downloads/windows/
  echo and tick "Add python.exe to PATH". Then run setup.bat again.
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo Your Python is too old. Install Python 3.12 or 3.13 and run setup.bat again.
  exit /b 1
)
echo Using: %PY%

rem --- virtual environment + packages ---
if not exist .venv (
  %PY% -m venv .venv || exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet || (echo Package install failed. & exit /b 1)

rem --- .env ---
if not exist .env (
  copy .env.example .env >nul
  echo Created .env - add your Kite API key and secret to it.
)

rem --- dashboard (needs Node.js; a prebuilt copy is included) ---
where npm >nul 2>nul
if errorlevel 1 (
  echo Node.js not found - using the prebuilt dashboard in dashboard\dist.
) else (
  pushd dashboard
  call npm install --silent && call npm run build --silent
  popd
)

rem --- database ---
python -m agent.cli init || exit /b 1

echo.
echo Setup complete. Next:
echo   1. Open .env and fill in KITE_API_KEY and KITE_API_SECRET
echo   2. Double-click run.bat
echo.
endlocal
