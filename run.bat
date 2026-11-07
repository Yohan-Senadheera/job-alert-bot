@echo off
cd /d "%~dp0"
set JOBBOT_ONLY=xpress
python jobbot.py >> run_output.log 2>&1
set JOBBOT_ONLY=topjobs
python jobbot.py >> run_output.log 2>&1
