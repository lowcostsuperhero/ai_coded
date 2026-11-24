#!/usr/bin/python
# name: $Id: vq.py 2.main-2 22:18:31 23-Nov-2026 claude-opus-5-5 $
# use: draws the structure of an Oracle SQL query (tables, referenced
#      columns, returned columns, join and filter predicates, and the
#      AND/OR grouping of those predicates) as an interactive HTML page
# usage: vq.py [-oOutput] [-fFormat] [-v] [-h] file.sql

import getopt
import html
import itertools
import math
import os
import re
import sys

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

REVISION     = "$Revision: 2.main-2 $"
FORMATS      = ("html",)

CHAR_PX      = 7.9
NAME_CHAR_PX = 8.7
SMALL_PX     = 6.7
SLOT_PX      = 7
COL_GAP_PX   = 24
BAND_PX      = 14
                               # row just off the column names that
                               # holds the join operators
OP_ROW_PX    = 18
                               # how far a bracket or arc runs past
                               # its top and bottom terms
ENCLOSE_PX   = 6
ROW_PX       = 13
GAP_PX       = 36
MARGIN_PX    = 30
PASS_PX      = 6
CHECK_PX     = 9
ORDER_PX     = 6.6
                               # a value compared to a column hangs off
                               # it at 45 degrees from vertical
FILTER_TILT  = math.tan(math.radians(45))
                               # gap left between a value's arrowhead
                               # and the point it aims at
ARROW_GAP_PX = 4
                               # least drop of the slanted part of a
                               # value's line, so its arrow has room
FILTER_REACH_PX = 14
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

                               # colour of the horizontal grouping bars
BAR_COLOR    = "#111827"
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
                        else f"{message.capitalize()}")

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
                               # future: "*" for a column effectively
                               # used by an index, or its position in
                               # the primary key
      self.marker   = None
      self.slots    = {"up": [], "down": []}
                               # position in the ORDER BY list, if any
      self.order_by = None
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
      self.rows       = 0
                               # the query that fills a CTE reference or
                               # inline view; None for everything else,
                               # including tables made up for columns
                               # that match no FROM table
      self.body       = None
                               # a CTE reference / inline view that
                               # could not be folded into its query
      self.virtual    = False

   ########################################################################

   def column(self, key, display = None):
      """
      post: returns the ColumnRef for key, creating it if needed; the
            first spelling seen is the one displayed
      """
      if (key not in self.columns):
         self.columns[key] = ColumnRef(display or key)
      return(self.columns[key])

   ########################################################################

   def title(self):
      if (self.alias and self.alias.upper() != self.name.upper()):
         return(f"{self.name} {self.alias}")
      return(self.name)

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
                               # inside a bracket or arc it is drawn
                               # even for a single line, to anchor it
      self.forced = False
      self.drawn  = False
      self.band   = 0

###########################################################################

