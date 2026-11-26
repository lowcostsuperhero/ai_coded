#!/usr/bin/python
# name: $Id: vq.py 5.main-5 12:50:15 26-Nov-2026 claude-opus-5-5 $
# use: draws the structure of an Oracle SQL query (tables, referenced
#      columns, returned columns, join and filter predicates, and the
#      AND/OR grouping of those predicates) as an interactive HTML page
# usage: vq.py [-oOutput] [-FFormat] [-fFile] [-qSQLID] [-v] [-h]
#              [file.sql] [user/password@database]

import getopt
import getpass
import html
import itertools
import json
import math
import os
import platform
import re
import stat
import subprocess
import sys

from pathlib import Path

E_SUCCESS = 0
E_WARN    = 1
E_FAIL    = 2
E_USAGE   = 3

try:
   import sqlglot
   from sqlglot        import exp
   from sqlglot.errors import ParseError
except ImportError:
   print("vq: the sqlglot module is required"
         " (pip install sqlglot)", file = sys.stderr)
   sys.exit(E_FAIL)

REVISION     = "$Revision: 5.main-5 $"
FORMATS      = ("html",)

                               # characters outside plain ascii, built
                               # from their code points so that none
                               # appears in this script
ELLIPSIS     = chr(0x2026)
MIDDLE_DOT   = chr(0x00B7)
EM_DASH      = chr(0x2014)
LEFT_ARROW   = chr(0x2190)
STAR_FULL    = chr(0x2605)
STAR_OPEN    = chr(0x2606)

CHAR_PX      = 7.9
NAME_CHAR_PX = 8.7
SMALL_PX     = 6.7
SLOT_PX      = 7
                               # spacing of the separate stubs of a
                               # column with more than one line
STUB_SEP_PX  = 14
                               # the same for a same-table U's stub
U_SEP_PX     = 6
                               # gap between the boxes on the left and
                               # a SELECT list subquery's box beside
SIDE_GAP_PX  = 12
                               # spacing of the lines across to boxes
                               # beside a table, below its levels
TRACK_PX     = 6
COL_GAP_PX   = 24
                               # a level: its marker (operator, value
                               # arrow, stub end) sits LEVEL_MID_PX in
                               # from its edge nearer the column names;
                               # a level with no bar is LEVEL_PX deep
LEVEL_PX     = 18
LEVEL_MID_PX = 9
LEVEL_GAP_PX = 3
                               # from a bar member's operator to its bar,
                               # and to it past a partner's name
BAR_STUB_PX  = 13
                               # half the width of a stub line (.ln)
STUB_HALF_PX = 0.55
PARTNER_PX   = 25
ROW_PX       = 13
                               # space kept between a value's label and
                               # any other value's line
LABEL_CLEAR_PX = 8
GAP_PX       = 36
MARGIN_PX    = 30
PASS_PX      = 6
MARK_PX      = 13
                               # length of the check's short leg
CHECK_SHORT_PX = 2.6
CHECK_LEAN_DEG = 18
CHECK_PX     = 9
ORDER_PX     = 6.6
                               # width of the ORDER BY DESC arrow
DESC_PX      = 7
                               # cut of a same table U's corners
CHAMFER_PX   = 5
                               # least length of an aggregate's arrow
                               # left of its check / ORDER BY position
AGG_GAP_PX   = 10
                               # a value compared to a column hangs off
                               # it at 45 degrees from vertical
FILTER_TILT  = math.tan(math.radians(45))
                               # gap left between a value's arrowhead
                               # and the point it aims at
ARROW_GAP_PX = 4
                               # least drop of the slanted part of a
                               # value's line, so its arrow has room
FILTER_REACH_PX = 14
HOP_PX       = 4
                               # room for the word NOT left of a bracket
NOT_PX       = 22
CORNER_PX    = 3
INDENT_PX    = 28
BOX_PAD_PX   = 14
BOX_TITLE_PX = 24
LABEL_MAX    = 30
                               # False: a CTE reference or inline view
                               # always folds into its query, and lines
                               # go to the real tables there; True: one
                               # that would mislead when folded stays as
                               # a virtual table in a dotted box
VIRTUAL_TABLES = False

                               # colour of the bars of multi-column
                               # joins: that of the stubs (.ln)
BAR_COLOR    = "#4b5563"
PALETTE      = ("#2563eb", "#d97706", "#059669", "#db2777",
                "#7c3aed", "#0891b2", "#65a30d", "#dc2626")

OPERATORS    = {exp.EQ   : "=" ,
                exp.NEQ  : "<>",
                exp.GT   : ">" ,
                exp.GTE  : ">=",
                exp.LT   : "<" ,
                exp.LTE  : "<=",
                exp.Like : "LIKE",
                exp.Is   : "IS"}

FLIP         = {">" : "<" ,
                ">=": "<=",
                "<" : ">" ,
                "<=": ">="}

                               # unqualified oracle pseudo columns are
                               # not columns of any table
PSEUDO       = {"ROWNUM", "LEVEL", "SYSDATE", "SYSTIMESTAMP", "USER",
                "UID", "CONNECT_BY_ISLEAF", "CONNECT_BY_ISCYCLE"}

                               # the ones that describe a row: drawn
                               # as a virtual column, in italics
ROW_PSEUDO   = {"ROWNUM", "LEVEL", "CONNECT_BY_ISLEAF",
                "CONNECT_BY_ISCYCLE"}

                               # sqlglot renamed Union's base class at
                               # some point; accept either spelling
SETOP        = getattr(exp, "SetOperation", exp.Union)

###########################################################################
###
### e x c e p t i o n s
###
###########################################################################

class VqError(Exception):
   """
   use: error reported to the user, then the script exits
   imp: mirrors ExceptionClass (message, exec_name, print_error) so it
        can be rebased onto that class when the lib is available
   """
   def __init__(self, message, exec_name = "", exit_code = E_FAIL):
      super().__init__(message)
      self.exit_code = exit_code
      self.message   = (     f"{exec_name}: {message}" if (exec_name)
                        else f"{message[:1].upper()}{message[1:]}")

   ########################################################################

   def print_error(self):
      print(self.message, file = sys.stderr)

###########################################################################
###
### m o d e l
###
###########################################################################

class ColumnRef:
   """
   use: one column of a table, as referenced by the query
   """
   def __init__(self, name):
      self.name     = name
      self.returned = False
                               # how an index serves a join column
                               # (see mark_indexes): the glyph, its
                               # style class, and the indexes holding
                               # it as [index, leading?, [(column,
                               # used by the query?)]]
      self.marker       = None
      self.marker_class = ""
      self.marker_index = []
      self.slots    = {"up": [], "down": []}
                               # position in the ORDER BY list, if any
      self.order_by = None
      self.order_desc = False
                               # an aggregate (COUNT(*)) shown as a
                               # column for a HAVING condition
      self.virtual  = False
                               # in GROUP BY, not in the select list
      self.implied  = False
                               # for an aggregate, the column it reads
                               # (drawn to its left, arrowed to it)
      self.source   = None
      self.after    = None
                               # other columns it reads (SUM(QTY *
                               # PRICE): PRICE), drawn just right of
                               # it, arrowed back to it; for such a
                               # column, the aggregate it feeds
      self.extra    = []
      self.feeds    = None
                               # offset in the SQL text of its first
                               # mention: columns are laid out in the
                               # order they first appear
      self.first    = None
      self.x        = 0.0
      self.width    = 0.0

###########################################################################

class TableRef:
   """
   use: a table, view, cte or inline view in the FROM of a query block
   """
   def __init__(self, key, name, alias, kind):
      self.key        = key
      self.name       = name
      self.alias      = alias
      self.kind       = kind
      self.columns    = {}
      self.scope      = None
      self.index      = 0
      self.left       = 0.0
      self.right      = 0.0
      self.name_right = 0.0
      self.bands      = {"up": 0, "down": 0}
                               # the table a SELECT list subquery's
                               # table is drawn beside, if any
      self.beside     = None
      self.rows       = 0
                               # the query that fills a CTE reference or
                               # inline view; None for everything else,
                               # including tables made up for columns
                               # that match no FROM table
      self.body       = None
                               # a CTE reference / inline view that
                               # could not be folded into its query
      self.virtual    = False
                               # owner and name as the dictionary has
                               # them, for reading index details
      self.db_owner   = None
      self.db_name    = name
                               # the dictionary says it is a view
      self.db_view    = False

   ########################################################################

   def column(self, key, display = None, first = None):
      """
      post: returns the ColumnRef for key, creating it if needed; the
            first spelling seen is the one displayed; first is where in
            the SQL text this mention is, if known
      """
      if (key not in self.columns):
         self.columns[key] = ColumnRef(display or key)
      column = self.columns[key]
      if ((first is not None) and
          ((column.first is None) or (first < column.first))):
         column.first = first
      return(column)

   ########################################################################

   def title(self):
      name = self.name
      if (self.alias and self.alias.upper() != self.name.upper()):
         name = f"{name} {self.alias}"
      return(name + (" (view)" if (self.db_view) else ""))

###########################################################################

class Endpoint:
   """
   use: one end of a predicate segment, attached to a column
   """
   def __init__(self, table, column, join_mark = False):
      self.table     = table
      self.column    = column
      self.join_mark = join_mark
                               # bar the end is drawn on as a stub; the
                               # next layout run gives stubs of one
                               # column on one bar a single slot
      self.merge     = None
                               # for a value compared to a column: how
                               # far below the column names its slanted
                               # line leaves the column
      self.join_off  = 0.0
      self.column_key = next((k for (k, c) in table.columns.items()
                              if (c is column)), None)
      self.side      = "down"
      self.segment   = None
      self.x         = 0.0

###########################################################################

class Segment:
   """
   use: one drawn line of a predicate: column to column of another
        table (join), of the same table (self), or to a value (filter)
   """
   def __init__(self, a, b, op):
      self.a       = a
      self.b       = b
      self.op      = op
      self.kind    = (     "filter" if (b is None)
                      else "self"   if (a.table is b.table)
                      else "join")
      self.upper   = a
      self.lower   = b
                               # the trunk this join is drawn on, if any
      self.trunk   = None
                               # endpoints sharing a merge key share one
                               # stub (set by one layout run for the next)
      self.members = []
      self.row     = 0
                               # x where the line crosses each table
                               # between its two ends
      self.via     = []
      self.lean    = "middle"
      a.segment    = self
      if (b is not None):
         b.segment = self

   ########################################################################

   def ends(self):
      return((self.a,) if (self.b is None) else (self.a, self.b))

###########################################################################

class Unit:
   """
   use: one horizontal bar at one side of one table: the conditions of
        one AND group, or of one OR alternative, that touch that side;
        everything on one bar is AND'ed together
   """
   def __init__(self, ident, node, table, side, ends):
      self.ident  = ident
      self.node   = node
      self.table  = table
      self.side   = side
      self.ends   = ends
      self.color  = node.color if (node is not None) else "#4b5563"
      self.band   = 0

###########################################################################

class Mark:
   """
   use: encloses bars and single lines at one side of one table when
        parentheses nest, with a delimiter left and right: [ ] for AND
        (joining levels), ( ) for OR, [ ] with the word NOT on its
        left for NOT; a nested enclosure sits inside its parent
   """
   def __init__(self, kind, node, children, depth):
      self.kind     = kind
      self.node     = node
      self.children = children
      self.depth    = depth
      self.color    = node.color
      self.table    = None
      self.side     = None
      self.left     = 0.0
      self.right    = 0.0

###########################################################################

class Leaf:
   """
   use: one predicate (comparison, IN, BETWEEN, ...) of a query block
   """
   def __init__(self, sql):
      self.ident     = ""
      self.sql       = sql
                               # a CTE / inline view column link, not a
                               # predicate
      self.feed      = False
      self.segments  = []
      self.ancestors = []
                               # an EXISTS has nothing to attach to; its
                               # subquery box shows it instead
      self.silent    = False
                               # for [NOT] EXISTS: its subquery's SELECT
      self.exists    = None

###########################################################################

class Node:
   """
   use: an AND, OR or NOT grouping of predicates
   """
   def __init__(self, kind, sql = ""):
      self.ident    = ""
      self.kind     = kind
      self.sql      = sql
      self.children = []
      self.depth    = 0
      self.color    = PALETTE[0]
      self.is_root  = False
                               # dotted lines tying an OR's alternatives
      self.links    = []

   ########################################################################

   def endpoints(self):
      """
      post: returns every endpoint of every leaf below this node
      """
      return([end for leaf in iter_leaves(self)
                  for segment in leaf.segments
                  for end in segment.ends()])

   ########################################################################

   def descendants(self):
      result = []
      for child in self.children:
         if (isinstance(child, Node)):
            result.append(child)
            result.extend(child.descendants())
      return(result)

###########################################################################

def iter_leaves(tree):
   if (isinstance(tree, Leaf)):
      yield(tree)
      return
   for child in tree.children:
      yield from iter_leaves(child)

###########################################################################

class Scope:
   """
   use: one query block (one SELECT); all but the main one are drawn
        in a box: indented for subqueries, outdented for CTEs
   """
   def __init__(self, select, label, context, parent):
      self.select     = select
      self.label      = label
      self.context    = context
      self.parent     = parent
      self.tables     = {}
      self.sources    = []
      self.placed     = []
      self.root       = None
      self.leaves     = []
      self.nodes      = []
      self.notes      = []
      self.indent     = 0
      self.boxed      = False
      self.left       = 0.0
                               # the outer table a SELECT list
                               # subquery's box is drawn beside
      self.host       = None
      self.box_top    = 0.0
      self.box_bottom = 0.0

   ########################################################################

   def ordered_tables(self):
      """
      post: FROM order, then tables of unknown qualifiers, then the
            pseudo table of unresolved columns
      """
      tables = list(self.tables.values())
      return(  [t for t in tables if (t.kind not in ("unknown", "?"))]
             + [t for t in tables if (t.kind == "unknown")]
             + [t for t in tables if (t.kind == "?")])

###########################################################################

class Diagram:
   """
   use: one statement: its scopes and the top to bottom sequence of
        items to draw ("open"/"close" a box, "table", "sep" between
        set operation branches)
   """
   def __init__(self, sql, scopes, items):
      self.sql      = sql
      self.scopes   = scopes
      self.items    = items
      self.width    = 0.0
      self.height   = 0.0
      self.right    = 0.0
      self.notes_y  = 0.0
      self.seps     = []
      self.feeds    = []
      self.units    = []
      self.marks    = []
      self.trunks   = []
      self.layout   = None

###########################################################################
###
### p a r s i n g
###
###########################################################################

def to_sql(expression):
   """
   post: returns expression as oracle sql, with any sql*plus
         substitution variable (see sqlplus_text) spelled as written
   """
   return(re.sub(r":VQAMP([12])_(\w+)",
                 lambda m: "&" * int(m.group(1)) + m.group(2),
                 expression.sql(dialect = "oracle")))

###########################################################################

                               # sql*plus commands that may sit in a
                               # script around the queries, matched on
                               # their shortest abbreviation
SQLPLUS_COMMANDS = re.compile(
   r"(?i)^\s*(@|!|\$|"
   r"(acc(e(p(t)?)?)?|archive|attr(i(b(u(t(e)?)?)?)?)?"
   r"|bre(a(k)?)?|bti(t(l(e)?)?)?|cl(e(a(r)?)?)?"
   r"|col(u(m(n)?)?)?|comp(u(t(e)?)?)?|conn(e(c(t)?)?)?|copy"
   r"|def(i(n(e)?)?)?|desc(r(i(b(e)?)?)?)?|disc(o(n(n(e(c(t)?)?)?)?)?)?"
   r"|exec(u(t(e)?)?)?|exit|ho(s(t)?)?"
   r"|passw(o(r(d)?)?)?|pau(s(e)?)?|pri(n(t)?)?"
   r"|pro(m(p(t)?)?)?|quit|rem(a(r(k)?)?)?|repf(o(o(t(e(r)?)?)?)?)?"
   r"|reph(e(a(d(e(r)?)?)?)?)?|set|sho(w)?"
   r"|spo(o(l)?)?|sta(r(t)?)?|store|timi(n(g)?)?"
   r"|tti(t(l(e)?)?)?|undef(i(n(e)?)?)?|var(i(a(b(l(e)?)?)?)?)?"
   r"|whenever)\b)")

###########################################################################

def sqlplus_text(text):
   """
   use: makes a sql*plus script parseable: its commands (define, set,
        prompt, @file, ...) become blank lines, so line numbers in
        errors still match the file, and each &name or &&name
        substitution variable becomes a bind placeholder that to_sql
        spells back as written
   imp: a command is only looked for between statements, where
        sql*plus looks for it, so a line of sql that starts like one
        (set, col, ...) is left alone; a substitution variable's "."
        terminator is dropped; SET DEFINE OFF stops substitution from
        there on
   """
   lines  = []
   define = True
   inside = False
   for line in text.split("\n"):
      code = re.sub(r"--.*$", "", line).strip()
      if (inside):
                               # a statement ends with ; or a lone /
         inside = not (code.endswith(";") or code == "/")
      elif (code and code != "/" and not SQLPLUS_COMMANDS.match(line)):
         inside = not code.endswith(";")
      elif (code and code != "/"):
         words = line.split()
         if (    len(words) >= 3
             and words[0].lower() == "set"
             and words[1].lower().startswith("def")):
            define = words[2].lower() not in ("off", "")
         lines.append("")
         continue
      if (define):
         line = re.sub(r"(&&?)(\w+)\.?",
                       lambda m: f":VQAMP{len(m.group(1))}_{m.group(2)}",
                       line)
      lines.append(line)
   return("\n".join(lines))

###########################################################################

def shorten(text, limit = LABEL_MAX):
   text = re.sub(r"\s+", " ", text)
   return(     text if (len(text) <= limit)
          else text[:limit - 1] + ELLIPSIS)

###########################################################################

def strip_paren(expression):
   while (isinstance(expression, exp.Paren)):
      expression = expression.this
   return(expression)

###########################################################################

def table_name(identifier):
   """
   post: table (and owner) names are always shown in upper case
   """
   if (isinstance(identifier, exp.Identifier)):
      return(identifier.this.upper())
   return(str(identifier).upper())

###########################################################################

def column_name(identifier):
   """
   post: returns (key, display): the key folds unquoted names to upper
         case as oracle does, so t.Col_1 and t.COL_1 are one column;
         the display keeps the case of the source
   """
   if (isinstance(identifier, exp.Identifier)):
      return(     (identifier.this, identifier.this)
             if (identifier.quoted)
             else (identifier.this.upper(), identifier.this))
   return(str(identifier).upper(), str(identifier))

###########################################################################

def own_columns(expression):
   """
   use: finds columns in expression that belong to the current query
        block, i.e. EXCLUDING those inside nested subqueries and
        unqualified pseudo columns such as ROWNUM
   """
   found = []
   stack = (     list(reversed(expression))
            if (isinstance(expression, list))
            else [expression])
   while (stack):
      node = stack.pop()
      if (isinstance(node, (exp.Subquery, exp.Query))):
         continue
      if (isinstance(node, exp.Column)):
         if (node.table or node.name.upper() not in PSEUDO):
            found.append(node)
         continue
      stack.extend(reversed(list(node.iter_expressions())))
   return(found)

###########################################################################

def nested_queries(query):
   """
   post: returns the subqueries directly inside query (not inside one
         of those, and not its WITH clause), in clause order
   """
   found = []
   stack = [value for (key, value) in reversed(list(query.args.items()))
            if (key not in ("with", "with_"))]
   while (stack):
      node = stack.pop()
      if (isinstance(node, list)):
         stack.extend(reversed(node))
         continue
      if (not isinstance(node, exp.Expression)):
         continue
      if (isinstance(node, exp.Query)):
         found.append(node)
         continue
      stack.extend(reversed(list(node.iter_expressions())))
   return(found)

###########################################################################

def setop_name(setop):
   name = setop.key.upper()
   if (name == "EXCEPT"):
      return("MINUS")
   if (name == "UNION" and not setop.args.get("distinct")):
      return("UNION ALL")
   return(name)

###########################################################################

