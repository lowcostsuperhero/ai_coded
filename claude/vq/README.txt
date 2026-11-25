vq - Visualize Query: diagram the structure of an Oracle SQL query
==================================================================

Reads an Oracle SQL query from a .sql file and draws its structure as
one interactive HTML page: the tables it reads, the columns it uses,
the columns it returns, the join and value conditions, and how those
conditions are grouped by AND and OR. Given a database login and a
SQLID, it reads the statement from the database instead, and with a
login it also marks which join columns an index can serve. It only
diagrams the query; it does not optimize or tune anything.

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
   python vq.py -q 7h35uxf5uhmm1 system@orcl
                                           writes 7h35uxf5uhmm1.html
   python vq.py query.sql scott@orcl       query.html with index marks

Any argument with an "@" is a database login, any other is the .sql
file; they may come in either order. A file whose name has an "@"
must be given with -f.

A login is one of the following, resolved the way getconnection does:
   user/password@db  as given
   user@db           password from ~/.credential/user@db (or
                     ~/etc/.credential, ~/etc/credential; directory
                     mode 700, file 600), otherwise prompted for
   /@db              "/ as sysdba" to the local db
   user/@db          SEPS wallet ~/.seps_wallet/user/ewallet.p12
sqlplus is run from the ORACLE_HOME found in oratab (Linux) or the
registry (Windows); the password goes to it on stdin, not on its
command line.

A SQLID's text is taken from the first of GV$SQL, GV$SQLSTATS,
DBA_HIST_SQLTEXT (AWR: needs the Diagnostics Pack licence) and
DBA_SQLSET_STATEMENTS that has it; views the login cannot read are
skipped. The parsing schema (from GV$SQL, DBA_HIST_SQLSTAT or the
tuning set) decides which schema unqualified table names belong to;
for a file it is the login's current schema.

With a login, each join column is marked at its top right corner:
   ★            a leading column of an index: every index column
                before it is also used by some condition on that
                table in the query (so (JOB, DEPTNO) serves a join on
                DEPTNO when the query also has a condition on JOB)
   ☆            in an index, but not among its leading columns
   (no mark)    in no index
Hover near a mark for a popup listing each index that holds the
column, side by side: its name (underlined), then its columns one
per line, those the query uses in bold. Invisible and unusable
indexes are ignored. Index details are read from ALL_TABLES,
ALL_VIEWS, ALL_SYNONYMS, ALL_INDEXES and ALL_IND_COLUMNS, so a table
the login cannot see gets no marks. A name the database says is a
view is shown with "(view)" after its alias, as "NAME alias (view)".

A file may hold several statements, separated by ";" or by a "/" on
its own line (SQL*Plus style); each gets its own diagram on the page.

Options
-------
   -oOutput     output file (default: the .sql file's name, or the
                SQLID, with .html)
   -FFormat     output format: html (the only one so far)
   -fFile       the .sql file; needed only if its name has an "@"
   -qSQLID      read the statement with this SQLID from the database;
                needs a login, and cannot be used with a file
   -v           verbose: lists each query block, and where a SQLID's
                text was found; more -v for more verbosity
   -h           help

