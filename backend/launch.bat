@echo off
title Kick ^& Twitch Downloader

CD /D "%~dp0"

echo =================================================
echo   Kick ^& Twitch Downloader v2.0 (Python)
echo   Open http://localhost:7897 in your browser
echo =================================================
echo.
echo Starting server...
start http://localhost:7897
echo.

python run.py
pause
