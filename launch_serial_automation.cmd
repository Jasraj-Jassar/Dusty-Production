@echo off
setlocal EnableExtensions

echo Serial Entry is integrated into DustyBot.
echo Launching DustyBot...
echo Open the "Serial Entry" button from the upload page.
echo.

cd /d "%~dp0"
call ".\DustyBot.cmd"
exit /b %ERRORLEVEL%
