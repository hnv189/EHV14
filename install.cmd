@echo off
setlocal
pushd "%~dp0"
where conda >nul 2>nul
if errorlevel 1 (
    echo conda was not found. Install Miniconda or Anaconda, then run this again.
    goto failed
)
if not exist ".conda-env\python.exe" (
    conda create -y -p ".conda-env" python=3.12 -c conda-forge --override-channels --no-default-packages
    if errorlevel 1 goto failed
)
".conda-env\python.exe" -m pip install -e .
if errorlevel 1 goto failed
echo.
echo Installed. Set ANTHROPIC_API_KEY, then run ehv-agent.cmd start.
popd
exit /b 0
:failed
popd
exit /b 1
