@echo off
REM Start the AtlasOS supervisor: runs ngrok + the remote MCP server and
REM restarts either one if it crashes, hangs, or loses its tunnel hostname.
REM Replaces running start-mcp-http.bat and ngrok by hand.
REM
REM Runs automatically as the "AtlasOS Supervisor" scheduled task at sign-in
REM and on unlock (a no-op if it is already running).
REM   Status:  uv run agent supervise status
REM   Logs:    uv run agent supervise logs mcp      (or ngrok)
REM   Stop:    uv run agent supervise stop
REM   Disable autostart:  Disable-ScheduledTask -TaskName "AtlasOS Supervisor"
REM
REM Opt in to the autonomous healing daemon as well (it applies fixes on its
REM own):  uv run agent supervise run --services ngrok,mcp,daemon

cd /d "%~dp0"
if not exist data\logs mkdir data\logs
uv run agent supervise run %* >> data\logs\supervisor.log 2>&1
