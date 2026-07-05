@echo off
setlocal

REM ChiCTR crawl-only one-click runner.
REM Run this from cmd, double-click it, or use it as an External Tool in PyCharm.
REM It only crawls raw ChiCTR list/detail data. It does not run recall classification,
REM LLM review, or final registry loading.

cd /d "%~dp0"

REM Optional: set TRIALGPT_PYTHON to a specific Python executable before running.
REM Example:
REM   set TRIALGPT_PYTHON=C:\Anaconda\envs\trialgpt\python.exe
if defined TRIALGPT_PYTHON (
  set "PYTHON_EXE=%TRIALGPT_PYTHON%"
) else if exist "C:\Anaconda\envs\trialgpt\python.exe" (
  set "PYTHON_EXE=C:\Anaconda\envs\trialgpt\python.exe"
) else (
  set "PYTHON_EXE=python"
)

set "DB_PATH=sources\china_chictr\data\chictr_raw_crawl.db"
set "START_YEAR=2026"
set "END_YEAR=2026"
set "DATE_PREFIX="
set "PHASE=both"
set "DETAIL_STRATEGY=all"
set "SLOW_MIN=3"
set "SLOW_MAX=7"
set "VERIFY_TIMEOUT=900"

echo Project root: %CD%
echo Python: %PYTHON_EXE%
echo Database: %DB_PATH%
echo Year range: %START_YEAR% - %END_YEAR%
echo Date prefix: %DATE_PREFIX%
echo.
echo If ChiCTR shows slide verification, complete it in the opened browser.
echo If automatic drag fails, complete verification manually and press Enter in this terminal.
echo.

"%PYTHON_EXE%" "sources\china_chictr\scripts\run_chictr_crawl_only.py" ^
  --db "%DB_PATH%" ^
  --start-year %START_YEAR% ^
  --end-year %END_YEAR% ^
  --date-prefix "%DATE_PREFIX%" ^
  --phase %PHASE% ^
  --detail-strategy %DETAIL_STRATEGY% ^
  --slow-min %SLOW_MIN% ^
  --slow-max %SLOW_MAX% ^
  --manual-verification-timeout %VERIFY_TIMEOUT%

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo ChiCTR crawl-only workflow finished.
) else (
  echo ChiCTR crawl-only workflow stopped with exit code %EXIT_CODE%.
)
echo.
pause
exit /b %EXIT_CODE%


