@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if %errorlevel%==0 (
        py -3 -m venv .venv
    ) else (
        where python >nul 2>nul
        if %errorlevel%==0 (
            python -m venv .venv
        ) else (
            echo Python 3.10+ is required to run from source.
            echo For normal use, download TubeDrop-windows-x64.zip from Releases.
            pause
            exit /b 1
        )
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment was not created.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "%~dp0app.py"
) else (
    start "" ".venv\Scripts\python.exe" "%~dp0app.py"
)
exit /b 0
