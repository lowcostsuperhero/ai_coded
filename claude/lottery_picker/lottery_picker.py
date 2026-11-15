#!/usr/bin/env python3
# lottery_picker.py
#
# Picks PowerBall and Mega Millions numbers (both sold in Pennsylvania)
# from the historical record of winning numbers.
#
# For each game it produces two groups of tickets (5 tickets each by
# default):
#    HOT  - numbers drawn most often and/or most recently
#    COLD - numbers drawn least often and/or not drawn for the longest
#
# Commands:
#    pick     show the HOT and COLD tickets          (default)
#    freq     show how often each number has been drawn
#    window   show how many draws are needed before the numbers have
#             "filled in" N times (used by the automatic window)
#    update   download / refresh the local history only
#
# History comes from the public NY Open Data (Socrata) feeds, which carry
# the multi-state PowerBall and Mega Millions results, and is cached in
# ~/.lottery_picker so it is only downloaded when it is stale.
#
# Only draws made under the CURRENT game matrix are used, because older
# matrices had different number pools (see GAMES below).
#
# Examples:
#    python lottery_picker.py
#    python lottery_picker.py freq --game powerball --sort count
#    python lottery_picker.py pick --draws 150
#    python lottery_picker.py pick --since 2025-01-01
#    python lottery_picker.py pick --min-times 5 --coverage 0.9
#    python lottery_picker.py window
#
# NOTE: lottery draws are independent and random.  No selection method
# changes the odds of winning; this is for entertainment only.

import argparse
import csv
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

BASE_URL = "https://data.ny.gov/resource/"
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".lottery_picker")
MIN_WINDOW = 20                 # never use fewer draws than this

# era_start is the first draw under the current matrix.  Change it here
# if the games change their rules again.
GAMES = {
   "powerball": {
      "title": "PowerBall",
      "dataset": "d6yy-54nr",
      "white_max": 69,
      "bonus_max": 26,
      "bonus_name": "PowerBall",
      "era_start": "2015-10-07",
   },
   "megamillions": {
      "title": "Mega Millions",
      "dataset": "5xaw-6ayf",
      "white_max": 70,
      "bonus_max": 24,
      "bonus_name": "Mega Ball",
      "era_start": "2025-04-08",
   },
}


# ---------------------------------------------------------------------
# Data retrieval and local storage
# ---------------------------------------------------------------------

# Convert one Socrata row into {"date", "white", "bonus"} or None.
def parse_row(row):
   try:
      day = str(row["draw_date"])[:10]
      nums = [int(x) for x in str(row["winning_numbers"]).split()]
      if row.get("mega_ball") not in (None, ""):
         bonus = int(row["mega_ball"])
      else:
         bonus = nums.pop()     # last value is the bonus ball
      if len(nums) != 5:
         return None
      return {"date": day, "white": sorted(nums), "bonus": bonus}
   except (KeyError, ValueError, IndexError):
      return None


# Download rows drawn on or after "since" (YYYY-MM-DD).
def fetch_rows(cfg, since):
   query = urllib.parse.urlencode({
      "$where": "draw_date >= '%sT00:00:00'" % since,
      "$order": "draw_date ASC",
      "$limit": 50000,
   })
   url = "%s%s.json?%s" % (BASE_URL, cfg["dataset"], query)
   req = urllib.request.Request(
      url, headers={"User-Agent": "lottery-picker/1.0"})
   with urllib.request.urlopen(req, timeout=30) as resp:
      return json.load(resp)


def load_cache(path):
   try:
      with open(path, "r", encoding="utf-8") as fh:
         return json.load(fh)
   except (OSError, ValueError):
      return []


def save_cache(path, draws):
   os.makedirs(os.path.dirname(path), exist_ok=True)
   tmp = path + ".tmp"
   with open(tmp, "w", encoding="utf-8") as fh:
      json.dump(draws, fh)
   os.replace(tmp, path)


