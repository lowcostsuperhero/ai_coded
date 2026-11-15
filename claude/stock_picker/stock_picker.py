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

NOT financial advice. Daily bars only, so "short" mode is a 1-3 day swing
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
BENCH = "SPY"

UNIVERSE = """
AAPL MSFT GOOGL AMZN NVDA META AVGO ORCL ADBE CRM CSCO IBM TXN QCOM
INTC AMAT ACN INTU
JPM BAC WFC GS MS AXP BLK SPGI V MA BRK-B
UNH JNJ LLY MRK ABBV PFE TMO ABT DHR MDT AMGN GILD BMY
PG KO PEP WMT COST CL MDLZ MO PM MCD SBUX NKE HD LOW TGT
XOM CVX COP LIN
HON UNP UPS CAT DE GE MMM LMT RTX
VZ T CMCSA TMUS DIS SO DUK NEE
""".split()

# Quality gate applied to every stock on every date (trailing data only):
# no low-priced stocks, median 60-day dollar volume must be liquid,
# 252-day annualized volatility capped, no >50% drop from the 252-day
# high, and at least ~15 months of history.
FILTERS = dict(min_price=20.0, min_dollar_vol=1e8, max_vol=0.55,
               max_dd=-0.50, min_history=300)

MODES = dict(
   short=dict(horizon=2, train_days=750, max_hold=3, stop_atr=1.5,
              target_mult=2.5, min_gain=0.004, min_conf=35.0,
              max_pos=3, stride=5, rebalance=1, topk=5),
   long=dict(horizon=63, train_days=1250, max_hold=126, stop_atr=3.0,
             target_mult=1.5, min_gain=0.03, min_conf=35.0,
             max_pos=8, stride=21, rebalance=5, topk=5))

# (population, generations, ensemble size)
EFFORT = dict(low=(24, 12, 3), normal=(40, 20, 4), high=(64, 40, 6))

FEATS = ["ret1", "ret3", "ret5", "ret10", "ret21", "ret63", "rsi14",
         "dist_sma20", "dist_sma50", "dist_sma200", "vol20", "atr14",
         "volratio", "bbpos", "macd", "gap", "stoch"]

