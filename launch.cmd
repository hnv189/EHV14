@echo off
setlocal
call "%~dp0ehv-agent.cmd" start --new-window %*
exit /b %errorlevel%
