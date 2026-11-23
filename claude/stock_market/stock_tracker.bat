@echo off
rem name: $Id: stock_tracker.bat 1.main-1 18:31:28 23-Nov-2026 rudyz $
rem use: DOS batch file wrapper for running python script

setlocal EnableExtensions DisableDelayedExpansion
   set "SELF=%~n0%"
   set "SELFPREFIX=%~dp0%"
   if "%SELFPREFIX:~-1%" equ "\" set "SELFPREFIX=%SELFPREFIX:~0,-1%"
   set "EXITCODE="
                                rem TCC and uSoft cmd.exe use different
                                rem syntax to pass full command line
                                rem arguments. TCC has an internal
                                rem _CMDPROC variable naming itself, which
                                rem cmd.exe does not, so in cmd.exe it is
                                rem empty
   if "%_CMDPROC%" == "" goto CMD
                                rem for use from TCC command line
      python.exe "%SELFPREFIX%\%SELF%.py" %$
      set "EXITCODE=%ERRORLEVEL%"
   goto END

:CMD
                                rem for use in GnuEmacs make
   python.exe "%SELFPREFIX%\%SELF%.py" %*%
   set "EXITCODE=%ERRORLEVEL%"
   goto END

:END
   if "%EXITCODE%" neq "" exit /B %EXITCODE%

endlocal
