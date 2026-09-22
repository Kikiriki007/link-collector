@echo off
REM Runs collect.py using this project's own venv. Safe to double-click for a manual run,
REM and this is exactly what the daily scheduled task (see setup_task.ps1) invokes too.
REM Output is shown live (manual runs) AND appended to run.log - the scheduled task runs
REM with no visible window, so without this a crash there leaves zero trace anywhere.
cd /d "%~dp0"
echo ===== run started %DATE% %TIME% ===== >> run.log
powershell -NoProfile -Command "& '.venv\Scripts\python.exe' 'collect.py' 2>&1 | Tee-Object -FilePath 'run.log' -Append"
echo ===== run finished %DATE% %TIME%, exit %ERRORLEVEL% ===== >> run.log
