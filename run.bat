@echo off
REM Run the MTGA Draft Overlay on Windows.
REM Usage: run.bat [path\to\Player.log]
IF "%~1"=="" (
    python main.py
) ELSE (
    python main.py -f "%~1"
)
