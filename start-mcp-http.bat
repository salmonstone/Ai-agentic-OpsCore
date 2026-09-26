@echo off
REM Start the AtlasOS MCP server in remote (HTTP) mode.
REM
REM MCP_AUTH_TOKEN is read from .env (gitignored), and the ngrok hostname is
REM auto-detected from the local ngrok agent, so neither needs to be set here.
REM MCP_TRANSPORT deliberately is NOT in .env: Claude Code spawns this same
REM module over stdio and would otherwise inherit http and fail to start.

cd /d "%~dp0"
set MCP_TRANSPORT=http

echo Starting AtlasOS MCP (HTTP)...
echo   If ngrok is running, its hostname is detected automatically.
echo.
uv run python -m agent.mcp_server
