#!/usr/bin/python
# name: $Id: stock_tracker.py 3.main-3 16:27:45 26-Nov-2026 claude-opus-5-5 $
# use: local INTRADAY stock tracker with a live chart window in the style
#      of stock_ticker.py. It polls 5-minute bars
#      for the stocks on its own watchlist (plus any open positions),
#      works out each day's low, high and expected range, and issues
#         BUY  when the price appears to have bottomed out and turned up
#              (a "rebound"), or is on a trajectory to set a new high
#              for the day (a "breakout");
#         SELL when a held stock reaches its volatility-based profit
#              target, hits its trailing stop, turns down, or plateaus.
#      It keeps a log of the buys and sells you record (shares, price,
#      time; correctable later) and reports profit and loss on them.
#      Everything is computed on this machine. The only network traffic
#      is the download of PUBLIC prices from Yahoo Finance through the
#      yfinance package (ticker symbols only). The watchlist, trade log,
#      settings and price cache live in ~/.stock_tracker.
# usage: stock_tracker.py [-c] [-f] [-r seconds] [-p span] [-t] [-s] [-d]
#                         [TICKER ...]
#        returns to the command prompt once the chart window is open;
#        -f keeps it in the foreground instead; -t opens the text prompt
#        (stock_picker style: simulate, follow, asof, practice trades)
#        requires: pip install numpy pandas yfinance matplotlib
#        tkinter ships with python (on RHEL/Alma/Oracle Linux:
#        dnf install python3-tkinter)
# imp: expected range: the daily ATR(14) of the PRIOR days says how far a
#      typical day travels, so with the day's low L and high H so far the
#      expected high is max(H, L + ATR) and the expected low is
#      min(L, H - ATR). The trend is a least-squares fit of log price
#      over the last trend_bars bars; its t-statistic says how clean the
#      trend is. Decisions use completed bars only. Yahoo serves about 60
#      days of 5-minute bars, which bounds 'simulate', and its intraday
#      quotes may be delayed. Research tool only; NOT financial advice.

import argparse
import cmd
import json
import math
import os
import pickle
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import warnings

import numpy as np
import pandas as pd

from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

###########################################################################
###
### c o n s t a n t s
###
###########################################################################

E_SUCCESS  = 0
E_WARN     = 1
E_FAIL     = 2
E_USAGE    = 3

REVISION   = "$Revision: 3.main-3 $"
SELF       = os.path.splitext(os.path.basename(sys.argv[0]))[0]
SELFPREFIX = os.path.dirname(os.path.abspath(sys.argv[0]))

TZ         = ZoneInfo("America/New_York")
BAR_MIN    = 5
OPEN_MIN   = 9 * 60 + 30
LAST_MIN   = 15 * 60 + 55
CLOSE_MIN  = 16 * 60
KEEP_DAYS  = 60
KINDS      = {1: "rebound", 2: "breakout"}
EXIT_NAMES = dict(target  = "profit target",
                  stop    = "trailing stop",
                  down    = "turning down",
                  plateau = "plateau",
                  eod     = "end of day",
                  open    = "still open")
SYNTH      = ["SYNA", "SYNB", "SYNC", "SYND"]

HOME       = os.path.join(os.path.expanduser("~"), ".stock_tracker")
INTRADAY   = os.path.join(HOME, "intraday.pkl")
DAILY      = os.path.join(HOME, "daily.pkl")
LEDGER     = os.path.join(HOME, "ledger.json")
WATCH      = os.path.join(HOME, "watchlist.json")
SETTINGS   = os.path.join(HOME, "settings.json")

                                # every tunable of the engine; 'set'
                                # changes them and they are kept in
                                # SETTINGS between sessions
DEFAULTS = dict(poll         = 300,
                skip_open    = 3,
                no_buy_after = 1530,
                bottom_zone  = 0.30,
                confirm_bars = 3,
                rsi_low      = 35.0,
                rsi_bounce   = 10.0,
                near_high    = 0.10,
                trend_bars   = 12,
                t_min        = 2.0,
                ahead_bars   = 6,
                max_used     = 1.25,
                vol_min      = 1.0,
                target_atr   = 0.80,
                trail_atr    = 0.50,
                min_hold     = 6,
                plateau_bars = 12,
                t_flat       = 1.0,
                eod_exit     = 0,
                slip         = 0.05,
                bell         = 1)

INTEGERS = ("poll", "skip_open", "no_buy_after", "confirm_bars",
            "trend_bars", "ahead_bars", "min_hold", "plateau_bars",
            "eod_exit", "bell")

SETTING_HELP = dict(
   poll         = "seconds between 'monitor' polls (60 minimum)",
   skip_open    = "bars after the open with no BUY (opening noise)",
   no_buy_after = "no new BUY at or after this time, HHMM",
   bottom_zone  = "rebound: price in this lowest part of the expected "
                  "range (0-1)",
   confirm_bars = "rebound: bars with no new day low before a bottom is "
                  "called",
   rsi_low      = "rebound: RSI(14) must have been at or below this "
                  "recently",
   rsi_bounce   = "rebound: RSI points recovered from that low",
   near_high    = "breakout: price within this many ATRs of the day high",
   trend_bars   = "bars in the trend regression",
   t_min        = "trend t-statistic for a breakout or a down-turn",
   ahead_bars   = "bars ahead for the projected price and range",
   max_used     = "breakout: no BUY once the day range used exceeds this "
                  "many ATRs",
   vol_min      = "breakout: volume vs. usual for the time of day "
                  "(0 = ignore)",
   target_atr   = "profit target above the buy, in daily ATRs (0 = off)",
   trail_atr    = "trailing stop below the highest price since the buy, "
                  "in ATRs (0 = off)",
   min_hold     = "bars held before a plateau can trigger a SELL",
   plateau_bars = "bars without a new high since the buy that make a "
                  "plateau",
   t_flat       = "plateau: trend t-statistic must be inside +/- this",
   eod_exit     = "1 = sell at the last bar of the day",
   slip         = "backtest slippage per side, percent",
   bell         = "1 = ring the terminal bell on 'monitor' alerts")

                                # command help in the layout of
                                # stock_picker.py: the command and its
                                # arguments, then what it does
HELP = dict(
   asof="asof DATE [HH:MM]|-N|+N|now   pretend it is that bar (DATE alone "
        "is that\n                          day's close; N = 5-minute "
        "bars back/forward), or now",
   nextbar="nextbar [N]          advance the as-of time N bars (default 1)",
   nextday="nextday              advance the as-of time to the same time "
           "on the next\n                          trading day",
   recommend="recommend [N]        BUY signals (rebound / breakout) with "
             "target and stop,\n                          then sells for "
             "held positions ('recommendation(s)'\n"
             "                          and 'scan' work too)",
   track="track [N]            record the BUY signals as open positions "
         "(1 share each)",
   buy="buy TICKER [price] [shares]   record a buy (price defaults to the "
       "current\n                          price, shares to 1)",
   sell="sell TICKER [price] [shares|all]   record a sale (defaults: the "
        "current\n                          price, 1 share; the earliest "
        "shares bought are\n                          sold first)",
   positions="positions            positions held at the as-of time (from "
             "your real ledger\n                          by buy/sell time), "
             "SELL/HOLD, shares sold and profit\n                          "
             "and loss; 'position' works too",
   pnl="pnl                  profit and loss per stock: unrealized on the "
       "shares\n                          held, realized on the shares "
       "sold ('profit' works too)",
   trades="trades               every buy and sell recorded, with its #",
   edittrade="edittrade # [shares=N] [price=P] [time=YYYY-MM-DD HH:MM]\n"
             "                          correct a trade after the fact",
   deltrade="deltrade #           remove a trade recorded by mistake",
   realpositions="realpositions        the real ledger's open positions and "
                 "SELL/HOLD on the\n                          latest bar "
                 "(ignores asof)",
   savepositions="savepositions        write practice buys/sells made while "
                 "an as-of time\n                          was active into "
                 "the real ledger",
   compare="compare [N]          judge the signals at the as-of time "
           "against what happened\n                          over the next "
           "N bars and by the close",
   range="range [TICKER ...]   the day's low and high, the expected range "
         "and where\n                          the price is headed",
   follow="follow TICKER [DATE]  walk one stock through a day bar by bar "
          "with its signals\n                          and trades "
          "('replay' works too)",
   monitor="monitor [quiet]      poll every 'poll' seconds and print ALERTs "
           "until Ctrl-C;\n                          from an as-of time it "
           "replays up to now first",
   simulate="simulate [days] [TICKER ...]   trade the signals over the ~60 "
            "days of\n                          5-minute bars ('backtest' "
            "works too)",
   watch="watch TICKER ...    add stocks to the watchlist",
   unwatch="unwatch TICKER ...  remove stocks from the watchlist",
   watchlist="watchlist            price and data of the watched stocks",
   refresh="refresh              re-download price history",
   set="set NAME VALUE       change a setting ('set' alone lists them, "
       "'set NAME\n                          default' resets one)",
   status="status",
   quit="quit")

HELP_ORDER = ("asof", "nextbar", "nextday", "recommend", "track", "buy",
              "sell", "positions", "pnl", "trades", "edittrade",
              "deltrade", "realpositions", "savepositions",
              "compare", "range", "follow", "monitor", "simulate", "watch",
              "unwatch", "watchlist", "refresh", "set", "status", "quit")

###########################################################################
###
### h e l p e r s
###
###########################################################################

class ExceptionClass(Exception):
   """
   name: ExceptionClass
   use: an error that knows which program raised it
   """

   def __init__(self, message, exec_name = ""):
      Exception.__init__(self, message)
      self.exec_name = exec_name
      self.message   = (     f"{exec_name}: {message}" if (exec_name)
                        else f"{message.capitalize()}")

   ########################################################################
   def print_error(self):
      """
      name: print_error
      use: writes the message to stderr
      """
      print(self.message, file = sys.stderr)

###########################################################################
def set_home(path):
   """
   name: set_home
   use: points every persistent file at another directory
   imp: used by -s so synthetic test data never mixes with real files
   """
   global HOME, INTRADAY, DAILY, LEDGER, WATCH, SETTINGS
   HOME     = path
   INTRADAY = os.path.join(HOME, "intraday.pkl")
   DAILY    = os.path.join(HOME, "daily.pkl")
   LEDGER   = os.path.join(HOME, "ledger.json")
   WATCH    = os.path.join(HOME, "watchlist.json")
   SETTINGS = os.path.join(HOME, "settings.json")

###########################################################################
def load_json(path, default):
   """
   name: load_json
   use: reads a JSON file
   post: returns default when the file is missing or unreadable
   """
   try:
      with open(path) as fh:
         return(json.load(fh))
   except Exception:
      return(default)

###########################################################################
def dump_json(path, obj):
   """
   name: dump_json
   use: writes a JSON file atomically
   post: never leaves a half-written file behind
   """
   os.makedirs(os.path.dirname(path), exist_ok = True)
   tmp = path + ".tmp"
   with open(tmp, "w") as fh:
      json.dump(obj, fh, indent = 1)
   os.replace(tmp, path)

###########################################################################
def pct(x):
   """
   name: pct
   use: a fraction as a signed percentage, e.g. +1.25%
   """
   return("%+.2f%%" % (x * 100.0))

###########################################################################
def usd(v):
   """
   name: usd
   use: a signed dollar amount, e.g. +$1,234.50 or -$0.37
   """
   return("%s$%s" % ("+" if (v >= 0) else "-", format(abs(v), ",.2f")))

###########################################################################
def pctd(x, px, sep = "/"):
   """
   name: pctd
   use: a percentage plus the dollar change on one share bought at px
   usage: sep is "/" for running text and " " for table columns
   """
   return("%s%s%s" % (pct(x), sep, usd(x * px)))

###########################################################################
def f2(x):
   """
   name: f2
   use: a price with two decimals, or n/a when it is not known
   """
   if (x is None or not np.isfinite(x)):
      return("n/a")
   return("%.2f" % x)

###########################################################################
def unique(items):
   """
   name: unique
   use: the items without repeats, in the order they first appear
   """
   return(list(dict.fromkeys(items)))

###########################################################################
def wrap(text, indent = 8):
   """
   name: wrap
   use: text wrapped to 78 columns under an indent
   """
   pad = " " * indent
   return(textwrap.fill(text, 78, initial_indent = pad,
                        subsequent_indent = pad + "   "))

###########################################################################
def hhmm(ts):
   """
   name: hhmm
   use: the HH:MM of a timestamp
   """
   return(ts.strftime("%H:%M"))

###########################################################################
def market_open(now):
   """
   name: market_open
   use: predicate; is the regular US session trading at now (New York)
   imp: exchange holidays are not known; on a holiday no new bars arrive
   """
   m = now.hour * 60 + now.minute
   return((now.weekday() < 5) and (OPEN_MIN <= m < CLOSE_MIN))

###########################################################################
###
### p r i c e    d a t a
###
###########################################################################

def to_ny(index):
   """
   name: to_ny
   use: a DatetimeIndex as naive New York time
   """
   idx = pd.DatetimeIndex(index)
   if (idx.tz is not None):
      idx = idx.tz_convert(TZ).tz_localize(None)
   return(idx)

###########################################################################
def session_only(df):
   """
   name: session_only
   use: keeps the regular-session 5-minute bars (09:30 to 15:55 starts)
   """
   m = df.index.hour * 60 + df.index.minute
   keep = (m >= OPEN_MIN) & (m <= LAST_MIN) & (df.index.dayofweek < 5)
   return(df[keep])

###########################################################################
def yf_download(tickers, period, interval):
   """
   name: yf_download
   use: downloads public OHLCV bars from Yahoo Finance
   post: returns {ticker: DataFrame} indexed by New York time; symbols
         without data are left out
   """
   try:
      import yfinance as yf
   except ImportError:
      raise(ExceptionClass("missing package; run: pip install yfinance",
                           SELF))
   want = sorted(set(tickers))
   try:
      raw = yf.download(want, period = period, interval = interval,
                        group_by = "ticker", auto_adjust = True,
                        prepost = False, progress = False, threads = True)
   except Exception as error:
      print("%s: download failed: %s" % (SELF, error), file = sys.stderr)
      return({})
   if (len(raw) == 0):
      return({})
   if (not isinstance(raw.columns, pd.MultiIndex)):
      raw = pd.concat({want[0]: raw}, axis = 1)
   frames = {}
   for t in want:
      try:
         df = raw[t][["Open", "High", "Low", "Close", "Volume"]].copy()
      except KeyError:
         continue
      df = df.dropna(subset = ["Close"])
      if (len(df) == 0):
         continue
      for col in ("Open", "High", "Low"):
         df[col] = df[col].fillna(df["Close"])
      df["Volume"] = df["Volume"].fillna(0.0)
      df.index = to_ny(df.index)
      frames[t] = df
   return(frames)

###########################################################################
def merge(old, new):
   """
   name: merge
   use: adds freshly downloaded bars to the cached ones
   post: new bars replace cached bars with the same time; only the last
         KEEP_DAYS days are kept
   """
   if (old is None or len(old) == 0):
      df = new
   else:
      df = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
   cut = df.index[-1] - pd.Timedelta(days = KEEP_DAYS + 5)
   return(df[df.index >= cut])

###########################################################################
def to_daily(bars):
   """
   name: to_daily
   use: daily OHLCV bars built from 5-minute bars
   """
   g = bars.groupby(bars.index.normalize())
   return(pd.DataFrame(dict(Open   = g["Open"].first(),
                            High   = g["High"].max(),
                            Low    = g["Low"].min(),
                            Close  = g["Close"].last(),
                            Volume = g["Volume"].sum())))

###########################################################################
def synth_bars(ticker, days = KEEP_DAYS):
   """
   name: synth_bars
   use: fake 5-minute bars for offline testing of the program
   post: each day follows one of a few shapes (trend up, trend down,
         V-shaped rebound, fade, chop) under random noise, with an
         overnight gap and U-shaped volume; the same ticker always gives
         the same bars
   """
   seed  = sum((k + 1) * ord(ch) for k, ch in enumerate(ticker))
   rng   = np.random.default_rng(seed)
   dates = pd.bdate_range(end     = pd.Timestamp.today().normalize(),
                          periods = days)
   slots = np.arange(OPEN_MIN, LAST_MIN + 1, BAR_MIN)
   nb    = len(slots)
   k     = np.arange(nb)
   mus, vols = [], []
   for d in range(days):
      shape = rng.choice(["up", "down", "v", "fade", "chop"])
      a = int(rng.integers(3, nb // 2))
      b = int(rng.integers(a + 6, nb))
      s = rng.uniform(0.0004, 0.0012)
      mu = np.zeros(nb)
      if (shape == "up"):
         mu[a:b] = s
      elif (shape == "down"):
         mu[a:b] = -s
      elif (shape == "v"):
         mu[:a], mu[a:b] = -s, s
      elif (shape == "fade"):
         mu[:a], mu[a:b] = s, -s
      v = (2e5 * (1 + 2.0 * np.exp(-k / 5.0) +
                  np.exp(-(nb - 1 - k) / 5.0)) *
           rng.lognormal(0, 0.35, nb) * (1 + 400 * np.abs(mu)))
      mus.append(mu)
      vols.append(v)
   mu  = np.concatenate(mus)
   n   = len(mu)
   r   = mu + rng.normal(0, 0.0012, n)
   gap = np.where(np.tile(k, days) == 0, rng.normal(0, 0.006, n),
                  rng.normal(0, 0.0002, n))
   o = np.empty(n)
   c = np.empty(n)
   prev = 80.0 + seed % 120
   for i in range(n):
      o[i] = prev * math.exp(gap[i])
      c[i] = o[i] * math.exp(r[i])
      prev = c[i]
   hi = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, 0.0007, n)))
   lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, 0.0007, n)))
   idx = (dates.repeat(nb) +
          pd.to_timedelta(np.tile(slots, days), unit = "m"))
   return(pd.DataFrame(dict(Open = o, High = hi, Low = lo, Close = c,
                            Volume = np.concatenate(vols)), index = idx))

