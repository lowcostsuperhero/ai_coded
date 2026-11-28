stock_tracker - local intraday stock tracker with BUY / SELL signals
=====================================================================

Follows a watchlist on 5-minute bars, works out each day's low, high and
expected range, and says when to buy (the price has bottomed out and
turned up, or is on track to set a new high for the day) and when to sell
(the profit target is reached, the trailing stop is hit, the price turns
down, or it plateaus). Everything runs on your machine. The only network
traffic is the download of PUBLIC prices from Yahoo Finance (ticker
symbols only). Positions and settings never leave the machine.

It is the intraday companion to stock_picker, which works on daily bars.
The chart window looks and works like stock_ticker; the text prompt (-t)
looks and works like stock_picker.

Needs Python 3 with numpy, pandas, yfinance and matplotlib, plus tkinter
for the window (it ships with python; on RHEL/Alma/Oracle Linux:
dnf install python3-tkinter):

   pip install numpy pandas yfinance matplotlib

NOT financial advice. Yahoo serves only about 60 days of 5-minute bars
and its intraday quotes may be delayed. Decisions use completed bars
only. Treat all output as an experiment, not a promise.

Run
---
   python stock_tracker.py                    chart window, the watchlist
   python stock_tracker.py AAPL MSFT ORCL     put these first on the
                                              watchlist, in this order
   python stock_tracker.py -c -p 5d           candles, last 5 sessions
   python stock_tracker.py -f                 keep the window in the
                                              foreground
   python stock_tracker.py -t                 text prompt instead
   python stock_tracker.py -s                 offline demo, fake prices
   python stock_tracker.py -h                 all options

Options: -c (start in candle mode), -f (stay in the foreground), -r N
(auto-refresh every N seconds, 0 = off, default 60), -p 1d|2d|5d|10d|20d
(time span shown), -t (text prompt), -s (synthetic data, kept apart in
.stock_tracker-synthetic), -d (re-download all price history), -v (more
messages), then any ticker symbols to put first on the watchlist.

Stocks are shown in watchlist order, not alphabetically: the ones named
on the command line first, in the order given, then the rest of the
watchlist, then any stock you hold that is not on it. "watch" adds new
stocks at the end. "recommend" and "compare" still rank by signal.

The window starts in the background and gives the command prompt back
(on Windows it runs under pythonw.exe with no console). If it fails to
start, its messages are in stock_tracker.log in the temp directory.

