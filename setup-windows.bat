@echo off
REM CDPlayer v1.16.1 - Windows Setup Script
REM Adds cdplay and uncdplayer commands to PATH

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "INSTALL_DIR=%USERPROFILE%\.cdplayer\bin"
set "UNINSTALL=false"

REM Parse arguments
if "%1"=="--uninstall" set UNINSTALL=true
if "%1"=="-u" set UNINSTALL=true
if "%1"=="--help" goto :show_help
if "%1"=="-h" goto :show_help

if "%UNINSTALL%"=="true" goto :uninstall

:show_help
echo ==============================================
echo   CDPlayer v1.16.1 - Windows Setup
echo ==============================================
echo.
echo Usage: setup-windows.bat [OPTIONS]
echo.
echo Options:
echo   -u, --uninstall   Remove cdplay from PATH
echo   -h, --help        Show this help message
echo.
echo This script adds 'cdplay' and 'uncdplayer' commands to your PATH.
echo.
goto :end

:uninstall
echo Removing cdplay from PATH...
echo.

REM Remove installation directory
if exist "%INSTALL_DIR%" (
    rmdir /s /q "%INSTALL_DIR%" 2>nul
    echo   ✓ Removed %INSTALL_DIR%
)

REM Try to remove from PATH (best effort - requires admin for system PATH)
echo.
echo Note: Manual PATH cleanup may be required.
echo Open System Properties ^> Advanced ^> Environment Variables
echo and remove %INSTALL_DIR% from the Path variable.
echo.
echo Uninstallation complete!
echo Restart your terminal or run: setx PATH "%PATH%"
goto :end

:main
echo ==============================================
echo   CDPlayer v1.16.1 - Windows Setup
echo ==============================================
echo.

REM Check Python
where python >nul 2>&1
if errorlevel 1 (
    where python3 >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python not found!
        echo Please install Python 3.9+ from https://python.org
        echo Make sure to check "Add Python to PATH" during installation.
        goto :error
    )
    set "PYTHON_CMD=python3"
) else (
    set "PYTHON_CMD=python"
)

%PYTHON_CMD% --version
echo.

REM Create installation directory
if not exist "%INSTALL_DIR%" (
    mkdir "%INSTALL_DIR%"
    echo Created %INSTALL_DIR%
)

REM Create cdplay.bat wrapper
(
echo @echo off
echo REM CDPlayer wrapper for Windows
echo set "SCRIPT_DIR=%%~dp0"
echo set "CDPLAYER_DIR=%%~dp0.."
echo.
echo REM Check for Win-Ver.py
echo if exist "%%CDPLAYER_DIR%%\\Win-Ver.py" ^(
echo     %PYTHON_CMD% "%%CDPLAYER_DIR%%\\Win-Ver.py" %%*
echo ^) else if exist ".\\Win-Ver.py" ^(
echo     %PYTHON_CMD% ".\\Win-Ver.py" %%*
echo ^) else ^(
echo     echo [ERROR] Cannot find Win-Ver.py
echo     echo Please ensure CDPlayer is installed correctly.
echo     exit /b 1
echo ^)
) > "%INSTALL_DIR%\cdplay.bat"

echo ✓ Created %INSTALL_DIR%\cdplay.bat

REM Create uncdplayer.bat wrapper
(
echo @echo off
echo REM CDPlayer uninstaller wrapper
echo call "%SCRIPT_DIR%setup-windows.bat" --uninstall %%*
) > "%INSTALL_DIR%\uncdplayer.bat"

echo ✓ Created %INSTALL_DIR%\uncdplayer.bat

REM Add to PATH using setx
echo.
echo Adding %INSTALL_DIR% to user PATH...

REM Get current PATH
for /f "tokens=2*" reg query "HKCU\Environment" /v PATH 2>nul | findstr PATH do set "CURRENT_PATH=%%B"
if "!CURRENT_PATH!"=="" set "CURRENT_PATH=%PATH%"

REM Check if already in PATH
echo "!CURRENT_PATH!" | findstr /i /c:"%INSTALL_DIR%" >nul
if not errorlevel 1 (
    echo   Already in PATH
) else (
    setx PATH "!CURRENT_PATH!;%INSTALL_DIR%" >nul
    echo   ✓ Added to user PATH
    echo.
    echo   Note: PATH changes take effect in new terminal windows.
)

echo.
echo ==============================================
echo   Installation Complete!
echo ==============================================
echo.
echo You can now run CDPlayer from anywhere using:
echo   cdplay          REM Launch CD Player
echo   uncdplayer      REM Uninstall CDPlayer
echo.
echo Or run directly:
echo   %PYTHON_CMD% "%SCRIPT_DIR%Win-Ver.py"
echo.
echo Debug mode:
echo   %PYTHON_CMD% Win-Ver.py --win-debug
echo.
echo NOTE: Close and reopen your terminal to use 'cdplay' command.
echo.

goto :end

:error
exit /b 1

:end
endlocal
