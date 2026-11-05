@echo off
setlocal
if exist "%~dp0.conda-env\Scripts\ehv-agent.exe" (
    "%~dp0.conda-env\Scripts\ehv-agent.exe" %*
) else (
    echo EHV Agent is not installed. Run install.cmd first.
    exit /b 1
)
exit /b %errorlevel%
