#!/usr/bin/python
# name: $Id: stock_screener.py 1.main-1 00:37:40 28-Nov-2026 claude-opus-5-5 $
# use: suggests stocks worth putting on stock_tracker's watchlist: large,
#      liquid US stocks in an upward trend, strong against the market,
#      near their highs, with rising volume and a daily range wide
#      enough to trade intraday. It ranks them; it does NOT forecast. It
#      also replays its own rules over the past year and shows how the
#      stocks it would have picked did afterwards, against all the stocks
#      it looked at.
#      Everything is computed on this machine. The only network traffic
#      is the download of PUBLIC daily prices from Yahoo Finance through
#      the yfinance package (ticker symbols only); they are cached for 12
#      hours in the folder .stock_screener next to this script.
#      stock_tracker.py imports this file (from the same folder) for its
#      "Find stocks..." button and 'screen' command.
# usage: stock_screener.py [-n count] [-u file] [-d] [-s] [-v] [TICKER ...]
#        -n count  how many suggestions (default 15)
#        -u file   a text file of tickers to look through instead of the
#                  built-in list of about 280 large US stocks
#        -d        re-download the prices now
#        -s        synthetic (fake) prices, for testing without a network
#        -v        more messages
#        TICKER    tickers to look through instead of the built-in list
#        requires: pip install numpy pandas yfinance
# imp: research tool only; NOT financial advice. the built-in list is
#      today's large companies, so the replay of past picks suffers
#      survivorship bias and looks better than reality.

import argparse
import math
import os
import pickle
import sys
import textwrap
import time
import warnings

import numpy as np
import pandas as pd

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

REVISION   = "$Revision: 1.main-1 $"
SELF       = os.path.splitext(os.path.basename(sys.argv[0]))[0]
SELFPREFIX = os.path.dirname(os.path.abspath(sys.argv[0]))

                                # the cache folder sits next to this
                                # script; OLD_HOME (where versions
                                # before 26-Nov-2026 kept it) is used
                                # only when that folder cannot be written
HOME       = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".stock_screener")
OLD_HOME   = os.path.join(os.path.expanduser("~"), ".stock_screener")
CACHE      = os.path.join(HOME, "daily.pkl")
BENCH      = "SPY"
FIELDS     = ("Open", "High", "Low", "Close", "Volume")
                                # cached prices are re-downloaded after
                                # this many hours
FRESH      = 12

                                # the same large, established US stocks
                                # that stock_picker.py uses (its --universe
                                # large); symbols Yahoo no longer serves
                                # are skipped
