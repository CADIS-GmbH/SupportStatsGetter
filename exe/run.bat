@echo off
setlocal
REM Runs SupportStatsGetter.exe from this folder: the database and the log are
REM kept next to the exe, the Excel reports go to the reports subfolder.
REM First run: creates config.yaml from config.example.yaml and opens it.
REM Jira email and API token: taken from config.yaml, else from the environment
REM variables SUPPORTSTATS_JIRA_USER / SUPPORTSTATS_JIRA_TOKEN. When neither
REM has them, this file asks for them once and saves them for your Windows user.
REM When Jira rejects them (e.g. the token expired), it asks again.
REM Extra arguments are passed on, e.g.  run.bat --start-date 2025-01-01
REM Works from the Task Scheduler as well (no prompts unless something fails).

cd /d "%~dp0"
set "EXE=%~dp0SupportStatsGetter.exe"
set "RETRIED="
echo Starting SupportStatsGetter - the program needs a few seconds to start...

if not exist config.yaml (
  copy /y config.example.yaml config.yaml >nul
)
findstr /c:"<your API token>" /c:"your-site.atlassian.net" config.yaml >nul
if not errorlevel 1 (
  echo config.yaml still has placeholder values. Notepad opens now:
  echo fill in jira_base_url and base_jql, save, close, then run this file
  echo again. find_custom_fields.exe shows your field IDs.
  notepad config.yaml
  exit /b 0
)

REM --- Jira credentials -------------------------------------------------------
:check_credentials
REM both saved for the Windows user: nothing to ask, save a program start
if defined SUPPORTSTATS_JIRA_USER if defined SUPPORTSTATS_JIRA_TOKEN goto :run
call :missing_credentials || goto :error
if "%MISSING%"=="0" goto :run
echo Your Jira login is not set up for this Windows user yet. It is saved in
echo your user's environment variables, so this is only asked once.

:ask_credentials
echo Create an API token at https://id.atlassian.com/manage-profile/security/api-tokens
echo.
if "%NEED_USER%"=="0" goto :ask_token
set "SUPPORTSTATS_JIRA_USER="
if defined OLD_USER goto :ask_user_again
set /p "SUPPORTSTATS_JIRA_USER=Jira email address: "
goto :check_user
:ask_user_again
REM after a rejected login: Enter keeps the saved email, only the token is new
set "SUPPORTSTATS_JIRA_USER=%OLD_USER%"
set /p "SUPPORTSTATS_JIRA_USER=Jira email address [Enter = %OLD_USER%]: "
:check_user
if not defined SUPPORTSTATS_JIRA_USER (
  echo No email address entered.
  goto :error
)
setx SUPPORTSTATS_JIRA_USER "%SUPPORTSTATS_JIRA_USER%" >nul || goto :error

:ask_token
if "%NEED_TOKEN%"=="0" goto :run
REM read the token hidden and save it for the user without showing it anywhere
powershell -NoProfile -Command "$s = Read-Host 'Jira API token (input hidden)' -AsSecureString; $t = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)); if (-not $t) { exit 1 }; [Environment]::SetEnvironmentVariable('SUPPORTSTATS_JIRA_TOKEN', $t, 'User')"
if errorlevel 1 (
  echo No API token entered.
  goto :error
)
REM this window doesn't see the new user variable yet: read it back
for /f "tokens=2,*" %%a in ('reg query HKCU\Environment /v SUPPORTSTATS_JIRA_TOKEN 2^>nul') do set "SUPPORTSTATS_JIRA_TOKEN=%%b"
echo Saved. To change them later: run  setx SUPPORTSTATS_JIRA_TOKEN "<new token>"
echo (or SUPPORTSTATS_JIRA_USER) in a terminal, or delete them in the Windows
echo settings under "Edit environment variables for your account".
echo.

:run
if not exist reports mkdir reports

"%EXE%" -c config.yaml %*
set "RC=%errorlevel%"
if "%RC%"=="0" goto :done
if not "%RC%"=="32" goto :error
if defined RETRIED goto :error

REM --- Jira rejected the login (401) ------------------------------------------
REM ask again for what comes from the environment variables; values written in
REM config.yaml have to be fixed there
set "RETRIED=1"
set "OLD_USER=%SUPPORTSTATS_JIRA_USER%"
set "SUPPORTSTATS_JIRA_USER="
set "SUPPORTSTATS_JIRA_TOKEN="
call :missing_credentials || goto :error
if "%MISSING%"=="0" (
  echo.
  echo Jira rejected the login written in config.yaml. Fix jira_user /
  echo jira_token there, or remove them to use the saved Windows login instead.
  goto :error
)
REM forget the rejected token, so the next run asks even if this one stops;
REM the email stays saved (the next prompt offers it) - usually only the
REM token has expired
if not "%NEED_TOKEN%"=="0" powershell -NoProfile -Command "[Environment]::SetEnvironmentVariable('SUPPORTSTATS_JIRA_TOKEN', $null, 'User')"
echo.
echo Jira rejected your saved login - the API token has probably expired.
echo Please enter a new token (and correct the email address if it is wrong).
goto :ask_credentials

:done
echo Done. Reports: %~dp0reports
exit /b 0

:error
echo.
echo Something went wrong - see the messages above and support_stats.log
REM waits when started by hand; returns at once in the Task Scheduler
timeout /t 60 >nul 2>nul
exit /b 1

REM --- sets MISSING, NEED_USER and NEED_TOKEN; fails on other errors ----------
REM exit code of --missing-credentials: 0 = all set, +8 = no email,
REM +16 = no token, 2 = option unknown (exe older than this file), anything
REM else = error (e.g. no config)
:missing_credentials
"%EXE%" -c config.yaml --missing-credentials 2>nul
set "MISSING=%errorlevel%"
if "%MISSING%"=="2" (
  echo SupportStatsGetter.exe is older than this run.bat and can't check the
  echo Jira login. Copy SupportStatsGetter.exe and run.bat from the same build
  echo ^(build_exe.bat creates both^).
  exit /b 1
)
set /a "NEED_USER=MISSING & 8, NEED_TOKEN=MISSING & 16, OTHER=MISSING & ~24"
if not "%OTHER%"=="0" (
  "%EXE%" -c config.yaml --missing-credentials
  exit /b 1
)
exit /b 0
