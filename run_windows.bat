@echo off
setlocal
cd /d "%~dp0"
title Bhoonidhi Downloader

call :find_conda
if not defined CONDA_BAT goto :no_conda

call "%CONDA_BAT%" activate bhoonidhi
if errorlevel 1 goto :no_env

echo.
echo  Starting Bhoonidhi Downloader...
echo  It opens in your web browser. KEEP THIS WINDOW OPEN while you use the app.
echo  To stop the app: close this window (or press Ctrl + C).
echo.
python -m streamlit run app.py --browser.gatherUsageStats false
pause
exit /b 0

:no_env
echo.
echo  The "bhoonidhi" environment is not installed yet.
echo  Double-click  install_windows.bat  first.
echo.
pause
exit /b 1

:no_conda
echo.
echo  Anaconda / Miniconda was not found. Run install_windows.bat first
echo  (it tells you how to install Miniconda).
echo.
pause
exit /b 1

rem ---------------------------------------------------------------
:find_conda
set "CONDA_BAT="
if not defined CONDA_EXE goto :fc_list
for %%I in ("%CONDA_EXE%") do if exist "%%~dpI..\condabin\conda.bat" set "CONDA_BAT=%%~dpI..\condabin\conda.bat"
if defined CONDA_BAT exit /b 0
:fc_list
for %%D in (
    "%USERPROFILE%\miniconda3"
    "%USERPROFILE%\anaconda3"
    "%USERPROFILE%\miniforge3"
    "%USERPROFILE%\mambaforge"
    "%LOCALAPPDATA%\miniconda3"
    "%LOCALAPPDATA%\anaconda3"
    "%LOCALAPPDATA%\miniforge3"
    "%ProgramData%\miniconda3"
    "%ProgramData%\anaconda3"
    "%ProgramData%\miniforge3"
    "C:\miniconda3"
    "C:\anaconda3"
) do (
    if not defined CONDA_BAT if exist "%%~D\condabin\conda.bat" set "CONDA_BAT=%%~D\condabin\conda.bat"
)
if defined CONDA_BAT exit /b 0
for /f "delims=" %%C in ('where conda.bat 2^>nul') do if not defined CONDA_BAT set "CONDA_BAT=%%C"
exit /b 0