UNIVERSE   = sorted(set("""
AAPL MSFT GOOGL AMZN NVDA META AVGO ORCL ADBE CRM CSCO IBM TXN QCOM INTC
AMAT ACN INTU JPM BAC WFC GS MS AXP BLK SPGI V MA BRK-B UNH JNJ LLY MRK
ABBV PFE TMO ABT DHR MDT AMGN GILD BMY PG KO PEP WMT COST CL MDLZ MO PM
MCD SBUX NKE HD LOW TGT XOM CVX COP LIN HON UNP UPS CAT DE GE MMM LMT RTX
VZ T CMCSA TMUS DIS SO DUK NEE
AMD NOW PANW SNPS CDNS KLAC LRCX ADI MU MCHP NXPI FTNT ANET ADSK ROP APH
TEL GLW HPQ HPE DELL NTAP STX WDC KEYS TYL FICO PTC ZBRA TSLA NFLX PYPL
UBER BKNG EA TTWO C SCHW CB PGR MMC AON ICE CME MCO USB PNC TFC COF MET
PRU AIG ALL TRV AFL BK STT FITB HBAN RF KEY MTB NTRS CFG ISRG SYK BSX
VRTX REGN ZTS CI ELV HUM CVS MCK COR CAH BDX EW IQV A IDXX MTD RMD HCA
BIIB DXCM STE ZBH WAT KMB GIS KHC HSY SYY KR ADM STZ KDP MNST EL CHD CLX
TSN HRL TJX ROST ORLY AZO DG DLTR MAR HLT YUM CMG LULU F GM DHI LEN NVR
PHM EBAY BBY ULTA DRI RCL EOG SLB OXY PSX MPC VLO KMI WMB OKE HAL BKR DVN
FANG SHW ECL APD FCX NEM NUE DD DOW PPG VMC MLM NOC GD BA ETN EMR ITW PH
CMI PCAR FDX CSX NSC WM RSG ADP PAYX CTAS FAST ODFL URI JCI TT CARR OTIS
GWW IR XYL LHX CHTR D AEP EXC SRE XEL PEG ED WEC EIX ES DTE ETR AEE CMS
PLD AMT EQIX CCI PSA O SPG WELL DLR AVB EQR VICI
""".split()))

                                # what a stock must have to be suggested
                                # at all (daily bars, data known on the
                                # day only):
                                #   price at least min_price dollars
                                #   median daily dollar volume over 50
                                #   days at least min_dollar_vol
                                #   at least min_history days of prices
                                #   daily ATR(14) between min_atr and
                                #   max_atr of the price: wide enough for
                                #   stock_tracker's 0.8 ATR profit target,
                                #   not wild
                                #   an up trend: close above its 50-day
                                #   average, the 50-day above the 200-day,
                                #   and the 50-day rising over 10 days
GATES = dict(min_price      = 20.0,
             min_dollar_vol = 1e8,
             min_history    = 260,
             min_atr        = 0.012,
             max_atr        = 0.06)

                                # the score is the average of these
                                # percentile ranks among the stocks that
                                # pass the gates (1 = best)
SCORES = ("rel63", "mom", "off", "volr")

                                # replay: picks taken every `step` days
                                # over the last `days` trading days, and
                                # judged after each horizon
TEST = dict(days = 250, horizons = (5, 20))

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
def pct(x):
   """
   name: pct
   use: a fraction as a signed percentage, e.g. +1.25%, or n/a
   """
   if (x is None or not np.isfinite(x)):
      return("n/a")
   return("%+.1f%%" % (x * 100.0))

###########################################################################
def read_tickers(path):
   """
   name: read_tickers
   use: the tickers in a text file (spaces, commas or new lines between
        them; text after a # on a line is ignored)
   """
   out = []
   with open(path) as fh:
      for line in fh:
         out += line.split("#")[0].replace(",", " ").upper().split()
   return(out)

###########################################################################
###
### p r i c e s
###
###########################################################################

def download(tickers, say):
   """
   name: download
   use: two years of daily bars from Yahoo Finance
   post: returns {field: DataFrame} with one column per ticker that had
         data, indexed by date
   """
   try:
      import yfinance as yf
   except ImportError:
      raise(ExceptionClass("missing package; run: pip install yfinance",
                           SELF))
   want = sorted(set(tickers))
   say("Downloading daily prices for %d stocks (about a minute)..." %
       len(want))
   raw = yf.download(want, period = "2y", interval = "1d",
                     group_by = "ticker", auto_adjust = True,
                     progress = False, threads = True)
   if (not isinstance(raw.columns, pd.MultiIndex)):
      raw = pd.concat({want[0]: raw}, axis = 1)
   panel = dict((f, {}) for f in FIELDS)
   for t in want:
      try:
         df = raw[t]
      except KeyError:
         continue
      if (df["Close"].notna().sum() < 2):
         continue
      for f in FIELDS:
         panel[f][t] = df[f]
   out = {}
   for f in FIELDS:
      frame = pd.DataFrame(panel[f])
      frame.index = pd.to_datetime(frame.index).tz_localize(None) \
                      .normalize()
      out[f] = frame.sort_index()
   return(out)

