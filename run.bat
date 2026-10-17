@echo off
cd /d "%~dp0"
python jobbot.py >> run_output.log 2>&1
