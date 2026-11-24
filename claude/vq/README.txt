vq - Visualize Query: diagram the structure of an Oracle SQL query
==================================================================

Reads an Oracle SQL query from a .sql file and draws its structure as
one interactive HTML page: the tables it reads, the columns it uses,
the columns it returns, the join and value conditions, and how those
conditions are grouped by AND and OR. It only diagrams the query; it
does not connect to a database, optimize or tune anything.

Python 3.9 or later, plus the sqlglot parser (built and tested with
30.33.0):

   pip install sqlglot==30.33.0

On a system with no internet access, download the wheel elsewhere and
install it from the file:

   pip download sqlglot==30.33.0 --no-deps --only-binary=:all: -d vq_offline
   (copy vq_offline to the target system)
   pip install --no-index --find-links=vq_offline sqlglot==30.33.0

sqlglot is pure Python with no required dependencies, so the one wheel
(sqlglot-30.33.0-py3-none-any.whl) works on Windows and Linux alike.

Run
---
   python vq.py query.sql                  writes query.html
   python vq.py -oout.html query.sql       choose the output file
   python vq.py -v query.sql               list each query block
   python vq.py -h                         all options

A file may hold several statements, separated by ";" or by a "/" on
its own line (SQL*Plus style); each gets its own diagram on the page.

Options
-------
   -oOutput     output file (default: the .sql file's name, .html)
   -fFormat     output format: html (the only one so far)
   -v           verbose; more -v for more verbosity
   -h           help

Tables and columns
------------------
Tables are stacked top to bottom in FROM order. Each shows its name
in upper case with its alias, underlined, and below it only the
columns the query uses, spelled as in the query (matched without
regard to case, as Oracle does).

   ✓            the column is returned by the query
   1 2 3        position of the column in the ORDER BY list (after
                the ✓, or in its place); an ORDER BY alias or position
                counts for the select list entry it names

Joins and values
----------------
   connector    a line between columns of two tables is a join
   operator     sits on the line just off the column name, blanking
                the line behind it: "=" and "<>" at both ends, others
                (>, IN, LIKE ...) only at the end of the column on the
                left hand side of the condition
   (+)          outer join, Oracle style, beside the operator: on the
                left when the column name is above it, on the right
                when it is below; ANSI LEFT / RIGHT / FULL joins are
                shown the same way
   arrow        a value compared to a column: the column runs down to
                where it meets its group's bar, and a 45 degree arrow
                from the value points at that spot
   U shape      a condition between two columns of the same table

Lines between tables that are not next to each other run straight
through the tables in between, in the white space between column
names where possible, ordered so that they do not twist round each
other.

AND and OR
----------
   bar          a horizontal bar (black) holds one AND group at one
                side of a table: its columns join it by short stubs,
                and one connector runs from the bar to each other
                table it joins; an open circle marks a line that
                meets or crosses the bar instead of ending on it as a
                stub (a value's arrow, or a join that cannot share a
                connector)
   separate     bars not tied together are OR'ed alternatives; side
                by side on one level they need no other mark
   [ ]          encloses what is AND'ed, with a stub to each term (the
                vertical equivalent of a bar)
   ( )          encloses what is OR'ed, one arc per OR, meeting at
                each term between the first and the last
   dash-dot [ ] a NOT
   dotted line  joins OR'ed alternatives that never meet at one table

Enclosures run a little past their first and last terms and are
closed into a box by grey dots; a nested one sits inside its parent,
and none sits on a vertical connector line.

Joins between the same two bars share one connector only when their
columns pair up in the same left to right order at both ends and no
other join sits between them on a bar; otherwise they stay separate
lines.

So for

   select m.COL_1, m.COL_2, t.COL_1
      from THEIRTABLE t,
           MYTABLE m
      where t.COL_1 > m.COL_1 or
            (t.COL_4 = m.COL_2 and
             t.COL_2 = m.COL_1)

t.COL_1 > m.COL_1 is a lone line, and the two "=" joins stub onto a
bar at each table with one connector between the bars.

Subqueries, CTEs and set operations
-----------------------------------
Every query block is drawn in the same diagram:

   - CTEs come first, in a purple box set out to the left
   - subqueries and inline views are boxed and set in to the right,
     after the tables of the block that contains them, labelled with
     what they are (Q2 . NOT EXISTS subquery, IN subquery, inline
     view v, ...)
   - a correlated condition draws straight to the outer table
   - "x IN (select y ...)" links x to the column the subquery returns
   - UNION, UNION ALL, INTERSECT and MINUS branches are drawn one
     below the other, with the operator between them

Each table appears once: a reference to a CTE or an inline view is
folded into its query, and lines go straight to the real columns
inside its box. A column made from an expression is drawn to every
column in it; one made from no column at all (rownum, a literal) has
nowhere to go, and a condition on it is listed under "not drawn".

With VIRTUAL_TABLES = True (top of vq.py) a reference that would
mislead when folded (an expression or aggregate column, a set
operation, a CTE used twice, alias.*) is kept as a virtual table in a
dotted box, with purple arrows showing which inner column feeds each
of its columns.

In the page
-----------
Hover a line, bar or enclosure to highlight it with the rest of its
group; the condition's SQL shows in the yellow bar at the top. The
Legend and the SQL source are folded away and open on a click. The
zoom slider scales the diagram, and the SVG and PNG buttons download
it as an image.

Limits
------
   - unqualified columns can only be placed when the block reads one
     table; otherwise they go in an "(unqualified)" box until the data
     dictionary can be used
   - GROUP BY and HAVING columns are shown, their conditions are not
     drawn
   - EXISTS conditions are shown by their subquery's box

Planned
-------
   - read the query from an Oracle database
   - mark columns that an index can use ("*") and primary key columns
     with their position in the key
   - more output formats (PNG / JPG)

Settings
--------
Layout constants at the top of vq.py, for example:

   CORNER_PX       rounding of connector corners (0 = pointed; 3)
   GAP_PX          space between a table and the one below (36)
   ROW_PX          spacing of stacked value labels (13)
   OP_ROW_PX       height of the operator row by the columns (18)
   ENCLOSE_PX      how far [ ] and ( ) run past their terms (6)
   FILTER_TILT     angle of value arrows (45 degrees)
   BAR_COLOR       colour of the grouping bars (black)
   VIRTUAL_TABLES  keep misleading CTE / inline view references as
                   virtual tables (False)

Files
-----
   vq.py        the program (single file)
   README.txt   this file
