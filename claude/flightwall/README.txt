flightwall - TheFlightWall_OSS in a terminal
============================================

A Python port of the TheFlightWall_OSS firmware
(github.com/AxisNimble/TheFlightWall_OSS). Instead of driving an LED
wall from an ESP32, it draws the LED panel in a terminal window. No
browser and no extra packages: just Python 3.8 or later.

Run
---
   python flightwall.py                         use the .ini
   python flightwall.py -a40.4914,-80.2328 -r15  around a position
   python flightwall.py -d                      demo, no network
   python flightwall.py -o                      print once, exit
   python flightwall.py -h                      all options

Ctrl+C stops it.

Options
-------
   -aLAT,LON    centre of the area
   -rKM         radius in km
   -cFILE       settings file (default flightwall.ini)
   -sSTYLE      half (LED panel, default) or text (plain lines)
   -gWxH        panel size in pixels
   -lFILE       write the log to FILE
   -o           fetch once, print the flights as text and exit
   -d           demo data, no network needed
   -v           more detail in the log (-vv for more)
   -h           help

Values attach directly to the letter: -r15, not -r 15.

The display
-----------
The firmware's card on its 160 x 32 panel (ten 16 x 16 tiles, two
rows): a border and three lines, cycled through the aircraft in the
area, nearest first.

With layout = full (the default) each aircraft gets two pages, with
the airline's badge (its code on a coloured disc) at the left:

   card       airline / origin>destination / aircraft
   readings   ALT altitude
              SPD speed TRK track
              VR climb or descent rate (an arrow shows which)

layout = classic shows the firmware's card only, without the badge.
Speeds, altitudes, climb rates and tracks can be shown in other units
(kt, mph, km/h, m/s, Mach; ft, m; ft/min, m/s; degrees, compass
points or both): see [display] in the .ini.

Each LED is drawn as half a character cell with the upper half block
character, so the panel needs a terminal of 160 columns by 17 rows.
Terminals without 24-bit colour can use -stext for plain lines.

Settings
--------
On first run the script writes flightwall.ini next to itself, with
the same settings as the firmware's config/*.h files: location and
radius, API keys and data sources, timing, brightness, text colour,
route codes (ICAO or IATA), layout, units and the panel's tiles.
Command-line options override it for that run.

Data sources
------------
Live positions: OpenSky with an account (client id and secret, as
the firmware uses), else airplanes.live (free; adsb.lol as backup).
Routes: FlightAware AeroAPI with a key (paid per lookup), else
adsbdb.com (free). Airline and aircraft display names come from
TheFlightWall's CDN, as in the firmware.

Differences from the firmware
-----------------------------
- AeroAPI answers are cached so you aren't billed again for every
  plane on every fetch.
- When AeroAPI returns several flights for a callsign, the one in the
  air is used rather than simply the first.
- Flights cycle every display_cycle_seconds; the firmware only
  redraws after each 30 s fetch.
- Aircraft are listed nearest first.
- Free stand-ins (airplanes.live, adsbdb.com) when there are no
  OpenSky/AeroAPI credentials, and a demo mode.
- Airline badges and the ADS-B readings page (layout = full) are
  additions.
