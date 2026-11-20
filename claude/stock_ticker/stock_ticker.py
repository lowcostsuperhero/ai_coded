#!/usr/bin/python
# name: $Id$
# use: track one or more stock tickers with a live-updating chart
# usage: stock_ticker.py [-c] [-f] [-r seconds] [-p period]
#                        [-i interval] [TICKER ...]
#        returns to the command prompt once the chart window is open;
#        -f keeps it in the foreground instead
#        requires: pip install yfinance matplotlib pandas
#        tkinter ships with python (on RHEL/Alma/Oracle Linux:
#        dnf install python3-tkinter)

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import tkinter
from tkinter import ttk

import matplotlib
matplotlib.use("TkAgg")
import numpy
import pandas
import yfinance
from matplotlib.dates                  import DateFormatter
from matplotlib.dates                  import date2num
from matplotlib.dates                  import num2date
from matplotlib.figure                 import Figure
from matplotlib.ticker                 import FuncFormatter
from matplotlib.ticker                 import Locator
from matplotlib.ticker                 import MaxNLocator
from matplotlib.transforms             import blended_transform_factory
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.backends.backend_tkagg import NavigationToolbar2Tk

E_SUCCESS = 0
E_WARN    = 1
E_FAIL    = 2
E_USAGE   = 3

                               # set in the background copy so it does not
                               # launch yet another copy of itself
CHILD     = "STOCK_TICKER_CHILD"
READY     = "ready"
LOG       = os.path.join(tempfile.gettempdir(), "stock_ticker.log")

AUTO      = "auto"
CHARTS    = ["Line", "Candles"]
UP        = "#26a69a"
VOLUME    = "#7e57c2"
DOWN      = "#ef5350"
HINT      = "Hover over chart for details"
                               # quote table columns: (alignment, (left,
                               # right) padding); "w" = left justify,
                               # "e" = right justify
QUOTE_COLUMNS = [("w", (4, 4)),          # symbol
                 ("e", (6, 0)),          # price
                 ("e", (12, 0)),         # change
                 ("w", (3, 0)),          # (
                 ("e", (0, 0)),          # percent
                 ("w", (0, 0)),          # )
                 ("w", (14, 0)),         # Low
                 ("e", (4, 0)),          # low price
                 ("w", (8, 0)),          # High
                 ("e", (4, 0))]          # high price

                               # crosshair shows only when the mouse is
                               # this many screen pixels from a candle or
                               # a point on a line
NEAR      = 20
                               # horizontal grid: panel split into 3, 6, 9..
                               # equal gaps, as many as fit with each gap at
                               # least this many screen pixels tall
GRID_GAP  = 40

                               # period => (line interval, candle interval)
                               # used when the interval is "auto"; candles
                               # use coarser bars so each one is readable
PERIODS   = {"1d"  : ("1m" , "5m" ),
             "5d"  : ("5m" , "30m"),
             "1mo" : ("30m", "1d" ),
             "3mo" : ("1h" , "1d" ),
             "6mo" : ("1d" , "1d" ),
             "ytd" : ("1d" , "1d" ),
             "1y"  : ("1d" , "1wk"),
             "2y"  : ("1d" , "1wk"),
             "5y"  : ("1wk", "1mo"),
             "max" : ("1mo", "3mo")}

                               # bar sizes yahoo will serve for each
                               # period: 1m only goes back ~7 days, other
                               # minute bars ~60 days, 1h ~730 days
INTERVALS = {"1d"  : ["1m", "2m", "5m", "15m", "30m", "1h"],
             "5d"  : ["1m", "2m", "5m", "15m", "30m", "1h", "1d"],
             "1mo" : ["2m", "5m", "15m", "30m", "1h", "1d", "1wk"],
             "3mo" : ["1h", "1d", "1wk"],
             "6mo" : ["1h", "1d", "1wk", "1mo"],
             "ytd" : ["1h", "1d", "1wk", "1mo"],
             "1y"  : ["1h", "1d", "1wk", "1mo"],
             "2y"  : ["1h", "1d", "1wk", "1mo"],
             "5y"  : ["1d", "1wk", "1mo", "3mo"],
             "max" : ["1d", "1wk", "1mo", "3mo"]}

                               # oracle "hh24:mi Dy dd-Mon-yy"; daily and
                               # longer bars have no meaningful time of day
                               # so they drop the hh24:mi part
TIME_FMT  = "%H:%M %a %d-%b-%y"
DATE_FMT  = "%a %d-%b-%y"
                               # instrument types that trade with volume;
                               # indexes and mutual funds report none
