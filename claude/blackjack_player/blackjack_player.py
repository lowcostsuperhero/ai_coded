#!/usr/bin/python
# name: $Id$
# use: plays blackjack as a player using perfect basic strategy for
#      4-8 decks, dealer stands on soft 17, double after split,
#      late surrender, blackjack pays 3:2; each press of Proceed makes
#      one move and explains the strategy chart rule behind it
# usage: python blackjack_player.py [-f] [-h]
#        the window starts in the background and the terminal gets
#        its prompt back; -f keeps it in the foreground
#        Proceed (or Enter) advances one step: deal, each player
#        decision, then the dealer's play and settlement
#        Reset restores the bankroll and reshuffles the shoe

import os
import random
import subprocess
import sys
import tkinter as tk
from tkinter import scrolledtext

E_SUCCESS = 0
E_WARN    = 1
E_FAIL    = 2
E_USAGE   = 3

SELF      = os.path.basename(sys.argv[0])
CHILD_ENV = "BLACKJACK_PLAYER_DETACHED"

DECKS          = 6
PENETRATION    = 0.75
MAX_HANDS      = 4
START_BANKROLL = 1000
DEFAULT_BET    = 10

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["♠", "♥", "♦", "♣"]

###########################################################################
###
### s t r a t e g y    c h a r t
###
###########################################################################

                               # columns are the dealer's up card:
                               #   2  3  4  5  6  7  8  9  10 A
                               # codes: H hit, S stand, D double else
                               # hit, Ds double else stand, P split,
                               # R surrender else hit
PAIR_CHART = {
    2: "P  P  P  P  P  P  H  H  H  H",
    3: "P  P  P  P  P  P  H  H  H  H",
    4: "H  H  H  P  P  H  H  H  H  H",
    5: "D  D  D  D  D  D  D  D  H  H",
    6: "P  P  P  P  P  H  H  H  H  H",
    7: "P  P  P  P  P  P  H  H  H  H",
    8: "P  P  P  P  P  P  P  P  P  P",
    9: "P  P  P  P  P  S  P  P  S  S",
   10: "S  S  S  S  S  S  S  S  S  S",
   11: "P  P  P  P  P  P  P  P  P  P",
}

                               # keyed by soft total: A-2 is soft 13;
                               # soft 12 only occurs as a multi-card
                               # hand (or A-A that cannot be split)
SOFT_CHART = {
   12: "H  H  H  H  H  H  H  H  H  H",
   13: "H  H  H  D  D  H  H  H  H  H",
   14: "H  H  H  D  D  H  H  H  H  H",
   15: "H  H  D  D  D  H  H  H  H  H",
   16: "H  H  D  D  D  H  H  H  H  H",
   17: "H  D  D  D  D  H  H  H  H  H",
   18: "S  Ds Ds Ds Ds S  S  H  H  H",
   19: "S  S  S  S  S  S  S  S  S  S",
   20: "S  S  S  S  S  S  S  S  S  S",
   21: "S  S  S  S  S  S  S  S  S  S",
}

HARD_CHART = {
    9: "H  D  D  D  D  H  H  H  H  H",
   10: "D  D  D  D  D  D  D  D  H  H",
   11: "D  D  D  D  D  D  D  D  D  H",
   12: "H  H  S  S  S  H  H  H  H  H",
   13: "S  S  S  S  S  H  H  H  H  H",
   14: "S  S  S  S  S  H  H  H  H  H",
   15: "S  S  S  S  S  H  H  H  R  H",
   16: "S  S  S  S  S  H  H  R  R  R",
}
for t in range(4, 9):
   HARD_CHART[t] = "H  " * 10
for t in range(17, 22):
   HARD_CHART[t] = "S  " * 10

                               # same labels as the .ods chart
CODE_TEXT = {
   "H":  "Hit",
   "S":  "Stand",
   "D":  "DD (double if allowed, otherwise hit)",
   "Ds": "DD/St (double if allowed, otherwise stand)",
   "P":  "Split",
   "R":  "Sur/Hit (surrender if allowed, otherwise hit)",
}

ACTION_TEXT = {
   "hit":       "HIT",
   "stand":     "STAND",
   "double":    "DOUBLE DOWN",
   "split":     "SPLIT",
   "surrender": "SURRENDER",
}

###########################################################################

def up_name(up):
   """
   use: printable name of a dealer up card value
   post: "A" for 11, otherwise the number as a string
   """
   return("A" if (up == 11) else str(up))

###########################################################################

def article(up):
   """
   post: the up card with "a" or "an" in front: "an A", "an 8", "a 7"
   """
   return(("an " if (up in (8, 11)) else "a ") + up_name(up))

###########################################################################

def chart_code(chart, key, up):
   """
   use: looks up one cell of a strategy chart
   pre: up is 2..11 (11 = ace)
   """
   return(chart[key].split()[up - 2])

###########################################################################
###
### c a r d s    a n d    h a n d s
###
###########################################################################

class Card:
   """
   name: Card
   use: one playing card
   """
   def __init__(self, rank, suit):
      self.rank = rank
      self.suit = suit

   ########################################################################

   @property
   def value(self):
      """
      post: ace is 11, face cards are 10
      """
      if (self.rank == "A"):
         return(11)
      if (self.rank in ("J", "Q", "K")):
         return(10)
      return(int(self.rank))

   ########################################################################

   def __str__(self):
      return(f"{self.rank}{self.suit}")

###########################################################################

class Shoe:
   """
   name: Shoe
   use: a multi-deck shoe that reshuffles at the cut card
   """
   def __init__(self, decks = DECKS):
      self.decks = decks
      self.shuffle()

   ########################################################################

   def shuffle(self):
      self.cards = [Card(r, s) for _ in range(self.decks)
                               for s in SUITS
                               for r in RANKS]
      random.shuffle(self.cards)
      self.size = len(self.cards)

   ########################################################################

   def needs_shuffle(self):
      return(len(self.cards) < self.size * (1 - PENETRATION))

   ########################################################################

   def draw(self):
      return(self.cards.pop())

###########################################################################

class Hand:
   """
   name: Hand
   use: one player (or dealer) hand and its bet
   """
   def __init__(self, bet = 0):
      self.cards       = []
      self.bet         = bet
      self.doubled     = False
      self.surrendered = False
      self.from_split  = False
      self.split_aces  = False
      self.done        = False
      self.result      = ""

   ########################################################################

   def total(self):
      """
      post: returns (total, soft); soft is True when an ace is still
            being counted as 11
      """
      total = sum(c.value for c in self.cards)
      aces  = sum(1 for c in self.cards if (c.rank == "A"))
      while (total > 21 and aces):
         total -= 10
         aces  -= 1
      return(total, aces > 0)

   ########################################################################

   def is_pair(self):
      return(len(self.cards) == 2 and
             self.cards[0].value == self.cards[1].value)

   ########################################################################

   def is_blackjack(self):
      return(len(self.cards) == 2 and not self.from_split and
             self.total()[0] == 21)

   ########################################################################

   def is_bust(self):
      return(self.total()[0] > 21)

   ########################################################################

   def describe(self):
      total, soft = self.total()
      cards       = " ".join(str(c) for c in self.cards)
      kind        = (     "blackjack" if (self.is_blackjack())
                     else "bust"      if (total > 21)
                     else f"soft {total}" if (soft)
                     else f"{total}")
      return(f"{cards}  = {kind}")

###########################################################################
###
### s t r a t e g y    r e a s o n s
###
###########################################################################