class Mark:
   """
   use: encloses bars and single lines at one side of one table when
        parentheses nest, with a delimiter left and right: [ ] for AND
        (the vertical equivalent of a bar), ( ) for OR, dash-dot [ ]
        for NOT; a nested enclosure sits inside its parent
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
   return(expression.sql(dialect = "oracle"))

###########################################################################

def shorten(text, limit = LABEL_MAX):
   text = re.sub(r"\s+", " ", text)
   return(text if (len(text) <= limit) else text[:limit - 1] + "…")

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
                               # CTE name -> its query
      self.bodies   = {}
      self.counter  = 0
                               # sql*plus terminators on their own line
                               # are not sql
      text = re.sub(r"(?m)^\s*/\s*$", ";", sql_text)
      try:
         parsed = sqlglot.parse(text, read = "oracle")
      except ParseError as error:
         raise(VqError(f"cannot parse sql: {error}"))
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
         first = statement
         while (isinstance(first, (SETOP, exp.Subquery))):
            first = first.this
                               # a set operation's ORDER BY names the
                               # select list of its first branch
         if (order is not None and id(first) in self.scopes):
            self.add_order(self.scopes[id(first)], order)
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
                        target.order_by = column.order_by
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
               for column in own_columns(projection):
                  end = self.endpoint(inner, column)
                  found.append((end.table, end.column))
            elif ("*" in names and len(inner.sources) == 1):
               source = inner.sources[0]
               found.append((source,
                             source.column(key, table.columns[key].name)))
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
         leaf = Leaf(f"{table.title()}.{column.name} \u2190 {table.name}")
         leaf.feed = True
         for select in branches:
            inner = self.scopes[id(select)]
            if (key in names and names.index(key) < len(select.expressions)):
               sources = own_columns(select.expressions[names.index(key)])
               ends    = [self.endpoint(inner, c) for c in sources]
            elif (    "*" in names
                  and len(inner.sources) == 1):
               source = inner.sources[0]
               ends   = [Endpoint(source, source.column(key,
                                                        column.name))]
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
      return(Endpoint(table, table.column(key, display),
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
            leaf.segments.append(
               Segment(Endpoint(left,  left.column(key, name)),
                       Endpoint(table, table.column(key, name)), "="))
            self.mark_outer(leaf, table, side)
            self.attach(children, leaf)
      where = select.args.get("where")
      if (where is not None):
         self.attach(children, self.build(scope, where.this))
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
      for projection in scope.select.expressions:
         target = (     projection.this
                   if (isinstance(projection, exp.Alias))
                   else projection)
         if (isinstance(target, exp.Star)):
            for table in scope.sources:
               table.column("*").returned = True
            continue
         for column in own_columns(projection):
            self.endpoint(scope, column).column.returned = True

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
               self.endpoint(scope, column)
      order = scope.select.args.get("order")
      if (order is not None):
         self.add_order(scope, order)

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
         for column in own_columns(target):
            if (not column.table and len(scope.sources) != 1):
               continue
            ref = self.endpoint(scope, column).column
            if (ref.order_by is None):
               ref.order_by = position

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
         pairs = [(expression.this, expression.args["low"] , ">="),
                  (expression.this, expression.args["high"], "<=")]
      elif (isinstance(expression, exp.In)):
         values = (   expression.args.get("query")
                   or expression.expressions)
         pairs  = [(expression.this, values, "IN")]
      elif (isinstance(expression, exp.Exists)):
         leaf.sql    = (f"{prefix}EXISTS "
                        f"({self.subquery_label(expression)})")
         leaf.silent = True
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
      return([self.endpoint(inner, c)
              for c in own_columns(query.expressions[0])])

   ########################################################################

   def add_pair(self, scope, leaf, left, right, op):
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
                  else f"{self.side_text(left)} {op} …")
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
                               # the first run settles where every line
                               # heads; the second orders the lines
                               # leaving each column by that, so they
                               # fan out instead of crossing
      self.order = {}
      self.run()
      self.order = self.line_directions()
      self.run()

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
      self.place_trunks()
      self.diagram.units  = self.units
      self.diagram.marks  = self.marks
      self.diagram.trunks = self.trunks
      self.place_rows()
      self.place_vertical()
                               # needs table heights, changes none
      self.place_passes()
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
            if (lower.table.index - upper.table.index > 1):
               self.passes.append(segment)
         for end in segment.ends():
            end.column.slots[end.side].append(end)

   ########################################################################

   def place_columns(self):
      for table in self.tables:
         names      = sorted(table.columns,
                             key = lambda n: (n != "*", n))
         table.left = table.scope.left
         cursor     = table.left + 14
         for name in names:
            column       = table.columns[name]
            slots        = max(len(self.slot_groups(column, "up")),
                               len(self.slot_groups(column, "down"))
                               ) * SLOT_PX
                               # the check sits at the top right corner
                               # of the name, so it widens the column
            check        = (  (CHECK_PX if (column.returned) else 0)
                            + self.order_px(column))
            column.width = max(text_px(name) + check, slots)
            column.x     = cursor + max(text_px(name), slots) / 2
            cursor      += column.width + COL_GAP_PX
         table.order      = names
         table.name_right = table.left + text_px(table.title(),
                                                 NAME_CHAR_PX)
         table.right      = max(cursor - COL_GAP_PX, table.name_right)

   ########################################################################

   def order_px(self, column):
      if (column.order_by is None):
         return(0)
      return(len(str(column.order_by)) * ORDER_PX + 2)

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
      use: spreads several lines leaving one column side by side,
           left to right in the order of the direction they head, so
           they do not cross as they leave
      """
      for table in self.tables:
         for column in table.columns.values():
            for side in ("up", "down"):
               groups = sorted(self.slot_groups(column, side),
                               key = lambda g: sum(self.direction(e)
                                                   for e in g) / len(g))
               count  = len(groups)
               for (position, group) in enumerate(groups):
                  for end in group:
                     end.x = (  column.x
                              + (position - (count - 1) / 2) * SLOT_PX)

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

   def place_passes(self):
      """
      use: picks where lines between non-adjacent tables cross each
           table between them: the white space between two column
           names nearest their straight course, avoiding the table
           name when practical, else straight through a column name
      imp: tables are swept top to bottom; lines sharing a gap are
           spaced in order of where they come from and where they
           end, so they neither cross inside it nor where they part
      """
      current = {}
      for segment in self.passes:
         current[id(segment)] = (segment.upper.x,
                                 segment.upper.table.bottom)
      for table in self.tables:
         crossing = [s for s in self.passes
                     if (  s.upper.table.index
                         < table.index
                         < s.lower.table.index)]
         if (not crossing):
            continue
         (gaps, spans) = self.table_gaps(table)
         chosen        = {}
                               # lines ending at this table: where they
                               # come from and where they land
         enders = []
         for other in self.segments():
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
                                                enders)
            chosen.setdefault(key, (center, room, []))[2].append(
               (x, lower.x, segment))
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
                               # same extent as place_columns gave it
         left   = column.x - max(text_px(name), slots) / 2
         spans.append((left - 2, left + column.width + 2, name))
                               # filter labels below the columns are in
                               # the way too
      blocks = list(spans)
      for segment in self.segments():
         if (segment.kind != "join" and segment.a.table is table):
            (a, b) = self.row_span(table, segment, segment.row)
            if (segment.kind == "filter"):
               a = segment.a.x
            blocks.append((a - 3, b + 3, None))
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
                enders = ()):
      """
      post: returns (key, center, room) of the cheapest place with
            room left to cross table
      imp: passing on the other side of a line that ends at this
           table than the side it came down on means crossing it,
           which costs more than going through the table name
      """
      def crossings(at):
         if (x is None):
            return(0)
         return(sum(1 for (start, end) in enders
                    if ((x - start) * (at - end) < 0)))
      best = None
      for (center, width, key) in gaps:
         room = max(int(width // PASS_PX), 1)
         if (key != "end" and len(chosen.get(key, (0, 0, []))[2]) >= room):
            continue
         cost = abs(center - ideal) + 60 * crossings(center)
         if (table.left - 4 <= center <= table.name_right + 4):
            cost += 40
         if (best is None or cost < best[0]):
            best = (cost, key, center, room)
      for (a, b, name) in spans:
                               # through a column name only when every
                               # gap is far out of the way
         at   = min(max(ideal, a + 2), b - 2)
         cost = abs(at - ideal) + 160 + 60 * crossings(at)
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
      use: works out the bars at each side of each table: for every
           condition tree, the part touching that side is cut out; each
           AND group's own conditions become one bar, and nested groups
           are enclosed in [ ] (AND) or ( ) (OR)
      imp: top level OR alternatives that sit side by side on one level
           need no marks: separate bars are alternatives; stacked on
           different levels they are enclosed in one ( )
      """
      self.units     = []
      self.marks     = []
      self.unit_of   = {}
      self.single_of = {}
      roots = [scope.root for scope in self.diagram.scopes
               if (scope.root is not None)]
      for table in self.tables:
         for side in ("down", "up"):
            band = 0
            for root in roots:
               item = self.restrict(root, table, side)
               if (item is None):
                  continue
               if (item[0] != "or"):
                  band = self.assign_bands(self.shape(item, table, side, 0),
                                           band)
                  continue
               kids = [self.shape(k, table, side, 1) for k in item[2]]
               if (self.side_by_side(kids)):
                  used = False
                  for kid in kids:
                     if (len(kid.ends) >= 2):
                        self.make_bar(kid, band)
                        used = True
                  band += 1 if (used) else 0
               else:
                  mark = self.new_mark("OR", item[1], kids, 0, table, side)
                  band = self.assign_bands(mark, band)
            table.bands[side] = max(table.bands[side], band)
                               # an operator row is kept only where an
                               # operator sits in it, not on a level
      for table in self.tables:
         table.ops = {"down": False, "up": False}
      for segment in self.segments():
         if (segment.kind != "join" or not segment.op):
            continue
         for end in segment.ends():
            shown = (   end is segment.a
                     or segment.op in ("=", "<>", "!=")
                     or end.join_mark)
            if (shown and id(end) not in self.single_of):
               end.table.ops[end.side] = True
                               # a value's line leaves its column where
                               # the column meets its bar, its own level
                               # in an enclosure, or where the nearest
                               # bar would be
      for segment in self.segments():
         if (segment.kind != "filter"):
            continue
         end  = segment.a
         unit = (   self.unit_of.get(id(end))
                 or self.single_of.get(id(end)))
         ops  = OP_ROW_PX if (end.table.ops["down"]) else 0
                               # with no bar of its own: below every bar
                               # there, so it is not read as a member
         band = (     unit.band if (unit is not None)
                 else end.table.bands["down"])
         end.join_off = 3 + ops + (band + 0.5) * BAND_PX

   ########################################################################

   def side_by_side(self, kids):
      """
      post: true when every alternative is a plain bar or line and no
            two bars overlap horizontally, so all fit on one level; a
            single line is separate from any bar already
      """
      if (any(not isinstance(k, Unit) for k in kids)):
         return(False)
      spans = sorted(self.unit_span(k, 0) for k in kids
                     if (len(k.ends) >= 2))
      return(all(b[0] > a[1] + 6 for (a, b) in zip(spans, spans[1:])))

   ########################################################################

   def make_bar(self, unit, band):
      unit.drawn = True
      unit.band  = band
      for end in unit.ends:
         self.unit_of[id(end)] = unit

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
      for child in children:
         if (isinstance(child, Unit)):
            child.forced = True
      self.marks.append(mark)
      return(mark)

   ########################################################################

   def assign_bands(self, element, band):
      """
      post: drawn bars get consecutive bands in SQL order, so the bars
            under one bracket or arc sit together; returns the next
            free band
      """
      if (isinstance(element, Mark)):
         for child in element.children:
            band = self.assign_bands(child, band)
         return(band)
                               # two or more lines make a bar; a single
                               # line inside an enclosure gets a level
                               # of its own for its operator; any other
                               # single line is left as it is
      if (len(element.ends) >= 2):
         self.make_bar(element, band)
         band += 1
      elif (element.forced):
         element.band = band
         for end in element.ends:
            self.single_of[id(end)] = element
         band += 1
      return(band)

   ########################################################################

   def place_trunks(self):
      """
      use: joins between the same two bars (or a bar and a column) are
           drawn as stubs from their columns to the bar plus a single
           trunk between the tables; from one bar a trunk goes to each
           other table
      imp: when several joins share a trunk their columns must pair up
           in the same left to right order at both ends, and no other
           join may sit between them on a bar, or nothing would show
           which column goes with which; such joins stay separate lines
           with dots on the bars
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
            if (top is None and bottom is None):
               continue
            key = (id(top), id(bottom), id(segment.upper.table),
                   id(segment.lower.table))
            groups.setdefault(key, (top, bottom, []))[2].append(segment)
      for (top, bottom, segments) in groups.values():
         segments.sort(key = lambda s: (s.upper.column.x,
                                        s.lower.column.x))
         lowers = [s.lower.column.x for s in segments]
         if (any(b < a for (a, b) in zip(lowers, lowers[1:]))):
            continue
         if (   self.interleaved(top, segments, "upper")
             or self.interleaved(bottom, segments, "lower")):
            continue
         self.trunks.append(self.new_trunk(top, bottom, segments))
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
            end.x = (min(e.x for e in mine) + max(e.x for e in mine)) / 2
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
         if (element.drawn):
            return(self.unit_span(element))
         x = element.ends[0].x
         return(x - 7, x + 7)
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
      return(left, right)

   ########################################################################

   def unit_span(self, unit, y = None):
      """
      post: returns (x0, x1) of a unit's bar: from its leftmost line to
            its rightmost, no further
      imp: when every line meets it at one x (one column's stubs) it
           is kept 6 wide, so it still shows
      """
      if (y is None):
         y = (     band_y(unit.table, unit.side, unit.band)
              if (hasattr(unit.table, "down_top")) else 0)
      xs = [self.member_x(end, y) for end in unit.ends]
      (x0, x1) = (min(xs), max(xs))
      if (x1 - x0 < 6):
         (x0, x1) = ((x0 + x1) / 2 - 3, (x0 + x1) / 2 + 3)
      return(x0, x1)

   ########################################################################

   def member_x(self, end, y):
                               # a value's line runs straight down to
                               # its bar before it slants
      return(end.x)

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
                   if (s.kind != "join" and s.a.table is table)]
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
               if (a - 2 <= x <= b + 2):
                  return(False)
         elif (strict):
            (c, d) = self.row_span(table, other, other.row)
            for x in self.lines_at(table, segment, other.row):
               if (c - 2 <= x <= d + 2):
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
      for item in diagram.items:
         kind = item[0]
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
      right = max([t.right for t in self.tables] + [300])
      self.right            = right
      diagram.right         = right
      diagram.width         = right + 40
      notes                 = sum(len(s.notes) for s in diagram.scopes)
      diagram.notes_y       = last + 26
      diagram.height        = last + 24 + notes * 16

   ########################################################################

   def place_table(self, table, y):
      table.top        = y
      table.name_y     = y + 20
                               # bars for lines from tables above sit
                               # between the name and the column names,
                               # mirroring the bars below the columns:
                               # the nearest is as far above the names
                               # as the nearest bar below is under them
      bands            = table.bands["up"] * BAND_PX
      ops_up           = OP_ROW_PX if (table.ops["up"]) else 0
      ops_down         = OP_ROW_PX if (table.ops["down"]) else 0
      table.up_bottom  = table.name_y + 8 + bands
                               # the nearest level is as far from the
                               # column names as the operator row, so
                               # every stub to an operator is alike
      table.col_y      = (     table.name_y + 22 + ops_up if (not bands)
                          else table.up_bottom + 15 + ops_up)
      table.col_top    = table.col_y - 12
      table.col_bottom = table.col_y + 5
                               # operator rows, then the bar levels
      table.op_y       = {"up"  : table.col_top - 10,
                          "down": table.col_bottom + 10}
      table.down_top   = table.col_bottom + 3 + ops_down
      table.filter_top = (  table.down_top
                          + table.bands["down"] * BAND_PX + 4
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
   ops = OP_ROW_PX if (table.ops["down"]) else 0
   return(  12 + FILTER_REACH_PX + ops + table.bands["down"] * BAND_PX
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

def band_y(table, side, band):
   if (side == "up"):
      return(table.up_bottom - (band + 0.5) * BAND_PX)
   return(table.down_top + (band + 0.5) * BAND_PX)

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

def rounded_path(points, radius = CORNER_PX):
   """
   use: svg path through points, made of straight segments whose
        corners are rounded
   usage: radius 0 gives pointed corners
   """
   parts = [f"M{num(points[0][0])},{num(points[0][1])}"]
   for index in range(1, len(points) - 1):
      ((px, py), (cx, cy), (nx, ny)) = points[index - 1:index + 2]
      before = math.hypot(cx - px, cy - py)
      after  = math.hypot(nx - cx, ny - cy)
      cross  = (cx - px) * (ny - cy) - (cy - py) * (nx - cx)
      if (before < 0.01 or after < 0.01 or abs(cross) < 0.01
          or radius <= 0):
         parts.append(f"L{num(cx)},{num(cy)}")
         continue
      r = min(radius, before / 2, after / 2)
      parts.append(f"L{num(cx - (cx - px) / before * r)},"
                   f"{num(cy - (cy - py) / before * r)} "
                   f"Q{num(cx)},{num(cy)} "
                   f"{num(cx + (nx - cx) / after * r)},"
                   f"{num(cy + (ny - cy) / after * r)}")
   parts.append(f"L{num(points[-1][0])},{num(points[-1][1])}")
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
             '</marker></defs>'
             + "".join(panels) + "</svg>")

   ########################################################################

   def diagram_svg(self, diagram):
      out    = [f'<rect class="panel" x="6" y="2" '
                f'width="{num(diagram.width - 12)}" '
                f'height="{num(diagram.height - 4)}" rx="6"/>']
      right  = diagram.right + 24
      for scope in diagram.scopes:
         if (scope.boxed):
            out.append(self.box_svg(scope, right))
      for (text, indent, y) in diagram.seps:
         x = MARGIN_PX + BOX_PAD_PX + indent * INDENT_PX
         out.append(f'<text class="sep" x="{num(x)}" y="{num(y)}">'
                    f'{esc(text)}</text>')
      self.layout    = diagram.layout
                               # operators go over every line, so each
                               # blanks out whatever line runs under it
      self.ops_layer = []
      for scope in diagram.scopes:
         for leaf in scope.leaves:
            out.append(self.leaf_svg(leaf))
      for leaf in diagram.feeds:
         out.append(self.leaf_svg(leaf))
      self.layout = diagram.layout
      for trunk in diagram.trunks:
         out.append(self.trunk_svg(trunk))
      for unit in diagram.units:
         if (unit.drawn):
            out.append(self.unit_svg(diagram.layout, unit))
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
      x    = scope.left - BOX_PAD_PX
      x2   = right - max(scope.indent, 0) * 6
      return(f'<rect class="{cls}" x="{num(x)}" '
             f'y="{num(scope.box_top)}" width="{num(x2 - x)}" '
             f'height="{num(scope.box_bottom - scope.box_top)}" '
             'rx="4"/>'
             f'<text class="btitle" x="{num(x + 8)}" '
             f'y="{num(scope.box_top + 15)}">{esc(scope.label)} '
             f'· {esc(scope.context)}</text>')

   ########################################################################

   def leaf_svg(self, leaf):
      nodes  = " ".join(node.ident for node in leaf.ancestors)
      out    = [f'<g class="leaf" data-leaf="{leaf.ident}" '
                f'data-nodes="{nodes}" data-sql="{esc(leaf.sql)}">'
                f'<title>{esc(leaf.sql)}</title>']
      labels = []
      for segment in leaf.segments:
         if (segment.trunk is not None):
            trunk = segment.trunk
            for (end, unit) in ((segment.upper, trunk.units[0]),
                                (segment.lower, trunk.units[1])):
               if (unit is None):
                  continue
               table = end.table
               y_bar = band_y(table, end.side, unit.band)
               (y0, y1) = (     (table.col_bottom, y_bar)
                           if (end.side == "down")
                           else (y_bar, table.col_top))
               stub = f"M{num(end.x)},{num(y0)} V{num(y1)}"
               out.append(f'<path class="hit" d="{stub}"/>')
               out.append(f'<path class="ln" d="{stub}"/>')
            self.ops_layer += self.op_svg(segment)
            continue
         if (segment.kind == "join"):
            (path, _) = self.join_path(segment)
         else:
            table = segment.a.table
            y_end = table.filter_top + segment.row * ROW_PX + 5
            if (segment.kind == "filter"):
                               # straight down the column to where it
                               # meets its bar, then slanted out to the
                               # value; drawn from that point out, so
                               # the reversed start marker points back
                               # at it
               (x_end, y_end) = filter_end(segment)
               x_join = segment.a.x
               y_join = table.col_bottom + segment.a.join_off
               stub   = (f"M{num(x_join)},{num(table.col_bottom)} "
                         f"V{num(y_join)}")
                               # the arrowhead stops short of the point
                               # it aims at
               length = math.hypot(x_end - x_join, y_end - y_join) or 1
               x_tip  = x_join + (x_end - x_join) / length * ARROW_GAP_PX
               y_tip  = y_join + (y_end - y_join) / length * ARROW_GAP_PX
               path   = rounded_path([(x_tip, y_tip), (x_end, y_end)])
               for d in (stub, path):
                  out.append(f'<path class="hit" d="{d}"/>')
               out.append(f'<path class="ln" d="{stub}"/>')
               out.append(f'<path class="ln" d="{path}" '
                          'marker-start="url(#vqArrow)"/>')
               labels.append(((x_end + 4, y_end + 4), segment.op,
                              "flt", "start"))
               path = None
            else:
               path = rounded_path([(segment.a.x, table.col_bottom),
                                    (segment.a.x, y_end),
                                    (segment.b.x, y_end),
                                    (segment.b.x, table.col_bottom)])
               labels.append((((segment.a.x + segment.b.x) / 2,
                               y_end + 13), segment.op, "flt",
                              "middle"))
                               # oracle style (+) at the column end of
                               # the line, on the optional table's side;
                               # a join's goes beside its operator
         for end in segment.ends():
            if (end.join_mark and segment.kind != "join"):
               y = (     end.table.col_top - 1 if (end.side == "up")
                    else end.table.col_bottom + 11)
                               # under its column name, so its (+) goes
                               # on the left, just above that point
               if (segment.kind == "filter"):
                  y = end.table.col_bottom + end.join_off - 3
                  labels.append(((end.x - 4, y), "(+)", "mark", "end"))
               else:
                  labels.append(((end.x + 4, y), "(+)", "mark",
                                 "start"))
         if (path is not None and leaf.feed):
                               # the arrow points into the column read
                               # through the CTE / inline view: the a
                               # end; paths run from upper to lower
            marker = (     "marker-start" if (segment.a is segment.upper)
                      else "marker-end")
            out.append(f'<path class="hit" d="{path}"/>')
            out.append(f'<path class="ln feed" d="{path}" '
                       f'{marker}="url(#vqFeed)"/>')
         elif (path is not None):
            out.append(f'<path class="hit" d="{path}"/>')
            out.append(f'<path class="ln" d="{path}"/>')
            if (segment.kind == "join"):
               self.ops_layer += self.op_svg(segment)
      for ((x, y), text, kind, anchor) in labels:
         out.append(f'<text class="{kind}" x="{num(x)}" y="{num(y)}" '
                    f'text-anchor="{anchor}">{esc(text)}</text>')
      out.append("</g>")
      return("".join(out))

   ########################################################################

   def join_path(self, segment):
      """
      post: returns (svg path, operator label position) of a join
      imp: a line between non-adjacent tables runs straight through
           the tables between, at the x chosen by place_passes
      """
      (upper, lower) = (segment.upper, segment.lower)
      (y0, y3)       = (upper.table.col_bottom, lower.table.col_top)
      if (segment.members):
                               # a trunk runs between bars, or to a
                               # column where a table has no bar
         (top, bottom) = segment.units
         if (top is not None):
            y0 = band_y(upper.table, "down", top.band)
         if (bottom is not None):
            y3 = band_y(lower.table, "up", bottom.band)
      points = [(upper.x, y0),
                (upper.x, upper.table.bottom)]
      for (table, x) in segment.via:
         points.append((x, table.top))
         points.append((x, table.bottom))
      points.append((lower.x, lower.table.top))
      points.append((lower.x, y3))
                               # the sloped legs are every other pair
                               # of points after the first stub
      legs = [(points[i], points[i + 1])
              for i in range(1, len(points) - 2, 2)]
      if (len(legs) == 1):
         (leg, t) = (legs[0], {"upper": 0.25,
                               "lower": 0.75}.get(segment.lean, 0.5))
      else:
         leg = (     legs[0]  if (segment.lean == "upper")
                else legs[-1] if (segment.lean == "lower")
                else legs[len(legs) // 2])
         t   = 0.5
      ((x0, y0), (x1, y1)) = leg
      return(rounded_path(points),
             (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t + 4))

   ########################################################################

   def op_svg(self, segment):
      """
      post: returns a join's operator, centered on its line just off
            the column name and blanking the line behind it: at both
            ends for "=" or "<>", which read the same from either side,
            else at the left hand side's end; a line that has a level of
            its own inside an enclosure carries it at that level
      """
      out    = []
      layout = self.layout
      for end in segment.ends():
         if (    end is not segment.a
             and segment.op not in ("=", "<>", "!=")
             and not end.join_mark):
            continue
         unit = layout.single_of.get(id(end))
         y    = (     band_y(end.table, end.side, unit.band)
                 if (unit is not None)
                 else end.table.op_y[end.side])
         text = segment.op if (end is segment.a or
                               segment.op in ("=", "<>", "!=")) else ""
         if (text):
            width = text_px(text, SMALL_PX) + 3
            out.append(f'<rect class="opbg" x="{num(end.x - width / 2)}" '
                       f'y="{num(y - 6)}" width="{num(width)}" '
                       'height="12"/>'
                       f'<text class="op stub" x="{num(end.x)}" '
                       f'y="{num(y + 4)}" text-anchor="middle">'
                       f'{esc(text)}</text>')
         if (end.join_mark):
                               # (+) on the left under a column name,
                               # on the right above one
            gap = text_px(text, SMALL_PX) / 2 + 3 if (text) else 3
            if (end.side == "down"):
               (x, anchor) = (end.x - gap, "end")
            else:
               (x, anchor) = (end.x + gap, "start")
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
      (path, _) = self.join_path(trunk)
      return(f'<g class="leaf trunk" data-leaf="T{id(trunk)}" '
             f'data-members="{members}" data-nodes="{nodes}" '
             f'data-sql="{esc(sql)}"><title>{esc(sql)}</title>'
             f'<path class="hit" d="{path}"/>'
             f'<path class="ln" d="{path}"/></g>')

   ########################################################################

   def unit_svg(self, layout, unit):
      """
      post: returns a bar, with a dot on each line that crosses it
            rather than ending on it as a stub
      """
      y        = band_y(unit.table, unit.side, unit.band)
      (x0, x1) = layout.unit_span(unit, y)
      ident    = unit.node.ident if (unit.node is not None) else ""
      sql      = (     f"{unit.node.kind}: {unit.node.sql}"
                  if (unit.node is not None) else "")
      out = [f'<g class="bar" data-node="{ident}" data-sql="{esc(sql)}" '
             f'style="--c:{BAR_COLOR}">'
             f'<title>{esc(shorten(sql, 300))}</title>'
             f'<line x1="{num(x0)}" y1="{num(y)}" x2="{num(x1)}" '
             f'y2="{num(y)}"/>']
      for end in unit.ends:
         if (end.segment.trunk is None):
            out.append(f'<circle class="member" '
                       f'cx="{num(layout.member_x(end, y))}" '
                       f'cy="{num(y)}" r="2.6"/>')
      out.append("</g>")
      return("".join(out))

   ########################################################################

   def mark_svg(self, layout, mark):
      """
      post: returns an enclosure, built like a bracket on each side: an
            upright from a little above its top term to a little below
            its bottom one, with a stub in at each end and at each term
            between; for AND (and NOT, dash-dot) the upright is
            straight, for OR each stretch between stubs is bowed out
            into its own arc, so there is one arc per OR
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
             f'<path class="{cls}" d="{" ".join(parts)}"/></g>')

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
            level give or take ENCLOSE_PX, an enclosure from its top
            end to its bottom end
      """
      if (isinstance(element, Unit)):
         y = band_y(element.table, element.side, element.band)
         return(y - ENCLOSE_PX, y + ENCLOSE_PX)
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
             f'y="{num(table.name_y)}">{esc(table.title())}</text>',
             f'<line class="uline" x1="{num(table.left)}" '
             f'y1="{num(table.name_y + 5)}" '
             f'x2="{num(table.name_right)}" '
             f'y2="{num(table.name_y + 5)}"/>']
      for name in table.order:
         column = table.columns[name]
         half   = text_px(name) / 2
         out.append(f'<text class="col" x="{num(column.x)}" '
                    f'y="{num(table.col_y)}" text-anchor="middle">'
                    f'{esc(column.name)}</text>')
         corner = column.x + half + 1
         if (column.returned):
            out.append(f'<text class="chk" x="{num(corner)}" '
                       f'y="{num(table.col_y - 8)}">✓</text>')
            corner += CHECK_PX
                               # ORDER BY position: after the check, or
                               # in its place
         if (column.order_by is not None):
            out.append(f'<text class="ord" x="{num(corner)}" '
                       f'y="{num(table.col_y - 8)}">'
                       f'<title>ORDER BY position {column.order_by}'
                       f'</title>{column.order_by}</text>')
         if (column.marker):
            out.append(f'<text class="mrk" '
                       f'x="{num(column.x - half - 1)}" '
                       f'y="{num(table.col_y - 8)}" text-anchor="end">'
                       f'{esc(column.marker)}</text>')
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
.uline { stroke: #1f2937; stroke-width: 1.2; }
.col { font-size: 13px; }
.chk { fill: #059669; font-size: 11px; font-weight: bold;
       stroke-width: 2px; }
.mrk { fill: #b45309; font-weight: bold; }
.ord { fill: #2563eb; font-size: 11px; font-weight: bold;
       stroke-width: 2px; }
.ln { fill: none; stroke-linejoin: round; stroke: #4b5563;
      stroke-width: 1.4; }
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
.brk.not { stroke-dasharray: 7 2 1.5 2; }
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
g.leaf.hl .ln { stroke: #111827; stroke-width: 2.4; }
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
#info { position: sticky; top: 44px; z-index: 1; min-height: 15px;
        padding: 6px 16px; background: #fffbeb; font: 12px monospace;
        border-bottom: 1px solid #fde68a; white-space: pre-wrap; }
#canvas { padding: 12px; overflow: auto; }
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
<div id="info"></div>
<details class="legend"><summary>Legend</summary><ul>
<li>&#10003; returned column; 1 2 3 its position in the ORDER BY</li>
<li>things on one horizontal bar are AND'ed; separate bars are
  OR'ed</li>
<li>a column joins its bar by a short stub, and one connector runs
  from a bar to each other table; an open circle marks a line that
  meets or crosses a bar instead of ending on it as a stub</li>
<li>operators sit on the line next to the column: "=" at both ends,
  others at the left hand side's end; (+) marks an outer join</li>
<li>an arrow from a value points at the column it is compared
  to</li>
<li>where parentheses nest, [ ] encloses what is AND'ed, ( ) what
  is OR'ed (one arc per OR), dash-dot [ ] a NOT</li>
<li>a coloured dotted line joins OR'ed alternatives that never meet
  at one table</li>
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
            f"usage: {self.self} [options] file.sql\n"
            "where options are:\n"
            "   -oOutput output file; default is file.sql with its\n"
            "            extension replaced by the format\n"
            f"   -fFormat output format: {', '.join(FORMATS)}"
            " (default html)\n"
            "   -v       verbose; more -v for more verbosity\n"
            "   -h       help", file = sys.stderr)

   ########################################################################

   def execute(self, argv):
      try:
         (opts, args) = getopt.getopt(argv, "o:f:vh")
      except getopt.GetoptError as error:
         raise(VqError(str(error), self.self, E_USAGE))
      for (option, value) in opts:
         if (option == "-o"):
            self.output = value
         elif (option == "-f"):
            self.format = value.lower()
         elif (option == "-v"):
            self.verbose += 1
         elif (option == "-h"):
            self.usage()
            return(E_SUCCESS)
      if (len(args) != 1):
         self.usage()
         return(E_USAGE)
      if (self.format not in FORMATS):
         raise(VqError(f"unsupported format {self.format}",
                       self.self, E_USAGE))
      source = args[0]
      try:
         with open(source, encoding = "utf-8") as handle:
            sql_text = handle.read()
      except OSError as error:
         raise(VqError(f"cannot read {source}: {error.strerror}",
                       self.self))
      try:
         model = QueryModel(sql_text)
      except VqError as error:
         raise(VqError(error.message.lower(), self.self))
      base   = os.path.splitext(os.path.basename(source))[0]
      output = (   self.output
                or os.path.splitext(source)[0] + "." + self.format)
      page   = render_html(model, f"{base} — query structure",
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
