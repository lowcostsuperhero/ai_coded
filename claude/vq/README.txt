vq - Visualize Query: diagram the structure of an Oracle SQL query
==================================================================

Reads an Oracle SQL query from a .sql file and draws its structure as
one interactive HTML page: the tables it reads, the columns it uses,
the columns it returns, the join and filter conditions, and how those
conditions are grouped by AND and OR. It only diagrams the query; it
does not connect to a database, optimize or tune anything.

Python 3.8 or later, plus the sqlglot parser:

   pip install sqlglot

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

Reading the diagram
-------------------
Tables are stacked top to bottom in FROM order. Each shows its name in
upper case with its alias, underlined, and below it only the columns
the query uses, spelled as in the query.

   ✓            the column is returned by the query
   1 2 3        position of the column in the ORDER BY list (after
                the ✓, or in its place)
   =, >, IN...  a line between two columns is a join condition; the
                operator sits nearer the column on the left hand side
                of the condition ("=" follows its neighbours, or sits
                in the middle)
   arrow        a slanted line with an arrowhead pointing at a column
                compares it to a value, written at the line's end
   U shape      a condition between two columns of the same table
   (+)          outer join, Oracle style, at the column end of the line
                on the optional table's side; ANSI LEFT / RIGHT / FULL
                joins are shown the same way

Lines between tables that are not next to each other run straight
through the tables in between, in the white space between column
names where possible.

Grouping:

   solid bar    ties together the lines of one AND group; a dot marks
                each line that belongs to it. Bars sit just below the
                column names for lines going down, just above them for
                lines coming from a table above
   dotted line  joins the alternatives of a nested OR
   no marking   at the top level, groups with nothing connecting them
                are OR'ed alternatives
   dash-dot bar a NOT group

So for

   select m.COL_1, m.COL_2, t.COL_1
      from THEIRTABLE t,
           MYTABLE m
      where t.COL_1 > m.COL_1 or
            (t.COL_4 = m.COL_2 and
             t.COL_2 = m.COL_1)

t.COL_1 > m.COL_1 is a lone line, and the two "=" lines are tied by a
bar at each end.

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

Each table appears once where possible: a reference to a CTE or an
inline view is folded into its query, and lines go straight to the
real columns inside the box. A reference is kept as a virtual table,
in a dotted box, only when folding would mislead: a column comes from
an expression or aggregate, the query is a set operation, the CTE is
referenced more than once, or alias.* is read. Purple arrows then show
which inner column feeds each of its columns.

In the page
-----------
Hover a line or a bar to highlight it with the rest of its group; the
condition's SQL shows at the top. The zoom slider scales the diagram,
and the SVG and PNG buttons download it as an image.

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

   CORNER_PX    rounding of connector corners (0 = pointed; default 3)
   ROW_PX       vertical spacing of stacked values
   GAP_PX       vertical space between tables

Files
-----
   vq.py        the program (single file)
   README.txt   this file
