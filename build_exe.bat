@echo off
setlocal
REM Builds standalone Windows programs that run without Python:
REM   dist\SupportStatsGetter\SupportStatsGetter.exe
REM   dist\SupportStatsGetter\find_custom_fields.exe
REM plus run.bat and config.example.yaml (database and log next to the exe,
REM Excel reports in reports\). Copy the whole dist\SupportStatsGetter folder to
REM the other computer and start run.bat; the first run creates config.yaml.
REM Needs the .venv from SETUP.md step 2 (Python 3.13).

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Create it first, see SETUP.md step 2.
  pause & exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt pyinstaller || goto :error

if exist dist\SupportStatsGetter rmdir /s /q dist\SupportStatsGetter
for %%s in (SupportStatsGetter find_custom_fields) do (
  ".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --console ^
    --distpath dist\SupportStatsGetter --workpath build --specpath build ^
    --name %%s %%s.py || goto :error
)
".venv\Scripts\python.exe" exe\make_exe_config.py config.example.yaml dist\SupportStatsGetter\config.example.yaml || goto :error
copy /y exe\run.bat dist\SupportStatsGetter\ >nul
copy /y SETUP.md dist\SupportStatsGetter\ >nul

echo.
echo Done: %~dp0dist\SupportStatsGetter
pause
exit /b 0

:error
echo.
echo Build failed - see the messages above.
pause
exit /b 1
