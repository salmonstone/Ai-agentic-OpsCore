@echo off
REM Send the AtlasOS daily summary to Slack (cluster, Jenkins, GitHub Actions,
REM AWS spend, pending approvals, backups, AtlasOS status). At most once a
REM day: re-running it the same day does nothing. Extra args pass through,
REM e.g. summary.bat --force
REM
REM Scheduled as the "AtlasOS Daily Summary" task: 9:00 AM daily, and as soon
REM as the laptop is available if it was asleep/off at 9.
REM   Check it:   Get-ScheduledTaskInfo -TaskName "AtlasOS Daily Summary"
REM   Remove it:  Unregister-ScheduledTask -TaskName "AtlasOS Daily Summary"
REM   Preview without sending:  uv run agent summary show
REM
REM Output of the last run: data\logs\summary-last-run.log

cd /d "%~dp0"
if not exist data\logs mkdir data\logs
uv run agent summary send %* > data\logs\summary-last-run.log 2>&1
exit /b %errorlevel%
