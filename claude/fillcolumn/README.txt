fillcolumn - lay out a list of names in aligned columns
=======================================================

Takes a list of names, such as the columns of a sql select list or a
list of file names, and lays them out in columns that fit within a
maximum line length (78 by default). Names, aliases and delimiters line
up down the page. Python 3.6 or later, no extra packages.

Run
---
   python fillcolumn.py a b c d e f            names on the command line
   python fillcolumn.py "EMP_ID id, DEPT dept, HIRE_DATE hired"
   python fillcolumn.py -f names.csv           names from a file
   ls | python fillcolumn.py                   names from stdin
   python fillcolumn.py -c4 -f names.csv       exactly 4 columns
   python fillcolumn.py -o -f names.csv        fewest lines
   python fillcolumn.py -l60 -i3 -f names.csv  60 wide, indented by 3
   python fillcolumn.py -h                     all options

Example
-------
   $ python fillcolumn.py -l60 "EMP_ID id, FIRST_NAME first,
     LAST_NAME last, SALARY sal, DEPT dept, HIRE_DATE hired, MGR"
   EMP_ID id  , FIRST_NAME first, LAST_NAME last, SALARY sal,
   DEPT   dept, HIRE_DATE  hired, MGR

Options
-------
   -cColumns    fill exactly Columns columns per line
   -o           optimal: the column count that takes the fewest lines,
                then has the fewest spills; not with -c
   -dDelimiter  separator between names (see Input)
   -fFile       read names from File; - reads stdin; may be repeated
   -lLength     maximum output line length (78)
   -iIndent     indent every line by Indent spaces
   -iFirst,Indent
                indent by Indent, and adjust the first line by First:
                negative outdents, positive indents further, and 0 does
                not indent the first line at all
   -w           wrap: cut a name too long for any line at the line
                length and continue it on the next line, instead of
                letting the line run long
   -v           report the delimiter, column widths and spills on
                stderr
   -h           help

Option values may be attached (-l60) or separate (-l 60); -- ends the
options.

Input
-----
Without -d, the names are comma separated if a comma appears anywhere
in the input, otherwise whitespace separated. Quoting on the command
line does not matter: the arguments are put back together as if they
had not been quoted.

   whitespace   any run of whitespace separates names; there are no
                blank names
   -d"   "      a run of at least that many spaces separates names, so
                a name may contain shorter runs of spaces
   , or other   with a delimiter that is not whitespace, blank names
                are kept (a,,b), a single trailing delimiter is
                ignored, and the last word of a name with more than one
                word is its alias, as in a sql select list:
                "LAST_NAME last" is the name LAST_NAME, alias last

How the columns are laid out
----------------------------
   - Each column is only as wide as the longest name in it that does
     not spill.
   - Within a column, aliases all start in the same place, with the
     longest alias ending at the delimiter. Only names that have an
     alias push that place to the right.
   - Delimiters line up at the ends of columns, unless that would put
     them past the line length.
   - A name spills into the next column(s) only when the columns are
     too wide for the line together. The names that spill are chosen
     one at a time, each time the one whose spill narrows the columns
     the most. The name after a spilled one starts in the next free
     column.
   - A name that would run past the line length moves to the next
     line. A name too long for any line stays where it is and the line
     runs long (exit code 1, with a warning), or with -w is cut and
     continued.

Choosing the number of columns:

   default   the most columns where no name spills, unless a spill
             cannot be avoided
   -o        the column count that takes the fewest lines, then has the
             fewest spills
   -c        exactly the count given

The most columns tried is the number of names that fit on the first
line without padding.

Exit codes
----------
   0   success
   1   a line runs past the line length
   2   a file could not be read
   3   bad command line

Planned
-------
   - a maximum width per column
   - more choices for handling names that do not fit

Files
-----
   fillcolumn.py   the program (single file); it reports whatever
                   name it is saved under
   README.txt      this file