# Return the usable draws for a game, newest first.  The cache is
# refreshed (incrementally) when it is older than max_age hours.
def get_draws(key, args):
   cfg = GAMES[key]
   path = os.path.join(args.data_dir, key + ".json")
   cached = [] if args.refresh else load_cache(path)
   age_hrs = 1e9
   if cached and os.path.exists(path):
      age_hrs = (time.time() - os.path.getmtime(path)) / 3600.0
   if not args.offline and (not cached or age_hrs > args.max_age):
      since = cfg["era_start"]
      if cached:
         since = max(since, cached[-1]["date"])
      try:
         merged = {d["date"]: d for d in cached}
         for row in fetch_rows(cfg, since):
            draw = parse_row(row)
            if draw:
               merged[draw["date"]] = draw
         cached = [merged[k] for k in sorted(merged)]
         save_cache(path, cached)
      except (OSError, ValueError) as exc:
         if not cached:
            sys.exit("Cannot download %s history (%s) and there is no "
                     "local copy.  Check your connection." %
                     (cfg["title"], exc))
         print("WARNING: update failed (%s); using local copy." % exc,
               file=sys.stderr)
   good = [d for d in cached
           if d["date"] >= cfg["era_start"]
           and max(d["white"]) <= cfg["white_max"]
           and min(d["white"]) >= 1
           and 1 <= d["bonus"] <= cfg["bonus_max"]]
   good.sort(key=lambda d: d["date"], reverse=True)
   return good


# ---------------------------------------------------------------------
# Window selection (how far back to look)
# ---------------------------------------------------------------------

# Fraction of numbers 1..pool_max that have been drawn >= times.
def filled(counts, pool_max, times):
   hit = sum(1 for n in range(1, pool_max + 1) if counts[n] >= times)
   return hit / float(pool_max)


# Smallest number of recent draws in which at least "coverage" of the
# white balls AND of the bonus balls have each been drawn >= times.
# Returns None if the history never gets there.
def draws_needed(draws, cfg, times, coverage):
   wc = [0] * (cfg["white_max"] + 1)
   bc = [0] * (cfg["bonus_max"] + 1)
   for i, d in enumerate(draws, 1):
      for w in d["white"]:
         wc[w] += 1
      bc[d["bonus"]] += 1
      if (filled(wc, cfg["white_max"], times) >= coverage and
          filled(bc, cfg["bonus_max"], times) >= coverage):
         return i
   return None


# Pick the slice of draws to analyze.  Returns (draws, description).
def select_window(draws, cfg, args):
   if args.draws:
      sub = draws[:args.draws]
      why = "last %d draws (user specified)" % len(sub)
   elif args.since:
      sub = [d for d in draws if d["date"] >= args.since]
      why = "draws since %s (user specified)" % args.since
   elif args.years:
      cutoff = date.today() - timedelta(days=int(365.25 * args.years))
      sub = [d for d in draws if d["date"] >= cutoff.isoformat()]
      why = "last %g years (user specified)" % args.years
   elif args.all:
      sub = draws
      why = "entire history under the current game matrix"
   else:
      need = draws_needed(draws, cfg, args.min_times, args.coverage)
      if need is None:
         sub = draws
         why = ("auto: history too short for %d%% of numbers to hit %d "
                "time(s); using all of it" %
                (round(args.coverage * 100), args.min_times))
      else:
         need = max(need, MIN_WINDOW)
         sub = draws[:need]
         why = ("auto: %d%% of all numbers drawn at least %d time(s)" %
                (round(args.coverage * 100), args.min_times))
   if len(sub) < MIN_WINDOW:
      sub = draws[:MIN_WINDOW]
      why += "; widened to the minimum of %d draws" % MIN_WINDOW
   return sub, why


# ---------------------------------------------------------------------
# Statistics and scoring
# ---------------------------------------------------------------------

# Per-number statistics over a list of draws (newest first).
# bonus=False analyzes the five white balls, True the bonus ball.
def pool_stats(draws, pool_max, bonus, halflife):
   n = len(draws)
   rows = {}
   for num in range(1, pool_max + 1):
      rows[num] = {"num": num, "count": 0, "gap": n, "recency": 0.0,
                   "last_date": None}
   for age, d in enumerate(draws):
      weight = 0.5 ** (age / float(halflife))
      for num in ([d["bonus"]] if bonus else d["white"]):
         r = rows[num]
         r["count"] += 1
         r["recency"] += weight
         if r["last_date"] is None:
            r["last_date"] = d["date"]
            r["gap"] = age     # draws since it was last drawn
   return [rows[k] for k in sorted(rows)]


# Scale values to 0..1.
def minmax(vals):
   lo, hi = min(vals), max(vals)
   if hi == lo:
      return [0.5] * len(vals)
   return [(v - lo) / float(hi - lo) for v in vals]


# Adds "hot" and "cold" scores (0..1) to each row.
#    hot  = mix of how OFTEN and how RECENTLY a number was drawn
#    cold = mix of how SELDOM and how LONG AGO a number was drawn
# mix is the weight given to frequency (the rest goes to recency/gap).
def add_scores(rows, mix):
   cnt = minmax([r["count"] for r in rows])
   rec = minmax([r["recency"] for r in rows])
   gap = minmax([r["gap"] for r in rows])
   for i, r in enumerate(rows):
      r["hot"] = mix * cnt[i] + (1 - mix) * rec[i]
      r["cold"] = mix * (1 - cnt[i]) + (1 - mix) * gap[i]


