@echo off
REM Take a verified backup of AtlasOS state (data\ and chroma_db\) into
REM backups\, pruning to the last 7 days + 4 weeks. Safe to run while the
REM MCP server or daemon is running.
REM
REM Run it daily with Task Scheduler (runs at next login if the laptop was
REM off at 13:00). From PowerShell, in this folder:
REM
REM   $a = New-ScheduledTaskAction -Execute "$PWD\backup.bat" -WorkingDirectory "$PWD"
REM   $t = New-ScheduledTaskTrigger -Daily -At 13:00
REM   $s = New-ScheduledTaskSettingsSet -StartWhenAvailable
REM   Register-ScheduledTask -TaskName "AtlasOS Backup" -Action $a -Trigger $t -Settings $s
REM
REM Output of the last run: backups\last-run.log

cd /d "%~dp0"
if not exist backups mkdir backups
uv run agent backup create > backups\last-run.log 2>&1
exit /b %errorlevel%