###########################################################################
def synthetic(tickers = None):
   """
   name: synthetic
   use: two years of fake daily bars for testing without a network: 60
        random-walk stocks SYN00 .. SYN59 with slowly changing drifts,
        and SPY
   post: the same {field: DataFrame} as download(); always the same data
   """
   rng   = np.random.default_rng(7)
   n     = 2 * 252
   dates = pd.bdate_range(end = pd.Timestamp.today().normalize(),
                          periods = n)
   mkt   = rng.normal(0.0004, 0.009, n)
   panel = dict((f, {}) for f in FIELDS)
   for t in [BENCH] + ["SYN%02d" % k for k in range(60)]:
      bench = (t == BENCH)
      beta  = 1.0 if (bench) else rng.uniform(0.6, 1.4)
      noise = np.zeros(n) if (bench) else rng.normal(0, 0.012, n)
      mu    = np.zeros(n)
      for i in range(1, n):
         mu[i] = 0.99 * mu[i - 1] + rng.normal(0, 0.0005)
      r  = beta * mkt + noise + (0 if (bench) else mu)
      c  = rng.uniform(30, 300) * np.exp(np.cumsum(r))
      o  = np.roll(c, 1) * (1 + rng.normal(0, 0.004, n))
      o[0] = c[0]
      hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.008, n)))
      lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.008, n)))
      v  = rng.uniform(2e6, 6e6, n) * (1 + 50 * np.abs(r))
      for f, values in zip(FIELDS, (o, hi, lo, c, v)):
         panel[f][t] = values
   out = dict((f, pd.DataFrame(panel[f], index = dates)) for f in FIELDS)
   if (tickers):
      keep = [t for t in out["Close"].columns
              if (t in set(tickers) | {BENCH})]
      out = dict((f, out[f][keep]) for f in FIELDS)
   return(out)

###########################################################################
def save_cache(content, say):
   """
   name: save_cache
   use: writes the price cache into the folder next to this script
   post: if that folder cannot be written to, the cache goes to OLD_HOME
         from then on (with a message); if neither can, it is not kept
   """
   global HOME, CACHE
   for home in (HOME, OLD_HOME):
      cache = os.path.join(home, "daily.pkl")
      try:
         os.makedirs(home, exist_ok = True)
         with open(cache + ".tmp", "wb") as fh:
            pickle.dump(content, fh)
         os.replace(cache + ".tmp", cache)
      except OSError as error:
         say("%s: cannot write %s (%s)" % (SELF, cache,
                                           error.strerror or error))
         continue
      if (home != HOME):
         say("%s: keeping the prices in %s instead" % (SELF, home))
         HOME, CACHE = home, cache
      return
   say("%s: the prices are not kept; they are downloaded every time" %
       SELF)

###########################################################################
def prices(tickers, reload = False, fake = False, say = print):
   """
   name: prices
   use: daily bars of the tickers and SPY, from the cache when it is
        fresh (FRESH hours) and holds them all, else downloaded
   post: returns ({field: DataFrame}, missing tickers)
   """
   if (fake):
      data = synthetic(tickers if (tickers != UNIVERSE) else None)
      cols = set(data["Close"].columns)
      return(data, [t for t in tickers if (t not in cols)
                    and (tickers != UNIVERSE)])
   want = sorted(set(tickers) | {BENCH})
   data = None
                                # the cache next to the script, else the
                                # one in the home folder (used when the
                                # script's folder cannot be written)
   for cache in ([] if (reload) else
                 [CACHE, os.path.join(OLD_HOME, "daily.pkl")]):
      try:
         with open(cache, "rb") as fh:
            c = pickle.load(fh)
         if (time.time() - c["time"] < FRESH * 3600 and
             set(want) <= set(c["asked"])):
            data = c["data"]
            break
      except Exception:
         pass
   if (data is None):
      data = download(want, say)
      save_cache(dict(time = time.time(), asked = want, data = data), say)
   have = set(data["Close"].columns)
   if (BENCH not in have):
      raise(ExceptionClass("could not get prices of the benchmark %s" %
                           BENCH, SELF))
   cols = [t for t in want if (t in have)]
   data = dict((f, data[f][cols]) for f in FIELDS)
   return(data, [t for t in want if (t not in have)])

