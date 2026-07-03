@echo off
setlocal
rem dbHMS xeokit viewer launcher (Phase 1 spike).
rem Double-click this file. It serves your exported models and opens the xeokit
rem viewer in your default browser. Close the console window to stop.

set "MODELS=%LOCALAPPDATA%\dbHMS\3DViewer\models"
if not exist "%MODELS%" (
  echo Could not find your exported models at:
  echo   "%MODELS%"
  echo Export a model from the 3D Viewer button in Revit first, then run this again.
  echo.
  pause
  exit /b 1
)

copy /y "%~dp0xeokit_viewer.html" "%MODELS%\xeokit_viewer.html" >nul

set "PORT=8137"
set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD where python >nul 2>nul && set "PYCMD=python"
if not defined PYCMD (
  echo Python was not found on your PATH, so the local server cannot start.
  echo Install Python 3 (python.org) or ask for help, then run this again.
  echo.
  pause
  exit /b 1
)

echo.
echo dbHMS xeokit viewer
echo   serving: %MODELS%
echo   opening: http://localhost:%PORT%/xeokit_viewer.html
echo   (close this window to stop)
echo.

start "" /b cmd /c "timeout /t 2 >nul & start "" http://localhost:%PORT%/xeokit_viewer.html"
%PYCMD% -m http.server %PORT% --directory "%MODELS%"
