#!/usr/bin/python
# name: $Id: fillcolumn.py 1.main-1 16:17:36 26-Nov-2026 claude-opus-5-5 $
# use: organize a list of names, each with an optional alias, into
#      aligned columns that fit within a maximum line length
# usage: fillcolumn.py [options] [name ...]
#        run with -h for the list of options

import os
import re
import sys

E_SUCCESS = 0
E_WARN    = 1
E_FAIL    = 2
E_USAGE   = 3

DEFAULT_LENGTH   = 78
REVISION_KEYWORD = "$Revision: 1.main-1 $"

###########################################################################
###
### c l a s s e s
###
###########################################################################

class ExceptionClass(Exception):
   """
   name: ExceptionClass
   use: stand-in with the same interface as the ExceptionClass in the
        python lib; carries a message prefixed by the executable name
        and the exit code to leave with
   """

   def __init__(self, message, exec_name = "", exit_code = E_FAIL):
      super().__init__(message)
      self.exec_name = exec_name
      self.exit_code = exit_code
      self.message   = (     f"{exec_name}: {message}" if (exec_name)
                        else f"{message.capitalize()}")

   ########################################################################
   def print_error(self):
      print(self.message, file = sys.stderr)

###########################################################################
class FillColumn:
   """
   name: FillColumn
   use: splits text into (name, alias) elements, then lays the elements
        out in columns where names, aliases and delimiters line up
   usage: fill_column = FillColumn(",", 78, 0, 3)
          elements    = fill_column.parse([["a x, b y", "c"]])
          (lines, widths, fits, spills) = fill_column.layout(elements)
          (lines, widths, fits, spills) = fill_column.fill(elements, 4)
   pre: delimiter is a non-empty string; length is positive; indents
        are not negative
   imp: a delimiter that is all whitespace never yields aliases; any
        other delimiter makes the last word of a multi-word name its
        alias
   """

   ALIAS_RE = re.compile(r"(.*\S)\s+(\S+)", re.DOTALL)

   def __init__(self, delimiter, length, first_indent, indent):
      self.delimiter    = delimiter
      self.length       = length
      self.first_indent = first_indent
      self.indent       = indent
      self.whitespace   = delimiter.isspace()
      self.pattern      = None
                                # on output, every element but the last
                                # is followed by suffix, and elements on
                                # a line are joined by joiner; on input,
                                # lines and arguments are joined by joint
                                # before splitting
      if (self.whitespace):
         self.suffix = ""
         self.joiner = delimiter
         self.joint  = delimiter
      else:
         self.suffix = delimiter.rstrip()
         self.joiner = delimiter[len(self.suffix):] or " "
         self.joint  = " "
                                # a single space means any run of
                                # whitespace; a run of one whitespace
                                # character means that run or longer,
                                # so shorter runs stay inside the name
      if (self.whitespace and delimiter != " "):
         if (delimiter == delimiter[0] * len(delimiter)):
            self.pattern = re.compile(re.escape(delimiter[0])
                                      + "{%d,}" % len(delimiter))
         else:
            self.pattern = re.compile(re.escape(delimiter))

   ########################################################################
   def split_items(self, text):
      """
      name: split_items
      use: slice text into trimmed items at each delimiter
      post: returns a list of strings; blank items are possible only
            with a non-whitespace delimiter
      """
      if (not self.whitespace):
         items = [item.strip() for item in text.split(self.delimiter)]
                                # a trailing delimiter does not start
                                # another (blank) name
         if (items[-1] == ""):
            items.pop()
         return(items)
      if (self.pattern is None):
         return(text.split())
      return([item.strip() for item in self.pattern.split(text)
              if (item.strip())])

   ########################################################################
   def split_alias(self, item):
      """
      name: split_alias
      use: split the last word off an item as its alias
      post: returns (name, alias); alias is "" when there is none;
            embedded spaces in name are kept as is
      """
      if (not self.whitespace):
         match = self.ALIAS_RE.fullmatch(item)
         if (match):
            return((match.group(1), match.group(2)))
      return((item, ""))

   ########################################################################
   def parse(self, segments):
      """
      name: parse
      use: turn segments of text into (name, alias) elements
      usage: segments is a list of lists of strings; each inner list is
             one source (a file's lines, or the command line arguments)
      imp: each segment is split on its own so that a source ending
           without a delimiter does not run into the next source
      """
      elements = []
      for segment in segments:
         text = self.joint.join(segment)
         elements.extend(self.split_alias(item)
                         for item in self.split_items(text))
      return(elements)

   ########################################################################
   def lead(self, row):
      return(self.first_indent if (row == 0) else self.indent)

   ########################################################################
   def element_text(self, elements, index, name_width = 0,
                    alias_width = 0, pad = False):
      """
      name: element_text
      use: an element as text: name, alias, then the delimiter unless it
           is the last element
      usage: pass name_width and alias_width with pad True to pad the
             name and alias so they line up with other elements of the
             same column; leave them out for compact text
      """
      (name, alias) = elements[index]
      text          = name.ljust(name_width)
      if (alias_width):
         text += " " + alias.ljust(alias_width)
      elif (alias):
         text += " " + alias
                                # pad the missing final delimiter so the
                                # last element lines up like the others
      if (index < len(elements) - 1):
         text += self.suffix
      elif (pad):
         text += " " * len(self.suffix)
      return(text)

   ########################################################################
   def layout(self, elements, wrap = False, optimal = False):
      """
      name: layout
      use: choose the number of columns, then lay out elements
      post: returns (lines, widths, fits, spills) as fill does
      imp: the first line is filled greedily with unpadded elements;
           that count is the most columns that could fit, and every
           count from there down to 1 is tried
           by default the layout with the fewest spills wins, so
           nothing spills unless that cannot be avoided, the larger
           count winning a tie
           when optimal, the layout that fits in the fewest lines with
           the fewest spills wins, the smaller count winning a tie
      """
      count = len(elements)
      if (count == 0):
         return(([], [], True, 0))
      width   = self.first_indent
      columns = 0
      for index in range(count):
         size = len(self.element_text(elements, index))
         if (columns):
            size += len(self.joiner)
         if (columns and width + size > self.length):
            break
         width   += size
         columns += 1
      (best, best_key) = (None, None)
      for columns in range(columns, 0, -1):
         result = self.fill(elements, columns, wrap)
         (lines, widths, fits, spills) = result
         if (optimal):
            key    = (not fits, len(lines), spills)
            better = best is None or key <= best_key
         else:
            key    = (spills, not fits)
            better = best is None or key < best_key
         if (better):
            (best, best_key) = (result, key)
      return(best)

   ########################################################################
   def fill(self, elements, columns, wrap = False):
      """
      name: fill
      use: lay out elements in a fixed number of columns, each column as
           wide as the longest element in it that does not spill
      post: returns (lines, widths, fits, spills); widths are the column
            widths; fits is False when a line runs past the maximum
            length; spills is how many elements spill
      imp: no element spills until the columns, each as wide as the
           longest element in it, are too wide for the line together;
           then elements are made to spill one at a time, as chosen by
           choose_spill, until the columns fit
           an element that spills runs into as many of the following
           columns as it needs, and the next element starts in the next
           free column
           an element that would run past the maximum length moves to
           the next line; if it does not fit there either, it stays
           where it is and the line runs long, or with wrap it is cut
           at the maximum length and the rest continues on the next
           line
           every delimiter is placed at the end of a column, so they
           line up down the page, unless that would put it past the
           maximum length
      """
      count = len(elements)
      if (count == 0):
         return(([], [], True, 0))
      room    = (  self.length - max(self.first_indent, self.indent)
                 - (columns - 1) * len(self.joiner))
      texts   = [self.element_text(elements, index)
                 for index in range(count)]
      regular = [True] * count
      while (True):
         (placements, widths) = self.settle(elements, texts, regular,
                                            columns, wrap)
         if (sum(widths) <= room or not any(regular)):
            break
         regular[self.choose_spill(placements, columns)] = False

      (needed, alias_starts, alias_widths) = (
         self.column_widths(elements, placements, columns))
      starts = self.column_starts(widths)
      ends   = [start + width for (start, width) in zip(starts, widths)]
      lines  = []
      for (row, column, index, text, whole, final) in placements:
         lead     = self.lead(row)
         position = lead + starts[column]
                                # an alias starts where the others in
                                # its column start
         if (whole and elements[index][1]):
            text = self.element_text(elements, index,
                                     alias_starts[column] - 1,
                                     alias_widths[column], True)
         elif (final and index < count - 1):
            text = self.align_delimiter(text, position,
                                        [lead + end for end in ends])
         if (row == len(lines)):
            lines.append("")
         lines[row] = lines[row].ljust(position) + text
      lines  = [line.rstrip() for line in lines]
      fits   = all(len(line) <= self.length for line in lines)
      spills = regular.count(False)
      return((lines, widths, fits, spills))

   ########################################################################
   def choose_spill(self, placements, columns):
      """
      name: choose_spill
      use: pick the element whose spilling narrows the columns the most
           for each element that has to spill
      post: returns the index of an element that does not yet spill
      imp: a column narrows only when every element as long as its
           longest spills, so the saving in a column is the difference
           between its longest and next longest element, divided by
           how many elements are that longest; the best saving wins,
           then the widest column, then the column nearest the start
      """
      sizes = [[] for column in range(columns)]
      for (row, column, index, text, whole, final) in placements:
         if (whole):
            sizes[column].append((len(text), index))
      (best, best_key) = (None, None)
      for (column, entries) in enumerate(sizes):
         if (not entries):
            continue
         ordered = sorted({size for (size, index) in entries},
                          reverse = True)
         longest = ordered[0]
         drop    = longest - (ordered[1] if (len(ordered) > 1) else 0)
         ties    = [index for (size, index) in entries
                    if (size == longest)]
         key     = (drop / len(ties), longest, -column)
         if (best is None or key > best_key):
            (best, best_key) = (min(ties), key)
      return(best)

   ########################################################################
   def settle(self, elements, texts, regular, columns, wrap):
      """
      name: settle
      use: find column widths that agree with where the elements land
      post: returns (placements, widths)
      imp: column widths depend on which elements land in a column, and
           where elements land depends on the column widths, so the
           widths are grown from 0 until the placement settles, then
           shrunk to fit the final placement if that changes nothing
           widths only grow, and never past the widest element, so the
           loop ends
      """
      widths = [0] * columns
      while (True):
         placements = self.place(texts, regular, widths, wrap)
         needed     = self.column_widths(elements, placements, columns)[0]
         grown      = [max(old, new) for (old, new) in zip(widths, needed)]
         if (grown == widths):
            break
         widths = grown
      if (needed != widths):
         trial = self.place(texts, regular, needed, wrap)
         if (self.column_widths(elements, trial, columns)[0] == needed):
            (placements, widths) = (trial, needed)
      return((placements, widths))

   ########################################################################
   def align_delimiter(self, text, position, ends):
      """
      name: align_delimiter
      use: pad text so its delimiter ends where a column ends
      usage: position is where text starts on the line; ends are where
             the columns of that line end
      post: returns text unchanged when there is no delimiter, or when
            the end of the column it reaches is past the maximum length
      imp: an alias stays right after its name; only an element that
           sits wholly in a column has an alias position to line up with
      """
      if (not self.suffix or not text.endswith(self.suffix)):
         return(text)
      natural = position + len(text)
      for end in ends:
         if (end >= natural):
            if (end > self.length):
               break
            body = text[:len(text) - len(self.suffix)]
            return(body.ljust(end - position - len(self.suffix))
                   + self.suffix)
      return(text)

   ########################################################################
   def place(self, texts, regular, widths, wrap):
      """
      name: place
      use: decide the line and column where each element goes, for the
           given column widths
      post: returns a list of [row, column, index, text, whole, final];
            whole means text is an entire element that does not spill;
            final means text holds the end of its element
      imp: an element that does not spill always takes exactly one
           column, even if that column is not yet wide enough for it;
           settle grows the column and places again
      """
      columns       = len(widths)
      starts        = self.column_starts(widths)
      placements    = []
      (row, column) = (0, 0)
      for (index, text) in enumerate(texts):
         if (column >= columns):
            (row, column) = (row + 1, 0)
                                # fill spills elements until the columns
                                # fit the line, so an element that does
                                # not spill always takes its column
         if (regular[index]):
            placements.append([row, column, index, text, True, True])
            column += 1
            continue
         size = len(text)
         fits = self.lead(row) + starts[column] + size <= self.length
         if (not fits and column and self.indent + size <= self.length):
            (row, column, fits) = (row + 1, 0, True)
         if (fits):
            placements.append([row, column, index, text,
                               regular[index], True])
            column = (     column + 1 if (regular[index])
                      else self.next_column(starts, column, size))
         elif (not wrap):
                                # too long for any line; leave it here
                                # and let the line run long
            placements.append([row, column, index, text, False, True])
            column = columns
         else:
            while (text):
               room = self.length - self.lead(row) - starts[column]
               if (room < 1 and column):
                  (row, column) = (row + 1, 0)
                  continue
               room          = max(1, room)
               (piece, text) = (text[:room], text[room:])
               placements.append([row, column, index, piece, False,
                                  not text])
               if (text):
                  (row, column) = (row + 1, 0)
               else:
                  column = self.next_column(starts, column, len(piece))
      return(placements)

   ########################################################################
   def column_widths(self, elements, placements, columns):
      """
      name: column_widths
      use: measure each column from the elements that sit wholly in it
      post: returns (widths, alias starts, alias widths); in a column
            whose elements have aliases, every alias starts at the same
            place, with the longest alias ending at the delimiter; the
            alias start is counted from the column start
      imp: only names that have an alias push the alias start right;
           a longer name without an alias just widens the column
      """
      alias_names  = [0] * columns
      alias_widths = [0] * columns
      plain        = [0] * columns
      for (row, column, index, text, whole, final) in placements:
         if (whole):
            (name, alias) = elements[index]
            if (alias):
               alias_names[column]  = max(alias_names[column], len(name))
               alias_widths[column] = max(alias_widths[column],
                                          len(alias))
            else:
               plain[column] = max(plain[column], len(text))
      widths       = []
      alias_starts = []
      for column in range(columns):
         size = plain[column]
         if (alias_widths[column]):
            size = max(size,
                         alias_names[column] + 1 + alias_widths[column]
                       + len(self.suffix))
         widths.append(size)
         alias_starts.append(size - len(self.suffix)
                             - alias_widths[column])
      return((widths, alias_starts, alias_widths))

   ########################################################################
   def column_starts(self, widths):
      """
      name: column_starts
      post: returns where each column starts, counted from the indent
      """
      (starts, position) = ([], 0)
      for width in widths:
         starts.append(position)
         position += width + len(self.joiner)
      return(starts)

   ########################################################################
   def next_column(self, starts, column, size):
      """
      name: next_column
      use: the first column after column that starts clear of text of
           the given size, with a gap before it
      post: returns len(starts) when no column on the line is clear
      """
      end = starts[column] + size + len(self.joiner)
      for later in range(column + 1, len(starts)):
         if (starts[later] >= end):
            return(later)
      return(len(starts))