TRADED    = ("EQUITY", "ETF")

                               # auto-refresh choices, label => seconds
REFRESH   = {"5s"  : 5,
             "10s" : 10,
             "15s" : 15,
             "30s" : 30,
             "1m"  : 60,
             "2m"  : 120,
             "5m"  : 300,
             "15m" : 900,
             "off" : 0}

###########################################################################
###
### f u n c t i o n s
###
###########################################################################

def is_intraday(interval):
   """
   post: True for minute/hour bars ("5m", "1h"), False for "1mo" etc.
   """
   return(     interval.endswith("h")
          or  (interval.endswith("m") and not interval.endswith("mo")))

###########################################################################
def date_format(interval):
   return(TIME_FMT if (is_intraday(interval)) else DATE_FMT)

###########################################################################
def traded_only(ticker, history):
   """
   use: drop bars that are not real trades
   post: returns (history, None), or (None, reason) if nothing is left
   imp: before a stock's first trade (an ipo still in price discovery)
        yahoo can return bars built from quotes or another security
        that once used the symbol; those bars have zero volume and/or
        predate the firstTradeDate yahoo reports, so remove them.
        indexes and funds never report volume, so only stocks and etfs
        are volume-filtered
   """
   history = history.dropna(subset = ["Open", "High", "Low", "Close"])
   try:
      meta = ticker.history_metadata or {}
   except Exception:
      meta = {}

   first = meta.get("firstTradeDate")
   if (isinstance(first, (int, float)) and len(history)):
      first = pandas.Timestamp(int(first), unit = "s", tz = "UTC")
      if (history.index.tz is None):
         first = first.tz_convert(None)
      else:
         first = first.tz_convert(history.index.tz)
      history = history[history.index >= first.normalize()]

   kind = str(meta.get("instrumentType", "EQUITY")).upper()
   if (kind in TRADED and "Volume" in history.columns):
      history = history[history["Volume"] > 0]

   if (history.empty):
      return(None, "no trades yet")
   return(history, None)

###########################################################################
def volume_label(value, tick_number):
   """
   post: 1500 -> "1.5K", 2534000 -> "2.53M", 3e9 -> "3B"
   """
   for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
      if (abs(value) >= limit):
         return(f"{value / limit:.3g}{suffix}")
   return(f"{value:.3g}")

###########################################################################
def seconds_label(seconds):
   """
   use: turn -r seconds into a REFRESH label, adding one if needed
   """
   for label, value in REFRESH.items():
      if (value == seconds):
         return(label)
   label          = f"{seconds}s"
   REFRESH[label] = seconds
   return(label)

###########################################################################
###
### c l a s s e s
###
###########################################################################

