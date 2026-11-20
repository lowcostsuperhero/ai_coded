@if (@X)==(@Y) @end /* Batch part; JScript sees this as a comment
@echo off
rem ==================================================================
rem name: $Id: rpn.bat 8.main-7 23:16:57 05-Nov-2026 rudyz $
rem imp: implemented completely by Claude Opus 5.5 based on prompts
rem      decribing an earlier implementation of rpn.btm (Claude did a
rem      better job with this .bat than I did with with the .btm)

setlocal EnableExtensions DisableDelayedExpansion
set "SELF=%~n0"
set "E_USAGE=3"
rem\\ reCmd \\rem
rem name: %Id: reCmd 3.main-3 23:15:49 05-Nov-2026 rudyz %
rem use: some of the really unconventional esoteric cmd.exe "features"
rem      used have not been duplicated under tcc/LE; re-execute under
rem      cmd.exe
for %%C in ( "%ComSpec%" ) do if /I "%%~nxC" equ "cmd.exe" goto RECMD_END
   %SystemRoot%\system32\cmd.exe /C ""%~f0" %*"
   exit /B %ERRORLEVEL%
:RECMD_END
rem// reCmd //rem
endlocal

rem  rpn.bat - simple RPN (Reverse Polish Notation) calculator
rem
rem  Usage:
rem     rpn                  interactive mode
rem     rpn 2 5 *            evaluate the expression and exit
rem     rpn /?               show help
rem
rem  Floating point.  Results are shown rounded to at most 4 decimal
rem  places, with no trailing zeros:  2  4.5  5.006  3.1416  0.001
rem  Full precision is kept internally.
rem
rem  Operators:  +  -  *  /  ^ or ** (power)  %% (modulus)
rem  ** works on the command line and in pasted text; when typing,
rem  the first * already ends the line as multiply, so type ^.
rem
rem  The stack has two registers: X (top of stack) and Y (one level
rem  below it).  Entering a number pushes it: Y gets the old X and X
rem  gets the number; whatever was in Y is lost.  An operator
rem  computes Y op X, leaves the result in X and empties Y.
rem
rem  Interactive mode shows no prompt.  Type numbers (digits and .)
rem  separated by spaces.  ENTER pushes what has been typed; an
rem  operator ends the line at once -- no ENTER needed.  After every
rem  ENTER, operator or C, = and X are printed with the decimal
rem  point (shown or implied) in column 16, or just = when X is
rem  empty.  No blank lines are printed:
rem     2[ENTER]
rem     =             2
rem     3*
rem     =             6
rem     =             6        (ENTER alone)
rem     1 8/
rem     =             0.125
rem  An operator with nothing in Y is ignored and = and X are added
rem  to the end of the same line:
rem     +=            0.125
rem  Two or more numbers ended by ENTER (no operator) also print Y,
rem  aligned the same way, on the line above = and X:
rem     1 2 3 4[ENTER]
rem                   3
rem     =             4
rem  This is the only time Y is printed.
rem  BACKSPACE erases the last character typed, C clears the stack,
rem  Q or ESC quits.
rem
rem  ^V or Y pastes the clipboard exactly as if it had been typed:
rem  each newline is an ENTER and each operator ends its line, so
rem  text that ends with a newline is finished with ENTER, and text
rem  that does not is left on the line, unfinished, to type on.
rem  Characters other than digits, ".", spaces, tabs, newlines and
rem  operators are skipped (C, Q and Y in the text do nothing):
rem     [^V]2 3 +            (clipboard holds "2 3 +4" and no newline)
rem     =             5
rem     4_                   (4 is left typed; _ is the cursor)
rem  ^V only reaches rpn when the console does not take it as its
rem  own paste (Windows Terminal: unbind ctrl+v; classic console:
rem  turn off "Enable Ctrl key shortcuts"); Y always does.
rem
rem  On the command line just the value is printed, so the output
rem  is easy to capture.  An operator with nothing in Y there is an
rem  error, reported as  X ? op  (or  ? ? op  when X is empty).
rem
rem  On the command line ^ must be quoted or doubled:  rpn 2 10 "^"
rem
rem  This file is a batch/JScript hybrid.  The batch part handles
rem  the keyboard; the math is done by the JScript part at the end
rem  of this file, run with CSCRIPT (Windows Script Host).
rem ==================================================================
setlocal EnableExtensions DisableDelayedExpansion

set "SELF=%~f0"

rem --- Control characters: DEL = <BS><space><BS>, ESC and CR
set "_C=prompt #$H#$E# & echo on & for %%b in (1) do rem"
for /f "tokens=1,2 delims=#" %%a in ('"%_C%"') do (
   set "DEL=%%a"
   set "ESC=%%b"
)
set "_C="
for /f %%a in ('copy /z "%~f0" nul') do set "CR=%%a"

rem --- ^ and %% kept in variables so they compare safely later
set "CARET=^"
set "PCT=%%"

setlocal EnableDelayedExpansion
set "BS=!DEL:~0,1!"
set "X="
set "Y="

if    "%~1" equ "/?" goto :help
if /I "%~1" equ "-h" goto :help
if "%~1"=="" goto :interactive
goto :evaluate_args


rem ==================================================================
rem  :evaluate_args
rem  Evaluate the command-line arguments as one RPN expression and
rem  print X.
rem ==================================================================
:evaluate_args
set "RPN_IN="
:args_next
if "%~1"=="" goto :args_end
set "TOK=%~1"
set "RPN_IN=!RPN_IN! !TOK!"
shift /1
goto :args_next
:args_end
set "RPN_MODE=args"
call :calc
if "!R_STA!"=="0" (
   echo(!R_OUT!
   endlocal & endlocal & exit /b 0
)
>&2 echo(!R_OUT!
endlocal & endlocal & exit /b 1


rem ==================================================================
rem  :interactive
rem  Read keystrokes one at a time.  Digits, . and spaces build the
rem  line; ENTER pushes it; an operator pushes it, applies the
rem  operator and prints X.
rem ==================================================================
:interactive
set "LINE="
set "RPN_MODE=line"
:key_loop
call :get_key
if not defined KEY goto :key_loop
if "!KEY!"=="!CR!" goto :on_enter
if "!KEY!"=="!ESC!" goto :on_quit
if /i "!KEY!"=="q" goto :on_quit
if /i "!KEY!"=="c" goto :on_clear
if /i "!KEY!"=="y" goto :on_paste
if "!KEY!"=="!BS!" goto :on_backspace
if "!KEY!"==" " goto :on_char
if "!KEY!"=="." goto :on_char
if "!KEY!"=="+" goto :on_operator
if "!KEY!"=="-" goto :on_operator
if "!KEY!"=="*" goto :on_operator
if "!KEY!"=="/" goto :on_operator
if "!KEY!"=="!CARET!" goto :on_operator
if "!KEY!"=="!PCT!" goto :on_operator
for %%d in (0 1 2 3 4 5 6 7 8 9) do if "!KEY!"=="%%d" goto :on_char
                               rem not a plain key: maybe ^V; its control
                               rem character is only made when first
                               rem needed, so startup costs nothing
if not defined CTRLV call :make_ctrlv
if "!KEY!"=="!CTRLV!" goto :on_paste
goto :key_loop

:on_char
set "LINE=!LINE!!KEY!"
if "!KEY!"==" " (
   <nul set /p "=.!DEL! "
) else (
   <nul set /p "=!KEY!"
)
goto :key_loop

:on_backspace
if not defined LINE goto :key_loop
set "LINE=!LINE:~0,-1!"
<nul set /p "=.!DEL!!DEL!"
goto :key_loop

rem  After each ENTER, C or operator the cursor is left at the start
rem  of a fresh line, so a newline is needed first only when
rem  something has been typed on the current line (LINE defined).

:on_clear
if defined LINE echo(
set "LINE="
set "X="
set "Y="
echo(=
goto :key_loop

:on_operator
<nul set /p "=!KEY!"
set "RPN_IN=!LINE! !KEY!"
set "RPN_PRE=!LINE!!KEY!"
call :calc
if not "!R_SAM!"=="1" echo(
goto :print_result

:on_enter
if defined LINE echo(
set "RPN_IN=!LINE!"
set "RPN_PRE="
call :calc
goto :print_result

:print_result
if defined R_ERM echo(!R_ERM!
if defined R_YLN echo(!R_YLN!
echo(!R_OUT!
set "LINE="
goto :key_loop

:on_paste
                               rem keys arriving between XCOPY /W reads
                               rem are lost, so a console paste never
                               rem gets here; instead the JScript part
                               rem reads the clipboard and plays it back
                               rem as keys typed after LINE; :calc shows
                               rem each finished screen line as it comes
set "RPN_IN=!LINE!"
set "RPN_PRE="
set "RPN_MODE=clip"
call :calc
set "RPN_MODE=line"
if not defined R_CLP (
                               rem CSCRIPT could not run at all
   if defined LINE echo(
   goto :print_result
)
                               rem an unfinished last line is shown with
                               rem no newline and stays typed; the "."
                               rem and DEL keep its leading spaces
set "LINE=!R_LIN!"
if defined R_PAR (
   <nul set /p "=.!DEL!!R_PAR!"
)
goto :key_loop

:on_quit
if defined LINE echo(
endlocal & endlocal & exit /b 0


rem ==================================================================
rem  :get_key
rem  Wait for one keystroke and return it in KEY without needing
rem  ENTER.  XCOPY /W echoes the key as the last character of its
rem  first output line; copying this file onto itself then fails
rem  harmlessly.
rem ==================================================================
:get_key
set "KEY="
for /f "delims=" %%k in ('xcopy /w "%SELF%" "%SELF%" 2^>nul') do (
   if not defined KEY set "KEY=%%k"
)
if defined KEY set "KEY=!KEY:~-1!"
exit /b


rem ==================================================================
rem  :make_ctrlv
rem  Set CTRLV to the ^V control character (code 22), or to "none"
rem  if it cannot be made, so that it never matches a key.
rem  imp: FORFILES turns 0xHH in its /C command into that character;
rem       its /P and /M just need some existing file, so this script
rem       is used.  Run once, on the first key that is not a plain
rem       key, which is when the delay of FORFILES is seen.
rem  BEWARE: most consoles treat ^V as their own paste and never
rem       pass it on as a key, and keys pasted that way are lost.
rem ==================================================================
:make_ctrlv
for %%F in ("%SELF%") do (
   for /f %%a in (
      'forfiles /p "%%~dpF." /m "%%~nxF" /c "cmd /c echo(0x16"'
   ) do set "CTRLV=%%a"
)
if not defined CTRLV set "CTRLV=none"
exit /b


rem ==================================================================
rem  :calc
rem  Run the tokens in RPN_IN against X and Y using the JScript
rem  part of this file.  Data goes in and out through environment
rem  variables so ^ %% * and friends need no quoting.
rem     in:  X, Y, RPN_IN, RPN_MODE (line, args or clip),
rem          RPN_PRE = text already on the screen line
rem     out: X, Y updated; R_OUT = text to print;
rem          R_ERM = error message to print first (line mode);
rem          R_YLN = Y line to print before R_OUT (line mode);
rem          R_SAM = 1 if R_OUT goes on the same screen line;
rem          R_STA = 0 ok, 1 error
rem          clip mode only: R_CLP = ok once the paste was handled
rem          (even if the clipboard could not be read: that is shown
rem          as an error line); R_PAR = an unfinished last line to
rem          show without a newline; R_LIN = the line typed so far
rem          after the paste
rem  Each line from the JScript part is a 3-letter tag, a space and
rem  a value, so values may start with = or spaces.  SCR lines are
rem  finished screen lines (clip mode) and are shown at once, in
rem  order.
rem ==================================================================
:calc
set "RPN_X=!X!"
set "RPN_Y=!Y!"
for %%v in (REX REY OUT ERM YLN SAM STA CLP PAR LIN) do set "R_%%v="
for /f "delims=" %%l in (
   'cscript //nologo //e:jscript "%SELF%" 2^>nul'
) do (
   set "_L=%%l"
   if "!_L:~0,3!" equ "SCR" (
      echo(!_L:~4!
   ) else (
      set "R_!_L:~0,3!=!_L:~4!"
   )
)
if not defined R_STA (
   set "R_ERM=error: cannot run CSCRIPT (Windows Script Host)"
   set "R_OUT=="
   if "!RPN_MODE!"=="args" set "R_OUT=!R_ERM!"
   set "R_SAM="
   set "R_STA=1"
   exit /b
)
set "X=!R_REX!"
set "Y=!R_REY!"
exit /b


rem ==================================================================
rem  :help
rem ==================================================================
:help
                               rem gitkw expands the Revision keyword on
                               rem commit; :~11,-2 keeps just its value
set "SELFVERSION=$Revision: 8.main-7 $"
echo(%~n0 !SELFVERSION:~11,-2!
echo(simple RPN (Reverse Polish Notation) calculator
echo(usage: rpn [expression]
echo(  rpn              interactive mode (no prompt)
echo(  rpn 2 5 *        evaluate an expression and print the result
echo(  rpn -h  or  /?   show this help
echo(
echo(Operators:
echo(  + - / * add, subtract, divide, multiply
echo(  %%       modulus
echo(  ^^ **    power
echo(On the command line, ^^ must be quoted:
echo(  rpn 2 20 "^"
echo(
echo(Floating point; results show at most 4 decimal places.
echo(Stack: X (top) and Y.  A number pushes X into Y; an operator
echo(computes Y op X into X and empties Y.
echo(
echo(Interactive keys:
echo(  C                clear the stack
echo(  Q or ESC         quit
endlocal & endlocal & exit /b 0

==================================================================
 JScript part.  Reads RPN_X, RPN_Y, RPN_IN, RPN_MODE and RPN_PRE
 from the environment; writes tagged lines REX, REY, OUT, ERM,
 YLN, SAM, STA, CLP, PAR and LIN, and in clip mode SCR lines, for
 the batch part.
================================================================== */

