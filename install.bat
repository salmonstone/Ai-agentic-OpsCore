@echo off
setlocal enabledelayedexpansion

:: AI Agentic OS — Windows Installer
:: Usage: Double-click install.bat  OR  run from PowerShell: .\install.bat

echo.
echo   AI Agentic OS — Windows Installer
echo   ====================================
echo.

:: ── 1. Python 3.11+ ─────────────────────────────────────────────────────────

set PYTHON=
for %%P in (python3.13 python3.12 python3.11 python3 python) do (
    where %%P >nul 2>&1
    if !errorlevel! == 0 (
        for /f "tokens=*" %%V in ('%%P -c "import sys; print(sys.version_info.major)" 2^>nul') do set PYMAJ=%%V
        for /f "tokens=*" %%V in ('%%P -c "import sys; print(sys.version_info.minor)" 2^>nul') do set PYMIN=%%V
        if !PYMAJ! == 3 (
            if !PYMIN! GEQ 11 (
                set PYTHON=%%P
                goto :found_python
            )
        )
    )
)

echo [FAIL] Python 3.11 or newer is required.
echo        Download from: https://www.python.org/downloads/
echo        Make sure to check "Add Python to PATH" during install.
echo.
pause
exit /b 1

:found_python
for /f "tokens=*" %%V in ('!PYTHON! -c "import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\")"') do set PYVER=%%V
echo [OK]   Python !PYVER! found

:: ── 2. uv ────────────────────────────────────────────────────────────────────

where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing uv...
    powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    :: Refresh PATH
    set PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%
    where uv >nul 2>&1
    if !errorlevel! neq 0 (
        echo [FAIL] uv installation failed.
        echo        Install manually: https://docs.astral.sh/uv/getting-started/installation/
        pause
        exit /b 1
    )
)

for /f "tokens=*" %%V in ('uv --version 2^>^&1') do set UVVER=%%V
echo [OK]   uv ready — %UVVER%

:: ── 3. Optional dependencies ─────────────────────────────────────────────────

echo.
echo   Checking optional dependencies...
echo.

where kubectl >nul 2>&1
if %errorlevel% == 0 (
    echo [OK]   kubectl found — Kubernetes features available
) else (
    echo [WARN] kubectl not found — Kubernetes commands will be unavailable
    echo        Install: https://kubernetes.io/docs/tasks/tools/install-kubectl-windows/
)

where aws >nul 2>&1
if %errorlevel% == 0 (
    echo [OK]   AWS CLI found — AWS features available
) else (
    echo [WARN] AWS CLI not found — AWS commands will be unavailable
    echo        Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
)

:: ── 4. Install dependencies ───────────────────────────────────────────────────

echo.
echo   Installing Python dependencies...
echo.

uv sync
if %errorlevel% neq 0 (
    echo [FAIL] uv sync failed.
    pause
    exit /b 1
)

uv pip install -e .
if %errorlevel% neq 0 (
    echo [FAIL] uv pip install failed.
    pause
    exit /b 1
)

echo.
echo [OK]   Installation complete.

:: ── 5. Create data directory ─────────────────────────────────────────────────

if not exist "data\vault" mkdir "data\vault"
echo [OK]   data/ directory ready.

:: ── 6. Copy .env.example if .env doesn't exist ───────────────────────────────

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [OK]   .env created from .env.example
    )
)

:: ── 7. Verify CLI ─────────────────────────────────────────────────────────────

uv run agent --help >nul 2>&1
if %errorlevel% == 0 (
    echo [OK]   CLI available via: uv run agent
) else (
    echo [WARN] Could not verify CLI. Try restarting your terminal.
)

:: ── 8. Run setup wizard ───────────────────────────────────────────────────────

echo.
echo   ====================================
echo   Ready! Launching setup wizard...
echo   (Press Ctrl+C to skip and run later with: uv run agent setup)
echo   ====================================
echo.

uv run agent setup

echo.
echo   ====================================
echo   All done!
echo   Run:  uv run agent --help
echo   ====================================
echo.

pause