# ---------------------------------------------------------------------
# Ticket generation
# ---------------------------------------------------------------------

# Weighted random choice of k distinct numbers; higher score = likelier.
def sample(rows, key, k, rng, sharp):
   pool = list(rows)
   chosen = []
   for _ in range(k):
      wts = [(r[key] + 0.02) ** sharp for r in pool]
      pick = rng.choices(pool, weights=wts)[0]
      chosen.append(pick["num"])
      pool.remove(pick)
   return sorted(chosen)


# Build "count" distinct tickets for one group (key = "hot"/"cold").
# Ticket 1 is the pure top-ranked set; the others are weighted random
# draws favoring high-ranked numbers, with the bonus ball varied.
def make_tickets(white_rows, bonus_rows, key, count, rng, sharp):
   rank_w = sorted(white_rows, key=lambda r: (-r[key], r["num"]))
   rank_b = sorted(bonus_rows, key=lambda r: (-r[key], r["num"]))
   first = (tuple(sorted(r["num"] for r in rank_w[:5])), rank_b[0]["num"])
   tickets = [first]
   used = {first[1]}
   tries = 0
   while len(tickets) < count and tries < 20000:
      tries += 1
      whites = tuple(sample(white_rows, key, 5, rng, sharp))
      avail = [r for r in bonus_rows if r["num"] not in used]
      bonus = sample(avail or bonus_rows, key, 1, rng, sharp)[0]
      if (whites, bonus) not in tickets:
         tickets.append((whites, bonus))
         used.add(bonus)
   return tickets


# ---------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------

def show_tickets(label, blurb, tickets, bonus_name):
   print("\n  %s - %s" % (label, blurb))
   for i, (whites, bonus) in enumerate(tickets, 1):
      print("    %d)  %s   %s %02d" % (
         i, " ".join("%02d" % w for w in whites), bonus_name, bonus))


# Frequency table for one pool of numbers.
def show_freq(title, rows, n_draws, per_draw, sort):
   if sort == "count":
      rows = sorted(rows, key=lambda r: (-r["count"], r["num"]))
   elif sort == "gap":
      rows = sorted(rows, key=lambda r: (-r["gap"], r["num"]))
   top = max(r["count"] for r in rows) or 1
   exp = per_draw * n_draws
   print("\n  %s  (expected per number: %.1f)" % (title, exp))
   print("    Num  Count    Pct  vsExp  Last drawn  DrawsAgo")
   for r in rows:
      bar = "#" * int(round(30.0 * r["count"] / top))
      print("    %02d   %5d  %5.1f%%  %+5.1f  %-10s  %8d  %s" % (
         r["num"], r["count"], 100.0 * r["count"] / n_draws,
         r["count"] - exp, r["last_date"] or "never", r["gap"], bar))


def write_csv(folder, key, pool, rows):
   os.makedirs(folder, exist_ok=True)
   path = os.path.join(folder, "%s_%s.csv" % (key, pool))
   with open(path, "w", newline="", encoding="utf-8") as fh:
      out = csv.writer(fh)
      out.writerow(["number", "count", "draws_since_last", "last_drawn",
                    "recency_weight", "hot_score", "cold_score"])
      for r in rows:
         out.writerow([r["num"], r["count"], r["gap"], r["last_date"],
                       round(r["recency"], 4), round(r["hot"], 4),
                       round(r["cold"], 4)])
   print("  wrote %s" % path)


