@echo off
setlocal

set "REPO_DIR=%~dp0.."
set "BACKEND_URL=http://127.0.0.1:8000/health"

echo Checking StreamScribe_fwhisper backend...
for /f "delims=" %%A in ('curl -s -o nul -w "%%{http_code}" %BACKEND_URL% 2^>nul') do set "HEALTH_CODE=%%A"

if not "%HEALTH_CODE%"=="200" (
    echo.
    echo Backend not reachable at %BACKEND_URL%.
    echo Start it first with: docker compose up -d
    echo.
    pause
    exit /b 1
)

cd /d "%REPO_DIR%"
python -m frontend.main
pause
