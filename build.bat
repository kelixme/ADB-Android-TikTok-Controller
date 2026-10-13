@echo off
setlocal
cd /d "%~dp0"

set "NAME=ADB TikTok Controller"

echo Building executable in ONEDIR mode...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
if exist "%NAME%.spec" del /q "%NAME%.spec"

pyi-makespec --onedir --windowed --icon "app.ico" --name "%NAME%" ^
  --hidden-import=win32file --hidden-import=win32event --hidden-import=win32api ^
  --add-data "templates;templates" ^
  --add-data "adb.exe;." ^
  --add-data "AdbWinApi.dll;." ^
  --add-data "libusb-1.0.dll;." ^
  --add-data "scrcpy.exe;." ^
  --add-data "SDL3.dll;." ^
  --add-data "avcodec-63.dll;." ^
  --add-data "avformat-63.dll;." ^
  --add-data "avutil-61.dll;." ^
  --add-data "swresample-7.dll;." ^
  --add-data "app.ico;." ^
  --add-data "disconnected.png;." ^
  --add-data "scrcpy.png;." ^
  --add-data "app.bat;." ^
  --add-data "build.bat;." ^
  --add-data "config.json;." ^
  "app.py"

if errorlevel 1 (
    echo ERROR: failed to create spec.
    pause
    exit /b 1
)

echo import sys >> "%NAME%.spec"
echo sys.setrecursionlimit(5000) >> "%NAME%.spec"

pyinstaller --noconfirm --distpath "./" --clean "%NAME%.spec"

if errorlevel 1 (
    echo.
    echo BUILD FAILED.
    pause
    exit /b 1
)

echo ========================================
echo Ready! Result: "%NAME%\%NAME%.exe"
echo Data files in "%NAME%\_internal\"
echo ========================================
pause
endlocal