The chart window
----------------
   Ticker(s)            the watchlist; Enter or "Refresh now" saves it
   Chart                Line or Candles
   Record buy...        record a buy: ticker, shares (default 1),
                        price (default the current price) and time
                        (default now); change any of them before OK
   Record sell...       record a sale of a stock you hold, with the same
                        fields (shares default to 1)
   Trades / P&L...      profit and loss per stock, and every trade, which
                        can be corrected or deleted (see "Your trades and
                        profit / loss" below)
   Time span            sessions shown: 1d, 2d, 5d, 10d, 20d
   Bar size             5m (the signals are built on 5-minute bars)
   Auto-refresh every   5s to 15m, or off

Under the controls, one row per stock: price and change over the span,
the span's low and high, today's expected range, and the signal (BUY
breakout, BUY rebound, SELL, HOLD or no signal). The low and high are
coloured by where the price sits between them, as in stock_ticker. For a
stock you own the row ends with "Own", the shares held and the average
price paid for them, and the gain or loss on them at the current price
(green for a gain, red for a loss).

The dark bar shows the bar under the mouse: open, close, low, high,
volume with its estimated share of buying, VWAP, the expected range and
any BUY signal on that bar.

The alert box below it lists each BUY or SELL signal as it first appears,
newest first, with the reason, and rings the bell (set bell 0 to stop).

Each stock gets its own panel:

   price               line or candles; nights and weekends leave no gap
   VWAP                orange dashed line, restarts each day
   expected range      gray dotted lines at today's expected low and high
   BUY markers         triangles where a signal starts: green for a
                       breakout, blue for a rebound
   your positions      black dot at the buy, green target line and red
                       trailing stop line from the buy on, and a red
                       triangle when the signal says SELL
   volume              right-hand scale, kept to the bottom third; green
                       where a bar had more buying than selling, red where
                       it had more selling, gray when even

Yahoo does not report buying and selling volume, so it is estimated from
where each bar closed within its range: a close at the high counts as all
buying, at the low as all selling.

The text prompt (-t)
--------------------
   asof DATE [HH:MM]|-N|+N|now   pretend it is that bar (DATE alone is
                            that day's close; N = 5-minute bars), or now
   nextbar [N]              advance the as-of time N bars
   nextday                  same time on the next trading day
   recommend [N]            BUY signals with target and stop, then SELL /
                            HOLD for positions held ("scan" works too)
   track [N]                record the BUY signals as open positions,
                            1 share each
   buy TICKER [price] [shares]   record a buy (defaults: the current
                            price, 1 share)
   sell TICKER [price] [shares|all]   record a sale (defaults: the
                            current price, 1 share)
   positions                positions held at the as-of time, with SELL /
                            HOLD, recent sales and profit and loss
   pnl                      profit and loss per stock ("profit" works too)
   trades                   every buy and sell, with its #
   edittrade # [shares=N] [price=P] [time=YYYY-MM-DD HH:MM]
                            correct a trade after the fact
   deltrade #               remove a trade recorded by mistake
   realpositions            the real ledger's positions, judged on the
                            latest bar (ignores asof)
   savepositions            write practice buys and sells into the ledger
   compare [N]              judge the signals at the as-of time against
                            what happened over the next N bars and by the
                            close
   range [TICKER ...]       the day's low and high, the expected range and
                            where the price is headed
   follow TICKER [DATE]     walk one stock through a day bar by bar with
                            its signals and trades ("replay" works too)
   monitor [quiet]          poll and print ALERTs until Ctrl-C; from an
                            as-of time it replays up to now first
   simulate [days] [TICKER ...]  trade the signals over the loaded bars
                            and compare with random entries ("backtest"
                            works too)
   watch TICKER ...         add stocks to the watchlist
   unwatch TICKER ...       remove stocks from the watchlist
   watchlist                price and data of the watched stocks
   set [NAME VALUE]         list or change the settings ("set NAME
                            default" resets one)
   refresh                  re-download price history
   status, help, quit

Buys and sells made while an as-of time is active are practice trades,
kept apart from your real ledger until "savepositions". The prompt shows
"practice".

Your trades and profit / loss
-----------------------------
Every buy and sell you record is kept in a trade log, with its shares,
price and time. What you hold, and your profit or loss, is worked out
from it. A sale is matched to the earliest shares of that stock still
held (first in, first out, as brokers do by default), so selling part of
a holding is fine. A sale of more shares than you held at that time is
refused.

   unrealized   the gain or loss on the shares still held, at the
                current price, against the average price paid for them
   realized     the gain or loss on the shares sold
   total        the two together

In the window, "Trades / P&L..." shows a table of these per stock with a
total, and the list of trades, newest first. Double-click a trade (or
select it and press "Correct trade...") to change its shares, price or
time; "Delete trade" removes one. A change that would leave a sale of
shares not held is refused. Correcting a buy's price restarts its
trailing stop from that price. At the prompt, "trades", "pnl",
"edittrade" and "deltrade" do the same.

Each buy keeps its own profit target and trailing stop, so two buys of
the same stock are judged separately.

The expected range
------------------
The daily ATR(14) of the days before today says how far a typical day
travels. With the day's low L and high H so far:

   expected high = the larger of H and L + ATR
   expected low  = the smaller of L and H - ATR

Once the day has already moved a full ATR, new extremes are less likely.

When to buy
-----------
No BUY in the first 3 bars of the day (opening noise) or from 15:30 on.

   rebound    the price is in the lowest 30% of the expected range, has
              made no new day low for 3 bars, RSI(14) has bounced at
              least 10 points from 35 or below, the close is back above a
              rising 9-bar average, and it is still below VWAP (room to
              recover)
   breakout   the price is within 0.1 ATR of the day high, the trend over
              the last 12 bars is clean (t-statistic 2 or more) and
              projects above the day high within 6 bars, the 9-bar
              average is above the 21-bar one, the close is above VWAP,
              the day has used no more than 1.25 ATR of range, and
              volume is at least usual for the time of day

When to sell
------------
   profit target   the price reaches 0.8 ATR above the buy
   trailing stop   the price falls 0.5 ATR below the highest price since
                   the buy; it only ever moves up
   turning down    a clean downtrend (t-statistic -2 or less), the 9-bar
                   average below the 21-bar one, and the price below VWAP
   plateau         after at least 6 bars held, no new high for 12 bars
                   and a flat trend (t-statistic between -1 and 1)
   end of day      only with "set eod_exit 1"

The ATR used for a position is the one known when it was bought.

Settings
--------
Every number above is a setting. "set" lists them with their meaning,
"set NAME VALUE" changes one, and changes are kept between sessions.
They apply to the window too. Among them: target_atr, trail_atr,
bottom_zone, near_high, t_min, max_used, vol_min, min_hold, plateau_bars,
no_buy_after, eod_exit, poll (seconds between "monitor" polls) and slip
(backtest slippage per side, percent).

Reading the output
------------------
Gains are shown as a percent and as dollars for one share, e.g.
+0.20%/+$0.32 in running text and +0.20% +$0.32 in tables, as in
stock_picker. Times are the START of each 5-minute bar.

Testing it
----------
"simulate" trades the signals over the ~60 days of 5-minute bars:
decisions on a bar's close, execution at the next bar's open, the stop
and target checked inside each bar, slippage on both sides. It compares
each trade with buying at a random bar and holding as long, and reports
the edge and, with 100 or more trades, its t-statistic. With fewer
trades the result can be luck; watch more stocks for a broader test.
"follow" shows any one day bar by bar.

What to expect
--------------
Tested so far on synthetic prices only, which have trends built in, so
those results say nothing about real stocks. Run "simulate" on your own
watchlist before trusting an alert.

Files
-----
In the folder .stock_tracker next to stock_tracker.py (versions before
26-Nov-2026 kept it in your home directory; the first run copies it next
to the script, and the home folder is used only when the script's folder
cannot be written to):

   watchlist.json   the watched stocks
   ledger.json      your trade log (buys and sells); a ledger written
                    by an older version is converted the first time and
                    the old file kept as ledger.json.v1 (a position
                    recorded without a share count is taken as 1 share)
   settings.json    settings changed from the defaults
   intraday.pkl     cached 5-minute bars
   daily.pkl        cached daily bars (for the ATR)

   stock_tracker.py           the program (single file)
   README-stock_tracker.txt   this file
