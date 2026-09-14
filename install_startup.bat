@echo off
REM ============================================================
REM  SBS2Flat - install as a startup item (runs hidden at login)
REM  Run this ONCE. It adds a shortcut to your Startup folder so
REM  SBS2Flat launches automatically (hidden) when you log in.
REM  Run again any time to update it.
REM ============================================================

set APPDIR=%~dp0
set VBS=%APPDIR%run_hidden.vbs
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

echo Creating startup shortcut...
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%STARTUP%\SBS2Flat.lnk');" ^
  "$s.TargetPath='%VBS%';" ^
  "$s.WorkingDirectory='%APPDIR%';" ^
  "$s.Description='SBS2Flat 3D-to-2D';" ^
  "$s.Save()"

echo.
echo Done. SBS2Flat will start automatically (hidden) when you log in.
echo To remove it: delete "SBS2Flat.lnk" from this folder:
echo   %STARTUP%
echo.
echo NOTE: edit run_hidden.vbs first if sbs2flat.py is not in this folder.
pause
