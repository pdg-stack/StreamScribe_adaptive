@echo off
setlocal enabledelayedexpansion

set "REPO_DIR=%~dp0.."
set "COMPOSE_FILE=%REPO_DIR%\docker-compose.yml"
set "BACKEND_URL=http://127.0.0.1:8000/health"
set "PYTHON_EXE=%REPO_DIR%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo ============================================================
echo StreamScribe_adaptive -- starting up
echo ============================================================

for /f "delims=" %%A in ('curl -s -o nul -w "%%{http_code}" %BACKEND_URL% 2^>nul') do set "HEALTH_CODE=%%A"

if not "!HEALTH_CODE!"=="200" (
    echo.
    echo Backend not ready -- building the image ^(if needed^) and starting the container...
    echo On a first run this also downloads the ASR/translation models, which can take a while.
    echo.
    docker compose -f "%COMPOSE_FILE%" up -d --build

    echo.
    echo Waiting for the backend to finish loading models...
    echo ------------------------------------------------------------

    set "PREV_LINES=0"
    set "RETRIES=0"
    :waitloop
    timeout /t 2 /nobreak >nul

    for /f %%N in ('docker compose -f "%COMPOSE_FILE%" logs --no-color backend 2^>nul ^| find /v /c ""') do set "CUR_LINES=%%N"
    if !CUR_LINES! GTR !PREV_LINES! (
        set /a NEXT_LINE=PREV_LINES+1
        docker compose -f "%COMPOSE_FILE%" logs --no-color backend 2>nul | more +!NEXT_LINE!
        set "PREV_LINES=!CUR_LINES!"
    )

    for /f "delims=" %%A in ('curl -s -o nul -w "%%{http_code}" %BACKEND_URL% 2^>nul') do set "HEALTH_CODE=%%A"
    set /a RETRIES+=1
    if "!HEALTH_CODE!"=="200" goto ready
    if !RETRIES! GEQ 300 (
        echo.
        echo Backend still not reachable after waiting ~10 minutes. Check: docker compose logs backend
        pause
        exit /b 1
    )
    goto waitloop
)

:ready
echo.
echo ------------------------------------------------------------
echo Backend ready.
echo ------------------------------------------------------------
cd /d "%REPO_DIR%"
"%PYTHON_EXE%" -m frontend.main
pause
