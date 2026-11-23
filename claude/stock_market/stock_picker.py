#!/usr/bin/env python3
"""
stock_picker.py -- local, interactive stock recommendation lab.

Everything (features, genetic algorithm, simulation) runs on this machine.
The only network traffic is the download of PUBLIC price history from
Yahoo Finance through the yfinance package (ticker symbols only; your
positions, results and models never leave the machine). Prices are cached
under ~/.stock_picker. Use --synthetic for a fully offline demo.

Setup:  pip install numpy pandas yfinance
Run:    python stock_picker.py
        python stock_picker.py --mode long --effort high
        python stock_picker.py --tickers AAPL,MSFT,KO,JNJ

How it works: a genetic algorithm evolves weights over technical features
(momentum, RSI, moving-average distance, volatility, volume, ...) so that
the weighted score ranks stocks by their following N-day return. An
ensemble of independently evolved scorers votes; a validation slice that
the GA never saw measures real predictive skill, and that skill shrinks
the expected gain and drives the confidence figure. Walk-forward only:
a model used on date X is trained solely on data that was known by X.

Learning over time (files live in ~/.stock_picker): every 'recommend'
on the latest date is journaled and later scored against what really
happened ('scorecard'). 'calibrate' / 'learn' replay history walk-forward
to turn raw scores into honest numbers: expected gain is rescaled by how
much of it really materialized, confidence becomes the observed chance of
a gain, and BUY calls are suppressed when the picks show no edge. Evolved
weights are saved and seed the next run's genetic search (warm start), and
a ridge-regression member joins the ensemble when it shows validation
skill. Calibration for a past date only uses outcomes known by then.

Sell rule: a position is held for as long as the model still projects a
rise. Both exits are measured from the highest close since the buy, so
they only ever move up:
  - trailing stop: SELL when the price falls stop_atr x ATR below that
    high, whatever the projection ('set stop_atr 0' turns it off);
  - expected drop: SELL when the model projects a decline AND the price
    is already dip_atr x ATR below that high, so one bad day alone does
    not trigger a sale ('set dip_atr 0' sells on the projection alone).
There is no maximum holding period unless one is set with 'set max_hold
N'.

NOT financial advice. Daily bars only, so "short" mode is a swing
strategy rather than true intraday trading. The built-in universe is
today's blue chips, so backtests suffer survivorship bias and look better
than reality. Treat all output as an experiment, not a promise.
"""
import argparse
import cmd
import json
import math
import os
import pickle
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

HOME = os.path.join(os.path.expanduser("~"), ".stock_picker")
CACHE = os.path.join(HOME, "prices.pkl")
LEDGER = os.path.join(HOME, "ledger.json")
STATE = os.path.join(HOME, "state.json")
PREDS = os.path.join(HOME, "predictions.json")
WATCH = os.path.join(HOME, "watchlist.json")
MIN_PAIRS = 150                # past predictions needed to calibrate
BENCH = "SPY"

SMALL_UNIVERSE = """
AAPL MSFT GOOGL AMZN NVDA META AVGO ORCL ADBE CRM CSCO IBM TXN QCOM
INTC AMAT ACN INTU
JPM BAC WFC GS MS AXP BLK SPGI V MA BRK-B
UNH JNJ LLY MRK ABBV PFE TMO ABT DHR MDT AMGN GILD BMY
PG KO PEP WMT COST CL MDLZ MO PM MCD SBUX NKE HD LOW TGT
XOM CVX COP LIN
HON UNP UPS CAT DE GE MMM LMT RTX
VZ T CMCSA TMUS DIS SO DUK NEE
""".split()

# About 300 large, established US stocks (--universe large, the
# default). Symbols that Yahoo no longer serves are skipped with a
# message at download time.
LARGE_UNIVERSE = sorted(set(SMALL_UNIVERSE + """
AMD NOW PANW SNPS CDNS KLAC LRCX ADI MU MCHP NXPI FTNT ANET ADSK ROP
APH TEL GLW HPQ HPE DELL NTAP STX WDC KEYS TYL FICO PTC ZBRA TSLA NFLX
PYPL UBER BKNG EA TTWO
C SCHW CB PGR MMC AON ICE CME MCO USB PNC TFC COF MET PRU AIG ALL TRV
AFL BK STT FITB HBAN RF KEY MTB NTRS CFG
ISRG SYK BSX VRTX REGN ZTS CI ELV HUM CVS MCK COR CAH BDX EW IQV A IDXX
MTD RMD HCA BIIB DXCM STE ZBH WAT
KMB GIS KHC HSY SYY KR ADM STZ KDP MNST EL CHD CLX TSN HRL
TJX ROST ORLY AZO DG DLTR MAR HLT YUM CMG LULU F GM DHI LEN NVR PHM EBAY
BBY ULTA DRI RCL
EOG SLB OXY PSX MPC VLO KMI WMB OKE HAL BKR DVN FANG
SHW ECL APD FCX NEM NUE DD DOW PPG VMC MLM
NOC GD BA ETN EMR ITW PH CMI PCAR FDX CSX NSC WM RSG ADP PAYX CTAS FAST
ODFL URI JCI TT CARR OTIS GWW IR XYL LHX
CHTR
D AEP EXC SRE XEL PEG ED WEC EIX ES DTE ETR AEE CMS
PLD AMT EQIX CCI PSA O SPG WELL DLR AVB EQR VICI
""".split()))

# Quality gate applied to every stock on every date (trailing data only):
# no low-priced stocks, median 60-day dollar volume must be liquid,
# 252-day annualized volatility capped, no >50% drop from the 252-day
# high, and at least ~15 months of history.
FILTERS = dict(min_price=20.0, min_dollar_vol=1e8, max_vol=0.55,
               max_dd=-0.50, min_history=300)

# horizon is how far ahead the BUY projection looks; max_hold=0 means no
# holding limit. stop_atr is the trailing stop and dip_atr the smaller
# pullback (both in ATRs below the highest close since the buy) that
# must also be present before a projected drop triggers a SELL.
MODES = dict(
   short=dict(horizon=2, train_days=750, max_hold=0, stop_atr=1.5,
              dip_atr=0.75,
              target_mult=2.5, min_gain=0.004, min_conf=35.0,
              min_lift=0.03, max_pos=3, stride=5, rebalance=1, topk=5,
              cal_days=250, cal_step=2, cal_stride=10),
   long=dict(horizon=63, train_days=1250, max_hold=0, stop_atr=3.0,
             dip_atr=1.5,
             target_mult=1.5, min_gain=0.03, min_conf=35.0,
             min_lift=0.03, max_pos=8, stride=21, rebalance=5, topk=5,
             cal_days=500, cal_step=10, cal_stride=21))

# (population, generations, ensemble size)
EFFORT = dict(low=(24, 12, 3), normal=(40, 20, 4), high=(64, 40, 6))

FEATS = ["ret1", "ret3", "ret5", "ret10", "ret21", "ret63", "rsi14",
         "dist_sma20", "dist_sma50", "dist_sma200", "vol20", "atr14",
         "volratio", "bbpos", "macd", "gap", "stoch", "rel21", "rel63",
         "hi252", "mom12_1"]

HELP = dict(
   mode="mode short|long       switch between days-to-weeks and "
        "multi-month",
   horizon="horizon N            override holding horizon (trading days)",
   asof="asof DATE|-N|+N|now  pretend today is DATE (YYYY-MM-DD), N "
        "trading days back/forward, or now",
   nextday="nextday              advance the as-of date to the next "
           "business day",
   recommend="recommend [N]        top N picks with gain, confidence, "
             "sells for held positions\n                          "
             "('recommendation(s)' work too)",
   track="track [N]            record the BUY picks as open positions",
   buy="buy TICKER [price]   record a position you opened",
   sell="sell TICKER          close a recorded position",
   positions="positions            positions held on the as-of date "
             "(from your real ledger by\n                          "
             "buy/sell date) and SELL/HOLD; 'position' works too",
   realpositions="realpositions        the real ledger's open positions "
                 "and SELL/HOLD on the\n                          latest "
                 "date (ignores asof)",
   savepositions="savepositions        write practice buys/sells made "
                 "while an as-of date was\n                          "
                 "active into the real ledger",
   compare="compare [N]          judge picks at the as-of date against "
           "what really happened",
   follow="follow TICKER [steps] [step_days]   walk one stock forward "
          "period by period",
   simulate="simulate [days] [capital]   trade the model over the last "
            "N days (default 90)",
   watch="watch TICKER ...    add newly listed (IPO) stocks to the "
         "watchlist; they are\n                          tracked but "
         "never recommended until they pass the quality gate",
   unwatch="unwatch TICKER ...  remove stocks from the watchlist",
   watchlist="watchlist            price, history and gate status of "
             "watched stocks",
   universe="universe             show which stocks pass the quality gate",
   refresh="refresh              re-download price history",
   calibrate="calibrate [days]      replay history to measure how reliable "
             "past picks were",
   scorecard="scorecard            real results of past recommendations "
             "(live + backtest)",
   learn="learn [days|all]     score outcomes, calibrate, retrain, save",
   set="set NAME VALUE       min_gain, min_lift (percent), min_conf, "
       "max_pos, stop_atr,\n                          dip_atr, "
       "target_mult, max_hold  (current mode;\n"
       "                          stop_atr 0 = no trailing stop, dip_atr 0 "
       "= sell on a\n                          projected drop alone, "
       "max_hold 0 = no holding limit)",
   effort="effort low|normal|high   GA search effort (speed vs. depth)",
   status="status",
   quit="quit")


def pct(x):
   return "%+.2f%%" % (x * 100.0)


# Signed dollar amount, e.g. +$1,234.50 or -$0.37.
def usd(v):
   return "%s$%s" % ("+" if v >= 0 else "-", format(abs(v), ",.2f"))


# Percent plus the dollar gain on one share bought at price px.
# sep is "/" for running text and " " for table columns.
def pctd(x, px, sep="/"):
   return "%s%s%s" % (pct(x), sep, usd(x * px))


def phi(x):
   return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Stop-loss price for a buy at px, or 0.0 when the stop is turned off.
def stop_px(px, c, atr):
   if c["stop_atr"] <= 0:
      return 0.0
   return px * (1 - c["stop_atr"] * atr)


# Holding-limit text for status lines.
def hold_text(c):
   if c["max_hold"] > 0:
      return "max %d days" % c["max_hold"]
   return "no holding limit"


# The IPO / new-listing watchlist: [{"ticker": "XYZ", "added": date}].
def load_watch():
   try:
      with open(WATCH) as fh:
         return [w for w in json.load(fh) if w.get("ticker")]
   except Exception:
      return []


def save_watch(w):
   dump_json(WATCH, w)


# ---------------------------------------------------------------- data
# Write the price cache (frames and the list of symbols asked for).
def save_cache(frames, requested, years, stamp=None):
   os.makedirs(HOME, exist_ok=True)
   with open(CACHE, "wb") as fh:
      pickle.dump(dict(time=stamp or time.time(), requested=sorted(
         set(requested)), years=years, frames=frames), fh)