class GridLocator(Locator):
   """
   name: GridLocator
   use: tick (and so grid line) positions that divide an axis into a
        multiple of 3 equal gaps: 3 gaps (2 lines) on a short panel, 6
        gaps (5 lines) once it is twice as tall, then 9, 12 ...
   usage: axes.yaxis.set_major_locator(GridLocator())
   post: lines fall on exact fractions of the visible range, not on
         round numbers, so labels read like 236.94
   imp: recomputed on every redraw, so resizing the window, zooming or
        new prices move the lines with the panel
   """

   ########################################################################
   def __call__(self):
      low, high = self.axis.get_view_interval()
      return(self.tick_values(low, high))

   ########################################################################
   def tick_values(self, vmin, vmax):
      gaps = self.gaps()
      step = (vmax - vmin) / gaps
      return([vmin + step * line for line in range(1, gaps)])

   ########################################################################
   def gaps(self):
      """
      post: the largest multiple of 3 gaps, minimum 3, that keeps each
            gap at least GRID_GAP pixels tall
      """
      if (self.axis is None):
         return(3)
      height = self.axis.axes.get_window_extent().height
      count  = int(height // GRID_GAP)
      return(max(3, count - count % 3))

###########################################################################

class StockTicker(object):
   """
   name: StockTicker
   use: tkinter window with ticker entry, price quote, hover readout and
        matplotlib line or candlestick chart
   pre: tkinter display available (DISPLAY set on linux)
   imp: yahoo is queried on a worker thread so the gui never freezes;
        results are handed back to the gui thread via root.after()
   """

   ########################################################################
   def __init__(self, root, tickers, refresh_seconds = 60,
                chart = "Line", period = "1d", interval = AUTO):
      self.root     = root
      self.after_id = None
      self.busy     = False
                               # what is on screen, used by the hover
                               # readout: [(axes, symbol, history), ...]
      self.panels   = []
      self.crosshair = {}
      self.hosts    = {}
      self.relative = False
      self.candles  = False
      self.interval = ""
      self.timeline = pandas.DatetimeIndex([])
      self.waiting  = []
      self.quote_rows = 0

      root.title("Stock Ticker")
      root.geometry("1100x700")

      ###
      ### row 1: what to chart
      ###
      bar = ttk.Frame(root, padding = (6, 6, 6, 2))
      bar.pack(side = tkinter.TOP, fill = tkinter.X)

      ttk.Label(bar, text = "Ticker(s):").pack(side = tkinter.LEFT)
      self.ticker_var = tkinter.StringVar(value = " ".join(tickers))
      entry = ttk.Entry(bar, textvariable = self.ticker_var, width = 26)
      entry.pack(side = tkinter.LEFT, padx = (4, 10))
      entry.bind("<Return>", lambda event: self.refresh())

      self.chart_var = tkinter.StringVar(value = chart)
      self.combo(bar, "Chart:", self.chart_var, CHARTS, 8,
                 lambda event: self.refresh())

      ttk.Button(bar, text = "Refresh now",
                 command = self.refresh).pack(side = tkinter.LEFT,
                                              padx = 4)

      self.status_var = tkinter.StringVar(value = "")
      ttk.Label(bar, textvariable = self.status_var,
                foreground = "gray").pack(side = tkinter.RIGHT)

      ###
      ### row 2: time scale and refresh rate
      ###
      bar = ttk.Frame(root, padding = (6, 2, 6, 4))
      bar.pack(side = tkinter.TOP, fill = tkinter.X)

      self.period_var = tkinter.StringVar(value = period)
      self.combo(bar, "Time span:", self.period_var, list(PERIODS), 5,
                 self.period_changed)

      self.interval_var = tkinter.StringVar(value = interval)
      self.interval_box = self.combo(bar, "Bar size:",
                                     self.interval_var, [], 5,
                                     lambda event: self.refresh())
      self.set_intervals()

      self.refresh_var = tkinter.StringVar(
                            value = seconds_label(refresh_seconds))
      self.combo(bar, "Auto-refresh every:", self.refresh_var,
                 list(REFRESH), 5, lambda event: self.schedule())

      ###
      ### row 3: latest quote per ticker; row 4: hover readout
      ###
      self.quote_frame = ttk.Frame(root, padding = (6, 0))
      self.quote_frame.pack(side = tkinter.TOP, fill = tkinter.X)

      self.hover_var = tkinter.StringVar(value = HINT)
      self.hover     = tkinter.Label(root, textvariable = self.hover_var,
                                     anchor = "w", padx = 8, pady = 3,
                                     font = ("TkFixedFont", 11),
                                     background = "#20242c",
                                     foreground = "#e0e0e0")
      self.hover.pack(side = tkinter.TOP, fill = tkinter.X)

      self.figure = Figure(figsize = (10, 5), dpi = 100)
      self.canvas = FigureCanvasTkAgg(self.figure, master = root)
      NavigationToolbar2Tk(self.canvas, root)
      self.canvas.get_tk_widget().pack(fill = tkinter.BOTH, expand = True)
      self.canvas.mpl_connect("motion_notify_event", self.on_hover)
      self.canvas.mpl_connect("axes_leave_event",    self.on_leave)

      root.protocol("WM_DELETE_WINDOW", self.close)
      self.refresh()

   ########################################################################
   def combo(self, parent, text, variable, values, width, command):
      """
      use: label + read-only combobox packed left
      post: returns the combobox
      """
      ttk.Label(parent, text = text).pack(side = tkinter.LEFT)
      box = ttk.Combobox(parent, textvariable = variable,
                         values = values, width = width,
                         state = "readonly")
      box.pack(side = tkinter.LEFT, padx = (4, 12))
      box.bind("<<ComboboxSelected>>", command)
      return(box)

   ########################################################################
   def set_intervals(self):
      """
      use: offer only the bar sizes yahoo allows for the time span
      post: interval_var is AUTO or one of the offered values
      """
      allowed = [AUTO] + INTERVALS[self.period_var.get()]
      self.interval_box.configure(values = allowed)
      if (self.interval_var.get() not in allowed):
         self.interval_var.set(AUTO)

   ########################################################################
   def period_changed(self, event):
      self.set_intervals()
      self.refresh()

   ########################################################################
   def tickers(self):
      """
      post: returns list of upper-case symbols, commas or spaces allowed
      """
      text = self.ticker_var.get().replace(",", " ").upper()
      return([symbol for symbol in text.split() if (symbol)])

   ########################################################################
   def refresh(self):
      """
      use: start a background fetch unless one is already running
      """
      if (self.busy):
         return
      symbols = self.tickers()
      if (not symbols):
         self.status_var.set("enter a ticker symbol")
         return
      self.busy = True
      self.status_var.set("fetching...")
      period   = self.period_var.get()
      chart    = self.chart_var.get()
      interval = self.interval_var.get()
      if (interval == AUTO):
         interval = PERIODS[period][CHARTS.index(chart)]
      threading.Thread(target = self.fetch,
                       args   = (symbols, period, interval, chart),
                       daemon = True).start()

   ########################################################################
   def fetch(self, symbols, period, interval, chart):
      """
      use: worker thread; download history for each symbol
      post: results keeps every symbol, in order: a DataFrame, or a
            reason string when there is nothing to chart yet
      imp: DO NOT touch tk widgets here, tk is not thread safe
      """
      results = {}
      for symbol in symbols:
         try:
            ticker  = yfinance.Ticker(symbol)
            history = ticker.history(period = period, interval = interval)
            if (history.empty):
               results[symbol] = "no data yet"
               continue
            history, reason = traded_only(ticker, history)
            results[symbol] = reason or history
         except Exception as exception:
            results[symbol] = f"error: {exception}"
      self.root.after(0, self.draw, results, period, interval, chart)

   ########################################################################
   def draw(self, results, period, interval, chart):
      """
      use: gui thread; redraw quotes and chart from fetched data
      post: every symbol gets a place on the chart; one with no data yet
            gets an empty panel (candles) or legend entry (line) that the
            next auto-refresh fills in once trades show up
      """
      self.busy = False
      for child in self.quote_frame.winfo_children():
         child.destroy()

      real = {symbol: history for symbol, history in results.items()
              if (not isinstance(history, str))}

      self.figure.clear()
      self.panels    = []
      self.crosshair = {}
      self.hosts     = {}
      self.interval  = interval
      self.candles   = (chart == "Candles")
                               # with several tickers on a line chart plot
                               # % change so they share one scale
      self.relative  = (len(real) > 1 and not self.candles)
      self.hover_var.set(HINT)

      self.quote_rows = 0
      ttk.Label(self.quote_frame,
                text = f"Change over {period} ({interval} bars):").grid(
                   row = 0, column = 0, sticky = "nw", padx = (0, 4))
      if (not self.candles):
         axes = self.figure.add_subplot(111)
      self.waiting = []
                               # stacked candle panels share one timeline,
                               # every bar time seen for any ticker, so a
                               # ticker that started trading late (an ipo)
                               # lines up under the others and an empty
                               # panel shows the span it will fill in
      self.timeline = pandas.DatetimeIndex([])
      for history in real.values():
         self.timeline = self.timeline.union(history.index)
      top = None

      for number, (symbol, history) in enumerate(results.items(), 1):
         if (self.candles):
                               # one stacked panel per ticker
            axes = self.figure.add_subplot(
                      len(results), 1, number,
                      sharex = top)
            top  = top or axes
            axes.set_ylabel(symbol)
            axes.grid(True, alpha = 0.3)
            if (number < len(results)):
               axes.tick_params(axis = "x", labelbottom = False)

         if (isinstance(history, str)):
            self.draw_empty(axes, symbol, history)
            continue

         close = history["Close"]
         first = float(close.iloc[0])
         last  = float(close.iloc[-1])
         delta = last - first
         pct   = (delta / first * 100) if (first) else 0.0
         color = "green" if (delta >= 0) else "red"
         low   = float(history["Low"].min())
         high  = float(history["High"].max())
                               # split low..high into thirds: the price in
                               # the lower third colours the low, in the top
                               # third the high (the other one black), in
                               # the middle third both are blue
         third = (last - low) / (high - low) if (high > low) else 0.5
         if (third < 1 / 3):
            low_color, high_color = color, "black"
         elif (third > 2 / 3):
            low_color, high_color = "black", color
         else:
            low_color, high_color = "blue", "blue"
         self.quote(color, symbol,
                    f"{last:,.2f}",
                    f"{delta:+,.2f}",
                    "(", f"{pct:+.2f}%", ")",
                    ("Low",  "black"), (f"{low:,.2f}",  low_color),
                    ("High", "black"), (f"{high:,.2f}", high_color))

         self.hosts[axes] = axes
         axes.yaxis.set_major_locator(GridLocator())
         axes.yaxis.set_major_formatter(
            FuncFormatter(lambda value, tick: f"{value:,.2f}"))
         if (self.candles):
            self.draw_candles(axes, history, interval, self.timeline)
            self.draw_volume(axes, self.timeline.get_indexer(history.index),
                             history, VOLUME)
         else:
            series = (     (close / first - 1) * 100 if (self.relative)
                      else close)
            price  = axes.plot(history.index, series, label = symbol,
                               linewidth = 1.5)[0]
            self.draw_volume(axes, history.index, history,
                             price.get_color())
         self.panels.append((axes, symbol, history))

      if (self.candles):
                               # slant the long date labels like the line
                               # chart does; autofmt_xdate is not used
                               # because it hides labels on upper panels
         for axes in self.figure.axes:
            if (not real):
               axes.set_xticks([])
            for text in axes.get_xticklabels():
               text.set_rotation(30)
               text.set_horizontalalignment("right")
      else:
         axes.set_ylabel("% change" if (self.relative) else "price")
         axes.grid(True, alpha = 0.3)
         axes.legend(loc = "upper left")
         if (self.waiting):
                               # one shared chart, so tickers without data
                               # are listed in a box in the middle of it;
                               # their lines appear here once they trade
            axes.text(0.5, 0.5, "\n".join(self.waiting),
                      transform = axes.transAxes,
                      horizontalalignment = "center",
                      verticalalignment   = "center",
                      color = "gray", fontsize = 12, zorder = 5,
                      bbox = {"boxstyle"  : "round,pad=0.6",
                              "facecolor" : "white",
                              "edgecolor" : "lightgray",
                              "alpha"     : 0.85})
         if (real):
            axes.xaxis.set_major_formatter(
               DateFormatter(date_format(interval),
                             tz = next(iter(real.values())).index.tz))
            self.figure.autofmt_xdate()
         else:
            axes.set_xticks([])
            axes.set_yticks([])

                               # hidden crosshair per price axes, moved and
                               # shown by on_hover: a vertical line on the
                               # bar, a horizontal line edge to edge, a tag
                               # on the price scale and one on the volume
                               # scale; they start at the lower left corner,
                               # at 0 they would stretch a date axis to 1970
      for axes in {panel[0] for panel in self.panels}:
         left, bottom = axes.get_xlim()[0], axes.get_ylim()[0]
                               # solid, one screen pixel wide: linewidth is
                               # in points (72 per inch) and the window's
                               # dpi varies, so aim for 0.75 pixel, which
                               # agg draws as 1 pixel with antialiasing
                               # off (a full 1.0 pixel comes out as 3)
         style        = {"color"       : "#a0a0a0",
                         "linewidth"   : 0.75 * 72 / self.figure.dpi,
                         "linestyle"   : "-",
                         "antialiased" : False,
                         "visible"     : False}
         self.crosshair[axes] = (axes.axvline(left, **style),
                                 axes.axhline(bottom, **style),
                                 self.scale_tag(axes, 0, "right"),
                                 self.scale_tag(axes, 1, "left"))
      self.figure.tight_layout()
      self.canvas.draw_idle()
      self.status_var.set(
         "updated " + pandas.Timestamp.now().strftime("%H:%M:%S"))
      self.schedule()

   ########################################################################
   def scale_tag(self, axes, edge, side):
      """
      use: hidden value tag that sits just outside the left (edge 0) or
           right (edge 1) side of axes, at a data y set later by on_hover
      post: returns the matplotlib Text
      """
      return(axes.text(edge, axes.get_ylim()[0], "",
                       transform = blended_transform_factory(
                                      axes.transAxes, axes.transData),
                       horizontalalignment = side,
                       verticalalignment   = "center",
                       fontsize = 8, color = "white",
                       clip_on = False, zorder = 10, visible = False,
                       bbox = {"boxstyle"  : "square,pad=0.25",
                               "facecolor" : "#20242c",
                               "edgecolor" : "#20242c"}))

   ########################################################################
   def draw_volume(self, host, x, history, color):
      """
      use: overlay volume as a line on a second y axis, scale on the right
      pre: x holds the plot positions of history's rows on host
      post: twin axes registered in self.hosts so hovering over it acts
            as hovering over host
      imp: the line chart has one twin shared by every ticker; candle
           panels get one each. the volume scale is stretched to 3x the
           peak so the line keeps to the bottom third, under the prices.
           stocks before their first trade, indexes and funds report no
           volume, so nothing is drawn for them
      """
      if ("Volume" not in history.columns):
         return
      volume = history["Volume"].fillna(0).to_numpy(dtype = float)
      if (not volume.any()):
         return

      twin = self.twin_of(host)
      twin.plot(x, volume, color = color, linewidth = 1, alpha = 0.55)
      top  = max(twin.get_ylim()[1] / 3, volume.max())
      twin.set_ylim(0, top * 3)

   ########################################################################
   def twin_of(self, host):
      """
      post: returns the volume axes for host, creating it on first use
      """
      for twin, owner in self.hosts.items():
         if (owner is host and twin is not host):
            return(twin)
      twin = host.twinx()
      twin.set_ylabel("volume", color = "gray")
      twin.tick_params(axis = "y", labelsize = 8, colors = "gray")
      twin.yaxis.set_major_formatter(FuncFormatter(volume_label))
                               # same lines as the price scale, so each
                               # grid line has a volume label on the right
      twin.yaxis.set_major_locator(GridLocator())
      twin.grid(False)
                               # twinx puts volume in front; swap so the
                               # price line and candles stay on top, and
                               # clear host's background so volume shows
      host.set_zorder(twin.get_zorder() + 1)
      host.patch.set_visible(False)
                               # toolbar's (x, y) readout shows the price,
                               # not the volume, under the mouse
      def coordinates(x, y):
         x, y = host.transData.inverted().transform(
                   twin.transData.transform((x, y)))
         return(host.format_coord(x, y))
      twin.format_coord = coordinates
      self.hosts[twin]  = host
      return(twin)

   ########################################################################
   def quote(self, color, symbol, *cells):
      """
      use: add one ticker's row to the quote table above the chart
      usage: quote(color, symbol, price, change, "(", percent, ")",
                   "Low", low, "High", high)
             quote(color, symbol, reason)
             any cell may be a (text, colour) pair to override color
      post: each piece in its own column, aligned per QUOTE_COLUMNS so
            every ticker's numbers line up; a lone reason ("no trades
            yet") is left-aligned and spans the rest of the row
      imp: one row per ticker so several tickers never run off the
           right edge of the window
      """
      row  = self.quote_rows
      font = ("TkDefaultFont", 11, "bold")
      self.quote_rows += 1
      if (len(cells) == 1):
         layout = [("w", (4, 4))] + [("w", (6, 0))]
         spans  = [1, len(QUOTE_COLUMNS) - 1]
      else:
         layout = QUOTE_COLUMNS
         spans  = [1] * len(QUOTE_COLUMNS)
      for column, (text, (sticky, padx), span) in enumerate(
                                                    zip((symbol,) + cells,
                                                        layout, spans), 1):
         text, shade = text if (isinstance(text, tuple)) else (text, color)
         ttk.Label(self.quote_frame, text = text, foreground = shade,
                   font = font).grid(row        = row,
                                     column     = column,
                                     columnspan = span,
                                     sticky     = sticky,
                                     padx       = padx)

   ########################################################################
   def draw_empty(self, axes, symbol, reason):
      """
      use: placeholder for a ticker with nothing to chart yet
      post: gray "SYMBOL reason" in the quote row; candles get an empty
            panel with the reason in the middle; the line chart gets a
            legend entry and a line in the message box draw() puts in
            the middle of the chart (the line itself appears once there
            is data)
      """
      self.quote("gray", symbol, reason)
      if (self.candles):
         axes.set_yticks([])
         axes.text(0.5, 0.5, f"{symbol}: {reason}",
                   transform = axes.transAxes,
                   horizontalalignment = "center",
                   verticalalignment   = "center",
                   color = "gray", fontsize = 12)
      else:
         axes.plot([], [], label = f"{symbol} ({reason})",
                   color = "lightgray")
         self.waiting.append(f"{symbol}: {reason}")

   ########################################################################
   def draw_candles(self, axes, history, interval, timeline):
      """
      use: draw ohlc candlesticks: thin wick from low to high, body from
           open to close, UP colour when close >= open else DOWN
      pre: history has Open, High, Low, Close columns; timeline holds
           every timestamp in history.index
      imp: bars are plotted at their position in timeline, not at their
           timestamps, so nights and weekends do not leave gaps; the
           tick formatter maps positions back to dates
      """
      opens  = history["Open"].to_numpy(dtype = float)
      highs  = history["High"].to_numpy(dtype = float)
      lows   = history["Low"].to_numpy(dtype = float)
      closes = history["Close"].to_numpy(dtype = float)
      x      = timeline.get_indexer(history.index)
      colors = numpy.where(closes >= opens, UP, DOWN)

                               # a doji (open == close) still needs a
                               # visible sliver of body
      sliver = max(highs.max() - lows.min(), 0.01) * 0.002
      axes.vlines(x, lows, highs, colors = colors, linewidth = 1)
      axes.bar(x, numpy.maximum(numpy.abs(closes - opens), sliver),
               bottom    = numpy.minimum(opens, closes),
               width     = 0.6,
               color     = colors,
               edgecolor = colors,
               linewidth = 0.5)

      dates  = timeline
      layout = date_format(interval)
      def label(position, tick_number):
         index = int(round(position))
         return(     dates[index].strftime(layout)
                if (0 <= index < len(dates))
                else "")
      axes.xaxis.set_major_locator(MaxNLocator(7, integer = True))
      axes.xaxis.set_major_formatter(FuncFormatter(label))
      axes.set_xlim(-1, len(dates))
      axes.tick_params(axis = "x", labelsize = 8)

   ########################################################################
   def nearest_row(self, history, x):
      """
      use: map the mouse x position to a row of history
      post: returns row number, or None if the mouse is off the data
      imp: candle x is a position on the shared timeline, matched
           exactly (a ticker may have no bar there); line x is a
           matplotlib date number matched to the nearest (tz-aware) bar
      """
      if (self.candles):
         spot = int(round(x))
         if (not 0 <= spot < len(self.timeline)):
            return(None)
         row = int(history.index.get_indexer([self.timeline[spot]])[0])
      else:
         moment = pandas.Timestamp(num2date(x))
         index  = history.index
         if (index.tz is None):
            moment = moment.tz_convert(None)
         else:
            moment = moment.tz_convert(index.tz)
         row = int(index.get_indexer([moment], method = "nearest")[0])
      return(row if (0 <= row < len(history)) else None)

   ########################################################################
   def on_hover(self, event):
      """
      use: move the crosshair to the mouse and fill the readout line with
           the bar under it
      imp: the volume axes sits on top of the price axes, so matplotlib
           reports the volume axes and volume y; map both back to the
           price axes. on a multi-ticker line chart all lines share one
           axes, so pick the line nearest the mouse. distances are in
           screen pixels so NEAR means the same at any zoom or scale
      """
      host = self.hosts.get(event.inaxes)
      if (host is None or host not in self.crosshair):
         self.on_leave(event)
         return
      x, y = host.transData.inverted().transform((event.x, event.y))
                               # volume at the mouse height, if this panel
                               # has a volume scale
      at_volume = None
      for twin, owner in self.hosts.items():
         if (owner is host and twin is not host):
            at_volume = twin.transData.inverted().transform(
                        (event.x, event.y))[1]

      best = None
      for axes, symbol, history in self.panels:
         if (axes is not host):
            continue
         row = self.nearest_row(history, x)
         if (row is None):
            continue
         moment = history.index[row]
         if (self.candles):
                               # nearest point of the candle's wick: the
                               # mouse height clamped to low..high
            spot  = self.timeline.get_loc(moment)
            value = min(max(y, float(history["Low"].iloc[row])),
                        float(history["High"].iloc[row]))
         else:
            spot  = date2num(moment)
            close = float(history["Close"].iloc[row])
            first = float(history["Close"].iloc[0])
            value = (     (close / first - 1) * 100 if (self.relative)
                     else close)
         px, py = host.transData.transform((spot, value))
         gap    = numpy.hypot(px - event.x, py - event.y)
         if (gap <= NEAR and (best is None or gap < best[0])):
            best = (gap, symbol, history, row)

      if (best is None):
                               # blank spot, nowhere near the data
         self.on_leave(event)
         return

      gap, symbol, history, row = best
      bar     = history.iloc[row]
      moment  = history.index[row]
      layout  = date_format(self.interval)
      volume  = bar.get("Volume", float("nan"))
      volume  = "n/a" if (pandas.isna(volume)) else f"{int(volume):,}"
      self.hover_var.set(f"{symbol:<6} {moment.strftime(layout)}   "
                         f"Open {bar['Open']:,.2f}   "
                         f"Close {bar['Close']:,.2f}   "
                         f"Low {bar['Low']:,.2f}   "
                         f"High {bar['High']:,.2f}   "
                         f"Volume {volume}")
                               # snap the vertical line to the bar itself
      vertical = (     self.timeline.get_loc(moment) if (self.candles)
                  else date2num(moment))

                               # crosshair only in the panel being hovered;
                               # the horizontal line runs from the price
                               # scale on the left to the volume scale on
                               # the right, each with a value tag
      for axes, (upright, level, price, size) in self.crosshair.items():
         here = (axes is host)
         upright.set_visible(here)
         level.set_visible(here)
         price.set_visible(here)
         size.set_visible(here and at_volume is not None)
         if (not here):
            continue
         upright.set_xdata([vertical, vertical])
         level.set_ydata([y, y])
         price.set_y(y)
         price.set_text(f"{y:+.2f}%" if (self.relative) else f"{y:,.2f}")
         if (at_volume is not None):
            size.set_y(y)
            size.set_text(volume_label(max(at_volume, 0), None))
      self.canvas.draw_idle()

   ########################################################################
   def on_leave(self, event):
      self.hover_var.set(HINT)
      for guides in self.crosshair.values():
         for guide in guides:
            guide.set_visible(False)
      self.canvas.draw_idle()

   ########################################################################
   def schedule(self):
      """
      post: exactly one pending auto-refresh, or none when set to "off"
      """
      if (self.after_id):
         self.root.after_cancel(self.after_id)
         self.after_id = None
      seconds = REFRESH[self.refresh_var.get()]
      if (seconds):
         self.after_id = self.root.after(seconds * 1000, self.refresh)

   ########################################################################
   def close(self):
      if (self.after_id):
         self.root.after_cancel(self.after_id)
      self.root.destroy()

###########################################################################
###
### s t a r t    o f    e x e c u t i o n
###
###########################################################################

def detach(prog, argv):
   """
   name: detach
   use: start this script again as a background process with no console
        and return once its window is open, so the prompt comes back
   usage: detach(parser.prog, sys.argv[1:])
   post: E_SUCCESS once the background copy reports READY on its stdout;
         E_FAIL if it exits first, after echoing its log to stderr
   imp: windows: runs pythonw.exe (no console window) as a detached
        process in its own process group, so closing the cmd window or
        ctrl-c there does not take the chart down. linux: a new session
        does the same. the copy's stderr goes to LOG for later problems
   """
   python = sys.executable
   extra  = {}
   if (os.name == "nt"):
      quiet = os.path.join(os.path.dirname(python), "pythonw.exe")
      if (os.path.exists(quiet)):
         python = quiet
                               # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
      extra["creationflags"] = 0x00000008 | 0x00000200
   else:
      extra["start_new_session"] = True

   with open(LOG, "w") as log:
      child = subprocess.Popen([python, os.path.abspath(sys.argv[0])] + argv,
                               stdin     = subprocess.DEVNULL,
                               stdout    = subprocess.PIPE,
                               stderr    = log,
                               env       = dict(os.environ, **{CHILD: "1"}),
                               close_fds = True,
                               text      = True,
                               **extra)
                               # blocks until the window is up or the copy
                               # dies (end of file)
   line = child.stdout.readline().strip()
   child.stdout.close()
   if (line == READY):
      return(E_SUCCESS)

   child.wait()
   with open(LOG) as log:
      sys.stderr.write(log.read())
   print(f"{prog}: chart failed to start (exit {child.returncode}); "
         f"see {LOG}", file = sys.stderr)
   return(E_FAIL)

###########################################################################
def main(argv):
   parser = argparse.ArgumentParser(
               description = "live stock ticker chart")
   parser.add_argument("-c", dest = "candles", action = "store_true",
                       help = "start in candlestick mode")
   parser.add_argument("-f", dest = "foreground", action = "store_true",
                       help = "stay in the foreground (keeps the prompt)")
   parser.add_argument("-r", dest = "refresh", type = int, default = 60,
                       help = "auto-refresh seconds, 0 = off (default 60)")
   parser.add_argument("-p", dest = "period", default = "1d",
                       choices = list(PERIODS),
                       help = "time span shown (default 1d)")
   parser.add_argument("-i", dest = "interval", default = AUTO,
                       help = "bar size, e.g. 5m 1h 1d (default auto)")
   parser.add_argument("tickers", nargs = "*", default = ["AAPL"],
                       help = "ticker symbols, e.g. AAPL MSFT ORCL")
   args = parser.parse_args(argv)
   if (args.refresh < 0):
      parser.error("-r must be 0 or more")

   child = (os.environ.get(CHILD) == "1")
   if (not args.foreground and not child):
      return(detach(parser.prog, argv))

   try:
      root = tkinter.Tk()
   except tkinter.TclError as exception:
      print(f"{parser.prog}: cannot open display: {exception}",
            file = sys.stderr)
      return(E_FAIL)
   StockTicker(root, args.tickers, args.refresh,
               "Candles" if (args.candles) else "Line",
               args.period, args.interval)

   if (child):
                               # window is on screen: tell the waiting
                               # parent, then stop using the pipe (the
                               # parent is gone); later output goes to LOG
      root.update()
      sys.stdout.write(READY + "\n")
      sys.stdout.flush()
      sys.stdout.close()
      sys.stdout = sys.stderr
   root.mainloop()
   return(E_SUCCESS)

if ( __name__ == "__main__" ):
   sys.exit(main(sys.argv[1:]))