var env  = WScript.CreateObject("WScript.Shell").Environment("Process");
var sx   = env.Item("RPN_X");
var sy   = env.Item("RPN_Y");
var mode = env.Item("RPN_MODE");
var pre  = env.Item("RPN_PRE").length;
var raw  = env.Item("RPN_IN");
var NUM  = /^-?(\d+\.?\d*|\.\d+)$/;
var POINT    = 16;             // screen column of the decimal point
var MAXPASTE = 4000;

var X    = num(sx);
var Y    = num(sy);

/*
** num: number from a register string, null for an empty register
*/
function num(s) {
   return((s === "") ? null : parseFloat(s));
}

/*
** fmt: round to at most 4 decimal places, drop trailing zeros
** and a trailing point, keep one leading zero below 1.
*/
function fmt(x) {
   var neg = x < 0, a = Math.abs(x), s, n, ip, fp, f;
   if (a >= 9e11) {            // no room left for decimals
      s = String(Math.round(a));
   } else {
      n  = Math.round(a * 10000);
      ip = Math.floor(n / 10000);
      fp = n % 10000;
      s  = String(ip);
      if (fp > 0) {
         f = String(fp);
         while (f.length < 4) f = "0" + f;
         s += "." + f.replace(/0+$/, "");
      }
   }
   if (neg && s !== "0") s = "-" + s;
   return s;
}

