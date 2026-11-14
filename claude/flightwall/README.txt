flightwall - TheFlightWall_OSS in a terminal
============================================

A Python port of the TheFlightWall_OSS firmware
(github.com/AxisNimble/TheFlightWall_OSS). Instead of driving an LED
wall from an ESP32, it draws the LED panel in a terminal window. No
browser and no extra packages: just Python 3.8 or later.

Run
---
   python flightwall.py PIT                    around an airport
   python flightwall.py "brighton heights, pa"    around a place
   python flightwall.py -x40.484,-80.037 -r8    around a position
   python flightwall.py -fUAL1179,N512GA        follow flights
   python flightwall.py                         use the .ini
   python flightwall.py -d                      demo, no network
   python flightwall.py -o PIT                  print once, exit
   python flightwall.py -h                      all options

Ctrl+C stops it.

Options
-------
   -xLAT,LON    centre of the area
   -rKM         radius in km
   -fIDENTS     follow up to 5 flights anywhere: callsigns (UAL1179),
                flight numbers (UA1179) or tail numbers (N512GA)
   -cFILE       settings file (default flightwall.ini)
   -sSTYLE      half (LED panel, default) or text (plain lines)
   -gWxH        panel size for layout = classic (the wall layout is
                always 128x64)
   -lFILE       write the log to FILE
   -o           fetch once, print the flights as text and exit
   -d           demo data, no network needed
   -v           more detail in the log (-vv for more)
   -h           help

Values attach directly to the letter: -r15, not -r 15.

A place on the command line (instead of -x) can be:
- a three-letter airport code, e.g. PIT. Three letters are always
  tried as an airport code first, then as a place name.
- a place name, in quotes if it has spaces: "brighton heights, pa".
-x and a place can't be used together.

The display (layout = wall)
---------------------------
A 128 x 64 LED panel with the firmware's 5x7 font. At full size it
needs a terminal of 128 columns by 33 rows.

   [logo]  flight number (UA1179), else callsign or airline
           route and % flown (KEWR>KDEN  48%)
           aircraft (Boeing 737-900 when it fits)
   takeoff time ---green progress bar--- landing time
   altitude                          speed
   track (heading)        climb/descent rate
   what the plane is doing

The bottom line says At KPIT (on the ground, not yet departed),
Departing KPIT, Over <town>, Near <town>, Approaching KBOS, Landing at
KBOS or Landed at KBOS. "At" is green; Approaching, Landing at and
Landed at are red; the rest are white. The progress bar is green, and
turns red while the plane is landing and once it has landed. The
climb rate's unit is dropped when the line would be too long.
Takeoff and landing times sit at the ends of the bar when known:
both with AeroAPI; with the free route data only the estimated
landing time. Private planes without a route show "Route unknown"
and no bar.

Window size: the panel is always drawn pixel for pixel, never
scaled, because a bitmap font can't be resized and stay crisp. In a
smaller window you see its top left part, with the right and/or
bottom edge cut off; in a larger one it is centred at full size.

layout = classic in the .ini gives the original firmware's three-line
card on the [hardware] panel (160 x 32) instead.

Settings
--------
On first run the script writes flightwall.ini next to itself:
location and radius, flights to follow, API keys and data sources,
timing, brightness, text colour, units and logos.
Command-line options override it for that run.

Data sources
------------
Live positions: airplanes.live (free), or OpenSky with an account.
Routes: adsbdb.com (free), or FlightAware AeroAPI with a key (paid
per lookup). Airline logos: images.kiwi.com. To use your own, put
<ICAO>.png or <IATA>.png (e.g. UAL.png) in a "logos" folder beside
the script.

Files downloaded once and kept beside the script:
- airports.csv: airport codes and positions from OurAirports (public
  domain), about 12 MB, refreshed every 90 days. Downloaded the first
  time you give a three-letter code, or when AeroAPI is used.
- cities1000.zip: towns of 1,000+ people from GeoNames
  (geonames.org, CC BY 4.0), about 10 MB, refreshed every 180 days.
  "Over <town>" is looked up in it on your computer; no online
  service is asked about aircraft positions.
- flightwall_places.json: place names you have looked up, so each
  name is only ever looked up once.

Place names are looked up with OpenStreetMap's Nominatim service.
Data (c) OpenStreetMap contributors, ODbL. Its usage policy is at
https://operations.osmfoundation.org/policies/nominatim/ - in short:
at most one request a second, an identifying User-Agent (the script
sends one), results cached (they are), and no bulk, automatic or
repeated lookups. The script only asks when you type a new place name.

Font
----
Adafruit GFX classic 5x7 font, BSD licence, (c) 2012 Adafruit
Industries: the firmware's own font.

Differences from the firmware
-----------------------------
- AeroAPI answers are cached so you aren't billed again for every
  plane on every fetch.
- When AeroAPI returns several flights for a callsign, the one in the
  air is used rather than simply the first.
- Flights cycle every display_cycle_seconds; the firmware only
  redraws after each 30 s fetch.
- Aircraft are listed nearest first.
- The wall layout, flight following (-f) and place-name lookup are
  additions.
