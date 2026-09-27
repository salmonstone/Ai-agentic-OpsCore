@echo off
REM Take a verified backup of AtlasOS state (data\ and chroma_db\) into
REM backups\, pruning to the last 7 days + 4 weeks. Safe to run while the
REM MCP server or daemon is running. Extra arguments pass through, e.g.
REM   backup.bat --skip-if-within 12
REM
REM Scheduled as the "AtlasOS Backup" task: runs at sign-in and on unlock
REM (opening the laptop from sleep), skipping if a backup is under 12h old.
REM Check it:   Get-ScheduledTaskInfo -TaskName "AtlasOS Backup"
REM Remove it:  Unregister-ScheduledTask -TaskName "AtlasOS Backup"
REM
REM Output of the last run: backups\last-run.log

cd /d "%~dp0"
if not exist backups mkdir backups
uv run agent backup create %* > backups\last-run.log 2>&1
exit /b %errorlevel%