/*
** show: "=" followed by x, padded so that the decimal point (shown
** or implied) of x falls in column POINT of a screen line that
** already holds col characters.  Just "=" when x is null.
*/
function show(x, col) {
   if (x === null) return "=";
   var s = fmt(x), dot = s.indexOf("."), n;
   n = POINT - (dot < 0 ? s.length : dot) - (col + 2);
   if (n < 1) n = 1;
   return "=" + new Array(n + 1).join(" ") + s;
}

/*
** yline: Y shown like show(Y, 0) but with a space for the "="
*/
function yline() {
   return(" " + show(Y, 0).substring(1));
}

/*
** run: evaluate the tokens of text, left to right, against X and Y
** usage: args is true on the command line, where an operator with
**        nothing in Y is an error
** post: returns { err, opEnd, lacking, nums }:
**          err     error message, "" if none
**          opEnd   the last token was an operator that was applied
**          lacking the last token was an operator with nothing in
**                  Y; it was ignored (interactive only)
**          nums    count of numbers pushed
**       on error X and Y are put back as they were
*/
function run(text, args) {
   var r    = { err: "", opEnd: false, lacking: false, nums: 0 };
   var oldX = X, oldY = Y, i, t, op, v;
   var toks;

   text = text.replace(/^\s+|\s+$/g, "");
   toks = (text === "") ? [] : text.split(/\s+/);
   for (i = 0; i < toks.length && !r.err; i++) {
      t         = toks[i];
      r.opEnd   = false;
      r.lacking = false;
      if (NUM.test(t)) {       // number: push
         Y = X;
         X = parseFloat(t);
         r.nums++;
         continue;
      }
                               // ** is another way to write ^
      op = (t === "**") ? "^" : t;
      if (op.length != 1 || "+-*/^%".indexOf(op) < 0) {
                               // no "!": the batch part cannot pass it
                               // through unharmed
         r.err = "error: not a number or operator: " +
                 t.replace(/!/g, "?");
         break;
      }
      if (Y === null) {        // too few operands
         if (args) {
            r.err = (X === null ? "?" : fmt(X)) + " ? " + t;
            break;
         }
                               // interactive: ignore the operator,
                               // keep what was pushed, go on with the
                               // rest of a pasted line
         r.lacking = true;
         continue;
      }
      switch (op) {
         case "+": v = Y + X; break;
         case "-": v = Y - X; break;
         case "*": v = Y * X; break;
         case "/":
            if (X === 0) r.err = "error: division by zero";
            v = Y / X;
            break;
         case "%":
            if (X === 0) r.err = "error: division by zero";
            v = Y % X;
            break;
         case "^":
            if (Y === 0 && X < 0) r.err = "error: division by zero";
            v = Math.pow(Y, X);
            break;
      }
      if (!r.err && isNaN(v))     r.err = "error: no real result";
      if (!r.err && !isFinite(v)) r.err = "error: overflow";
      if (!r.err) {
         X       = v;
         Y       = null;
         r.opEnd = true;
      }
   }
   if (r.err) {
      X = oldX;
      Y = oldY;
   }
   return(r);
}

