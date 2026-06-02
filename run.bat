@echo off
REM Double-click this file to start sportsbot.
REM It opens a window and keeps running while it watches for tips.
REM Close the window (or press Ctrl+C) to stop the bot.
cd /d "%~dp0"
python main.py
echo.
echo sportsbot stopped. Press any key to close this window.
pause >nul