HELP = dict(
   mode="mode short|long       switch between 1-3 day and multi-month",
   horizon="horizon N            override holding horizon (trading days)",
   asof="asof DATE|-N|+N|now  pretend today is DATE (YYYY-MM-DD), N "
        "trading days back/forward, or now",
   recommend="recommend [N]        top N picks with gain, confidence, "
             "sells for held positions",
   track="track [N]            record the BUY picks as open positions",
   buy="buy TICKER [price]   record a position you opened",
   sell="sell TICKER          close a recorded position",
   positions="positions            show open positions and SELL/HOLD",
   compare="compare [N]          judge picks at the as-of date against "
           "what really happened",
   follow="follow TICKER [steps] [step_days]   walk one stock forward "
          "period by period",
   simulate="simulate [days] [capital]   trade the model over the last "
            "N days (default 90)",
   universe="universe             show which stocks pass the quality gate",
   refresh="refresh              re-download price history",
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


# ---------------------------------------------------------------- data
# Download (or load from cache) daily OHLCV for the tickers + benchmark.
def load_frames(tickers, years, force=False, synthetic=False):
   if synthetic:
      return synth_frames()
   want = sorted(set(tickers) | set([BENCH]))
   if os.path.exists(CACHE) and not force:
      try:
         with open(CACHE, "rb") as fh:
            c = pickle.load(fh)
         fresh = time.time() - c["time"] < 20 * 3600
         if fresh and set(want) <= set(c["requested"]):
            print("Using cached prices (use 'refresh' to re-download).")
            return dict((t, c["frames"][t]) for t in want
                        if t in c["frames"])
      except Exception:
         pass
   frames = download(want, years)
   os.makedirs(HOME, exist_ok=True)
   with open(CACHE, "wb") as fh:
      pickle.dump(dict(time=time.time(), requested=want,
                       frames=frames), fh)
   return frames


def download(want, years):
   try:
      import yfinance as yf
   except ImportError:
      sys.exit("Missing package. Run: pip install yfinance")
   print("Downloading %d symbols of public price history..." % len(want))
   raw = yf.download(want, period="%dy" % years, auto_adjust=True,
                     group_by="ticker", progress=False, threads=True)
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
      if len(df) > 400:
         frames[t] = df
   bad = [t for t in want if t not in frames]
   if bad:
      print("No usable data for: %s" % " ".join(bad))
   if BENCH not in frames:
      sys.exit("Could not download benchmark %s." % BENCH)
   return frames


# Random-walk stocks with slow drift regimes, for offline testing only.
def synth_frames():
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
   return frames


# Technical features for one stock (all use trailing data only).
def make_features(df):
   c, h, l, v, o = (df["Close"], df["High"], df["Low"], df["Volume"],
                    df["Open"])
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
         X[:, j, :] = make_features(df).reindex(self.dates).values
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


def run_ga(X, Yz, M, k, rng, pop, gens):
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


class Model(object):
   pass


# Train on data whose outcome was known by `anchor`; validate on a later
# slice the GA never saw; calibrate score -> expected return.
def train_model(d, h, k, train_days, anchor, effort, seed=7):
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
   wv, _ = run_ga(Xs[:cut], Ys[:cut], Ms[:cut], k, rng, pop, gens)
   vs = slice(cut + gap, None)
   val_ic, val_hit = 0.0, 0.5
   if (Ms[vs].sum(axis=1) >= need).any():
      _, ic = eval_pop(wv[None], Xs[vs], Ys[vs], Ms[vs], k)
      okv = Ms[vs].sum(axis=1) >= need
      val_ic = float(ic[0][okv].mean())
      val_hit = float((ic[0][okv] > 0).mean())
   Wm = np.array([run_ga(Xs, Ys, Ms, k, rng, pop, gens)[0]
                  for _ in range(nens)])
   Wm = Wm / (np.linalg.norm(Wm, axis=1, keepdims=True) + 1e-12)
   D, T = Ms.shape
   S = (Xs.reshape(-1, Wm.shape[1]) @ Wm.T).T.reshape(nens, D, T)
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
   m.W, m.val_ic, m.val_hit, m.nens = Wm, val_ic, val_hit, nens
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
      p_s = min(1.0, max(0.0, (pup - 0.5) / 0.15))
      conf = 100.0 * (0.40 * agree[j] + 0.30 * p_s + 0.30 * quality)
      contrib = z[j] * wavg
      top = np.argsort(-np.abs(contrib))[:3]
      why = ", ".join("%s %+.1fsd(%s)" % (FEATS[q], z[j, q],
                      "+" if contrib[q] > 0 else "-") for q in top)
      rows.append(dict(
         j=j, t=d.tickers[j], price=px, exp=exp, conf=conf, sig=ens[j],
         agree=agree[j], pup=pup, why=why,
         stop=px * (1 - c["stop_atr"] * d.atr[idx, j]),
         target=px * (1 + c["target_mult"] * max(exp, 0.0))))
   rows.sort(key=lambda r: -r["exp"])
   for i, r in enumerate(rows):
      r["rank"] = i + 1
      r["n"] = n
   info = dict(n=n, val_ic=m.val_ic, val_hit=m.val_hit,
               train_ic=m.train_ic, shrink=m.shrink, nens=m.nens)
   return dict(rows=rows, info=info)


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
      self.last_buys = []

   def load_ledger(self):
      try:
         with open(LEDGER) as fh:
            return json.load(fh)
      except Exception:
         return []

   def save_ledger(self):
      if self.asof is None:
         os.makedirs(HOME, exist_ok=True)
         with open(LEDGER, "w") as fh:
            json.dump(self.real, fh, indent=1)

   def ledger(self):
      return self.real if self.asof is None else self.pretend

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
         self.models[key] = train_model(
            self.d, c["horizon"], c["topk"], c["train_days"], anchor,
            EFFORT[self.effort])
      return self.models[key]

   # exact=True trains on data up to idx; otherwise the model is only
   # retrained every `stride` days (faster, still strictly walk-forward).
   def scores(self, mode, idx, exact=True):
      c = self.cfg(mode)
      anchor = idx if exact else idx - idx % c["stride"]
      key = ("s", mode, c["horizon"], idx, anchor, self.effort)
      if key not in self.cache:
         self.cache[key] = score_rows(self.d, self.model(mode, anchor),
                                      c, idx)
      return self.cache[key]

   def is_buy(self, r, c):
      return r["exp"] >= c["min_gain"] and r["conf"] >= c["min_conf"]

   # SELL / HOLD decision for a held position on date index idx.
   def judge(self, p, idx, by, check_levels=True):
      d = self.d
      c = self.cfg(p["mode"])
      j = d.col[p["ticker"]]
      px = d.C[idx, j]
      if not np.isfinite(px):
         return "HOLD", "no price on this date"
      held = idx - d.idx_of(p["entry_date"])
      if check_levels:
         if px <= p["stop"]:
            return "SELL", "stop-loss hit (%.2f)" % p["stop"]
         if px >= p["target"]:
            return "SELL", "target reached (%.2f), take profit" % \
               p["target"]
      if held >= c["max_hold"]:
         return "SELL", "max holding period (%d days) reached" % \
            c["max_hold"]
      if by is None:
         return "HOLD", "inside holding window (%d days held)" % held
      r = by.get(j)
      if r is None:
         return "SELL", "no longer passes the quality filters"
      if r["exp"] < 0:
         return "SELL", "model now expects %s" % pctd(r["exp"], px)
      if r["rank"] > max(10, 3 * c["max_pos"]):
         return "SELL", "signal faded to rank %d of %d" % (r["rank"],
                                                         r["n"])
      return "HOLD", "still expects %s (rank %d, conf %.0f%%)" % (
         pctd(r["exp"], px), r["rank"], r["conf"])

   def make_pos(self, mode, t, entry_idx, price, r=None):
      d = self.d
      c = self.cfg(mode)
      j = d.col[t]
      atr = d.atr[entry_idx, j]
      atr = atr if np.isfinite(atr) else 0.02
      return dict(ticker=t, mode=mode, entry_date=d.date_str(entry_idx),
                  entry_price=float(price),
                  stop=float(r["stop"] if r else price *
                             (1 - c["stop_atr"] * atr)),
                  target=float(r["target"] if r else price *
                               (1 + 2 * c["min_gain"])),
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
   # close, execution happens at the NEXT open, stops/targets use the
   # day's low/high (stop wins if both are touched).
   def simulate(self, mode, days, capital, slip):
      d, c = self.d, self.cfg(mode)
      end = self.cur()
      start = int(d.dates.searchsorted(d.dates[end] -
                                       pd.Timedelta(days=days)))
      start = max(start, 1)
      cash, eq_prev = capital, capital
      pos, trades, equity = {}, [], []
      pend_sell, pend_buy = {}, []

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
            pos[j] = dict(shares=size / px, px=px, i0=i, exp=r["exp"],
                          conf=r["conf"], stop=r["stop"],
                          target=r["target"], entry=d.date_str(i))
         pend_buy = []
         for j in list(pos):
            p, lo, hi, op = pos[j], d.L[i, j], d.H[i, j], d.O[i, j]
            if lo <= p["stop"]:
               close_pos(j, i, min(p["stop"], op), "stop-loss")
            elif hi >= p["target"]:
               close_pos(j, i, max(p["target"], op), "target hit")
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
         by = dict((r["j"], r) for r in sc["rows"]) if sc else None
         for j, p in pos.items():
            pp = dict(ticker=d.tickers[j], mode=mode,
                      entry_date=p["entry"], stop=p["stop"],
                      target=p["target"])
            act, why = self.judge(pp, i, by, check_levels=False)
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
                  spy=d.BC[end] / d.BC[start] - 1,
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
      self.prompt = "[%s | as of %s] > " % (self.p.mode, a)

   def postcmd(self, stop, line):
      self.set_prompt()
      return stop

   def emptyline(self):
      pass

   def default(self, line):
      print("Unknown command '%s'. Type 'help'." % line.split()[0])

   def do_help(self, arg):
      print("")
      for k in ("mode", "horizon", "asof", "recommend", "track", "buy",
                "sell", "positions", "compare", "follow", "simulate",
                "universe", "refresh", "effort", "status", "quit"):
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
      print("mode=%s horizon=%dd max_hold=%dd min_gain=%s min_conf=%.0f "
            "effort=%s" % (p.mode, c["horizon"], c["max_hold"],
                           pct(c["min_gain"]), c["min_conf"], p.effort))
      print("as of %s (%d trading days of future data available), "
            "%d eligible stocks" % (d.date_str(i), len(d.dates) - 1 - i,
                                    int(d.el[i].sum())))

   def do_mode(self, arg):
      a = arg.strip().lower()[:1]
      if a not in ("s", "l"):
         print("usage: mode short|long")
         return
      self.p.mode = "short" if a == "s" else "long"
      c = self.p.cfg()
      print("Mode %s: hold ~%d trading days (max %d), up to %d positions."
            % (self.p.mode, c["horizon"], c["max_hold"], c["max_pos"]))

   def do_horizon(self, arg):
      try:
         h = int(arg)
         assert h >= 1
      except Exception:
         print("usage: horizon N   (trading days)")
         return
      self.p.over.setdefault(self.p.mode, {}).update(
         horizon=h, max_hold=max(h + 1, int(h * 1.5)))
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
      if p.asof is None:
         p.pretend = []
      else:
         p.pretend = [q for q in p.pretend
                      if d.idx_of(q["entry_date"]) <= i]
      print("Pretending today is %s; %d trading days of 'future' data "
            "exist for checking." % (d.date_str(i), last - i))

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

   def do_refresh(self, arg):
      p = self.p
      frames = load_frames(p.d.tickers, ARGS.years, force=True,
                           synthetic=ARGS.synthetic)
      p.d = Data(frames)
      p.models, p.cache = {}, {}
      print("Reloaded through %s." % p.d.date_str(-1))

   # ------------------------------------------------ recommendations
   def show_picks(self, res, c, n):
      info = res["info"]
      print("Model: ensemble of %d GA-evolved scorers | validation IC "
            "%+.3f (hit rate %.0f%%) | skill shrink %.2f | %d eligible"
            % (info["nens"], info["val_ic"], 100 * info["val_hit"],
               info["shrink"], info["n"]))
      print("(IC near 0 = no demonstrated edge; 'why': (+) supports "
            "buying, (-) argues against)")
      print(" #  %-6s %9s %-20s %5s %9s %9s  %s" % (
         "Ticker", "Price", "ExpGain % $/share", "Conf", "Stop",
         "Target", "Call"))
      buys = []
      for r in res["rows"][:n]:
         call = "BUY" if self.p.is_buy(r, c) else "watch"
         if call == "BUY":
            buys.append(r)
         print("%2d  %-6s %9.2f %-20s %4.0f%% %9.2f %9.2f  %s" % (
            r["rank"], r["t"], r["price"],
            pctd(r["exp"], r["price"], " "), r["conf"], r["stop"],
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
      if not buys:
         print("\nNo stock clears the bar (gain >= %s and confidence >= "
               "%.0f%%). Standing aside is a valid recommendation." %
               (pct(c["min_gain"]), c["min_conf"]))
      else:
         print("\nBUY candidates: %s  ('track' records the top %d)" % (
            " ".join(r["t"] for r in buys), len(p.last_buys)))
      self.show_positions(i)

   def show_positions(self, i):
      p, d = self.p, self.p.d
      led = [q for q in p.ledger() if d.idx_of(q["entry_date"]) <= i]
      if not led:
         return
      print("\n--- open positions: sell / hold advice ---")
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
            q["entry_price"], px, pct(px / q["entry_price"] - 1), why))

   def do_positions(self, arg):
      if not self.p.ledger():
         print("No open positions. Use 'track' or 'buy TICKER'.")
         return
      self.show_positions(self.p.cur())

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
         p.ledger().append(p.make_pos(p.mode, r["t"], i, r["price"], r))
         print("Tracking %s @ %.2f" % (r["t"], r["price"]))
      p.save_ledger()

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
      p.ledger().append(p.make_pos(p.mode, t, i, px, r))
      p.save_ledger()
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
            t, q["entry_price"], px, pct(px / q["entry_price"] - 1)))
         led.remove(q)
      p.save_ledger()

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
                          "RIGHT" if a[0] < a[1] else "WRONG"))

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
         if r is None:
            label, note = "N/A", "not eligible (fails quality gate)"
            if pos:
               label, note = "SELL", "dropped out of the quality gate"
         elif pos:
            label, note = p.judge(pos, i, by)
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


def main():
   global ARGS
   ap = argparse.ArgumentParser(description="Local stock picker lab")
   ap.add_argument("--mode", choices=["short", "long"], default="short")
   ap.add_argument("--tickers", help="comma separated custom universe")
   ap.add_argument("--universe-file", help="text file of tickers")
   ap.add_argument("--years", type=int, default=8)
   ap.add_argument("--effort", choices=list(EFFORT), default="normal")
   ap.add_argument("--refresh", action="store_true")
   ap.add_argument("--synthetic", action="store_true",
                   help="offline fake data for testing the program")
   ARGS = ap.parse_args()
   tickers = UNIVERSE
   if ARGS.tickers:
      tickers = [t.strip().upper() for t in ARGS.tickers.split(",")]
   elif ARGS.universe_file:
      with open(ARGS.universe_file) as fh:
         tickers = fh.read().upper().replace(",", " ").split()
   frames = load_frames(tickers, ARGS.years, ARGS.refresh,
                        ARGS.synthetic)
   data = Data(frames)
   Shell(Picker(data, ARGS.mode, ARGS.effort)).cmdloop()


if __name__ == "__main__":
   main()
