lottery_picker - PowerBall and Mega Millions number picker
==========================================================

Picks numbers for the PowerBall and Mega Millions drawings sold in
Pennsylvania, based on how often, and how recently, each number has
been drawn. It downloads the draw history itself and keeps a local
copy. Python 3.6 or later, no extra packages.

Every combination has exactly the same chance of winning. Nothing
here improves the odds; it is for fun.

Run
---
   python lottery_picker.py                    HOT and COLD tickets, both games
   python lottery_picker.py freq               frequency tables
   python lottery_picker.py freq --game powerball --sort count
   python lottery_picker.py window             how the auto window is chosen
   python lottery_picker.py update             refresh the local history only
   python lottery_picker.py pick --draws 150   look back 150 draws only
   python lottery_picker.py pick --seed 7      repeatable picks
   python lottery_picker.py -h                 all options

What it picks
-------------
For each game it makes two groups of tickets, 5 tickets per group
(--tickets changes that). A ticket is 5 white balls plus the bonus
ball (PowerBall or Mega Ball).

   HOT    numbers drawn most often and/or most recently
   COLD   numbers drawn least often and/or not drawn for the longest

Each number gets a score from 0 to 1 for each group, mixing how often
and how recently it was drawn (--mix sets the balance, default 0.5;
--halflife sets how fast old draws fade). Ticket 1 in a group is the
top-ranked set. Tickets 2 to 5 are weighted random picks that favor
high-ranked numbers (--sharpness), with the bonus ball varied.

How far back it looks
---------------------
By default the code decides. It walks back from the latest draw until
at least 95% of all numbers, white and bonus, have each been drawn at
least 3 times (--coverage and --min-times). "window" shows how many
draws that takes for N = 1 to 10. You can set the window yourself:

   --draws N          the last N draws
   --since YYYY-MM-DD draws on or after a date
   --years Y          the last Y years
   --all              everything under the current game matrix

The window is never smaller than 20 draws.

Frequency report
----------------
"freq" lists every number with its count, percent of draws, how far
that is from the expected count, the date it was last drawn, how many
draws ago that was, and a bar. --sort number|count|gap changes the
order. --csv DIR also writes the statistics (including the hot and
cold scores) to CSV files.

Data
----
   Source    NY Open Data (data.ny.gov) feeds for PowerBall and Mega
             Millions. They carry the national results, which are the
             same draws Pennsylvania sells.
   Stored    ~/.lottery_picker/powerball.json and megamillions.json
             (--data-dir changes the folder)
   Refresh   only when the local copy is more than 12 hours old
             (--max-age), and only the new draws are fetched.
             --refresh re-downloads everything, --offline never
             touches the network.

Only draws under the current game matrix are used, because older
matrices had different number pools:

   PowerBall      5 of 69 + 1 of 26   since 2015-10-07
   Mega Millions  5 of 70 + 1 of 24   since 2025-04-08

If the games change again, edit GAMES at the top of the script and
use --refresh. Check the dates against palottery.pa.gov.

Notes
-----
   Mega Millions has a short history under its current matrix, so a
   high --min-times (4 or more) may never be reached and the whole
   history is used.
   The latest draws can lag a day or two in the feed.