# Download (or load from cache) daily OHLCV for the tickers + benchmark.
# ipo lists watched symbols: they are kept however short their history.
# A fresh cache that lacks some symbols only downloads the missing ones.
def load_frames(tickers, years, force=False, synthetic=False, ipo=()):
   if synthetic:
      return synth_frames(set(tickers) | set(ipo))
   want = sorted(set(tickers) | set([BENCH]) | set(ipo))
   if os.path.exists(CACHE) and not force:
      try:
         with open(CACHE, "rb") as fh:
            c = pickle.load(fh)
         fresh = time.time() - c["time"] < 20 * 3600
         deep = c.get("years", 0) >= years
         if fresh and deep:
            # new symbols, plus watched ones that had no data last time
            # (an IPO that was not trading yet)
            miss = sorted((set(want) - set(c["requested"])) |
                          (set(ipo) - set(c["frames"])))
            if miss:
               c["frames"].update(download(miss, years, ipo, False))
               save_cache(c["frames"], set(c["requested"]) | set(want),
                          years, c["time"])
            else:
               print("Using cached prices (use 'refresh' to "
                     "re-download).")
            return dict((t, c["frames"][t]) for t in want
                        if t in c["frames"])
      except Exception:
         pass
   frames = download(want, years, ipo)
   save_cache(frames, want, years)
   return frames


def download(want, years, ipo=(), need_bench=True):
   try:
      import yfinance as yf
   except ImportError:
      sys.exit("Missing package. Run: pip install yfinance")
   print("Downloading %d symbols of public price history..." % len(want))
   raw = yf.download(want, period="%dy" % years, auto_adjust=True,
                     group_by="ticker", progress=False, threads=True)
   if len(want) == 1 and not isinstance(raw.columns, pd.MultiIndex):
      raw = pd.concat({want[0]: raw}, axis=1)
   frames = {}
   for t in want:
      try:
         df = raw[t][["Open", "High", "Low", "Close", "Volume"]].copy()
      except KeyError:
         continue
      df = df.dropna(subset=["Close"])
      for col in ("Open", "High", "Low"):
         df[col] = df[col].fillna(df["Close"])
      df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
      if len(df) > 400 or (t in ipo and len(df) >= 2):
         frames[t] = df
   bad = [t for t in want if t not in frames]
   if bad:
      print("No usable data for: %s" % " ".join(bad))
   if need_bench and BENCH not in frames:
      sys.exit("Could not download benchmark %s." % BENCH)
   return frames


# Random-walk stocks with slow drift regimes, for offline testing only.
def synth_frames(want=None):
   rng = np.random.default_rng(42)
   n = 8 * 252
   dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
   mkt = rng.normal(0.0004, 0.009, n)
   frames = {}
   for t in [BENCH] + ["SYN%02d" % i for i in range(45)]:
      bench = t == BENCH
      beta = 1.0 if bench else rng.uniform(0.6, 1.3)
      noise = np.zeros(n) if bench else rng.normal(0, 0.008, n)
      mu = np.zeros(n)
      for i in range(1, n):
         mu[i] = 0.995 * mu[i - 1] + rng.normal(0, 0.0004)
      r = beta * mkt + noise + (0 if bench else mu)
      c = 100 * np.exp(np.cumsum(r))
      o = np.roll(c, 1) * (1 + rng.normal(0, 0.003, n))
      o[0] = c[0]
      hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.004, n)))
      lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.004, n)))
      v = rng.uniform(4e6, 9e6, n)
      frames[t] = pd.DataFrame(dict(Open=o, High=hi, Low=lo, Close=c,
                                    Volume=v), index=dates)
   # SYNIPO: a stock that began trading 40 days ago (for testing watch)
   m = 40
   c = 30 * np.exp(np.cumsum(rng.normal(0.004, 0.03, m)))
   o = np.roll(c, 1) * (1 + rng.normal(0, 0.01, m))
   o[0] = c[0] * 1.1
   frames["SYNIPO"] = pd.DataFrame(dict(
      Open=o, High=np.maximum(o, c) * 1.01, Low=np.minimum(o, c) * 0.99,
      Close=c, Volume=rng.uniform(4e6, 9e6, m)), index=dates[-m:])
   if want is not None and "SYNIPO" not in want:
      del frames["SYNIPO"]
   return frames


# Technical features for one stock (all use trailing data only).
def make_features(df, bc):
   c, h, l, v, o = (df["Close"], df["High"], df["Low"], df["Volume"],
                    df["Open"])
   bc = bc.reindex(df.index).ffill()
   r1 = c.pct_change()
   f = pd.DataFrame(index=df.index)
   for n in (1, 3, 5, 10, 21, 63):
      f["ret%d" % n] = c.pct_change(n)
   d = c.diff()
   up = d.clip(lower=0).rolling(14).mean()
   dn = (-d.clip(upper=0)).rolling(14).mean()
   f["rsi14"] = (100 - 100 / (1 + up / (dn + 1e-12))) / 50 - 1
   for n in (20, 50, 200):
      f["dist_sma%d" % n] = c / c.rolling(n).mean() - 1
   f["vol20"] = r1.rolling(20).std()
   tr = pd.concat([h - l, (h - c.shift()).abs(),
                   (l - c.shift()).abs()], axis=1).max(axis=1)
   f["atr14"] = tr.rolling(14).mean() / c
   f["volratio"] = np.log((v + 1) / (v.rolling(20).mean() + 1))
   sd = c.rolling(20).std()
   f["bbpos"] = (c - c.rolling(20).mean()) / (2 * sd + 1e-12)
   f["macd"] = (c.ewm(span=12).mean() - c.ewm(span=26).mean()) / c
   f["gap"] = o / c.shift() - 1
   lo, hi = l.rolling(14).min(), h.rolling(14).max()
   f["stoch"] = (c - lo) / (hi - lo + 1e-12) - 0.5
   # relative strength vs the market, distance from the 52-week high,
   # and 12-month momentum skipping the latest month
   f["rel21"] = f["ret21"] - bc.pct_change(21)
   f["rel63"] = f["ret63"] - bc.pct_change(63)
   f["hi252"] = c / c.rolling(252, min_periods=120).max() - 1
   f["mom12_1"] = c.shift(21) / c.shift(252) - 1
   return f[FEATS]


# Inputs for the quality gate plus a trailing daily-volatility estimate.
def make_extras(df):
   c, v = df["Close"], df["Volume"]
   r1 = c.pct_change()
   e = pd.DataFrame(index=df.index)
   e["sig60"] = r1.rolling(60).std()
   e["dvol"] = (c * v).rolling(60).median()
   e["vol252"] = r1.rolling(252).std() * math.sqrt(252)
   e["dd252"] = c / c.rolling(252, min_periods=60).max() - 1
   e["hist"] = c.notna().cumsum()
   return e


# All stocks aligned on the benchmark calendar as dense numpy panels.
class Data(object):
   def __init__(self, frames):
      bench = frames[BENCH]
      self.dates = bench.index
      self.tickers = sorted(t for t in frames if t != BENCH)
      self.col = dict((t, j) for j, t in enumerate(self.tickers))
      D, T, F = len(self.dates), len(self.tickers), len(FEATS)
      nan = np.nan
      self.O = np.full((D, T), nan)
      self.H = np.full((D, T), nan)
      self.L = np.full((D, T), nan)
      self.C = np.full((D, T), nan)
      X = np.full((D, T, F), nan)
      ex = dict((k, np.full((D, T), nan))
                for k in ("sig60", "dvol", "vol252", "dd252", "hist"))
      for j, t in enumerate(self.tickers):
         df = frames[t]
         b = df.reindex(self.dates)
         self.O[:, j] = b["Open"].values
         self.H[:, j] = b["High"].values
         self.L[:, j] = b["Low"].values
         self.C[:, j] = b["Close"].values
         X[:, j, :] = make_features(
            df, bench["Close"]).reindex(self.dates).values
         e = make_extras(df).reindex(self.dates)
         for k in ex:
            ex[k][:, j] = e[k].values
      self.BO = bench["Open"].reindex(self.dates).values
      self.BC = bench["Close"].reindex(self.dates).values
      self.sig60 = ex["sig60"]
      self.atr = X[:, :, FEATS.index("atr14")].copy()
      f = FILTERS
      self.why_not = dict(
         price=~(self.C >= f["min_price"]),
         liquidity=~(ex["dvol"] >= f["min_dollar_vol"]),
         volatility=~(ex["vol252"] <= f["max_vol"]),
         drawdown=~(ex["dd252"] >= f["max_dd"]),
         history=~(ex["hist"] >= f["min_history"]))
      el = np.isfinite(X).all(axis=2)
      for v in self.why_not.values():
         el &= ~v
      self.el = el
      Xm = np.where(el[:, :, None], X, np.nan)
      mu = np.nanmean(Xm, axis=1, keepdims=True)
      sd = np.nanstd(Xm, axis=1, keepdims=True)
      Xz = np.clip((Xm - mu) / (sd + 1e-9), -3, 3)
      self.Xz = np.nan_to_num(Xz)
      self._tg = {}

   def date_str(self, i):
      return self.dates[i].strftime("%Y-%m-%d")

   def idx_of(self, s):
      return int(self.dates.searchsorted(pd.Timestamp(s),
                                         side="right")) - 1

   # Winsorized forward returns, their cross-sectional z-scores, and mask.
   def target(self, h):
      if h not in self._tg:
         Y = np.full(self.C.shape, np.nan)
         Y[:-h] = self.C[h:] / self.C[:-h] - 1
         M = self.el & np.isfinite(Y)
         Ym = np.where(M, Y, np.nan)
         mu = np.nanmean(Ym, axis=1, keepdims=True)
         sd = np.nanstd(Ym, axis=1, keepdims=True)
         Yw = np.clip(Ym, mu - 3 * sd, mu + 3 * sd)
         Yz = (Yw - mu) / (sd + 1e-12)
         self._tg[h] = (np.nan_to_num(Yw), np.nan_to_num(Yz),
                        M.astype(float))
      return self._tg[h]


# ------------------------------------------------- genetic algorithm
# Score a whole population of weight vectors at once. Fitness = mean
# daily information coefficient (cross-sectional correlation between the
# score and the future return) + half the mean z-return of the top-k.
def eval_pop(W, X, Yz, M, k):
   P, F = W.shape
   D, T = M.shape
   S = (X.reshape(-1, F) @ W.T).T.reshape(P, D, T)
   n = M.sum(axis=1)
   ok = n >= max(10, 2 * k)
   if not ok.any():
      raise ValueError("no usable dates")
   Mb = M[None]
   Sm = (S * Mb).sum(2) / np.maximum(n, 1)[None]
   Sc = (S - Sm[:, :, None]) * Mb
   cov = (Sc * Yz[None]).sum(2)
   den = np.sqrt((Sc ** 2).sum(2) * (Yz ** 2).sum(1)[None]) + 1e-12
   ic = cov / den
   Sk = np.where(Mb > 0, S, -np.inf)
   idx = np.argpartition(Sk, -k, axis=2)[:, :, -k:]
   top = np.take_along_axis(np.broadcast_to(Yz, S.shape), idx,
                            axis=2).mean(2)
   fit = (ic[:, ok].mean(1) + 0.5 * top[:, ok].mean(1)
          - 0.02 * (W ** 2).mean(1))
   return fit, ic