###########################################################################
class Source(object):
   """
   name: Source
   use: where prices come from: Yahoo Finance, or synthetic bars that are
        revealed one bar at a time (a pretend clock) for testing
   """

   def __init__(self, synthetic):
      self.synthetic = synthetic
      self.full      = {}
      self.cursor    = None
      if (synthetic):
         frame = self.synth("SYNA")
         last  = frame.index.normalize()[-1]
                                # start the pretend clock at 10:30 on the
                                # last synthetic day
         self.cursor = last + pd.Timedelta(minutes = 10 * 60 + 30)

   ########################################################################
   def synth(self, ticker):
      """
      name: synth
      use: the full synthetic history of one ticker (made on first use)
      """
      if (ticker not in self.full):
         self.full[ticker] = synth_bars(ticker)
      return(self.full[ticker])

   ########################################################################
   def now(self):
      """
      name: now
      use: the current New York time (naive)
      post: with synthetic data, the end of the bar the clock is on
      """
      if (self.synthetic):
         return((self.cursor + pd.Timedelta(minutes = BAR_MIN))
                .to_pydatetime())
      return(datetime.now(TZ).replace(tzinfo = None))

   ########################################################################
   def advance(self, n = 1):
      """
      name: advance
      use: moves the synthetic clock forward n bars
      post: returns False when the synthetic history is used up
      """
      idx = self.synth("SYNA").index
      i = int(idx.searchsorted(self.cursor, side = "right")) - 1 + n
      if (i >= len(idx)):
         return(False)
      self.cursor = idx[i]
      return(True)

   ########################################################################
   def intraday(self, tickers, period):
      """
      name: intraday
      use: 5-minute bars of the regular session
      usage: period is a Yahoo period such as "5d" or "60d"
      """
      if (self.synthetic):
         return(dict((t, self.synth(t)[self.synth(t).index <=
                                       self.cursor]) for t in tickers))
      got = yf_download(tickers, period, "5m")
      return(dict((t, session_only(df)) for t, df in got.items()))

   ########################################################################
   def daily(self, tickers):
      """
      name: daily
      use: about a year of daily bars (only prior days are ever used)
      """
      if (self.synthetic):
         return(dict((t, to_daily(df))
                     for t, df in self.intraday(tickers, "").items()))
      got = yf_download(tickers, "1y", "1d")
      for df in got.values():
         df.index = df.index.normalize()
      return(got)

###########################################################################
###
### i n d i c a t o r s    a n d    s i g n a l s
###
###########################################################################

def roll_trend(y, n):
   """
   name: roll_trend
   use: least-squares slope of y over each trailing window of n points,
        and the t-statistic of that slope
   post: returns (slope, t) numpy arrays, nan until n points exist
   imp: built from rolling sums, so it costs O(len(y)), not O(len * n)
   """
   y     = np.asarray(y, dtype = float)
   m     = len(y)
   slope = np.full(m, np.nan)
   tstat = np.full(m, np.nan)
   if (m < n or n < 3):
      return(slope, tstat)
   xc  = np.arange(n) - (n - 1) / 2.0
   sxx = float((xc ** 2).sum())
                                # np.convolve flips its kernel, so the
                                # reversed kernel gives, for the window
                                # ending at i, sum(xc[k] * y[i-n+1+k])
   sxy = np.convolve(y, xc[::-1], mode = "valid")
   syy = pd.Series(y).rolling(n).var(ddof = 0).values[n - 1:] * n
   b   = sxy / sxx
   sse = np.maximum(syy - b * b * sxx, 0.0)
   se  = np.sqrt(sse / (n - 2) / sxx)
   slope[n - 1:] = b
   tstat[n - 1:] = b / (se + 1e-12)
   return(slope, tstat)

###########################################################################
def indicators(bars, daily, cfg):
   """
   name: indicators
   use: adds every value the signals use to a copy of the 5-minute bars
   pre: bars holds completed 5-minute OHLCV rows indexed by New York
        time; daily holds daily OHLC rows indexed by date
   post: returns the new DataFrame; each value at a bar uses only data
         known at the close of that bar, and the daily ATR, high and
         close come from the days BEFORE the bar's day
   """
   df = bars.copy()
   ix = df.index
   c  = df["Close"]
   df["day"]   = ix.normalize()
   df["mins"]  = ix.hour * 60 + ix.minute
   g = df.groupby("day", sort = False)
   df["nbar"]  = g.cumcount()
   df["dopen"] = g["Open"].transform("first")
   df["dhi"]   = g["High"].cummax()
   df["dlo"]   = g["Low"].cummin()
                                # volume-weighted average price since the
                                # open
   df["_pv"]   = (df["High"] + df["Low"] + c) / 3.0 * df["Volume"]
   cpv = df.groupby("day")["_pv"].cumsum()
   cv  = df.groupby("day")["Volume"].cumsum()
   df["vwap"]  = (cpv / cv.replace(0, np.nan)).fillna(c)
   df["ema9"]  = c.ewm(span = 9, adjust = False).mean()
   df["ema21"] = c.ewm(span = 21, adjust = False).mean()
   d  = c.diff()
   up = d.clip(lower = 0).ewm(alpha = 1 / 14.0, adjust = False).mean()
   dn = (-d.clip(upper = 0)).ewm(alpha = 1 / 14.0, adjust = False).mean()
   df["rsi"]    = 100 - 100 / (1 + up / (dn + 1e-12))
   df["rsimin"] = df["rsi"].rolling(6).min()
   slope, tstat = roll_trend(np.log(c.values), int(cfg["trend_bars"]))
   df["slope"]  = slope
   df["tstat"]  = tstat
   df["sig"]    = np.log(c).diff().rolling(78, min_periods = 20).std()
                                # volume against the average of the same
                                # time of day over the 10 prior sessions
                                # (opening and closing bars are always
                                # heavy)
   usual = df.groupby("mins")["Volume"].transform(
      lambda s: s.shift(1).rolling(10, min_periods = 3).mean())
   df["vr"]  = df["Volume"] / (usual + 1.0)
   df["vr3"] = df["vr"].rolling(3).mean()
                                # bars since the day's low / high was set
   for col, ext, cmp in (("lowage", "dlo", "Low"), ("hiage", "dhi",
                                                    "High")):
      at = (df[cmp] <= df[ext]) if (cmp == "Low") else \
           (df[cmp] >= df[ext])
      seen = pd.Series(np.where(at, df["nbar"], np.nan), index = ix)
      df[col] = df["nbar"] - seen.groupby(df["day"].values).ffill()
                                # daily ATR(14), high and close of the
                                # last day before each bar's day
   dl = daily.sort_index()
   dd = pd.DatetimeIndex(dl.index).normalize()
   pc = dl["Close"].shift(1)
   tr = pd.concat([dl["High"] - dl["Low"], (dl["High"] - pc).abs(),
                   (dl["Low"] - pc).abs()], axis = 1).max(axis = 1)
   atr = tr.rolling(14).mean().values
   days = pd.DatetimeIndex(df["day"].unique())
   pos = dd.searchsorted(days, side = "left") - 1
   amap, hmap, cmap = {}, {}, {}
   for dy, k in zip(days, pos):
      if (k >= 0):
         amap[dy] = atr[k]
         hmap[dy] = dl["High"].values[k]
         cmap[dy] = dl["Close"].values[k]
   df["atr"] = df["day"].map(amap).astype(float)
   df["pdh"] = df["day"].map(hmap).astype(float)
   df["pdc"] = df["day"].map(cmap).astype(float)
                                # expected range for the day and the
                                # projected price ahead_bars from now
   df["projhi"] = np.maximum(df["dhi"], df["dlo"] + df["atr"])
   df["projlo"] = np.minimum(df["dlo"], df["dhi"] - df["atr"])
   width = (df["projhi"] - df["projlo"]).replace(0, np.nan)
   df["pos"]  = (c - df["projlo"]) / width
   df["used"] = (df["dhi"] - df["dlo"]) / df["atr"]
   ah = int(cfg["ahead_bars"])
   df["proj"] = c * np.exp(df["slope"] * ah)
   df["band"] = c * df["sig"] * math.sqrt(ah)
   return(df.drop(columns = ["_pv"]))