###########################################################################
###
### s c o r i n g
###
###########################################################################

def features(data):
   """
   name: features
   use: every value the gates and the score use, for every stock on every
        day, from data known on that day only
   post: returns {name: DataFrame} (dates x tickers, SPY left out):
         rel63  3-month return minus SPY's
         ret63  3-month return
         mom    12-month return skipping the latest month
         off    close against the 52-week high (0 = at the high)
         volr   10-day average volume over the 50-day average
         atr    daily ATR(14) as a fraction of the price
         pass   True where every gate is passed
   """
   C, H, L, V = (data["Close"], data["High"], data["Low"], data["Volume"])
   spy = C[BENCH]
   C, H, L, V = (x.drop(columns = [BENCH]) for x in (C, H, L, V))
   f = {}
   f["ret63"] = C / C.shift(63) - 1
   f["rel63"] = f["ret63"].sub(spy / spy.shift(63) - 1, axis = 0)
   f["mom"]   = C.shift(21) / C.shift(252) - 1
   f["off"]   = C / H.rolling(252, min_periods = 200).max() - 1
   f["volr"]  = V.rolling(10).mean() / V.rolling(50).mean()
   pc  = C.shift(1)
   tr  = np.maximum.reduce([(H - L).values, (H - pc).abs().values,
                            (L - pc).abs().values])
   f["atr"]   = pd.DataFrame(tr, index = C.index,
                             columns = C.columns).rolling(14).mean() / C
   sma50  = C.rolling(50).mean()
   sma200 = C.rolling(200).mean()
   dvol   = (C * V).rolling(50).median()
   hist   = C.notna().cumsum()
   g = GATES
   f["pass"] = ((C >= g["min_price"]) &
                (dvol >= g["min_dollar_vol"]) &
                (hist >= g["min_history"]) &
                (f["atr"] >= g["min_atr"]) &
                (f["atr"] <= g["max_atr"]) &
                (C > sma50) &
                (sma50 > sma200) &
                (sma50 > sma50.shift(10)))
   f["close"] = C
   f["spy63"] = spy / spy.shift(63) - 1
   return(f)

###########################################################################
def rank(f, i):
   """
   name: rank
   use: the stocks that pass the gates on day i, best first
   post: returns a DataFrame indexed by ticker with the feature values
         and score (average percentile rank of SCORES); a missing value
         counts as the middle rank
   """
   ok  = f["pass"].iloc[i].fillna(False).astype(bool)
   tks = ok.index[ok.values]
   if (len(tks) == 0):
      return(pd.DataFrame(columns = list(SCORES) + ["score"]))
   row = pd.DataFrame(dict((k, f[k].iloc[i][tks])
                           for k in ("rel63", "ret63", "mom", "off",
                                     "volr", "atr", "close")))
   ranks = row[list(SCORES)].rank(pct = True).fillna(0.5)
   row["score"] = ranks.mean(axis = 1)
   return(row.sort_values("score", ascending = False))