class QueryModel:
   """
   use: parses SQL text into one Diagram per statement
   usage: model = QueryModel(sql_text)
          for diagram in model.diagrams: ...
   """
   def __init__(self, sql_text):
      self.diagrams = []
      self.labels   = {}
      self.scopes   = {}
      self.ctes     = set()
                               # CTE name -> the aliases it is read by
      self.cte_aliases = {}
                               # CTE name -> its query
      self.bodies   = {}
      self.counter  = 0
                               # sql*plus terminators on their own line
                               # are not sql
      text = re.sub(r"(?m)^\s*/\s*$", ";", sqlplus_text(sql_text))
      try:
         parsed = sqlglot.parse(text, read = "oracle")
      except ParseError as error:
                               # without the terminal's underline
         detail = re.sub(r"\x1b\[[0-9;]*m", "", str(error))
         raise(VqError(f"cannot parse sql: {detail}"))
      for statement in parsed:
         if (statement is not None and isinstance(statement, exp.Query)):
            self.diagrams.append(self.build_statement(statement))
      if (not self.diagrams):
         raise(VqError("no query found in the input"))

   ########################################################################

   def build_statement(self, statement):
      """
      imp: every scope gets its FROM tables before any predicate is
           read, so predicates can point into subqueries and back out
           to outer (correlated) tables
      """
      selects = list(statement.find_all(exp.Select, bfs = False))
      for select in selects:
         self.counter += 1
         self.labels[id(select)] = f"Q{self.counter}"
      for cte in statement.find_all(exp.CTE):
         self.ctes.add(cte.alias.upper())
         self.bodies[cte.alias.upper()] = cte.this
      scopes = []
      for select in selects:
         ancestor = (     select.parent.find_ancestor(exp.Select)
                     if (select.parent is not None)
                     else None)
         parent   = (     self.scopes.get(id(ancestor))
                     if (ancestor is not None)
                     else None)
         scope    = Scope(select, self.labels[id(select)],
                          self.context(select), parent)
         self.scopes[id(select)] = scope
         self.collect_sources(scope)
         scopes.append(scope)
      for scope in scopes:
         self.build_scope(scope)
      if (isinstance(statement, SETOP)):
         order = statement.args.get("order")
                               # a set operation's ORDER BY sorts the
                               # rows of every branch that returns
                               # any, by select list position
         if (order is not None):
            self.add_set_order(statement, order)
      self.collapse(scopes)
      feeds = []
      for scope in scopes:
         for table in list(scope.tables.values()):
            if (table.body is not None and table.virtual):
               feeds.extend(self.feeds(table))
      items = []
      self.emit(statement, 0, False, items)
      diagram       = Diagram(to_sql(statement), scopes, items)
      diagram.feeds = feeds
      return(diagram)

   ########################################################################

   def collapse(self, scopes):
      """
      use: draws each table only once: a CTE reference or inline view
           is removed, and every line to it is moved to the columns
           that produce it inside its query's box
      post: with VIRTUAL_TABLES on, a reference that would mislead when
            folded is left in place and marked virtual (drawn in a
            dotted box); with it off every reference is folded
      imp: repeated until nothing changes, so a CTE built on another
           CTE folds all the way down
      """
      uses = {}
      for scope in scopes:
         for table in scope.tables.values():
            if (table.kind == "cte"):
               uses[table.name] = uses.get(table.name, 0) + 1
      changed = True
      while (changed):
         changed = False
         for scope in scopes:
            for (key, table) in list(scope.tables.items()):
               if (table.body is None or table.virtual):
                  continue
               if (table.kind == "cte" and uses.get(table.name, 0) > 1):
                               # read more than once: each reference
                               # stays a table of its own, fed from
                               # the CTE, or the joins between them
                               # would fold onto the same columns
                  table.virtual = True
                  continue
               targets = (     self.fold_targets(table, uses)
                          if (VIRTUAL_TABLES)
                          else self.fold_all_targets(table))
               if (targets is None):
                  table.virtual = True
                  continue
               for (name, column) in table.columns.items():
                  for (_, target) in targets.get(name, []):
                     target.returned = target.returned or column.returned
                     if (target.order_by is None):
                        target.order_by   = column.order_by
                        target.order_desc = column.order_desc
               for other in scopes:
                  for leaf in other.leaves:
                     leaf.segments = self.retarget(leaf.segments, table,
                                                   targets)
               del scope.tables[key]
               changed = True
      for scope in scopes:
         for leaf in scope.leaves:
            for segment in leaf.segments:
                               # a move can put both ends on one table
               segment.kind = (     "filter" if (segment.b is None)
                               else "self" if (  segment.a.table
                                               is segment.b.table)
                               else "join")
                               # a condition on a column made from no
                               # column at all (rownum, a literal) has
                               # nowhere left to be drawn
            if (    not leaf.segments
                and not leaf.silent
                and leaf.sql not in scope.notes):
               scope.notes.append(leaf.sql)

   ########################################################################

   def retarget(self, segments, table, targets):
      """
      post: returns segments with every end on table moved to the
            column(s) that produce it; an end produced by several
            columns becomes one segment per column, and a segment with
            an end produced by none is dropped
      """
      result = []
      for segment in segments:
         ends = segment.ends()
         if (all(end.table is not table for end in ends)):
            result.append(segment)
            continue
         choices = [     targets.get(end.column_key, [])
                    if (end.table is table)
                    else [(end.table, end.column)]
                    for end in ends]
         for picks in itertools.product(*choices):
            moved = [Endpoint(target, column, end.join_mark)
                     for ((target, column), end) in zip(picks, ends)]
            result.append(Segment(moved[0],
                                  moved[1] if (len(moved) > 1) else None,
                                  segment.op))
      return(result)

   ########################################################################

   def fold_all_targets(self, table):
      """
      post: returns {column key: [(table, column), ...]}: for each
            column read through a CTE reference / inline view, every
            column inside its query that the select list entry is made
            from; an expression gives all its columns, rownum or a
            literal none, and each branch of a set operation adds its
            entry at the same position
      """
      body = table.body
      while (isinstance(body, exp.Subquery)):
         body = body.this
      branches = [select for select in self.branches(body)
                  if (id(select) in self.scopes)]
      if (not branches):
         return({})
      names   = [self.output_name(p) for p in branches[0].expressions]
      targets = {}
      for key in table.columns:
         found = []
         for select in branches:
            inner = self.scopes[id(select)]
            if (key == "*"):
               for source in inner.sources:
                  found.append((source, source.column("*")))
            elif (    key in names
                  and names.index(key) < len(select.expressions)):
               projection = select.expressions[names.index(key)]
               for end in self.projection_ends(inner, projection):
                  found.append((end.table, end.column))
            elif ("*" in names and len(inner.sources) == 1):
               source = inner.sources[0]
               found.append((source,
                             source.column(key, table.columns[key].name,
                                           table.columns[key].first)))
         targets[key] = found
      return(targets)

   ########################################################################

   def fold_targets(self, table, uses):
      """
      post: returns {column key: [(table, column)]} inside the query of
            a CTE reference / inline view when every column read
            through it is a plain column there, else None
      """
      if (table.kind == "cte" and uses.get(table.name, 0) > 1):
         return(None)
      body = table.body
      while (isinstance(body, exp.Subquery)):
         body = body.this
      if (not isinstance(body, exp.Select) or id(body) not in self.scopes):
         return(None)
      inner   = self.scopes[id(body)]
      names   = [self.output_name(p) for p in body.expressions]
      targets = {}
      for key in table.columns:
         if (key == "*" or key not in names):
            return(None)
         projection = body.expressions[names.index(key)]
         target     = (     projection.this
                       if (isinstance(projection, exp.Alias))
                       else projection)
         if (   not isinstance(target, exp.Column)
             or isinstance(target.this, exp.Star)):
            return(None)
         end = self.endpoint(inner, target)
         targets[key] = [(end.table, end.column)]
      return(targets)

   ########################################################################

   def feeds(self, table):
      """
      use: links each column used through a CTE reference or inline
           view to the column(s) inside the query that produce it
      post: returns a list of Leaf objects, one per linked column; they
            are not predicates, so they belong to no AND/OR tree
      imp: columns are matched by select list name (alias, else column
           name); every branch of a set operation feeds by position;
           a "*" select list feeds a column of the same name when the
           branch reads a single table
      """
      body = table.body
      while (isinstance(body, exp.Subquery)):
         body = body.this
      branches = [select for select in self.branches(body)
                  if (id(select) in self.scopes)]
      if (not branches):
         return([])
      names = [self.output_name(p) for p in branches[0].expressions]
      leaves = []
      for (key, column) in table.columns.items():
         if (key == "*"):
            continue
         leaf = Leaf(f"{table.title()}.{column.name} {LEFT_ARROW} "
                     f"{table.name}")
         leaf.feed = True
         for select in branches:
            inner = self.scopes[id(select)]
            if (key in names and names.index(key) < len(select.expressions)):
               ends = self.projection_ends(
                         inner, select.expressions[names.index(key)])
            elif (    "*" in names
                  and len(inner.sources) == 1):
               source = inner.sources[0]
               ends   = [Endpoint(source, source.column(key,
                                                        column.name,
                                                        column.first))]
            else:
               ends = []
            for end in ends:
               leaf.segments.append(
                  Segment(Endpoint(table, column), end, ""))
         if (leaf.segments):
            leaves.append(leaf)
      return(leaves)

   ########################################################################

   def branches(self, query):
      """
      post: returns the selects of query, leftmost branch first
      """
      if (isinstance(query, SETOP)):
         return(self.branches(query.this) + self.branches(query.expression))
      while (isinstance(query, exp.Subquery)):
         query = query.this
      return([query] if (isinstance(query, exp.Select)) else [])

   ########################################################################

   def output_name(self, projection):
      """
      post: returns the key of the name a select list entry is known by
            outside its query, "*" for a star, or None
      """
      if (isinstance(projection, exp.Alias)):
         return(column_name(projection.args["alias"])[0])
      if (isinstance(projection, exp.Star)):
         return("*")
      if (isinstance(projection, exp.Column)):
         if (isinstance(projection.this, exp.Star)):
            return("*")
         return(column_name(projection.this)[0])
      return(None)

   ########################################################################

   def emit(self, query, indent, boxed, items):
      """
      use: lays the query blocks out top to bottom: CTEs first, then
           the block's own tables, then its subqueries; set operation
           branches one under the other with the operator between
      """
      while (isinstance(query, exp.Subquery)):
         query = query.this
      with_ = query.args.get("with") or query.args.get("with_")
      if (with_ is not None):
         for cte in with_.expressions:
            self.emit(cte.this, indent - 1, True, items)
      if (isinstance(query, SETOP)):
         self.emit(query.this, indent, boxed, items)
         items.append(("sep", setop_name(query), indent))
         self.emit(query.expression, indent, boxed, items)
         return
      if (not isinstance(query, exp.Select)):
         return
      scope        = self.scopes[id(query)]
      scope.indent = indent
      scope.boxed  = boxed
      if (boxed):
         items.append(("open", scope))
      for table in scope.ordered_tables():
         table.scope = scope
         items.append(("table", table))
      for child in nested_queries(query):
         self.emit(child, indent + 1, True, items)
      if (boxed):
         items.append(("close", scope))

   ########################################################################

   def context(self, select):
      """
      post: returns a short description of where the block sits
      """
      node = select
      while (isinstance(node.parent, SETOP)):
         node = node.parent
      parent = node.parent
      if (parent is None):
         return("main query")
      if (isinstance(parent, exp.CTE)):
         return(f"WITH {parent.alias.upper()}")
      if (    isinstance(parent, exp.Subquery)
          and isinstance(parent.parent, (exp.From, exp.Join))):
         return(f"inline view {parent.alias}".rstrip())
      if (isinstance(parent, exp.Exists)):
         negated = isinstance(parent.parent, exp.Not)
         return(f"{'NOT ' if (negated) else ''}EXISTS subquery")
      holder = (     parent.parent if (isinstance(parent, exp.Subquery))
                else parent)
      if (isinstance(holder, exp.In)):
         negated = isinstance(holder.parent, exp.Not)
         return(f"{'NOT ' if (negated) else ''}IN subquery")
      for (word, cls) in (("ALL", exp.All), ("ANY", exp.Any)):
         if (isinstance(holder, cls)):
            return(f"{word} subquery")
      while (holder is not None):
         for (cls, where) in ((exp.Where , "WHERE"),
                              (exp.Having, "HAVING"),
                              (exp.Join  , "JOIN ON"),
                              (exp.Select, "SELECT list")):
            if (isinstance(holder, cls)):
               return(f"subquery in {where}")
         holder = holder.parent
      return("query block")

   ########################################################################

   def subquery_label(self, expression):
      select = (     expression if (isinstance(expression, exp.Select))
                else expression.find(exp.Select))
      return(self.labels.get(id(select), "subquery"))

   ########################################################################

   def add_source(self, scope, source):
      """
      post: returns the TableRef added to scope for one FROM item
      """
      if (isinstance(source, exp.Table)):
         name  = table_name(source.this)
         owner = source.args.get("db")
         alias = source.alias
         kind  = "cte" if (name in self.ctes) else "table"
         if ((kind == "cte") and alias and
             (alias.upper() != name.upper())):
            aliases = self.cte_aliases.setdefault(name, [])
            if (alias not in aliases):
               aliases.append(alias)
         if (owner is not None):
            name = f"{table_name(owner)}.{name}"
         key   = (alias or name.split(".")[-1]).upper()
      elif (isinstance(source, exp.Subquery)):
         alias = source.alias
         name  = f"({self.subquery_label(source)})"
         kind  = "view"
         key   = (alias or name).upper()
      else:
         alias = getattr(source, "alias", "")
         name  = shorten(to_sql(source), 24)
         kind  = "other"
         key   = (alias or name).upper()
      table = TableRef(key, name, alias, kind)
      if (isinstance(source, exp.Table)):
         table.db_owner = (column_name(owner)[0] if (owner is not None)
                           else None)
         table.db_name  = column_name(source.this)[0]
                               # the query that fills a CTE reference or
                               # an inline view
      table.body = (     source.this if (kind == "view")
                    else self.bodies.get(name) if (kind == "cte")
                    else None)
      scope.tables[key] = table
      scope.sources.append(table)
      return(table)

   ########################################################################

   def collect_sources(self, scope):
      select = scope.select
      from_  = select.args.get("from") or select.args.get("from_")
      items  = []
      if (from_ is not None):
         items.append((from_.this, None))
         for extra in from_.expressions:
            items.append((extra, None))
      for join in (select.args.get("joins") or []):
         items.append((join.this, join))
      for (source, join) in items:
         scope.placed.append((self.add_source(scope, source), join))

   ########################################################################

   def resolve(self, scope, column):
      """
      use: finds the TableRef a column belongs to; a qualifier from an
           enclosing block resolves to that block's own table, so
           correlated predicates draw straight to it
      imp: unqualified columns can only be resolved when there is one
           table; until the dictionary is available the rest go to a
           "?" pseudo table
      """
      qualifier = column.table.upper()
      if (qualifier):
         outer = scope
         while (outer is not None):
            if (qualifier in outer.tables):
               return(outer.tables[qualifier])
            outer = outer.parent
         table = TableRef(qualifier, qualifier, "", "unknown")
         scope.tables[qualifier] = table
         return(table)
      if (len(scope.sources) == 1):
         return(scope.sources[0])
      if ("?" not in scope.tables):
         scope.tables["?"] = TableRef("?", "(unqualified)", "", "?")
      return(scope.tables["?"])

   ########################################################################

   def endpoint(self, scope, column):
      table = self.resolve(scope, column)
      (key, display) = (     ("*", "*")
                        if (isinstance(column.this, exp.Star))
                        else column_name(column.this))
      meta = getattr(column.this, "meta", None) or {}
      return(Endpoint(table, table.column(key, display, meta.get("start")),
                      bool(column.args.get("join_mark"))))

   ########################################################################

   def build_scope(self, scope):
      select = scope.select
      self.add_projections(scope)
      children = []
      for (position, (table, join)) in enumerate(scope.placed):
         if (join is None):
            continue
         side = (join.side or "").upper()
         on   = join.args.get("on")
         if (on is not None):
            tree = self.build(scope, on)
            self.mark_outer(tree, table, side)
            self.attach(children, tree)
         for ident in (join.args.get("using") or []):
            if (position == 0):
               continue
            (key, name) = column_name(ident)
            left        = scope.placed[position - 1][0]
            leaf        = Leaf(f"USING ({name})")
            first       = (getattr(ident, "meta", None) or {}).get("start")
            leaf.segments.append(
               Segment(Endpoint(left,  left.column(key, name, first)),
                       Endpoint(table, table.column(key, name, first)),
                       "="))
            self.mark_outer(leaf, table, side)
            self.attach(children, leaf)
      where = select.args.get("where")
      if (where is not None):
         self.attach(children, self.build(scope, where.this))
      having = select.args.get("having")
      if (having is not None and scope.placed):
                               # an aggregate compared in HAVING is
                               # shown as a column of its own
         self.having = scope
         try:
            self.attach(children, self.build(scope, having.this))
         finally:
            self.having = None
      self.add_other_columns(scope)
      if (not children):
         scope.root = None
      elif (len(children) == 1 and isinstance(children[0], Node)):
         scope.root = children[0]
      else:
         scope.root          = Node("AND")
         scope.root.children = children
      self.finish_tree(scope)

   ########################################################################

   def mark_outer(self, tree, joined, side):
      """
      use: shows an ANSI outer join the oracle way, with (+) on the
           columns of the optional table(s)
      """
      if (not side):
         return
      for leaf in iter_leaves(tree):
         for segment in leaf.segments:
            for end in segment.ends():
               if (   side == "FULL"
                   or (side == "LEFT"  and end.table is joined)
                   or (side == "RIGHT" and end.table is not joined)):
                  end.join_mark = True

   ########################################################################

   def attach(self, children, tree):
      """
      use: adds a predicate tree to the implied top level AND, merging
           an inner AND into it
      """
      if (isinstance(tree, Node) and tree.kind == "AND"):
         children.extend(tree.children)
      else:
         children.append(tree)

   ########################################################################

   def add_projections(self, scope):
      """
      use: adds the select list's columns (and aggregates) to their
           tables, checked as returned only when the user gets them
      """
      returned = self.returns(scope)
                               # what an EXISTS subquery selects means
                               # nothing: its select 1 is not shown
      exists   = scope.context.endswith("EXISTS subquery")
      for projection in scope.select.expressions:
         target = (     projection.this
                   if (isinstance(projection, exp.Alias))
                   else projection)
         if (exists and isinstance(strip_paren(target), exp.Literal)):
            continue
         if (isinstance(target, exp.Star)):
            for table in scope.sources:
               table.column("*").returned |= returned
            continue
         for end in self.projection_ends(scope, projection):
            end.column.returned |= returned

   ########################################################################

   def returns(self, scope):
      """
      post: true when the rows of scope's select list reach the user:
            the top level query (each branch of a top level set
            operation), or a scalar subquery in the select list of
            one that does
      imp: a CTE or inline view passes columns out only through the
           columns read from it, so collapse checks those it feeds
      """
      if (scope.parent is None):
                               # the right side of a MINUS only takes
                               # rows away
         node = scope.select
         while (isinstance(node.parent, (exp.Subquery, SETOP))):
            if (    isinstance(node.parent, exp.Except)
                and node is node.parent.expression):
               return(False)
            node = node.parent
         return(True)
      return(    scope.context == "subquery in SELECT list"
             and self.returns(scope.parent))

   ########################################################################

   def selected(self, scope, ref):
      """
      post: true when column ref is a plain entry of scope's select list
      """
      for projection in scope.select.expressions:
         target = strip_paren(     projection.this
                              if (isinstance(projection, exp.Alias))
                              else projection)
         if (    isinstance(target, exp.Column)
             and not isinstance(target.this, exp.Star)
             and self.endpoint(scope, target).column is ref):
            return(True)
      return(False)

   ########################################################################

   def add_other_columns(self, scope):
      """
      use: shows columns used only by GROUP BY / ORDER BY / HAVING
      imp: unqualified ones are skipped when there are several tables
           since they are often select list aliases
      """
      for key in ("group", "having"):
         clause = scope.select.args.get(key)
         if (clause is None):
            continue
         for column in own_columns(clause):
            if (column.table or len(scope.sources) == 1):
               ref = self.endpoint(scope, column).column
                               # grouped on but not selected: in
                               # italics, like an aggregate, since it
                               # is not in the rows returned
               if (key == "group" and not self.selected(scope, ref)):
                  ref.implied = True
      order = scope.select.args.get("order")
      if (order is not None):
         self.add_order(scope, order)

   ########################################################################

   def add_set_order(self, statement, order):
      """
      use: numbers, in every branch of a set operation that returns
           rows, the select list entry each ORDER BY item names: by
           position, or by a name in the first branch's select list
      """
      branches = [self.scopes[id(s)] for s in self.branches(statement)
                  if (id(s) in self.scopes)]
      if (not branches):
         return
      names = [self.output_name(p) for p in branches[0].select.expressions]
      for (position, ordered) in enumerate(order.expressions, 1):
         target = strip_paren(ordered.this)
         if (    isinstance(target, exp.Literal)
             and not target.is_string
             and target.this.isdigit()):
            at = int(target.this) - 1
         elif (    isinstance(target, exp.Column)
               and column_name(target.this)[0] in names):
            at = names.index(column_name(target.this)[0])
         else:
            continue
         for scope in branches:
            projections = scope.select.expressions
            if (not self.returns(scope) or at >= len(projections)):
               continue
            for end in self.projection_ends(scope, projections[at]):
               if (end.column.order_by is None):
                  end.column.order_by   = position
                  end.column.order_desc = bool(ordered.args.get("desc"))

   ########################################################################

   def add_order(self, scope, order):
      """
      use: numbers the columns of each ORDER BY item with its position
           in the list
      imp: an item may name a select list position (ORDER BY 2) or a
           select list alias; both stand for that select list entry
      """
      projections = scope.select.expressions
      aliases     = {}
      for projection in projections:
         if (isinstance(projection, exp.Alias)):
            aliases[projection.alias.upper()] = projection
      for (position, ordered) in enumerate(order.expressions, 1):
         target = strip_paren(ordered.this)
         if (    isinstance(target, exp.Literal)
             and not target.is_string
             and target.this.isdigit()
             and 0 < int(target.this) <= len(projections)):
            target = projections[int(target.this) - 1]
         elif (    isinstance(target, exp.Column)
               and not target.table
               and target.name.upper() in aliases):
            target = aliases[target.name.upper()]
         if (target.find(exp.AggFunc) is not None):
            refs = [e.column for e in self.value_ends(scope, target)]
         else:
            refs = [self.endpoint(scope, c).column
                    for c in own_columns(target)
                    if (c.table or len(scope.sources) == 1)]
         for ref in refs:
            if (ref.order_by is None):
               ref.order_by   = position
               ref.order_desc = bool(ordered.args.get("desc"))

   ########################################################################

   def build(self, scope, expression):
      """
      post: returns a Node or a Leaf for a boolean expression
      """
      expression = strip_paren(expression)
      for (cls, kind) in ((exp.And, "AND"), (exp.Or, "OR")):
         if (isinstance(expression, cls)):
            node  = Node(kind, to_sql(expression))
            stack = [expression]
            while (stack):
               item = strip_paren(stack.pop())
               if (isinstance(item, cls)):
                  stack.append(item.expression)
                  stack.append(item.this)
               else:
                  node.children.append(self.build(scope, item))
            return(node)
      if (isinstance(expression, exp.Not)):
         inner = strip_paren(expression.this)
         if (isinstance(inner, (exp.And, exp.Or, exp.Not))):
            node = Node("NOT", to_sql(expression))
            node.children.append(self.build(scope, inner))
            return(node)
         if (isinstance(inner, exp.Is)):
            leaf = Leaf(to_sql(expression))
            self.add_pair(scope, leaf, inner.this, inner.expression,
                          "IS NOT")
            return(leaf)
         return(self.make_leaf(scope, inner, negate = True))
      return(self.make_leaf(scope, expression))

   ########################################################################

   def side_text(self, side):
      if (isinstance(side, list)):
         return("(" + ", ".join(to_sql(item) for item in side) + ")")
      if (isinstance(side, (exp.Subquery, exp.Query))):
         return(f"({self.subquery_label(side)})")
      return(to_sql(side))

   ########################################################################

   def make_leaf(self, scope, expression, negate = False):
      prefix = "NOT " if (negate) else ""
      leaf   = Leaf(prefix + to_sql(expression))
      if (isinstance(expression, exp.Between)):
         (low, high) = (expression.args["low"], expression.args["high"])
         pseudo = self.row_end(scope, expression.this)
         if (((own_columns(expression.this)) or pseudo) and
             (not own_columns(low)) and (not own_columns(high))):
                               # bounds are values: one arrow labelled
                               # with the whole range
            label = shorten(f"{prefix}BETWEEN {to_sql(low)}"
                            f" AND {to_sql(high)}")
            ends  = ([pseudo] if (pseudo) else
                     [self.endpoint(scope, c)
                      for c in own_columns(expression.this)])
            for end in ends:
               leaf.segments.append(Segment(end, None, label))
            return(leaf)
                               # a bound that is a column joins: one
                               # line per bound
         pairs = [(expression.this, low , ">="),
                  (expression.this, high, "<=")]
      elif (isinstance(expression, exp.In)):
         values = (   expression.args.get("query")
                   or expression.expressions)
         pairs  = [(expression.this, values, "IN")]
      elif (isinstance(expression, exp.Exists)):
         leaf.sql    = (f"{prefix}EXISTS "
                        f"({self.subquery_label(expression)})")
         leaf.silent = True
         leaf.exists = (     expression.this
                        if (isinstance(expression.this, exp.Select))
                        else expression.find(exp.Select))
         pairs       = []
      elif (type(expression) in OPERATORS):
         pairs = [(expression.this, expression.expression,
                   OPERATORS[type(expression)])]
      else:
         label = shorten(prefix + to_sql(expression))
         for column in own_columns(expression):
            leaf.segments.append(
               Segment(self.endpoint(scope, column), None, label))
         return(leaf)
      for (left, right, op) in pairs:
         self.add_pair(scope, leaf, left, right, prefix + op)
      return(leaf)

   ########################################################################

   def side_ends(self, scope, side):
      """
      post: endpoints for the columns of one side of a comparison; a
            single column subquery side links to the column it
            returns inside its box
      """
      pseudo = self.row_end(scope, side)
      if (pseudo is not None):
         return([pseudo])
      aggregate = (     strip_paren(side)
                   if (not isinstance(side, list))
                   else None)
      if (    getattr(self, "having", None) is scope
          and isinstance(aggregate, exp.AggFunc)):
         return(self.value_ends(scope, aggregate))
      ends = [self.endpoint(scope, c) for c in own_columns(side)]
      if (ends):
         return(ends)
      query = side
      if (isinstance(query, list)):
         if (len(query) != 1):
            return([])
         query = query[0]
      while (isinstance(query, (exp.Subquery, exp.Paren))):
         query = query.this
      if (    not isinstance(query, exp.Select)
          or  len(query.expressions) != 1
          or  id(query) not in self.scopes):
         return([])
      inner = self.scopes[id(query)]
      return(self.value_ends(inner, query.expressions[0]))

   ########################################################################

   def row_end(self, scope, side):
      """
      post: returns, when side is a pseudo column describing a row
            (ROWNUM, LEVEL), an endpoint on a virtual column for it on
            the first table in FROM, else None
      """
      value = None if (isinstance(side, list)) else strip_paren(side)
      if (   not isinstance(value, exp.Column)
          or value.table
          or value.name.upper() not in ROW_PSEUDO
          or not scope.placed):
         return(None)
      table  = scope.placed[0][0]
      name   = value.name.upper()
      meta   = getattr(value.this, "meta", None) or {}
      column = table.column(name, name, meta.get("start"))
      column.virtual = True
      return(Endpoint(table, column))

   ########################################################################

   def projection_ends(self, scope, projection):
      """
      post: as value_ends, for a select list entry; one that is a bare
            value ('CURRENT', 10) gives a virtual column named for it,
            quotes and all, on the first table in FROM
      """
      target = (     projection.this
                if (isinstance(projection, exp.Alias))
                else projection)
      value  = strip_paren(target)
      pseudo = self.row_end(scope, value)
      if (pseudo is not None):
         return([pseudo])
      if (isinstance(value, exp.Neg)):
         value = value.this
      if (not isinstance(value, exp.Literal) or not scope.placed):
         return(self.value_ends(scope, projection))
      table  = scope.placed[0][0]
      name   = to_sql(strip_paren(target))
      meta   = getattr(value, "meta", None) or {}
      column = table.column(name, name, meta.get("start"))
      column.virtual = True
      return([Endpoint(table, column)])

   ########################################################################

   def value_ends(self, scope, expression):
      """
      post: returns the endpoints an expression's value is drawn from:
            its columns, except that an aggregate (AVG(SAL), COUNT(*))
            gives a virtual column of its own, set to the right of the
            column it reads; COUNT(*) goes on the first table in FROM
      imp: nested aggregates chain: SAL -> AVG(SAL) -> MAX(AVG(SAL))
      """
      expression = strip_paren(expression)
      if (isinstance(expression, (exp.Subquery, exp.Query))):
         return([])
      if (isinstance(expression, exp.Column)):
         return(own_columns(expression) and
                [self.endpoint(scope, expression)])
      if (not isinstance(expression, exp.AggFunc)):
         ends = []
         for item in expression.iter_expressions():
            ends += self.value_ends(scope, item)
         return(ends)
      inner = []
      for item in expression.iter_expressions():
         inner += self.value_ends(scope, item)
      if (inner):
         table = inner[0].table
      elif (scope.placed):
         table = scope.placed[0][0]
      else:
         return([])
                               # shown without table qualifiers:
                               # AVG(SAL), not AVG(E.SAL)
      bare   = expression.copy()
      for item in bare.find_all(exp.Column):
         item.set("table", None)
      name   = to_sql(bare).upper()
      column = table.column(name, name)
      column.virtual = True
      if (inner and inner[0].table is table):
         column.source = inner[0].column
         for end in inner[1:]:
            if (    end.table is table
                and end.column is not column.source
                and end.column not in column.extra):
               column.extra.append(end.column)
      return([Endpoint(table, column)])

   ########################################################################

   def add_pair(self, scope, leaf, left, right, op):
                               # x > ALL (select y ...) links x to y,
                               # the operator reading "> ALL"
      for (word, cls) in (("ALL", exp.All), ("ANY", exp.Any)):
         if (isinstance(right, cls)):
            (right, op) = (right.this, f"{op} {word}")
      lends = self.side_ends(scope, left)
      rends = self.side_ends(scope, right)
      if (lends and rends):
                               # an endpoint belongs to one segment, so
                               # each pair gets its own copies
         copy = lambda e: Endpoint(e.table, e.column, e.join_mark)
         for lend in lends:
            for rend in rends:
               leaf.segments.append(Segment(copy(lend), copy(rend), op))
      elif (lends):
         label = shorten(f"{op} {self.side_text(right)}")
         for lend in lends:
            leaf.segments.append(Segment(lend, None, label))
      elif (rends):
         bare  = op.replace("NOT ", "")
         label = (     f"{op.replace(bare, FLIP[bare])} "
                       f"{self.side_text(left)}" if (bare in FLIP)
                  else f"{self.side_text(left)} {op} {ELLIPSIS}")
         for rend in rends:
            leaf.segments.append(Segment(rend, None, shorten(label)))

   ########################################################################

   def finish_tree(self, scope):
      """
      post: nodes and leaves numbered, depths set, every leaf knows
            its ancestor nodes, column-less predicates noted
      """
      if (scope.root is None):
         return
      scope.root.is_root = True
                               # a group put together from ON and WHERE
                               # conditions has no sql of its own
      def fill(item):
         if (isinstance(item, Node) and not item.sql):
            item.sql = f" {item.kind} ".join(
                          (f"({fill(c)})" if (isinstance(c, Node))
                           else fill(c)) for c in item.children)
         return(item.sql)
      fill(scope.root)
      stack = [(scope.root, 0, [])]
      while (stack):
         (item, depth, ancestors) = stack.pop()
         if (isinstance(item, Node)):
            item.ident = f"{scope.label}N{len(scope.nodes)}"
            item.depth = depth
            item.color = PALETTE[len(scope.nodes) % len(PALETTE)]
            scope.nodes.append(item)
            for child in reversed(item.children):
               stack.append((child, depth + 1, ancestors + [item]))
         else:
            item.ident     = f"{scope.label}L{len(scope.leaves)}"
            item.ancestors = ancestors
            scope.leaves.append(item)
            if (not item.segments and not item.silent):
               scope.notes.append(item.sql)