Tables and columns
------------------
Tables are stacked top to bottom in FROM order. Each shows its name
in upper case with its alias, underlined, and below it only the
columns the query uses, spelled as in the query (matched without
regard to case, as Oracle does). Columns are laid out left to right
in the order they first appear in the SQL text.

   ✓            the column is returned to the user by the whole
                query (top left). A column a subquery, CTE or inline
                view returns gets one only if the top level query
                passes it on (a scalar subquery in the top level
                select list does)
   'CURRENT'    in italics: a select list entry that is a bare value
                (a string or number), shown quotes and all as a column
                of the first table in FROM, checked when returned
   JOB          in italics, no ✓: a GROUP BY column that is not in the
                select list, so not in the rows returned
   1 2 3        position of the column in the ORDER BY list (before
                the ✓, or in its place, just before the name); an
                ORDER BY alias or position counts for the select list
                entry it names
   1↓           the item is sorted DESC (the arrow sits between the
                position and the ✓)
   AVG(SAL)     in italics: an aggregate (in the select list or
                compared in HAVING) is a virtual column of its own,
                just right of the column it reads, with an arrow from
                it (running under its ✓ and ORDER BY position):
                SAL -> AVG(SAL). Further aggregates of the same
                column follow, each arrowed from the one before:
                SAL -> AVG(SAL) -> MAX(SAL). An aggregate of several
                columns follows the first; the others come right
                after it, arrowed back into it:
                QTY -> SUM(QTY * PRICE) <- PRICE. Joins, ORDER BY and the
                ✓ go to the aggregate, not the column. COUNT(*) reads
                no column and stands alone on the first table in FROM;
                a HAVING condition is drawn like any other, e.g. an
                arrow labelled "> 3"

Levels, stubs and joins
-----------------------
Every column used in a condition has a stub, a short vertical line
out from its name (down below the columns, or up above them for
lines arriving from a table higher up). The space by the column
names is divided into levels, separated by faint grey lines:

   level        conditions on one level are AND'ed; conditions on
                different levels are OR'ed
   stub end     a stub ends at the level of its condition; a value's
                arrow points at that spot (stopping just short)
   filled dot   a column taking part in more than one level has one
                stub, to its farthest level, with a dot at each other
                level it takes part in; a dot also marks where a line
                arrives at a stub with no operator at that end
   operator     sits on the stub at its level, blanking the line
                behind it: "=" and "<>" at both ends, others (>, IN,
                LIKE ...) only at the end of the column on the left
                hand side; two on one stub at one level sit either
                side of it
   (+)          outer join, Oracle style, beside the operator; ANSI
                LEFT / RIGHT / FULL joins are shown the same way
   connector    a line from a stub to a column of another table is a
                join; it leaves straight down the stub, or branches
                off beside it when the stub carries more
   arrow        a value compared to a column, from the value to the
                end of the stub (or its dot); BETWEEN with two values
                is one arrow, "BETWEEN 1 AND 100"
   U shape      a condition between two columns of the same table,
                its corners cut at 45 degrees, its operator on the
                flat part, nearer the column on the left hand side
   bar          only when two or more columns of one table are
                joined, on one level, to columns of the same other
                table: their stubs end on a thick bar from the
                leftmost of them to the rightmost, and one connector
                (thinner than the bar) runs between the two bars
   partners     when the columns on the two bars do not pair up left
                to right, each stub shows its partner column in small
                grey, between its operator and the bar
   hop          a vertical line that crosses a bar without being part
                of it jumps over it with a small curve, like a
                backward C

Lines between tables that are not next to each other run straight
through the tables in between, in the white space between column
names where possible, ordered so that they do not twist round each
other. A subquery's own conditions share the first level of the
query around it, being AND'ed with it.

Grouping across levels
----------------------
   [ ]          encloses levels that are AND'ed together (overriding
                the levels-are-OR'ed rule)
   ( )          encloses what is OR'ed, one arc per OR, meeting at
                each term between the first and the last
   NOT [ ]      a NOT: the word NOT against the left of a [ ]
   dotted line  joins OR'ed alternatives that never meet at one table

Inside an enclosure the same rules hold: one level AND'ed, different
levels OR'ed. Enclosures are closed into a box by grey dots; a nested
one sits inside its parent, and none sits on a vertical line.

A filter normally hangs below the column names. When the group it is
in (short of the whole WHERE) has joins at the same table that all
arrive from above, the filter goes above the names too, so the group
is drawn on one side: its stub rises to its level and its value's
arrow comes down to it from the upper right, from above every level.
In

   left join SHIPMENTS s on s.ORDER_ID = o.ORDER_ID and
                            (s.STATUS = 'SHIPPED' or
                             s.SHIPPED_ON > o.ORDER_DATE)