def pair_reason(v, up, code):
   """
   use: explains a pairs-chart decision
   pre: v is the value of each card in the pair (11 = aces)
   """
   u  = up_name(up)
   au = article(up)
   if (v == 11):
      return("Always split aces. Played together, A-A is only a soft "
             "12. Split, each ace starts a new hand at 11, where any "
             "10-value card (the most common rank) makes 21. Two "
             "strong starts are worth far more than one weak total.")
   if (v == 8):
      return("Always split 8s. Together they make 16, the worst total "
             "in blackjack: too high to hit safely and too low to win "
             "by standing. Each 8 on its own starts an average hand, "
             "so splitting loses less money even against a 9, 10 or A.")
   if (v == 10):
      return("Never split 10s. A hard 20 wins most of the time against "
             "every up card. Splitting trades a near-certain win for "
             "two hands that each start at only 10.")
   if (v == 5):
      return("Never split 5s. Two 5s are a hard 10, one of the best "
             "totals to double on, while two hands starting at 5 "
             "would each be weak. So 5-5 plays exactly like hard 10. "
             + hard_reason(10, up, code))
   if (v == 9):
      if (code == "S" and up == 7):
         return("Stand on 9-9 against a 7. Your 18 already beats the "
                "dealer's most likely finish of 17 (a 10 in the hole), "
                "so splitting would only risk more money.")
      if (code == "S"):
         return(f"Stand on 9-9 against {au}. The dealer is strong "
                "here and will often make 19-21. Two hands starting at "
                "9 would both face that, so keeping one 18 loses less.")
      if (up in (8, 9)):
         return(f"Split 9s against {au}. 18 only ties or loses to the "
                "dealer's likely 18 or 19, but each 9 can grow into "
                "19 with a 10-value card.")
      return(f"Split 9s against {au}. The dealer busts often with "
             "2-6, and two hands starting at 9 put twice the money on "
             "the table while he is weak.")
   if (v == 4):
      if (code == "P"):
         return(f"Split 4s against {au}. 5 and 6 are the dealer's "
                "worst cards. With double after split, a 4 that "
                "catches a 6 or 7 becomes a 10 or 11 you can double.")
      return(f"Don't split 4s against {au}; play them as hard 8 and "
             "hit. 8 can't bust with one card, and two hands starting "
             "at 4 only pay off when the dealer shows 5 or 6.")
   total = v * 2
   if (code == "P"):
      why = {2: "two hands that start at 2 grow into better totals "
                "than one hard 4",
             3: "two hands that start at 3 grow into better totals "
                "than one hard 6",
             6: "12 is a stiff hand that rarely wins on its own",
             7: "14 is a stiff hand, while each 7 can become 17-21"}[v]
      return(f"Split {v}s against {au}: {why}. The dealer's {u} is "
             "weak enough that putting out a second bet pays, and "
             "double after split adds value when a split hand "
             "catches a good card.")
   return(f"Don't split {v}s against {au}. The dealer's card is "
          "strong, so splitting just puts more money on two weak "
          f"hands. Play the pair as hard {total} and hit.")

###########################################################################

def soft_reason(total, up, code):
   """
   use: explains a soft-chart decision
   """
   u  = up_name(up)
   au = article(up)
   if (total >= 19):
      return(f"Always stand on soft {total}. It is already a strong "
             "total, and hitting rarely improves it.")
   if (total == 18):
      if (up in (3, 4, 5, 6)):
         return(f"Soft 18 against {au} is DD/St. The dealer is likely "
                "to bust, and an ace-7 can't bust on one card, so "
                "this is the time to double the bet. If you can't "
                "double (more than two cards), stand: 18 is good "
                "enough against a weak card.")
      if (up in (2, 7, 8)):
         return(f"Stand on soft 18 against {au}. 18 beats or ties the "
                "dealer's most likely finish (17 with a 7, 18 with an "
                "8). Against a 2, doubling isn't quite profitable.")
      return(f"Hit soft 18 against {au}. A 9, 10 or ace usually "
             "finishes 19-21, so 18 is an underdog. Because the ace "
             "can drop to 1, one hit can't bust you and often helps.")
   if (total == 12):
      return("Hit soft 12. It is a weak total, and with an ace that "
             "can count as 1, no card can bust you.")
   if (code == "D"):
      return(f"Soft {total} against {au} is DD. The dealer's card is "
             "weak and busts often, and a soft hand can't bust on one "
             "card, so it is worth doubling. The lower the soft total, "
             "the weaker the dealer card must be: A-2/A-3 double on "
             "5-6, A-4/A-5 on 4-6, A-6 on 3-6. If you can't double, "
             "hit.")
   return(f"Hit soft {total} against {au}. It's too weak to stand on, "
          "the ace means a hit can't bust you, and the dealer's card "
          "isn't weak enough to make doubling pay.")

###########################################################################

def hard_reason(total, up, code):
   """
   use: explains a hard-chart decision
   """
   u  = up_name(up)
   au = article(up)
   if (total <= 8):
      return(f"Always hit hard {total}. No card can bust you, and the "
             "total is too low to win by standing.")
   if (total == 9):
      if (code == "D"):
         return(f"Double 9 against {au}. The dealer's 3-6 are weak, "
                "and a 10-value card gives you 19.")
      return(f"Hit 9 against {au}. Doubling 9 only pays against the "
             "dealer's weak 3-6.")
   if (total == 10):
      if (code == "D"):
         return(f"Double 10 against {au}. A 10-value card, the most "
                "common rank, gives you 20, and your 10 beats the "
                "dealer's starting card.")
      return(f"Hit 10 against {au}. The dealer's card is as good as "
             "or better than yours, so don't double.")
   if (total == 11):
      if (code == "D"):
         return(f"Double 11 against {au}. 11 is the best total to "
                "double on: any 10-value card makes 21 and no card can "
                "bust you.")
      return("Hit 11 against an ace. When the dealer stands on soft "
             "17, the ace is strong enough that doubling 11 makes "
             "slightly less than hitting. (If the dealer hits soft "
             "17, double here.)")
   if (total == 12):
      if (code == "S"):
         return(f"Stand on 12 against {au}. The dealer's 4-6 bust "
                "most often. Only 10-value cards bust your 12, but "
                "there are a lot of them, so let the dealer take the "
                "risk.")
      if (up in (2, 3)):
         return(f"Hit 12 against {au}. A 2 or 3 busts less often "
                "than 4-6, and only 10-value cards can bust you, so "
                "hitting is the slightly better risk.")
   if (code == "R"):
      return(f"Surrender {total} against {au}. This hand loses so "
             "often, whether you hit or stand, that its expected loss "
             "is more than half the bet. Surrendering caps the loss "
             "at exactly half.")
   if (total <= 16):
      if (code == "S"):
         return(f"Stand on {total} against {au}. With a stiff hand "
                "(12-16) against a weak up card, let the dealer take "
                "the risk: he must hit 16 or less and busts often.")
      return(f"Hit {total} against {au}. The dealer will most likely "
             f"finish with 17 or better, so standing on {total} "
             "usually loses. Hitting risks a bust but gives the "
             "better chance.")
   return(f"Always stand on hard {total}. The chance of busting with "
          "a hit is too high.")

def bust_ranks(total):
   """
   post: how many of the 13 ranks bust a hard total with one card
         (an ace counts as 1)
   """
   return(sum(1 for r in RANKS
              if ((1 if (r == "A") else min(10, Card(r, "").value))
                  > 21 - total)))

###########################################################################

