@echo off
setlocal
REM One-click setup and run for SupportStatsGetter.
REM First run: installs Python packages, creates config.yaml and opens it.
REM Next runs: fetches new Jira tickets and writes data\SupportStats.xlsx.
REM Extra arguments are passed on, e.g.  RUN.bat --start-date 2025-01-01

cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.13 from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during installation. Then run this file again.
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  REM Pinned to 3.13: newer versions lack prebuilt wheels for some packages (e.g. PyYAML).
  py -3.13 --version >nul 2>nul
  if errorlevel 1 (
    echo Python 3.13 was not found. Install it from https://www.python.org/downloads/
    echo and run this file again.
    pause & exit /b 1
  )
  echo Creating Python environment...
  py -3.13 -m venv .venv || goto :error
)
echo Installing / checking packages...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || goto :error

if not exist data mkdir data

if not exist config.yaml (
  copy /y config.example.yaml config.yaml >nul
)
findstr /c:"<your API token>" /c:"your-site.atlassian.net" config.yaml >nul
if not errorlevel 1 (
  echo.
  echo config.yaml still has placeholder values. Notepad opens now:
  echo fill in jira_base_url, jira_user, jira_token and base_jql, save, close,
  echo then run RUN.bat again.
  notepad config.yaml
  exit /b 0
)

echo.
echo Checking Jira connection and custom fields...
".venv\Scripts\python.exe" find_custom_fields.py config.yaml || goto :error

echo.
echo Fetching tickets (the first run can take a long time)...
".venv\Scripts\python.exe" SupportStatsGetter.py -c config.yaml %* || goto :error

echo.
echo Done. Report: %~dp0data\SupportStats.xlsx
start "" "%~dp0data"
pause
exit /b 0

:error
echo.
echo Something went wrong - see the messages above and data\support_stats.log
pause
exit /b 1
