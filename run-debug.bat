@echo off
rem Jalankan main.py dengan jendela browser terlihat (mode debug)
cd /d "%~dp0"
call "%~dp0run.bat" --show-browser %*
