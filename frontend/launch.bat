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

REM Lifecycle: the image is built only if it doesn't exist (or when this
REM script is run with --rebuild, e.g. after changing backend code); models
REM live in the separate shared-model-cache volume; the containers are
REM started with the app and removed when it closes. The image and the
REM volume are never deleted here.
set "IMAGE_NAME=streamscribe_adaptive-backend:latest"
set "MODEL_VOLUME=shared-model-cache"
set "BUILD_FLAG="
if /i "%~1"=="--rebuild" set "BUILD_FLAG=--build"

REM The Docker engine must be up before compose can do anything. If Docker
REM Desktop isn't running, `compose up` fails instantly and the old script
REM then polled a backend that could never start for ~10 minutes. Start
REM Docker Desktop ourselves and wait for the engine, failing fast otherwise.
docker info >nul 2>&1
if errorlevel 1 (
    set "DOCKER_DESKTOP=%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe"
    if not exist "!DOCKER_DESKTOP!" set "DOCKER_DESKTOP=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
    if not exist "!DOCKER_DESKTOP!" (
        echo Docker engine is not running and Docker Desktop was not found.
        pause
        exit /b 1
    )
    echo Docker engine not running -- starting Docker Desktop...
    start "" "!DOCKER_DESKTOP!"
    set "DOCKER_TRIES=0"
    :dockerwait
    timeout /t 3 /nobreak >nul
    docker info >nul 2>&1
    if not errorlevel 1 goto dockerready
    set /a DOCKER_TRIES+=1
    if !DOCKER_TRIES! GEQ 60 (
        echo Docker engine did not start within ~3 minutes. Open Docker Desktop and check for errors.
        pause
        exit /b 1
    )
    goto dockerwait
)
:dockerready

docker volume inspect %MODEL_VOLUME% >nul 2>&1
if errorlevel 1 (
    echo Creating model volume "%MODEL_VOLUME%" ^(models download into it on first run^)...
    docker volume create %MODEL_VOLUME% >nul
)

docker image inspect %IMAGE_NAME% >nul 2>&1
if errorlevel 1 (
    echo Backend image not found -- building it ^(first run only, this takes a while^)...
) else if defined BUILD_FLAG (
    echo Rebuilding the backend image...
) else (
    echo Backend image found -- skipping build.
)
echo.
REM Right after Docker Desktop starts, `docker info` can succeed slightly
REM before compose/buildx are usable, so retry a few times before giving up.
set "COMPOSE_TRIES=0"
:composeup
REM No --build unless requested: compose still builds on its own when the
REM image is missing, and reuses it otherwise.
docker compose -f "%COMPOSE_FILE%" up -d %BUILD_FLAG%
if not errorlevel 1 goto composedone
set /a COMPOSE_TRIES+=1
if !COMPOSE_TRIES! GEQ 5 (
    echo.
    echo docker compose failed -- see the error above.
    pause
    exit /b 1
)
echo docker compose failed, retrying in 10s ^(!COMPOSE_TRIES!/5^)...
timeout /t 10 /nobreak >nul
goto composeup
:composedone

for /f "delims=" %%A in ('curl -s -o nul -w "%%{http_code}" %BACKEND_URL% 2^>nul') do set "HEALTH_CODE=%%A"

if not "!HEALTH_CODE!"=="200" (
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
set "APP_EXIT=%errorlevel%"

echo.
echo App closed -- stopping and removing the containers ^(image and model volume are kept^)...
docker compose -f "%COMPOSE_FILE%" down
if not "!APP_EXIT!"=="0" pause
