@echo off
REM ====================================================================
REM  sportsbot updater - double-click to get the latest features.
REM  Your .env, sessions.yaml and Telegram login are NOT touched
REM  (they are git-ignored), so updating never wipes your settings.
REM  After it finishes, start the bot again with run.bat.
REM ====================================================================
cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
  echo.
  echo Git is not installed. Install it from https://git-scm.com/download/win
  echo  ^(accept all the defaults^), then run this again. See SETUP_STEPS.md.
  echo.
  pause
  exit /b 1
)

echo Checking for updates...
git pull
if errorlevel 1 (
  echo.
  echo Update FAILED. Most likely you have not cloned this folder from GitHub,
  echo or you edited a code file by hand. Send this window to whoever set up
  echo the bot. ^(Your .env / sessions.yaml are safe.^)
  echo.
  pause
  exit /b 1
)

echo.
echo Installing any new requirements...
python -m pip install -r requirements.txt

echo.
echo ===== Up to date. Start the bot with run.bat. =====
echo If new settings were added, compare your .env against .env.example.
echo.
pause
