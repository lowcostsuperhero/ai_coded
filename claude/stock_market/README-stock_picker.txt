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
   python stock_picker.py --universe small         the original 78 stocks
   python stock_picker.py --tickers AAPL,MSFT,KO,JNJ
   python stock_picker.py --universe-file my_tickers.txt
   python stock_picker.py --synthetic              offline demo, fake prices
   python stock_picker.py -h                       all options

On Windows, stock_picker.bat runs the script with the same arguments.

Options: --mode short|long, --universe large|small (built-in list: about
280 large caps, the default, or the original 78), --tickers LIST,
--universe-file FILE, --years N (price history to download), --effort
low|normal|high (how hard the genetic search works), --refresh
(re-download), --learn (run "learn" at start-up), --synthetic.
--tickers and --universe-file override the built-in lists. Symbols Yahoo
no longer serves are skipped with a message.
Prices are cached in ~/.stock_picker so later starts are quick; a fresh
cache only downloads the symbols it does not have yet.

Modes
-----
   short   buy on a projection about 2 trading days ahead; swing style
   long    buy on a projection about 63 trading days (3 months) ahead


Switch at the prompt with "mode short" or "mode long".

Commands
--------
   mode short|long          switch between short and long holding
   horizon N                override the holding horizon (trading days)
   asof DATE|-N|+N|now      pretend today is DATE, N trading days back or
                            forward, or return to now
   recommend [N]            top N picks with expected gain, confidence and
                            SELL advice for positions you hold
                            ("recommendation" and "recommendations" work
                            too)
   track [N]                record the BUY picks as open positions
   buy TICKER [price]       record a position you opened
   sell TICKER              close a recorded position
   positions                positions held on the as-of date, with SELL /
                            HOLD advice ("position" works too)
   compare [N]              judge the picks at the as-of date against what
                            really happened afterwards
   follow TICKER [n] [d]    walk one stock forward period by period and
                            show what the picker would say and why
   simulate [days] [cap]    trade the model over the last N days (default
                            90) and report profit/loss against actual
   watch TICKER ...         add newly listed (IPO) stocks to the watchlist
   unwatch TICKER ...       remove stocks from the watchlist
   watchlist                price, history and gate status of watched
                            stocks
   universe                 which stocks pass the quality gate
   refresh                  re-download price history
   effort low|normal|high   genetic search effort
   status, help, quit

   calibrate [days]         replay history to measure how reliable past
                            picks were
   scorecard                real results of past recommendations (live and
                            backtest)
   learn [days|all]         score outcomes, calibrate, retrain, save
   set NAME VALUE           change a threshold for the current mode

   nextday                  advance the as-of date one business day
   realpositions            the real ledger's open positions, judged on
                            the latest date (ignores asof and practice
                            trades; "realposition" works too)
   savepositions            write practice buys and sells into the real
                            ledger ("saveposition" works too)

Which stocks are considered
---------------------------
Only established, liquid names. A quality gate is applied to every stock
on every date, using only data known by then:

   price at least $20, median 60-day dollar volume at least $100 million,
   annualized volatility at most 55%, no drop of more than 50% from the
   252-day high, and at least about 15 months of history

No penny stocks, thin stocks or junk. "universe" shows who passes.

New listings (IPOs)
-------------------
A newly listed stock fails the history test for about 15 months. To
follow one anyway, "watch TICKER" adds it to a watchlist kept in
~/.stock_picker/watchlist.json. Its prices are downloaded however short
the history, and "watchlist" shows its first trade date, trading days,
price, return since the IPO-day open, 5- and 21-day moves, and why it is
watch-only (with about how many trading days of history remain). It is
never recommended until it passes the normal quality gate; then
"watchlist" also shows the model's expected gain, confidence and rank.
A watched symbol with no data yet (not trading, or a typo) stays on the
list and is retried at start-up and on "refresh".

How it scores
-------------
Each stock gets 21 technical features (momentum over several windows,
RSI, distance from moving averages, volatility, ATR, volume ratio,
Bollinger position, MACD, gap, stochastic, strength relative to SPY,
distance from the 252-day high, 12-1 month momentum). A genetic algorithm
evolves weights so the weighted score ranks stocks by their following
N-day return, and its fitness is the cross-sectional rank correlation
plus the return of the top picks. An ensemble of independently evolved
scorers votes, joined by a ridge-regression member when that member shows
skill on the validation slice. The slice the search never saw measures
real skill, and that skill shrinks the expected gain and drives the
confidence figure. Evolved weights are saved and seed the next search
(warm start). Walk-forward only: a model used on date X was trained
solely on data known by X.

Reading the output
------------------
Expected gain is shown as a percent and as dollars for one share, e.g.
+0.20%/+$0.32 in running text. In tables the two are separated by a
space: +0.20% +$0.32. Gain or loss on open positions and after a sell is
shown the same way.

Learning over time
------------------
The first answer is honest, not flattering. Files live in ~/.stock_picker:

   predictions.json   journal of every prediction and, later, its outcome
   state.json         evolved weights that seed the next search
   ledger.json        your positions, open and sold
   prices.pkl         cached price history
   watchlist.json     watched new listings

Every "recommend" on the latest date is journaled and later scored
against what really happened ("scorecard"). "calibrate" and "learn"
replay history walk-forward to produce paired predictions and outcomes.
From these pairs the program:

   - rescales expected gain by how much of it really materialized
   - turns confidence into the observed chance of a gain
   - checks whether the picks show any edge at all

When there is no demonstrated edge, or the expected gain is too small,
BUY calls are suppressed. That is deliberate. With fewer than 150 past
predictions it falls back to a heuristic. Calibration for a past date
only uses outcomes that were known by that date, so replays never peek.
"set" changes thresholds (min_gain, min_lift, min_conf, max_pos, ...).

When to sell
------------
A position is held for as long as the model still projects a rise.
Both exits are measured from the highest close since the buy, so they
only ever move up:

   trailing stop   SELL when the price falls stop_atr x ATR below that
                   high, whatever the projection (set stop_atr 0 to turn
                   it off)
   expected drop   SELL when the model projects a decline AND the price is
                   already dip_atr x ATR below that high, so one bad day
                   alone does not trigger a sale (set dip_atr 0 to sell
                   on the projection alone)

Defaults: short mode stop_atr 1.5, dip_atr 0.75; long mode 3.0 and 1.5.
There is no maximum holding period unless you set one ("set max_hold N").
Reaching the target never forces a sale; it is only noted.

Past dates and practice trades
------------------------------
"positions" shows what your real ledger says you held on the as-of date:
bought on or before it and not yet sold. Moving "asof" back and forward
changes the list, and "asof" prints the holdings for the new date. A
sale keeps the position in the ledger with its exit date and price, so
past holdings can be rebuilt (positions sold by versions before
23-Nov-2026 were deleted and cannot be shown on past dates).

Buys and sells made while an as-of date is active are practice trades.
They live only in the session, the prompt shows "practice" while any
exist, and nothing is saved automatically. "savepositions" writes them
into the real ledger: practice buys are added and practice sales get
their exit date and price. "nextday" steps forward one business day, and
"realpositions" always shows the real ledger's open positions judged on
the latest date.

What to expect
--------------
On real price history of 78 large caps, in tests with this code, the
picks showed no demonstrated edge in either mode, so BUY calls were
mostly suppressed. That is the program being honest. The default
universe is now about 280 large caps, which gives the calibration more
to work with; it has not yet been tested on real prices.

Files
-----
   stock_picker.py           the program (single file)
   stock_picker.bat          Windows launcher
   README-stock_picker.txt   this file