###########################################################################
class MAIN:
   """
   name: MAIN
   use: command line driver for FillColumn
   usage: MAIN(sys.argv[0]).execute(sys.argv[1:])
   imp: quoting on the command line is not significant; the arguments
        are rejoined as if they had not been quoted, using a space, or
        the delimiter itself when the delimiter is whitespace (so with
        a multi-space delimiter each argument is at least one name)
        the lines of a -f file are rejoined the same way
   """

   def __init__(self, exec_name = ""):
      self.exec_name = os.path.basename(exec_name)
                                # gitkw expands the keyword on commit;
                                # [$] keeps it from expanding this
                                # pattern too
      match          = re.search(r"[$]Revision:\s*(\S+)\s*[$]",
                                 REVISION_KEYWORD)
      self.revision  = f" {match.group(1)}" if (match) else ""

   ########################################################################
   def usage(self):
      return(f"""{self.exec_name}{self.revision}
usage: {self.exec_name} [options] [name ...]
where options are:
   -cColumns    fill exactly Columns columns per line; without -c, the
                most columns where no name spills are used, unless a
                spill cannot be avoided
   -o           optimal; instead, use the column count that takes the
                fewest lines, then has the fewest spills; not with -c
   -dDelimiter  separator between names; without -d the separator is a
                comma if any comma appears in the input, otherwise any
                whitespace. a delimiter of two or more spaces lets a
                name contain shorter runs of spaces
   -fFile       read names from File; - reads stdin; may be repeated
   -lLength     maximum output line length; default {DEFAULT_LENGTH}
   -iIndent     indent every line by Indent spaces
   -iFirst,Indent
                indent by Indent, and adjust the first line by First:
                negative outdents, positive indents further, and 0
                does not indent the first line at all
   -w           wrap; instead of letting a line run long, cut it at the
                line length and continue on the next line
   -v           verbose; reports the delimiter and the column widths
   -h           help
each column is only as wide as its longest name that does not spill.
names spill only when the columns are too wide for the line together,
longest name first; a name that spills runs into the next column(s),
and the following name starts in the next free column. a name
that would run past the line length moves to the next line, and if it
is too long for any line it stays put and the line runs long, or is
cut and continued with -w. delimiters line up at the ends of columns
unless that would put them past the line length
with a delimiter that is not whitespace, the last word of a name that
has more than one word is its alias, as in a sql select list
names are read from stdin when no names and no -f are given""")

   ########################################################################
   def usage_error(self, message):
      return(ExceptionClass(f"{message}; -h for help", self.exec_name,
                            E_USAGE))

   ########################################################################
   def parse_command_line(self, argv):
      """
      name: parse_command_line
      use: separate options from names
      post: returns (options, names)
      imp: option values may be attached (-l60) or separate (-l 60);
           -- ends the options
      """
      options      = {"c": None, "d": None, "f": [],    "l": None,
                      "i": None, "v": 0,    "w": False, "h": False,
                      "o": False}
      names        = []
      index        = 0
      options_done = False
      while (index < len(argv)):
         arg    = argv[index]
         index += 1
         if (options_done or arg == "-" or not arg.startswith("-")):
            names.append(arg)
            continue
         if (arg == "--"):
            options_done = True
            continue
         flags = arg[1:]
         while (flags):
            (option, flags) = (flags[0], flags[1:])
            if (option == "v"):
               options["v"] += 1
            elif (option in "how"):
               options[option] = True
            elif (option in "cdfli"):
               if (flags):
                  (value, flags) = (flags, "")
               elif (index < len(argv)):
                  value  = argv[index]
                  index += 1
               else:
                  raise(self.usage_error(f"-{option} needs a value"))
                                # an unquoted "-i 2, 4" reaches us as
                                # two arguments; put them back together
               if (    option == "i"
                   and value.rstrip().endswith(",")
                   and index < len(argv)
                   and re.fullmatch(r"\d+", argv[index])):
                  value += argv[index]
                  index += 1
               if (option == "f"):
                  options["f"].append(value)
               else:
                  options[option] = value
            else:
               raise(self.usage_error(f"unknown option -{option}"))
      return((options, names))

   ########################################################################
   def parse_positive(self, value, option, default = None):
      if (value is None):
         return(default)
      if (not re.fullmatch(r"\s*\d+\s*", value) or int(value) < 1):
         raise(self.usage_error(f"{option} must be a positive integer: "
                                f"{value}"))
      return(int(value))

   ########################################################################
   def parse_indent(self, value):
      """
      name: parse_indent
      use: interpret the -i value
      post: returns (first line indent, indent of the other lines)
      """
      if (value is None):
         return((0, 0))
      match = re.fullmatch(r"\s*(-?\d+)\s*(?:,\s*(\d+)\s*)?", value)
      if (not match):
         raise(self.usage_error(f"bad -i value: {value}"))
      if (match.group(2) is None):
         if (int(match.group(1)) < 0):
            raise(self.usage_error(f"-i must not be negative: {value}"))
         return((int(match.group(1)), int(match.group(1))))
      offset = int(match.group(1))
      indent = int(match.group(2))
      first  = (     0 if (offset == 0)
                else max(0, indent + offset))
      return((first, indent))

   ########################################################################
   def read_lines(self, path):
      try:
         if (path == "-"):
            return(sys.stdin.read().splitlines())
         with open(path) as handle:
            return(handle.read().splitlines())
      except OSError as error:
         raise(ExceptionClass(f"cannot read {path}: {error.strerror}",
                              self.exec_name, E_FAIL))

   ########################################################################
   def execute(self, argv):
      """
      name: execute
      use: run fillcolumn with the given command line arguments
      post: returns the exit code; the columns are on stdout
      """
      (options, names) = self.parse_command_line(argv)
      if (options["h"]):
         print(self.usage())
         return(E_SUCCESS)
      length                 = self.parse_positive(options["l"], "-l",
                                                   DEFAULT_LENGTH)
      columns                = self.parse_positive(options["c"], "-c")
      if (columns and options["o"]):
         raise(self.usage_error("-c and -o cannot be used together"))
      (first_indent, indent) = self.parse_indent(options["i"])
      if (options["d"] == ""):
         raise(self.usage_error("-d must not be empty"))

      segments = [self.read_lines(path) for path in options["f"]]
      if (names):
         segments.append(names)
      if (not segments):
         if (sys.stdin.isatty()):
            print(self.usage(), file = sys.stderr)
            return(E_USAGE)
         segments.append(self.read_lines("-"))

      delimiter = options["d"]
      if (delimiter is None):
         delimiter = (     "," if (any("," in text
                                       for segment in segments
                                       for text in segment))
                      else " ")

      fill_column = FillColumn(delimiter, length, first_indent, indent)
      elements    = fill_column.parse(segments)
      if (columns):
         (lines, widths, fits, spills) = fill_column.fill(elements,
                                                          columns,
                                                          options["w"])
      else:
         (lines, widths, fits, spills) = fill_column.layout(elements,
                                                            options["w"],
                                                            options["o"])
      if (options["v"]):
         print(f"{self.exec_name}: delimiter {delimiter!r}, "
               f"{len(elements)} names, {len(widths)} columns, "
               f"widths {widths}, {spills} spill", file = sys.stderr)
      for line in lines:
         print(line)
      if (not fits):
         print(f"{self.exec_name}: warning: lines exceed {length} "
               f"characters", file = sys.stderr)
         return(E_WARN)
      return(E_SUCCESS)

###########################################################################
###
### s t a r t    o f    e x e c u t i o n
###
###########################################################################

if ( __name__ == "__main__" ):
   try:
      sys.exit(MAIN(sys.argv[0]).execute(sys.argv[1:]))
   except ExceptionClass as exception:
      exception.print_error()
      sys.exit(exception.exit_code)