def wrong_reason(total, up, chosen, correct, code, v):
   """
   use: explains why the move the user picked loses money compared
        with the chart's move
   pre: total is the hand's (total, soft) before the move; v is the
        value of its first card (used for pairs)
   """
   t, soft = total
   u       = up_name(up)
   au      = article(up)
   desc    = f"soft {t}" if (soft) else f"{t}"
   weak    = up in (2, 3, 4, 5, 6)
   if (chosen == "surrender"):
      fav = (     ", and here you are actually the favourite"
             if (correct == "double" or
                 (correct == "stand" and t >= 19))
             else "")
      return(f"Surrender gives up half the bet for certain. Played "
             f"out, {desc} against {au} loses less than that on "
             f"average{fav}, so surrendering throws money away.")
   if (chosen == "split"):
      if (v == 10):
         return("Splitting 10s breaks up a 20, which wins most of the "
                "time, into two hands that start at only 10.")
      if (v == 5):
         return("Splitting 5s turns a hard 10, a great total to double "
                "on, into two weak hands that start at 5.")
      if (v == 9):
         if (up == 7):
            return("18 already beats the dealer's most likely finish "
                   "of 17, so splitting only puts more money at risk.")
         return(f"Against {au} the dealer will often make 19-21; two "
                "hands starting at 9 would both face that, so one 18 "
                "loses less.")
      if (v == 4):
         return("Two hands starting at 4 are weak. Splitting 4s only "
                "pays against the dealer's 5 or 6.")
      return(f"The dealer's {u} is strong, so splitting just puts "
             "twice the money on two weak hands.")
   if (chosen == "double"):
      if (correct == "split"):
         return(f"Doubling plays the pair as one {desc} with only one "
                "more card. Splitting it is worth more here.")
      if (correct == "surrender"):
         return(f"{desc} against {au} loses more often than it wins. "
                "Doubling puts twice the money on it, with only one "
                "more card.")
      if (correct == "stand"):
         if (not soft and 12 <= t <= 16):
            return(f"Doubling {t} takes a card on twice the bet, and "
                   f"{bust_ranks(t)} of the 13 ranks bust you. Against "
                   f"a weak {u}, standing lets the dealer take that "
                   "risk instead.")
         return(f"Your {desc} is already good enough to stand on. "
                "Doubling forces one more card, which is as likely "
                "to hurt as help, on twice the money.")
      if (not soft and t <= 8):
         return(f"With {t}, one card can't make a strong total, and "
                "doubling stops you from taking more cards.")
      if (not soft and t >= 12):
         return(f"Doubling {t} risks busting with twice the money, and "
                "you can't take another card if you end up low.")
      return(f"Against {au}, {desc} isn't strong enough to make you "
             "the favourite, so the extra bet loses money. Hit, and "
             "keep the option of taking more cards.")
   if (chosen == "hit"):
      if (correct == "double"):
         return("Taking a card is the right idea, but you are the "
                "favourite here, so double: the extra bet earns money "
                "while you have the edge. You still get a card, just "
                "one.")
      if (correct == "split"):
         return(f"Played as one {desc}, the pair is weaker than two "
                "split hands. The chart splits it to start two better "
                "hands.")
      if (correct == "surrender"):
         return(f"Hitting {t} against {au} still loses more than half "
                "the bet on average. Surrendering loses exactly half.")
      if (soft):
         return(f"Soft {t} already beats or ties the dealer's likely "
                "finish. A hit is more likely to make it worse than "
                "better.")
      tail = (     f", and the dealer's {u} is weak: he must hit and "
                   "busts often, so let him take the risk"
              if (weak)
              else f", and {t} is already a strong total" if (t >= 17)
              else "")
      return(f"With {t}, {bust_ranks(t)} of the 13 ranks bust you"
             f"{tail}.")
                               # stand
   if (correct == "double"):
      if (code == "Ds"):
         return(f"Standing is the fallback when you can't double. "
                f"Doubling is allowed here, and against a weak {u} it "
                "earns more than standing.")
      return(f"{desc} can't bust with one card, and you are the "
             f"favourite against {au}. Standing gives that edge away; "
             "doubling gets more money on the table.")
   if (correct == "split"):
      return(f"Standing on {desc} wastes a pair that is worth more "
             "split.")
   if (correct == "surrender"):
      return(f"Standing on {t} against {au} loses more than half the "
             "bet on average. Surrendering loses exactly half.")
   if (not soft and t <= 11):
      return(f"No card can bust {t}, so standing gives up a free "
             "chance to improve. It only wins if the dealer busts.")
   if (soft):
      return(f"Soft {t} can't bust with one card, and it's too weak to "
             f"win by standing against {au}.")
   return(f"Standing on {t} only wins if the dealer busts, and with "
          f"{au} showing he usually makes 17 or better.")

###########################################################################
###
### g a m e
###
###########################################################################