###########################################################################
###
### l a y o u t
###
###########################################################################

def text_px(text, size = CHAR_PX):
   return(len(text) * size)

###########################################################################

class Layout:
   """
   use: computes coordinates for every box, table, column, line and
        bar of one Diagram
   """
   def __init__(self, diagram):
      self.diagram = diagram
      self.tables  = [item[1] for item in diagram.items
                      if (item[0] == "table")]
      self.leaves  = [leaf for scope in diagram.scopes
                      for leaf in scope.leaves] + diagram.feeds
      self.nodes   = [node for scope in diagram.scopes
                      for node in scope.nodes]
      lowest       = min([s.indent for s in diagram.scopes] + [0])
      self.base    = MARGIN_PX + BOX_PAD_PX - lowest * INDENT_PX
      for scope in diagram.scopes:
         scope.left = self.base + scope.indent * INDENT_PX
      for (index, table) in enumerate(self.tables):
         table.index = index
      self.find_sides()
                               # the first run settles where every line
                               # heads; the second orders the lines
                               # leaving each column by that, so they
                               # fan out instead of crossing
      self.order = {}
      self.run()
      self.order = self.line_directions()
      self.run()

   ########################################################################

   def find_sides(self):
      """
      use: a correlated subquery in the SELECT list, reading one table
           and holding no subquery of its own, is drawn to the right
           of the outer table it correlates to, its box's top level
           with that table's column names
      """
      diagram = self.diagram
      for scope in diagram.scopes:
         tables = list(scope.tables.values())
         if (   not scope.boxed
             or scope.context != "subquery in SELECT list"
             or len(tables) != 1
             or any(s.parent is scope for s in diagram.scopes)):
            continue
         outside = {id(e.table): e.table
                    for leaf in scope.leaves
                    for segment in leaf.segments
                    if (segment.kind == "join")
                    for e in segment.ends()
                    if (e.table.scope is not scope)}
         hosts = list(outside.values())
         if (len(hosts) == 1 and hosts[0].scope is scope.parent):
            scope.host       = hosts[0]
            tables[0].beside = hosts[0]

   ########################################################################

   def side_join(self, segment):
      """
      post: true when segment runs from an outer table across to the
            table of a SELECT list subquery drawn beside it
      """
      return(    segment.kind == "join"
             and (   segment.a.table.beside is segment.b.table
                  or segment.b.table.beside is segment.a.table))

   ########################################################################

   def side_track(self, segment):
      """
      post: returns the y of the horizontal stretch of a line across
            to a box beside its table
      imp: like lines to boxes stacked below, each runs down its stub
           past its operator and on below every level of the table;
           the rightmost column's line runs highest, so lines do not
           cross on the way across
      """
      upper = segment.upper
      host  = upper.table
      ends  = sorted({id(e): e for leaf in self.leaves
                      for other in leaf.segments
                      if (self.side_join(other))
                      for e in other.ends()
                      if (e.table is host)
                      }.values(),
                     key = lambda e: -e.x)
      index = next(i for (i, e) in enumerate(ends) if (e is upper))
      return(  host.col_bottom + host.level_total["down"]
             + LEVEL_GAP_PX + index * TRACK_PX)

   ########################################################################

   def run(self):
      self.passes = []
      for table in self.tables:
         table.bands = {"up": 0, "down": 0}
         table.rows  = 0
         for column in table.columns.values():
            column.slots = {"up": [], "down": []}
      for node in self.nodes:
         node.links = []
      for segment in self.segments():
         segment.via   = []
         segment.trunk = None
      self.place_endpoints()
      self.place_columns()
      self.place_slots()
      self.place_leans()
      self.place_groups()
                               # the next run widens the gap before the
                               # leftmost column of each NOT, so its
                               # word fits beside its bracket
      self.not_first = {id(min((e.column for u in self.mark_units(m)
                                for e in u.ends),
                               key = lambda c: c.x))
                        for m in self.marks if (m.kind == "NOT")}
      self.place_trunks()
      self.place_levels()
      self.diagram.units  = self.units
      self.diagram.marks  = self.marks
      self.diagram.trunks = self.trunks
      self.place_rows()
      self.place_vertical()
      self.label_edge = max([end_of_filter(s)[0] + 4
                             + text_px(s.op, SMALL_PX)
                             for s in self.segments()
                             if (    s.kind == "filter"
                                 and s.a.table.beside is None)]
                            + [0])
                               # needs table heights, changes none;
                               # the second run orders lines sharing a
                               # gap by where the first sent them next
      self.place_passes()
      hints = {id(segment): segment.via for segment in self.passes}
      for segment in self.passes:
         segment.via = []
      self.place_passes(hints)
      self.place_links()
      self.place_marks()

   ########################################################################

   def segments(self):
      for leaf in self.leaves:
         for segment in leaf.segments:
            yield(segment)

   ########################################################################

   def place_endpoints(self):
      """
      use: decides which side (above the table name, or below the
           column names) each end of each line attaches to
      """
      for segment in self.segments():
         if (segment.kind == "join"):
            (upper, lower) = (     (segment.a, segment.b)
                              if (  segment.a.table.index
                                  < segment.b.table.index)
                              else (segment.b, segment.a))
            segment.upper = upper
            segment.lower = lower
            upper.side    = "down"
            lower.side    = "up"
            if (    lower.table.index - upper.table.index > 1
                and not self.side_join(segment)):
               self.passes.append(segment)
      for leaf in self.leaves:
         for segment in leaf.segments:
            if (segment.kind == "filter"):
               segment.a.side = self.filter_side(leaf, segment.a)
      for segment in self.segments():
         for end in segment.ends():
            end.column.slots[end.side].append(end)

   ########################################################################

   def filter_side(self, leaf, end):
      """
      post: returns the side a filter's stub goes to: below the column
            names, unless the nearest group around it (short of the
            whole WHERE) that has joins at the same table has them all
            above, so the group can be drawn on one side
      """
      for node in reversed(leaf.ancestors[1:]):
         sides = {e.side for other in iter_leaves(node)
                  if (other is not leaf)
                  for segment in other.segments
                  if (segment.kind == "join")
                  for e in segment.ends()
                  if (e.table is end.table)}
         if (sides):
            return("up" if (sides == {"up"}) else "down")
      return("down")

   ########################################################################

   def place_columns(self):
      stack = [t for t in self.tables if (t.beside is None)]
      side  = [t for t in self.tables if (t.beside is not None)]
      edge  = None
      for table in stack + side:
         if (table.beside is not None):
                               # right of every stacked box, and of
                               # any box beside placed before it, so
                               # no two boxes overlap
            if (edge is None):
                               # and of the stacked tables' value
                               # labels, as the first run found them
               edge = max([t.right for t in stack] + [300]
                          + [getattr(self, "label_edge", 0)]) + 24
            table.scope.left = edge + SIDE_GAP_PX + BOX_PAD_PX
                               # in the order of their first mention in
                               # the SQL; "*" first
         names      = sorted(table.columns,
                             key = lambda n: (
                                n != "*",
                                     table.columns[n].first
                                if (table.columns[n].first is not None)
                                else float("inf"),
                                n))
                               # each aggregate just right of the
                               # column it reads, after any earlier
                               # aggregate of that column
         for name in [n for n in names if (table.columns[n].source)]:
            names.remove(name)
         for name in [n for n in table.columns
                      if (table.columns[n].source)]:
            column = table.columns[name]
            at     = names.index(self.key_of(table, column.source))
            while (    at + 1 < len(names)
                   and table.columns[names[at + 1]].after is not None
                   and self.chained(table, names[at + 1], column.source)):
               at += 1
            column.after = table.columns[names[at]]
            names.insert(at + 1, name)
                               # the other columns an aggregate reads
                               # follow it, arrowed back into it, when
                               # they have no aggregates of their own
         for column in table.columns.values():
            column.feeds = None
         for name in [n for n in names if (table.columns[n].extra)]:
            column = table.columns[name]
            for other in column.extra:
               key = self.key_of(table, other)
               if (   other.feeds is not None
                   or any(c.source is other
                          for c in table.columns.values())):
                  continue
               names.remove(key)
               at = names.index(name)
               while (    at + 1 < len(names)
                      and table.columns[names[at + 1]].feeds is column):
                  at += 1
               names.insert(at + 1, key)
               other.feeds = column
         table.left = table.scope.left
         cursor     = table.left + 14
         for name in names:
            column       = table.columns[name]
            slots        = max(len(self.slot_groups(column, "up")),
                               len(self.slot_groups(column, "down"))
                               ) * SLOT_PX
                               # the ORDER BY position and check (top
                               # left) and the index mark (top right)
                               # widen the column
            check        = (  (CHECK_PX if (column.returned) else 0)
                            + self.order_px(column))
            mark         = MARK_PX if (column.marker) else 0
            if (id(column) in getattr(self, "not_first", ())):
               cursor   += NOT_PX + 10
            if (column.after is not None and check):
                               # an aggregate's arrow runs under its
                               # check and ORDER BY position: the gap
                               # need only clear them, not add to them
               cursor   -= COL_GAP_PX + check - max(COL_GAP_PX,
                                                    check + AGG_GAP_PX)
            column.width = max(text_px(name) + check + mark, slots)
            column.x     = cursor + check + max(text_px(name), slots) / 2
            cursor      += column.width + COL_GAP_PX
         table.order      = names
         table.name_right = table.left + text_px(table.title(),
                                                 NAME_CHAR_PX)
         table.right      = max(cursor - COL_GAP_PX, table.name_right)
         if (table.beside is not None):
                               # wide enough for its title too
            scope           = table.scope
            title           = (f"{scope.label} {MIDDLE_DOT} "
                               f"{scope.context}")
            scope.box_right = max(table.right + BOX_PAD_PX,
                                  scope.left - BOX_PAD_PX + 16
                                  + text_px(title, SMALL_PX))
            edge            = scope.box_right

   ########################################################################

   def key_of(self, table, column):
      return(next(k for (k, c) in table.columns.items() if (c is column)))

   ########################################################################

   def chained(self, table, name, source):
      """
      post: true when column name is an aggregate drawn from source,
            directly or through other aggregates
      """
      column = table.columns[name]
      while (column is not None):
         if (column.source is source):
            return(True)
         column = column.source
      return(False)

   ########################################################################

   def order_px(self, column):
      if (column.order_by is None):
         return(0)
      return(  len(str(column.order_by)) * ORDER_PX + 2
             + (DESC_PX if (column.order_desc) else 0))

   ########################################################################

   def direction(self, end):
      """
      post: returns the angle at which the line of end leaves its
            column: negative heads left, positive heads right
      imp: before the first run only the far end is known, with the
           table distance standing in for height
      """
      segment = end.segment
      if (id(end) in self.order):
         return(self.order[id(end)])
      if (segment.kind == "filter"):
         return(0.0)
      other = segment.b if (end is segment.a) else segment.a
      if (segment.kind == "self"):
         return(math.atan2(other.column.x - end.column.x, 1.0))
      rise = abs(other.table.index - end.table.index) * 100.0
      return(math.atan2(other.column.x - end.column.x, rise))

   ########################################################################

   def line_directions(self):
      """
      post: maps each endpoint to the angle of the first stretch of
            its line, using the routing of the run just made
      """
      order = {}
      for segment in self.segments():
         for end in segment.ends():
            order[id(end)] = self.direction(end)
         if (segment.kind != "join"):
            continue
         (upper, lower) = (segment.upper, segment.lower)
         nearest = (     (segment.via[0][1], segment.via[0][0].top)
                    if (segment.via)
                    else (lower.column.x, lower.table.top))
         order[id(upper)] = math.atan2(nearest[0] - upper.column.x,
                                       nearest[1] - upper.table.bottom)
         nearest = (     (segment.via[-1][1], segment.via[-1][0].bottom)
                    if (segment.via)
                    else (upper.column.x, upper.table.bottom))
         order[id(lower)] = math.atan2(nearest[0] - lower.column.x,
                                       lower.table.top - nearest[1])
      return(order)

   ########################################################################

   def place_slots(self):
      """
      use: every column has one stub per side; values and bar stubs
           meet it on the stub; a connector (to another table, to a
           box beside, or a same-table U) runs down that stub when it
           is the only line there, else it gets a stub of its own
           beside it, left to right in the order of the direction they
           head, so no stub forks
      """
      for table in self.tables:
         for column in table.columns.values():
            for side in ("up", "down"):
               ends   = column.slots[side]
               joins  = [e for e in ends
                         if (    e.segment.kind in ("join", "self")
                             and e.merge is None)]
               for end in ends:
                  end.x = column.x
               if (len(ends) < 2 or not joins):
                  continue
               joins.sort(key = self.direction)
               count  = len(joins)
               shift  = [(i - (count - 1) / 2) * STUB_SEP_PX
                         for i in range(count)]
                               # a branch never runs down the stub when
                               # something else is on it
               if (len(ends) > count or count > 1):
                  shift = [s if (abs(s) > 0.1)
                           else (STUB_SEP_PX
                                 if (self.direction(e) >= 0)
                                 else -STUB_SEP_PX)
                           for (s, e) in zip(shift, joins)]
               for (end, s) in zip(joins, shift):
                               # a U's stub carries no operator, so it
                               # can sit closer
                  if (end.segment.kind == "self"):
                     s *= U_SEP_PX / STUB_SEP_PX
                  end.x = column.x + s

   ########################################################################

   def slot_groups(self, column, side):
      """
      post: returns the ends at one side of a column, grouped so that
            stubs to the same bar share one slot
      """
      groups = {}
      for end in column.slots[side]:
         key = end.merge if (end.merge is not None) else id(end)
         groups.setdefault(key, []).append(end)
      return(list(groups.values()))

   ########################################################################

   def place_passes(self, hints = None):
      """
      use: picks where lines between non-adjacent tables cross each
           table between them: the white space between two column
           names nearest their straight course, avoiding the table
           name when practical, else straight through a column name
      imp: tables are swept top to bottom; lines sharing a gap are
           spaced in order of where they come from and where they
           go next (the next table they cross, per hints from an
           earlier run, else where they end), so they neither cross
           inside it nor where they part
      """
      current = {}
      for segment in self.passes:
         current[id(segment)] = (segment.upper.x,
                                 segment.upper.table.bottom)
      for table in self.tables:
         crossing = [s for s in self.passes
                     if (    table.beside is None
                         and (  s.upper.table.index
                              < table.index
                              < s.lower.table.index))]
         if (not crossing):
            continue
         (gaps, spans) = self.table_gaps(table)
         chosen        = {}
                               # this table's bars: a line crossing one
                               # looks as if it joins there
         bars          = [(x0, x1) for (t, _, _, x0, x1, _, _) in self.bars
                          if (t is table)]
                               # lines ending at this table: where they
                               # come from and where they land
         enders = []
                               # a trunk stands for its joins, ending
                               # where it meets the bar
         for other in (  [s for s in self.segments() if (s.trunk is None)]
                       + self.trunks):
            if (other.kind != "join" or other.lower.table is not table):
               continue
            if (id(other) in current):
               enders.append((current[id(other)][0], other.lower.x))
            elif (other.upper.table.index == table.index - 1):
               enders.append((other.upper.x, other.lower.x))
         for segment in crossing:
            (x, y)  = current[id(segment)]
            lower   = segment.lower
                               # aim at the straight course from here
                               # to the far end
            share   = (table.top - y) / max(lower.table.top - y, 1.0)
            ideal   = x + (lower.x - x) * share
            (key, center, room) = self.best_gap(table, gaps, spans,
                                                ideal, chosen, x,
                                                enders, bars)
            ahead   = [px for (other, px)
                       in (hints or {}).get(id(segment), [])
                       if (other.index > table.index)]
            chosen.setdefault(key, (center, room, []))[2].append(
               (x, ahead[0] if (ahead) else lower.x, segment))
         for (key, (center, room, members)) in chosen.items():
                               # weigh where a line comes from with
                               # where it ends: a bundle running down
                               # together then splits without crossing
            members.sort(key = lambda m: (m[0] + m[1], m[0]))
            count = len(members)
            if (key == "end"):
               center += (count - 1) * PASS_PX / 2
            for (position, (_, _, segment)) in enumerate(members):
               px = center + (position - (count - 1) / 2) * PASS_PX
               segment.via.append((table, px))
               current[id(segment)] = (px, table.bottom)

   ########################################################################

   def table_gaps(self, table):
      """
      post: returns (gaps, spans): the white spaces a line may cross
            table through, as (center, width, key), and the extents
            of the column names, as (left, right, name)
      """
      spans = []
      for name in table.order:
         column = table.columns[name]
         slots  = max(len(column.slots["up"]),
                      len(column.slots["down"])) * SLOT_PX
                               # same extent as place_columns gave it:
                               # the check and ORDER BY position are on
                               # the left of the name
         check  = (  (CHECK_PX if (column.returned) else 0)
                   + self.order_px(column))
         left   = column.x - max(text_px(name), slots) / 2 - check
         spans.append((left - 2, left + column.width + 2, name))
                               # filter labels below the columns are in
                               # the way too
      blocks = list(spans)
      for segment in self.segments():
         if (segment.kind != "join" and segment.a.table is table):
            if (segment.kind == "filter" and segment.a.side == "up"):
               x_end  = up_filter_end(segment)[0]
               (a, b) = (segment.a.x,
                         x_end + 4 + text_px(segment.op, SMALL_PX))
            else:
               (a, b) = self.row_span(table, segment, segment.row)
            if (segment.kind == "filter"):
               a = segment.a.x
            blocks.append((a - 3, b + 3, None))
                               # and the word of a NOT at this table
      for mark in self.marks:
         if ((mark.kind == "NOT") and (mark.table is table)):
            kids  = [self.unit_span(u) for u in self.mark_units(mark)]
            left  = min(a for (a, _) in kids) - 7
            blocks.append((left - 5 - NOT_PX, left + 2, None))
      gaps = []
      edge = table.left
      for (a, b, name) in sorted(blocks, key = lambda s: s[0]):
         if (a - edge > 8):
            gaps.append(((edge + a) / 2, a - edge, f"g{a:.0f}"))
         edge = max(edge, b)
      gaps.append((edge + 14, 1000.0, "end"))
      return(gaps, spans)

   ########################################################################

   def best_gap(self, table, gaps, spans, ideal, chosen, x = None,
                enders = (), bars = ()):
      """
      post: returns (key, center, room) of the cheapest place with
            room left to cross table
      imp: passing on the other side of a line that ends at this
           table than the side it came down on means crossing it,
           which costs more than going through the table name; going
           across one of the table's bars costs more still, since the
           line would seem to end on it
      """
      def crossings(at):
         if (x is None):
            return(0)
         return(sum(1 for (start, end) in enders
                    if ((x - start) * (at - end) < 0)))
      def on_bar(at):
         return(any(a - PASS_PX <= at <= b + PASS_PX for (a, b) in bars))
      best = None
      for (center, width, key) in gaps:
         room = max(int(width // PASS_PX), 1)
         if (key != "end" and len(chosen.get(key, (0, 0, []))[2]) >= room):
            continue
         cost = (  abs(center - ideal) + 60 * crossings(center)
                 + 200 * on_bar(center))
         if (table.left - 4 <= center <= table.name_right + 4):
            cost += 40
         if (best is None or cost < best[0]):
            best = (cost, key, center, room)
      for (a, b, name) in spans:
                               # through a column name only when every
                               # gap is far out of the way
         at   = min(max(ideal, a + 2), b - 2)
         cost = (  abs(at - ideal) + 160 + 60 * crossings(at)
                 + 200 * on_bar(at))
         if (cost < best[0]):
            best = (cost, ("col", name), at, 99)
      return(best[1:])

   ########################################################################

   def place_leans(self):
      """
      use: decides which end of a join line its operator sits nearer:
           the end of the left hand operand, as the predicate reads
           (e IN b, t > m); "=" follows its sibling predicates, or
           sits in the middle when they give no direction
      """
      symmetric = ("=", "<>", "!=")
      def subject(segment):
         if (segment.op in symmetric):
            return(None)
         return(segment.a.table)
      for leaf in self.leaves:
         for segment in leaf.segments:
            if (segment.kind != "join"):
               continue
            table = subject(segment)
            if (table is None and leaf.ancestors):
               tables = set()
               for sibling in leaf.ancestors[-1].children:
                  if (sibling is leaf or not isinstance(sibling, Leaf)):
                     continue
                  for other in sibling.segments:
                     if (other.kind == "join" and subject(other)):
                        tables.add(subject(other))
               pair = (segment.a.table, segment.b.table)
               if (len(tables) == 1 and list(tables)[0] in pair):
                  table = list(tables)[0]
            segment.lean = (     "middle" if (table is None)
                            else "upper"  if (table is segment.upper.table)
                            else "lower")

   ########################################################################

   def place_groups(self):
      """
      use: works out the levels at each side of each table: for every
           condition tree, the part touching that side is cut out; each
           AND group's own conditions share one level, conditions on
           different levels are OR'ed, and nested groups are enclosed
           in [ ] (AND, joining levels) or ( ) (OR)
      imp: the alternatives of a top level OR need no marks: being on
           different levels already makes them alternatives
      """
      self.units   = []
      self.marks   = []
      self.unit_of = {}
                               # a correlated [NOT] EXISTS: the ends its
                               # subquery's conditions have at outer
                               # tables sit on the level of the EXISTS
                               # condition itself, in the outer query
      self.exists_ends = {}
      claimed          = set()
      for leaf in self.leaves:
         inner = next((s for s in self.diagram.scopes
                       if (s.select is leaf.exists)), None)
         if ((leaf.exists is None) or (inner is None)):
            continue
         ends = [e for l in inner.leaves for s in l.segments
                 if (s.kind == "join") for e in s.ends()
                 if (e.table.scope is not inner)]
         self.exists_ends[id(leaf)] = ends
         claimed.update(id(e) for e in ends)
      self.claimed = claimed
      roots = [scope.root for scope in self.diagram.scopes
               if (scope.root is not None)]
      for table in self.tables:
         for side in ("down", "up"):
            band = 0
                               # the level a subquery's own conditions
                               # can share, being AND'ed with the query
                               # around it: the first level, when that
                               # is not one of several alternatives
            shared = None
            for root in roots:
               item = self.restrict(root, table, side)
               if (item is None):
                  continue
               if (item[0] == "or"):
                  for kid in item[2]:
                     band = self.assign_bands(
                               self.shape(kid, table, side, 1), band)
                  continue
               element = self.shape(item, table, side, 0)
               if (isinstance(element, Unit) and shared is not None):
                  self.assign_bands(element, shared)
                  continue
               if (band == 0):
                  shared = 0
               band = self.assign_bands(element, band)
            table.bands[side] = max(table.bands[side], band)

   ########################################################################

   def restrict(self, item, table, side):
      """
      post: returns item cut down to the conditions with an end at one
            side of table, as ("leaf", leaf, ends) or (kind, node,
            kids), or None when nothing is left
      """
      if (isinstance(item, Leaf)):
         if (item.feed):
            return(None)
         ends = [e for s in item.segments for e in s.ends()
                 if (    e.table is table and e.side == side
                     and id(e) not in self.claimed)]
         ends += [e for e in self.exists_ends.get(id(item), [])
                  if (e.table is table and e.side == side)]
         return(("leaf", item, ends) if (ends) else None)
      kids = [k for k in (self.restrict(c, table, side)
                          for c in item.children) if (k is not None)]
      if (not kids):
         return(None)
      return((item.kind.lower(), item, kids))

   ########################################################################

   def shape(self, item, table, side, depth):
      """
      post: returns the Unit or Mark that draws a cut down item
      """
      if (item[0] == "leaf"):
         leaf  = item[1]
         owner = leaf.ancestors[-1] if (leaf.ancestors) else None
         return(self.new_unit(owner, item[2], table, side))
      if (item[0] == "and"):
         leaves   = [k for k in item[2] if (k[0] == "leaf")]
         children = []
         if (leaves):
            children.append(self.new_unit(item[1],
                                          [e for k in leaves
                                             for e in k[2]],
                                          table, side))
         children += [self.shape(k, table, side, depth + 1)
                      for k in item[2] if (k[0] != "leaf")]
         if (len(children) == 1):
            return(children[0])
         return(self.new_mark("AND", item[1], children, depth, table,
                              side))
      children = [self.shape(k, table, side, depth + 1) for k in item[2]]
      return(self.new_mark(item[0].upper(), item[1], children, depth,
                           table, side))

   ########################################################################

   def new_unit(self, node, ends, table, side):
      unit = Unit(f"U{len(self.units)}", node, table, side, ends)
      self.units.append(unit)
      return(unit)

   ########################################################################

   def new_mark(self, kind, node, children, depth, table, side):
      mark       = Mark(kind, node, children, depth)
      mark.table = table
      mark.side  = side
      self.marks.append(mark)
      return(mark)

   ########################################################################

   def assign_bands(self, element, band):
      """
      post: every AND group gets its own level, in SQL order, so the
            levels under one bracket or arc sit together; returns the
            next free level
      """
      if (isinstance(element, Mark)):
         for child in element.children:
            band = self.assign_bands(child, band)
         return(band)
      element.band = band
      for end in element.ends:
         self.unit_of[id(end)] = element
      return(band + 1)

   ########################################################################

   def place_trunks(self):
      """
      use: two or more columns of one table joined, on one level, to
           columns of the same other table are drawn as stubs to a bar
           at each table plus a single trunk between the bars
      imp: no other join may sit between them on a bar, or nothing
           would show which column belongs; such joins stay separate
           lines. columns that pair up crossed (a.X = b.Q, a.Y = b.P
           with P left of Q) are labelled with their partners
      """
      self.trunks = []
      for end in [e for s in self.segments() for e in s.ends()]:
         end.merge = None
      groups = {}
      for leaf in self.leaves:
         if (leaf.feed):
            continue
         for segment in leaf.segments:
            if (segment.kind != "join"):
               continue
            top    = self.unit_of.get(id(segment.upper))
            bottom = self.unit_of.get(id(segment.lower))
            if (top is None or bottom is None):
               continue
            key = (id(top), id(bottom), id(segment.upper.table),
                   id(segment.lower.table))
            groups.setdefault(key, (top, bottom, []))[2].append(segment)
      for (top, bottom, segments) in groups.values():
                               # a bar only gathers two or more columns
                               # joined to the same other bar
         if (len({id(s.upper.column) for s in segments}) < 2):
            continue
         segments.sort(key = lambda s: (s.upper.column.x,
                                        s.lower.column.x))
         if (   self.interleaved(top, segments, "upper")
             or self.interleaved(bottom, segments, "lower")):
            continue
         trunk = self.new_trunk(top, bottom, segments)
                               # pairs that do not line up left to right
                               # get each partner's name on both bars
         lowers        = [s.lower.column.x for s in segments]
         trunk.crossed = any(b < a for (a, b) in zip(lowers, lowers[1:]))
         self.trunks.append(trunk)
      self.passes = [s for s in self.passes if (s.trunk is None)]
      for trunk in self.trunks:
         if (trunk.lower.table.index - trunk.upper.table.index > 1):
            self.passes.append(trunk)

   ########################################################################

   def interleaved(self, unit, segments, which):
      """
      post: true when another join's column sits strictly between the
            columns these segments use on unit's bar
      """
      if (unit is None):
         return(False)
      mine   = [getattr(s, which) for s in segments]
      xs     = [e.column.x for e in mine]
      (a, b) = (min(xs), max(xs))
      for end in unit.ends:
         if (    all(end is not m for m in mine)
             and end.segment.kind == "join"
             and a < end.column.x < b):
            return(True)
      return(False)

   ########################################################################

   def new_trunk(self, top, bottom, segments):
      """
      post: returns the trunk segment between pseudo ends: on a bar,
            in the middle of its stubs; with no bar, on the column
      """
      ends = []
      for (unit, which, side) in ((top, "upper", "down"),
                                  (bottom, "lower", "up")):
         mine = [getattr(s, which) for s in segments]
         end  = Endpoint(mine[0].table, None)
         end.side = side
         if (unit is None):
            end.x      = mine[0].x
            end.column = mine[0].column
         else:
            end.x = self.landing(mine[0].table, [e.x for e in mine])
            for e in mine:
                               # stubs of one column to one bar share a
                               # slot from the next run on
               e.merge = unit.ident
         ends.append(end)
      trunk         = Segment(ends[0], ends[1], "")
      trunk.upper   = ends[0]
      trunk.lower   = ends[1]
      trunk.members = segments
      trunk.units   = (top, bottom)
      trunk.leaves  = [l for l in self.leaves
                       if (any(s in segments for s in l.segments))]
      for segment in segments:
         segment.trunk = trunk
                               # an operator on a trunk (one join, one
                               # end on a column) leans to its left
                               # hand side's end
      only        = segments[0]
      trunk.lean  = "upper" if (only.a is only.upper) else "lower"
      return(trunk)

   ########################################################################

   def landing(self, table, xs):
      """
      post: returns where a trunk meets a bar spanning xs: as near its
            middle as it can be while clear of every column's stub
      """
      (x0, x1) = (min(xs), max(xs))
      middle   = (x0 + x1) / 2
      stubs    = sorted(c.x for c in table.columns.values())
      if (all(abs(middle - s) >= 8 for s in stubs)):
         return(middle)
      gaps = [(a + b) / 2 for (a, b) in zip(stubs, stubs[1:])
              if (x0 < (a + b) / 2 < x1 and b - a >= 16)]
      return(min(gaps, key = lambda g: abs(g - middle)) if (gaps)
             else middle)

   ########################################################################

   def place_levels(self):
      """
      use: sizes the levels at each side of each table: a level with a
           bar is deeper, and deeper still when partner names sit
           between its operators and its bar; then sets where each
           value's line leaves its column
      post: table.levels[side][band] holds distances out from the
            column names: near and far edge, marker, partner, bar
      """
      bars = {}
      for trunk in self.trunks:
         for (unit, side) in zip(trunk.units, ("down", "up")):
            key = (id(unit.table), side, unit.band)
            bars[key] = bars.get(key, False) or trunk.crossed
      for table in self.tables:
         table.levels = {}
         for side in ("down", "up"):
            levels = []
            cursor = LEVEL_GAP_PX
            for band in range(table.bands[side]):
               key   = (id(table), side, band)
               level = {"near": cursor, "c": cursor + LEVEL_MID_PX,
                        "bar": None, "partner": None}
               if (key in bars):
                  if (bars[key]):
                     level["partner"] = level["c"] + PARTNER_PX / 2
                     level["bar"]     = level["c"] + PARTNER_PX
                  else:
                     level["bar"]     = level["c"] + BAR_STUB_PX
                  level["far"] = level["bar"] + 6
               else:
                  level["far"] = cursor + LEVEL_PX
               cursor = level["far"]
               levels.append(level)
            table.levels[side] = levels
            table.level_total  = getattr(table, "level_total", {})
            table.level_total[side] = cursor if (levels) else 0
      for segment in self.segments():
         if (segment.kind == "filter"):
            unit = self.unit_of.get(id(segment.a))
            segment.a.join_off = (
                 segment.a.table.levels[segment.a.side][unit.band]["c"]
                 if (unit is not None) else LEVEL_GAP_PX + LEVEL_MID_PX)
                               # the bars, for hops and gap finding
      self.bars = []
      for trunk in self.trunks:
         for (unit, which, side) in zip(trunk.units, ("upper", "lower"),
                                        ("down", "up")):
            xs = [getattr(s, which).x for s in trunk.members]
            self.bars.append((unit.table, side, unit.band,
                              min(xs), max(xs), xs, trunk))

   ########################################################################

   def place_marks(self):
      """
      use: sizes each enclosure to just outside what it holds, so a
           nested one sits inside its parent's delimiters, and clear of
           every vertical line at that side of the table
      """
      self.verticals = {}
      for segment in list(self.segments()) + self.trunks:
         for end in segment.ends():
            self.verticals.setdefault((id(end.table), end.side),
                                      []).append(end.x)
         for (table, x) in segment.via:
                               # a line passing through runs straight
                               # down through both sides of the table
            for side in ("up", "down"):
               self.verticals.setdefault((id(table), side),
                                         []).append(x)
      for mark in self.marks:
         self.mark_extent(mark)

   ########################################################################

   def mark_extent(self, element):
      """
      post: returns (left, right) of a bar, a single line's operator or
            an enclosure (setting the enclosure's own left and right)
      """
      if (isinstance(element, Unit)):
         return(self.unit_span(element))
      spans = [self.mark_extent(child) for child in element.children]
      left  = min(a for (a, _) in spans) - 7
      right = max(b for (_, b) in spans) + 7
                               # a side and its stubs take 4 px; step it
                               # outward until no vertical line runs in
                               # that strip (lines at an angle are fine)
      xs    = self.verticals.get((id(element.table), element.side), [])
      while (any(left - 2 <= x <= left + 6 for x in xs)):
         left -= 4
      while (any(right - 6 <= x <= right + 2 for x in xs)):
         right += 4
      (element.left, element.right) = (left, right)
      if (element.kind != "NOT"):
         return(left, right)
                               # a NOT has its word against the left of
                               # its bracket; where a line runs there,
                               # just above the bracket's top left
                               # corner instead, never apart from it
      element.word_above = any(left - 3 - NOT_PX - 2 <= x <= left
                               for x in xs)
      return((left, right) if (element.word_above)
             else (left - 3 - NOT_PX, right))

   ########################################################################

   def mark_units(self, mark):
      """
      post: returns every level group (Unit) inside an enclosure
      """
      return([u for child in mark.children
              for u in ([child] if (isinstance(child, Unit))
                        else self.mark_units(child))])

   ########################################################################

   def unit_span(self, unit):
      """
      post: returns (x0, x1) of what a level holds at one side of a
            table: its stubs, give or take room for their operators
      """
      (lo, hi) = ([], [])
      mark     = text_px("(+)", SMALL_PX) + 3
      for end in unit.ends:
         x = end.column.x
         (left, right) = (x - 7, x + 7)
                               # and their (+): left of a filter's
                               # stub; past a join's operator, on the
                               # left below the names, right above
         if (end.join_mark and end.segment.kind == "filter"):
            left = x - 4 - mark
         elif (end.join_mark):
            gap = text_px(end.segment.op, SMALL_PX) / 2 + 3 + mark
            if (end.side == "down"):
               left  = x - gap
            else:
               right = x + gap
         lo.append(left)
         hi.append(right)
      return(min(lo), max(hi))

   ########################################################################

   def place_links(self):
      """
      use: ties the alternatives of a nested OR together with dotted
           lines, each between the closest points of two neighbouring
           alternatives
      imp: a top level OR is left unmarked: unconnected lines already
           read as alternatives; two alternatives that meet at a side
           of a table are already tied there by the OR's arc, so they
           get no link
      """
      for node in self.nodes:
         if (node.kind != "OR" or node.is_root):
            continue
         alternatives = []
         for child in node.children:
            ends = (     child.endpoints() if (isinstance(child, Node))
                    else [e for s in child.segments for e in s.ends()])
            if (ends):
               alternatives.append(ends)
         for (first, second) in zip(alternatives, alternatives[1:]):
            meet = ({(id(e.table), e.side) for e in first}
                    & {(id(e.table), e.side) for e in second})
            if (meet):
               continue
            first  = [self.anchor(end) for end in first]
            second = [self.anchor(end) for end in second]
            node.links.append(min(((a, b) for a in first
                                          for b in second),
                                  key = lambda p: math.hypot(
                                     p[0][0] - p[1][0],
                                     p[0][1] - p[1][1])))

   ########################################################################

   def anchor(self, end):
      """
      post: returns the point on end's line, just off its column name,
            where an OR link attaches
      """
      table = end.table
                               # clear of the column name and of the
                               # AND bars further out
      if (end.side == "up"):
         return(end.x, table.col_top - 1)
      return(end.x, table.col_bottom + 1)

   ########################################################################

   def place_rows(self):
      """
      use: stacks the value labels of filter lines and the bottoms of
           same-table connectors in rows below a table
      imp: placed right to left: the rightmost gets the top row, and
           each one to its left drops a row only when its label would
           run into a line or label already placed, so labels never
           sit on a line
      """
      for table in self.tables:
         items  = [s for s in self.segments()
                   if (    s.kind == "filter" and s.a.table is table
                       and s.a.side == "down")]
         for segment in self.segments():
            if (segment.kind == "filter" and segment.a.side == "up"):
               segment.row = 0
         placed = []
         rows   = 0
         for segment in sorted(items, key = lambda s: -max(
                                  e.x for e in s.ends())):
            row   = 0
            limit = len(placed) + 1
            while (    row <= limit
                   and not self.row_fits(table, segment, row, placed)):
               row += 1
                               # when its own line must cross a label
                               # above whatever row it takes, settle for
                               # the first row where its label is clear
            if (row > limit):
               row = 0
               while (not self.row_fits(table, segment, row, placed,
                                        strict = False)):
                  row += 1
            segment.row = row
            placed.append(segment)
            rows = max(rows, row + 1)
         table.rows = rows
         for segment in placed:
            table.right = max(table.right,
                              self.row_span(table, segment,
                                            segment.row)[1])

   ########################################################################

   def row_fits(self, table, segment, row, placed, strict = True):
      """
      post: true when segment's label in row clears every label in its
            row and every line passing through its row, and (if strict)
            its own lines clear the labels in the rows above
      imp: the non-strict test always passes below every placed row,
           so a search with it ends
      """
      (a, b) = self.row_span(table, segment, row)
      for other in placed:
         if (other.row == row):
            (c, d) = self.row_span(table, other, row)
            if (not (b < c or d < a)):
               return(False)
         if (other.row >= row):
            for x in self.lines_at(table, other, row):
               if (a - LABEL_CLEAR_PX <= x <= b + LABEL_CLEAR_PX):
                  return(False)
         elif (strict):
            (c, d) = self.row_span(table, other, other.row)
            for x in self.lines_at(table, segment, other.row):
               if (c - LABEL_CLEAR_PX <= x <= d + LABEL_CLEAR_PX):
                  return(False)
      return(True)

   ########################################################################

   def lines_at(self, table, segment, row):
      """
      post: returns the x of segment's line(s) at the height of row
      """
      if (segment.kind == "filter"):
         reach = filter_drop(table, row) - segment.a.join_off
         return([segment.a.x + max(reach, 0) * FILTER_TILT])
      return([segment.a.x, segment.b.x])

   ########################################################################

   def row_span(self, table, segment, row):
      """
      post: returns the horizontal extent segment's label takes in
            row: a filter's label sits to the right of the end of its
            slanted line, a same-table connector's under its middle
      """
      if (segment.kind == "filter"):
         reach = filter_drop(table, row) - segment.a.join_off
         x_end = segment.a.x + max(reach, 0) * FILTER_TILT
         return(x_end, x_end + 4 + text_px(segment.op, SMALL_PX))
      half     = text_px(segment.op, SMALL_PX) / 2 + 4
      (xa, xb) = sorted((segment.a.x, segment.b.x))
      middle   = (xa + xb) / 2
      return(min(xa, middle - half) - 2, max(xb, middle + half) + 2)

   ########################################################################

   def place_vertical(self):
      """
      use: walks the items top to bottom; boxes open above their
           first table and close below their last one
      """
      diagram  = self.diagram
      y        = 16
      last     = 16
      previous = None
      lowest   = 0
      for item in diagram.items:
         kind = item[0]
         if (kind in ("open", "close") and item[1].host is not None):
                               # a box beside: placed against its
                               # host, the stack carries on below
            if (kind == "open"):
               self.place_side(item[1])
            lowest = max(lowest, item[1].box_bottom)
            continue
         if (kind == "table" and item[1].beside is not None):
            continue
         if (kind in ("open", "close") and self.empty(item[1])):
                               # every table it read was folded away
            continue
         if (kind == "open"):
                               # a box starts a little below whatever
                               # is above it
            if (previous in ("table", "close")):
               y = last + 14
            item[1].box_top = y
            y += BOX_TITLE_PX
         elif (kind == "close"):
            last += 10
            item[1].box_bottom = last
            y = last + GAP_PX
         elif (kind == "sep"):
            diagram.seps.append((item[1], item[2], last + 28))
            y = last + 46
         else:
            self.place_table(item[1], y)
            last = item[1].bottom
            y    = last + GAP_PX
         previous = kind
      right = max([t.right for t in self.tables if (t.beside is None)]
                  + [300])
      self.right            = right
      diagram.right         = right
      diagram.width         = max([right + 40]
                                  + [s.box_right + 20
                                     for s in diagram.scopes
                                     if (s.host is not None)])
      last                  = max(last, lowest)
      notes                 = sum(len(s.notes) for s in diagram.scopes)
      diagram.notes_y       = last + 26
      diagram.height        = last + 24 + notes * 16

   ########################################################################

   def empty(self, scope):
      """
      post: true when a box would hold no table: its own tables, and
            those of the boxes inside it, were all folded away
      """
      return(not any(t.scope is scope or self.inside(t.scope, scope)
                     for t in self.tables))

   ########################################################################

   def inside(self, scope, outer):
      while (scope is not None):
         if (scope.parent is outer):
            return(True)
         scope = scope.parent
      return(False)

   ########################################################################

   def place_side(self, scope):
      """
      use: places a SELECT list subquery's box beside its host: its top
           level with the underline of the host's name, its table's
           name below the lines coming across to it
      """
      host  = scope.host
      table = next(iter(scope.tables.values()))
      lines = [self.side_track(segment)
               for leaf in scope.leaves
               for segment in leaf.segments
               if (self.side_join(segment))]
      scope.box_top = host.name_y + 5
      self.place_table(table, max([scope.box_top + BOX_TITLE_PX]
                                  + [y - 2 for y in lines]))
      scope.box_bottom = table.bottom + 10

   ########################################################################

   def place_table(self, table, y):
      table.top        = y
      table.name_y     = y + 20
                               # levels for lines from tables above sit
                               # between the name and the column names,
                               # mirroring the levels below the columns
      up               = table.level_total["up"]
      table.col_top    = table.name_y + 10 + up
      table.col_y      = table.col_top + 12
      table.col_bottom = table.col_y + 5
      down             = table.level_total["down"]
      table.filter_top = (  table.col_bottom + 3 + down + 4
                          + (FILTER_REACH_PX if (table.rows) else 0))
      table.bottom     = (  table.filter_top
                          + table.rows * ROW_PX
                          + (6 if (table.rows) else 0))

###########################################################################

def filter_drop(table, row):
   """
   post: returns how far below the column names a filter line in row
         ends; place_table's spacing, so usable before tables are
         placed
   """
   return(  12 + FILTER_REACH_PX + table.level_total["down"]
          + row * ROW_PX)

###########################################################################

def filter_end(segment):
   """
   post: returns (x, y) of the value end of a filter line
   """
   table = segment.a.table
   drop  = filter_drop(table, segment.row)
   reach = max(drop - segment.a.join_off, 0)
   return(segment.a.x + reach * FILTER_TILT, table.col_bottom + drop)

###########################################################################

def up_filter_end(segment):
   """
   post: returns (x, y) of the value end of a filter line above the
         column names: up and to the right, clear above every level
   """
   table = segment.a.table
                               # clear of the enclosures' edges too
   rise  = table.level_total["up"] + 12
   reach = max(rise - segment.a.join_off, 0)
   return(segment.a.x + reach * FILTER_TILT, table.col_top - rise)

###########################################################################

def end_of_filter(segment):
   """
   post: returns (x, y) of the value end of a filter line, above or
         below the column names
   """
   return(     up_filter_end(segment) if (segment.a.side == "up")
          else filter_end(segment))

###########################################################################

def level_y(table, side, band, what = "c"):
   """
   post: returns the y of one part of a level: "c" its marker,
         "partner" its partner names, "bar" its bar, "near"/"far" its
         edges
   """
   distance = table.levels[side][band][what]
   return(     table.col_top - distance if (side == "up")
          else table.col_bottom + distance)

###########################################################################

def band_y(table, side, band):
   return(level_y(table, side, band, "c"))

###########################################################################
###
### r e n d e r i n g
###
###########################################################################

def num(value):
   return(f"{value:.1f}".rstrip("0").rstrip("."))

###########################################################################

def esc(text):
   return(html.escape(str(text), quote = True))

###########################################################################

                               # bars that vertical lines hop over,
                               # set by SvgRenderer for each diagram:
                               # (y, x0, x1, xs of the lines on it)
HOPS = []

def line_to(x0, y0, x1, y1):
   """
   use: svg for a straight line from (x0, y0) to (x1, y1); a vertical
        one that crosses a bar it is not part of jumps it with a small
        curve bulging right, like a backward C
   """
   if ((abs(x1 - x0) > 0.01) or (abs(y1 - y0) < 2 * HOP_PX)):
      return(f"L{num(x1)},{num(y1)}")
   (step, sweep) = ((1, 1) if (y1 > y0) else (-1, 0))
   parts = []
   for (y, a, b, members) in sorted(HOPS, key = lambda h: h[0] * step):
      if (    (a + 0.5 < x0 < b - 0.5)
          and (min(y0, y1) + HOP_PX < y < max(y0, y1) - HOP_PX)
          and all(abs(x0 - m) > 0.5 for m in members)):
         parts.append(f"L{num(x0)},{num(y - step * HOP_PX)} "
                      f"A{HOP_PX},{HOP_PX} 0 0 {sweep} "
                      f"{num(x0)},{num(y + step * HOP_PX)}")
   parts.append(f"L{num(x1)},{num(y1)}")
   return(" ".join(parts))

###########################################################################

def rounded_path(points, radius = CORNER_PX):
   """
   use: svg path through points, made of straight segments whose
        corners are rounded
   usage: radius 0 gives pointed corners
   imp: vertical stretches hop the bars they cross (see line_to)
   """
   parts = [f"M{num(points[0][0])},{num(points[0][1])}"]
   (x, y) = points[0]
   for index in range(1, len(points) - 1):
      ((px, py), (cx, cy), (nx, ny)) = points[index - 1:index + 2]
      before = math.hypot(cx - px, cy - py)
      after  = math.hypot(nx - cx, ny - cy)
      cross  = (cx - px) * (ny - cy) - (cy - py) * (nx - cx)
      if (before < 0.01 or after < 0.01 or abs(cross) < 0.01
          or radius <= 0):
         parts.append(line_to(x, y, cx, cy))
         (x, y) = (cx, cy)
         continue
      r = min(radius, before / 2, after / 2)
      (ax, ay) = (cx - (cx - px) / before * r, cy - (cy - py) / before * r)
      (bx, by) = (cx + (nx - cx) / after * r, cy + (ny - cy) / after * r)
      parts.append(line_to(x, y, ax, ay) +
                   f" Q{num(cx)},{num(cy)} {num(bx)},{num(by)}")
      (x, y) = (bx, by)
   parts.append(line_to(x, y, points[-1][0], points[-1][1]))
   return(" ".join(parts))

###########################################################################

class SvgRenderer:
   """
   use: turns the model's diagrams into one SVG document
   """
   def __init__(self, model):
      self.model = model

   ########################################################################

   def render(self):
      """
      post: returns the svg markup for every statement of the model
      """
      panels = []
      y      = 10
      width  = 400
      many   = len(self.model.diagrams) > 1
      for (number, diagram) in enumerate(self.model.diagrams):
         if (many):
            panels.append(f'<text class="stmt" x="{MARGIN_PX - 20}" '
                          f'y="{num(y + 16)}">statement {number + 1}'
                          '</text>')
            y += 28
         diagram.layout = Layout(diagram)
         panels.append(f'<g transform="translate(0,{num(y)})">')
         panels.append(self.diagram_svg(diagram))
         panels.append("</g>")
         y    += diagram.height + 26
         width = max(width, diagram.width)
      return(f'<svg class="vq" xmlns="http://www.w3.org/2000/svg" '
             f'width="{num(width)}" height="{num(y)}" '
             f'viewBox="0 0 {num(width)} {num(y)}">'
             f'<style>{SVG_STYLE}</style>'
             '<defs><marker id="vqArrow" viewBox="0 0 10 12" '
             'refX="10" refY="6" markerWidth="8" markerHeight="9.6" '
             'markerUnits="userSpaceOnUse" '
             'orient="auto-start-reverse">'
             '<path d="M0,0 L10,6 L0,12 z" fill="#4b5563"/>'
             '</marker>'
             '<marker id="vqFeed" viewBox="0 0 10 10" '
             'refX="10" refY="5" markerWidth="7" markerHeight="7" '
             'markerUnits="userSpaceOnUse" '
             'orient="auto-start-reverse">'
             '<path d="M0,1 L10,5 L0,9 z" fill="#7c3aed"/>'
             '</marker>'
             '<marker id="vqAgg" viewBox="0 0 10 10" '
             'refX="10" refY="5" markerWidth="6" markerHeight="6" '
             'markerUnits="userSpaceOnUse" orient="auto">'
             '<path d="M0,1 L10,5 L0,9 z" fill="#4b5563"/>'
             '</marker></defs>'
             + "".join(panels) + "</svg>")

   ########################################################################

   def diagram_svg(self, diagram):
      out    = [f'<rect class="panel" x="6" y="2" '
                f'width="{num(diagram.width - 12)}" '
                f'height="{num(diagram.height - 4)}" rx="6"/>']
      right  = diagram.right + 24
      for scope in diagram.scopes:
         if (scope.boxed and not diagram.layout.empty(scope)):
            out.append(self.box_svg(scope, right))
      for (text, indent, y) in diagram.seps:
         x = MARGIN_PX + BOX_PAD_PX + indent * INDENT_PX
         out.append(f'<text class="sep" x="{num(x)}" y="{num(y)}">'
                    f'{esc(text)}</text>')
      self.layout    = diagram.layout
      HOPS[:] = []
      for (table, side, band, x0, x1, xs, _) in diagram.layout.bars:
         HOPS.append((level_y(table, side, band, "bar"), x0, x1, xs))
                               # faint lines between levels, under all
      for table in diagram.layout.tables:
         for side in ("down", "up"):
            for level in table.levels[side][1:]:
               y = (     table.col_top - level["near"] if (side == "up")
                    else table.col_bottom + level["near"])
               out.append(f'<line class="lvl" x1="{num(table.left)}" '
                          f'y1="{num(y)}" x2="{num(table.right)}" '
                          f'y2="{num(y)}"/>')
                               # operators go over every line, so each
                               # blanks out whatever line runs under it
      self.ops_layer  = []
      self.scope_of = {id(leaf): scope for scope in diagram.scopes
                       for leaf in scope.leaves}
      for scope in diagram.scopes:
         for leaf in scope.leaves:
            out.append(self.leaf_svg(leaf))
      for leaf in diagram.feeds:
         out.append(self.leaf_svg(leaf))
      self.layout = diagram.layout
      for trunk in diagram.trunks:
         out.append(self.trunk_svg(trunk))
      for bar in diagram.layout.bars:
         out.append(self.bar_svg(bar))
      for mark in diagram.marks:
         out.append(self.mark_svg(diagram.layout, mark))
      out.append('<g class="ops">' + "".join(self.ops_layer) + "</g>")
      for scope in diagram.scopes:
         for node in scope.nodes:
            out.append(self.node_svg(node))
      for item in diagram.items:
         if (item[0] == "table"):
            out.append(self.table_svg(item[1]))
      y = diagram.notes_y
      for scope in diagram.scopes:
         for note in scope.notes:
            out.append(f'<text class="note" x="{MARGIN_PX}" '
                       f'y="{num(y)}">not drawn: '
                       f'{esc(shorten(note, 90))}</text>')
            y += 16
      return("".join(out))

   ########################################################################

   def box_svg(self, scope, right):
      """
      imp: nested boxes step in a little on the right too, so their
           edges stay apart from their parent's
      """
      cls  = "box cte" if (scope.context.startswith("WITH")) else "box"
      text = scope.context
      if (cls == "box cte"):
                               # with the alias(es) the query reads the
                               # CTE by: WITH RECENT r
         aliases = self.model.cte_aliases.get(text[5:], [])
         text   += "".join(f" {a}" if (i == 0) else f", {a}"
                           for (i, a) in enumerate(aliases))
      x    = scope.left - BOX_PAD_PX
      x2   = (     scope.box_right if (scope.host is not None)
              else right - max(scope.indent, 0) * 6)
      return(f'<rect class="{cls}" x="{num(x)}" '
             f'y="{num(scope.box_top)}" width="{num(x2 - x)}" '
             f'height="{num(scope.box_bottom - scope.box_top)}" '
             'rx="4"/>'
             f'<text class="btitle" x="{num(x + 8)}" '
             f'y="{num(scope.box_top + 15)}">{esc(scope.label)} '
             f'{MIDDLE_DOT} {esc(text)}</text>')

   ########################################################################

   def leaf_svg(self, leaf):
      nodes  = " ".join(node.ident for node in leaf.ancestors)
      out    = [f'<g class="leaf" data-leaf="{leaf.ident}" '
                f'data-nodes="{nodes}" data-sql="{esc(leaf.sql)}">'
                f'<title>{esc(leaf.sql)}</title>']
      labels = []
      layout = self.layout
      def draw(d, extra = ""):
         out.append(f'<path class="hit" d="{d}"/>')
         out.append(f'<path class="ln{extra}" d="{d}"/>')
      for segment in leaf.segments:
         if (segment.trunk is not None):
                               # a column's stub to its bar
            trunk = segment.trunk
            for (end, unit) in ((segment.upper, trunk.units[0]),
                                (segment.lower, trunk.units[1])):
               table = end.table
               y_bar = level_y(table, end.side, unit.band, "bar")
               y0    = (     table.col_bottom if (end.side == "down")
                        else table.col_top)
               draw(rounded_path([(end.column.x, y0),
                                  (end.column.x, y_bar)]))
            self.ops_layer += self.op_svg(segment)
            continue
         if (segment.kind == "join"):
            (path, _) = self.join_path(segment)
            if (leaf.feed):
                               # the arrow points into the column read
                               # through the CTE / inline view: the a
                               # end; paths run from upper to lower
               marker = (     "marker-start" if (segment.a is segment.upper)
                         else "marker-end")
               out.append(f'<path class="hit" d="{path}"/>')
               out.append(f'<path class="ln feed" d="{path}" '
                          f'{marker}="url(#vqFeed)"/>')
            else:
               draw(path)
               self.ops_layer += self.op_svg(segment)
               self.ops_layer += self.exists_svg(leaf, segment, _)
            continue
         table = segment.a.table
         unit  = layout.unit_of.get(id(segment.a))
         c     = (     band_y(table, "down", unit.band)
                  if (unit is not None and segment.a.side == "down")
                  else table.col_bottom + LEVEL_GAP_PX + LEVEL_MID_PX)
         if (segment.kind == "filter"):
                               # along the stub to its level (above the
                               # names or below), where the value's
                               # arrow points, stopping short
            x  = segment.a.column.x
            up = segment.a.side == "up"
            if (up):
               c = (     band_y(table, "up", unit.band)
                    if (unit is not None)
                    else table.col_top - LEVEL_GAP_PX - LEVEL_MID_PX)
            edge = table.col_top if (up) else table.col_bottom
            draw(rounded_path([(x, edge), (x, c)]))
                               # a stub going on to a deeper level
                               # carries a dot at this one
            if (abs(c - edge) < abs(self.stub_depth(segment.a) - edge)
                                - 0.5):
               out.append(f'<circle class="lvc" cx="{num(x)}" '
                          f'cy="{num(c)}" r="2.6"/>')
            (x_end, y_end) = end_of_filter(segment)
            length = math.hypot(x_end - x, y_end - c) or 1
            x_tip  = x + (x_end - x) / length * ARROW_GAP_PX
            y_tip  = c + (y_end - c) / length * ARROW_GAP_PX
            path   = rounded_path([(x_tip, y_tip), (x_end, y_end)])
            out.append(f'<path class="hit" d="{path}"/>')
            out.append(f'<path class="ln" d="{path}" '
                       'marker-start="url(#vqArrow)"/>')
            labels.append(((x_end + 4, y_end + 4), segment.op,
                           "flt", "start"))
            if (segment.a.join_mark):
               labels.append(((x - 4, c - 3), "(+)", "mark", "end"))
            continue
                               # same table: a U at its level, the
                               # operator on its flat part, nearer the
                               # left hand side's column
         (xa, xb) = (segment.a.x, segment.b.x)
                               # corners cut at 45 degrees
         dx = dy = min(CHAMFER_PX, abs(xb - xa) / 2,
                       c - table.col_bottom)
         s = 1 if (xb > xa) else -1
         draw(f"M{num(xa)},{num(table.col_bottom)} "
              f"L{num(xa)},{num(c - dy)} L{num(xa + s * dx)},{num(c)} "
              f"L{num(xb - s * dx)},{num(c)} L{num(xb)},{num(c - dy)} "
              f"L{num(xb)},{num(table.col_bottom)}")
         if (segment.op):
            x     = xa + (xb - xa) * 0.25
            width = text_px(segment.op, SMALL_PX) + 3
            self.ops_layer.append(
               f'<rect class="opbg" x="{num(x - width / 2)}" '
               f'y="{num(c - 6)}" width="{num(width)}" height="12"/>'
               f'<text class="op stub" x="{num(x)}" y="{num(c + 4)}" '
               f'text-anchor="middle">{esc(segment.op)}</text>')
      for ((x, y), text, kind, anchor) in labels:
         out.append(f'<text class="{kind}" x="{num(x)}" y="{num(y)}" '
                    f'text-anchor="{anchor}">{esc(text)}</text>')
      out.append("</g>")
      return("".join(out))

   ########################################################################

   def crossings(self, y, exclude):
      """
      post: returns the x of every join line or trunk, other than
            exclude, where it crosses the height y
      """
      found = []
      lines = ([s for s in self.layout.segments()
                if (s.kind == "join" and s.trunk is None)]
               + list(self.layout.trunks))
      for other in lines:
         if (other is exclude):
            continue
         (_, points) = self.join_path(other)
         for ((xa, ya), (xb, yb)) in zip(points, points[1:]):
            if (min(ya, yb) <= y <= max(ya, yb) and ya != yb):
               found.append(xa + (xb - xa) * (y - ya) / (yb - ya))
      return(found)

   ########################################################################

   def exists_svg(self, leaf, segment, points):
      """
      post: returns, for a line from an outer table into a [NOT] EXISTS
            subquery's box, that test's words where the line crosses
            into the box, blanking the line and border behind them
      """
      scope = self.scope_of.get(id(leaf))
      if (   (scope is None) or (not scope.boxed)
          or ("EXISTS" not in scope.context)
          or all(e.table.scope is scope for e in segment.ends())):
         return([])
      y = scope.box_top
      for ((xa, ya), (xb, yb)) in zip(points, points[1:]):
         if (min(ya, yb) <= y <= max(ya, yb) and ya != yb):
            x     = xa + (xb - xa) * (y - ya) / (yb - ya)
            text  = scope.context.replace(" subquery", "")
            width = text_px(text, SMALL_PX) + 4
                               # kept on the box's top edge, clear of
                               # its rounded corner; centred on its
                               # line, or else beside it, so it covers
                               # no other line entering there
            low   = scope.left - BOX_PAD_PX + 6 + width / 2
            others = self.crossings(y, segment)
            for at in (x, x - width / 2 + 1, x + width / 2 - 1):
               at = max(at, low)
               if (not any(at - width / 2 - 3 <= o <= at + width / 2 + 3
                           for o in others)):
                  x = at
                  break
            else:
               x = max(x, low)
            return([f'<rect class="opbg" x="{num(x - width / 2)}" '
                    f'y="{num(y - 6)}" width="{num(width)}" '
                    'height="12"/>'
                    f'<text class="op stub" x="{num(x)}" '
                    f'y="{num(y + 4)}" text-anchor="middle">'
                    f'{esc(text)}</text>'])
      return([])

   ########################################################################

   def end_depth(self, end):
      """
      post: returns the y where end's line leaves or ends on its stub:
            its bar for a bar member, else its level
      """
      layout = self.layout
      unit   = layout.unit_of.get(id(end))
      if (unit is None):
         return(None)
      what = "bar" if (end.segment.trunk is not None) else "c"
      return(level_y(end.table, end.side, unit.band, what))

   ########################################################################

   def stub_depth(self, end):
      """
      post: returns the y the stub of end's column reaches at end's
            side: the deepest of every line on it
      """
      ys = [y for y in (self.end_depth(e)
                        for e in end.column.slots[end.side]
                        if (abs(e.x - e.column.x) < 0.1))
            if (y is not None)]
      if (not ys):
         return(self.end_depth(end))
      return(min(ys) if (end.side == "up") else max(ys))

   ########################################################################

   def join_path(self, segment):
      """
      post: returns (svg path, operator label position) of a join
      imp: a line between non-adjacent tables runs straight through
           the tables between, at the x chosen by place_passes; it
           runs down its own stub, beside the column's other stubs
           when it has more
      """
      (upper, lower) = (segment.upper, segment.lower)
      if (self.layout.side_join(segment) and not segment.members):
         return(self.side_path(segment))
      if (segment.members):
                               # a trunk runs between bars
         (top, bottom) = segment.units
         points = [(upper.x, level_y(upper.table, "down", top.band,
                                     "bar")),
                   (upper.x, upper.table.bottom)]
         tail   = [(lower.x, lower.table.top),
                   (lower.x, level_y(lower.table, "up", bottom.band,
                                     "bar"))]
      else:
                               # each on its own stub (see place_slots)
         points = [(upper.x, upper.table.col_bottom),
                   (upper.x, upper.table.bottom)]
         tail   = [(lower.x, lower.table.top),
                   (lower.x, lower.table.col_top)]
      for (table, x) in segment.via:
         points.append((x, table.top))
         points.append((x, table.bottom))
      points += tail
      return(rounded_path(points), points)

   ########################################################################

   def side_path(self, segment):
      """
      post: returns (svg path, points) of a line from a host column
            down its stub to its track, across, and down into the
            column of the box beside it
      """
      (upper, lower) = (segment.upper, segment.lower)
      table    = upper.table
      (x0, x1) = (upper.x, lower.column.x)
      track    = self.layout.side_track(segment)
                               # down past its operator, then across
      points   = [(x0, table.col_bottom), (x0, track), (x1, track),
                  (x1, lower.table.col_top)]
      return(rounded_path(points), points)

   ########################################################################

   def op_svg(self, segment):
      """
      post: returns a join's operator, on its column's stub at its
            level and blanking the line behind it: at both ends for "="
            or "<>", which read the same from either side, else at the
            left hand side's end; bar members whose pairs cross also
            get their partner's name between the operator and the bar
      """
      out    = []
      layout = self.layout
      trunk  = segment.trunk
      for end in segment.ends():
         unit = layout.unit_of.get(id(end))
         if (unit is None):
            continue
         x = end.x
         y = band_y(end.table, end.side, unit.band)
         if ((trunk is not None) and trunk.crossed):
            other = segment.b if (end is segment.a) else segment.a
            py    = level_y(end.table, end.side, unit.band, "partner")
            out.append(f'<text class="ptn" x="{num(x)}" '
                       f'y="{num(py + 3)}" text-anchor="middle">'
                       f'{esc(other.column.name)}</text>')
         if (    end is not segment.a
             and segment.op not in ("=", "<>", "!=")
             and not end.join_mark):
                               # no operator at this end: a dot shows
                               # where its line meets the stub; not at
                               # the column an IN, ALL or ANY subquery
                               # returns, which the line runs into
            if (    trunk is None
                and segment.op.replace("NOT ", "") != "IN"
                and not segment.op.endswith((" ALL", " ANY"))):
               out.append(f'<circle class="lvc" cx="{num(x)}" '
                          f'cy="{num(y)}" r="2.6"/>')
            continue
         text = segment.op if (end is segment.a or
                               segment.op in ("=", "<>", "!=")) else ""
         if (text):
            width = text_px(text, SMALL_PX) + 3
            out.append(f'<rect class="opbg" x="{num(x - width / 2)}" '
                       f'y="{num(y - 6)}" width="{num(width)}" '
                       'height="12"/>'
                       f'<text class="op stub" x="{num(x)}" '
                       f'y="{num(y + 4)}" text-anchor="middle">'
                       f'{esc(text)}</text>')
         if (end.join_mark):
                               # (+) on the left under a column name,
                               # on the right above one
            gap = text_px(text, SMALL_PX) / 2 + 3 if (text) else 3
            if (end.side == "down"):
               (x, anchor) = (x - gap, "end")
            else:
               (x, anchor) = (x + gap, "start")
            out.append(f'<text class="mark" x="{num(x)}" '
                       f'y="{num(y + 4)}" text-anchor="{anchor}">(+)'
                       '</text>')
      return(out)

   ########################################################################

   def trunk_svg(self, trunk):
      """
      post: returns a trunk, grouped so hovering it highlights the
            joins it carries
      """
      leaf    = trunk.leaves[0]
      nodes   = " ".join(n.ident for n in leaf.ancestors)
      members = " ".join(l.ident for l in trunk.leaves)
      sql     = " AND ".join(l.sql for l in trunk.leaves)
      (path, points) = self.join_path(trunk)
      self.ops_layer += self.exists_svg(leaf, trunk, points)
      return(f'<g class="leaf trunk" data-leaf="T{id(trunk)}" '
             f'data-members="{members}" data-nodes="{nodes}" '
             f'data-sql="{esc(sql)}"><title>{esc(sql)}</title>'
             f'<path class="hit" d="{path}"/>'
             f'<path class="ln" d="{path}"/></g>')

   ########################################################################

   def bar_svg(self, bar):
      """
      post: returns the bar gathering the columns of a multi-column join
            at one table, from its leftmost column to its rightmost
      """
      (table, side, band, x0, x1, xs, trunk) = bar
      y       = level_y(table, side, band, "bar")
                               # flush with the outer edges of its end
                               # stubs, not their centres
      (x0, x1) = (x0 - STUB_HALF_PX, x1 + STUB_HALF_PX)
      leaf    = trunk.leaves[0]
      nodes   = " ".join(n.ident for n in leaf.ancestors)
      members = " ".join(l.ident for l in trunk.leaves)
      sql     = " AND ".join(l.sql for l in trunk.leaves)
                               # hovered like the trunk it ends
      return(f'<g class="leaf trunk" data-leaf="B{id(trunk)}" '
             f'data-members="{members}" data-nodes="{nodes}" '
             f'data-sql="{esc(sql)}"><title>{esc(sql)}</title>'
             f'<path class="hit" d="M{num(x0)},{num(y)} H{num(x1)}"/>'
             f'<line class="tbar" style="stroke:{BAR_COLOR}" '
             f'x1="{num(x0)}" y1="{num(y)}" '
             f'x2="{num(x1)}" y2="{num(y)}"/></g>')

   ########################################################################

   def mark_svg(self, layout, mark):
      """
      post: returns an enclosure, built like a bracket on each side: an
            upright from a little above its top term to a little below
            its bottom one, with a stub in at each end and at each term
            between; for AND (and NOT, with its word on the left) the
            upright is straight, for OR each stretch between stubs is
            bowed out into its own arc, so there is one arc per OR
      imp: an OR with one alternative at this side draws nothing here;
           its other alternatives are elsewhere, tied by a dotted link
      """
      kids   = sorted(mark.children, key = self.mark_level)
      ys     = [self.mark_level(child) for child in kids]
      (l, r) = (mark.left, mark.right)
      cls    = {"AND": "brk", "OR": "arc", "NOT": "brk not"}[mark.kind]
      parts  = []
                               # both run a little past the top and
                               # bottom terms (past the edge of an
                               # enclosure), so they enclose them; terms
                               # between are met at their level
      step = lambda k: 3 if (isinstance(k, Mark)) else 0
      ends = (  [self.mark_rows(kids[0])[0] - step(kids[0])]
              + ys[1:-1]
              + [self.mark_rows(kids[-1])[1] + step(kids[-1])])
      if (mark.kind == "OR"):
         for (ya, yb) in zip(ends, ends[1:]):
            if (len(ys) < 2):
               break
            ry = (yb - ya) / 2
            parts.append(f"M{num(l + 4)},{num(ya)} "
                         f"A4,{num(ry)} 0 0 0 {num(l + 4)},{num(yb)} "
                         f"M{num(r - 4)},{num(ya)} "
                         f"A4,{num(ry)} 0 0 1 {num(r - 4)},{num(yb)}")
      else:
         parts.append(f"M{num(l)},{num(ends[0])} V{num(ends[-1])} "
                      f"M{num(r)},{num(ends[0])} V{num(ends[-1])}")
         for y in ends:
            parts.append(f"M{num(l)},{num(y)} H{num(l + 4)} "
                         f"M{num(r)},{num(y)} H{num(r - 4)}")
      if (not parts):
         return("")
                               # grey dots join the corners, closing the
                               # enclosure into a box
      inset = 4 if (mark.kind == "OR") else 0
      box   = " ".join(f"M{num(l + inset)},{num(y)} H{num(r - inset)}"
                       for y in (ends[0], ends[-1]))
      return(f'<g class="bar mark" data-node="{mark.node.ident}" '
             f'data-sql="{esc(mark.kind + ": " + mark.node.sql)}" '
             f'style="--c:{mark.color}">'
             f'<title>{esc(mark.kind + ": " + shorten(mark.node.sql, 300))}'
             f'</title><path class="encl" d="{box}"/>'
             f'<path class="{cls}" d="{" ".join(parts)}"/>'
                               # NOT spelled out left of its bracket
             + ((f'<text class="notw" x="{num(l)}" '
                 f'y="{num(ends[0] - 3)}" text-anchor="start">NOT</text>'
                 if (mark.word_above)
                 else f'<text class="notw" x="{num(l - 3)}" '
                      f'y="{num((ends[0] + ends[-1]) / 2 + 3.5)}" '
                      'text-anchor="end">NOT</text>')
                if (mark.kind == "NOT") else "")
             + '</g>')

   ########################################################################

   def mark_level(self, element):
      """
      post: returns the level a stub or arc meets an element at: a bar
            or single line's own level, the middle of an enclosure
      """
      if (isinstance(element, Unit)):
         return(band_y(element.table, element.side, element.band))
      (top, bottom) = self.mark_rows(element)
      return((top + bottom) / 2)

   ########################################################################

   def mark_rows(self, element):
      """
      post: returns (top, bottom) an element is drawn over: a term's
            level inside its faint lines, an enclosure from its top
            end to its bottom end
      """
      if (isinstance(element, Unit)):
                               # its level, inset from the faint lines
         (a, b) = sorted(
            (level_y(element.table, element.side, element.band, "near"),
             level_y(element.table, element.side, element.band, "far")))
         return(a + 2, b - 2)
      kids  = sorted(element.children, key = self.mark_level)
      step  = lambda k: 3 if (isinstance(k, Mark)) else 0
      return(self.mark_rows(kids[0])[0] - step(kids[0]),
             self.mark_rows(kids[-1])[1] + step(kids[-1]))

   ########################################################################

   def node_svg(self, node):
      cls = f"bar {node.kind.lower()}"
      out = [f'<g class="{cls}" data-node="{node.ident}" '
             f'data-sql="{esc(node.kind + ": " + node.sql)}" '
             f'style="--c:{node.color}">'
             f'<title>{esc(node.kind + ": " + shorten(node.sql, 300))}'
             '</title>']
      for ((xa, ya), (xb, yb)) in node.links:
         out.append(f'<line class="link" x1="{num(xa)}" y1="{num(ya)}" '
                    f'x2="{num(xb)}" y2="{num(yb)}"/>'
                    f'<circle cx="{num(xa)}" cy="{num(ya)}" r="2.6"/>'
                    f'<circle cx="{num(xb)}" cy="{num(yb)}" r="2.6"/>')
      out.append("</g>")
      return("".join(out))

   ########################################################################

   def table_svg(self, table):
      out = [f'<g class="tbl {table.kind}">']
      if (table.virtual):
         top = table.name_y - 16
         out.append(f'<rect class="virt" x="{num(table.left - 7)}" '
                    f'y="{num(top)}" '
                    f'width="{num(table.right - table.left + 14)}" '
                    f'height="{num(table.col_bottom + 3 - top)}" '
                    'rx="3"><title>virtual table: shows the columns a '
                    'query block reads through this CTE / inline view'
                    '</title></rect>')
      out += [
             f'<text class="tname" x="{num(table.left)}" '
             f'y="{num(table.name_y)}">'
             + esc(table.name)
                               # the alias and "(view)" are not bold
             + (f' <tspan class="al">{esc(table.alias)}</tspan>'
                if (    table.alias
                    and table.alias.upper() != table.name.upper())
                else "")
             + (' <tspan class="vw">(view)</tspan>'
                if (table.db_view) else "")
             + '</text>',
             f'<line class="uline" x1="{num(table.left)}" '
             f'y1="{num(table.name_y + 5)}" '
             f'x2="{num(table.name_right)}" '
             f'y2="{num(table.name_y + 5)}"/>']
      for name in table.order:
         column = table.columns[name]
         half   = text_px(name) / 2
         vcol = " vcol" if (column.virtual or column.implied) else ""
         if (column.feeds is not None):
                               # an arrow back into the aggregate on
                               # its left that also reads it
            into = column.feeds
            x0   = column.x - half - 4
            x1   = into.x + text_px(into.name) / 2 + 4
            y    = table.col_y - 4.5
            out.append(f'<path class="ln agg" d="M{num(x0)},{num(y)} '
                       f'H{num(x1)}" marker-end="url(#vqAgg)"/>')
         if (column.after is not None):
                               # an arrow from the column it is made
                               # from (or the aggregate before it)
            x0 = column.after.x + text_px(column.after.name) / 2 + 4
                               # running under its ORDER BY position
                               # and check up to the name
            x1 = column.x - half - 4
            y  = table.col_y - 4.5
            out.append(f'<path class="ln agg" d="M{num(x0)},{num(y)} '
                       f'H{num(x1)}" marker-end="url(#vqAgg)"/>')
         out.append(f'<text class="col{vcol}" x="{num(column.x)}" '
                    f'y="{num(table.col_y)}" text-anchor="middle">'
                    f'{esc(column.name)}</text>')
                               # top left, right to left: the check,
                               # then the ORDER BY position before it
         corner = column.x - half - 1
         if (column.returned):
                               # drawn, not a font's glyph, so its
                               # legs can be set: the vertex and long
                               # leg are fixed; the short leg is
                               # CHECK_SHORT_PX long, leaning
                               # CHECK_LEAN_DEG off the vertical
            (x, y)   = (corner - 0.5, table.col_y - 8)
            (vx, vy) = (x - 3.7, y - 0.8)
            lean     = math.radians(CHECK_LEAN_DEG)
            out.append(f'<path class="chk" '
                       f'd="M{num(vx - CHECK_SHORT_PX * math.sin(lean))},'
                       f'{num(vy - CHECK_SHORT_PX * math.cos(lean))} '
                       f'L{num(vx)},{num(vy)} '
                       f'L{num(x + 0.4)},{num(y - 6.6)}"/>')
            corner -= CHECK_PX
         if (column.order_desc):
                               # a down arrow between the position and
                               # the check
            (x, y) = (corner - 3, table.col_y - 8)
            out.append(f'<path class="dsc" d="M{num(x)},{num(y - 9)} '
                       f'V{num(y - 1)} M{num(x - 2.6)},{num(y - 3.6)} '
                       f'L{num(x)},{num(y - 0.6)} '
                       f'L{num(x + 2.6)},{num(y - 3.6)}">'
                       '<title>ORDER BY ... DESC</title></path>')
            corner -= DESC_PX
         if (column.order_by is not None):
            out.append(f'<text class="ord" x="{num(corner)}" '
                       f'y="{num(table.col_y - 8)}" text-anchor="end">'
                       f'<title>ORDER BY position {column.order_by}'
                       f'</title>{column.order_by}</text>')
         if (column.marker):
                               # a group with a clear square around the
                               # star, so hovering near it is enough
            subject = f"{table.alias or table.name}.{column.name}"
            data    = json.dumps([subject, column.marker_index])
            out.append(f'<g class="mrkhit" data-index="{esc(data)}">'
                       f'<rect x="{num(column.x + half - 3)}" '
                       f'y="{num(table.col_y - 24)}" width="18" '
                       f'height="20"/>'
                       f'<text class="mrk {column.marker_class}" '
                       f'x="{num(column.x + half + 1)}" '
                       f'y="{num(table.col_y - 8)}">'
                       f'{esc(column.marker)}</text></g>')
      out.append("</g>")
      return("".join(out))

###########################################################################

SVG_STYLE = """
text { font-family: Consolas, "DejaVu Sans Mono", Menlo, monospace;
       font-size: 13px; fill: #1f2937;
       paint-order: stroke; stroke: #fff; stroke-width: 4px;
       stroke-linejoin: round; }
.panel { fill: #fff; stroke: #d1d5db; }
.box { fill: none; stroke: #94a3b8; stroke-width: 1.2; }
.box.cte { stroke: #7c3aed; }
.btitle { font-size: 11px; fill: #64748b; stroke-width: 3px; }
.sep { font-size: 13px; font-weight: bold; fill: #1f2937; }
.stmt { font-size: 14px; font-weight: bold; stroke-width: 0; }
.tname { font-size: 14px; font-weight: bold; stroke-width: 0; }
.tbl.unknown .tname, .tbl.\\? .tname
   { fill: #6b7280; font-style: italic; }
.tbl.view .tname, .tbl.cte .tname { fill: #7c3aed; }
.tname .vw { font-weight: normal; fill: #6b7280; }
.tname .al { font-weight: normal; }
.uline { stroke: #1f2937; stroke-width: 1.2; }
.col { font-size: 13px; }
.chk { fill: none; stroke: #059669; stroke-width: 1.3;
       stroke-linecap: round; stroke-linejoin: round; }
.mrk { fill: #b45309; font-size: 14px; font-weight: bold;
       stroke-width: 2px; }
.mrk.lead { fill: #059669; }
.mrkhit { cursor: default; }
.mrkhit rect { fill: transparent; stroke: none; }
.mrkhit:hover .mrk { text-decoration: underline; }
.dsc { fill: none; stroke: #2563eb; stroke-width: 1.3;
       stroke-linecap: round; stroke-linejoin: round; }
.vcol { font-style: italic; }
.ord { fill: #2563eb; font-size: 11px; font-weight: bold;
       stroke-width: 2px; }
.ln { fill: none; stroke-linejoin: round; stroke: #4b5563;
      stroke-width: 1.1; }
.tbar { stroke-width: 2.6; }
.lvl { stroke: #e5e7eb; stroke-width: 1; }
.lvc { fill: #374151; }
.ptn { font-size: 8px; fill: #9ca3af; stroke-width: 3px; }
.ln.feed { stroke: #7c3aed; }
.virt { fill: none; stroke: #7c3aed; stroke-width: 1.2;
        stroke-dasharray: 1.5 3; stroke-linecap: round; }
.hit { fill: none; stroke: transparent; stroke-width: 9; }
.end { fill: #4b5563; }
.op { font-size: 13px; font-weight: bold; fill: #b91c1c; }
.op.stub { font-size: 11px; stroke-width: 0; }
.brk, .arc { fill: none; stroke: var(--c); stroke-width: 1.6; }
.opbg { fill: #fff; }
.encl { fill: none; stroke: #9ca3af; stroke-width: 1.3;
        stroke-dasharray: 0.1 4; stroke-linecap: round; }
.notw { font-size: 10px; font-weight: bold; fill: var(--c);
        stroke-width: 3px; }
.flt { font-size: 11px; fill: #374151; }
.mark { font-size: 10px; font-weight: bold; fill: #b91c1c;
        stroke-width: 3px; }
.note { font-size: 11px; fill: #6b7280; stroke-width: 0; }
.bar line { stroke: var(--c); stroke-width: 1.6; }
.bar line.link { stroke-width: 2; stroke-dasharray: 0.1 5;
                 stroke-linecap: round; }
.bar circle { fill: var(--c); }
.bar circle.member { fill: #fff; stroke: var(--c); stroke-width: 1.4; }
svg.dim g.leaf:not(.hl), svg.dim g.bar:not(.hl) { opacity: .15; }
g.leaf.hl .ln { stroke: #111827; stroke-width: 1.9; }
"""

###########################################################################

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@TITLE@@</title>
<style>
body { margin: 0; font-family: system-ui, sans-serif; color: #1f2937;
       background: #f3f4f6; }
header { padding: 10px 16px; background: #1f2937; color: #f9fafb;
         display: flex; gap: 16px; align-items: center;
         position: sticky; top: 0; z-index: 2; }
header h1 { font-size: 15px; margin: 0; font-weight: 600; }
header .tools { margin-left: auto; display: flex; gap: 10px;
                align-items: center; font-size: 13px; }
button { font: inherit; padding: 3px 10px; cursor: pointer; }
#infoslot { position: sticky; top: 44px; z-index: 1; height: 29px; }
#info { position: absolute; left: 0; right: 0; top: 0;
        min-height: 16px; line-height: 16px;
        padding: 6px 16px; background: #fffbeb; font: 12px monospace;
        border-bottom: 1px solid #fde68a; white-space: pre-wrap; }
#canvas { padding: 12px; overflow: auto; }
#tip { display: none; position: fixed; z-index: 5; pointer-events: none;
       background: #fff; border: 1px solid #9ca3af; border-radius: 3px;
       box-shadow: 0 2px 6px rgba(0, 0, 0, .2); padding: 4px 8px;
       align-items: flex-start;
       font: 12px monospace; color: #1f2937; white-space: nowrap; }
#tip .ihead { color: #374151; text-decoration: underline; }
#tip .iblock + .iblock { margin-left: 10px; padding-left: 10px;
                         border-left: 1px solid #e5e7eb; }
#tip .icol { display: block; }
details { margin: 0 16px 16px; }
pre { background: #fff; padding: 10px; border: 1px solid #d1d5db; }
.legend { font-size: 12px; margin: 8px 16px 0; color: #4b5563; }
.legend summary { cursor: pointer; }
.legend ul { margin: 4px 0 0; padding-left: 20px; }
</style>
</head>
<body>
<header>
  <h1>@@TITLE@@</h1>
  <div class="tools">
    zoom <input id="zoom" type="range" min="40" max="250" value="100">
    <button id="svgbtn">SVG</button>
    <button id="pngbtn">PNG</button>
  </div>
</header>
<div id="infoslot"><div id="info"></div></div>
<div id="tip"></div>
<details class="legend"><summary>Legend</summary><ul>
<li>&#10003; returned column; 1 2 3 its position in the ORDER BY</li>
<li>with a database login, join columns are marked &#9733; a leading
  column of an index (every index column before it is also used by
  the query), &#9734; in an index but not a leading column; an
  unmarked join column is in no index; hover a mark for the
  indexes, with the columns the query uses in bold; a table the
  database says is a view shows "(view)"</li>
<li>every column in a condition has a stub; conditions on one
  level (between the faint lines) are AND'ed, on different levels
  OR'ed</li>
<li>a stub ends at its condition's level, where a value's arrow points;
  a filled dot marks another level the column takes part in, or where
  a line meets the stub with no operator there</li>
<li>operators sit on the stub at their level: "=" at both ends, others
  at the left hand side's end; (+) marks an outer join; a condition
  between two columns of one table is a U, its operator nearer the
  left hand side</li>
<li>a thick bar gathers two or more columns joined to the same other
  table, so one connector runs between the tables; grey names between
  operator and bar give each column's partner when the pairs cross; a
  line that only passes a bar hops over it</li>
<li>where parentheses nest, [ ] encloses what is AND'ed, ( ) what
  is OR'ed (one arc per OR), and the word NOT on the left of a [ ]
  negates what it encloses</li>
<li>a coloured dotted line joins OR'ed alternatives that never meet
  at one table</li>
<li>a line into an EXISTS or NOT EXISTS subquery's box carries those
  words where it enters the box</li>
<li>boxes: subqueries (indented), CTEs (outdented); a dotted box
  marks a CTE / inline view drawn as a virtual table, and purple
  arrows show which column inside it feeds each column read through
  it</li>
</ul></details>
<div id="canvas">@@SVG@@</div>
<details><summary>SQL source</summary><pre>@@SQL@@</pre></details>
<script>
(function () {
  const svg  = document.querySelector("svg.vq");
  const info = document.getElementById("info");
  const w0   = svg.width.baseVal.value, h0 = svg.height.baseVal.value;
  function clear() {
    svg.classList.remove("dim");
    svg.querySelectorAll(".hl").forEach(e => e.classList.remove("hl"));
    info.textContent = "";
  }
  svg.querySelectorAll("g.leaf").forEach(g => {
    g.addEventListener("mouseenter", () => {
      svg.classList.add("dim");
      g.classList.add("hl");
      g.dataset.nodes.split(" ").filter(Boolean).forEach(n =>
        svg.querySelectorAll('g.bar[data-node="' + n + '"]')
           .forEach(b => b.classList.add("hl")));
      const id = g.dataset.leaf;
      svg.querySelectorAll("g.trunk").forEach(b => {
        if ((b.dataset.members || "").split(" ").includes(id))
          b.classList.add("hl");
      });
      (g.dataset.members || "").split(" ").filter(Boolean).forEach(m =>
        svg.querySelectorAll('g.leaf[data-leaf="' + m + '"]')
           .forEach(l => l.classList.add("hl")));
      info.textContent = g.dataset.sql;
    });
    g.addEventListener("mouseleave", clear);
  });
  // index marks: a popup by the pointer, like a tooltip, listing each
  // index holding the column and its columns, those the query uses
  // in bold
  const tip = document.getElementById("tip");
  function place(e) {
    const x = Math.min(e.clientX + 14,
                       window.innerWidth - tip.offsetWidth - 4);
    const y = (e.clientY + 18 + tip.offsetHeight < window.innerHeight)
            ? e.clientY + 18 : e.clientY - tip.offsetHeight - 8;
    tip.style.left = x + "px";
    tip.style.top  = y + "px";
  }
  svg.querySelectorAll("g.mrkhit").forEach(g => {
    g.addEventListener("mouseenter", e => {
      const indexes = JSON.parse(g.dataset.index)[1];
      tip.textContent = "";
      // one block per index, side by side
      indexes.forEach(([name, lead, cols]) => {
        const block = document.createElement("div");
        const head  = document.createElement("div");
        block.className  = "iblock";
        head.className   = "ihead";
        head.textContent = name;
        block.append(head);
        cols.forEach(([col, used]) => {
          const row = document.createElement(used ? "b" : "div");
          row.className   = "icol";
          row.textContent = col;
          block.append(row);
        });
        tip.append(block);
      });
      tip.style.display = "flex";
      place(e);
    });
    g.addEventListener("mousemove", place);
    g.addEventListener("mouseleave", () => tip.style.display = "none");
  });
  svg.querySelectorAll("g.bar").forEach(g => {
    g.addEventListener("mouseenter", () => {
      const n = g.dataset.node;
      svg.classList.add("dim");
      g.classList.add("hl");
      svg.querySelectorAll("g.leaf").forEach(l => {
        if (l.dataset.nodes.split(" ").includes(n))
          l.classList.add("hl");
      });
      info.textContent = g.dataset.sql;
    });
    g.addEventListener("mouseleave", clear);
  });
  document.getElementById("zoom").addEventListener("input", e => {
    const z = e.target.value / 100;
    svg.setAttribute("width",  w0 * z);
    svg.setAttribute("height", h0 * z);
  });
  function svgText() {
    clear();
    const copy = svg.cloneNode(true);
    copy.setAttribute("width", w0);
    copy.setAttribute("height", h0);
    return new XMLSerializer().serializeToString(copy);
  }
  function save(blob, name) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  document.getElementById("svgbtn").onclick = () =>
    save(new Blob([svgText()], {type: "image/svg+xml"}),
         "@@BASE@@.svg");
  document.getElementById("pngbtn").onclick = () => {
    const img = new Image();
    img.onload = () => {
      const c = document.createElement("canvas");
      c.width = w0 * 2; c.height = h0 * 2;
      const x = c.getContext("2d");
      x.fillStyle = "#fff"; x.fillRect(0, 0, c.width, c.height);
      x.scale(2, 2); x.drawImage(img, 0, 0);
      c.toBlob(b => save(b, "@@BASE@@.png"));
    };
    img.src = "data:image/svg+xml;charset=utf-8,"
            + encodeURIComponent(svgText());
  };
})();
</script>
</body>
</html>
"""

###########################################################################

def render_html(model, title, base, sql_text):
   page = HTML_PAGE
   for (key, value) in (("@@SVG@@"  , SvgRenderer(model).render()),
                        ("@@TITLE@@", esc(title)),
                        ("@@BASE@@" , esc(base)),
                        ("@@SQL@@"  , esc(sql_text))):
      page = page.replace(key, value)
   return(page)

###########################################################################
###
### o r a c l e    c o n n e c t i o n
###
###########################################################################

WINDOWS_P = (platform.system() == "Windows")

###########################################################################

def oracle_home(dbn):
   """
   use: ORACLE_HOME from the windows registry, or from oratab: the dbn
        entry, else "*", else the first entry that is not +ASM/+APX
   """
   if (WINDOWS_P):
      import winreg
      try:
         hive = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"SOFTWARE\ORACLE")
      except OSError:
         return("")
      index = 0
      while (True):
         try:
            name = winreg.EnumKey(hive, index)
         except OSError:
            return("")
         index += 1
         try:
            with winreg.OpenKey(hive, name) as key:
               home = winreg.QueryValueEx(key, "ORACLE_HOME")[0]
         except OSError:
            continue
         if (Path(home, "bin", "sqlplus.exe").is_file()):
            return(home)

   entries = []
   for oratab in (Path.home() / "etc/oratab",
                  Path("/var/opt/oracle/oratab"), Path("/etc/oratab")):
      if (oratab.is_file()):
         entries = [line.split(":")[0:2]
                    for line in oratab.read_text().splitlines()
                    if (re.match(r"\s*[^#\s][^:]*:[^:]+", line))]
         break
   for wanted in (lambda sid: sid.lower() == dbn.lower(),
                  lambda sid: sid == "*",
                  lambda sid: sid[0] not in "+-*"):
      for sid, home in entries:
         if (wanted(sid.strip())):
            return(home.strip())
   return("")

###########################################################################

def tns_admin(home):
   """
   use: getdbhome -t: first directory holding sqlnet.ora, switched to a
        seps_wallet, wallet, or seps_admin directory with its own
        sqlnet.ora (the one with WALLET_LOCATION) if there is one
   """
   for this in (Path.home() / "etc/network/admin",
                Path.home() / "etc/oracle", Path.home() / "etc",
                Path(home, "network", "admin"),
                Path("/var/opt/oracle"), Path("/etc")):
      if (this.joinpath("sqlnet.ora").is_file()):
         for seps in (this / "seps_wallet", this / "wallet",
                      this.parent / "seps_admin",
                      this.parent / "seps_wallet", this.parent / "wallet"):
            if (seps.joinpath("sqlnet.ora").is_file()):
               return(str(seps))
         return(str(this))
   return("")

###########################################################################

def check_mode(path):
   """
   use: credential/wallet directories must be 700 and files 600, as
        getconnection checks; windows has no such modes
   """
   if ((not WINDOWS_P) and
       (path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))):
      raise(VqError("permissions too lax; should be mode " +
                    ("700" if (path.is_dir()) else "600") + f": {path}"))

###########################################################################

def parse_connection(text):
   """
   use: PARSECONNECTION: user/password@db, user@db, /@db, or user/@db
   post: returns usr, psw, dbn, kind; kind is ".." (traditional),
         "/" (/ as sysdba), or "/@" (SEPS)
   """
   rest, _, dbn = text.rpartition("@")
   dbn          = dbn.split()[0] if (dbn.split()) else ""
   usr, slash, psw = re.sub(r"\s", "", rest).partition("/")
   if   (not slash): kind = ".."
   elif (not usr  ): kind = "/"
   elif (not psw  ): kind = "/@"
   else:             kind = ".."
   return(usr, psw, dbn, kind)

###########################################################################

def oracle_connection(text):
   """
   use: resolves a connection for sqlplus like getconnection
   post: returns connect string, environment (None means unset it),
         and the path to sqlplus
   imp: a missing password comes from the first existing directory of
        ~/.credential, ~/etc/.credential, ~/etc/credential, file
        user@db, whose last non-comment line is a full connection;
        otherwise the user is prompted
   """
   usr, psw, dbn, kind = parse_connection(text)
   if ((not dbn) or ((not usr) and (kind != "/"))):
      raise(VqError("missing " + ("database name" if (not dbn)
                                  else "username") + " for connection"))

   if ((kind == "..") and (not psw)):
      for this in (".credential", "etc/.credential", "etc/credential"):
         directory = Path.home() / this
         if (directory.is_dir()):
            credential = directory / f"{usr}@{dbn}"
            if (credential.is_file()):
               check_mode(directory)
               check_mode(credential)
               lines = [line.strip()
                        for line in credential.read_text().splitlines()
                        if (re.match(r"\s*[^\s#;]", line))]
               if (lines):
                  usr, psw, dbn, kind = parse_connection(lines[-1])
            break
      if (not psw):
         psw = getpass.getpass(f"{usr}@{dbn} password: ").strip()

   if (kind == "/@"):
      wallet = Path.home() / ".seps_wallet" / usr / "ewallet.p12"
      if (not wallet.is_file()):
         raise(VqError(f"no SEPS wallet for {usr}"))
      check_mode(wallet.parent.parent)
      check_mode(wallet)

   home = oracle_home(dbn)
   if (not home):
      raise(VqError("unable to determine ORACLE_HOME"))
   sqlplus = Path(home, "bin", "sqlplus.exe" if (WINDOWS_P) else "sqlplus")
                               # SEPS's sqlnet.ora breaks "/ as sysdba"
   admin   = tns_admin(home) if (kind != "/") else ""

   role = ""
   if (usr.lower() in ("", "sys")):
      role = (" as sysasm" if (re.fullmatch(r"\+?ASM\d{0,2}", dbn, re.I))
              else " as sysdba")
   if   (kind == "/" ): connect = "/" + role
   elif (kind == "/@"): connect = f"/@{dbn}" + role
   else:
      if (re.search(r"[/@]", psw) and (not psw.startswith('"'))):
         psw = f'"{psw}"'
      connect = f"{usr}/{psw}@{dbn}" + role

   environment = {"ORACLE_HOME": home,
                  "ORACLE_SID" : dbn   if (kind == "/" ) else None,
                  "ORACLE_USER": usr   if (kind == "/@") else None,
                  "TNS_ADMIN"  : admin if (admin       ) else None}
   return(connect, environment, str(sqlplus))

###########################################################################

def run_sqlplus(connection, sql):
   """
   use: runs sql through sqlplus
   usage: connection is what oracle_connection() returned, so the
          password is asked for only once
   post: returns sqlplus's output lines; raises VqError on failure
   imp: connect goes in on stdin, so the password is not visible in ps
   """
   (connect, environment, sqlplus) = connection
   env = dict(os.environ)
   for name, value in environment.items():
      if   (value is not None): env[name] = value
      elif (name in env      ): del env[name]

   script = ("whenever sqlerror exit failure\n" +
             f"connect {connect}\n" +
             "set define off long 2000000000 longchunksize 32767"
             " linesize 32767 pagesize 0\n"
             "set heading off feedback off verify off tab off"
             " trimout on\n" +
             f"{sql.rstrip()}\nexit\n")
   done   = subprocess.run([sqlplus, "-L", "-S", "/nolog"],
                           input          = script,
                           env            = env,
                           capture_output = True,
                           text           = True)
   lines = (done.stdout + done.stderr).splitlines()
   if (done.returncode != 0):
      raise(VqError("\n".join(lines) or "sqlplus failed"))
   return(lines)

###########################################################################

                               # where a statement's text can be found,
                               # in order: shared pool, AWR (needs the
                               # diagnostics pack), sql tuning sets
SQL_SOURCES = (
   ("GV$SQL",
    "select SQL_FULLTEXT, PARSING_SCHEMA_NAME from GV$SQL"
    " where SQL_ID = :1 and ROWNUM = 1"),
   ("GV$SQLSTATS",
    "select SQL_FULLTEXT, null from GV$SQLSTATS"
    " where SQL_ID = :1 and ROWNUM = 1"),
   ("DBA_HIST_SQLTEXT",
    "select T.SQL_TEXT, (select max(S.PARSING_SCHEMA_NAME)"
    " from DBA_HIST_SQLSTAT S where S.SQL_ID = T.SQL_ID)"
    " from DBA_HIST_SQLTEXT T where T.SQL_ID = :1 and ROWNUM = 1"),
   ("DBA_SQLSET_STATEMENTS",
    "select SQL_TEXT, PARSING_SCHEMA_NAME from DBA_SQLSET_STATEMENTS"
    " where SQL_ID = :1 and ROWNUM = 1"))

def fetch_sql(connection, sqlid):
   """
   use: finds the text of the statement with this SQLID
   post: returns (text, where it was found, parsing schema or None)
   imp: each source is tried in its own execute immediate, so a view
        that is missing or not granted is just skipped; the text is
        taken from the first source that has it, the parsing schema
        from the first that knows it
   """
   if (not re.fullmatch(r"[0-9a-z]{13}", sqlid)):
      raise(VqError(f"invalid SQLID {sqlid}"))
   tries = "\n".join(f"   TRY('{name}', '{query}');"
                     for (name, query) in SQL_SOURCES)
   lines = run_sqlplus(connection, f"""
variable RESULT clob
declare
   sqlText    clob;
   sqlSource  varchar2(30);
   sqlSchema  varchar2(128);

   procedure TRY(source in varchar2, query in varchar2) is
      thisText   clob;
      thisSchema varchar2(128);
   begin
      if ((sqlText is null) or (sqlSchema is null)) then
         execute immediate query into thisText, thisSchema
            using '{sqlid}';
         if (sqlText is null) then
            sqlText   := thisText;
            sqlSource := source;
         end if;
         sqlSchema := nvl(sqlSchema, thisSchema);
      end if;
   exception
      when others then
         null;
   end TRY;
begin
{tries}
   if (sqlText is not null) then
      :RESULT := sqlSource || chr(10) || sqlSchema || chr(10) || sqlText;
   end if;
end;
/
print RESULT""")
   while ((lines) and (not lines[0].strip())):
      lines.pop(0)
   if (len(lines) < 3):
      raise(VqError(f"SQLID {sqlid} not found in any of " +
                    ", ".join(name for (name, query) in SQL_SOURCES)))
   return("\n".join(lines[2:]).strip(), lines[0].strip(),
          lines[1].strip() or None)

###########################################################################

def fetch_indexes(connection, tables, schema = None):
   """
   use: reads the index columns of each (owner, name) in tables
   usage: owner is None for a table not qualified in the query; it is
          then looked for the way oracle resolves it: in schema (the
          parsing schema, or the session's current schema if None),
          then as a public synonym
   post: returns ({(owner, name): {index: [columns]}} for each table
         found ({} if it has no indexes), {(owner, name)} of those that
         are views); names not found (no access) are in neither
   imp: invisible and unusable indexes are left out, since the
        optimizer cannot use them
   """
   def quote(value):
      return("cast(null as varchar2(128))" if (value is None)
             else "'" + value.replace("'", "''") + "'")

   rows   = "\n           union all\n".join(
               f"           select {quote(owner)} QOWNER,"
               f" {quote(name)} QNAME from DUAL"
               for (owner, name) in tables).lstrip()
   home   = (quote(schema) if (schema)
             else "sys_context('USERENV', 'CURRENT_SCHEMA')")
   lines  = run_sqlplus(connection, f"""
with Q as ({rows}),
     X as (select OWNER, TABLE_NAME NAME, OWNER TOWNER, TABLE_NAME TNAME
              from ALL_TABLES
           union all
           select OWNER, VIEW_NAME, OWNER, VIEW_NAME
              from ALL_VIEWS
           union all
           select OWNER, SYNONYM_NAME, TABLE_OWNER, TABLE_NAME
              from ALL_SYNONYMS),
     R as (select QOWNER, QNAME, TOWNER, TNAME
              from (select Q.QOWNER, Q.QNAME, X.TOWNER, X.TNAME,
                           row_number() over (
                              partition by Q.QOWNER, Q.QNAME
                              order by decode(X.OWNER, 'PUBLIC', 2, 1)) RN
                       from Q, X
                       where X.NAME  = Q.QNAME and
                             X.OWNER in (nvl(Q.QOWNER, {home}),
                                         nvl2(Q.QOWNER, null, 'PUBLIC')))
              where RN = 1)
select R.QOWNER          || '|' || R.QNAME        || '|' ||
       C.INDEX_OWNER     || '.' || C.INDEX_NAME   || '|' ||
       C.COLUMN_POSITION || '|' || C.COLUMN_NAME  || '|' ||
       I.VISIBILITY      || '|' || I.STATUS       || '|' ||
       (select 'VIEW'
           from ALL_VIEWS V
           where V.OWNER     = R.TOWNER and
                 V.VIEW_NAME = R.TNAME)
   from R, ALL_IND_COLUMNS C, ALL_INDEXES I
   where C.TABLE_OWNER (+) = R.TOWNER     and
         C.TABLE_NAME  (+) = R.TNAME      and
         I.OWNER       (+) = C.INDEX_OWNER and
         I.INDEX_NAME  (+) = C.INDEX_NAME;""")
   result = {}
   views  = set()
   for line in lines:
      fields = line.strip().split("|")
      if (len(fields) != 8):
         continue
      (owner, name, index, position, column, visible, status,
       kind) = fields
      if (kind == "VIEW"):
         views.add((owner or None, name))
         continue
      indexes = result.setdefault((owner or None, name), {})
      if ((index != ".") and
          (visible != "INVISIBLE") and (status != "UNUSABLE")):
         columns = indexes.setdefault(index, {})
         columns[int(position)] = column
   return({table: {index: [columns[p] for p in sorted(columns)]
                   for (index, columns) in indexes.items()}
           for (table, indexes) in result.items()}, views)

###########################################################################

def mark_indexes(model, connection, schema = None):
   """
   use: marks each join column by how an index can serve it:
           lead  the column is in the leading columns of an index: every
                 index column before it is also used by a predicate on
                 that table in the query
           part  the column is in an index, but not in its leading
                 columns
        a column in no index is left unmarked
        tables that turn out to be views are flagged, to be shown so
   post: returns the (owner, name) pairs that were not found
   """
   tables = {}
   for diagram in model.diagrams:
                               # items are ("table", table), or
                               # ("open", ...), ("sep", ...) and so on
      for item in diagram.items:
         if ((item[0] == "table") and (item[1].kind == "table")):
            tables.setdefault((item[1].db_owner, item[1].db_name),
                              []).append((diagram, item[1]))
   if (not tables):
      return([])
   (found, views) = fetch_indexes(connection, list(tables), schema)
   for pair in views:
      for (diagram, table) in tables.get(pair, ()):
         table.db_view = True

   for diagram in model.diagrams:
      used   = {}
      joined = {}
      for leaf in (leaf for scope in diagram.scopes
                        for leaf in scope.leaves):
         for segment in leaf.segments:
            for end in segment.ends():
               used.setdefault(id(end.table), set()).add(end.column_key)
               if (segment.kind == "join"):
                  joined.setdefault(id(end.table),
                                    set()).add(end.column_key)
      for (pair, refs) in tables.items():
         if (pair not in found):
            continue
         for (this_diagram, table) in refs:
            if (this_diagram is not diagram):
               continue
            for key in joined.get(id(table), ()):
               holding = []
               for (index, columns) in sorted(found[pair].items()):
                  if (key in columns):
                     before = columns[:columns.index(key)]
                     holding.append(
                        [index, set(before) <= used[id(table)],
                         [(c, c in used[id(table)]) for c in columns]])
                               # leading ones first
               holding.sort(key = lambda h: not h[1])
               column = table.columns[key]
               column.marker_index = holding
               (column.marker, column.marker_class) = (
                    (STAR_FULL, "lead") if (any(h[1] for h in holding))
                  else (STAR_OPEN, "part") if (holding)
                  else (None, ""))
   return([pair for pair in tables
           if ((pair not in found) and (pair not in views))])

###########################################################################
###
### c o m m a n d    l i n e
###
###########################################################################

class MAIN:
   """
   use: command line driver
   """
   def __init__(self, exec_path):
      self.self    = os.path.basename(exec_path)
      self.output  = None
      self.format  = "html"
      self.verbose = 0

   ########################################################################

   def usage(self):
      print(f"{self.self} {REVISION}\n"
            f"usage: {self.self} [options] [file.sql]"
            " [user/password@database]\n"
            "where options are:\n"
            "   -oOutput output file; default is file.sql (or SQLID)\n"
            "            with its extension replaced by the format\n"
            f"   -FFormat output format: {', '.join(FORMATS)}"
            " (default html)\n"
            "   -fFile   sql file; needed only if its name has an @\n"
            "   -qSQLID  read the statement with this SQLID from the\n"
            "            database; needs a login, not a file\n"
            "   -v       verbose; more -v for more verbosity\n"
            "   -h       help\n"
            "an argument with an @ is a database login (user/password@db,\n"
            "user@db, /@db or user/@db), any other argument is a file;\n"
            "with a login, join columns are marked by how indexes serve"
            " them",
            file = sys.stderr)

   ########################################################################

   def execute(self, argv):
      try:
         (opts, args) = getopt.gnu_getopt(argv, "o:F:f:q:vh")
      except getopt.GetoptError as error:
         raise(VqError(str(error), self.self, E_USAGE))
      files  = []
      logins = []
      sqlid  = None
      for (option, value) in opts:
         if   (option == "-o"):
            self.output = value
         elif (option == "-F"):
            self.format = value.lower()
         elif (option == "-f"):
            files.append(value)
         elif (option == "-q"):
            sqlid = value.strip().lower()
         elif (option == "-v"):
            self.verbose += 1
         elif (option == "-h"):
            self.usage()
            return(E_SUCCESS)
      for arg in args:
         (logins if ("@" in arg) else files).append(arg)

      if ((len(files) > 1) or (len(logins) > 1)):
         raise(VqError("only one file and one login may be given",
                       self.self, E_USAGE))
      source = files[0]  if (files ) else None
      login  = logins[0] if (logins) else None
      if   ((sqlid) and (source)):
         raise(VqError("-q and a file are mutually exclusive",
                       self.self, E_USAGE))
      elif ((sqlid) and (not login)):
         raise(VqError("-q needs a database login", self.self, E_USAGE))
      elif ((login) and (not sqlid) and (not source)):
         raise(VqError("a database login needs -q", self.self, E_USAGE))
      elif ((not sqlid) and (not source)):
         self.usage()
         return(E_USAGE)
      if (self.format not in FORMATS):
         raise(VqError(f"unsupported format {self.format}",
                       self.self, E_USAGE))

      connection = None
      schema     = None
      try:
         if (login):
            connection = oracle_connection(login)
         if (sqlid):
            (sql_text, where, schema) = fetch_sql(connection, sqlid)
            if (self.verbose):
               print(f"{self.self}: {sqlid} from {where}"
                     f" parsed as {schema or 'unknown schema'}",
                     file = sys.stderr)
            base = sqlid
      except VqError as error:
                               # str(error) keeps the case of ORA-
                               # and SP2- errors
         raise(VqError(str(error), self.self))
      if (not sqlid):
         try:
            with open(source, encoding = "utf-8") as handle:
               sql_text = handle.read()
         except OSError as error:
            raise(VqError(f"cannot read {source}: {error.strerror}",
                          self.self))
         base = os.path.splitext(os.path.basename(source))[0]

      try:
         model = QueryModel(sql_text)
      except VqError as error:
                               # only its first letter lowered: the
                               # sql it quotes keeps its case
         message = error.message
         raise(VqError(message[:1].lower() + message[1:], self.self))
      if (connection):
         try:
            missing = mark_indexes(model, connection, schema)
         except VqError as error:
            raise(VqError(str(error), self.self))
         for (owner, name) in missing:
            print(f"{self.self}: no index details for"
                  f" {owner + '.' if (owner) else ''}{name}"
                  " (not found, or not visible to this login)",
                  file = sys.stderr)
      output = (   self.output
                or (os.path.splitext(source)[0] if (source) else base) +
                   "." + self.format)
      page   = render_html(model, f"{base} {EM_DASH} query structure",
                           base, sql_text)
      with open(output, "w", encoding = "utf-8") as handle:
         handle.write(page)
      if (self.verbose):
         self.report(model, output)
      return(E_SUCCESS)

   ########################################################################

   def report(self, model, output):
      print(f"{self.self}: wrote {output}", file = sys.stderr)
      for diagram in model.diagrams:
         for scope in diagram.scopes:
            names = ", ".join(t.title() for t in scope.ordered_tables())
            print(f"{self.self}:   {scope.label} {scope.context}:"
                  f" {names}; {len(scope.leaves)} predicate(s)",
                  file = sys.stderr)

###########################################################################
###
### s t a r t    o f    e x e c u t i o n
###
###########################################################################

if ( __name__ == "__main__" ):
   try:
      sys.exit(MAIN(sys.argv[0]).execute(sys.argv[1:]))
   except VqError as exception:
      exception.print_error()
      sys.exit(exception.exit_code)
