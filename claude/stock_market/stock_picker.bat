@echo off
rem name: $Id: stock_picker.bat 2.main-2 17:05:10 23-Nov-2026 claude-opus-5-5 $

setlocal
   set SELF=%~n0%
   set "SELFPREFIX=%~dp0%"

rem calling make from GnuEmacs uses the MS cmd.exe, so we have to check
rem what command processor we're using
   if "%ComSpec%" == "%SystemRoot%\system32\cmd.exe" goto CMD
                                rem for use from TCC command line
      python.exe %SELFPREFIX%\%SELF%.py %1% %2% %3% %4% %5% %6% %7% %8% %9%
   goto END

:CMD
                                rem for use in GnuEmacs make
   python.exe %SELFPREFIX%\%SELF%.py %*%
   goto END

:END

endlocal