###########################################################################
def buy_signals(df, cfg):
   """
   name: buy_signals
   use: which bars carry a BUY signal, and of which kind
   post: returns an int array: 0 none, 1 rebound, 2 breakout
   imp: rebound = the price is in the lowest part of the expected range,
        has made no new day low for confirm_bars bars, RSI has bounced
        from oversold, the close is back above a rising EMA9 and still
        below VWAP (room to recover).
        breakout = the price is at or near the day high, the trend is
        clean (t >= t_min) and projects above the day high, EMA9 is
        above EMA21, the close is above VWAP, the day has not already
        used up its range, and volume is at least usual.
   """
   c    = df["Close"]
   hm   = (df["mins"] // 60) * 100 + df["mins"] % 60
   ok   = (df["atr"].notna() & df["sig"].notna() &
           (df["nbar"] >= cfg["skip_open"]) & (hm < cfg["no_buy_after"]))
   rebound = (ok &
              (df["pos"]    <= cfg["bottom_zone"]) &
              (df["lowage"] >= cfg["confirm_bars"]) &
              (c            >  df["ema9"]) &
              (df["ema9"]   >  df["ema9"].shift(1)) &
              (df["rsimin"] <= cfg["rsi_low"]) &
              (df["rsi"]    >= df["rsimin"] + cfg["rsi_bounce"]) &
              (c            <  df["vwap"]))
   breakout = (ok &
               (c            >= df["dhi"] - cfg["near_high"] * df["atr"]) &
               (df["slope"]  >  0) &
               (df["tstat"]  >= cfg["t_min"]) &
               (df["ema9"]   >  df["ema21"]) &
               (c            >  df["vwap"]) &
               (df["proj"]   >  df["dhi"]) &
               (df["used"]   <= cfg["max_used"]))
   if (cfg["vol_min"] > 0):
      breakout &= df["vr3"] >= cfg["vol_min"]
   return(np.where(breakout, 2, np.where(rebound, 1, 0)))

###########################################################################
def buy_reason(r, kind, cfg):
   """
   name: buy_reason
   use: the evidence behind a BUY signal, as one sentence
   """
   c = r["Close"]
   if (kind == 1):
      return("rebound: day low %.2f set %d bars ago; price is %.0f%% of "
             "the way up the expected range %.2f-%.2f; RSI %.0f, up from "
             "%.0f; back above a rising EMA9 (%.2f); VWAP %.2f overhead"
             % (r["dlo"], r["lowage"], 100 * r["pos"], r["projlo"],
                r["projhi"], r["rsi"], r["rsimin"], r["ema9"], r["vwap"]))
   where = (     "at" if (c >= r["dhi"])
            else "%.2f%% below" % (100 * (1 - c / r["dhi"])))
   s = ("breakout: price %.2f is %s the day high %.2f; trend t=%+.1f "
        "over %d bars projects %.2f in %d bars; range used %.2f ATR; "
        "volume %.1fx usual" % (c, where, r["dhi"], r["tstat"],
                                cfg["trend_bars"], r["proj"],
                                cfg["ahead_bars"], r["used"], r["vr3"]))
   if (np.isfinite(r["pdh"]) and c > r["pdh"]):
      s += "; above yesterday's high %.2f" % r["pdh"]
   return(s)

###########################################################################
def idle_reason(r, cfg):
   """
   name: idle_reason
   use: a short note on a watched stock that has no signal
   """
   if (not np.isfinite(r["atr"])):
      return("waiting for 14 days of daily history (ATR)")
   return("no setup: price is %.0f%% of the way up the expected range, "
          "trend t=%+.1f, RSI %.0f, %s VWAP" % (
             100 * r["pos"], r["tstat"], r["rsi"],
             "above" if (r["Close"] > r["vwap"]) else "below"))

###########################################################################
def trail_stop(high, atr, cfg):
   """
   name: trail_stop
   use: the trailing stop below the highest price since the buy
   post: returns 0.0 when the trailing stop is turned off
   """
   if (cfg["trail_atr"] <= 0 or not np.isfinite(atr)):
      return(0.0)
   return(high - cfg["trail_atr"] * atr)

###########################################################################
def target_px(entry, atr, cfg):
   """
   name: target_px
   use: the profit target above the buy price
   post: returns 0.0 when the target is turned off
   """
   if (cfg["target_atr"] <= 0 or not np.isfinite(atr)):
      return(0.0)
   return(entry + cfg["target_atr"] * atr)

###########################################################################
def judge(r, p, cfg, check_levels = True):
   """
   name: judge
   use: SELL / HOLD decision for a held position at one bar
   usage: r is the indicator row of the bar; p is the position state:
          entry (price), atr (daily ATR at the buy), high (highest price
          since the buy through the PREVIOUS bar, for the stop), peak
          (highest through this bar), hiage (bars since that peak) and
          held (bars since the buy)
   post: returns (action, reason, code); code is one of stop, target,
         down, plateau, eod, hold
   imp: with check_levels False the target and trailing stop are left to
        the caller, which checks them inside the bar (backtest)
   """
   c      = r["Close"]
   peak   = p.get("peak", p["high"])
   stop   = trail_stop(p["high"], p["atr"], cfg)
   target = target_px(p["entry"], p["atr"], cfg)
   off    = "%s from the high %.2f" % (pct(c / peak - 1), peak)
   if (check_levels):
      if (stop > 0 and r["Low"] <= stop):
         return("SELL", "trailing stop %.2f hit (%s)" % (stop, off),
                "stop")
      if (target > 0 and r["High"] >= target):
         return("SELL", "profit target %.2f reached (%s above the buy)"
                % (target, pct(target / p["entry"] - 1)), "target")
   if (r["ema9"] < r["ema21"] and r["tstat"] <= -cfg["t_min"] and
       c < r["vwap"]):
      return("SELL", "turning down: trend t=%+.1f, EMA9 below EMA21, "
             "price below VWAP %.2f (%s)" % (r["tstat"], r["vwap"], off),
             "down")
   if (p["held"] >= cfg["min_hold"] and
       p["hiage"] >= cfg["plateau_bars"] and
       abs(r["tstat"]) < cfg["t_flat"]):
      return("SELL", "plateaued: no new high for %d bars and the trend "
             "is flat (t=%+.1f); %s" % (p["hiage"], r["tstat"], off),
             "plateau")
   if (cfg["eod_exit"] and r["mins"] >= LAST_MIN):
      return("SELL", "end-of-day exit (eod_exit is on)", "eod")
   return("HOLD", "trend t=%+.1f, high %.2f was %d bars ago, %d bars "
          "held; trailing stop %s, target %s" % (
             r["tstat"], peak, p["hiage"], p["held"],
             f2(stop) if (stop > 0) else "off",
             f2(target) if (target > 0) else "off"), "hold")

###########################################################################
def run_ticker(df, kind, cfg, events = None):
   """
   name: run_ticker
   use: trades one stock by the signals over all of its bars
   usage: events, when given, is a dict that receives short notes keyed
          by bar number (for 'replay')
   post: returns a list of trades: dict(i0, i1, px0, px1, ret, kind,
         why, bars, open); why is a key of EXIT_NAMES
   imp: decisions use the close of a bar and execute at the NEXT bar's
        open; the trailing stop and target are checked inside every bar
        against its low and high, the stop first (the cautious guess
        when one bar touches both); slippage is charged on both sides
   """
   O, H = df["Open"].values, df["High"].values
   L, C = df["Low"].values, df["Close"].values
   rows = df.to_dict("records")
   day  = df["day"].values
   last = np.r_[day[1:] != day[:-1], True]
   slip = cfg["slip"] / 100.0
   n    = len(df)
   trades, pos, pend = [], None, None

   def note(i, text):
      if (events is not None):
         events.setdefault(i, []).append(text)

   def close(i, px, why):
      px = px * (1 - slip)
      trades.append(dict(i0 = pos["i0"], i1 = i, px0 = pos["entry"],
                         px1 = px, ret = px / pos["entry"] - 1,
                         kind = pos["kind"], why = why,
                         bars = i - pos["i0"] + 1,
                         open = (why == "open")))
      note(i, "sold %.2f (%s) %s" % (px, EXIT_NAMES[why],
                                      pct(px / pos["entry"] - 1)))

   for i in range(n):
      if (pend is not None):
         if (pend[0] == "BUY" and pos is None):
            px  = O[i] * (1 + slip)
            pos = dict(i0 = i, entry = px, atr = rows[i - 1]["atr"],
                       high = px, hibar = i, kind = pend[1])
            note(i, "bought %.2f (%s)" % (px, KINDS[pend[1]]))
         elif (pend[0] == "SELL" and pos is not None):
            close(i, O[i], pend[1])
            pos = None
         pend = None
      sold = False
      if (pos is not None):
         stop   = trail_stop(pos["high"], pos["atr"], cfg)
         target = target_px(pos["entry"], pos["atr"], cfg)
         if (stop > 0 and L[i] <= stop):
            close(i, min(stop, O[i]), "stop")
            pos, sold = None, True
         elif (target > 0 and H[i] >= target):
            close(i, max(target, O[i]), "target")
            pos, sold = None, True
         else:
            if (H[i] > pos["high"]):
               peak, hibar = H[i], i
            else:
               peak, hibar = pos["high"], pos["hibar"]
            st = dict(entry = pos["entry"], atr = pos["atr"],
                      high = pos["high"], peak = peak,
                      hiage = i - hibar, held = i - pos["i0"] + 1)
            pos["high"], pos["hibar"] = peak, hibar
            act, why, code = judge(rows[i], st, cfg,
                                   check_levels = False)
            if (act == "SELL" and code == "eod"):
               close(i, C[i], "eod")
               pos, sold = None, True
            elif (act == "SELL"):
               pend = ("SELL", code)
               note(i, "SELL signal: %s" % code)
      if (kind[i] > 0):
         note(i, "BUY signal (%s)%s" % (KINDS[kind[i]],
                                         "" if (pos is None) else
                                         ", already held"))
         if (pos is None and pend is None and not sold and i < n - 1):
            pend = ("BUY", int(kind[i]))
   if (pos is not None):
      close(n - 1, C[n - 1] / (1 - slip), "open")
   return(trades)

###########################################################################
def random_base(df, h):
   """
   name: random_base
   use: average return of buying at a random bar's open and holding for
        h bars, over the bars that could carry a signal
   post: returns (mean, count); the yardstick a signal has to beat
   """
   O, C = df["Open"].values, df["Close"].values
   ok   = df["atr"].notna().values
   h    = max(1, int(round(h)))
   n    = len(df)
   if (n < h + 2):
      return(0.0, 0)
   t = np.arange(n - h - 1)
   t = t[ok[t]]
   if (len(t) == 0):
      return(0.0, 0)
   fwd = C[t + h] / O[t + 1] - 1
   return(float(fwd.mean()), len(t))

###########################################################################
###
### t r a d e s ,    p o s i t i o n s    a n d    p r o f i t
###
###########################################################################

def parse_time(text):
   """
   name: parse_time
   use: a trade time typed as YYYY-MM-DD HH:MM (or with a T between)
   post: returns it as YYYY-MM-DDTHH:MM, the form kept in the ledger;
         raises ValueError when it is not a valid date and time
   """
   ts = pd.Timestamp(text.strip().replace("T", " "))
   return(ts.strftime("%Y-%m-%dT%H:%M"))

###########################################################################
def trade_order(x):
   """
   name: trade_order
   use: sort key of a trade: time, buys before sells in the same minute,
        then the order they were recorded
   """
   return((x["time"], 0 if (x["side"] == "buy") else 1, x["id"]))

###########################################################################
def book(trades):
   """
   name: book
   use: matches every sell against the earliest shares still held of
        that stock (first in, first out, as brokers do by default)
   usage: trades is a list of dict(id, ticker, side "buy" or "sell",
          time, shares, price[, atr, high, practice])
   post: returns (lots, closed, problems):
         lots     shares still held, one dict per buy in the order
                  bought: id (of the buy), ticker, entry_time,
                  entry_price, shares (left), atr, high, practice and
                  trade (the buy record itself)
         closed   one dict per piece of a buy matched to a sell: ticker,
                  buy, sell (ids), shares, entry_time, entry_price,
                  exit_time, exit_price, pnl
         problems sentences about sells of more shares than were held
   """
   bought, lots, closed, problems = {}, [], [], []
   for x in sorted(trades, key = trade_order):
      t = x["ticker"]
      if (x["side"] == "buy"):
         lot = dict(id          = x["id"],
                    ticker      = t,
                    entry_time  = x["time"],
                    entry_price = x["price"],
                    shares      = x["shares"],
                    atr         = x.get("atr"),
                    high        = x.get("high"),
                    practice    = bool(x.get("practice")),
                    trade       = x)
         bought.setdefault(t, []).append(lot)
         lots.append(lot)
         continue
      left = x["shares"]
      for lot in bought.get(t, []):
         if (left <= 1e-9):
            break
         n = min(left, lot["shares"])
         if (n <= 1e-9):
            continue
         lot["shares"] -= n
         left          -= n
         closed.append(dict(ticker      = t,
                            buy         = lot["id"],
                            sell        = x["id"],
                            shares      = n,
                            entry_time  = lot["entry_time"],
                            entry_price = lot["entry_price"],
                            exit_time   = x["time"],
                            exit_price  = x["price"],
                            pnl         = n * (x["price"] -
                                               lot["entry_price"])))
      if (left > 1e-9):
         problems.append("sale #%d (%s) sells %g more %s share%s than "
                         "were held then" % (x["id"], x["time"].replace(
                            "T", " "), left, t, "" if (left == 1) else
                            "s"))
   return([lot for lot in lots if (lot["shares"] > 1e-9)], closed,
          problems)

###########################################################################
def profit(lots, closed, prices):
   """
   name: profit
   use: profit and loss per stock and in total
   usage: prices maps a ticker to its current price (missing = unknown)
   post: returns (rows, total); one row per stock ever traded, in the
         order first bought: dict(ticker, shares, cost (average price
         paid for the shares held), price, value, unreal, unreal_pct,
         real, total); total has the same money fields summed. unreal
         is None when a held stock has no price
   """
   order = unique([x["ticker"] for x in closed] +
                  [x["ticker"] for x in lots])
   order = sorted(order, key = lambda t: min(
      [x["entry_time"] for x in lots + closed if (x["ticker"] == t)]))
   rows = []
   tot  = dict(value = 0.0, paid = 0.0, unreal = 0.0, real = 0.0,
               known = True)
   for t in order:
      mine  = [x for x in lots if (x["ticker"] == t)]
      sh    = sum(x["shares"] for x in mine)
      paid  = sum(x["shares"] * x["entry_price"] for x in mine)
      real  = sum(x["pnl"] for x in closed if (x["ticker"] == t))
      px    = prices.get(t)
      known = (px is not None and np.isfinite(px))
      row   = dict(ticker = t, shares = sh, price = px if (known)
                   else None, cost = paid / sh if (sh) else None,
                   value = None, unreal = None, unreal_pct = None,
                   real = real, total = None)
      if (sh == 0):
         row.update(value = 0.0, unreal = 0.0, total = real)
      elif (known):
         row.update(value = sh * px, unreal = sh * px - paid,
                    unreal_pct = px * sh / paid - 1, total = real +
                    sh * px - paid)
         tot["value"] += sh * px
         tot["paid"]  += paid
         tot["unreal"] += sh * px - paid
      else:
         tot["known"] = False
      tot["real"] += real
      rows.append(row)
   tot["total"] = tot["real"] + tot["unreal"]
   return(rows, tot)

###########################################################################
def load_ledger(path):
   """
   name: load_ledger
   use: reads the trade log
   post: returns the list of trades; a ledger written by an older
         version (a list of positions, each sold one carrying exit_time
         and exit_price) is converted to trades, and the old file is
         kept once as <ledger>.v1
   imp: old positions recorded without a share count are taken as 1
        share
   """
   data = load_json(path, dict(version = 2, trades = []))
   if (isinstance(data, dict)):
      return(data.get("trades", []))
   trades = []
   nid = max([q.get("id", 0) for q in data] + [0])
   for q in data:
      sh = float(q.get("shares") or 1.0)
      trades.append(dict(id = q["id"], ticker = q["ticker"], side = "buy",
                         time = q["entry_time"], shares = sh,
                         price = q["entry_price"], atr = q.get("atr"),
                         high = q.get("high")))
      if (q.get("exit_time")):
         nid += 1
         trades.append(dict(id = nid, ticker = q["ticker"], side = "sell",
                            time = q["exit_time"], shares = sh,
                            price = q["exit_price"]))
   if (data and not os.path.exists(path + ".v1")):
      os.replace(path, path + ".v1")
      dump_json(path, dict(version = 2, trades = trades))
   return(trades)

###########################################################################
###
### t h e    t r a c k e r
###
###########################################################################

class Tracker(object):
   """
   name: Tracker
   use: the watchlist, positions, settings and prices, and the signals
        evaluated on them at the latest bar or at an as-of (pretend) time
   imp: the ledger is a log of trades (buys and sells with shares and
        price); what is held, and the profit or loss, is worked out from
        it (see book). as in stock_picker, buys and sells made while an
        as-of time is active are practice trades; they live only in this
        session until 'savepositions' writes them into the real ledger
   """

   def __init__(self, synthetic, verbose):
      self.synthetic    = synthetic
      self.verbose      = verbose
      self.source       = Source(synthetic)
      self.cfg          = dict(DEFAULTS)
      for k, v in load_json(SETTINGS, {}).items():
         if (k in DEFAULTS):
            self.cfg[k] = v
      self.bars         = {}
      self.daily        = {}
      self.dtime        = 0.0
      self.synced       = 0.0
      self.trades       = load_ledger(LEDGER)
      self.asof         = None
      self.pretend      = []
      if (synthetic and not os.path.exists(WATCH)):
         self.save_watch(SYNTH)
      if (not synthetic):
         self.load_cache()

   ########################################################################
   def now(self):
      """
      name: now
      use: the current New York time: the end of the as-of bar when one is
           set, else the clock (the pretend clock with --synthetic)
      """
      if (self.asof is not None):
         return((self.asof + pd.Timedelta(minutes = BAR_MIN))
                .to_pydatetime())
      return(self.source.now())

   ########################################################################
   def stamp(self):
      """
      name: stamp
      use: now() as YYYY-MM-DDTHH:MM, the form kept in the ledger
      """
      return(self.now().isoformat(timespec = "minutes"))

   ########################################################################
   def timeline(self):
      """
      name: timeline
      use: start times of every completed bar of the followed stocks
      """
      end = (pd.Timestamp(self.source.now()) -
             pd.Timedelta(minutes = BAR_MIN))
      u = pd.DatetimeIndex([])
      for t in self.tickers():
         df = self.bars.get(t)
         if (df is not None):
            u = u.union(df.index[df.index <= end])
      return(u)

   ########################################################################
   def load_cache(self):
      """
      name: load_cache
      use: reads the cached 5-minute and daily bars
      """
      for path, attr in ((INTRADAY, "bars"), (DAILY, "daily")):
         try:
            with open(path, "rb") as fh:
               c = pickle.load(fh)
            setattr(self, attr, c["frames"])
            if (attr == "daily"):
               self.dtime = c["time"]
         except Exception:
            pass

   ########################################################################
   def save_cache(self):
      """
      name: save_cache
      use: writes the 5-minute and daily bars to the cache
      """
      if (self.synthetic):
         return
      os.makedirs(HOME, exist_ok = True)
      for path, frames, stamp in ((INTRADAY, self.bars, time.time()),
                                  (DAILY, self.daily, self.dtime)):
         tmp = path + ".tmp"
         with open(tmp, "wb") as fh:
            pickle.dump(dict(time = stamp, frames = frames), fh)
         os.replace(tmp, path)

   ########################################################################
   def watch(self):
      """
      name: watch
      use: the watched tickers
      """
      return([w["ticker"] for w in load_json(WATCH, [])
              if (w.get("ticker"))])

   ########################################################################
   def save_watch(self, tickers):
      """
      name: save_watch
      use: writes the watchlist, keeping the date each ticker was added
      """
      old = dict((w["ticker"], w) for w in load_json(WATCH, []))
      dump_json(WATCH, [old.get(t, dict(ticker = t, added =
                                        time.strftime("%Y-%m-%d")))
                        for t in tickers])

   ########################################################################
   def save_settings(self):
      """
      name: save_settings
      use: writes the settings that differ from the defaults
      """
      dump_json(SETTINGS, dict((k, v) for k, v in self.cfg.items()
                               if (v != DEFAULTS[k])))

   ########################################################################
   def save_ledger(self):
      """
      name: save_ledger
      use: writes the trade log
      """
      dump_json(LEDGER, dict(version = 2, trades = self.trades))

   ########################################################################
   def books(self, upto = True, practice = True):
      """
      name: books
      use: book() over the trades
      usage: upto True = only trades made by now() (the as-of time);
             practice False = the real ledger only
      post: returns (lots, closed, problems)
      """
      ts = self.stamp()
      xs = self.trades + (self.pretend if (practice) else [])
      return(book([x for x in xs if (not upto or x["time"] <= ts)]))

   ########################################################################
   def held(self, ticker = None, practice = True):
      """
      name: held
      use: the lots held at now(): shares bought by then and not yet sold
           by then, practice trades included unless practice is False
      post: moving 'asof' therefore shows what was held at that time
      """
      lots = self.books(practice = practice)[0]
      return([q for q in lots if (ticker is None or q["ticker"] == ticker)])

   ########################################################################
   def closed(self):
      """
      name: closed
      use: the shares sold by now(), each matched to its buy (see book)
      """
      return(self.books()[1])

   ########################################################################
   def shares(self, ticker):
      """
      name: shares
      use: how many shares of ticker are held at now()
      """
      return(sum(q["shares"] for q in self.held(ticker)))

   ########################################################################
   def practice(self):
      """
      name: practice
      use: predicate; are there unsaved practice trades
      """
      return(bool(self.pretend))

   ########################################################################
   def tickers(self):
      """
      name: tickers
      use: every ticker followed: the watchlist plus every stock held
      post: in watchlist order (the order given on the command line),
            then held stocks not on it in the order they were bought;
            NOT sorted alphabetically
      """
      lots = book(self.trades + self.pretend)[0]
      return(unique(self.watch() + [q["ticker"] for q in lots]))

   ########################################################################
   def trade(self, tid):
      """
      name: trade
      use: the trade with id tid (real or practice), or None
      """
      for x in self.trades + self.pretend:
         if (x["id"] == tid):
            return(x)
      return(None)

   ########################################################################
   def check(self, trades):
      """
      name: check
      use: refuses a trade log in which a sale sells shares not held
      post: raises ExceptionClass with the reasons
      """
      problems = book(trades)[2]
      if (problems):
         raise(ExceptionClass("; ".join(problems)))

   ########################################################################
   def record(self, side, t, shares, price, when = None, atr = None):
      """
      name: record
      use: records a buy or a sell: into the real ledger at the latest
           bar, otherwise as a practice trade
      usage: when is the trade time (YYYY-MM-DD HH:MM), default now();
             atr is the stock's daily ATR, kept with a buy for its stop
             and target
      post: returns the trade; raises ExceptionClass when the values are
            not valid or a sale is for more shares than were held
      """
      if (not (shares > 0)):
         raise(ExceptionClass("the number of shares must be more than 0"))
      if (not (price > 0)):
         raise(ExceptionClass("the price must be more than 0"))
      ids = [x["id"] for x in self.trades + self.pretend]
      x = dict(id = max(ids + [0]) + 1, ticker = t.upper(), side = side,
               time = parse_time(when) if (when) else self.stamp(),
               shares = float(shares), price = float(price))
      if (side == "buy"):
         ok = (atr is not None and np.isfinite(atr))
         x.update(atr = float(atr) if (ok) else None, high = float(price))
      if (self.asof is not None):
         x["practice"] = True
      self.check(self.trades + self.pretend + [x])
      if (self.asof is None):
         self.trades.append(x)
         self.save_ledger()
      else:
         self.pretend.append(x)
      return(x)

   ########################################################################
   def edit(self, tid, shares = None, price = None, when = None):
      """
      name: edit
      use: corrects the shares, price or time of a trade after the fact
      post: returns the trade; raises ExceptionClass when the values are
            not valid or the change would leave a sale of shares that
            were not held
      imp: a corrected buy price resets the highest price since the buy
           that the trailing stop is measured from
      """
      x = self.trade(tid)
      if (x is None):
         raise(ExceptionClass("there is no trade #%s" % tid))
      new = dict(x)
      if (shares is not None):
         if (not (shares > 0)):
            raise(ExceptionClass("the number of shares must be more "
                                 "than 0"))
         new["shares"] = float(shares)
      if (price is not None):
         if (not (price > 0)):
            raise(ExceptionClass("the price must be more than 0"))
         new["price"] = float(price)
         if (new["side"] == "buy"):
            new["high"] = float(price)
      if (when):
         new["time"] = parse_time(when)
      self.check([new if (y is x) else y
                  for y in self.trades + self.pretend])
      x.update(new)
      if (not x.get("practice")):
         self.save_ledger()
      return(x)

   ########################################################################
   def delete(self, tid):
      """
      name: delete
      use: removes a trade recorded by mistake
      post: raises ExceptionClass when there is no such trade or a sale
            would be left selling shares that were not held
      """
      x = self.trade(tid)
      if (x is None):
         raise(ExceptionClass("there is no trade #%s" % tid))
      self.check([y for y in self.trades + self.pretend if (y is not x)])
      if (x.get("practice")):
         self.pretend = [y for y in self.pretend if (y is not x)]
      else:
         self.trades = [y for y in self.trades if (y is not x)]
         self.save_ledger()

   ########################################################################
   def save_practice(self):
      """
      name: save_practice
      use: writes the practice trades into the real ledger
      post: returns how many were saved
      """
      n = len(self.pretend)
      for x in self.pretend:
         x.pop("practice", None)
         self.trades.append(x)
      self.pretend = []
      self.save_ledger()
      return(n)

   ########################################################################
   def sync(self, tickers = None, force = False):
      """
      name: sync
      use: brings the 5-minute (and daily) bars up to date
      post: a ticker that is new, forced, or more than 4 days stale gets
            the full ~60 days; the others get the last 5 days merged in
      """
      ts = sorted(set(tickers or self.tickers()))
      if (not ts):
         return
      now = self.source.now()
      groups = {"60d": [], "5d": []}
      for t in ts:
         df = self.bars.get(t)
         full = (force or df is None or len(df) == 0 or
                 now - df.index[-1] > timedelta(days = 4))
         groups["60d" if (full) else "5d"].append(t)
      for period, lst in groups.items():
         if (not lst):
            continue
         if (not self.synthetic and (period == "60d" or self.verbose)):
            print("Downloading %s of 5-minute bars for %d stock(s)..." %
                  (period, len(lst)))
         got = self.source.intraday(lst, period)
         for t in lst:
            if (t in got and len(got[t])):
               self.bars[t] = (     got[t] if (self.synthetic)
                               else merge(self.bars.get(t), got[t]))
            elif (t not in self.bars):
               print("%s: no price data for %s" % (SELF, t),
                     file = sys.stderr)
      miss = [t for t in ts if (t not in self.daily)]
      if (force or self.synthetic or time.time() - self.dtime > 12 * 3600):
         miss = ts
      if (miss):
         if (not self.synthetic):
            print("Downloading daily bars for %d stock(s)..." % len(miss))
         self.daily.update(self.source.daily(miss))
         self.dtime = time.time()
      self.synced = time.time()
      self.save_cache()

   ########################################################################
   def frame(self, t):
      """
      name: frame
      use: one ticker's bars completed by now(), with every indicator
      post: returns None when there is too little data
      """
      df = self.bars.get(t)
      dl = self.daily.get(t)
      if (df is None or dl is None or len(df) < 30):
         return(None)
      done = df[df.index + pd.Timedelta(minutes = BAR_MIN) <=
                pd.Timestamp(self.now())]
      if (len(done) < 30):
         return(None)
      return(indicators(done, dl, self.cfg))

   ########################################################################
   def pos_state(self, q, df, r):
      """
      name: pos_state
      use: the state judge() needs for a position
      post: at the latest bar the stored high of a real position is
            raised to the latest peak; at an as-of time the stored high
            is ignored (it may come from that time's future)
      """
      real  = (self.asof is None and not q.get("practice"))
      et    = pd.Timestamp(q["entry_time"])
      after = df[df.index + pd.Timedelta(minutes = BAR_MIN) > et]
      highs = after["High"].values
      held  = len(highs)
      base  = q["entry_price"]
      if (real):
         base = max(base, q.get("high") or 0.0)
      if (held):
         k     = int(np.argmax(highs))
         peak  = max(base, float(highs.max()))
         hiage = (held - 1 - k) if (highs.max() >= base) else held
         high  = (     max(base, float(highs[:-1].max())) if (held > 1)
                  else base)
      else:
         peak, hiage, high = base, 0, base
      atr = q.get("atr")
      if (atr is None or not np.isfinite(atr)):
         atr = r["atr"]
      if (real and peak > (q.get("high") or 0.0)):
                                # kept on the buy itself, so a restart
                                # remembers it
         q["high"] = float(peak)
         q["trade"]["high"] = float(peak)
         self.save_ledger()
      return(dict(entry = q["entry_price"], atr = atr, high = high,
                  peak = peak, hiage = hiage, held = held))

   ########################################################################
   def judge_pos(self, q):
      """
      name: judge_pos
      use: SELL / HOLD for one position on the bar at now()
      post: returns dict(act, why, code, st, r), or None without data
      """
      df = self.frame(q["ticker"])
      if (df is None):
         return(None)
      r = df.iloc[-1].to_dict()
      r["time"] = df.index[-1]
      st = self.pos_state(q, df, r)
      act, why, code = judge(r, st, self.cfg)
      return(dict(act = act, why = why, code = code, st = st, r = r))

   ########################################################################
   def evaluate(self, t):
      """
      name: evaluate
      use: the BUY signal for one ticker on the bar at now()
      post: returns dict(t, r, kind, why); r is the indicator row with
            its time (None without data) and kind 0 means no signal
      """
      df = self.frame(t)
      if (df is None):
         return(dict(t = t, r = None, kind = 0,
                     why = "not enough price data yet"))
      k = int(buy_signals(df, self.cfg)[-1])
      r = df.iloc[-1].to_dict()
      r["time"] = df.index[-1]
      why = (     buy_reason(r, k, self.cfg) if (k)
             else idle_reason(r, self.cfg))
      return(dict(t = t, r = r, kind = k, why = why))

###########################################################################
###
### t h e    s h e l l
###
###########################################################################

class Shell(cmd.Cmd):
   """
   name: Shell
   use: the interactive prompt; its commands, prompt and output follow
        stock_picker.py
   """

   def __init__(self, tracker):
      cmd.Cmd.__init__(self)
      self.tr        = tracker
      self.last_buys = []
      self.set_prompt()
      tl = tracker.timeline()
      self.intro = ("\n%s -- %d stocks, data through %s. Type 'help'.\n"
                    "Research tool only; not financial advice." % (
                       SELF, len(tracker.tickers()),
                       tl[-1].strftime("%Y-%m-%d %H:%M") if (len(tl))
                       else "(none yet)"))

   ########################################################################
   def set_prompt(self):
      tr = self.tr
      a = (     "latest" if (tr.asof is None)
           else tr.asof.strftime("%Y-%m-%d %H:%M"))
      self.prompt = "[5m | as of %s%s] > " % (
         a, " | practice" if (tr.practice()) else "")

   ########################################################################
   def postcmd(self, stop, line):
      self.set_prompt()
      return(stop)

   ########################################################################
   def emptyline(self):
      pass

   ########################################################################
   def default(self, line):
      print("Unknown command '%s'. Type 'help'." % line.split()[0])

   ########################################################################
   def do_help(self, arg):
      print("")
      for k in HELP_ORDER:
         print("  " + HELP[k])
      if (self.tr.synthetic):
         print("  step [N]             synthetic data only: the latest "
               "bar moves N bars on")
      print("\n  (Times are the START of each 5-minute bar.)\n")

   ########################################################################
   def do_quit(self, arg):
      return(True)

   do_exit = do_quit
   do_EOF  = do_quit

   ########################################################################
   def args(self, arg):
      """
      name: args
      use: the tickers named on a command line (upper case)
      """
      return([a.upper() for a in arg.replace(",", " ").split()
              if (a.replace("-", "").replace(".", "").isalpha())])

   ########################################################################
   def fresh(self, tickers = None):
      """
      name: fresh
      use: syncs prices when they are more than a minute old
      imp: at an as-of time nothing is downloaded unless a stock has no
           data at all
      """
      tr = self.tr
      ts = tickers or tr.tickers()
      miss = any(t not in tr.bars for t in ts)
      if (tr.asof is not None and not miss):
         return
      if (tr.synthetic or time.time() - tr.synced > 60 or miss):
         tr.sync(ts)

   ########################################################################
   def stale_note(self):
      """
      name: stale_note
      use: says so when the latest bar is not from today
      """
      tr = self.tr
      tl = tr.timeline()
      if (tr.asof is None and len(tl) and
          tl[-1].date() < tr.now().date()):
         print("\n(The latest bar is from %s; the market has not traded "
               "today yet.)" % tl[-1].strftime("%Y-%m-%d %H:%M"))

   ########################################################################
   def do_status(self, arg):
      tr, c = self.tr, self.tr.cfg
      tl = tr.timeline()
      now = tr.now()
      ahead = (0 if (tr.asof is None) else
               len(tl) - 1 - int(tl.searchsorted(tr.asof)))
      print("bars=5m poll=%ds target_atr=%.2f trail_atr=%.2f eod_exit=%d"
            % (c["poll"], c["target_atr"], c["trail_atr"], c["eod_exit"]))
      print("as of %s (%d bars of future data available), %d followed "
            "stocks" % (now.strftime("%Y-%m-%d %H:%M"), ahead,
                        len(tr.tickers())))
      print("rebound: bottom_zone=%.2f confirm_bars=%d rsi_low=%.0f "
            "rsi_bounce=%.0f" % (c["bottom_zone"], c["confirm_bars"],
                                 c["rsi_low"], c["rsi_bounce"]))
      print("breakout: near_high=%.2f t_min=%.1f max_used=%.2f "
            "vol_min=%.1f trend_bars=%d" % (
               c["near_high"], c["t_min"], c["max_used"], c["vol_min"],
               c["trend_bars"]))
      print("plateau: min_hold=%d plateau_bars=%d t_flat=%.1f | no BUY "
            "in the first %d bars or from %04d" % (
               c["min_hold"], c["plateau_bars"], c["t_flat"],
               c["skip_open"], c["no_buy_after"]))
      real = tr.source.now()
      print("market %s (%s New York)%s; files in %s" % (
         "open" if (market_open(real)) else "closed",
         real.strftime("%a %H:%M"),
         ", synthetic data" if (tr.synthetic) else "", HOME))
      for t in tr.tickers():
         df = tr.bars.get(t)
         if (df is None):
            print("   %-6s no data" % t)
         else:
            print("   %-6s %5d bars, %s .. %s" % (
               t, len(df), df.index[0].strftime("%Y-%m-%d"),
               df.index[-1].strftime("%Y-%m-%d %H:%M")))

   ########################################################################
   def do_asof(self, arg):
      tr = self.tr
      a = arg.strip().lower()
      tl = tr.timeline()
      if (not len(tl)):
         print("No price data yet. Use 'watch TICKER' first.")
         return
      last = len(tl) - 1
      cur = (     last if (tr.asof is None)
             else int(tl.searchsorted(tr.asof)))
      if (a in ("", "now", "latest")):
         i = last
      else:
         try:
            if (a[0] in "+-" and a[1:].isdigit()):
               i = cur + int(a) if (a[0] == "+") else cur - int(a[1:])
               i = min(i, last)
            else:
               p = arg.split()
               ts = pd.Timestamp(" ".join(p[:2]))
               if (len(p) == 1):
                  ts += pd.Timedelta(hours = 23, minutes = 59)
               i = int(tl.searchsorted(ts, side = "right")) - 1
            assert (0 <= i <= last)
         except Exception:
            print("usage: asof YYYY-MM-DD [HH:MM] | -N | +N | now   "
                  "(N = 5-minute bars)")
            return
      tr.asof = None if (i == last) else tl[i]
      print("Pretending it is %s (the end of the %s bar);\n%d bars of "
            "'future' data exist for checking." % (
               tr.now().strftime("%Y-%m-%d %H:%M"), hhmm(tl[i]),
               last - i))
      held = tr.held()
      print("Positions held then (real ledger by buy/sell time): %s" % (
         " ".join("%s#%d" % (q["ticker"], q["id"]) for q in held) or
         "none"))
      if (tr.asof is not None):
         print("Buys/sells you make while pretending are practice "
               "trades; 'savepositions'\nwrites them to the real ledger.")

   ########################################################################
   def do_nextbar(self, arg):
      tr = self.tr
      if (tr.asof is None):
         print("Already at the latest bar; there is no next bar yet. Use "
               "'asof DATE HH:MM'\nor 'asof -N' first.")
         return
      n = int(arg) if (arg.strip().isdigit()) else 1
      self.do_asof("+%d" % n)

   ########################################################################
   def do_nextday(self, arg):
      tr = self.tr
      if (tr.asof is None):
         print("Already at the latest bar; there is no next day yet. Use "
               "'asof DATE' or\n'asof -N' first.")
         return
      tl = tr.timeline()
      day = tr.asof.normalize()
      later = tl[tl.normalize() > day]
      if (not len(later)):
         self.do_asof("now")
         return
      nd = later.normalize()[0]
      same = later[(later.normalize() == nd) &
                   (later.time <= tr.asof.time())]
      self.do_asof((same[-1] if (len(same)) else later[0])
                   .strftime("%Y-%m-%d %H:%M"))

   ########################################################################
   def do_step(self, arg):
      tr = self.tr
      if (not tr.synthetic):
         print("'step' only works with synthetic data (--synthetic).")
         return
      n = int(arg) if (arg.strip().isdigit()) else 1
      if (not tr.source.advance(n)):
         print("Not that many synthetic bars left.")
         return
      tr.sync()
      print("The latest synthetic bar is now %s." % hhmm(
         tr.source.cursor))

   ########################################################################
   def do_watch(self, arg):
      tr = self.tr
      ts = self.args(arg)
      if (not ts):
         print("usage: watch TICKER [TICKER ...]   (e.g. 'watch AAPL "
               "MSFT')")
         return
      w = tr.watch()
      tr.save_watch(w + [t for t in ts if (t not in w)])
      tr.sync([t for t in ts if (t not in tr.bars)])
      print("Watching: %s" % " ".join(tr.watch()))
      self.show_watch(ts)

   ########################################################################
   def do_unwatch(self, arg):
      tr = self.tr
      ts = unique(self.args(arg))
      w = tr.watch()
      gone = [t for t in ts if (t in w)]
      if (not gone):
         print("Not on the watchlist: %s" % " ".join(ts))
         return
      tr.save_watch([t for t in w if (t not in gone)])
      print("Removed %s. (A stock you hold is still followed until it is "
            "sold.)" % " ".join(gone))

   ########################################################################
   def do_watchlist(self, arg):
      w = self.tr.watch()
      if (not w):
         print("The watchlist is empty. Use 'watch TICKER' to add a "
               "stock.")
         return
      self.show_watch(w)

   ########################################################################
   def show_watch(self, tickers):
      """
      name: show_watch
      use: one line per watched stock as of now(): price, move today and
           how much data is loaded
      """
      tr = self.tr
      added = dict((w["ticker"], w.get("added", "?"))
                   for w in load_json(WATCH, []))
      print("\n--- watchlist as of %s ---" % tr.now().strftime(
         "%Y-%m-%d %H:%M"))
      for t in tickers:
         df = tr.frame(t)
         if (df is None):
            print("%-6s no price data yet (unknown symbol, or too little "
                  "history); retried\n       on 'refresh'" % t)
            continue
         r = df.iloc[-1]
         move = ("today %s" % pct(r["Close"] / r["pdc"] - 1)
                 if (np.isfinite(r["pdc"])) else "today n/a")
         print("%-6s added %s | %.2f | %s | %d bars since %s" % (
            t, added.get(t, "held"), r["Close"], move, len(df),
            df.index[0].strftime("%Y-%m-%d")))

   ########################################################################
   def show_picks(self, entries, brief = False):
      """
      name: show_picks
      use: the signal table, ranked: BUY signals first, then the others
           by trend strength
      post: returns the BUY entries for stocks not already held
      """
      tr, cfg = self.tr, self.tr.cfg
      held = set(q["ticker"] for q in tr.held())
      print(" #  %-6s %9s %-20s %9s %9s  %s" % (
         "Ticker", "Price", "Target % $/share", "Stop", "Target", "Call"))
      buys = []
      for n, e in enumerate(entries):
         r = e["r"]
         if (r is None):
            print("%2d  %-6s %s" % (n + 1, e["t"], e["why"]))
            continue
         c = r["Close"]
         tg = target_px(c, r["atr"], cfg)
         sp = trail_stop(c, r["atr"], cfg)
         if (e["t"] in held):
            call = "held"
         elif (e["kind"]):
            call = "BUY %s" % KINDS[e["kind"]]
            buys.append(e)
         else:
            call = "watch"
         print("%2d  %-6s %9.2f %-20s %9s %9s  %s" % (
            n + 1, e["t"], c, pctd(tg / c - 1, c, " ") if (tg > 0)
            else "n/a", f2(sp) if (sp > 0) else "none",
            f2(tg) if (tg > 0) else "none", call))
         if (brief):
            continue
         print(wrap("why: %s" % e["why"], 6))
         print("      range: day %s-%s | expected %s-%s | VWAP %s | %s "
               "bar" % (f2(r["dlo"]), f2(r["dhi"]), f2(r["projlo"]),
                        f2(r["projhi"]), f2(r["vwap"]), hhmm(r["time"])))
      return(buys)

   ########################################################################
   def ranked(self, tickers):
      """
      name: ranked
      use: evaluate() for each ticker, breakouts first, then rebounds,
           then the rest by trend strength, stocks without data last
      """
      ev = [self.tr.evaluate(t) for t in tickers]

      def key(e):
         if (e["r"] is None):
            return((3, 0.0))
         t = e["r"]["tstat"]
         return((2 - e["kind"] if (e["kind"]) else 2,
                 -t if (np.isfinite(t)) else 0.0))

      return(sorted(ev, key = key))

   ########################################################################
   def do_recommend(self, arg):
      tr = self.tr
      ts = tr.tickers()
      if (not ts):
         print("Nothing to recommend from. Use 'watch TICKER' first.")
         return
      n = int(arg) if (arg.strip().isdigit()) else 10
      self.fresh(ts)
      print("\n=== signals as of %s (5-minute bars) ===\n(rebound = "
            "bottomed out, turning up; breakout = heading for a new day "
            "high)" % tr.now().strftime("%Y-%m-%d %H:%M"))
      buys = self.show_picks(self.ranked(ts)[:n])
      self.last_buys = buys
      if (not buys):
         print("\nNo BUY: none of the %d stocks shows a rebound or "
               "breakout setup on this bar.\nStanding aside is a valid "
               "recommendation." % len(ts))
      else:
         print("\nBUY candidates: %s  ('track' records them)" %
               " ".join(e["t"] for e in buys))
      self.show_positions()
      self.stale_note()

   do_recommendation  = do_recommend
   do_recommendations = do_recommend
   do_scan            = do_recommend
   do_check           = do_recommend

   ########################################################################
   def show_positions(self, led = None, title = None):
      """
      name: show_positions
      use: SELL / HOLD lines for the lots held at now()
      usage: by default the lots held at now() (see Tracker.held);
             'realpositions' passes the real ledger's, with its own title
      """
      tr = self.tr
      if (led is None):
         led = tr.held()
      if (not led):
         return
      print("\n--- %s ---" % (title or "open positions: sell / hold "
                               "advice"))
      for q in led:
         jp = tr.judge_pos(q)
         bought = "%g sh bought %s @ %.2f" % (
            q["shares"], q["entry_time"].replace("T", " "),
            q["entry_price"])
         if (jp is None):
            print("HOLD  %-6s %s: no price data loaded" % (q["ticker"],
                                                         bought))
            continue
         px, st = jp["r"]["Close"], jp["st"]
         g = px / q["entry_price"] - 1
         line = "%-5s %-6s %s, now %.2f (%s a share, %s): %s" % (
            jp["act"], q["ticker"], bought, px, pctd(g, q["entry_price"]),
            usd(g * q["entry_price"] * q["shares"]), jp["why"])
         print(textwrap.fill(line, 78, subsequent_indent = " " * 6))
         stop = trail_stop(st["high"], st["atr"], tr.cfg)
         tg = target_px(q["entry_price"], st["atr"], tr.cfg)
         print("      high since buy %.2f | trailing stop %s | profit "
               "target %s" % (st["peak"], f2(stop) if (stop > 0) else
                              "off", f2(tg) if (tg > 0) else "off"))

   ########################################################################
   def prices(self, tickers):
      """
      name: prices
      use: the price of each ticker at now() (the last completed bar's
           close)
      post: returns {ticker: price}; a ticker without data is left out
      """
      out = {}
      for t in tickers:
         df = self.tr.frame(t)
         if (df is not None):
            out[t] = float(df["Close"].iloc[-1])
      return(out)

   ########################################################################
   def show_pnl(self):
      """
      name: show_pnl
      use: the profit and loss table: per stock the shares held, the
           average price paid for them, the current price and value, the
           unrealized gain on the shares held, the realized gain on the
           shares sold, and the total
      """
      tr = self.tr
      lots, closed, problems = tr.books()
      if (not lots and not closed):
         print("No trades recorded by %s. Use 'buy TICKER [price] "
               "[shares]'." % tr.now().strftime("%Y-%m-%d %H:%M"))
         return
      held = unique(q["ticker"] for q in lots)
      self.fresh(held)
      rows, tot = profit(lots, closed, self.prices(held))
      print("\n--- profit and loss as of %s (first in, first out) ---"
            % tr.now().strftime("%Y-%m-%d %H:%M"))
      print("%-6s %7s %8s %8s %19s %11s %11s" % (
         "Ticker", "Shares", "AvgCost", "Price", "Unrealized", "Realized",
         "Total"))

      def money(v):
         return("n/a" if (v is None) else usd(v))

      for r in rows:
         unreal = ("" if (not r["shares"]) else
                   money(r["unreal"]) + (" (%s)" % pct(r["unreal_pct"])
                                         if (r["unreal_pct"] is not None)
                                         else ""))
         print("%-6s %7g %8s %8s %19s %11s %11s" % (
            r["ticker"], r["shares"], f2(r["cost"]) if (r["shares"])
            else "", f2(r["price"]) if (r["shares"]) else "", unreal,
            money(r["real"]), money(r["total"])))
      known = tot["known"]
      print("%-6s %7s %8s %8s %19s %11s %11s" % (
         "TOTAL", "", "", "",
         money(tot["unreal"]) + (" (%s)" % pct(tot["unreal"] /
                                               tot["paid"])
                                 if (known and tot["paid"]) else "")
         if (known) else "n/a", money(tot["real"]),
         money(tot["total"]) if (known) else "n/a"))
      if (tr.practice()):
         print("(includes practice trades not yet saved)")
      for p in problems:
         print("WARNING: %s" % p)

   ########################################################################
   def do_positions(self, arg):
      tr = self.tr
      if (not tr.held()):
         print("No positions held at %s. Use 'track' or 'buy TICKER'." %
               tr.now().strftime("%Y-%m-%d %H:%M"))
      else:
         self.fresh(unique(q["ticker"] for q in tr.held()))
         self.show_positions()
      done = tr.closed()
      if (done):
         print("\n--- shares sold (latest 10), bought -> sold ---")
         for q in done[-10:]:
            g = q["exit_price"] / q["entry_price"] - 1
            print("%-6s %5g sh %s @ %.2f -> %s @ %.2f %s %s" % (
               q["ticker"], q["shares"],
               q["entry_time"][5:].replace("T", " "), q["entry_price"],
               q["exit_time"][5:].replace("T", " "), q["exit_price"],
               pct(g), usd(q["pnl"])))
      if (tr.held() or done):
         self.show_pnl()

   do_position = do_positions

   ########################################################################
   def do_pnl(self, arg):
      self.show_pnl()

   do_profit = do_pnl

   ########################################################################
   def do_trades(self, arg):
      tr = self.tr
      xs = sorted(tr.trades + tr.pretend, key = trade_order)
      if (not xs):
         print("No trades recorded. Use 'buy TICKER [price] [shares]'.")
         return
      closed = book(tr.trades + tr.pretend)[1]
      print("\n%4s  %-16s %-6s %-4s %8s %9s %12s %11s" % (
         "#", "Time", "Ticker", "Side", "Shares", "Price", "Amount",
         "Realized"))
      for x in xs:
         real = ""
         if (x["side"] == "sell"):
            real = usd(sum(c["pnl"] for c in closed
                           if (c["sell"] == x["id"])))
         print("%4d%s %-16s %-6s %-4s %8g %9.2f %12s %11s" % (
            x["id"], "*" if (x.get("practice")) else " ",
            x["time"].replace("T", " "), x["ticker"], x["side"],
            x["shares"], x["price"],
            format(x["shares"] * x["price"], ",.2f"), real))
      if (tr.pretend):
         print("(* = practice trade, not yet saved)")
      print("Correct one with 'edittrade # shares=N price=P time=YYYY-MM-"
            "DD HH:MM',\nremove one with 'deltrade #'.")

   ########################################################################
   def do_edittrade(self, arg):
      tr = self.tr
      a = arg.split()
      usage = ("usage: edittrade # [shares=N] [price=P] [time=YYYY-MM-DD "
               "HH:MM]   ('trades' lists the #)")
      try:
         tid = int(a.pop(0))
         ch = {}
         while (a):
            k, _, v = a.pop(0).partition("=")
            if (k == "time" and a and ":" in a[0] and "=" not in a[0]):
               v += " " + a.pop(0)
            if (k not in ("shares", "price", "time") or not v):
               raise(ValueError(k))
            ch[k] = v if (k == "time") else float(v)
         if (not ch):
            raise(ValueError("nothing to change"))
      except (ValueError, IndexError):
         print(usage)
         return
      try:
         x = tr.edit(tid, ch.get("shares"), ch.get("price"), ch.get("time"))
      except (ExceptionClass, ValueError) as error:
         print("Not changed: %s" % error)
         return
      print("Trade #%d is now: %s %s %g sh @ %.2f at %s" % (
         x["id"], x["side"], x["ticker"], x["shares"], x["price"],
         x["time"].replace("T", " ")))

   ########################################################################
   def do_deltrade(self, arg):
      tr = self.tr
      try:
         tid = int(arg.strip())
         x = tr.trade(tid)
         tr.delete(tid)
      except ValueError:
         print("usage: deltrade #   ('trades' lists the #)")
         return
      except ExceptionClass as error:
         print("Not deleted: %s" % error)
         return
      print("Deleted trade #%d: %s %s %g sh @ %.2f at %s" % (
         tid, x["side"], x["ticker"], x["shares"], x["price"],
         x["time"].replace("T", " ")))

   ########################################################################
   def do_realpositions(self, arg):
      tr = self.tr
      keep = tr.asof
      tr.asof = None
      try:
         led = tr.held(practice = False)
         if (not led):
            print("No real positions in %s." % LEDGER)
            return
         self.fresh(unique(q["ticker"] for q in led))
         tl = tr.timeline()
         self.show_positions(led, "real positions (%s) as of the latest "
                             "bar, %s" % (LEDGER, tl[-1].strftime(
                                "%Y-%m-%d %H:%M") if (len(tl)) else "?"))
      finally:
         tr.asof = keep

   do_realposition = do_realpositions

   ########################################################################
   def do_savepositions(self, arg):
      tr = self.tr
      if (not tr.practice()):
         print("No practice trades to save.")
         return
      n = tr.save_practice()
      print("Saved %d practice trade%s to %s" % (
         n, "" if (n == 1) else "s", LEDGER))

   do_saveposition = do_savepositions

   ########################################################################
   def do_track(self, arg):
      tr = self.tr
      if (not self.last_buys):
         print("Run 'recommend' first (and have at least one BUY).")
         return
      n = int(arg) if (arg.strip().isdigit()) else len(self.last_buys)
      have = set(q["ticker"] for q in tr.held())
      for e in self.last_buys[:n]:
         if (e["t"] in have):
            continue
         r = e["r"]
         tr.record("buy", e["t"], 1, r["Close"], atr = r["atr"])
         print("Tracking 1 share of %s @ %.2f ('edittrade' changes the "
               "count)" % (e["t"], r["Close"]))

   ########################################################################
   def do_buy(self, arg):
      tr = self.tr
      a = arg.split()
      usage = ("usage: buy TICKER [price] [shares]   (price defaults to "
               "the current price,\n       shares to 1)")
      if (not a):
         print(usage)
         return
      t = a[0].upper()
      self.fresh([t])
      df = tr.frame(t)
      if (df is None and len(a) < 2):
         print("%s\nNo usable price data for %s; give the price." % (
            usage, t))
         return
      try:
         px = float(a[1]) if (len(a) > 1) else float(df["Close"].iloc[-1])
         sh = float(a[2]) if (len(a) > 2) else 1.0
         atr = float(df["atr"].iloc[-1]) if (df is not None) else None
         x = tr.record("buy", t, sh, px, atr = atr)
      except ValueError:
         print(usage)
         return
      except ExceptionClass as error:
         print("Not recorded: %s" % error)
         return
      tg = target_px(px, atr, tr.cfg) if (atr) else 0.0
      sp = trail_stop(px, atr, tr.cfg) if (atr) else 0.0
      print(textwrap.fill("Recorded trade #%d: bought %g %s @ %.2f%s. "
                          "Target %s, trailing stop starts at %s." % (
                             x["id"], sh, t, px, " (practice)" if
                             (x.get("practice")) else "", f2(tg) if
                             (tg > 0) else "off", f2(sp) if (sp > 0)
                             else "off"), 78))
      if (t not in tr.watch()):
         print("(%s is not on the watchlist; it is followed while held)"
               % t)

   ########################################################################
   def do_sell(self, arg):
      tr = self.tr
      a = arg.split()
      usage = ("usage: sell TICKER [price] [shares|all]   (price defaults "
               "to the current\n       price, shares to 1)")
      if (not a):
         print(usage)
         return
      t = a[0].upper()
      have = tr.shares(t)
      if (not have):
         print("No open position in %s." % t)
         return
      df = tr.frame(t)
      try:
         px = (     float(a[1]) if (len(a) > 1)
               else float(df["Close"].iloc[-1]))
         sh = (     have if (len(a) > 2 and a[2].lower() == "all")
               else float(a[2]) if (len(a) > 2) else 1.0)
         x = tr.record("sell", t, sh, px)
      except (ValueError, TypeError):
         print(usage)
         return
      except ExceptionClass as error:
         print("Not recorded: %s (%g held)" % (error, have))
         return
      real = sum(c["pnl"] for c in tr.closed() if (c["sell"] == x["id"]))
      print(textwrap.fill("Recorded trade #%d: sold %g %s @ %.2f%s, "
                          "realized %s; %g share%s left." % (
                             x["id"], sh, t, px, " (practice)" if
                             (x.get("practice")) else "", usd(real),
                             have - sh, "" if (have - sh == 1) else "s"),
                          78))

   ########################################################################
   def do_compare(self, arg):
      tr, cfg = self.tr, self.tr.cfg
      if (tr.asof is None):
         print("No future data after this bar. Use 'asof -N' or 'asof "
               "DATE HH:MM' first.")
         return
      n = int(arg) if (arg.strip().isdigit()) else int(cfg["ahead_bars"])
      print("\n=== signals as of %s vs. what happened next ===\n"
            "(over the next %d bars and by the day's close)" % (
               tr.now().strftime("%Y-%m-%d %H:%M"), n))
      print("%-6s %-13s %9s %9s %9s  %s" % (
         "Ticker", "Call", "Price", "Next %db" % n, "ToClose", "Result"))
      for e in self.ranked(tr.tickers()):
         r = e["r"]
         if (r is None):
            continue
         full = tr.bars[e["t"]]
         after = full[full.index > r["time"]]
         if (not len(after)):
            continue
         px = r["Close"]
         nxt = after["Close"].iloc[min(n, len(after)) - 1] / px - 1
         same = after[after.index.normalize() == r["day"]]
         toc = same["Close"].iloc[-1] / px - 1 if (len(same)) else 0.0
         calls = [tr.judge_pos(q)["act"] for q in tr.held(e["t"])]
         if (not calls):
            calls = ["BUY %s" % KINDS[e["kind"]] if (e["kind"])
                     else "watch"]
         for call in calls:
            if (call.startswith("BUY")):
               res = "WIN" if (nxt > 0) else "loss"
            elif (call == "SELL"):
               res = "sell was %s" % ("RIGHT" if (nxt < 0) else "WRONG")
            elif (call == "HOLD"):
               res = "hold was %s" % ("RIGHT" if (nxt >= 0) else "WRONG")
            else:
               res = ""
            print("%-6s %-13s %9.2f %9s %9s  %s" % (
               e["t"], call, px, pct(nxt), pct(toc), res))
      print("\n(One bar is a tiny sample; use 'simulate' for a broader "
            "verdict.)")

   ########################################################################
   def do_range(self, arg):
      tr, cfg = self.tr, self.tr.cfg
      ts = self.args(arg) or tr.tickers()
      if (not ts):
         print("usage: range TICKER [TICKER ...]")
         return
      self.fresh(ts)
      print("\n=== day range as of %s ===" % tr.now().strftime(
         "%Y-%m-%d %H:%M"))
      for t in ts:
         df = tr.frame(t)
         if (df is None):
            print("\n%-6s not enough price data yet" % t)
            continue
         r = df.iloc[-1]
         today = df[df["day"] == r["day"]]
         c, atr = r["Close"], r["atr"]
         print("\n%s  %s bar  price %.2f (%s vs yesterday's close)" % (
            t, hhmm(df.index[-1]), c,
            pctd(c / r["pdc"] - 1, r["pdc"]) if (np.isfinite(r["pdc"]))
            else "n/a"))
         print("   day:       open %.2f  low %.2f (%s)  high %.2f (%s)" %
               (r["dopen"], r["dlo"], hhmm(today["Low"].idxmin()),
                r["dhi"], hhmm(today["High"].idxmax())))
         if (not np.isfinite(atr)):
            print("   expected:  needs 14 days of daily history (ATR)")
            continue
         print("   typical:   daily ATR(14) %.2f (%.2f%%); today has used "
               "%.2f ATR" % (atr, 100 * atr / c, r["used"]))
         print("   expected:  low %.2f  high %.2f  (price is %.0f%% of the "
               "way up)" % (r["projlo"], r["projhi"], 100 * r["pos"]))
         print("   yesterday: high %.2f  close %.2f | VWAP %.2f  RSI %.0f"
               % (r["pdh"], r["pdc"], r["vwap"], r["rsi"]))
         mins = int(cfg["ahead_bars"]) * BAR_MIN
         print("   heading:   trend t=%+.1f over %d bars -> %.2f in %d "
               "min (likely %.2f-%.2f)" % (
                  r["tstat"], cfg["trend_bars"], r["proj"], mins,
                  r["proj"] - r["band"], r["proj"] + r["band"]))
         if (r["used"] >= 1.0):
            print("   note:      the day has already moved a full ATR; "
                  "new extremes are less likely")
      self.stale_note()

   ########################################################################
   def do_follow(self, arg):
      tr, cfg = self.tr, self.tr.cfg
      a = arg.split()
      if (not a):
         print("usage: follow TICKER [YYYY-MM-DD]")
         return
      t = a[0].upper()
      self.fresh([t])
      df = tr.frame(t)
      if (df is None):
         print("usage: follow TICKER [YYYY-MM-DD]   (not enough price "
               "data for %s)" % t)
         return
      try:
         day = (     pd.Timestamp(a[1]).normalize() if (len(a) > 1)
                else df["day"].iloc[-1])
      except ValueError:
         print("usage: follow TICKER [YYYY-MM-DD]")
         return
      sel = np.where(df["day"].values == np.datetime64(day))[0]
      if (len(sel) == 0):
         print("No bars for %s on %s (data: %s to %s)." % (
            t, day.strftime("%Y-%m-%d"), df.index[0].strftime("%Y-%m-%d"),
            df.index[-1].strftime("%Y-%m-%d")))
         return
      events = {}
      trades = run_ticker(df, buy_signals(df, cfg), cfg, events)
      print("\n=== following %s through %s, bar by bar (positions carried "
            "in from\n    earlier days count) ===" % (
               t, day.strftime("%Y-%m-%d")))
      print("%5s %8s %8s %8s %8s %8s %8s %5s  %s" % (
         "Bar", "Close", "DayLow", "DayHigh", "ExpLow", "ExpHigh", "VWAP",
         "t", "Events"))
      for i in sel:
         r = df.iloc[i]
         print("%5s %8.2f %8s %8s %8s %8s %8s %5s  %s" % (
            hhmm(df.index[i]), r["Close"], f2(r["dlo"]), f2(r["dhi"]),
            f2(r["projlo"]), f2(r["projhi"]), f2(r["vwap"]),
            "%+.1f" % r["tstat"] if (np.isfinite(r["tstat"])) else "",
            "; ".join(events.get(i, []))))
      mine = [x for x in trades if (x["i1"] >= sel[0] and
                                    x["i0"] <= sel[-1])]
      if (mine):
         print("\nTrades touching this day:")
         for x in mine:
            print("   %-8s %s -> %s  %.2f -> %.2f  %s  (%s)" % (
               KINDS[x["kind"]],
               df.index[x["i0"]].strftime("%m-%d %H:%M"),
               df.index[x["i1"]].strftime("%m-%d %H:%M"),
               x["px0"], x["px1"], pctd(x["ret"], x["px0"]),
               EXIT_NAMES[x["why"]]))
      else:
         print("\nNo trades touched this day.")

   do_replay = do_follow

   ########################################################################
   def do_monitor(self, arg):
      tr = self.tr
      quiet = "quiet" in arg.lower()
      if (not tr.tickers()):
         print("Nothing to monitor. Use 'watch TICKER' first.")
         return
      poll = 1 if (tr.synthetic) else max(60, int(tr.cfg["poll"]))
      print("Monitoring %s %s; Ctrl-C returns to the prompt." % (
         " ".join(tr.tickers()),
         "every bar (synthetic clock)" if (tr.synthetic)
         else "every %d s" % poll))
      if (tr.asof is not None):
         print("Replaying from %s, one bar a second, up to the latest "
               "bar." % tr.now().strftime("%Y-%m-%d %H:%M"))
      last, waiting = {}, False
      try:
         while (True):
            if (tr.asof is not None):
               self.poll_once(quiet, last)
               tl = tr.timeline()
               i = int(tl.searchsorted(tr.asof)) + 1
               tr.asof = None if (i >= len(tl) - 1) else tl[i]
               if (tr.asof is None):
                  print("\nCaught up with the latest bar; monitoring "
                        "live now.")
               time.sleep(1)
               continue
            now = tr.now()
            if (not tr.synthetic and not market_open(now)):
               if (not waiting):
                  print("%s market closed; waiting for the session "
                        "(Ctrl-C to stop)" % now.strftime("%a %H:%M"))
                  waiting = True
               time.sleep(poll)
               continue
            waiting = False
            tr.sync()
            self.poll_once(quiet, last)
            if (tr.synthetic):
               if (not tr.source.advance()):
                  print("End of the synthetic data.")
                  break
               time.sleep(poll)
            else:
                                # wake just after each bar closes (Yahoo
                                # needs a few seconds to publish it)
               time.sleep(poll - time.time() % poll + 20)
      except KeyboardInterrupt:
         print("\nMonitor stopped.")

   ########################################################################
   def poll_once(self, quiet, last):
      """
      name: poll_once
      use: one monitor round: the table (unless quiet) and the alerts
      """
      tr = self.tr
      ts = tr.tickers()
      ev = self.ranked(ts)
      if (not quiet):
         print("\n--- %s ---" % tr.now().strftime("%Y-%m-%d %H:%M"))
         self.show_picks(ev, brief = True)
         self.show_positions()
      held = set(q["ticker"] for q in tr.held())
      for e in ev:
         if (e["r"] is not None and e["t"] not in held):
            sig = "BUY %s" % KINDS[e["kind"]] if (e["kind"]) else "-"
            self.alert((e["t"], None), sig, e["t"], e["r"], e["why"],
                       last)
      for q in tr.held():
         jp = tr.judge_pos(q)
         if (jp is not None):
            self.alert((q["ticker"], q["id"]), jp["act"], q["ticker"],
                       jp["r"], jp["why"], last)

   ########################################################################
   def alert(self, key, sig, t, r, why, last):
      """
      name: alert
      use: prints an ALERT when a BUY or SELL signal newly appears
      post: last remembers each stock's / position's previous signal
      """
      old = last.get(key)
      last[key] = sig
      if (sig == old or not sig.startswith(("BUY", "SELL"))):
         return
      bell = "\a" if (self.tr.cfg["bell"]) else ""
      print("%s>>> ALERT %s %s %s @ %.2f" % (bell, hhmm(r["time"]), sig,
                                             t, r["Close"]))
      print(wrap(why, 6))

   ########################################################################
   def do_simulate(self, arg):
      tr, cfg = self.tr, self.tr.cfg
      a = arg.split()
      days = int(a.pop(0)) if (a and a[0].isdigit()) else 0
      ts = self.args(" ".join(a)) or tr.tickers()
      if (not ts):
         print("usage: simulate [days] [TICKER ...]   (default: all "
               "loaded days, the watchlist)")
         return
      self.fresh(ts)
      end = pd.Timestamp(tr.now())
      cut = end - pd.Timedelta(days = days) if (days) else None
      print("Simulating the signals on %d stock(s) over %s ending %s..." % (
         len(ts), "%d days" % days if (days) else "all loaded days",
         end.strftime("%Y-%m-%d %H:%M")))
      allt, rows, rnd, span = [], [], [], []
      for t in ts:
         df = tr.frame(t)
         if (df is None):
            rows.append("%-6s not enough price data" % t)
            continue
         trades = run_ticker(df, buy_signals(df, cfg), cfg)
         ok = df["atr"].notna().to_numpy(copy = True)
         if (cut is not None):
            ok &= (df.index >= cut)
            trades = [x for x in trades if (df.index[x["i0"]] >= cut)]
         ok = np.where(ok)[0]
         if (len(ok) == 0):
            rows.append("%-6s needs 14 days of daily history first" % t)
            continue
         span.append((df.index[ok[0]], df.index[-1]))
         bh = df["Close"].values[-1] / df["Open"].values[ok[0]] - 1
         if (not trades):
            rows.append("%-6s %6d %5s %9s %9s %9s" % (t, 0, "-", "-", "-",
                                                      pct(bh)))
            continue
         ret = np.array([x["ret"] for x in trades])
         bars = np.mean([x["bars"] for x in trades])
         base, nb = random_base(df, bars)
         rnd.append((base, len(trades)))
         allt += [dict(x, t = t, t0 = df.index[x["i0"]],
                       t1 = df.index[x["i1"]]) for x in trades]
         rows.append("%-6s %6d %4.0f%% %9s %9s %9s %7.1f %9s" % (
            t, len(trades), 100 * (ret > 0).mean(), pct(ret.mean()),
            pct(np.prod(1 + ret) - 1), pct(bh), bars, pct(base)))
      tl = sorted(allt, key = lambda x: x["t1"])
      if (tl):
         print("\n%-6s %-11s %-11s %8s %8s %8s  %s" % (
            "Ticker", "Entry", "Exit", "In", "Out", "Return",
            "Entry / exit reason"))
         for x in tl[-40:]:
            print("%-6s %-11s %-11s %8.2f %8.2f %8s  %s / %s" % (
               x["t"], x["t0"].strftime("%m-%d %H:%M"),
               x["t1"].strftime("%m-%d %H:%M"), x["px0"], x["px1"],
               pct(x["ret"]), KINDS[x["kind"]], x["why"]))
         if (len(tl) > 40):
            print("(showing the last 40 of %d trades)" % len(tl))
      print("\n%-6s %6s %5s %9s %9s %9s %7s %9s" % (
         "Ticker", "Trades", "Win%", "AvgTrade", "Total", "Buy&Hold",
         "AvgBars", "Random"))
      for line in rows:
         print(line)
      if (not allt):
         print("\nNo trades. The signals never fired with the current "
               "settings ('set' shows them).")
         return
      ret = np.array([x["ret"] for x in allt])
      base = sum(b * n for b, n in rnd) / sum(n for b, n in rnd)
      print("%-6s %6d %4.0f%% %9s %9s %9s %7.1f %9s" % (
         "ALL", len(allt), 100 * (ret > 0).mean(), pct(ret.mean()),
         "", "", np.mean([x["bars"] for x in allt]), pct(base)))
      print("\nBy entry:")
      for k, name in KINDS.items():
         self.bt_line(name, [x for x in allt if (x["kind"] == k)])
      print("By exit:")
      for why, name in EXIT_NAMES.items():
         self.bt_line(name, [x for x in allt if (x["why"] == why)])
      edge = ret.mean() - base
      wins = (ret > 0).mean()
      print("\nWindow %s -> %s | %d trades | win rate %.0f%%" % (
         min(s[0] for s in span).strftime("%Y-%m-%d"),
         max(s[1] for s in span).strftime("%Y-%m-%d"), len(allt),
         100 * wins))
      print("Avg trade %s | random entry held as long %s | edge per trade "
            "%s" % (pct(ret.mean()), pct(base), pct(edge)))
      if (len(allt) < 100):
         print("Only %d trades: too few to tell skill from luck; watch "
               "more stocks for a\nbroader test." % len(allt))
      else:
         se = ret.std(ddof = 1) / math.sqrt(len(ret))
         print("Edge t-statistic %.1f (|t| above ~2 suggests the edge is "
               "more than luck)." % (edge / (se + 1e-12)))
      print("Slippage %.2f%%/side included; no taxes. Past ~60 days only; "
            "backtest only." % cfg["slip"])

   do_backtest = do_simulate

   ########################################################################
   def bt_line(self, label, trades):
      """
      name: bt_line
      use: one summary line of a group of simulated trades
      """
      if (not trades):
         return
      ret = np.array([x["ret"] for x in trades])
      print("   %-14s n=%4d  win %3.0f%%  avg %8s  avg bars %5.1f" % (
         label, len(trades), 100 * (ret > 0).mean(), pct(ret.mean()),
         np.mean([x["bars"] for x in trades])))

   ########################################################################
   def do_set(self, arg):
      tr = self.tr
      a = arg.split()
      if (not a):
         for k in DEFAULTS:
            v = tr.cfg[k]
            mark = ("" if (v == DEFAULTS[k])
                    else "  (default %s)" % DEFAULTS[k])
            line = "%-12s %-7s %s%s" % (k, v, SETTING_HELP[k], mark)
            print(textwrap.fill(line, 78, initial_indent = "  ",
                                subsequent_indent = " " * 24))
         return
      if (len(a) != 2 or a[0] not in DEFAULTS):
         print("usage: set NAME VALUE   NAME is one of: %s" %
               ", ".join(DEFAULTS))
         return
      k = a[0]
      if (a[1].lower() == "default"):
         v = DEFAULTS[k]
      else:
         try:
            v = float(a[1])
         except ValueError:
            print("VALUE must be a number")
            return
         if (k in INTEGERS):
            v = int(v)
         if (k == "no_buy_after" and not (930 <= v <= 1600 and
                                          v % 100 < 60)):
            print("no_buy_after is a time HHMM between 0930 and 1600")
            return
         if (v < 0):
            print("VALUE must not be negative")
            return
      tr.cfg[k] = v
      tr.save_settings()
      print("%s = %s" % (k, a[1]))

   ########################################################################
   def do_refresh(self, arg):
      tr = self.tr
      if (not tr.tickers()):
         print("Nothing to refresh. Use 'watch TICKER' first.")
         return
      tr.sync(force = True)
      tl = tr.timeline()
      print("Reloaded %d stock(s) through %s." % (
         len(tr.tickers()),
         tl[-1].strftime("%Y-%m-%d %H:%M") if (len(tl)) else "?"))

###########################################################################
###
### t h e    w i n d o w
###
###########################################################################

                                # the chart window follows stock_ticker.py:
                                # the same rows of controls, quote table,
                                # hover readout, panels, volume overlay
                                # and crosshair
CHILD     = "STOCK_TRACKER_CHILD"
READY     = "ready"
LOG       = os.path.join(tempfile.gettempdir(), "stock_tracker.log")

CHARTS    = ["Line", "Candles"]
UP        = "#26a69a"
DOWN      = "#ef5350"
                                # volume line: green where more of a
                                # bar's volume was buying, red where more
                                # was selling (see buy_share)
VOL_BUY   = "#2e7d32"
VOL_SELL  = "#c62828"
VOL_EVEN  = "#9e9e9e"
VWAP_CLR  = "#ff9800"
RANGE_CLR = "#90a4ae"
REB_CLR   = "#1e88e5"
BRK_CLR   = "#2e7d32"
SELL_CLR  = "#c62828"
HINT      = "Hover over chart for details"
                                # quote table columns: (alignment, (left,
                                # right) padding); "w" = left justify,
                                # "e" = right justify
QUOTE_COLUMNS = [("w", (4, 4)),
                 ("e", (6, 0)),
                 ("e", (12, 0)),
                 ("w", (3, 0)),
                 ("e", (0, 0)),
                 ("w", (0, 0)),
                 ("w", (14, 0)),
                 ("e", (4, 0)),
                 ("w", (8, 0)),
                 ("e", (4, 0)),
                 ("w", (14, 0)),
                 ("e", (4, 0)),
                 ("w", (14, 0)),
                 ("w", (14, 0)),
                 ("e", (4, 0)),
                 ("e", (8, 0))]

                                # crosshair shows only when the mouse is
                                # this many screen pixels from a bar
NEAR      = 20
                                # horizontal grid: panel split into 3, 6,
                                # 9.. equal gaps, as many as fit with each
                                # gap at least this many pixels tall
GRID_GAP  = 40
TIME_FMT  = "%H:%M %a %d-%b-%y"

                                # time span => sessions of 5-minute bars
                                # shown (the signals always use all of
                                # the ~60 days loaded)
SPANS     = {"1d"  : 1,
             "2d"  : 2,
             "5d"  : 5,
             "10d" : 10,
             "20d" : 20}

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
def volume_label(value, tick_number):
   """
   name: volume_label
   post: 1500 -> "1.5K", 2534000 -> "2.53M", 3e9 -> "3B"
   """
   for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
      if (abs(value) >= limit):
         return(f"{value / limit:.3g}{suffix}")
   return(f"{value:.3g}")

###########################################################################
def seconds_label(seconds):
   """
   name: seconds_label
   use: turns -r seconds into a REFRESH label, adding one if needed
   """
   for label, value in REFRESH.items():
      if (value == seconds):
         return(label)
   label          = f"{seconds}s"
   REFRESH[label] = seconds
   return(label)

###########################################################################
def buy_share(view):
   """
   name: buy_share
   use: estimated share (0 to 1) of each bar's volume that was buying
   post: returns a numpy array; above 0.5 means more buying than selling
   imp: yahoo does not report buyer- and seller-initiated volume, so it
        is estimated from where the bar closed within its range: a close
        at the high counts as all buying, at the low as all selling, in
        the middle as half and half. a bar with no range (high == low)
        counts by its close against the previous close
   """
   h = view["High"].to_numpy(dtype = float)
   l = view["Low"].to_numpy(dtype = float)
   c = view["Close"].to_numpy(dtype = float)
   prev = np.r_[view["Open"].to_numpy(dtype = float)[:1], c[:-1]]
   rng = h - l
   flat = np.where(c > prev, 1.0, np.where(c < prev, 0.0, 0.5))
   return(np.where(rng > 0, (c - l) / np.where(rng > 0, rng, 1.0), flat))

###########################################################################
def gui_modules():
   """
   name: gui_modules
   use: imports tkinter and matplotlib for the window
   post: returns True when they are available, else prints why and
         returns False
   imp: imported here, not at the top, so the text prompt (-t) still
        works on a machine without tkinter or matplotlib, and only in
        the process that opens the window, so the console copy that
        starts it in the background (see detach) does not wait for them
   """
   global tkinter, ttk, messagebox, Figure, FuncFormatter, Locator
   global MaxNLocator, FigureCanvasTkAgg, NavigationToolbar2Tk
   global blended_transform_factory, LineCollection
   try:
      import tkinter
      from tkinter import messagebox
      from tkinter import ttk
      import matplotlib
      matplotlib.use("TkAgg")
      from matplotlib.figure                 import Figure
      from matplotlib.ticker                 import FuncFormatter
      from matplotlib.ticker                 import Locator
      from matplotlib.ticker                 import MaxNLocator
      from matplotlib.transforms             import \
           blended_transform_factory
      from matplotlib.collections            import LineCollection
      from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
      from matplotlib.backends.backend_tkagg import NavigationToolbar2Tk
   except ImportError as error:
      print("%s: the chart window needs tkinter and matplotlib (%s);\n"
            "   pip install matplotlib; on RHEL/Alma/Oracle Linux: dnf "
            "install python3-tkinter;\n   or use -t for the text prompt"
            % (SELF, error), file = sys.stderr)
      return(False)
   return(True)

###########################################################################
def grid_locator():
   """
   name: grid_locator
   use: a matplotlib Locator whose lines divide an axis into a multiple
        of 3 equal gaps: 3 gaps on a short panel, 6 once it is twice as
        tall, then 9, 12 ...
   post: returns a new locator instance
   imp: the class is built here because Locator is only imported with
        the window (see gui_modules)
   """

   class GridLocator(Locator):

      def __call__(self):
         low, high = self.axis.get_view_interval()
         return(self.tick_values(low, high))

      def tick_values(self, vmin, vmax):
         gaps = self.gaps()
         step = (vmax - vmin) / gaps
         return([vmin + step * line for line in range(1, gaps)])

      def gaps(self):
         if (self.axis is None):
            return(3)
         height = self.axis.axes.get_window_extent().height
         count  = int(height // GRID_GAP)
         return(max(3, count - count % 3))

   return(GridLocator())

###########################################################################
class TrackerWindow(object):
   """
   name: TrackerWindow
   use: tkinter window like stock_ticker.py: ticker entry, quote table
        with the day's low/high, expected range and signal, hover
        readout, an alert log, and one line or candlestick panel per
        stock with VWAP, the expected range, BUY markers and, for held
        positions, the buy, the target and the trailing stop; buy and
        sell dialogs and a trades / profit and loss window
   pre: tkinter display available (DISPLAY set on linux)
   imp: prices are fetched and the signals worked out on a worker thread
        so the window never freezes; results are handed back to the gui
        thread via root.after()
   """

   ########################################################################
   def __init__(self, root, tracker, refresh_seconds = 60, chart = "Line",
                span = "1d", reload = False):
      self.tr        = tracker
                                # -d: the first fetch re-downloads all
                                # history, after the window is open
      self.reload    = reload
      self.root      = root
      self.after_id  = None
      self.busy      = False
      self.first     = True
                                # what is on screen, used by the hover
                                # readout: [(axes, symbol, view), ...]
      self.panels    = []
      self.crosshair = {}
      self.hosts     = {}
      self.timeline  = pd.DatetimeIndex([])
      self.last      = {}
      self.quote_rows = 0
                                # latest close and daily ATR per stock,
                                # for the trade dialogs and profit / loss
      self.prices    = {}
      self.atrs      = {}
      self.trades_win = None

      root.title("Stock Tracker")
      root.geometry("1350x820")

      ###
      ### row 1: what to track
      ###
      bar = ttk.Frame(root, padding = (6, 6, 6, 2))
      bar.pack(side = tkinter.TOP, fill = tkinter.X)

      ttk.Label(bar, text = "Ticker(s):").pack(side = tkinter.LEFT)
      self.ticker_var = tkinter.StringVar(value = " ".join(tracker.watch()))
      entry = ttk.Entry(bar, textvariable = self.ticker_var, width = 30)
      entry.pack(side = tkinter.LEFT, padx = (4, 10))
      entry.bind("<Return>", lambda event: self.refresh())

      self.chart_var = tkinter.StringVar(value = chart)
      self.combo(bar, "Chart:", self.chart_var, CHARTS, 8,
                 lambda event: self.refresh())

      ttk.Button(bar, text = "Refresh now",
                 command = self.refresh).pack(side = tkinter.LEFT,
                                              padx = 4)
      ttk.Button(bar, text = "Record buy...",
                 command = self.record_buy).pack(side = tkinter.LEFT,
                                                 padx = 4)
      ttk.Button(bar, text = "Record sell...",
                 command = self.record_sell).pack(side = tkinter.LEFT,
                                                  padx = 4)
      ttk.Button(bar, text = "Trades / P&L...",
                 command = self.show_trades).pack(side = tkinter.LEFT,
                                                  padx = 4)

      self.status_var = tkinter.StringVar(value = "")
      ttk.Label(bar, textvariable = self.status_var,
                foreground = "gray").pack(side = tkinter.RIGHT)

      ###
      ### row 2: time scale and refresh rate
      ###
      bar = ttk.Frame(root, padding = (6, 2, 6, 4))
      bar.pack(side = tkinter.TOP, fill = tkinter.X)

      self.span_var = tkinter.StringVar(value = span)
      self.combo(bar, "Time span:", self.span_var, list(SPANS), 5,
                 lambda event: self.refresh())
                                # the signals are built on 5-minute bars,
                                # so that is the only bar size offered
      self.interval_var = tkinter.StringVar(value = "5m")
      self.combo(bar, "Bar size:", self.interval_var, ["5m"], 5,
                 lambda event: None)

      self.refresh_var = tkinter.StringVar(
                            value = seconds_label(refresh_seconds))
      self.combo(bar, "Auto-refresh every:", self.refresh_var,
                 list(REFRESH), 5, lambda event: self.schedule())

      ###
      ### row 3: latest quote per ticker; row 4: hover readout; row 5:
      ### alerts
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

      self.alerts = tkinter.Text(root, height = 5, wrap = "word",
                                 font = ("TkFixedFont", 10),
                                 background = "#f7f7f7",
                                 relief = "flat", padx = 8, pady = 3)
      self.alerts.tag_configure("BUY", foreground = BRK_CLR)
      self.alerts.tag_configure("SELL", foreground = SELL_CLR)
      self.alerts.tag_configure("why", foreground = "#555555")
      self.alerts.insert("end", "Alerts: BUY and SELL signals appear "
                         "here as they come up (newest first).\n", "why")
      self.alerts.configure(state = "disabled")
      self.alerts.pack(side = tkinter.TOP, fill = tkinter.X)

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
      name: combo
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
   def tickers(self):
      """
      name: tickers
      post: returns list of upper-case symbols, commas or spaces allowed
      """
      text = self.ticker_var.get().replace(",", " ").upper()
      return([symbol for symbol in text.split() if (symbol)])

   ########################################################################
   def refresh(self):
      """
      name: refresh
      use: starts a background fetch unless one is already running
      post: the ticker entry becomes the watchlist
      """
      if (self.busy):
         return
      symbols = self.tickers()
      if (not symbols):
         self.status_var.set("enter a ticker symbol")
         return
      if (symbols != self.tr.watch()):
         self.tr.save_watch(symbols)
      self.busy = True
      self.status_var.set("fetching...")
      threading.Thread(target = self.fetch, args = (symbols,),
                       daemon = True).start()

   ########################################################################
   def fetch(self, symbols):
      """
      name: fetch
      use: worker thread; downloads prices and works out every signal
      post: results keeps every followed symbol, in order: a dict of the
            indicator frame, BUY kinds, evaluation and positions, or a
            reason string when there is nothing to chart yet
      imp: DO NOT touch tk widgets here, tk is not thread safe. with
           synthetic data each refresh after the first moves the pretend
           clock one bar on, so the window can be seen updating
      """
      tr = self.tr
      try:
         if (tr.synthetic and not self.first):
            tr.source.advance()
         self.first = False
         held = [t for t in tr.tickers() if (t not in symbols)]
         tr.sync(symbols + held, force = self.reload)
         self.reload = False
         results = {}
         for t in symbols + held:
            df = tr.frame(t)
            if (df is None):
               results[t] = "no data yet"
               continue
            pos = []
            for q in tr.held(t):
               pos.append((q, tr.judge_pos(q)))
            results[t] = dict(df = df, kind = buy_signals(df, tr.cfg),
                              ev = tr.evaluate(t), pos = pos)
      except Exception as exception:
         results = dict((t, f"error: {exception}") for t in symbols)
      self.root.after(0, self.draw, results)

   ########################################################################
   def draw(self, results):
      """
      name: draw
      use: gui thread; redraws quotes, alerts and panels from fetched data
      post: every symbol gets a panel; one with no data yet gets an empty
            panel that the next auto-refresh fills in
      """
      self.busy = False
      for child in self.quote_frame.winfo_children():
         child.destroy()
      self.figure.clear()
      self.panels    = []
      self.crosshair = {}
      self.hosts     = {}
      self.hover_var.set(HINT)
      span = self.span_var.get()
      self.candles = (self.chart_var.get() == "Candles")

      self.quote_rows = 0
      ttk.Label(self.quote_frame,
                text = f"Change over {span} (5m bars):").grid(
                   row = 0, column = 0, sticky = "nw", padx = (0, 4))
                                # every panel shares one timeline, every
                                # bar time shown for any ticker, and bars
                                # sit at their position on it so nights
                                # and weekends leave no gaps
      views = {}
      for symbol, res in results.items():
         if (isinstance(res, str)):
            continue
         df = res["df"]
         days = df["day"].unique()[-SPANS[span]:]
         views[symbol] = df[df["day"] >= days[0]]
      self.timeline = pd.DatetimeIndex([])
      for view in views.values():
         self.timeline = self.timeline.union(view.index)

      top = None
      for symbol, res in results.items():
         if (not isinstance(res, str)):
            self.prices[symbol] = float(res["df"]["Close"].iloc[-1])
            self.atrs[symbol]   = float(res["df"]["atr"].iloc[-1])
      for number, (symbol, res) in enumerate(results.items(), 1):
         axes = self.figure.add_subplot(len(results), 1, number,
                                        sharex = top)
         top  = top or axes
         axes.set_ylabel(symbol)
         axes.grid(True, alpha = 0.3)
         if (number < len(results)):
            axes.tick_params(axis = "x", labelbottom = False)
         if (isinstance(res, str)):
            self.draw_empty(axes, symbol, res)
            continue
         self.draw_panel(axes, symbol, res, views[symbol], number == 1)
         self.alert_check(symbol, res)

      dates = self.timeline

      def label(position, tick_number):
         index = int(round(position))
         return(     dates[index].strftime(TIME_FMT)
                if (0 <= index < len(dates))
                else "")

      for axes in list(self.hosts.values()) or self.figure.axes:
         axes.xaxis.set_major_locator(MaxNLocator(7, integer = True))
         axes.xaxis.set_major_formatter(FuncFormatter(label))
         axes.set_xlim(-1, max(len(dates), 1))
         axes.tick_params(axis = "x", labelsize = 8)
         for text in axes.get_xticklabels():
            text.set_rotation(30)
            text.set_horizontalalignment("right")
      if (not views):
         for axes in self.figure.axes:
            axes.set_xticks([])

                                # hidden crosshair per price axes, moved
                                # and shown by on_hover
      for axes in set(panel[0] for panel in self.panels):
         left, bottom = axes.get_xlim()[0], axes.get_ylim()[0]
         style = {"color"       : "#a0a0a0",
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
      if (self.trades_win is not None and self.trades_win.alive()):
         self.trades_win.fill()
      now = self.tr.source.now()
      self.status_var.set("updated %s | %s" % (
         pd.Timestamp.now().strftime("%H:%M:%S"),
         "synthetic clock %s" % now.strftime("%H:%M") if (self.tr.synthetic)
         else "market %s" % ("open" if (market_open(now)) else "closed")))
      self.schedule()

   ########################################################################
   def draw_panel(self, axes, symbol, res, view, legend):
      """
      name: draw_panel
      use: one stock: quote row, price as a line or candles, VWAP, the
           expected range, BUY markers, positions, and volume
      """
      cfg   = self.tr.cfg
      close = view["Close"]
      first = float(close.iloc[0])
      last  = float(close.iloc[-1])
      delta = last - first
      pc    = (delta / first * 100) if (first) else 0.0
      color = "green" if (delta >= 0) else "red"
      low   = float(view["Low"].min())
      high  = float(view["High"].max())
                                # split low..high into thirds: the price in
                                # the lower third colours the low, in the
                                # top third the high (the other black), in
                                # the middle third both are blue
      third = (last - low) / (high - low) if (high > low) else 0.5
      if (third < 1 / 3):
         low_color, high_color = color, "black"
      elif (third > 2 / 3):
         low_color, high_color = "black", color
      else:
         low_color, high_color = "blue", "blue"
      r = view.iloc[-1]
      expected = (     "n/a" if (not np.isfinite(r["projlo"]))
                  else f"{r['projlo']:,.2f} - {r['projhi']:,.2f}")
      signal, shade = self.signal_text(res)
                                # for a stock held: shares, average price
                                # paid and the gain or loss on them now
      own    = ()
      shares = sum(q["shares"] for q, jp in res["pos"])
      if (shares):
         paid = sum(q["shares"] * q["entry_price"] for q, jp in res["pos"])
         gain = shares * last - paid
         own  = (("Own", "black"),
                 (f"{shares:g} @ {paid / shares:,.2f}", "black"),
                 (f"{usd(gain)} ({pct(gain / paid)})",
                  BRK_CLR if (gain > 0) else SELL_CLR if (gain < 0)
                  else "black"))
      self.quote(color, symbol,
                 f"{last:,.2f}",
                 f"{delta:+,.2f}",
                 "(", f"{pc:+.2f}%", ")",
                 ("Low",  "black"), (f"{low:,.2f}",  low_color),
                 ("High", "black"), (f"{high:,.2f}", high_color),
                 ("Expected", "black"), (expected, "black"),
                 (signal, shade), *own)

      self.hosts[axes] = axes
      axes.yaxis.set_major_locator(grid_locator())
      axes.yaxis.set_major_formatter(
         FuncFormatter(lambda value, tick: f"{value:,.2f}"))
      x = self.timeline.get_indexer(view.index)
      if (self.candles):
         self.draw_candles(axes, view, x)
      else:
         axes.plot(x, close.values, color = "#37474f", linewidth = 1.5)
                                # VWAP restarts each day, so it is drawn
                                # day by day
      for k, day in enumerate(view["day"].unique()):
         m = (view["day"] == day).values
         axes.plot(x[m], view["vwap"].values[m], color = VWAP_CLR,
                   linewidth = 1, linestyle = "--",
                   label = "VWAP" if (k == 0) else None)
                                # today's expected low and high as they
                                # stand now, across today's bars
      today = (view["day"] == r["day"]).values
      if (np.isfinite(r["projlo"]) and today.any()):
         x0, x1 = x[today][0], x[-1]
         for level, name in ((r["projhi"], "exp high"),
                             (r["projlo"], "exp low")):
            axes.hlines(level, x0, x1, colors = RANGE_CLR, linewidth = 1,
                        linestyles = ":",
                        label = "expected range" if (name == "exp high")
                        else None)
                                # labels sit inside the band, so the
                                # high's goes under its line
            axes.text(x1, level, f"{name} {level:,.2f} ", fontsize = 8,
                      color = RANGE_CLR, horizontalalignment = "right",
                      verticalalignment = "top" if (name == "exp high")
                      else "bottom")
                                # a BUY marker where a signal starts (not
                                # on every bar it lasts)
      kind = res["kind"][-len(view):]
      prev = np.r_[0, kind[:-1]]
      for k, clr in ((2, BRK_CLR), (1, REB_CLR)):
         at = np.where((kind == k) & (prev != k))[0]
         if (len(at)):
            axes.scatter(x[at], view["Low"].values[at] * 0.998,
                         marker = "^", s = 50, color = clr, zorder = 6,
                         label = "BUY %s" % KINDS[k])
      for q, jp in res["pos"]:
         self.draw_position(axes, q, jp, view, x)
      self.draw_volume(axes, x, view)
      if (legend):
         axes.legend(loc = "upper left", fontsize = 8, ncol = 4)
      self.panels.append((axes, symbol, view))

   ########################################################################
   def draw_position(self, axes, q, jp, view, x):
      """
      name: draw_position
      use: a held position: the buy, its target and trailing stop from
           the buy on, and a SELL marker when the signal says sell
      """
      cfg = self.tr.cfg
      et  = pd.Timestamp(q["entry_time"]) - pd.Timedelta(minutes = BAR_MIN)
      i0  = int(self.timeline.searchsorted(et))
      end = len(self.timeline) - 1
      if (i0 > end):
         return
      axes.scatter([i0], [q["entry_price"]], marker = "o", s = 40,
                   color = "black", zorder = 7)
      if (jp is None):
         return
      st     = jp["st"]
      stop   = trail_stop(st["high"], st["atr"], cfg)
      target = target_px(q["entry_price"], st["atr"], cfg)
      for level, clr, name in ((target, BRK_CLR, "target"),
                               (stop, SELL_CLR, "stop")):
         if (level > 0):
            axes.hlines(level, i0, end, colors = clr, linewidth = 1,
                        linestyles = "-.")
            axes.text(i0, level, f" {name} {level:,.2f}", color = clr,
                      fontsize = 8, verticalalignment = "bottom")
      if (jp["act"] == "SELL"):
         axes.scatter([x[-1]], [view["High"].values[-1] * 1.002],
                      marker = "v", s = 60, color = SELL_CLR, zorder = 7,
                      label = "SELL")

   ########################################################################
   def signal_text(self, res):
      """
      name: signal_text
      use: the signal for the quote row and its colour
      """
      acts = [jp["act"] for q, jp in res["pos"] if (jp is not None)]
      if ("SELL" in acts):
         return("SELL", SELL_CLR)
      if (acts):
         return("HOLD", "black")
      k = res["ev"]["kind"]
      if (k):
         return("BUY %s" % KINDS[k], BRK_CLR if (k == 2) else REB_CLR)
      return("no signal", "gray")

   ########################################################################
   def alert_check(self, symbol, res):
      """
      name: alert_check
      use: adds an alert line when a BUY or SELL signal newly appears
      post: self.last remembers each stock's / position's last signal
      """
      todo = []
      if (res["pos"]):
         for q, jp in res["pos"]:
            if (jp is not None):
               todo.append(((symbol, q["id"]), jp["act"], jp["r"],
                            jp["why"]))
      else:
         ev = res["ev"]
         sig = "BUY %s" % KINDS[ev["kind"]] if (ev["kind"]) else "-"
         todo.append(((symbol, None), sig, ev["r"], ev["why"]))
      for key, sig, r, why in todo:
         old = self.last.get(key)
         self.last[key] = sig
         if (sig == old or not sig.startswith(("BUY", "SELL"))):
            continue
         self.alerts.configure(state = "normal")
                                # newest first, under the heading line
         self.alerts.insert("2.0", "%s  %s %s @ %.2f: " % (
                               hhmm(r["time"]), sig, symbol, r["Close"]),
                            sig.split()[0], why + "\n", "why")
         self.alerts.configure(state = "disabled")
         if (self.tr.cfg["bell"]):
            self.root.bell()

   ########################################################################
   def record_buy(self):
      """
      name: record_buy
      use: the buy dialog: ticker, shares (default 1), price (default the
           current price) and time (default now); records the buy
      """
      tr      = self.tr
      choices = unique(self.tickers() + [q["ticker"] for q in tr.held()])
      TradeDialog(self.root, "Record buy", "buy", choices,
                  choices[0] if (choices) else "", 1, None,
                  tr.stamp().replace("T", " "), self.prices,
                  self.buy_apply)

   ########################################################################
   def buy_apply(self, t, shares, price, when):
      """
      name: buy_apply
      use: records a buy entered in the dialog
      post: raises ExceptionClass when it cannot be recorded
      """
      x = self.tr.record("buy", t, shares, price, when, self.atrs.get(t))
      self.status_var.set("recorded trade #%d: bought %g %s @ %.2f" % (
         x["id"], shares, x["ticker"], price))
      self.changed()

   ########################################################################
   def record_sell(self):
      """
      name: record_sell
      use: the sell dialog: a held ticker, shares (default 1), price
           (default the current price) and time (default now); records
           the sale (the earliest shares bought are sold first)
      """
      tr      = self.tr
      choices = unique(q["ticker"] for q in tr.held())
      if (not choices):
         messagebox.showinfo("Record sell", "No stock is held.",
                             parent = self.root)
         return
      TradeDialog(self.root, "Record sell", "sell", choices, choices[0], 1,
                  None, tr.stamp().replace("T", " "), self.prices,
                  self.sell_apply, held = tr.shares)

   ########################################################################
   def sell_apply(self, t, shares, price, when):
      """
      name: sell_apply
      use: records a sale entered in the dialog
      post: raises ExceptionClass when it cannot be recorded
      """
      x = self.tr.record("sell", t, shares, price, when)
      real = sum(c["pnl"] for c in self.tr.books(upto = False)[1]
                 if (c["sell"] == x["id"]))
      self.status_var.set("recorded trade #%d: sold %g %s @ %.2f, "
                          "realized %s" % (x["id"], shares, x["ticker"],
                                           price, usd(real)))
      self.changed()

   ########################################################################
   def show_trades(self):
      """
      name: show_trades
      use: opens (or raises) the trades and profit / loss window
      """
      if (self.trades_win is not None and self.trades_win.alive()):
         self.trades_win.top.lift()
         return
      self.trades_win = TradesWindow(self)

   ########################################################################
   def changed(self):
      """
      name: changed
      use: after a trade was recorded, corrected or removed: updates the
           trades window and redraws the chart
      """
      if (self.trades_win is not None and self.trades_win.alive()):
         self.trades_win.fill()
      self.refresh()

   ########################################################################
   def scale_tag(self, axes, edge, side):
      """
      name: scale_tag
      use: hidden value tag just outside the left (edge 0) or right (edge
           1) side of axes, at a data y set later by on_hover
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
   def draw_volume(self, host, x, view):
      """
      name: draw_volume
      use: overlays volume as a line on a second y axis, scale on the
           right, kept to the bottom third of the panel; each segment is
           green when its bar had more buying than selling, red when it
           had more selling, gray when even (see buy_share)
      post: twin axes registered in self.hosts so hovering over it acts
            as hovering over host
      """
      volume = view["Volume"].fillna(0).to_numpy(dtype = float)
      if (not volume.any()):
         return
      twin = host.twinx()
      twin.set_ylabel("volume", color = "gray")
      twin.tick_params(axis = "y", labelsize = 8, colors = "gray")
      twin.yaxis.set_major_formatter(FuncFormatter(volume_label))
      twin.yaxis.set_major_locator(grid_locator())
      twin.grid(False)
                                # one segment per bar, from the bar
                                # before it, in that bar's colour
      share  = buy_share(view)
      colors = np.where(share > 0.5, VOL_BUY,
                        np.where(share < 0.5, VOL_SELL, VOL_EVEN))
      points = np.column_stack([x, volume])
      lines  = LineCollection(np.stack([points[:-1], points[1:]], axis = 1),
                              colors = colors[1:], linewidths = 1.2,
                              alpha = 0.7)
      twin.add_collection(lines)
      twin.set_xlim(host.get_xlim())
      twin.set_ylim(0, volume.max() * 3)
                                # twinx puts volume in front; swap so the
                                # prices stay on top, and clear host's
                                # background so the volume shows
      host.set_zorder(twin.get_zorder() + 1)
      host.patch.set_visible(False)

      def coordinates(px, py):
         px, py = host.transData.inverted().transform(
                     twin.transData.transform((px, py)))
         return(host.format_coord(px, py))

      twin.format_coord = coordinates
      self.hosts[twin]  = host

   ########################################################################
   def quote(self, color, symbol, *cells):
      """
      name: quote
      use: adds one ticker's row to the quote table above the chart
      usage: quote(color, symbol, price, change, "(", percent, ")",
                   "Low", low, "High", high, "Expected", range, signal
                   [, "Own", shares @ price, gain])
             quote(color, symbol, reason)
             any cell may be a (text, colour) pair to override color
      post: each piece in its own column, aligned per QUOTE_COLUMNS
      """
      row  = self.quote_rows
      font = ("TkDefaultFont", 11, "bold")
      self.quote_rows += 1
      if (len(cells) == 1):
         layout = [("w", (4, 4)), ("w", (6, 0))]
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
      name: draw_empty
      use: placeholder for a ticker with nothing to chart yet
      """
      self.quote("gray", symbol, reason)
      axes.set_yticks([])
      axes.text(0.5, 0.5, f"{symbol}: {reason}",
                transform = axes.transAxes,
                horizontalalignment = "center",
                verticalalignment   = "center",
                color = "gray", fontsize = 12)

   ########################################################################
   def draw_candles(self, axes, view, x):
      """
      name: draw_candles
      use: ohlc candlesticks: thin wick from low to high, body from open
           to close, UP colour when close >= open else DOWN
      """
      opens  = view["Open"].to_numpy(dtype = float)
      highs  = view["High"].to_numpy(dtype = float)
      lows   = view["Low"].to_numpy(dtype = float)
      closes = view["Close"].to_numpy(dtype = float)
      colors = np.where(closes >= opens, UP, DOWN)
                                # a doji (open == close) still needs a
                                # visible sliver of body
      sliver = max(highs.max() - lows.min(), 0.01) * 0.002
      axes.vlines(x, lows, highs, colors = colors, linewidth = 1)
      axes.bar(x, np.maximum(np.abs(closes - opens), sliver),
               bottom    = np.minimum(opens, closes),
               width     = 0.6,
               color     = colors,
               edgecolor = colors,
               linewidth = 0.5)

   ########################################################################
   def on_hover(self, event):
      """
      name: on_hover
      use: moves the crosshair to the mouse and fills the readout line
           with the bar under it, its VWAP, expected range and signal
      imp: the volume axes sits on top of the price axes, so matplotlib
           reports the volume axes; map back to the price axes. distances
           are in screen pixels so NEAR means the same at any zoom
      """
      host = self.hosts.get(event.inaxes)
      if (host is None or host not in self.crosshair):
         self.on_leave(event)
         return
      x, y = host.transData.inverted().transform((event.x, event.y))
      at_volume = None
      for twin, owner in self.hosts.items():
         if (owner is host and twin is not host):
            at_volume = twin.transData.inverted().transform(
                           (event.x, event.y))[1]
      best = None
      for axes, symbol, view in self.panels:
         if (axes is not host):
            continue
         spot = int(round(x))
         if (not 0 <= spot < len(self.timeline)):
            continue
         row = int(view.index.get_indexer([self.timeline[spot]])[0])
         if (row < 0):
            continue
         value = min(max(y, float(view["Low"].iloc[row])),
                     float(view["High"].iloc[row]))
         px, py = host.transData.transform((spot, value))
         gap    = np.hypot(px - event.x, py - event.y)
         if (gap <= NEAR):
            best = (symbol, view, row, spot)
      if (best is None):
         self.on_leave(event)
         return
      symbol, view, row, spot = best
      b = view.iloc[row]
      bs = buy_share(view.iloc[max(row - 1, 0):row + 1])[-1]
      k = int(buy_signals(view.iloc[:row + 1], self.tr.cfg)[-1])
      self.hover_var.set(
         f"{symbol:<6} {view.index[row].strftime(TIME_FMT)}  "
         f"O {b['Open']:,.2f}  C {b['Close']:,.2f}  "
         f"L {b['Low']:,.2f}  H {b['High']:,.2f}  "
         f"V {int(b['Volume']):,} ({100 * bs:.0f}% buy)  "
         f"VWAP {b['vwap']:,.2f}  "
         f"exp {f2(b['projlo'])}-{f2(b['projhi'])}"
         + (f"  BUY {KINDS[k]}" if (k) else ""))
      for axes, (upright, level, price, size) in self.crosshair.items():
         here = (axes is host)
         upright.set_visible(here)
         level.set_visible(here)
         price.set_visible(here)
         size.set_visible(here and at_volume is not None)
         if (not here):
            continue
         upright.set_xdata([spot, spot])
         level.set_ydata([y, y])
         price.set_y(y)
         price.set_text(f"{y:,.2f}")
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
      name: schedule
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
class TradeDialog(object):
   """
   name: TradeDialog
   use: a small window to enter or correct one trade: ticker, shares,
        price and time
   usage: TradeDialog(parent, title, side, choices, ticker, shares, price,
                      when, prices, apply[, held][, fixed])
          choices are the tickers offered; price None takes the
          ticker's current price from prices; apply(ticker, shares,
          price, when) records it and raises ExceptionClass to refuse
          it; held(ticker), when given, gives the shares held, shown as
          a hint; fixed True keeps the ticker from being changed
   post: the dialog stays open, showing the reason, until apply accepts
         the values or Cancel is pressed
   imp: modal: the main window waits until it is closed
   """

   ########################################################################
   def __init__(self, parent, title, side, choices, ticker, shares, price,
                when, prices, apply, held = None, fixed = False):
      self.prices = prices
      self.apply  = apply
      self.held   = held
      self.shown  = ticker
      self.top    = top = tkinter.Toplevel(parent)
      top.title(title)
      top.transient(parent)
      top.resizable(False, False)
      frame = ttk.Frame(top, padding = 12)
      frame.pack(fill = tkinter.BOTH, expand = True)

      self.ticker_var = tkinter.StringVar(value = ticker)
      self.shares_var = tkinter.StringVar(value = "%g" % shares)
      self.price_var  = tkinter.StringVar(
                           value = "" if (price is None and
                                          prices.get(ticker) is None)
                           else "%.2f" % (price if (price is not None)
                                          else prices[ticker]))
      self.when_var   = tkinter.StringVar(value = when)
      self.hint_var   = tkinter.StringVar(value = "")
      self.error_var  = tkinter.StringVar(value = "")

      ttk.Label(frame, text = "Ticker:").grid(row = 0, column = 0,
                                              sticky = "w", pady = 3)
      box = ttk.Combobox(frame, textvariable = self.ticker_var,
                         values = choices, width = 12,
                         state = "disabled" if (fixed) else
                         "readonly" if (side == "sell") else "normal")
      box.grid(row = 0, column = 1, sticky = "w", pady = 3)
      box.bind("<<ComboboxSelected>>", self.ticker_changed)
      box.bind("<FocusOut>", self.ticker_changed)
      ttk.Label(frame, textvariable = self.hint_var,
                foreground = "gray").grid(row = 0, column = 2,
                                          sticky = "w", padx = (8, 0))
      rows = (("Shares:", self.shares_var, ""),
              ("Price:", self.price_var, "per share"),
              ("Time:", self.when_var, "YYYY-MM-DD HH:MM, New York"))
      for n, (text, var, note) in enumerate(rows, 1):
         ttk.Label(frame, text = text).grid(row = n, column = 0,
                                            sticky = "w", pady = 3)
         entry = ttk.Entry(frame, textvariable = var, width = 18)
         entry.grid(row = n, column = 1, sticky = "w", pady = 3)
         ttk.Label(frame, text = note, foreground = "gray").grid(
            row = n, column = 2, sticky = "w", padx = (8, 0))
         if (n == 1):
            first = entry
      ttk.Label(frame, textvariable = self.error_var, foreground = "red",
                wraplength = 380).grid(row = 4, column = 0,
                                       columnspan = 3, sticky = "w",
                                       pady = (6, 0))
      buttons = ttk.Frame(frame)
      buttons.grid(row = 5, column = 0, columnspan = 3, sticky = "e",
                   pady = (8, 0))
      ttk.Button(buttons, text = "OK",
                 command = self.ok).pack(side = tkinter.LEFT, padx = 4)
      ttk.Button(buttons, text = "Cancel",
                 command = top.destroy).pack(side = tkinter.LEFT)
      top.bind("<Return>", lambda event: self.ok())
      top.bind("<Escape>", lambda event: top.destroy())
      self.show_hint()
      first.focus_set()
      first.select_range(0, "end")
      top.grab_set()
      top.wait_window()

   ########################################################################
   def show_hint(self):
      """
      name: show_hint
      use: the shares held of the chosen ticker, for a sale
      """
      if (self.held is not None):
         self.hint_var.set("%g held" % self.held(
            self.ticker_var.get().strip().upper()))

   ########################################################################
   def ticker_changed(self, event):
      """
      name: ticker_changed
      use: a new ticker brings its current price into the price field
      """
      t = self.ticker_var.get().strip().upper()
      if (t != self.shown):
         self.shown = t
         px = self.prices.get(t)
         if (px is not None):
            self.price_var.set("%.2f" % px)
      self.show_hint()

   ########################################################################
   def ok(self):
      """
      name: ok
      use: checks the fields and hands them to apply
      """
      t = self.ticker_var.get().strip().upper()
      checks = (("Enter a ticker.", lambda: t or None),
                ("Shares must be a number.",
                 lambda: float(self.shares_var.get())),
                ("Price must be a number.",
                 lambda: float(self.price_var.get())),
                ("The time must be YYYY-MM-DD HH:MM.",
                 lambda: parse_time(self.when_var.get())))
      got = []
      for text, value in checks:
         try:
            got.append(value())
         except Exception:
            got.append(None)
         if (got[-1] is None):
            self.error_var.set(text)
            return
      try:
         self.apply(*got)
      except ExceptionClass as error:
         self.error_var.set("Not recorded: %s" % error)
         return
      self.top.destroy()

###########################################################################
class TradesWindow(object):
   """
   name: TradesWindow
   use: window with the profit and loss per stock and every trade, where
        a trade can be corrected or removed
   imp: sales are matched to the earliest shares bought (first in, first
        out); unrealized gains use the latest prices of the chart window
   """

   PNL_COLUMNS = (("Ticker", 70, "w"), ("Shares", 70, "e"),
                  ("Avg cost", 80, "e"), ("Price", 80, "e"),
                  ("Value", 100, "e"), ("Unrealized", 100, "e"),
                  ("%", 70, "e"), ("Realized", 100, "e"),
                  ("Total", 100, "e"))
   TRADE_COLUMNS = (("#", 50, "e"), ("Time", 130, "w"),
                    ("Ticker", 70, "w"), ("Side", 50, "w"),
                    ("Shares", 70, "e"), ("Price", 80, "e"),
                    ("Amount", 100, "e"), ("Realized", 100, "e"))

   ########################################################################
   def __init__(self, owner):
      self.owner = owner
      self.tr    = owner.tr
      self.top   = top = tkinter.Toplevel(owner.root)
      top.title("Trades and profit / loss")
      top.geometry("820x560")
      frame = ttk.Frame(top, padding = 8)
      frame.pack(fill = tkinter.BOTH, expand = True)

      self.title_var = tkinter.StringVar(value = "")
      ttk.Label(frame, textvariable = self.title_var,
                font = ("TkDefaultFont", 11, "bold")).pack(anchor = "w")
      self.pnl = self.table(frame, self.PNL_COLUMNS, 6)
      self.pnl.tag_configure("total", font = ("TkDefaultFont", 10, "bold"))

      ttk.Label(frame, text = "Trades (double-click one to correct it)",
                font = ("TkDefaultFont", 11, "bold")).pack(
                   anchor = "w", pady = (10, 0))
      self.list = self.table(frame, self.TRADE_COLUMNS, 12, True)
      self.list.bind("<Double-1>", lambda event: self.edit())
      for tree in (self.pnl, self.list):
         tree.tag_configure("gain", foreground = BRK_CLR)
         tree.tag_configure("loss", foreground = SELL_CLR)

      buttons = ttk.Frame(frame)
      buttons.pack(fill = tkinter.X, pady = (8, 0))
      ttk.Button(buttons, text = "Record buy...",
                 command = owner.record_buy).pack(side = tkinter.LEFT)
      ttk.Button(buttons, text = "Record sell...",
                 command = owner.record_sell).pack(side = tkinter.LEFT,
                                                   padx = 4)
      ttk.Button(buttons, text = "Correct trade...",
                 command = self.edit).pack(side = tkinter.LEFT, padx = 4)
      ttk.Button(buttons, text = "Delete trade",
                 command = self.delete).pack(side = tkinter.LEFT)
      ttk.Button(buttons, text = "Close",
                 command = top.destroy).pack(side = tkinter.RIGHT)
      self.fill()

   ########################################################################
   def table(self, parent, columns, height, expand = False):
      """
      name: table
      use: a Treeview with the given (heading, width, anchor) columns
      post: returns the Treeview, packed with a vertical scroll bar
      """
      box  = ttk.Frame(parent)
      box.pack(fill = tkinter.BOTH, expand = expand, pady = (4, 0))
      tree = ttk.Treeview(box, columns = [c[0] for c in columns],
                          show = "headings", height = height,
                          selectmode = "browse")
      for name, width, anchor in columns:
         tree.heading(name, text = name, anchor = anchor)
         tree.column(name, width = width, anchor = anchor,
                     stretch = (name in ("Time", "Ticker")))
      bar = ttk.Scrollbar(box, orient = "vertical", command = tree.yview)
      tree.configure(yscrollcommand = bar.set)
      tree.pack(side = tkinter.LEFT, fill = tkinter.BOTH, expand = True)
      bar.pack(side = tkinter.RIGHT, fill = tkinter.Y)
      return(tree)

   ########################################################################
   def alive(self):
      """
      name: alive
      use: predicate; is the window still open
      """
      try:
         return(bool(self.top.winfo_exists()))
      except tkinter.TclError:
         return(False)

   ########################################################################
   def fill(self):
      """
      name: fill
      use: (re)fills both tables from the ledger and the latest prices
      """
      tr = self.tr
      lots, closed, problems = tr.books(upto = False)
      rows, tot = profit(lots, closed, self.owner.prices)
      self.title_var.set("Profit and loss as of %s (sales matched to the "
                         "earliest buys)" % tr.stamp().replace("T", " "))

      def money(v):
         return("n/a" if (v is None) else usd(v))

      def tag(v):
         return(() if (not v) else ("gain",) if (v > 0) else ("loss",))

      self.pnl.delete(*self.pnl.get_children())
      for r in rows:
         sh = r["shares"]
         self.pnl.insert("", "end", tags = tag(r["total"]), values = (
            r["ticker"], "%g" % sh, f2(r["cost"]) if (sh) else "",
            f2(r["price"]) if (sh) else "",
            money(r["value"]) if (sh) else "",
            money(r["unreal"]) if (sh) else "",
            pct(r["unreal_pct"]) if (r["unreal_pct"] is not None) else "",
            money(r["real"]), money(r["total"])))
      if (rows):
         known = tot["known"]
         self.pnl.insert("", "end", tags = ("total",) + tag(tot["total"]),
                         values = (
            "TOTAL", "", "", "", money(tot["value"]) if (known) else "n/a",
            money(tot["unreal"]) if (known) else "n/a",
            pct(tot["unreal"] / tot["paid"]) if (known and tot["paid"])
            else "", money(tot["real"]),
            money(tot["total"]) if (known) else "n/a"))

      self.list.delete(*self.list.get_children())
      for x in sorted(tr.trades + tr.pretend, key = trade_order,
                      reverse = True):
         real = None
         if (x["side"] == "sell"):
            real = sum(c["pnl"] for c in closed if (c["sell"] == x["id"]))
         self.list.insert("", "end", iid = str(x["id"]), tags = tag(real),
                          values = (
            x["id"], x["time"].replace("T", " "), x["ticker"], x["side"],
            "%g" % x["shares"], "%.2f" % x["price"],
            format(x["shares"] * x["price"], ",.2f"),
            "" if (real is None) else usd(real)))
      if (problems):
         messagebox.showwarning("Trades", "\n".join(problems),
                                parent = self.top)

   ########################################################################
   def selected(self):
      """
      name: selected
      use: the trade selected in the list, or None (after saying so)
      """
      sel = self.list.selection()
      if (not sel):
         messagebox.showinfo("Trades", "Select a trade first.",
                             parent = self.top)
         return(None)
      return(self.tr.trade(int(sel[0])))

   ########################################################################
   def edit(self):
      """
      name: edit
      use: corrects the shares, price or time of the selected trade
      """
      x = self.selected()
      if (x is None):
         return

      def apply(t, shares, price, when):
         self.tr.edit(x["id"], shares, price, when)
         self.owner.status_var.set("corrected trade #%d" % x["id"])
         self.owner.changed()

      TradeDialog(self.top, "Correct trade #%d (%s)" % (x["id"],
                                                      x["side"]),
                  x["side"], [x["ticker"]], x["ticker"], x["shares"],
                  x["price"], x["time"].replace("T", " "),
                  self.owner.prices, apply, fixed = True)

   ########################################################################
   def delete(self):
      """
      name: delete
      use: removes the selected trade, after asking
      """
      x = self.selected()
      if (x is None):
         return
      if (not messagebox.askyesno(
                 "Delete trade", "Delete trade #%d: %s %g %s @ %.2f at "
                 "%s?" % (x["id"], x["side"], x["shares"], x["ticker"],
                          x["price"], x["time"].replace("T", " ")),
                 parent = self.top)):
         return
      try:
         self.tr.delete(x["id"])
      except ExceptionClass as error:
         messagebox.showerror("Delete trade", "Not deleted: %s" %
                              error, parent = self.top)
         return
      self.owner.status_var.set("deleted trade #%d" % x["id"])
      self.owner.changed()

###########################################################################
def detach(prog, argv):
   """
   name: detach
   use: starts this script again as a background process with no console
        and returns once its window is open, so the prompt comes back
   post: E_SUCCESS once the background copy reports READY on its stdout;
         E_FAIL if it exits first, after echoing its log to stderr
   imp: as in stock_ticker.py. windows: runs pythonw.exe (no console
        window) as a detached process in its own process group, so
        closing the cmd window or ctrl-c there does not take the window
        down. linux: a new session does the same. the copy's stderr goes
        to LOG for later problems
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
      child = subprocess.Popen([python, os.path.abspath(sys.argv[0])] +
                               argv,
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
   print(f"{prog}: window failed to start (exit {child.returncode}); "
         f"see {LOG}", file = sys.stderr)
   return(E_FAIL)

###########################################################################
###
### s t a r t    o f    e x e c u t i o n
###
###########################################################################

class MAIN(object):
   """
   name: MAIN
   use: parses the command line, then opens the chart window (in the
        background unless -f) or, with -t, the interactive text prompt
   imp: the options follow stock_ticker.py
   """

   def __init__(self, exec_name = ""):
      self.exec_name = exec_name

   ########################################################################
   def execute(self, argv):
      """
      name: execute
      use: runs the program
      post: returns the exit code
      """
      ap = argparse.ArgumentParser(prog        = SELF,
                                   description = "intraday stock tracker "
                                                 "with BUY / SELL signals")
      ap.add_argument("-c", dest = "candles", action = "store_true",
                      help = "start in candlestick mode")
      ap.add_argument("-f", dest = "foreground", action = "store_true",
                      help = "stay in the foreground (keeps the prompt)")
      ap.add_argument("-r", dest = "refresh", type = int, default = 60,
                      help = "auto-refresh seconds, 0 = off (default 60)")
      ap.add_argument("-p", dest = "span", default = "1d",
                      choices = list(SPANS),
                      help = "time span shown (default 1d)")
      ap.add_argument("-t", dest = "text", action = "store_true",
                      help = "interactive text prompt instead of the "
                             "window (simulate, follow, asof ...)")
      ap.add_argument("-s", dest = "synthetic", action = "store_true",
                      help = "offline fake data for testing the program")
      ap.add_argument("-d", dest = "reload", action = "store_true",
                      help = "re-download all price history at start-up")
      ap.add_argument("-v", dest = "verbose", action = "count",
                      default = 0, help = "more messages")
      ap.add_argument("tickers", nargs = "*",
                      help = "ticker symbols put first on the "
                             "watchlist, in this order, e.g. AAPL MSFT "
                             "ORCL")
      args = ap.parse_args(argv)
      if (args.refresh < 0):
         ap.error("-r must be 0 or more")

      child = (os.environ.get(CHILD) == "1")
                                # the console copy starts the window in
                                # the background before loading tkinter
                                # and matplotlib, so the prompt comes
                                # back as soon as the window is open
      if (not args.text and not args.foreground and not child):
         return(detach(ap.prog, argv))
      if (not args.text and not gui_modules()):
         return(E_FAIL)
      if (args.synthetic):
         set_home(HOME + "-synthetic")
      tr = Tracker(args.synthetic, args.verbose)
      if (args.tickers):
                                # the stocks named on the command line
                                # come first, in that order, then the
                                # rest of the watchlist
         add = unique(t for a in args.tickers
                      for t in a.upper().replace(",", " ").split())
         tr.save_watch(add + [t for t in tr.watch() if (t not in add)])
      if (args.text):
         if (tr.tickers()):
            tr.sync(force = args.reload)
         else:
            print("The watchlist is empty. Start with 'watch TICKER ...'.")
         Shell(tr).cmdloop()
         return(E_SUCCESS)

      if (not tr.watch()):
         tr.save_watch(["AAPL"])
      try:
         root = tkinter.Tk()
      except tkinter.TclError as exception:
         print(f"{SELF}: cannot open display: {exception}",
               file = sys.stderr)
         return(E_FAIL)
      TrackerWindow(root, tr, args.refresh,
                    "Candles" if (args.candles) else "Line", args.span,
                    args.reload)
      if (child):
                                # window is on screen: tell the waiting
                                # parent, then stop using the pipe (the
                                # parent is gone); later output goes to
                                # LOG
         root.update()
         sys.stdout.write(READY + "\n")
         sys.stdout.flush()
         sys.stdout.close()
         sys.stdout = sys.stderr
      root.mainloop()
      return(E_SUCCESS)

if ( __name__ == "__main__" ):
   try:
      sys.exit(MAIN(sys.argv[0]).execute(sys.argv[1:]))
   except ExceptionClass as exception:
      exception.print_error()
      sys.exit(E_FAIL)