/*
** showY: true when a line of only numbers (2 or more) ended with
** ENTER, the one case where Y is shown
*/
function showY(r) {
   return(!r.err && !r.opEnd && !r.lacking && r.nums >= 2 &&
          Y !== null);
}

/*
** readClip: the text on the clipboard ("" when it holds no text),
** or null when the clipboard cannot be read
** imp: uses the htmlfile COM object that comes with Windows, so no
**      PowerShell or extra program is needed
*/
function readClip() {
   var t;
   try {
      t = new ActiveXObject("htmlfile").parentWindow.
             clipboardData.getData("text");
   } catch (e) {
      return(null);
   }
   return((t === null || t === undefined) ? "" : String(t));
}

/*
** scr: send a finished screen line to the batch part, which shows
** it at once
*/
function scr(s) {
   WScript.Echo("SCR " + s);
}

/*
** results: send the lines that follow a finished line: any error,
** then Y when due, then = and X
*/
function results(r) {
   if (r.err)     scr(r.err);
   if (showY(r))  scr(yline());
   scr(show(X, 0));
}

/*
** pasteKeys: play the clipboard text back as if it had been typed
** after cur, the line typed so far (already on the screen)
** use: digits, "." and spaces (tabs too) are typed; a newline is
**      ENTER; an operator ends its line like a typed one; anything
**      else is ignored, as an unknown key would be (so C, Q and Y
**      in the text do NOT clear, quit or paste)
** post: returns { clp, par, lin }:
**          clp "ok": the paste was handled; a clipboard that cannot
**              be read or holds too much is shown as an error line
**              and what was typed is dropped
**          par text of an unfinished last line, to be shown
**              without a newline
**          lin the line typed so far after the paste
*/
function pasteKeys(cur) {
   var text = readClip(), pend = "", col = cur.length, i, c, r;
   var msg  = "";

   if (text === null) {
      msg = "error: cannot read the clipboard";
   } else if (text.length > MAXPASTE) {
      msg = "error: clipboard text longer than " + MAXPASTE +
            " characters";
   }
   if (msg) {
      if (col > 0) scr("");
      scr(msg);
      scr(show(X, 0));
      return({ clp: "ok", par: "", lin: "" });
   }
   text = text.replace(/\r\n?/g, "\n");
   for (i = 0; i < text.length; i++) {
      c = text.charAt(i);
      if (c === "\t") c = " ";
      if ("0123456789. ".indexOf(c) >= 0) {
         cur  += c;
         pend += c;
         col++;
      } else if (c === "\n") {
                               // ENTER: end the screen line if it holds
                               // anything, then evaluate what was typed
         if (col > 0) scr(pend);
         r    = run(cur, false);
         results(r);
         cur  = pend = "";
         col  = 0;
      } else if ("+-*/^%".indexOf(c) >= 0) {
                               // ** is power, as on the command line
         if (c === "*" && text.charAt(i + 1) === "*") {
            c = "**";
            i++;
         }
         pend += c;
         col  += c.length;
         r = run(cur + " " + c, false);
         if (r.lacking && !r.err) {
                               // nothing in Y: = and X on the same line
            scr(pend + show(X, col));
         } else {
            scr(pend);
            results(r);
         }
         cur  = pend = "";
         col  = 0;
      }
   }
   return({ clp: "ok", par: pend, lin: cur });
}

