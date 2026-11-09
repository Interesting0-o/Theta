@echo off
setlocal
set "PYTHONUTF8=1"
set "THETA_LANGGRAPH_DEV_PNA=1"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "LANGGRAPH=%~dp0..\.venv\Scripts\langgraph.exe"

if not exist "%LANGGRAPH%" (
    echo LangGraph CLI not found: "%LANGGRAPH%" 1>&2
    exit /b 1
)

"%LANGGRAPH%" dev --allow-blocking %*
exit /b %ERRORLEVEL%