class Game:
   """
   name: Game
   use: blackjack round state machine; one call to step() makes one
        move and returns (headline, explanation)
   imp: phases are "deal" -> "play" -> "dealer" -> back to "deal"
   """
   def __init__(self):
      self.reset()

   ########################################################################

   def reset(self):
      self.shoe     = Shoe()
      self.bankroll = START_BANKROLL
      self.bet      = DEFAULT_BET
      self.rounds   = 0
      self.hands    = []
      self.dealer   = Hand()
      self.current  = 0
      self.phase    = "deal"
      self.hole_up  = False
      self.fresh    = False

   ########################################################################

   def step(self, chosen = None):
      """
      usage: chosen is a move picked by the user during play; None
             lets the strategy decide
      """
      if (self.phase == "deal"):
         return(self.deal())
      if (self.phase == "play"):
         return(self.play(chosen))
      return(self.dealer_play())

   ########################################################################

   def up(self):
      return(self.dealer.cards[0].value)

   ########################################################################

   def deal(self):
      """
      use: deals a new round, handles insurance, the dealer's peek
           and blackjacks
      """
      if (self.bankroll < self.bet):
         return("Out of money",
                f"The bankroll (${self.bankroll}) won't cover a "
                f"${self.bet} bet. Lower the bet or press Reset.")
      lines = []
      if (self.shoe.needs_shuffle()):
         self.shoe.shuffle()
         lines.append("The cut card came out; the shoe was reshuffled.")
      self.rounds  += 1
      self.hands    = [Hand(self.bet)]
      self.dealer   = Hand()
      self.current  = 0
      self.hole_up  = False
      for _ in range(2):
         self.hands[0].cards.append(self.shoe.draw())
         self.dealer.cards.append(self.shoe.draw())
      hand = self.hands[0]
      lines[0:0] = [f"Round {self.rounds}: bet ${self.bet}.",
                    f"Player has {hand.describe()}.",
                    f"Dealer shows {self.dealer.cards[0]}."]
      if (self.up() == 11):
         lines.append("Insurance offered: DECLINED. Never take "
                      "insurance or even money. It is a side bet that "
                      "the hole card is a 10-value card, which happens "
                      "less than 1/3 of the time, but it pays only "
                      "2:1, so it loses money in the long run.")
      if (self.up() >= 10):
         if (self.dealer.is_blackjack()):
            self.hole_up = True
            self.phase   = "deal"
            if (hand.is_blackjack()):
               hand.result = "PUSH"
               lines.append("Dealer peeks and has blackjack.")
               lines.append("**Push: both have blackjack.**")
            else:
               self.bankroll -= hand.bet
               hand.result    = f"LOSE -${hand.bet}"
               lines.append("Dealer peeks and has blackjack.")
               lines.append("**-Dealer blackjack.** No decision was "
                            "possible.")
            lines.append(self.net_line(-hand.bet
                                       if (not hand.is_blackjack())
                                       else 0))
            return("Dealer blackjack", "\n".join(lines))
         lines.append("Dealer peeks for blackjack: none.")
      if (hand.is_blackjack()):
         win            = hand.bet * 3 // 2
         self.bankroll += win
         self.hole_up   = True
         self.phase     = "deal"
         hand.result    = f"BLACKJACK +${win}"
         lines.append("**+Blackjack! It pays 3:2.**")
         lines.append(self.net_line(win))
         return("Blackjack", "\n".join(lines))
      self.phase = "play"
                               # no decision made yet this round
      self.fresh = True
      return("Deal", "\n".join(lines))

   ########################################################################

   def lookup(self, hand, can_split):
      """
      use: finds the chart cell for a hand
      post: returns (section, row label, code, reason)
      """
      up          = self.up()
      total, soft = hand.total()
      if (hand.is_pair() and can_split):
         v     = hand.cards[0].value
         label = "A-A" if (v == 11) else f"{v}-{v}"
         code  = chart_code(PAIR_CHART, v, up)
         return("Pairs", label, code, pair_reason(v, up, code))
      if (soft):
         label = (     f"A-{total - 11} (soft {total})"
                       if (len(hand.cards) == 2)
                  else f"soft {total}")
         code  = chart_code(SOFT_CHART, total, up)
         return("Soft hands", label, code,
                soft_reason(total, up, code))
      code = chart_code(HARD_CHART, total, up)
      return("Hard totals", f"{total} points", code,
             hard_reason(total, up, code))

   ########################################################################

   def decide(self):
      """
      use: works out the chart's move for the current hand without
           making it
      pre: phase is "play"
      post: returns a dict: section, label, code, reason, action (the
            chart's move after any fallback), note, allowed (the set
            of moves the rules permit right now) and row (the chart
            row, e.g. ("pair", 8), ("soft", 18), ("hard", 16))
      """
      hand  = self.hands[self.current]
      first = len(hand.cards) == 2
                               # aces may not be re-split; up to
                               # MAX_HANDS hands otherwise
      can_split     = (hand.is_pair() and not hand.split_aces and
                       len(self.hands) < MAX_HANDS)
      can_double    = first and not hand.split_aces
      can_surrender = first and not hand.from_split
      section, label, code, reason = self.lookup(hand, can_split)

      note = ""
      if (code == "D"):
         action = "double" if (can_double) else "hit"
      elif (code == "Ds"):
         action = "double" if (can_double) else "stand"
      elif (code == "R"):
         action = "surrender" if (can_surrender) else "hit"
      else:
         action = {"H": "hit", "S": "stand", "P": "split"}[code]
      if (code in ("D", "Ds") and not can_double):
         note = ("Doubling is only allowed on the first two cards, so "
                 f"the fallback applies: {ACTION_TEXT[action]}.")
      if (code == "R" and not can_surrender):
         note = ("Surrender is only allowed on the first two cards of "
                 "an unsplit hand, so the fallback applies: HIT.")
      if (hand.is_pair() and not can_split and
          not hand.split_aces and len(self.hands) >= MAX_HANDS):
         note = (f"Already at the {MAX_HANDS}-hand split limit, so the "
                 "pair is played by its total.")
      allowed = {"hit", "stand"}
      if (can_double):
         allowed.add("double")
      if (can_split):
         allowed.add("split")
      if (can_surrender):
         allowed.add("surrender")
      total = hand.total()[0]
      row   = {"Pairs":      ("pair", hand.cards[0].value),
               "Soft hands": ("soft", total),
               "Hard totals": ("hard", total)}[section]
      return({"section": section, "label": label, "code":  code,
              "reason":  reason,  "action": action, "note": note,
              "allowed": allowed, "row":    row})

   ########################################################################

   def play(self, chosen = None):
      """
      use: makes one move for the current hand and explains it
      usage: chosen is the move picked with a move button; None (the
             Proceed button) makes the chart's move
      pre: chosen, when given, is in decide()["allowed"]
      """
      self.fresh = False
      hand    = self.hands[self.current]
      d       = self.decide()
      correct = d["action"]
      action  = chosen or correct
      up      = self.up()
      before  = hand.describe()
      total   = hand.total()
      first_value = hand.cards[0].value
      result  = self.execute(hand, action)
      n       = (f"Hand {self.current + 1}"
                 if (len(self.hands) > 1) else "Player")
      rule    = (f"Chart rule: player {d['label']} vs dealer "
                 f"{up_name(up)} -> {CODE_TEXT[d['code']]}.")
      body    = [f"{n} had {before} against the dealer's "
                 f"{up_name(up)}.",
                 f"Action: {ACTION_TEXT[action]}. {result}"]
      if (action == correct):
         head = f"{n}: {ACTION_TEXT[action]}"
         body += [rule, f"Why: {d['reason']}"]
      else:
         head = (f"{n}: {ACTION_TEXT[action]} (chart says "
                 f"{ACTION_TEXT[correct]})")
         body += [f"**Wrong move: the chart says "
                  f"{ACTION_TEXT[correct]}.**",
                  rule,
                  f"Why {ACTION_TEXT[action]} is wrong: "
                  + wrong_reason(total, up, action, correct,
                                 d["code"], first_value),
                  f"Why the chart plays it this way: {d['reason']}"]
      if (d["note"]):
         body.append(f"Note: {d['note']}")
      self.advance()
      return(head, "\n".join(body))

   ########################################################################

   def execute(self, hand, action):
      """
      use: carries out an action on a hand
      post: returns a sentence describing what happened
      """
      if (action == "stand"):
         hand.done = True
         return("The hand is complete.")
      if (action == "surrender"):
         hand.surrendered = True
         hand.done        = True
         return(f"Half the bet (${hand.bet // 2}) is returned; the "
                "hand is over.")
      if (action == "double"):
         hand.bet     *= 2
         hand.doubled  = True
         hand.cards.append(self.shoe.draw())
         hand.done     = True
         return(f"Bet doubled to ${hand.bet}; one card dealt: "
                f"{hand.describe()}.")
      if (action == "split"):
         new            = Hand(hand.bet)
         new.cards      = [hand.cards.pop()]
         new.from_split = hand.from_split = True
         self.hands.insert(self.current + 1, new)
         aces = hand.cards[0].rank == "A"
         for h in (hand, new):
            h.cards.append(self.shoe.draw())
            if (aces):
               h.split_aces = True
               h.done       = True
         text = (f"Two hands, ${hand.bet} each: {hand.describe()} | "
                 f"{new.describe()}.")
         if (aces):
            text += (" Split aces get one card each and stand "
                     "(house rule).")
         return(text)
                               # hit
      hand.cards.append(self.shoe.draw())
      total = hand.total()[0]
      if (total > 21):
         hand.done   = True
         hand.result = "BUST"
         return(f"Drew {hand.cards[-1]}: {hand.describe()}. The hand "
                f"loses ${hand.bet}.")
      if (total == 21):
         hand.done = True
         return(f"Drew {hand.cards[-1]}: {hand.describe()}. 21; the "
                "hand stands.")
      return(f"Drew {hand.cards[-1]}: {hand.describe()}.")

   ########################################################################

   def advance(self):
      while (self.current < len(self.hands) and
             self.hands[self.current].done):
         self.current += 1
      if (self.current >= len(self.hands)):
         self.phase = "dealer"

   ########################################################################

   def net_line(self, net):
      """
      use: the round's money summary
      pre: the bankroll already includes net
      """
      sign = "+" if (net >= 0) else "-"
      return(f"Round: {sign}${abs(net)}; bankroll = "
             f"${self.bankroll}.")

   ########################################################################

   def dealer_play(self):
      """
      use: reveals the hole card, plays the dealer's fixed rules and
           settles every hand
      """
      self.hole_up = True
      lines        = [f"Dealer reveals {self.dealer.describe()}."]
      live         = [h for h in self.hands
                      if (not h.is_bust() and not h.surrendered)]
      if (live):
                               # S17: dealer stands on ALL 17s
         while (self.dealer.total()[0] < 17):
            self.dealer.cards.append(self.shoe.draw())
            lines.append(f"Dealer draws: {self.dealer.describe()}.")
         lines.append("The dealer has no choices: he hits 16 or less "
                      "and stands on every 17, including soft 17.")
      else:
         lines.append("Every player hand is already settled, so the "
                      "dealer doesn't draw.")
      dealer = self.dealer.total()[0]
      net    = 0
      for i, h in enumerate(self.hands):
         total = h.total()[0]
         if (h.surrendered):
            delta, how, word = -(h.bet // 2), "Surrendered", "SURRENDER"
         elif (total > 21):
            delta, how, word = -h.bet, "Bust", "BUST"
         elif (dealer > 21):
            delta, how, word = h.bet, "Dealer busts", "WIN"
         elif (total > dealer):
            delta, how, word = h.bet, f"{total} beats {dealer}", "WIN"
         elif (total < dealer):
            delta, how, word = -h.bet, f"{total} loses to {dealer}", "LOSE"
         else:
            delta, how, word = 0, f"Push at {total}", "PUSH"
         h.result = (     word if (delta == 0)
                     else f"{word} {'+' if (delta > 0) else '-'}"
                          f"${abs(delta)}")
         net += delta
                               # one hand: the outcome alone; split
                               # hands are named
         name = f"Hand {i + 1}: " if (len(self.hands) > 1) else ""
                               # a leading + or - inside ** colours
                               # the outcome (win green, loss red)
         sign = "+" if (delta > 0) else "-" if (delta < 0) else ""
         lines.append(f"{name}**{sign}{how}.**")
      self.bankroll += net
      self.phase     = "deal"
      lines.append(self.net_line(net))
      return("Dealer plays / settle", "\n".join(lines))

###########################################################################
###
### u s e r    i n t e r f a c e
###
###########################################################################

FIRST_MOVE       = "#dbf5dc"
FIRST_MOVE_HOVER = "#c6eec8"
CARD_W  = 80
CARD_H  = 114
FELT    = "#0f5a3c"
GOLD    = "#ffd76a"
RED     = "#c8102e"
BLACK   = "#111111"
                               # pip positions as fractions of the
                               # card's width and height
PIP_COLS = (0.32, 0.5, 0.68)
PIPS = {
    2: [(1, .18), (1, .82)],
    3: [(1, .18), (1, .5), (1, .82)],
    4: [(0, .18), (2, .18), (0, .82), (2, .82)],
    5: [(0, .18), (2, .18), (1, .5), (0, .82), (2, .82)],
    6: [(0, .18), (2, .18), (0, .5), (2, .5), (0, .82), (2, .82)],
    7: [(0, .18), (2, .18), (1, .34), (0, .5), (2, .5), (0, .82),
        (2, .82)],
    8: [(0, .18), (2, .18), (1, .34), (0, .5), (2, .5), (1, .66),
        (0, .82), (2, .82)],
    9: [(0, .18), (2, .18), (0, .39), (2, .39), (1, .5), (0, .61),
        (2, .61), (0, .82), (2, .82)],
   10: [(0, .18), (2, .18), (1, .29), (0, .39), (2, .39), (0, .61),
        (2, .61), (1, .71), (0, .82), (2, .82)],
}

###########################################################################

def round_rect(cv, x1, y1, x2, y2, r, **kw):
   """
   use: draws a rounded rectangle on a canvas
   imp: a smoothed polygon with doubled corner points
   """
   pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
          x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
          x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
   return(cv.create_polygon(pts, smooth = True, **kw))

###########################################################################

def font(size, s, weight = "bold"):
   """
   post: a Helvetica font scaled by s, never below 7 points
   """
   return(("Helvetica", max(7, int(size * s)), weight))

###########################################################################

def draw_card(cv, x, y, card, s = 1.0, hidden = False):
   """
   use: draws one card face (or its back) with its top-left at x, y
   pre: Tk 8.6 or later for the upside-down bottom corner
   """
   w, h = CARD_W * s, CARD_H * s
   round_rect(cv, x + 3, y + 3, x + w + 3, y + h + 3, 8 * s,
              fill = "#073222", outline = "")
   if (hidden):
      round_rect(cv, x, y, x + w, y + h, 8 * s, fill = "#ffffff",
                 outline = "#999999")
      round_rect(cv, x + 5 * s, y + 5 * s, x + w - 5 * s, y + h - 5 * s,
                 6 * s, fill = "#1d4e9e", outline = "#9fc1ff")
      for row in range(7):
         for col in range(4):
            cv.create_text(x + (16 + col * 16) * s,
                           y + (17 + row * 13.5) * s,
                           text = "◆", fill = "#5d8fe0",
                           font = font(8, s, "normal"))
      return
   color = RED if (card.suit in ("♥", "♦")) else BLACK
   round_rect(cv, x, y, x + w, y + h, 8 * s, fill = "#ffffff",
              outline = "#999999")
                               # corner index, top-left and rotated
                               # bottom-right
   for cx, cy, ang in ((x + 11 * s, y + 13 * s, 0),
                       (x + w - 11 * s, y + h - 13 * s, 180)):
      d = 15 * s if (ang == 0) else -15 * s
      cv.create_text(cx, cy, text = card.rank, fill = color,
                     font = font(12, s), angle = ang)
      cv.create_text(cx, cy + d, text = card.suit, fill = color,
                     font = font(11, s, "normal"), angle = ang)
   if (card.rank == "A"):
      cv.create_text(x + w / 2, y + h / 2, text = card.suit,
                     fill = color, font = font(40, s, "normal"))
   elif (card.rank in ("J", "Q", "K")):
      round_rect(cv, x + 20 * s, y + 20 * s, x + w - 20 * s,
                 y + h - 20 * s, 4 * s, fill = "#fdf3d8",
                 outline = color)
      cv.create_text(x + w / 2, y + h / 2 - 9 * s, text = card.rank,
                     fill = color, font = font(26, s))
      cv.create_text(x + w / 2, y + h / 2 + 17 * s, text = card.suit,
                     fill = color, font = font(16, s, "normal"))
   else:
      for col, fy in PIPS[card.value]:
         cv.create_text(x + PIP_COLS[col] * w, y + fy * h,
                        text = card.suit, fill = color,
                        font = font(15, s, "normal"))

###########################################################################

def draw_badge(cv, x, y, big, small, s = 1.0, color = GOLD):
   """
   use: draws the points display, centred vertically on y
   """
   bw, bh = 66 * s, 62 * s
   round_rect(cv, x, y - bh / 2, x + bw, y + bh / 2, 10 * s,
              fill = "#0b2c21", outline = color, width = 2)
   cv.create_text(x + bw / 2, y - 6 * s, text = big, fill = color,
                  font = font(22, s))
   cv.create_text(x + bw / 2, y + 18 * s, text = small, fill = color,
                  font = font(9, s, "normal"))

###########################################################################

def hand_badge(hand):
   """
   post: returns (big text, small text, colour) for a hand's points
   """
   total, soft = hand.total()
   if (hand.is_blackjack()):
      return("21", "blackjack", GOLD)
   if (total > 21):
      return(str(total), "bust", "#ff7b72")
   return(str(total), "soft" if (soft) else "points", GOLD)

###########################################################################
###
### s t r a t e g y    c h a r t    w i n d o w
###
###########################################################################

                               # cell colours and labels from the .ods
                               # chart: (fill, text colour, label)
CHART_CELL = {
   "H":  ("#00a933", "#ffffff", "Hit"),
   "S":  ("#ff0000", "#ffffff", "Stand"),
   "D":  ("#3465a4", "#ffffff", "DD"),
   "Ds": ("#ffff00", "#000000", "DD/St"),
   "P":  ("#bbe33d", "#ffffff", "Split"),
   "R":  ("#8e7cc3", "#ffffff", "Sur/Hit"),
   "BJ": ("#000000", "#ffffff", "BLACKJACK"),
}
CHART_BAND = {"H": "ALWAYS HIT", "S": "ALWAYS STAND", "P": "ALWAYS SPLIT",
              "D": "ALWAYS DOUBLE DOWN", "BJ": "BLACKJACK"}

###########################################################################

def chart_rows():
   """
   use: lists the chart's rows in display order
   post: returns [(row key, hand label, points, codes)]; codes is a list
         of 10 codes for dealer 2..A
   """
                               # a pair or ace row shows its points
                               # only when it plays exactly like that
                               # points row (a blackjack stands, like
                               # any 21)
   def points(total, codes):
      plain = HARD_CHART.get(total, "").split()
      codes = ["S" if (c == "BJ") else c for c in codes]
      return(str(total) if (codes == plain) else "")
   rows = []
   for v in range(2, 11):
      codes = PAIR_CHART[v].split()
      rows.append((("pair", v), f"{v}-{v}", points(2 * v, codes),
                   codes))
   rows.append((("pair", 11), "A-A", "", PAIR_CHART[11].split()))
   for n in range(2, 10):
      codes = SOFT_CHART[11 + n].split()
      rows.append((("soft", 11 + n), f"A-{n}", points(11 + n, codes),
                   codes))
   rows.append((("soft", 21), "A-10", points(21, ["BJ"] * 10),
                ["BJ"] * 10))
   for t in range(5, 21):
      rows.append((("hard", t), "", str(t), HARD_CHART[t].split()))
   return(rows)

###########################################################################

def chart_bands(rows):
   """
   use: groups rows whose 10 cells are all the same move into one
        merged band, like the .ods chart
   post: returns [(first row index, last row index, code)]; hard 17
         keeps its own band, as on the .ods
   """
   bands = []
   for i, (key, _, _, codes) in enumerate(rows):
      if (len(set(codes)) != 1):
         continue
      last = bands[-1] if (bands) else None
      if (last and last[1] == i - 1 and last[2] == codes[0] and
          rows[last[0]][0][0] == key[0] and key != ("hard", 18)):
         bands[-1] = (last[0], i, codes[0])
      else:
         bands.append((i, i, codes[0]))
   return(bands)

###########################################################################

def draw_chart(cv, row = None, up = None):
   """
   use: draws the strategy chart to fill a canvas
   usage: row and up, when given, outline the current decision's cell
   """
   cv.delete("all")
   W     = max(cv.winfo_width(), 400)
   H     = max(cv.winfo_height(), 400)
   rows  = chart_rows()
   lab_w = W * 0.075
   pts_w = W * 0.06
   cw    = (W - lab_w - pts_w - 16) / 10
   ch    = (H - 16) / (len(rows) + 2 + 2.6)
   s     = ch / 24
   x0    = 8 + lab_w + pts_w
   y0    = 8 + 2 * ch
   f     = lambda size, w = "normal": ("Helvetica",
                                       max(7, int(size * s)), w)
   cv.create_rectangle(x0, 8, x0 + 10 * cw, 8 + ch, fill = "#b2b2b2",
                       outline = "")
   cv.create_text(x0 + 5 * cw, 8 + ch / 2, text = "DEALER’S UP CARD",
                  font = f(12, "bold"))
   cv.create_rectangle(8, 8 + ch, x0, y0, fill = "#b2b2b2", outline = "")
   cv.create_text(8 + (lab_w + pts_w) / 2, 8 + 1.5 * ch, text = "You",
                  font = f(12, "bold"))
   for c in range(10):
      x = x0 + c * cw
      cv.create_rectangle(x, 8 + ch, x + cw, y0, fill = "#dddddd",
                          outline = "#999999")
      cv.create_text(x + cw / 2, 8 + 1.5 * ch, text = up_name(c + 2),
                     font = f(12))
   bands  = chart_bands(rows)
   banded = set()
   for first, last, _ in bands:
      banded.update(range(first, last + 1))
   for i, (key, label, points, codes) in enumerate(rows):
      y = y0 + i * ch
      cv.create_rectangle(8, y, 8 + lab_w, y + ch, fill = "#dddddd",
                          outline = "#999999")
      cv.create_text(8 + lab_w / 2, y + ch / 2, text = label,
                     font = f(12))
      cv.create_rectangle(8 + lab_w, y, x0, y + ch, fill = "#dddddd",
                          outline = "#999999")
      cv.create_text(8 + lab_w + pts_w / 2, y + ch / 2, text = points,
                     font = f(12))
                               # banded rows are drawn as one band
      for c, code in enumerate([] if (i in banded) else codes):
         fill, fg, text = CHART_CELL[code]
         x = x0 + c * cw
         cv.create_rectangle(x, y, x + cw, y + ch, fill = fill,
                             outline = fill)
         cv.create_text(x + cw / 2, y + ch / 2, text = text, fill = fg,
                        font = f(12))
   for first, last, code in bands:
      fill, fg, _ = CHART_CELL[code]
      ya, yb      = y0 + first * ch, y0 + (last + 1) * ch
      text        = CHART_BAND[code]
      if (rows[first][0] == ("hard", 17)):
         text += " (HARD 17)"
      cv.create_rectangle(x0, ya, x0 + 10 * cw, yb, fill = fill,
                          outline = fill)
      cv.create_text(x0 + 5 * cw, (ya + yb) / 2, text = " ".join(text),
                     fill = fg, font = f(12))
                               # line between dealer 6 and 7, except
                               # across the bands
   for i in range(len(rows)):
      if (i not in banded):
         y = y0 + i * ch
         cv.create_line(x0 + 5 * cw, y, x0 + 5 * cw, y + ch, width = 2)
                               # section lines: pairs / soft / hard
   for i in (9, 19):
      y = y0 + i * ch
      cv.create_line(8, y, x0 + 10 * cw, y, width = 3)
   yn = y0 + len(rows) * ch + 0.4 * ch
   for n, text in enumerate(("DD/St = Double if allowed, otherwise "
                             "Stand",
                             "Sur/Hit = Surrender if allowed, otherwise "
                             "Hit")):
      cv.create_text(x0, yn + n * ch * 1.1, text = text, anchor = "nw",
                     font = f(11))
   if (row is None or up is None):
      return
   for i, r in enumerate(rows):
      if (r[0] == row):
         x = x0 + (up - 2) * cw
         y = y0 + i * ch
         cv.create_rectangle(x + 1, y + 1, x + cw - 1, y + ch - 1,
                             outline = "#ffffff", width = 5)
         cv.create_rectangle(x + 1, y + 1, x + cw - 1, y + ch - 1,
                             outline = "#000000", width = 2)

                               # table layout: margin beside the cards
                               # at the minimum width; points box and
                               # its gap; space between split hands
TABLE_MARGIN = 16
BADGE_SPAN   = 78
HAND_GAP     = 24
DEALER_Y     = 42
DEALER_OFF   = 30
                               # headline + odds line block height, the
                               # usual gap below it, the smallest gaps
                               # allowed and the side panel's minimum
BLOCK_H      = 56
GAP_BELOW    = 92
MIN_GAP      = 12
SIDE_MIN     = 280
                               # table size asked for at startup: room
                               # for two split hands at full overlap
START_W      = 660
START_H      = 640
                               # tallest the window must stay so both
                               # gaps around the headline are MIN_GAP
TABLE_MIN_H  = (DEALER_Y + 2 * CARD_H + 38 + 22 + 26 + BLOCK_H
                + 2 * MIN_GAP)

###########################################################################

def hands_width(hands, off, s):
   """
   post: width of the player's hands laid out side by side, from the
         left edge of the first card to the right edge of the last
         points box
   """
   if (not hands):
      return(0)
   return(sum(CARD_W * s + off * s * (len(h.cards) - 1) + BADGE_SPAN * s
              for h in hands) + HAND_GAP * (len(hands) - 1))

###########################################################################

def table_min_width(game):
   """
   post: the narrowest the table can be with the cards on it at full
         size (overlap squeezed to its minimum) plus TABLE_MARGIN on
         each side
   """
   n      = len(game.dealer.cards)
   dealer = (CARD_W + DEALER_OFF * (n - 1) + BADGE_SPAN) if (n) else 0
   player = hands_width(game.hands, 16, 1.0)
   return(int(max(dealer, player, CARD_W + BADGE_SPAN))
          + 2 * TABLE_MARGIN)

###########################################################################

class App:
   """
   name: App
   use: tkinter window: a card table (dealer at the top, player at the
        bottom, a points display beside each hand) and a side panel
        with the bet, Proceed and Reset buttons and the explanation
        log
   """
   def __init__(self, root):
      self.root = root
      self.game = Game()
      self.head = "Ready"
      root.title("Blackjack Basic Strategy Player")
      root.configure(bg = "#0b2c21")
      fg   = "#f4f1e8"
      bg   = "#0b2c21"
      big  = ("Helvetica", 14)
      bold = ("Helvetica", 15, "bold")

                               # table and side panel share a split
                               # pane: its divider can be dragged, and
                               # when the window is resized the table
                               # takes the change (stretch "always")
                               # while the side panel keeps its width
      pane = tk.PanedWindow(root, orient = "horizontal", sashwidth = 8,
                            sashrelief = "raised", bg = "#3a5a4a",
                            bd = 0, opaqueresize = True,
                            sashcursor = "sb_h_double_arrow")
      pane.pack(fill = "both", expand = True)
      self.pane = pane
      self.table = tk.Canvas(pane, width = START_W,
                             height = START_H, bg = FELT,
                             highlightthickness = 0)
      self.table.bind("<Configure>", lambda e: self.draw_table())

      holder = tk.Frame(pane, bg = bg)
      side   = tk.Frame(holder, bg = bg)
      side.pack(fill = "both", expand = True, padx = 10, pady = 10)
      pane.add(self.table, minsize = 420, stretch = "always")
      pane.add(holder, minsize = SIDE_MIN, stretch = "never")
      self.bank_lbl = tk.Label(side, font = bold, fg = fg, bg = bg,
                               anchor = "w")
      self.bank_lbl.pack(fill = "x")
      self.shoe_lbl = tk.Label(side, font = big, fg = fg, bg = bg,
                               anchor = "w")
      self.shoe_lbl.pack(fill = "x")
      betrow = tk.Frame(side, bg = bg)
      betrow.pack(fill = "x", pady = 6)
      tk.Label(betrow, text = "Bet $", font = big, fg = fg,
               bg = bg).pack(side = "left")
      self.bet_var = tk.StringVar(value = str(DEFAULT_BET))
      tk.Spinbox(betrow, from_ = 2, to = 500, increment = 2, width = 5,
                 textvariable = self.bet_var,
                 font = big).pack(side = "left")

      btns = tk.Frame(side, bg = bg)
      btns.pack(fill = "x", pady = 6)
      self.go_btn = tk.Button(btns, text = "Proceed", width = 16,
                              font = bold, command = self.proceed)
      self.go_btn.pack(side = "left")
      tk.Button(btns, text = "Reset", font = big,
                command = self.reset).pack(side = "left", padx = 8)
      tk.Button(btns, text = "Chart", font = big,
                command = self.show_chart).pack(side = "left")
      self.chart_win = None

                               # the moves the user can make; the
                               # chart's move is highlighted
      moves = tk.Frame(side, bg = bg)
      moves.pack(fill = "x", pady = (0, 6))
      self.move_btns = {}
      for action, text in (("hit",       "Hit"),
                           ("stand",     "Stand"),
                           ("double",    "Double"),
                           ("split",     "Split"),
                           ("surrender", "Surrender")):
         b = tk.Button(moves, text = text, font = ("Helvetica", 12),
                       command = lambda a = action: self.move(a))
         b.pack(side = "left", padx = (0, 4))
         self.move_btns[action] = b
      self.btn_bg     = self.go_btn.cget("background")
      self.btn_active = self.go_btn.cget("activebackground")
                               # the side panel may not get narrower
                               # than its widest row of buttons
      root.update_idletasks()
      self.side_min = max(SIDE_MIN, btns.winfo_reqwidth() + 24,
                          moves.winfo_reqwidth() + 24)
      self.pane.paneconfigure(holder, minsize = self.side_min)
                               # open at the requested size, but no
                               # bigger than most of the screen
      w = min(root.winfo_reqwidth(),  int(root.winfo_screenwidth() * 0.9))
      h = min(root.winfo_reqheight(),
              int(root.winfo_screenheight() * 0.85))
      root.geometry(f"{w}x{max(h, TABLE_MIN_H)}")

      self.log = scrolledtext.ScrolledText(side, wrap = "word",
                                           height = 30, width = 46,
                                           font = ("Helvetica", 11))
      self.log.pack(fill = "both", expand = True, pady = (6, 0))
      self.log.tag_configure("head", font = ("Helvetica", 12, "bold"))
      self.log.tag_configure("bold", font = ("Helvetica", 11, "bold"))
                               # a 1-point newline with a background
                               # paints a full-width horizontal line
      self.log.tag_configure("rule", font = ("Helvetica", 1),
                             background = "#7a7a7a")
                               # white space above a round's first
                               # heading, below the line
      self.log.tag_configure("gap", spacing1 = 10)
      self.round_start = "1.0"
      self.log_last    = ""
      self.log.bind("<Configure>", lambda e: self.anchor_log())
      self.log.tag_configure("win", foreground = "#1a7f37")
      self.log.tag_configure("loss", foreground = "#c8102e")
      root.bind("<Return>", lambda e: self.proceed())
      self.refresh()

   ########################################################################

   def proceed(self):
      if (self.game.phase == "deal"):
         try:
            bet = int(self.bet_var.get())
         except ValueError:
            bet = DEFAULT_BET
                               # keep bets even so a blackjack (3:2)
                               # and a surrender (1/2) pay whole dollars
         self.game.bet = max(2, bet - bet % 2)
         self.bet_var.set(str(self.game.bet))
      new_round  = self.game.phase == "deal"
      head, body = self.game.step()
      self.write(head, body, rule = new_round)
      self.refresh()

   ########################################################################

   def show_chart(self):
      """
      use: opens the strategy chart window (or raises it if open)
      imp: the chart follows the game: the current decision's cell is
           outlined while a hand is in play
      """
      if (self.chart_win and self.chart_win.winfo_exists()):
         self.chart_win.deiconify()
         self.chart_win.lift()
         return
      win = tk.Toplevel(self.root)
      win.title("Basic Strategy Chart")
      h   = min(self.root.winfo_screenheight() - 100, 900)
      win.geometry(f"{int(h * 0.92)}x{h}")
      self.chart_cv  = tk.Canvas(win, bg = "#ffffff",
                                 highlightthickness = 0)
      self.chart_cv.pack(fill = "both", expand = True)
      self.chart_cv.bind("<Configure>", lambda e: self.draw_chart())
      self.chart_win = win

   ########################################################################

   def draw_chart(self):
      if (not (self.chart_win and self.chart_win.winfo_exists())):
         return
      g = self.game
      if (g.phase == "play"):
         draw_chart(self.chart_cv, g.decide()["row"], g.up())
      else:
         draw_chart(self.chart_cv)

   ########################################################################

   def move(self, action):
      """
      use: makes the move the user picked
      """
      g = self.game
      if (g.phase != "play" or action not in g.decide()["allowed"]):
         return
      head, body = g.step(action)
      self.write(head, body)
      self.refresh()

   ########################################################################

   def reset(self):
      self.game.reset()
      self.log.delete("1.0", "end")
      self.round_start = "1.0"
      self.write("Reset", f"Bankroll ${START_BANKROLL}; new "
                 f"{DECKS}-deck shoe.")
      self.refresh()

   ########################################################################

   def write(self, head, body, rule = False):
      """
      use: appends one step to the log
      usage: text between ** pairs in body is shown in bold, green
             when it starts with + and red when it starts with -; rule
             starts a round: a horizontal line (except at the very
             top), a little white space, then the heading scrolled to
             the top of the pane with the line just out of sight
      imp: a text widget cannot scroll its last line to the top, so
           blank padding (tag "pad") is kept after the text and
           removed before each write
      """
      log       = self.log
      self.head = head.replace("**", "")
      pad       = log.tag_ranges("pad")
      if (pad):
         log.delete(pad[0], pad[-1])
      tags = ("head",)
      if (rule):
         if (log.index("end-1c") != "1.0"):
            log.insert("end", "\n", "rule")
            tags = ("head", "gap")
      start = log.index("end-1c")
      log.insert("end", f"{self.head}\n", tags)
      for i, part in enumerate(body.split("**")):
         if (i % 2 == 0):
            log.insert("end", part)
         elif (part[:1] in ("+", "-")):
            log.insert("end", part[1:],
                       ("bold", "win" if (part[0] == "+") else "loss"))
         else:
            log.insert("end", part, "bold")
      log.insert("end", "\n\n")
      last = log.index("end-1c")
      log.insert("end", "\n" * 80, "pad")
      if (rule):
         self.round_start = start
      self.log_last = last
      self.anchor_log()

   ########################################################################

   def anchor_log(self):
      """
      use: shows the current round's heading at the top of the log and
           keeps its newest line in view
      imp: also bound to the log's resizing, since rewrapped text would
           otherwise leave the view in the blank padding
      """
      if (not self.log_last):
         return
      self.log.yview(self.round_start)
      self.log.see(self.log_last)

   ########################################################################

   def refresh(self):
      g = self.game
      self.bank_lbl.config(text = f"Bankroll ${g.bankroll}")
      self.shoe_lbl.config(text = f"Shoe: {len(g.shoe.cards)} cards")
      nxt = {"deal": "Proceed: deal", "play": "Proceed: decide",
             "dealer": "Proceed: dealer"}[g.phase]
                               # Proceed is green before the first deal
                               # of a game and for each round's first
                               # decision, grey otherwise
      hot = ((g.phase == "deal" and g.rounds == 0) or
             (g.phase == "play" and g.fresh))
      self.go_btn.config(text = nxt,
                         background = FIRST_MOVE if (hot) else self.btn_bg,
                         activebackground = FIRST_MOVE_HOVER if (hot)
                                            else self.btn_active)
      self.update_min_size()
      d = g.decide() if (g.phase == "play") else None
      for action, b in self.move_btns.items():
         if (d and action in d["allowed"]):
            hot = action == d["action"]
            b.config(state = "normal",
                     background = GOLD if (hot) else self.btn_bg,
                     activebackground = "#ffe9a8" if (hot)
                                        else self.btn_bg,
                     font = ("Helvetica", 12,
                             "bold" if (hot) else "normal"))
         else:
            b.config(state = "disabled", background = self.btn_bg,
                     font = ("Helvetica", 12, "normal"))
      self.draw_table()
      self.draw_chart()

   ########################################################################

   def update_min_size(self):
      """
      use: keeps the table at least as wide as the cards on it, and the
           window no smaller than the table and side panel need
      imp: called on every move, since splits and draws widen the
           cards; if the table is already too narrow the window grows
           (as far as the screen allows)
      """
      need = min(table_min_width(self.game),
                 self.root.winfo_screenwidth() - self.side_min - 48)
      self.pane.paneconfigure(self.table, minsize = need)
      self.root.minsize(need + self.side_min + 8, TABLE_MIN_H)
      have = self.table.winfo_width()
      if (self.root.winfo_ismapped() and have < need):
         grow = min(self.root.winfo_width() + need - have,
                    self.root.winfo_screenwidth())
         self.root.geometry(f"{grow}x{self.root.winfo_height()}")

   ########################################################################

   def draw_table(self):
      """
      use: redraws the whole table from the game state
      imp: called on every move and whenever the window is resized.
           Dealer cards stay at the top and player hands at the
           bottom; the headline and odds line stay centred (clipped by
           the canvas when narrow) and use up the space above them
           first when the window gets shorter
      """
      g  = self.game
      cv = self.table
      cv.delete("all")
      W  = max(cv.winfo_width(), 1)
      H  = max(cv.winfo_height(), 1)
      cv.create_text(W / 2, 22, text = "DEALER", fill = "#cfe8d8",
                     font = ("Helvetica", 13, "bold"))
      cv.create_text(W / 2, H - 18, text = "PLAYER", fill = "#cfe8d8",
                     font = ("Helvetica", 13, "bold"))

                               # player hands: shrink the overlap,
                               # then the cards, to fit
      hands = g.hands
      s     = 1.0
      off   = 28
      while (hands_width(hands, off, s) > W - 2 * TABLE_MARGIN and
             off > 16):
         off -= 2
      while (hands_width(hands, off, s) > W - 2 * TABLE_MARGIN and
             s > 0.55):
         s -= 0.05
      py0 = H - 38 - CARD_H * s - 22

                               # headline block: keep it GAP_BELOW
                               # above the player's box, taking space
                               # from above until both gaps match
      top_edge = DEALER_Y + CARD_H
      bot_edge = py0 - 26
      free     = bot_edge - top_edge - BLOCK_H
      yh       = H / 2 - 4
      if (bot_edge - (yh + BLOCK_H - 14) < GAP_BELOW):
         below = min(GAP_BELOW, free / 2)
         yh    = bot_edge - below - (BLOCK_H - 14)
      cv.create_text(W / 2, yh, text = self.head, fill = GOLD,
                     font = ("Helvetica", 20, "bold"))
      cv.create_text(W / 2, yh + 34,
                     text = "BLACKJACK PAYS 3 TO 2  \u2022  DEALER "
                            "STANDS ON ALL 17s",
                     fill = "#2f7d5a", font = ("Helvetica", 11, "bold"))
      if (not g.dealer.cards):
         return

                               # dealer at the top
      n   = len(g.dealer.cards)
      dw  = CARD_W + DEALER_OFF * (n - 1)
      x0  = (W - dw - BADGE_SPAN) / 2
      for i, c in enumerate(g.dealer.cards):
         draw_card(cv, x0 + i * DEALER_OFF, DEALER_Y, c,
                   hidden = (i == 1 and not g.hole_up))
      if (g.hole_up):
         big, small, color = hand_badge(g.dealer)
      else:
         big, small, color = str(g.up()), "showing", GOLD
      draw_badge(cv, x0 + dw + 12, DEALER_Y + CARD_H / 2, big, small,
                 color = color)

                               # player hands along the bottom
      x  = (W - hands_width(hands, off, s)) / 2
      y0 = py0
      for i, h in enumerate(hands):
         hw = CARD_W * s + off * s * (len(h.cards) - 1)
         if (g.phase == "play" and i == g.current):
            round_rect(cv, x - 8, y0 - 26, x + hw + (BADGE_SPAN + 8) * s,
                       y0 + CARD_H * s + 8, 12, fill = "",
                       outline = GOLD, width = 3)
         tags = []
         if (h.doubled):
            tags.append("doubled")
         label = (     f"Hand {i + 1}  ${h.bet}" if (len(hands) > 1)
                  else f"Bet ${h.bet}")
         if (tags):
            label += "  (" + ", ".join(tags) + ")"
         cv.create_text(x, y0 - 13, text = label, anchor = "w",
                        fill = "#f4f1e8", font = font(11, s))
         for j, c in enumerate(h.cards):
            draw_card(cv, x + j * off * s, y0, c, s)
         big, small, color = hand_badge(h)
         if (h.surrendered):
            small, color = "surrendered", "#b9a6ff"
         draw_badge(cv, x + hw + 12 * s, y0 + CARD_H * s / 2, big,
                    small, s, color)
         if (h.result):
            rc = (     "#7ee787" if (h.result.startswith(("WIN",
                                                         "BLACKJACK")))
                  else "#f4f1e8" if (h.result == "PUSH")
                  else "#ff7b72")
            cv.create_text(x + (hw + BADGE_SPAN * s) / 2,
                           y0 + CARD_H * s + 16, text = h.result,
                           fill = rc, font = font(13, s))
         x += hw + BADGE_SPAN * s + HAND_GAP

###########################################################################

def usage():
   """
   use: prints the usage text to stderr
   """
   print(f"{SELF}\n"
         f"usage: {SELF} [options]\n"
         "where options are:\n"
         "   -f run in the foreground; the terminal waits until the\n"
         "      window is closed\n"
         "   -h help", file = sys.stderr)

###########################################################################

def detach(args):
   """
   use: starts a second copy of this script, cut loose from the
        terminal, to run the window
   pre: the window can be opened (checked by the caller)
   post: returns True when the copy started; the caller then exits so
         the terminal gets its prompt back
   imp: the copy is told it is the detached one through CHILD_ENV so it
        does not detach again. On Windows it runs under pythonw.exe
        when there is one (no console window) as a DETACHED_PROCESS; on
        Linux it gets a new session so closing the terminal (SIGHUP)
        does not take the window with it
   """
   env            = dict(os.environ)
   env[CHILD_ENV] = "1"
   exe            = sys.executable
   kw             = {"stdin":  subprocess.DEVNULL,
                     "stdout": subprocess.DEVNULL,
                     "stderr": subprocess.DEVNULL,
                     "env":    env,
                     "close_fds": True}
   if (os.name == "nt"):
      pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
      if (os.path.exists(pyw)):
         exe = pyw
      kw["creationflags"] = (subprocess.DETACHED_PROCESS |
                             subprocess.CREATE_NEW_PROCESS_GROUP)
   else:
      kw["start_new_session"] = True
   try:
      subprocess.Popen([exe, os.path.abspath(sys.argv[0])] + args, **kw)
   except OSError as exception:
      print(f"{SELF}: cannot start in the background: {exception}; "
            "running in the foreground", file = sys.stderr)
      return(False)
   return(True)

###########################################################################
###
### s t a r t    o f    e x e c u t i o n
###
###########################################################################

if ( __name__ == "__main__" ):
   args       = sys.argv[1:]
   foreground = False
   for arg in args:
      if (arg == "-h"):
         usage()
         sys.exit(E_SUCCESS)
      elif (arg == "-f"):
         foreground = True
      else:
         print(f"{SELF}: unknown option {arg}", file = sys.stderr)
         usage()
         sys.exit(E_USAGE)
   try:
      root = tk.Tk()
   except tk.TclError as exception:
      print(f"{SELF}: cannot open a window: {exception}",
            file = sys.stderr)
      sys.exit(E_FAIL)
                               # the window opened, so the detached copy
                               # will be able to open one too; errors
                               # are reported here while the terminal
                               # is still listening
   if (not foreground and not os.environ.get(CHILD_ENV)):
      root.withdraw()
      root.destroy()
      if (detach(args)):
         sys.exit(E_SUCCESS)
      root = tk.Tk()
   App(root)
   root.mainloop()
   sys.exit(E_SUCCESS)