/****************************************************************/

var r, p, st = 0, sam = 0, out = "", erm = "", yln = "";
var clp = "", par = "", lin = "";

if (mode === "args") {
   r = run(raw, true);
   if (r.err) {
      out = r.err;
      st  = 1;
   } else if (X === null) {
      out = "error: empty expression";
      st  = 1;
   } else {
      out = fmt(X);
   }
} else if (mode === "clip") {
   p   = pasteKeys(raw);
   clp = p.clp;
   par = p.par;
   lin = p.lin;
} else {                       // interactive: always = and X
   r   = run(raw, false);
   erm = r.err;
   if (r.err) st = 1;
   if (r.lacking && !r.err) {  // same line as the operator
      out = show(X, pre);
      sam = 1;
   } else {
      out = show(X, 0);
   }
   if (showY(r)) yln = yline();
}

WScript.Echo("REX " + (X === null ? "" : String(X)));
WScript.Echo("REY " + (Y === null ? "" : String(Y)));
WScript.Echo("OUT " + out);
WScript.Echo("ERM " + erm);
WScript.Echo("YLN " + yln);
WScript.Echo("SAM " + sam);
WScript.Echo("STA " + st);
WScript.Echo("CLP " + clp);
WScript.Echo("PAR " + par);
WScript.Echo("LIN " + lin);
