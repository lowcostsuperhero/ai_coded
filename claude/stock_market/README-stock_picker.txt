stock_picker - local, interactive stock recommendation lab
==========================================================

Recommends large, established stocks to buy, says when to sell, shows the
expected gain and a confidence figure, and lets you test every call
against what really happened. A genetic algorithm and a small ensemble do
the scoring, and everything runs on your machine. The only network
traffic is the download of PUBLIC price history from Yahoo Finance (ticker
symbols only). Positions, results and models never leave the machine.

Needs Python 3 with numpy, pandas and yfinance:

   pip install numpy pandas yfinance

NOT financial advice. Daily bars only, so "short" mode is a swing
strategy, not true intraday trading. The built-in universe is today's
blue chips, so backtests suffer survivorship bias and look better than
reality. Treat all output as an experiment, not a promise.

Run
---
   python stock_picker.py                          short mode, built-in list
   python stock_picker.py --mode long --effort high
   python stock_picker.py --tickers AAPL,MSFT,KO,JNJ
   python stock_picker.py --universe-file my_tickers.txt
   python stock_picker.py --synthetic              offline demo, fake prices
   python stock_picker.py -h                       all options

Options: --mode short|long, --tickers LIST, --universe-file FILE,
--years N (price history to download), --effort low|normal|high (how hard
the genetic search works), --refresh (re-download), --synthetic.
Prices are cached in ~/.stock_picker so later starts are quick.

Modes
-----
   short   hold for 1-3 days; swing-trading style
   long    hold for months (about 63 trading days ahead)


Switch at the prompt with "mode short" or "mode long".

Commands
--------
   mode short|long          switch between short and long holding
   horizon N                override the holding horizon (trading days)
   asof DATE|-N|+N|now      pretend today is DATE, N trading days back or
                            forward, or return to now
   recommend [N]            top N picks with expected gain, confidence and
                            SELL advice for positions you hold
   track [N]                record the BUY picks as open positions
   buy TICKER [price]       record a position you opened
   sell TICKER              close a recorded position
   positions                open positions with SELL / HOLD advice
   compare [N]              judge the picks at the as-of date against what
                            really happened afterwards
   follow TICKER [n] [d]    walk one stock forward period by period and
                            show what the picker would say and why
   simulate [days] [cap]    trade the model over the last N days (default
                            90) and report profit/loss against actual
   universe                 which stocks pass the quality gate
   refresh                  re-download price history
   effort low|normal|high   genetic search effort
   status, help, quit

Which stocks are considered
---------------------------
Only established, liquid names. A quality gate is applied to every stock
on every date, using only data known by then:

   price at least $20, median 60-day dollar volume at least $100 million,
   annualized volatility at most 55%, no drop of more than 50% from the
   252-day high, and at least about 15 months of history

No penny stocks, thin stocks or junk. "universe" shows who passes.

How it scores
-------------
Each stock gets technical features (momentum over several windows, RSI,
distance from moving averages, volatility, ATR, volume ratio, Bollinger
position, MACD, gap, stochastic, strength relative to SPY, distance from
the 252-day high). A genetic algorithm evolves weights so the weighted
score ranks stocks by their following N-day return. An ensemble of
independently evolved scorers votes. A validation slice the search never
saw measures real skill, and that skill shrinks the expected gain and
drives the confidence figure. It is walk-forward only: a model used on
date X was trained solely on data known by X.

Reading the output
------------------
Each pick shows the expected gain as a percent and a confidence figure
from 0 to 100. Confidence is not a probability.

When to sell
------------
A position is sold when the stop-loss is hit, the target is reached, the
maximum holding period ends, the model now expects a decline, or the
stock stops passing the quality gate.

Files
-----
   stock_picker.py   the program (single file)
   README.txt        this file
