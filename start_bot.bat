@echo off
rem Fluno Trading Bot watchdog — starts the bot and restarts it if it ever
rem crashes or is killed. Runs automatically at Windows logon (scheduled task).
cd /d "C:\Users\avina\Downloads\varun trading"
:loop
py app.py
timeout /t 10 /nobreak >nul
goto loop