###########################################################################
def replay(f, top):
   """
   name: replay
   use: how the picks would have done: on every h-th trading day of the
        last TEST["days"] days, take the top picks (using data known
        then) and measure their return h days later against the average
        of every stock looked at
   post: returns {h: dict(n, picks, all, excess, beat, t)} for each
         horizon h; picks and all are average returns, excess their
         difference, beat the share of dates the picks did better, t the
         t-statistic of the excess (above ~2 suggests more than luck)
   imp: the dates of one horizon do not overlap, so they are independent
        samples
   """
   C    = f["close"]
   last = len(C) - 1
   out  = {}
   for h in TEST["horizons"]:
      start = max(260, last - TEST["days"])
      exc, pk, al = [], [], []
      for i in range(last - h, start - 1, -h):
         fwd = C.iloc[i + h] / C.iloc[i] - 1
         r   = rank(f, i)
         if (len(r) == 0):
            continue
         mine = fwd[r.index[:top]].dropna()
         every = fwd.dropna()
         if (len(mine) == 0 or len(every) == 0):
            continue
         pk.append(mine.mean())
         al.append(every.mean())
         exc.append(mine.mean() - every.mean())
      n = len(exc)
      if (n == 0):
         out[h] = dict(n = 0)
         continue
      e  = np.array(exc)
      sd = e.std(ddof = 1) if (n > 1) else float("nan")
      out[h] = dict(n = n, picks = float(np.mean(pk)),
                    all = float(np.mean(al)), excess = float(e.mean()),
                    beat = float((e > 0).mean()),
                    t = float(e.mean() / (sd / math.sqrt(n)))
                    if (n > 1 and sd > 0) else float("nan"))
   return(out)

###########################################################################
###
### t h e    s c r e e n
###
###########################################################################

def screen(tickers = None, top = 15, reload = False, fake = False,
           test = True, say = print):
   """
   name: screen
   use: ranks the stocks and replays the rules; this is what
        stock_tracker.py calls
   usage: tickers None looks through UNIVERSE; fake uses synthetic
          prices; say(text) receives progress messages
   post: returns dict(date, looked, passed, picks, test, missing); picks
         is a list of dicts (ticker, price, score, rel63, ret63, spy63,
         mom, off, volr, atr), best first; test is replay()'s result, or
         None when test is False
   """
   tickers = [t.upper() for t in (tickers or UNIVERSE)]
   data, missing = prices(tickers, reload, fake, say)
   say("Scoring %d stocks..." % (len(data["Close"].columns) - 1))
   f = features(data)
   i = len(f["close"]) - 1
   r = rank(f, i)
   picks = []
   for t, row in r.head(top).iterrows():
      picks.append(dict(ticker = t, price = float(row["close"]),
                        score = float(row["score"]),
                        rel63 = float(row["rel63"]),
                        ret63 = float(row["ret63"]),
                        spy63 = float(f["spy63"].iloc[i]),
                        mom = float(row["mom"]), off = float(row["off"]),
                        volr = float(row["volr"]),
                        atr = float(row["atr"])))
   return(dict(date = f["close"].index[i].strftime("%Y-%m-%d"),
               looked = len(f["close"].columns), passed = len(r),
               picks = picks, missing = [t for t in missing
                                         if (t != BENCH)],
               test = replay(f, top) if (test) else None, top = top))

###########################################################################
def why(p):
   """
   name: why
   use: the reasons for one pick, in words
   """
   return("3 months %s vs SPY %s; %s; volume %.1fx usual; daily range "
          "%.1f%%%s" % (pct(p["ret63"]), pct(p["spy63"]),
                        "at its 52-week high" if (p["off"] > -0.0005)
                        else "%s below its 52-week high" %
                        pct(-p["off"]).lstrip("+"),
                        p["volr"], 100 * p["atr"],
                        "" if (not np.isfinite(p["mom"])) else
                        "; 12 months (less the latest) %s" %
                        pct(p["mom"])))

###########################################################################
def test_lines(res):
   """
   name: test_lines
   use: the replay of past picks, in words
   post: returns a list of lines of at most 78 characters
   """
   test = res.get("test")
   if (not test):
      return([])
   out = ["How these rules did over the past year (top %d each time, "
          "before costs):" % res["top"]]
   for h, x in sorted(test.items()):
      if (not x.get("n")):
         out.append("   %2d days later: not enough history" % h)
         continue
      out.append("   %2d days later: picks %s vs all stocks %s, a "
                 "difference of %s;" % (h, pct(x["picks"]),
                                        pct(x["all"]),
                                        pct(x["excess"])))
      out.append("                  better on %.0f%% of %d dates, t=%s "
                 "(above ~2 suggests more" % (100 * x["beat"], x["n"],
                                              "%.1f" % x["t"]
                                              if (np.isfinite(x["t"]))
                                              else "n/a"))
      out.append("                  than luck)")
   return(out)

