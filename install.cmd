@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto failed
echo.
echo Installed. Set ANTHROPIC_API_KEY, then run ehv-agent.cmd start.
popd
exit /b 0
:failed
popd
exit /b 1