def run_ga(X, Yz, M, k, rng, pop, gens, init=None):
   F = X.shape[2]
   W = rng.uniform(-1, 1, (pop, F))
   seeds = np.zeros((3, F))
   for name, wt in (("ret21", 1), ("ret63", 1), ("dist_sma50", 1)):
      seeds[0, FEATS.index(name)] = wt
   for name, wt in (("rsi14", -1), ("bbpos", -1), ("ret3", -1)):
      seeds[1, FEATS.index(name)] = wt
   for name, wt in (("dist_sma200", 1), ("macd", 1), ("ret10", 1)):
      seeds[2, FEATS.index(name)] = wt
   W[:3] = seeds
   # Warm start: a quarter of the population begins from the weights
   # evolved earlier (exact copies first, then jittered copies), so the
   # search keeps building on what it already learned.
   if init is not None and len(init):
      take = max(2, pop // 4)
      base = np.asarray(init)[np.arange(take) % len(init)]
      jit = rng.normal(0, 0.08, base.shape)
      jit[0] = 0.0
      W[3:3 + take] = np.clip(base + jit, -1, 1)
   n_el = max(2, pop // 10)
   for g in range(gens):
      fit, _ = eval_pop(W, X, Yz, M, k)
      order = np.argsort(-fit)
      W, fit = W[order], fit[order]
      if g == gens - 1:
         break
      sigma = 0.3 * (1.0 - g / float(gens)) + 0.05
      nc = pop - n_el
      par = rng.integers(0, pop, (nc, 2, 3)).min(axis=2)
      pa, pb = W[par[:, 0]], W[par[:, 1]]
      child = pa + rng.uniform(-0.25, 1.25, (nc, F)) * (pb - pa)
      mut = (rng.random((nc, F)) < 0.2) * rng.normal(0, sigma, (nc, F))
      W = np.vstack([W[:n_el], np.clip(child + mut, -1, 1)])
   return W[0], fit[0]


# Closed-form ridge regression of the future (z-scored) return on the
# features: a stable, non-evolutionary member for the ensemble.
def ridge_fit(X, Y, M, lam):
   F = X.shape[2]
   A = X.reshape(-1, F)
   keep = M.reshape(-1) > 0
   A, y = A[keep], Y.reshape(-1)[keep]
   G = A.T @ A + lam * len(A) * np.eye(F)
   return np.linalg.solve(G, A.T @ y)


class Model(object):
   pass


# Train on data whose outcome was known by `anchor`; validate on a later
# slice the models never saw; calibrate score -> expected return.
# `warm` (optional) holds weights evolved earlier, used to seed the GA.
def train_model(d, h, k, train_days, anchor, effort, seed=7, warm=None):
   pop, gens, nens = effort
   Yw, Yz, M = d.target(h)
   end = anchor - h
   start = max(0, end - train_days)
   if end - start < 200:
      raise ValueError("not enough history before this date")
   step = 2 if h < 10 else 3
   sub = np.arange(start, end + 1, step)
   Xs, Ys, Ms = d.Xz[sub], Yz[sub], M[sub]
   need = max(10, 2 * k)
   good = Ms.sum(axis=1) >= need
   if good.sum() < 100:
      raise ValueError("not enough eligible history before this date")
   rng = np.random.default_rng(seed + anchor)
   cut = int(len(sub) * 0.75)
   gap = h // step + 1
   vs = slice(cut + gap, None)
   okv = Ms[vs].sum(axis=1) >= need
   # Validation GA starts cold: warm weights may have seen this slice.
   wv, _ = run_ga(Xs[:cut], Ys[:cut], Ms[:cut], k, rng, pop, gens)
   val_ic, val_hit = 0.0, 0.5
   if okv.any():
      _, ic = eval_pop(wv[None], Xs[vs], Ys[vs], Ms[vs], k)
      val_ic = float(ic[0][okv].mean())
      val_hit = float((ic[0][okv] > 0).mean())
   # Ridge member: its penalty is chosen on the validation slice and it
   # joins the ensemble only if it shows positive skill there.
   ridge_ic, best, wr = -1.0, None, None
   if okv.any():
      for lam in (0.05, 0.5, 5.0):
         w = ridge_fit(Xs[:cut], Ys[:cut], Ms[:cut], lam)
         _, ic = eval_pop(w[None], Xs[vs], Ys[vs], Ms[vs], k)
         v = float(ic[0][okv].mean())
         if v > ridge_ic:
            ridge_ic, best = v, lam
      if ridge_ic > 0:
         wr = ridge_fit(Xs, Ys, Ms, best)
   ga = []
   for i in range(nens):
      init = None if warm is None else np.roll(warm, -i, axis=0)
      ga.append(run_ga(Xs, Ys, Ms, k, rng, pop, gens, init)[0])
   ga = np.array(ga)
   ga = ga / (np.linalg.norm(ga, axis=1, keepdims=True) + 1e-12)
   Wm = ga
   if wr is not None:
      Wm = np.vstack([ga, wr / (np.linalg.norm(wr) + 1e-12)])
   nm = len(Wm)
   D, T = Ms.shape
   S = (Xs.reshape(-1, Wm.shape[1]) @ Wm.T).T.reshape(nm, D, T)
   n = np.maximum(Ms.sum(1), 1)
   mu = (S * Ms[None]).sum(2) / n[None]
   dev = (S - mu[:, :, None]) * Ms[None]
   sd = np.sqrt((dev ** 2).sum(2) / n[None]) + 1e-12
   ens = (dev / sd[:, :, None]).mean(0)
   es = np.sqrt((ens ** 2).sum(1) / n) + 1e-12
   ens = ens / es[:, None]
   ic = (ens * Ys).sum(1) / (np.sqrt((ens ** 2).sum(1) *
                                     (Ys ** 2).sum(1)) + 1e-12)
   m = Model()
   m.train_ic = float(ic[good].mean())
   Ywsub = Yw[sub]
   m.mu = float((Ywsub * Ms).sum() / Ms.sum())
   m.slope = float(((ens * (Ywsub - m.mu)) * Ms).sum() /
                   ((ens ** 2 * Ms).sum() + 1e-12))
   if val_ic > 0 and m.train_ic > 0:
      m.shrink = min(1.0, max(0.15, val_ic / m.train_ic))
   else:
      m.shrink = 0.15
   m.W, m.val_ic, m.val_hit, m.nens = Wm, val_ic, val_hit, nm
   m.ga = ga
   m.ridge_ic = ridge_ic if wr is not None else None
   m.warm = warm is not None
   return m


# Score every eligible stock on date index `idx`.
def score_rows(d, m, c, idx):
   el = d.el[idx]
   n = int(el.sum())
   if n < 10:
      return None
   h = c["horizon"]
   z = d.Xz[idx]
   S = z @ m.W.T
   Sn = (S - S[el].mean(axis=0)) / (S[el].std(axis=0) + 1e-12)
   ens = Sn.mean(axis=1)
   ens = (ens - ens[el].mean()) / (ens[el].std() + 1e-12)
   k = max(5, int(math.ceil(0.1 * n)))
   votes = np.zeros(len(el))
   for q in range(Sn.shape[1]):
      col = np.where(el, Sn[:, q], -np.inf)
      votes[np.argpartition(col, -k)[-k:]] += 1.0
   agree = votes / Sn.shape[1]
   wavg = m.W.mean(axis=0)
   quality = (0.5 * min(1.0, max(0.0, 0.4 + 8 * m.val_ic)) +
              0.5 * min(1.0, max(0.0, (m.val_hit - 0.4) / 0.3)))
   rows = []
   for j in np.where(el)[0]:
      px = d.C[idx, j]
      sig_h = d.sig60[idx, j] * math.sqrt(h)
      exp = m.mu + m.shrink * m.slope * ens[j]
      if sig_h > 0:
         exp = min(2 * sig_h, max(-2 * sig_h, exp))
      pup = phi(exp / sig_h) if sig_h > 0 else 0.5
      p_s = min(1.0, max(0.0, (pup - 0.5) / 0.30))
      zc = min(1.0, max(0.0, ens[j] / 3.0))
      conf = 100.0 * (0.30 * agree[j] + 0.25 * p_s + 0.25 * quality +
                      0.20 * zc)
      contrib = z[j] * wavg
      top = np.argsort(-np.abs(contrib))[:3]
      why = ", ".join("%s %+.1fsd(%s)" % (FEATS[q], z[j, q],
                      "+" if contrib[q] > 0 else "-") for q in top)
      rows.append(dict(
         j=j, t=d.tickers[j], price=px, exp=exp, conf=conf, sig=ens[j],
         agree=agree[j], pup=pup, why=why,
         stop=stop_px(px, c, d.atr[idx, j]), atr=d.atr[idx, j],
         target=px * (1 + c["target_mult"] * max(exp, 0.0))))
   rows.sort(key=lambda r: -r["exp"])
   for i, r in enumerate(rows):
      r["rank"] = i + 1
      r["n"] = n
   info = dict(n=n, val_ic=m.val_ic, val_hit=m.val_hit,
               train_ic=m.train_ic, shrink=m.shrink, nens=m.nens,
               ridge=m.ridge_ic, warm=m.warm)
   return dict(rows=rows, info=info)


# Atomic JSON write (never leaves a half-written state file behind).
def dump_json(path, obj):
   os.makedirs(os.path.dirname(path), exist_ok=True)
   tmp = path + ".tmp"
   with open(tmp, "w") as fh:
      json.dump(obj, fh)
   os.replace(tmp, path)


# Fit the calibration from the first n resolved predictions (sorted by
# the day their outcome became known): a linear map raw expected gain ->
# realized gain, and a monotone map raw confidence -> realized chance of
# a gain (binned, shrunk toward the overall rate, pooled to monotone).
def fit_calib(R, n):
   e, cf = R["exp"][:n], R["conf"][:n]
   ret, ua, ub = R["ret"][:n], R["uavg"][:n], R["ub"][:n]
   var = float(e.var())
   b = 0.0
   corr = 0.0
   if var > 1e-12 and float(ret.std()) > 0:
      b = float(np.cov(e, ret, bias=True)[0, 1] / var)
      corr = float(np.corrcoef(e, ret)[0, 1])
   b = min(1.25, max(0.0, b))
   a = float(ret.mean() - b * e.mean())
   exc = float((ret - ua).mean())
   win = (ret > 0).astype(float)
   pw = float(win.mean())
   chunks = np.array_split(np.argsort(cf), max(2, min(6, n // 50)))
   vals = [(win[ch].sum() + 20 * pw) / (len(ch) + 20) for ch in chunks]
   wts = [len(ch) + 20.0 for ch in chunks]
   xsm = [float(cf[ch].mean()) * w for ch, w in zip(chunks, wts)]
   i = 0
   while i < len(vals) - 1:
      if vals[i] > vals[i + 1]:
         w = wts[i] + wts[i + 1]
         vals[i] = (vals[i] * wts[i] + vals[i + 1] * wts[i + 1]) / w
         xsm[i] += xsm[i + 1]
         wts[i] = w
         del vals[i + 1], wts[i + 1], xsm[i + 1]
         i = max(i - 1, 0)
      else:
         i += 1
   xs = [x / w for x, w in zip(xsm, wts)]
   return dict(n=n, a=a, b=b, corr=corr, exc=exc, base=float(ub.mean()),
               xs=xs, ys=vals,
               edge=bool(corr > 0.02 and b >= 0.15 and exc > 0))


# One-line description of a calibration (or the lack of one).
def cal_text(cal):
   if cal is None:
      return ("NONE yet (confidence is a heuristic score, not odds). "
              "Run 'calibrate'; needs %d+ past predictions." % MIN_PAIRS)
   s = ("%d past predictions | gain slope %.2f | rank corr %+.3f | picks "
        "beat universe by %s | universe up-rate %.0f%%" % (
           cal["n"], cal["b"], cal["corr"], pct(cal["exc"]),
           100 * cal["base"]))
   if not cal["edge"]:
      s += "\n  -> NO DEMONSTRATED EDGE: BUY calls are suppressed"
   return s


# -------------------------------------------------------------- engine
class Picker(object):
   def __init__(self, data, mode, effort):
      self.d = data
      self.mode = mode
      self.effort = effort
      self.over = {}
      self.asof = None
      self.models = {}
      self.cache = {}
      self.real = self.load_ledger()
      self.pretend = []
      # pretend_exit maps a real position's key to (day index, price)
      # of a sale made while an as-of date was active. Practice buys
      # (pretend) and these sales live only in this session until
      # 'savepositions' writes them into the real ledger.
      self.pretend_exit = {}
      self.last_buys = []
      self.warm = {}
      self.preds = []
      self.last_learn = {}
      self.pver = 0
      self._rp = {}
      self._fit = {}
      self.dirty = False
      self.load_state()
      self.resolve()

   # Learning state: warm-start weights, prediction journal, timestamps.
   def load_state(self):
      try:
         with open(STATE) as fh:
            st = json.load(fh)
      except Exception:
         st = {}
      if st.get("feats") == FEATS:
         for key, lst in st.get("warm", {}).items():
            mode, h = key.split("|")
            self.warm[(mode, int(h))] = [
               (e["date"], np.array(e["W"])) for e in lst]
      self.last_learn = st.get("last_learn", {})
      try:
         with open(PREDS) as fh:
            self.preds = json.load(fh)
      except Exception:
         self.preds = []

   def save_state(self):
      warm = {}
      for (mode, h), lst in self.warm.items():
         keep = sorted(lst, key=lambda e: e[0])[-6:]
         warm["%s|%d" % (mode, h)] = [
            dict(date=dt, W=W.tolist()) for dt, W in keep]
      dump_json(STATE, dict(version=1, feats=FEATS, warm=warm,
                            last_learn=self.last_learn))
      dump_json(PREDS, self.preds)
      self.dirty = False

   # Newest warm-start weights learned strictly before `date`.
   def warm_for(self, mode, h, date):
      best = None
      for dt, W in self.warm.get((mode, h), []):
         if dt < date and (best is None or dt > best[0]):
            best = (dt, W)
      return None if best is None else best[1]

   def add_warm(self, mode, h, date, W):
      lst = [e for e in self.warm.get((mode, h), []) if e[0] != date]
      lst.append((date, W))
      lst.sort(key=lambda e: e[0])
      self.warm[(mode, h)] = lst[-60:]

   # Fill in the outcome of every prediction whose horizon has passed.
   def resolve(self):
      d = self.d
      last = len(d.dates) - 1
      changed = 0
      for p in self.preds:
         if p.get("ret") is not None:
            continue
         i0 = d.idx_of(p["date"])
         i1 = i0 + p["h"]
         if i0 < 0 or i1 > last or d.date_str(i0) != p["date"]:
            continue
         if p["t"] not in d.col:
            continue
         j = d.col[p["t"]]
         if not (np.isfinite(d.C[i0, j]) and np.isfinite(d.C[i1, j])):
            continue
         el = d.el[i0] & np.isfinite(d.C[i1])
         allr = d.C[i1][el] / d.C[i0][el] - 1
         p.update(ret=float(d.C[i1, j] / d.C[i0, j] - 1),
                  spy=float(d.BC[i1] / d.BC[i0] - 1),
                  ub=float((allr > 0).mean()) if len(allr) else 0.5,
                  uavg=float(allr.mean()) if len(allr) else 0.0,
                  end=d.date_str(i1))
         changed += 1
      if changed:
         self.pver += 1
         self.dirty = True
      return changed

   # Resolved predictions for one mode/horizon as arrays sorted by the
   # day the outcome was known (a live record replaces a backtest one).
   def resolved(self, mode, h):
      key = (self.pver, mode, h)
      if key not in self._rp:
         seen = {}
         for p in self.preds:
            if p["mode"] != mode or p["h"] != h or p.get("ret") is None:
               continue
            k = (p["date"], p["t"])
            if k not in seen or p["src"] == "live":
               seen[k] = p
         P = sorted(seen.values(), key=lambda p: p["end"])
         arr = lambda f: np.array([p[f] for p in P], dtype=float)
         self._rp[key] = dict(
            endi=np.array([int(p["end"].replace("-", "")) for p in P]),
            exp=arr("exp"), conf=arr("conf"), ret=arr("ret"),
            uavg=arr("uavg"), ub=arr("ub"))
      return self._rp[key]

   # Calibration using only predictions whose outcome was already known
   # on `cutoff` (so a backtest never peeks at its own future).
   def calib(self, mode, h, cutoff):
      R = self.resolved(mode, h)
      n = int((R["endi"] <= int(cutoff.replace("-", ""))).sum())
      if n < MIN_PAIRS:
         return None
      key = (self.pver, mode, h, n)
      if key not in self._fit:
         self._fit[key] = fit_calib(R, n)
      return self._fit[key]

   # Replace raw expected gain / confidence by their calibrated values.
   def apply_cal(self, res, mode, h, idx):
      c = self.cfg(mode)
      cal = self.calib(mode, h, self.d.date_str(idx))
      info = dict(res["info"])
      info["cal"] = cal
      rows = []
      for r in res["rows"]:
         r = dict(r)
         r["exp_raw"], r["conf_raw"] = r["exp"], r["conf"]
         if cal:
            pw = float(np.interp(r["conf"], cal["xs"], cal["ys"]))
            r["exp"] = cal["a"] + cal["b"] * r["exp"]
            r["conf"] = 100.0 * pw
            r["lift"] = pw - cal["base"]
            r["edge"] = cal["edge"]
            r["cal"] = True
            r["target"] = r["price"] * (
               1 + c["target_mult"] * max(r["exp"], 0.0))
         rows.append(r)
      return dict(rows=rows, info=info)

   # Journal today's top picks so their real outcome can be scored later.
   def log_live(self, mode, idx, res):
      if self.asof is not None or idx != len(self.d.dates) - 1:
         return
      c = self.cfg(mode)
      h, ds = c["horizon"], self.d.date_str(idx)
      live = dict(((p["mode"], p["h"], p["date"], p["t"]), p)
                  for p in self.preds if p["src"] == "live")
      for r in res["rows"][:15]:
         rec = dict(src="live", mode=mode, h=h, date=ds, t=r["t"],
                    price=float(r["price"]), exp=float(r["exp_raw"]),
                    conf=float(r["conf_raw"]), expc=float(r["exp"]),
                    confc=float(r["conf"]), cal=bool(r.get("cal")),
                    buy=bool(self.is_buy(r, c)), ret=None)
         old = live.get((mode, h, ds, r["t"]))
         if old is None:
            self.preds.append(rec)
         elif old.get("ret") is None:
            old.update(rec)
      self.dirty = True

   # Replay history day by day (strictly walk-forward) and record what
   # the model would have predicted and what then happened.
   def calibrate(self, mode, days=None, say=None):
      d, c = self.d, self.cfg(mode)
      h, last = c["horizon"], len(self.d.dates) - 1
      hi = last - h
      lo = max(1, hi - (days or c["cal_days"]))
      idxs = sorted(range(hi, lo, -c["cal_step"]))
      have = set((p["mode"], p["h"], p["date"], p["t"])
                 for p in self.preds)
      added = 0
      for n, i in enumerate(idxs):
         if say and n % 4 == 0:
            say("   replaying %s  (%d/%d)" % (d.date_str(i), n + 1,
                                              len(idxs)))
         try:
            res = self.scores(mode, i, exact=False, raw=True,
                              stride=c["cal_stride"])
         except ValueError:
            continue
         if res is None:
            continue
         ds = d.date_str(i)
         for r in res["rows"][:15]:
            key = (mode, h, ds, r["t"])
            if key in have:
               continue
            have.add(key)
            self.preds.append(dict(
               src="bt", mode=mode, h=h, date=ds, t=r["t"],
               price=float(r["price"]), exp=float(r["exp"]),
               conf=float(r["conf"]), ret=None))
            added += 1
      self.resolve()
      self.dirty = True
      return added

   def load_ledger(self):
      try:
         with open(LEDGER) as fh:
            return json.load(fh)
      except Exception:
         return []

   def save_ledger(self):
      os.makedirs(HOME, exist_ok=True)
      with open(LEDGER, "w") as fh:
         json.dump(self.real, fh, indent=1)

   # Identity of a real position, used to record practice sales.
   def pkey(self, q):
      return (q["ticker"], q["mode"], q["entry_date"], q["entry_price"])

   # Positions held at the close of day idx: real ledger entries bought
   # on or before it and not yet sold (a sale records exit_date and
   # exit_price in the ledger), plus practice buys, minus practice
   # sales. Moving 'asof' therefore shows what was held on that date.
   def held(self, idx=None):
      d = self.d
      i = self.cur() if idx is None else idx
      out = []
      for q in self.real:
         if d.idx_of(q["entry_date"]) > i:
            continue
         if q.get("exit_date") and d.idx_of(q["exit_date"]) <= i:
            continue
         x = self.pretend_exit.get(self.pkey(q))
         if x and x[0] <= i:
            continue
         out.append(q)
      for q in self.pretend:
         if d.idx_of(q["entry_date"]) <= i:
            out.append(q)
      return out

   ledger = held

   def practice(self):
      return bool(self.pretend or self.pretend_exit)

   # Record a buy: into the real ledger on the latest date, otherwise
   # as a practice trade.
   def add_pos(self, q):
      if self.asof is None:
         self.real.append(q)
         self.save_ledger()
      else:
         self.pretend.append(q)

   # Record a sale of position q at price px on the current date.
   def close_pos(self, q, px):
      i = self.cur()
      if any(q is x for x in self.pretend):
         self.pretend = [x for x in self.pretend if x is not q]
      elif self.asof is None:
         q["exit_date"] = self.d.date_str(i)
         q["exit_price"] = float(px)
         self.save_ledger()
      else:
         self.pretend_exit[self.pkey(q)] = (i, float(px))

   def cfg(self, mode=None):
      m = mode or self.mode
      c = dict(MODES[m])
      c.update(self.over.get(m, {}))
      return c

   def cur(self):
      return len(self.d.dates) - 1 if self.asof is None else self.asof

   def model(self, mode, anchor):
      c = self.cfg(mode)
      key = (mode, c["horizon"], anchor, self.effort)
      if key not in self.models:
         ds = self.d.date_str(anchor)
         warm = self.warm_for(mode, c["horizon"], ds)
         m = train_model(
            self.d, c["horizon"], c["topk"], c["train_days"], anchor,
            EFFORT[self.effort], warm=warm)
         self.models[key] = m
         self.add_warm(mode, c["horizon"], ds, m.ga)
         self.dirty = True
      return self.models[key]

   # exact=True trains on data up to idx; otherwise the model is only
   # retrained every `stride` days (faster, still strictly walk-forward).
   # raw=True skips calibration (used while building the calibration).
   def scores(self, mode, idx, exact=True, raw=False, stride=None):
      c = self.cfg(mode)
      anchor = idx if exact else idx - idx % (stride or c["stride"])
      key = ("s", mode, c["horizon"], idx, anchor, self.effort)
      if key not in self.cache:
         self.cache[key] = score_rows(self.d, self.model(mode, anchor),
                                      c, idx)
      res = self.cache[key]
      if raw or res is None:
         return res
      return self.apply_cal(res, mode, c["horizon"], idx)

   # Calibrated rows must also show an edge and beat the universe's
   # up-rate by min_lift; uncalibrated rows use the heuristic bar.
   def is_buy(self, r, c):
      if r.get("cal"):
         return bool(r["edge"] and r["exp"] >= c["min_gain"] and
                     r["lift"] >= c["min_lift"])
      return r["exp"] >= c["min_gain"] and r["conf"] >= c["min_conf"]

   # Trailing levels for a held position as of the close of day idx:
   # the highest close since the buy (never below the entry price), the
   # trailing stop (0.0 when off) and the pullback level below which a
   # projected drop turns into a SELL. Both only ever move up.
   def levels(self, p, idx):
      d = self.d
      c = self.cfg(p["mode"])
      j = d.col[p["ticker"]]
      i0 = d.idx_of(p["entry_date"])
      cl = d.C[max(i0, 0):idx + 1, j]
      cl = cl[np.isfinite(cl)]
      high = max(p["entry_price"], float(cl.max()) if len(cl) else 0.0)
      atr = p.get("atr")
      if not atr:
         # positions recorded by older versions: recover the ATR used
         # for their stop, else take it from the entry date
         if p.get("stop", 0) > 0 and c["stop_atr"] > 0:
            atr = (1 - p["stop"] / p["entry_price"]) / c["stop_atr"]
         else:
            atr = d.atr[max(i0, 0), j]
         if not (np.isfinite(atr) and atr > 0):
            atr = 0.02
      stop = high * (1 - c["stop_atr"] * atr) if c["stop_atr"] > 0 \
         else 0.0
      dip = high * (1 - max(c["dip_atr"], 0.0) * atr)
      return high, stop, dip

   # SELL / HOLD decision for a held position on date index idx.
   # A position is held as long as the model projects a rise. SELL when
   #   - the close falls to the trailing stop (stop_atr ATRs below the
   #     highest close since the buy), whatever the projection, or
   #   - the model projects a drop AND the close is already at least
   #     dip_atr ATRs below that high (one bad day alone is not enough),
   #   - or an optional holding limit is reached (max_hold > 0).
   # Reaching the target or slipping in rank never forces a sale.
   def judge(self, p, idx, by, check_levels=True):
      d = self.d
      c = self.cfg(p["mode"])
      j = d.col[p["ticker"]]
      px = d.C[idx, j]
      if not np.isfinite(px):
         return "HOLD", "no price on this date"
      held = idx - d.idx_of(p["entry_date"])
      high, stop, dip = self.levels(p, idx)
      off = "%s from high %.2f" % (pct(px / high - 1), high)
      if check_levels and stop > 0 and px <= stop:
         return "SELL", "trailing stop %.2f hit (%s)" % (stop, off)
      if 0 < c["max_hold"] <= held:
         return "SELL", "holding limit (%d days) reached" % \
            c["max_hold"]
      if by is None:
         return "HOLD", "no fresh projection today (%d days held)" % held
      r = by.get(j)
      if r is None:
         why = [k for k, v in d.why_not.items() if v[idx, j]]
         return "HOLD", ("no projection: fails quality gate (%s); "
                         "trailing stop still applies" % (
                            ", ".join(why) or "insufficient data"))
      if r["exp"] < 0:
         if px <= dip:
            return "SELL", "model expects a drop of %s and price is %s" \
               % (pctd(r["exp"], px), off)
         return "HOLD", ("model expects a dip of %s, but price is only "
                         "%s (sell on a close below %.2f)" % (
                            pctd(r["exp"], px), off, dip))
      note = ""
      if px >= p["target"]:
         note = ", past target %.2f" % p["target"]
      return "HOLD", ("still expects %s (rank %d, conf %.0f%%%s, %d "
                      "days held)" % (pctd(r["exp"], px), r["rank"],
                                      r["conf"], note, held))

   def make_pos(self, mode, t, entry_idx, price, r=None):
      d = self.d
      c = self.cfg(mode)
      j = d.col[t]
      atr = d.atr[entry_idx, j]
      atr = atr if np.isfinite(atr) else 0.02
      # stop and target are measured from the actual entry price, which
      # can differ from the close when 'buy TICKER price' is used
      gain = max(r["exp"], 0.0) * c["target_mult"] if r else \
         2 * c["min_gain"]
      return dict(ticker=t, mode=mode, entry_date=d.date_str(entry_idx),
                  entry_price=float(price),
                  stop=float(stop_px(price, c, atr)),
                  target=float(price * (1 + gain)), atr=float(atr),
                  exp=float(r["exp"]) if r else 0.0,
                  conf=float(r["conf"]) if r else 0.0)

   def actual(self, j, i0, h):
      d = self.d
      last = len(d.dates) - 1
      i1 = min(i0 + h, last)
      if i1 <= i0:
         return None
      r = d.C[i1, j] / d.C[i0, j] - 1
      spy = d.BC[i1] / d.BC[i0] - 1
      return r, spy, i1 - i0

   # Trade the model day by day over [end - days, end]; decisions use the
   # close, execution happens at the NEXT open, the stop-loss uses the
   # day's low. Positions are kept while a rise is still projected.
   def simulate(self, mode, days, capital, slip):
      d, c = self.d, self.cfg(mode)
      end = self.cur()
      start = int(d.dates.searchsorted(d.dates[end] -
                                       pd.Timedelta(days=days)))
      start = max(start, 1)
      cash, eq_prev = capital, capital
      pos, trades, equity = {}, [], []
      pend_sell, pend_buy = {}, []
      ncal = ndec = 0

      def close_pos(j, i, px, why):
         nonlocal cash
         p = pos.pop(j)
         px = px * (1 - slip)
         cash += p["shares"] * px
         spy = d.BC[i] / d.BO[p["i0"]] - 1
         trades.append(dict(
            t=d.tickers[j], i0=p["i0"], i1=i, px0=p["px"], px1=px,
            pnl=p["shares"] * (px - p["px"]), ret=px / p["px"] - 1,
            why=why, spy=spy, exp=p["exp"], conf=p["conf"]))

      for i in range(start, end + 1):
         for j, why in list(pend_sell.items()):
            if j in pos and np.isfinite(d.O[i, j]):
               close_pos(j, i, d.O[i, j], why)
         pend_sell = {}
         for r in pend_buy:
            j, px = r["j"], d.O[i, r["j"]] * (1 + slip)
            size = min(cash, eq_prev / c["max_pos"])
            if j in pos or not np.isfinite(px) or size < 100:
               continue
            cash -= size
            atr = r["atr"] if np.isfinite(r["atr"]) else 0.02
            pos[j] = dict(shares=size / px, px=px, i0=i, exp=r["exp"],
                          conf=r["conf"], pp=dict(
                             ticker=d.tickers[j], mode=mode,
                             entry_date=d.date_str(i), entry_price=px,
                             atr=atr, stop=stop_px(px, c, atr),
                             target=px * (r["target"] / r["price"])))
         pend_buy = []
         # trailing stop known before today's trading (highest close
         # through yesterday), checked against today's low
         for j in list(pos):
            p, lo, op = pos[j], d.L[i, j], d.O[i, j]
            _, stop, _ = self.levels(p["pp"], i - 1)
            if stop > 0 and lo <= stop:
               close_pos(j, i, min(stop, op), "trailing stop")
         eq = cash + sum(p["shares"] * d.C[i, j] for j, p in pos.items())
         equity.append(eq)
         eq_prev = eq
         if i == end:
            for j in list(pos):
               close_pos(j, i, d.C[i, j] / (1 - slip), "window end")
            equity[-1] = cash
            break
         full = (i - start) % c["rebalance"] == 0
         sc = self.scores(mode, i, exact=False) if full else None
         if sc:
            ndec += 1
            ncal += 1 if sc["info"]["cal"] else 0
         by = dict((r["j"], r) for r in sc["rows"]) if sc else None
         for j, p in pos.items():
            act, why = self.judge(p["pp"], i, by, check_levels=False)
            if act == "SELL":
               pend_sell[j] = why
         if sc:
            free = c["max_pos"] - (len(pos) - len(pend_sell))
            held = set(pos)
            cands = [r for r in sc["rows"]
                     if self.is_buy(r, c) and r["j"] not in held]
            pend_buy = cands[:max(free, 0)]
      el0 = d.el[start]
      ew = d.C[end][el0] / d.C[start][el0] - 1
      return dict(start=start, end=end, trades=trades, equity=equity,
                  cal=(ncal, ndec), spy=d.BC[end] / d.BC[start] - 1,
                  ew=float(np.nanmean(ew)), capital=capital)


# ---------------------------------------------------------- the shell
class Shell(cmd.Cmd):
   def __init__(self, picker):
      cmd.Cmd.__init__(self)
      self.p = picker
      self.set_prompt()
      d = picker.d
      self.intro = ("\nstock_picker -- %d stocks, data through %s. "
                    "Type 'help'.\nResearch tool only; not financial "
                    "advice." % (len(d.tickers), d.date_str(-1)))

   def set_prompt(self):
      a = "latest" if self.p.asof is None else \
         self.p.d.date_str(self.p.asof)
      self.prompt = "[%s | as of %s%s] > " % (
         self.p.mode, a, " | practice" if self.p.practice() else "")

   def postcmd(self, stop, line):
      self.set_prompt()
      if self.p.dirty:
         self.p.save_state()
      return stop

   def emptyline(self):
      pass

   def default(self, line):
      print("Unknown command '%s'. Type 'help'." % line.split()[0])

   def do_help(self, arg):
      print("")
      for k in ("mode", "horizon", "asof", "nextday", "recommend",
                "track", "buy",
                "sell", "positions", "realpositions",
                "savepositions", "compare",
                "follow", "simulate", "watch", "unwatch", "watchlist",
                "universe", "refresh", "calibrate", "scorecard",
                "learn", "set", "effort", "status", "quit"):
         print("  " + HELP[k])
      print("")

   def do_quit(self, arg):
      return True

   do_exit = do_quit
   do_EOF = do_quit

   def do_status(self, arg):
      p, d = self.p, self.p.d
      c = p.cfg()
      i = p.cur()
      print("mode=%s horizon=%dd (%s) min_gain=%s min_conf=%.0f "
            "effort=%s" % (p.mode, c["horizon"], hold_text(c),
                           pct(c["min_gain"]), c["min_conf"], p.effort))
      print("as of %s (%d trading days of future data available), "
            "%d eligible stocks" % (d.date_str(i), len(d.dates) - 1 - i,
                                    int(d.el[i].sum())))
      print("min_lift=%.1f points, max_pos=%d, trailing stop_atr=%.2f, "
            "dip_atr=%.2f" % (100 * c["min_lift"], c["max_pos"],
                              c["stop_atr"], c["dip_atr"]))
      for mode in ("short", "long"):
         h = p.cfg(mode)["horizon"]
         R = p.resolved(mode, h)
         live = [q for q in p.preds if q["mode"] == mode and
                 q["src"] == "live"]
         print("%s: %d resolved predictions (%d live journal entries), "
               "%d warm-start sets, last learned %s" % (
                  mode, len(R["ret"]), len(live),
                  len(p.warm.get((mode, h), [])),
                  p.last_learn.get(mode, "never")))
         print("   calibration: %s" % cal_text(
            p.calib(mode, h, d.date_str(i))))

   def do_mode(self, arg):
      a = arg.strip().lower()[:1]
      if a not in ("s", "l"):
         print("usage: mode short|long")
         return
      self.p.mode = "short" if a == "s" else "long"
      c = self.p.cfg()
      print("Mode %s: buys on a projected rise over ~%d trading days, "
            "holds until a drop\nis expected (%s), up to %d positions."
            % (self.p.mode, c["horizon"], hold_text(c), c["max_pos"]))

   def do_horizon(self, arg):
      try:
         h = int(arg)
         assert h >= 1
      except Exception:
         print("usage: horizon N   (trading days)")
         return
      self.p.over.setdefault(self.p.mode, {}).update(horizon=h)
      print("Horizon for %s mode is now %d days." % (self.p.mode, h))

   def do_effort(self, arg):
      a = arg.strip().lower()
      if a not in EFFORT:
         print("usage: effort low|normal|high")
         return
      self.p.effort = a
      print("GA effort set to %s (retrains on next use)." % a)

   def do_asof(self, arg):
      p, d = self.p, self.p.d
      a = arg.strip().lower()
      last = len(d.dates) - 1
      if a in ("", "now", "latest"):
         p.asof = None
      else:
         try:
            if a.startswith("-") and a[1:].isdigit():
               i = last - int(a[1:])
            elif a.startswith("+") and a[1:].isdigit():
               i = min(last, p.cur() + int(a[1:]))
            else:
               i = d.idx_of(a)
            assert 0 <= i <= last
         except Exception:
            print("usage: asof YYYY-MM-DD | -N | +N | now")
            return
         p.asof = None if i == last else i
      i = p.cur()
      print("Pretending today is %s; %d trading days of 'future' data "
            "exist for checking." % (d.date_str(i), last - i))
      h = p.held(i)
      print("Positions held on %s (real ledger by buy/sell date): %s" % (
         d.date_str(i),
         " ".join("%s[%s]" % (q["ticker"], q["mode"]) for q in h)
         or "none"))
      if p.asof is not None:
         print("Buys/sells you make while pretending are practice "
               "trades; 'savepositions'\nwrites them to the real ledger.")

   # Step the pretend date forward one business day (the next trading
   # day in the price history; weekends and market holidays are skipped).
   def do_nextday(self, arg):
      p, d = self.p, self.p.d
      if p.asof is None:
         print("Already at the latest date (%s); there is no next day "
               "yet. Use 'asof DATE' or 'asof -N' first." %
               d.date_str(len(d.dates) - 1))
         return
      self.do_asof("+1")

   def do_universe(self, arg):
      d, i = self.p.d, self.p.cur()
      ok = [t for j, t in enumerate(d.tickers) if d.el[i, j]]
      print("%d of %d stocks pass the quality gate on %s:" % (
         len(ok), len(d.tickers), d.date_str(i)))
      print("  " + " ".join(ok))
      for j, t in enumerate(d.tickers):
         if not d.el[i, j]:
            why = [k for k, v in d.why_not.items() if v[i, j]]
            print("  excluded %-6s %s" % (t, ", ".join(why) or
                                          "insufficient data"))

   # Download price history for symbols that are not loaded yet, add it
   # to the session and to the price cache, and rebuild the data.
   def load_new(self, syms):
      p = self.p
      if ARGS.synthetic:
         allf = synth_frames(set(syms) | set([BENCH]))
         got = dict((t, allf[t]) for t in syms if t in allf)
         miss = [t for t in syms if t not in got]
         if miss:
            print("No usable data for: %s" % " ".join(miss))
      else:
         got = download(syms, ARGS.years, syms, False)
      if not got:
         return got
      FRAMES.update(got)
      if not ARGS.synthetic:
         try:
            with open(CACHE, "rb") as fh:
               c = pickle.load(fh)
            c["frames"].update(got)
            save_cache(c["frames"], set(c["requested"]) | set(syms),
                       c.get("years", ARGS.years), c["time"])
         except Exception:
            pass
      p.d = Data(FRAMES)
      p.models, p.cache = {}, {}
      return got

   def do_watch(self, arg):
      p = self.p
      ts = [x.upper() for x in arg.replace(",", " ").split()]
      ts = [t for t in ts if t.replace("-", "").replace(".", "").isalnum()]
      if not ts:
         print("usage: watch TICKER [TICKER ...]   (a newly listed "
               "stock, e.g. 'watch ABCD')")
         return
      w = load_watch()
      have = set(x["ticker"] for x in w)
      for t in ts:
         if t not in have:
            w.append(dict(ticker=t, added=time.strftime("%Y-%m-%d")))
            have.add(t)
      save_watch(w)
      need = [t for t in ts if t not in p.d.col]
      if need:
         self.load_new(need)
      print("Watching: %s" % " ".join(x["ticker"] for x in w))
      self.show_watch(ts)

   def do_unwatch(self, arg):
      ts = set(x.upper() for x in arg.replace(",", " ").split())
      w = load_watch()
      keep = [x for x in w if x["ticker"] not in ts]
      if len(keep) == len(w):
         print("Not on the watchlist: %s" % " ".join(sorted(ts)))
         return
      save_watch(keep)
      print("Removed %s. (A stock that passes the quality gate is still "
            "picked up if it is in the universe.)" % " ".join(
               sorted(x["ticker"] for x in w if x["ticker"] in ts)))

   def do_watchlist(self, arg):
      w = load_watch()
      if not w:
         print("The watchlist is empty. Use 'watch TICKER' to add a "
               "newly listed stock.")
         return
      self.show_watch([x["ticker"] for x in w])

   # One block per watched symbol as of the current date: when it began
   # trading, how it has done, and why it can or cannot be recommended.
   def show_watch(self, tickers):
      p, d = self.p, self.p.d
      i = p.cur()
      f = FILTERS
      by, c = None, p.cfg()
      try:
         res = p.scores(p.mode, i)
         by = dict((r["t"], r) for r in res["rows"]) if res else None
      except ValueError:
         pass
      print("\n--- watchlist as of %s ---" % d.date_str(i))
      for t in tickers:
         if t not in d.col:
            print("%-6s no price data yet (not trading, or unknown "
                  "symbol); retried at start-up and on 'refresh'" % t)
            continue
         j = d.col[t]
         col = d.C[:, j]
         ok = np.where(np.isfinite(col))[0]
         first = int(ok[0])
         n = int(np.isfinite(col[:i + 1]).sum())
         if i < first:
            print("%-6s first traded %s; not trading yet on this date" %
                  (t, d.date_str(first)))
            continue
         px = col[i]
         op = d.O[first, j] if np.isfinite(d.O[first, j]) else col[first]
         line = "%-6s first traded %s (%d trading days) | %.2f | " \
            "since IPO-day open %s" % (t, d.date_str(first), n, px,
                                       pct(px / op - 1))
         for k in (5, 21):
            if n > k and np.isfinite(col[i - k]):
               line += " | %dd %s" % (k, pct(px / col[i - k] - 1))
         print(line)
         if d.el[i, j]:
            r = (by or {}).get(t)
            msg = "ELIGIBLE: passes the quality gate"
            if r:
               msg += " | model: %s conf %.0f%% rank %d/%d -> %s" % (
                  pctd(r["exp"], r["price"]), r["conf"], r["rank"],
                  r["n"], "BUY" if p.is_buy(r, c) else "watch")
            print("      " + msg)
            continue
         why = []
         for k, v in d.why_not.items():
            if v[i, j]:
               if k == "history":
                  why.append("history %d of %d days (about %d more "
                             "trading days)" % (n, f["min_history"],
                                                max(0, f["min_history"]
                                                    - n)))
               else:
                  why.append(k)
         print("      WATCH ONLY, not recommended: %s" % (
            "; ".join(why) or "too little data for the model"))

   def do_refresh(self, arg):
      p = self.p
      frames = load_frames(
         p.d.tickers, ARGS.years, force=True, synthetic=ARGS.synthetic,
         ipo=[w["ticker"] for w in load_watch()])
      global FRAMES
      FRAMES = frames
      p.d = Data(frames)
      p.models, p.cache = {}, {}
      p.resolve()
      print("Reloaded through %s; %d past predictions now have outcomes."
            % (p.d.date_str(-1), sum(1 for q in p.preds
                                     if q.get("ret") is not None)))

   # ----------------------------------------------------- learning
   def do_set(self, arg):
      a = arg.split()
      names = ("min_gain", "min_lift", "min_conf", "max_pos", "stop_atr",
               "dip_atr", "target_mult", "max_hold")
      if len(a) != 2 or a[0] not in names:
         print("usage: set NAME VALUE   NAME is one of: %s" %
               ", ".join(names))
         return
      try:
         v = float(a[1])
      except ValueError:
         print("VALUE must be a number")
         return
      if a[0] in ("min_gain", "min_lift"):
         v /= 100.0
      if a[0] in ("max_pos", "max_hold"):
         v = int(v)
      self.p.over.setdefault(self.p.mode, {})[a[0]] = v
      self.p.cache = {}
      print("%s mode: %s = %s" % (self.p.mode, a[0], a[1]))

   def do_calibrate(self, arg):
      p, d = self.p, self.p.d
      c = p.cfg()
      days = int(arg) if arg.strip().isdigit() else None
      print("Replaying %d days of %s-mode history, retraining along the "
            "way (a few minutes on real data)..." % (
               days or c["cal_days"], p.mode))
      n = p.calibrate(p.mode, days, say=lambda m: print(m, end="\r"))
      print(" " * 50)
      print("Added %d predictions with known outcomes." % n)
      print("Calibration: " + cal_text(
         p.calib(p.mode, c["horizon"], d.date_str(len(d.dates) - 1))))

   def do_learn(self, arg):
      p, d = self.p, self.p.d
      a = arg.split()
      modes = ["short", "long"] if "all" in a else [p.mode]
      days = next((int(x) for x in a if x.isdigit()), None)
      last = len(d.dates) - 1
      p.resolve()
      for mode in modes:
         c = p.cfg(mode)
         print("Learning %s mode..." % mode)
         n = p.calibrate(mode, days, say=lambda m: print(m, end="\r"))
         p.scores(mode, last, raw=True)
         p.last_learn[mode] = time.strftime("%Y-%m-%d %H:%M")
         print(" " * 50)
         print("  %d new replayed predictions; model retrained on data "
               "through %s (warm-started: %s)" % (
                  n, d.date_str(last),
                  "yes" if p.warm_for(mode, c["horizon"],
                                      d.date_str(last)) is not None
                  else "no"))
         print("  calibration: " + cal_text(
            p.calib(mode, c["horizon"], d.date_str(last))))
      p.dirty = True
      p.save_state()

   def sc_line(self, label, P):
      ret = np.array([q["ret"] for q in P])
      ua = np.array([q["uavg"] for q in P])
      ub = np.array([q["ub"] for q in P])
      print("  %-12s n=%4d (%3d dates) | avg %8s vs universe %8s -> "
            "excess %8s | up %3.0f%% (universe %3.0f%%)" % (
               label, len(P), len(set(q["date"] for q in P)),
               pct(ret.mean()), pct(ua.mean()), pct((ret - ua).mean()),
               100 * (ret > 0).mean(), 100 * ub.mean()))

   def do_scorecard(self, arg):
      p = self.p
      p.resolve()
      pend = [q for q in p.preds if q["src"] == "live" and
              q.get("ret") is None]
      print("\nLive journal: %d predictions still waiting for their "
            "horizon to pass." % len(pend))
      shown = False
      for mode in ("short", "long"):
         h = p.cfg(mode)["horizon"]
         for src, title in (("live", "LIVE forward test (true "
                                     "out-of-sample)"),
                            ("bt", "Walk-forward replay (calibrate/"
                                   "learn)")):
            P = [q for q in p.preds if q["mode"] == mode and
                 q["h"] == h and q["src"] == src and
                 q.get("ret") is not None]
            if not P:
               continue
            shown = True
            print("\n=== %s mode, %d-day horizon: %s ===" % (
               mode.upper(), h, title))
            self.sc_line("all picks", P)
            B = [q for q in P if q.get("buy")]
            if B:
               self.sc_line("BUY calls", B)
            months = sorted(set(q["date"][:7] for q in P))[-8:]
            for mth in months:
               self.sc_line(mth, [q for q in P if q["date"][:7] == mth])
            if len(P) >= 40:
               cf = np.array([q["confc"] if q.get("cal") else q["conf"]
                              for q in P])
               ex = np.array([q["expc"] if q.get("cal") else q["exp"]
                              for q in P])
               rt = np.array([q["ret"] for q in P])
               print("  predicted vs realized by confidence quartile:")
               for ch in np.array_split(np.argsort(cf), 4):
                  print("    conf %3.0f%% -> up %3.0f%% | expected %8s "
                        "-> realized %8s" % (
                           cf[ch].mean(), 100 * (rt[ch] > 0).mean(),
                           pct(ex[ch].mean()), pct(rt[ch].mean())))
      if not shown:
         print("No resolved predictions yet. Run 'calibrate' to replay "
               "history now, and 'recommend' on the latest date to start "
               "the live journal.")

   # ------------------------------------------------ recommendations
   def show_picks(self, res, c, n):
      info = res["info"]
      print("Model: ensemble of %d scorers (GA%s%s) | validation IC "
            "%+.3f (hit rate %.0f%%) | skill shrink %.2f | %d eligible"
            % (info["nens"], " + ridge" if info.get("ridge") else "",
               ", warm-started" if info.get("warm") else "",
               info["val_ic"], 100 * info["val_hit"], info["shrink"],
               info["n"]))
      cal = info.get("cal")
      print("Calibration: " + cal_text(cal))
      print("(Conf = %s; 'why': (+) supports buying, (-) argues against)"
            % ("calibrated chance of a gain over the horizon" if cal
               else "heuristic score, not a probability"))
      print(" #  %-6s %9s %-20s %5s %9s %9s  %s" % (
         "Ticker", "Price", "ExpGain % $/share", "Conf", "Stop",
         "Target", "Call"))
      buys = []
      for r in res["rows"][:n]:
         call = "BUY" if self.p.is_buy(r, c) else "watch"
         if call == "BUY":
            buys.append(r)
         stop = "%.2f" % r["stop"] if r["stop"] > 0 else "none"
         print("%2d  %-6s %9.2f %-20s %4.0f%% %9s %9.2f  %s" % (
            r["rank"], r["t"], r["price"],
            pctd(r["exp"], r["price"], " "), r["conf"], stop,
            r["target"], call))
         print("      why: %s | P(up)=%.0f%%, votes %.0f%%" % (
            r["why"], 100 * r["pup"], 100 * r["agree"]))
      return buys

   def do_recommend(self, arg):
      p, d = self.p, self.p.d
      c, i = p.cfg(), p.cur()
      n = int(arg) if arg.strip().isdigit() else 10
      try:
         res = p.scores(p.mode, i)
      except ValueError as e:
         print("Cannot model this date: %s" % e)
         return
      if res is None:
         print("Too few eligible stocks on this date.")
         return
      print("\n=== %s picks as of %s (expected over %d trading days) ===" %
            (p.mode.upper(), d.date_str(i), c["horizon"]))
      buys = self.show_picks(res, c, n)
      p.last_buys = buys[:c["max_pos"]]
      p.log_live(p.mode, i, res)
      cal = res["info"].get("cal")
      if not buys:
         if cal and not cal["edge"]:
            bar = "the model shows no demonstrated edge on past picks"
         elif cal:
            bar = ("none reaches a %s gain with odds %.1f points above "
                   "the universe up-rate" % (pct(c["min_gain"]),
                                              100 * c["min_lift"]))
         else:
            bar = ("none reaches gain >= %s with confidence >= %.0f%%"
                   % (pct(c["min_gain"]), c["min_conf"]))
         print("\nNo BUY: %s. Standing aside is a valid recommendation."
               % bar)
      else:
         print("\nBUY candidates: %s  ('track' records the top %d)" % (
            " ".join(r["t"] for r in buys), len(p.last_buys)))
      self.show_positions(i)

   do_recommendation = do_recommend
   do_recommendations = do_recommend

   # SELL / HOLD lines for positions on date index i. By default these
   # are the positions held on that date (see Picker.held);
   # 'realpositions' passes the real ledger instead, with its own title.
   def show_positions(self, i, led=None, title=None):
      p, d = self.p, self.p.d
      if led is None:
         led = p.held(i)
      if not led:
         return
      print("\n--- %s ---" % (title or "open positions: sell / hold "
                               "advice"))
      for q in led:
         try:
            res = p.scores(q["mode"], i)
         except ValueError:
            res = None
         by = dict((r["j"], r) for r in res["rows"]) if res else None
         act, why = p.judge(q, i, by)
         px = d.C[i, d.col[q["ticker"]]]
         print("%-5s %-6s [%s] in %s @ %.2f now %.2f (%s): %s" % (
            act, q["ticker"], q["mode"], q["entry_date"],
            q["entry_price"], px,
            pctd(px / q["entry_price"] - 1, q["entry_price"]), why))
         high, stop, dip = p.levels(q, i)
         print("      high since buy %.2f | trailing stop %s | a "
               "projected drop sells below %.2f" % (
                  high, "%.2f" % stop if stop > 0 else "off", dip))

   def do_positions(self, arg):
      if not self.p.held():
         print("No positions held on %s. Use 'track' or 'buy TICKER'." %
               self.p.d.date_str(self.p.cur()))
         return
      self.show_positions(self.p.cur())

   do_position = do_positions

   # The real positions in the ledger file, judged on the latest date,
   # whatever the as-of date or timeline of this session.
   def do_realpositions(self, arg):
      p, d = self.p, self.p.d
      last = len(d.dates) - 1
      led, skip = [], []
      for q in p.load_ledger():
         if q.get("exit_date"):
            continue
         (led if q["ticker"] in d.col else skip).append(q)
      if not led and not skip:
         print("No real positions in %s." % LEDGER)
         return
      self.show_positions(last, led, "real positions (%s) as of the "
                          "latest date, %s" % (LEDGER, d.date_str(last)))
      for q in skip:
         print("%-6s in %s @ %.2f: no price data loaded for this ticker"
               % (q["ticker"], q["entry_date"], q["entry_price"]))

   do_realposition = do_realpositions

   # Commit practice trades into the real ledger: practice buys are
   # appended and practice sales get their exit date and price.
   def do_savepositions(self, arg):
      p, d = self.p, self.p.d
      if not p.practice():
         print("No practice trades to save.")
         return
      n = len(p.pretend)
      for q in p.pretend:
         p.real.append(q)
      m = 0
      for q in p.real:
         x = p.pretend_exit.get(p.pkey(q))
         if x and not q.get("exit_date"):
            q["exit_date"], q["exit_price"] = d.date_str(x[0]), x[1]
            m += 1
      p.pretend, p.pretend_exit = [], {}
      p.save_ledger()
      print("Saved %d practice buy%s and %d sale%s to %s" % (
         n, "" if n == 1 else "s", m, "" if m == 1 else "s", LEDGER))

   do_saveposition = do_savepositions

   def do_track(self, arg):
      p, d = self.p, self.p.d
      if not p.last_buys:
         print("Run 'recommend' first (and have at least one BUY).")
         return
      n = int(arg) if arg.strip().isdigit() else len(p.last_buys)
      i = p.cur()
      have = set((q["ticker"], q["mode"]) for q in p.ledger())
      for r in p.last_buys[:n]:
         if (r["t"], p.mode) in have:
            continue
         p.add_pos(p.make_pos(p.mode, r["t"], i, r["price"], r))
         print("Tracking %s @ %.2f" % (r["t"], r["price"]))

   def do_buy(self, arg):
      p, d = self.p, self.p.d
      a = arg.split()
      if not a or a[0].upper() not in d.col:
         print("usage: buy TICKER [price]   (ticker must be in the "
               "universe)")
         return
      t, i = a[0].upper(), p.cur()
      px = float(a[1]) if len(a) > 1 else d.C[i, d.col[t]]
      r = None
      try:
         res = p.scores(p.mode, i)
         r = dict((x["t"], x) for x in res["rows"]).get(t)
      except Exception:
         pass
      p.add_pos(p.make_pos(p.mode, t, i, px, r))
      print("Recorded %s @ %.2f (%s mode)." % (t, px, p.mode))

   def do_sell(self, arg):
      p, d = self.p, self.p.d
      t = arg.strip().upper()
      led = p.ledger()
      hit = [q for q in led if q["ticker"] == t]
      if not hit:
         print("No open position in %s." % t)
         return
      px = d.C[p.cur(), d.col[t]]
      for q in hit:
         print("Closed %s: %.2f -> %.2f (%s)" % (
            t, q["entry_price"], px,
            pctd(px / q["entry_price"] - 1, q["entry_price"])))
         p.close_pos(q, px)

   # ------------------------------------------------------ hindsight
   def do_compare(self, arg):
      p, d = self.p, self.p.d
      c, i = p.cfg(), p.cur()
      h = c["horizon"]
      last = len(d.dates) - 1
      if i >= last:
         print("No future data after this date. Use 'asof -%d' or a "
               "date at least %d trading days back." % (h + 5, h))
         return
      n = int(arg) if arg.strip().isdigit() else 10
      try:
         res = p.scores(p.mode, i)
      except ValueError as e:
         print("Cannot model this date: %s" % e)
         return
      rows = res["rows"]
      picks = [r for r in rows if p.is_buy(r, c)][:n] or rows[:n]
      flagged = any(p.is_buy(r, c) for r in rows)
      print("\n=== %s picks as of %s vs. what happened over the next %d "
            "days%s ===" % (p.mode.upper(), d.date_str(i), h,
                            "" if i + h <= last else " (partial window)"))
      if not flagged:
         print("(no BUY cleared the bar; showing the top %d ranked)" % n)
      print("%-6s %9s %-20s %5s %9s %9s %8s  %s" % (
         "Ticker", "Price", "ExpGain % $/share", "Conf", "Actual",
         "SPY", "Excess", "Result"))
      acts = []
      for r in picks:
         a = p.actual(r["j"], i, h)
         acts.append(a[0])
         print("%-6s %9.2f %-20s %4.0f%% %9s %9s %8s  %s" % (
            r["t"], r["price"], pctd(r["exp"], r["price"], " "),
            r["conf"], pct(a[0]), pct(a[1]), pct(a[0] - a[1]),
            "WIN" if a[0] > 0 else "loss"))
      a0 = [p.actual(r["j"], i, h)[0] for r in rows]
      spy = p.actual(0, i, h)[1]
      k = min(10, len(rows) // 3)
      top = np.mean(a0[:k])
      bot = np.mean(a0[-k:])
      avg_usd = np.mean([r["exp"] * r["price"] for r in picks])
      print("\nPicks: avg actual %s vs avg expected %s | "
            "win rate %.0f%% | SPY %s" % (
               pct(np.mean(acts)),
               "%s/%s" % (pct(np.mean([r["exp"] for r in picks])),
                          usd(avg_usd)),
               100 * np.mean([x > 0 for x in acts]), pct(spy)))
      print("Whole ranking: top-%d avg %s, bottom-%d avg %s, universe "
            "avg %s  -> spread %s" % (k, pct(top), k, pct(bot),
                                      pct(np.mean(a0)), pct(top - bot)))
      print("(One date is a tiny sample; use 'follow' or 'simulate' for "
            "a broader verdict.)")
      for q in p.ledger():
         if d.idx_of(q["entry_date"]) > i:
            continue
         by = dict((r["j"], r) for r in rows)
         act, why = p.judge(q, i, by)
         a = p.actual(d.col[q["ticker"]], i, h)
         if act == "SELL":
            print("SELL call on %s (%s): stock then moved %s -> sell was "
                  "%s" % (q["ticker"], why, pct(a[0]),
                          "RIGHT" if a[0] < 0 else "WRONG"))
         else:
            print("HOLD call on %s: stock then moved %s -> hold was %s"
                  % (q["ticker"], pct(a[0]),
                     "RIGHT" if a[0] >= 0 else "WRONG"))

   def do_follow(self, arg):
      p, d = self.p, self.p.d
      a = arg.split()
      if not a or a[0].upper() not in d.col:
         print("usage: follow TICKER [steps] [step_days]")
         return
      t = a[0].upper()
      j = d.col[t]
      c = p.cfg()
      h = c["horizon"]
      steps = int(a[1]) if len(a) > 1 else 8
      step = int(a[2]) if len(a) > 2 else h
      last = len(d.dates) - 1
      start = p.asof if p.asof is not None else last - steps * step
      start = max(start, 0)
      print("\n=== following %s, %s mode, every %d trading days ===" % (
         t, p.mode, step))
      pos, strat, calls, ok_calls = None, 1.0, 0, 0
      end_i = start
      for s in range(steps):
         i = start + s * step
         if i > last:
            break
         end_i = i
         try:
            res = p.scores(p.mode, i, exact=False)
         except ValueError as e:
            print("%s: cannot model (%s)" % (d.date_str(i), e))
            break
         by = dict((r["j"], r) for r in res["rows"]) if res else {}
         r = by.get(j)
         px = d.C[i, j]
         act = p.actual(j, i, h)
         if pos:
            label, note = p.judge(pos, i, by)
         elif r is None:
            label, note = "N/A", "not eligible (fails quality gate)"
         elif p.is_buy(r, c):
            label, note = "BUY", r["why"]
         else:
            label = "AVOID" if r["exp"] < 0 else "WATCH"
            note = r["why"]
         if label == "BUY":
            pos = p.make_pos(p.mode, t, i, px, r)
            pos["px"] = px
         elif label == "SELL" and pos:
            strat *= px / pos["px"]
            pos = None
         line = "%s  %8.2f  %-5s" % (d.date_str(i), px, label)
         if r:
            line += "  rank %2d/%d  exp %s  conf %3.0f%%" % (
               r["rank"], r["n"], pctd(r["exp"], px), r["conf"])
         if act:
            line += "  actual %7s (SPY %s)" % (pct(act[0]), pct(act[1]))
            if label in ("BUY", "AVOID", "SELL"):
               good = act[0] > 0 if label == "BUY" else act[0] < 0
               calls += 1
               ok_calls += int(good)
               line += "  %s" % ("ok" if good else "miss")
         print(line)
         print("      %s" % note)
      end_px = d.C[end_i, j]
      if pos:
         strat *= end_px / pos["px"]
      bh = end_px / d.C[start, j] - 1
      print("\nFollowing the picker: %s | buy & hold %s over the same "
            "span | SPY %s" % (pct(strat - 1), pct(bh),
                               pct(d.BC[end_i] / d.BC[start] - 1)))
      if calls:
         print("Directional calls correct: %d of %d" % (ok_calls, calls))

   # ----------------------------------------------------- simulation
   def do_simulate(self, arg):
      p, d = self.p, self.p.d
      a = arg.split()
      days = int(a[0]) if a else 90
      cap = float(a[1]) if len(a) > 1 else 100000.0
      print("Simulating %s mode over %d days ending %s (retrains "
            "periodically; first run can take a minute)..." % (
               p.mode, days, d.date_str(p.cur())))
      try:
         r = p.simulate(p.mode, days, cap, 0.0005)
      except ValueError as e:
         print("Cannot simulate: %s" % e)
         return
      tr = sorted(r["trades"], key=lambda x: x["i1"])
      print("\n%-6s %-10s %-10s %8s %8s %9s %8s %8s  %s" % (
         "Ticker", "Entry", "Exit", "In", "Out", "P/L $", "Return",
         "SPY", "Exit reason / model expected"))
      for x in tr[-40:]:
         print("%-6s %-10s %-10s %8.2f %8.2f %9.2f %8s %8s  %s / %s "
               "(conf %.0f%%)" % (
                  x["t"], d.date_str(x["i0"]), d.date_str(x["i1"]),
                  x["px0"], x["px1"], x["pnl"], pct(x["ret"]),
                  pct(x["spy"]), x["why"], pctd(x["exp"], x["px0"], " "),
                  x["conf"]))
      if len(tr) > 40:
         print("(showing the last 40 of %d trades)" % len(tr))
      eq = np.array(r["equity"])
      dd = float((eq / np.maximum.accumulate(eq) - 1).min())
      total = eq[-1] / r["capital"] - 1
      wins = [x for x in tr if x["pnl"] > 0]
      print("\nWindow %s -> %s | %d trades | win rate %.0f%%" % (
         d.date_str(r["start"]), d.date_str(r["end"]), len(tr),
         100.0 * len(wins) / max(len(tr), 1)))
      print("Simulated P/L: $%.2f (%s) on $%.0f | max drawdown %s" % (
         eq[-1] - r["capital"], pct(total), r["capital"], pct(dd)))
      if tr:
         print("Avg trade %s | avg model expectation %s | avg SPY over "
               "the same holds %s" % (
                  pct(np.mean([x["ret"] for x in tr])),
                  "%s/%s" % (
                     pct(np.mean([x["exp"] for x in tr])),
                     usd(np.mean([x["exp"] * x["px0"] for x in tr]))),
                  pct(np.mean([x["spy"] for x in tr]))))
      print("ACTUAL market over the window: SPY buy&hold %s | equal-"
            "weight universe buy&hold %s" % (pct(r["spy"]),
                                             pct(r["ew"])))
      print("Strategy vs SPY: %s. Slippage 0.05%%/side included; no "
            "taxes. Backtest only." % pct(total - r["spy"]))
      nc, nd = r["cal"]
      print("Calibrated odds were available on %d of %d decision days%s"
            % (nc, nd, "" if nc == nd else
               " (earlier days used the raw heuristic; 'calibrate' a "
               "longer span first for a cleaner test)"))


# Point every persistent file at another directory (used by --synthetic
# so test data never mixes with real predictions).
def set_home(path):
   global HOME, CACHE, LEDGER, STATE, PREDS, WATCH
   HOME = path
   WATCH = os.path.join(HOME, "watchlist.json")
   CACHE = os.path.join(HOME, "prices.pkl")
   LEDGER = os.path.join(HOME, "ledger.json")
   STATE = os.path.join(HOME, "state.json")
   PREDS = os.path.join(HOME, "predictions.json")


def main():
   global ARGS
   ap = argparse.ArgumentParser(description="Local stock picker lab")
   ap.add_argument("--mode", choices=["short", "long"], default="short")
   ap.add_argument("--universe", choices=["large", "small"],
                   default="large",
                   help="built-in list: ~300 large caps or the original "
                   "~75 (default large)")
   ap.add_argument("--tickers", help="comma separated custom universe")
   ap.add_argument("--universe-file", help="text file of tickers")
   ap.add_argument("--years", type=int, default=10)
   ap.add_argument("--effort", choices=list(EFFORT), default="normal")
   ap.add_argument("--refresh", action="store_true")
   ap.add_argument("--learn", action="store_true",
                   help="run 'learn' for the chosen mode at startup")
   ap.add_argument("--synthetic", action="store_true",
                   help="offline fake data for testing the program")
   ARGS = ap.parse_args()
   if ARGS.synthetic:
      set_home(HOME + "-synthetic")
   tickers = LARGE_UNIVERSE if ARGS.universe == "large" \
      else SMALL_UNIVERSE
   if ARGS.tickers:
      tickers = [t.strip().upper() for t in ARGS.tickers.split(",")]
   elif ARGS.universe_file:
      with open(ARGS.universe_file) as fh:
         tickers = fh.read().upper().replace(",", " ").split()
   watch = [w["ticker"] for w in load_watch()]
   frames = load_frames(tickers, ARGS.years, ARGS.refresh,
                        ARGS.synthetic, watch)
   global FRAMES
   FRAMES = frames
   data = Data(frames)
   sh = Shell(Picker(data, ARGS.mode, ARGS.effort))
   if ARGS.learn:
      sh.do_learn("")
   sh.cmdloop()


if __name__ == "__main__":
   main()
