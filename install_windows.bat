@echo off
setlocal
cd /d "%~dp0"
title Bhoonidhi Downloader - Install

echo.
echo  ============================================
echo    Bhoonidhi Downloader - one-time install
echo  ============================================
echo.

call :find_conda
if not defined CONDA_BAT goto :no_conda
echo Using conda: %CONDA_BAT%
echo.

call "%CONDA_BAT%" env list | findstr /b /c:"bhoonidhi " >nul
if errorlevel 1 goto :create

echo Environment "bhoonidhi" already exists - updating it...
call "%CONDA_BAT%" env update -n bhoonidhi -f environment.yml --prune
if errorlevel 1 goto :failed
goto :finish

:create
echo Creating environment "bhoonidhi". The first time this takes 5-15 minutes...
call "%CONDA_BAT%" env create -f environment.yml
if errorlevel 1 goto :failed

:finish
rem Skip Streamlit's one-time "enter your email" question
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
if exist "%USERPROFILE%\.streamlit\credentials.toml" goto :done
> "%USERPROFILE%\.streamlit\credentials.toml" echo [general]
>> "%USERPROFILE%\.streamlit\credentials.toml" echo email = ""

:done
echo.
echo  Install finished. Now double-click  run_windows.bat  to start the app.
echo.
pause
exit /b 0

:failed
echo.
echo  Install FAILED. Scroll up to see the error, or open an issue on GitHub:
echo  https://github.com/anurag-tm/Bhoonidhi-Downloader/issues
echo.
pause
exit /b 1

:no_conda
echo  Anaconda / Miniconda was not found on this PC.
echo.
echo  1. Install Miniconda (free):  https://docs.conda.io/en/latest/miniconda.html
echo  2. Then double-click install_windows.bat again.
echo.
echo  If conda IS installed somewhere unusual, open "Anaconda Prompt", go to this
echo  folder (cd) and run:  install_windows.bat
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
