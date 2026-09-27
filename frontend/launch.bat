@echo off
setlocal enabledelayedexpansion

set "REPO_DIR=%~dp0.."
set "BACKEND_URL=http://127.0.0.1:8000/health"
set "PYTHON_EXE=%REPO_DIR%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo Checking StreamScribe_adaptive backend...
for /f "delims=" %%A in ('curl -s -o nul -w "%%{http_code}" %BACKEND_URL% 2^>nul') do set "HEALTH_CODE=%%A"

if not "!HEALTH_CODE!"=="200" (
    echo Backend not reachable -- starting it with docker compose...
    pushd "%REPO_DIR%"
    docker compose up -d
    popd

    set "RETRIES=0"
    :waitloop
    timeout /t 2 /nobreak >nul
    for /f "delims=" %%A in ('curl -s -o nul -w "%%{http_code}" %BACKEND_URL% 2^>nul') do set "HEALTH_CODE=%%A"
    set /a RETRIES+=1
    if "!HEALTH_CODE!"=="200" goto ready
    if !RETRIES! GEQ 30 (
        echo.
        echo Backend still not reachable after waiting ~60s. Check: docker compose logs
        pause
        exit /b 1
    )
    goto waitloop
)

:ready
cd /d "%REPO_DIR%"
"%PYTHON_EXE%" -m frontend.main
pause