s.STATUS and s.SHIPPED_ON sit on two levels above SHIPMENTS' names,
in an OR arc, and a [ ] ANDs that arc with the level of
s.ORDER_ID = o.ORDER_ID.

A column with more than one connector on one side (a join to
another table, a line across to a box beside, or one end of a
same-table U) gets a separate stub per connector, side by side, so
no stub forks; values and bar stubs stay on the column's own stub.

So for

   select m.COL_1, m.COL_2, t.COL_1
      from THEIRTABLE t,
           MYTABLE m
      where t.COL_1 > m.COL_1 or
            (t.COL_4 = m.COL_2 and
             t.COL_2 = m.COL_1)

t.COL_1 > m.COL_1 is a connector on the first level; the two "="
joins are on the second level, their stubs ending on a bar at each
table with one connector between the bars.

Subqueries, CTEs and set operations
-----------------------------------
Every query block is drawn in the same diagram:

   - CTEs come first, in a purple box set out to the left
   - a box whose tables were all folded away (an IN subquery that
     only reads a CTE, say) is not drawn; its lines run on to the
     columns that produce the values
   - the select list of an EXISTS subquery (select 1) is not shown
   - subqueries and inline views are boxed and set in to the right,
     after the tables of the block that contains them, labelled with
     what they are (Q2 . NOT EXISTS subquery, IN subquery, inline
     view v, ...)
   - a correlated subquery in the SELECT list that reads one table
     sits beside the outer table it correlates to instead, right of
     the other boxes, its top level with the underline of that table's name;
     the line runs down the outer column's stub past its operator,
     on below the table's levels, then across, as it would to a box
     below; the rightmost column's line runs highest, each one to its
     left a little lower, so they do not cross; when the column has
     other connectors the line gets its own stub. A box beside clears
     the value labels of the tables to its left
   - a correlated condition draws straight to the outer table; for
     an EXISTS or NOT EXISTS subquery the line carries those words
     where it enters the subquery's box (centred on the line, or
     just beside it when another line enters the box close by), and
     it ends at the outer table on the level of the EXISTS condition
     itself
   - "x IN (select y ...)" links x to the column the subquery
     returns; the line runs straight into it, with no dot. So do
     "x > ALL (select y ...)" and "x = ANY (select y ...)", the
     operator at x reading "> ALL" or "= ANY"; against a list of
     values, ANY / ALL is an ordinary value label
   - UNION, UNION ALL, INTERSECT and MINUS branches are drawn one
     below the other, with the operator between them. The right
     side of a MINUS only removes rows, so its columns get no ✓.
     An ORDER BY of the whole set operation numbers the matching
     select list entry (by position, or by the first branch's
     names) in every branch that returns rows

Each table appears once: a reference to a CTE or an inline view is
folded into its query, and lines go straight to the real columns
inside its box. A column made from an expression is drawn to every
column in it; one made from no column at all (rownum, a literal) has
nowhere to go, and a condition on it is listed under "not drawn".

A CTE read more than once is the exception: folding both references
onto the same columns would turn the joins between them into
nonsense, so each reference is always kept as a table of its own, in
a dotted box, with purple arrows showing which column in the CTE
feeds each of its columns.

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
   - GROUP BY columns are shown, the grouping itself is not; HAVING
     conditions are drawn only where an aggregate is compared
   - EXISTS conditions are shown by their subquery's box

Planned
-------
   - primary key columns marked with their position in the key
   - more output formats (PNG / JPG)

Settings
--------
Layout constants at the top of vq.py, for example:

   CORNER_PX       rounding of connector corners (0 = pointed; 3)
   GAP_PX          space between a table and the one below (36)
   ROW_PX          spacing of stacked value labels (13)
   LEVEL_PX        depth of a level without a bar (18)
   FILTER_TILT     angle of value arrows (45 degrees)
   BAR_COLOR       colour of the multi-column join bars (as the
                   stubs, dark grey)
   VIRTUAL_TABLES  keep misleading CTE / inline view references as
                   virtual tables (False)

Files
-----
   vq.py        the program (single file)
   README.txt   this file
