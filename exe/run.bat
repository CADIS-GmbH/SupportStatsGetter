@echo off
setlocal
REM Runs SupportStatsGetter.exe from this folder: the database and the log are
REM kept next to the exe, the Excel reports go to the reports subfolder.
REM First run: creates config.yaml from config.example.yaml and opens it.
REM Extra arguments are passed on, e.g.  run.bat --start-date 2025-01-01
REM Works from the Task Scheduler as well (no prompts unless something fails).

cd /d "%~dp0"

if not exist config.yaml (
  copy /y config.example.yaml config.yaml >nul
)
findstr /c:"<your API token>" /c:"your-site.atlassian.net" config.yaml >nul
if not errorlevel 1 (
  echo config.yaml still has placeholder values. Notepad opens now:
  echo fill in jira_base_url, jira_user, jira_token and base_jql, save, close,
  echo then run this file again. find_custom_fields.exe shows your field IDs.
  notepad config.yaml
  exit /b 0
)

if not exist reports mkdir reports

"%~dp0SupportStatsGetter.exe" -c config.yaml %*
if errorlevel 1 goto :error

echo Done. Reports: %~dp0reports
exit /b 0

:error
echo.
echo Something went wrong - see the messages above and support_stats.log
REM waits when started by hand; returns at once in the Task Scheduler
timeout /t 60 >nul 2>nul
exit /b 1