###########################################################################
def report(res, watched = ()):
   """
   name: report
   use: the screen as text: a ranked table, the reasons, and the replay
   usage: watched lists tickers to mark as already watched
   post: returns a list of lines of at most 78 characters
   """
   out = ["Stocks to consider watching, as of the %s close." %
          res["date"],
          "%d of %d stocks pass the trend, liquidity and range tests." % (
             res["passed"], res["looked"])]
   if (not res["picks"]):
      out.append("No stock passes the tests today.")
   else:
      out += ["The best are ranked by strength (a ranking, not a "
              "forecast).", ""]
      out.append(" #  %-6s %9s %5s %7s %7s %7s %5s %5s  %s" % (
         "Ticker", "Price", "Score", "3m-SPY", "12-1m", "OffHigh", "Vol",
         "ATR", ""))
      for n, p in enumerate(res["picks"], 1):
         out.append("%2d  %-6s %9.2f %5.2f %7s %7s %7s %4.1fx %4.1f%%  %s"
                    % (n, p["ticker"], p["price"], p["score"],
                       pct(p["rel63"]), pct(p["mom"]), pct(p["off"]),
                       p["volr"], 100 * p["atr"],
                       "watching" if (p["ticker"] in watched) else ""))
   if (res["missing"]):
      out.append("")
      out += textwrap.wrap("No prices for: %s" %
                           " ".join(res["missing"]), 78)
   lines = test_lines(res)
   if (lines):
      out += [""] + lines
   out += ["", "Research tool only; NOT financial advice. The list is "
           "today's large companies,", "so the replay is flattered by "
           "survivorship bias."]
   return([line.rstrip() for line in out])

###########################################################################
###
### s t a r t    o f    e x e c u t i o n
###
###########################################################################

class MAIN(object):
   """
   name: MAIN
   use: parses the command line, runs the screen and prints it
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
                                   description = "suggests stocks to "
                                                 "watch in stock_tracker")
      ap.add_argument("-n", dest = "top", type = int, default = 15,
                      help = "how many suggestions (default 15)")
      ap.add_argument("-u", dest = "file",
                      help = "text file of tickers to look through")
      ap.add_argument("-d", dest = "reload", action = "store_true",
                      help = "re-download the prices now")
      ap.add_argument("-s", dest = "fake", action = "store_true",
                      help = "synthetic prices, for testing")
      ap.add_argument("-v", dest = "verbose", action = "count",
                      default = 0, help = "more messages")
      ap.add_argument("tickers", nargs = "*",
                      help = "tickers to look through instead of the "
                             "built-in list")
      args = ap.parse_args(argv)
      if (args.top < 1):
         ap.error("-n must be 1 or more")
      tickers = [t.upper() for a in args.tickers
                 for t in a.replace(",", " ").split()]
      if (args.file):
         try:
            tickers += read_tickers(args.file)
         except OSError as error:
            raise(ExceptionClass("cannot read %s: %s" % (args.file,
                                                         error.strerror),
                                 SELF))
      say = print if (args.verbose or not args.fake) else (lambda s: None)
      res = screen(tickers or None, args.top, args.reload, args.fake,
                   say = say)
      print("")
      print("\n".join(report(res)))
      return(E_SUCCESS if (res["picks"]) else E_WARN)

if ( __name__ == "__main__" ):
   try:
      sys.exit(MAIN(sys.argv[0]).execute(sys.argv[1:]))
   except ExceptionClass as exception:
      exception.print_error()
      sys.exit(E_FAIL)
