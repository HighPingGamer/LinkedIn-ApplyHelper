@echo off
setlocal

cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
) else if exist "env\Scripts\activate.bat" (
    call "env\Scripts\activate.bat"
) else (
    echo No virtual environment found. Using system Python.
)

python agent.py

echo.
pause