# Table of draws needed for N hits per number (the automatic window).
def show_window_table(draws, cfg, args):
   print("\n  Draws needed until %d%% of numbers (white and %s) have each"
         " been drawn N times:" %
         (round(args.coverage * 100), cfg["bonus_name"]))
   print("    N   Draws  Back to")
   for times in range(1, 11):
      need = draws_needed(draws, cfg, times, args.coverage)
      if need is None:
         print("    %-3d never reached in available history" % times)
         break
      mark = "   <-- current setting" if times == args.min_times else ""
      print("    %-3d %5d  %s%s" % (times, need, draws[need - 1]["date"],
                                    mark))


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def parse_args(argv):
   ap = argparse.ArgumentParser(
      description="Pick PowerBall / Mega Millions numbers from history.")
   ap.add_argument("command", nargs="?", default="pick",
                   choices=["pick", "freq", "window", "update"])
   ap.add_argument("--game", default="both",
                   choices=["both", "powerball", "megamillions"])
   grp = ap.add_argument_group("how far back to look (default: auto)")
   grp.add_argument("--draws", type=int, metavar="N",
                    help="use only the last N draws")
   grp.add_argument("--since", metavar="YYYY-MM-DD",
                    help="use only draws on/after this date")
   grp.add_argument("--years", type=float, metavar="Y",
                    help="use only the last Y years")
   grp.add_argument("--all", action="store_true",
                    help="use all draws under the current matrix")
   grp.add_argument("--min-times", type=int, default=3, metavar="N",
                    help="auto window: each number drawn >= N times "
                         "(default 3)")
   grp.add_argument("--coverage", type=float, default=0.95, metavar="F",
                    help="auto window: fraction of numbers that must "
                         "reach N (default 0.95)")
   ap.add_argument("--tickets", type=int, default=5,
                   help="tickets per group (default 5)")
   ap.add_argument("--mix", type=float, default=0.5,
                   help="weight of frequency vs recency, 0..1 "
                        "(default 0.5)")
   ap.add_argument("--halflife", type=int, metavar="DRAWS",
                   help="recency half-life in draws (default window/4)")
   ap.add_argument("--sharpness", type=float, default=2.0,
                   help="how strongly picks favor top-ranked numbers "
                        "(default 2)")
   ap.add_argument("--seed", type=int, help="random seed (repeatable)")
   ap.add_argument("--sort", default="number",
                   choices=["number", "count", "gap"],
                   help="freq table order")
   ap.add_argument("--csv", metavar="DIR",
                   help="also write the statistics as CSV files")
   ap.add_argument("--data-dir", default=DEFAULT_DIR)
   ap.add_argument("--max-age", type=float, default=12.0,
                   help="hours before the local history is refreshed")
   ap.add_argument("--refresh", action="store_true",
                   help="discard the local copy and re-download")
   ap.add_argument("--offline", action="store_true",
                   help="never touch the network")
   args = ap.parse_args(argv)
   if not 0 <= args.mix <= 1:
      ap.error("--mix must be between 0 and 1")
   if not 0 < args.coverage <= 1:
      ap.error("--coverage must be in (0, 1]")
   return args


def main(argv=None):
   args = parse_args(argv)
   rng = random.Random(args.seed)
   keys = list(GAMES) if args.game == "both" else [args.game]
   for key in keys:
      cfg = GAMES[key]
      draws = get_draws(key, args)
      print("\n" + "=" * 66)
      print(cfg["title"].upper())
      print("=" * 66)
      if not draws:
         print("  No draws available.")
         continue
      print("  Draws on file : %d  (%s to %s, current matrix %d/%d + "
            "%d/%d)" % (len(draws), draws[-1]["date"], draws[0]["date"],
                        5, cfg["white_max"], 1, cfg["bonus_max"]))
      if args.command == "update":
         continue
      if args.command == "window":
         show_window_table(draws, cfg, args)
         continue
      win, why = select_window(draws, cfg, args)
      n = len(win)
      half = args.halflife or max(10, n // 4)
      print("  Window used   : %d draws, %s to %s" % (
         n, win[-1]["date"], win[0]["date"]))
      print("                  %s" % why)
      white = pool_stats(win, cfg["white_max"], False, half)
      bonus = pool_stats(win, cfg["bonus_max"], True, half)
      add_scores(white, args.mix)
      add_scores(bonus, args.mix)
      if args.csv:
         write_csv(args.csv, key, "white", white)
         write_csv(args.csv, key, "bonus", bonus)
      if args.command == "freq":
         show_freq("White balls 1-%d" % cfg["white_max"], white, n,
                   5.0 / cfg["white_max"], args.sort)
         show_freq("%s 1-%d" % (cfg["bonus_name"], cfg["bonus_max"]),
                   bonus, n, 1.0 / cfg["bonus_max"], args.sort)
         continue
      for label, blurb, sk in (
            ("HOT", "drawn most often / most recently", "hot"),
            ("COLD", "drawn least often / longest ago", "cold")):
         tix = make_tickets(white, bonus, sk, args.tickets, rng,
                            args.sharpness)
         show_tickets(label, blurb, tix, cfg["bonus_name"])
   if args.command == "pick":
      print("\nReminder: every combination is equally likely to win; "
            "these picks\nare for fun and do not improve your odds.")


if __name__ == "__main__":
   main()
