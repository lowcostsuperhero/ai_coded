#!/usr/bin/python
# name: $Id$
# use: Python re-implementation of TheFlightWall_OSS firmware
#      (github.com/AxisNimble/TheFlightWall_OSS) that draws the 160x32 LED
#      matrix in a terminal window instead of on WS2812B panels; no
#      browser, no extra packages
# usage: python flightwall.py -h
# imp: data flow is the firmware's: state vectors (OpenSky) -> flight
#      info (AeroAPI) -> display names (TheFlightWall CDN) -> three-line
#      card cycled on the matrix. Without OpenSky/AeroAPI credentials the
#      free airplanes.live and adsbdb.com services stand in for them.
#      The 5x7 font is Adafruit GFX's classic glcdfont (BSD licence,
#      Copyright (c) 2012 Adafruit Industries), the font the panel uses.
#      Beyond the firmware: live ADS-B readings (altitude, speed, track,
#      vertical rate) and colour airline logos (layout = full).

import configparser
import csv
import datetime
import getopt
import io
import json
import math
import os
import random
import re
import shutil
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field

E_SUCCESS = 0
E_WARN    = 1
E_FAIL    = 2
E_USAGE   = 3

REVISION   = "1.2"
USER_AGENT = "flightwall/1.2 (personal, non-commercial)"

OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
                     "opensky-network/protocol/openid-connect/token")
OPENSKY_BASE_URL  = "https://opensky-network.org"
AEROAPI_BASE_URL  = "https://aeroapi.flightaware.com/aeroapi"
FLIGHTWALL_CDN    = "https://cdn.theflightwall.com"
ADSB_FEEDS        = ["https://api.airplanes.live/v2",
                     "https://api.adsb.lol/v2"]
ADSBDB_BASE_URL   = "https://api.adsbdb.com/v0"
NOMINATIM_URL     = "https://nominatim.openstreetmap.org"
OURAIRPORTS_URL   = ("https://davidmegginson.github.io/ourairports-data/"
                     "airports.csv")
GEONAMES_URL      = "https://download.geonames.org/export/dump/cities1000.zip"

                               # mirrors the firmware's config/*.h files;
                               # written out on first run for editing
DEFAULT_INI = """\
# flightwall settings; same knobs as the firmware's config/*.h

[location]
# UserConfiguration.h: CENTER_LAT, CENTER_LON, RADIUS_KM
# (Brighton Heights, Pittsburgh; the firmware ships with San Francisco)
center_lat = 40.484
center_lon = -80.037
radius_km  = 10.0

[tracking]
# not in the firmware: up to 5 flights to follow anywhere instead of
# watching the area (-f on the command line), e.g.
# flights = UAL1179, DL402, N512GA
# (callsign, airline flight number or tail number); blank = area mode
flights =

[api]
# APIConfiguration.h. Leave blank to use the free stand-ins:
# airplanes.live for positions, adsbdb.com for routes.
opensky_client_id     =
opensky_client_secret =
aeroapi_key           =
# auto | opensky | adsb       (auto: opensky if credentials are set)
state_source  = auto
# auto | aeroapi | adsbdb     (auto: aeroapi if a key is set)
flight_source = auto
# airline/aircraft display names from TheFlightWall CDN: yes | no
use_flightwall_cdn = yes

[timing]
# TimingConfiguration.h; keep fetches at 30 s or more with OpenSky
fetch_interval_seconds = 30
display_cycle_seconds  = 3

[display]
# UserConfiguration.h; brightness 0-255 (the firmware's 5 suits real
# LEDs, a terminal needs more), text colour as RRGGBB
brightness = 255
text_color = ffffff
# icao (KJFK>EGLL, as the firmware shows) | iata (JFK>LHR)
route_codes = icao
# also show aircraft with no flight info (private/GA): yes | no
show_unidentified = no
# half: LED matrix with half-block pixels | text: plain lines
style = half
# wall: logo, flight, route and %, aircraft, green progress bar with
#       takeoff/landing times, altitude/speed, track/climb, and what
#       the plane is doing, on a panel_size panel (128x64 needs a
#       terminal of about 128 x 33)
# classic: the firmware's three-line card on the [hardware] panel
layout     = wall
panel_size = 128x64
# a smaller window shrinks the panel to fit, down to this fraction of
# its size (below about 0.75 the text becomes hard to read); smaller
# still and the panel's right and bottom edges are cut off instead
min_scale  = 0.75
# airline logo in colour: yes | no. {iata} and {icao} are replaced by
# the airline's codes; a PNG in a "logos" folder beside this script
# named <ICAO>.png or <IATA>.png is used first, if there is one
show_logo = yes
logo_url  = https://images.kiwi.com/airlines/64/{iata}.png
# units: speed kt|mph|kmh|mps|mach, altitude ft|m,
# vertical rate fpm|mps, track deg|card|both
speed_unit         = kt
altitude_unit      = ft
vertical_rate_unit = fpm
track_unit         = both

[hardware]
# HardwareConfiguration.h: tile size and arrangement -> 160 x 32
# (used by layout = classic)
tile_pixel_w = 16
tile_pixel_h = 16
tiles_x      = 10
tiles_y      = 2
"""

###########################################################################
###
### f o n t
###
###########################################################################

                               # Adafruit GFX glcdfont, ASCII 32..126; five
                               # column bytes per glyph, bit 0 = top row
FONT_5X7 = bytes.fromhex(
   "000000000000005f00000007000700147f147f14242a7f2a1223130864623649562050"
   "0008070300001c2241000041221c002a1c7f1c2a08083e080800807030000808080808"
   "000060600020100804023e5149453e00427f400072494949462141494d331814127f10"
   "27454545393c4a49493141211109073649494936464949291e00001400000040340000"
   "00081422411414141414004122140802015909063e415d594e7c1211127c7f49494936"
   "3e414141227f4141413e7f494949417f090909013e414151737f0808087f00417f4100"
   "2040413f017f081422417f404040407f021c027f7f0408107f3e4141413e7f09090906"
   "3e4151215e7f09192946264949493203017f01033f4040403f1f2040201f3f4038403f"
   "631408146303047804036159494d43007f4141410204081020004141417f0402010204"
   "4040404040000307080020545478407f284444383844444428384444287f3854545418"
   "00087e090218a4a49c787f0804047800447d40002040403d007f1028440000417f4000"
   "7c047804787c080404783844444438fc1824241818242418fc7c080404084854545424"
   "04043f44243c4040207c1c2040201c3c4030403c44281028444c9090907c4464544c44"
   "0008364100000077000000413608000201020402"
   )
                               # glcdfont's cp437 glyphs 248, 24 and 25
                               # 3x5 digits for the progress bar's times,
                               # rows top to bottom, 3 pixels a row
TINY_3X5 = {"0": "111101101101111", "1": "010110010010111",
            "2": "111001111100111", "3": "111001111001111",
            "4": "101101111001001", "5": "111100111001111",
            "6": "111100111101111", "7": "111001010010010",
            "8": "111101111101111", "9": "111101111001111",
            "A": "010101111101101", "P": "110101110100100"}

FONT_EXTRA = {"°": bytes.fromhex("060f090f06"),
              "↑": bytes.fromhex("08047e0408"),
              "↓": bytes.fromhex("10207e2010")}

###########################################################################
###
### e x c e p t i o n s
###
###########################################################################

class ExceptionClass(Exception):
   """
      name: ExceptionClass
      use:  errors this program reports and exits on
   """
   def __init__(self, message, exec_name = "", exit_code = E_FAIL):
      self.message   = (     f"{exec_name}: {message}" if (exec_name)
                        else f"{message.capitalize()}")
      self.exit_code = exit_code
      super().__init__(self.message)

   ########################################################################

   def print_error(self):
      print(self.message, file = sys.stderr)
      sys.exit(self.exit_code)

###########################################################################

class FetchError(Exception):
   pass

###########################################################################
###
### l o g g i n g
###
###########################################################################

class Log:
   """
      name: Log
      use:  stands in for the firmware's Serial output; the terminal is
            busy showing the matrix, so lines go to a file (-l) or nowhere
   """
   handle  = None
   verbose = 0
   lock    = threading.Lock()

   @classmethod
   def line(cls, text, level = 0):
      if ((cls.handle is None) or (level > cls.verbose)):
         return
      with cls.lock:
         cls.handle.write(time.strftime("%H:%M:%S ") + text + "\n")
         cls.handle.flush()

###########################################################################
###
### m o d e l s
###
###########################################################################

@dataclass
class StateVector:
   """
      use: models/StateVector.h; SI units as OpenSky gives them
   """
   icao24         : str   = ""
   callsign       : str   = ""
   origin_country : str   = ""
   lat            : float = math.nan
   lon            : float = math.nan
   baro_altitude  : float = math.nan
   on_ground      : bool  = False
   velocity       : float = math.nan
   heading        : float = math.nan
   vertical_rate  : float = math.nan
   squawk         : str   = ""
   distance_km    : float = math.nan
   bearing_deg    : float = math.nan
                               # not in the firmware: the free feed knows
                               # the aircraft type, OpenSky does not
   type_code      : str   = ""
   registration   : str   = ""

###########################################################################

@dataclass
class AirportInfo:
   code_icao : str   = ""
   code_iata : str   = ""
                               # position, when the source knows it; used
                               # for a tracked flight's progress
   lat       : float = math.nan
   lon       : float = math.nan
   elev_ft   : float = 0.0

###########################################################################

@dataclass
class FlightInfo:
   """
      use: models/FlightInfo.h
   """
   ident                       : str = ""
   ident_icao                  : str = ""
   ident_iata                  : str = ""
   operator_code               : str = ""
   operator_icao               : str = ""
   operator_iata               : str = ""
   aircraft_code               : str = ""
   airline_display_name_full   : str = ""
   aircraft_display_name_short : str = ""
   origin      : AirportInfo = field(default_factory = AirportInfo)
   destination : AirportInfo = field(default_factory = AirportInfo)
   state       : StateVector = field(default_factory = StateVector)
                               # not in the firmware: logo pixels, rows of
                               # (r, g, b) or None for an unlit LED
   logo        : object      = None
                               # tracking (-t): progress 0..100, arrival
                               # as epoch seconds, and not-found marker
   progress    : float       = math.nan
   eta         : float       = 0.0
   missing     : bool        = False
                               # takeoff time (epoch, 0 = unknown) and the
                               # town beneath the plane ("Over Erie, PA")
   off_time    : float       = 0.0
   where       : str         = ""

###########################################################################
###
### h e l p e r s
###
###########################################################################

def haversine_km(lat1, lon1, lat2, lon2):
   """
      use: utils/GeoUtils.h haversineKm
   """
   dlat = math.radians(lat2 - lat1)
   dlon = math.radians(lon2 - lon1)
   a    = (math.sin(dlat / 2) ** 2 +
           math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
           math.sin(dlon / 2) ** 2)
   return(2 * 6371.0 * math.asin(min(1.0, math.sqrt(a))))

###########################################################################

def bearing_deg(lat1, lon1, lat2, lon2):
   """
      use: utils/GeoUtils.h computeBearingDeg
   """
   dlon = math.radians(lon2 - lon1)
   p1   = math.radians(lat1)
   p2   = math.radians(lat2)
   x    = math.sin(dlon) * math.cos(p2)
   y    = (math.cos(p1) * math.sin(p2) -
           math.sin(p1) * math.cos(p2) * math.cos(dlon))
   return((math.degrees(math.atan2(x, y)) + 360.0) % 360.0)

###########################################################################

def http_json(url, headers = None, data = None, timeout = 12):
   """
      use:  GET (or POST when data is given) and decode the json reply
      post: returns (status, decoded or None); network failures raise
            FetchError
   """
   head = {"User-Agent": USER_AGENT, "Accept": "application/json"}
   head.update(headers or {})
   request = urllib.request.Request(url, data = data, headers = head)
   try:
      with urllib.request.urlopen(request, timeout = timeout) as reply:
         return(reply.status, json.loads(reply.read().decode("utf-8")))
   except urllib.error.HTTPError as error:
      return(error.code, None)
   except (urllib.error.URLError, OSError, ValueError) as error:
      raise(FetchError(f"{urllib.parse.urlparse(url).hostname}: "
                       f"{error}"))

###########################################################################

class TtlCache:
   """
      name: TtlCache
      use:  remembers lookups so paid APIs are not asked again every fetch
   """
   def __init__(self):
      self.items = {}
      self.lock  = threading.Lock()

   ########################################################################

   def get(self, key):
      """
         post: returns (hit, value)
      """
      with self.lock:
         entry = self.items.get(key)
         if ((entry is None) or (entry[0] < time.time())):
            return(False, None)
         return(True, entry[1])

   ########################################################################

   def put(self, key, value, ttl):
      with self.lock:
         self.items[key] = (time.time() + ttl, value)

###########################################################################
###
### r e f e r e n c e    d a t a
###
###########################################################################

def data_path(name):
   """
      post: returns the path of a data file kept beside this script
   """
   return(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))

###########################################################################

def ensure_download(url, path, max_age_days):
   """
      use:  downloads url to path unless a copy younger than max_age_days
            is already there
      post: returns True when path is usable (a stale copy counts when
            the download fails)
   """
   if (os.path.isfile(path) and
       (time.time() - os.path.getmtime(path) < max_age_days * 86400)):
      return(True)
   Log.line(f"downloading {url}")
   request = urllib.request.Request(url, headers = {
                "User-Agent": USER_AGENT})
   partial = path + ".part"
   try:
      with urllib.request.urlopen(request, timeout = 60) as reply, \
           open(partial, "wb") as handle:
         shutil.copyfileobj(reply, handle)
      os.replace(partial, path)
      return(True)
   except (urllib.error.URLError, OSError) as error:
      Log.line(f"download of {url} failed: {error}")
      try:
         os.remove(partial)
      except OSError:
         pass
      return(os.path.isfile(path))

###########################################################################

class AirportDB:
   """
      name: AirportDB
      use:  airport codes, names and positions from OurAirports
            (public domain), downloaded once beside the script and
            refreshed every 90 days
      imp:  loaded on first use; about 12 MB to download, a second or
            two to read
   """
   lock    = threading.Lock()
   by_iata = None
   by_icao = None
   RANK    = {"large_airport": 0, "medium_airport": 1,
              "small_airport": 2, "seaplane_base": 3, "heliport": 4}

   @classmethod
   def _load(cls):
      with cls.lock:
         if (cls.by_iata is not None):
            return(bool(cls.by_iata))
         cls.by_iata = {}
         cls.by_icao = {}
         path = data_path("airports.csv")
         if (not ensure_download(OURAIRPORTS_URL, path, 90)):
            return(False)
         try:
            with open(path, "r", encoding = "utf-8", newline = "") as f:
               for row in csv.DictReader(f):
                  rank = cls.RANK.get(row.get("type"), 9)
                  if (rank > 4):
                     continue
                  try:
                     info = {"name": row.get("name") or "",
                             "lat" : float(row["latitude_deg"]),
                             "lon" : float(row["longitude_deg"]),
                             "elev": float(row.get("elevation_ft") or 0),
                             "iata": (row.get("iata_code") or "").upper(),
                             "icao": (row.get("icao_code") or
                                      row.get("gps_code") or
                                      row.get("ident") or "").upper(),
                             "sched": row.get("scheduled_service") ==
                                      "yes",
                             "rank": rank}
                  except (KeyError, ValueError):
                     continue
                  if (info["iata"]):
                               # one code, several rows: the airport
                               # with airline service wins
                     old = cls.by_iata.get(info["iata"])
                     if ((old is None) or
                         ((not info["sched"], info["rank"]) <
                          (not old["sched"], old["rank"]))):
                        cls.by_iata[info["iata"]] = info
                  for code in {info["icao"],
                               (row.get("ident") or "").upper()}:
                     if (code and (code not in cls.by_icao)):
                        cls.by_icao[code] = info
         except (OSError, csv.Error) as error:
            Log.line(f"AirportDB: cannot read {path}: {error}")
         return(bool(cls.by_iata))

   ########################################################################

   @classmethod
   def iata(cls, code):
      return(cls.by_iata.get(code.upper()) if (cls._load()) else None)

   ########################################################################

   @classmethod
   def icao(cls, code):
      return(cls.by_icao.get(code.upper()) if (cls._load()) else None)

###########################################################################

                               # GeoNames admin1 codes for Canada are
                               # numbers; show the usual abbreviations
CANADA_PROVINCES = {"01": "AB", "02": "BC", "03": "MB", "04": "NB",
                    "05": "NL", "07": "NS", "08": "ON", "09": "PE",
                    "10": "QC", "11": "SK", "12": "YT", "13": "NT",
                    "14": "NU"}

class PlaceIndex:
   """
      name: PlaceIndex
      use:  "Over <town>": nearest town of 1,000+ people from GeoNames'
            cities1000 list (CC BY 4.0), downloaded once beside the
            script; looked up offline, so no geocoding service is asked
            about aircraft positions
   """
   lock  = threading.Lock()
   cells = None

   @classmethod
   def _load(cls):
      with cls.lock:
         if (cls.cells is not None):
            return(bool(cls.cells))
         cls.cells = {}
         path = data_path("cities1000.zip")
         if (not ensure_download(GEONAMES_URL, path, 180)):
            return(False)
         try:
            with zipfile.ZipFile(path) as archive:
               with archive.open("cities1000.txt") as raw:
                  for line in io.TextIOWrapper(raw, encoding = "utf-8"):
                     p = line.rstrip("\n").split("\t")
                     if (len(p) < 15):
                        continue
                     try:
                        lat = float(p[4])
                        lon = float(p[5])
                     except ValueError:
                        continue
                     cc     = p[8]
                     region = (     p[10]  if (cc == "US")
                               else CANADA_PROVINCES.get(p[10], cc)
                                           if (cc == "CA")
                               else cc)
                     key    = (int(math.floor(lat)), int(math.floor(lon)))
                     cls.cells.setdefault(key, []).append(
                        (lat, lon, p[2] or p[1], region))
         except (OSError, zipfile.BadZipFile, KeyError) as error:
            Log.line(f"PlaceIndex: cannot read {path}: {error}")
         return(bool(cls.cells))

   ########################################################################

   @classmethod
   def nearest(cls, lat, lon):
      """
         post: returns (name, region, distance km) of the closest town,
               or None when there is none within about 100 km
      """
      if (not cls._load()):
         return(None)
      best = None
      cy   = int(math.floor(lat))
      cx   = int(math.floor(lon))
      for dy in (-1, 0, 1):
         for dx in (-1, 0, 1):
            for plat, plon, name, region in cls.cells.get(
                                               (cy + dy, cx + dx), ()):
               d = haversine_km(lat, lon, plat, plon)
               if ((best is None) or (d < best[2])):
                  best = (name, region, d)
      return(best)

   ########################################################################

   @classmethod
   def describe(cls, lat, lon):
      """
         post: returns "Over Moon Township, PA", "Near Erie, PA" or ""
      """
      hit = cls.nearest(lat, lon)
      if (hit is None):
         return("")
      name, region, dist = hit
      name = (name.replace(" Township", " Twp")
                  .replace("Saint ", "St. ")
                  .replace("Mount ", "Mt. "))
      if (dist <= 8):
         return(f"Over {name}, {region}")
      if (dist <= 40):
         return(f"Near {name}, {region}")
      return("")

###########################################################################

def geocode(query):
   """
      use:  turns a place name such as "brighton heights, pa" into a
            position, using OpenStreetMap's Nominatim service
      post: returns (lat, lon, label) or None when nothing matches
      imp:  one lookup per run, started by the user, and the answer is
            kept in flightwall_places.json so the same name is never
            looked up twice; see the Nominatim usage policy at
            https://operations.osmfoundation.org/policies/nominatim/
            (at most 1 request a second, an identifying User-Agent, no
            bulk or automatic lookups, credit OpenStreetMap)
   """
   key   = " ".join(query.lower().split())
   path  = data_path("flightwall_places.json")
   saved = {}
   try:
      with open(path, "r", encoding = "utf-8") as handle:
         saved = json.load(handle)
   except (OSError, ValueError):
      pass
   if (key in saved):
      lat, lon, label = saved[key]
      return(lat, lon, label)
   url = (NOMINATIM_URL + "/search?" +
          urllib.parse.urlencode({"q": query, "format": "jsonv2",
                                  "limit": 1}))
   status, reply = http_json(url)
   if ((status != 200) or (not reply)):
      return(None)
   hit   = reply[0]
   found = (float(hit["lat"]), float(hit["lon"]),
            hit.get("display_name") or query)
   saved[key] = list(found)
   try:
      with open(path, "w", encoding = "utf-8") as handle:
         json.dump(saved, handle, indent = 1)
   except OSError:
      pass
   return(found)

###########################################################################
###
### s t a t e    v e c t o r    f e t c h e r s
###
###########################################################################

class OpenSkyFetcher:
   """
      name: OpenSkyFetcher
      use:  adapters/OpenSkyFetcher: OAuth client credentials, bounding
            box query of /api/states/all, radius filter
   """
   def __init__(self, client_id, client_secret):
      self.client_id     = client_id
      self.client_secret = client_secret
      self.token         = ""
      self.token_expiry  = 0.0

   ########################################################################

   def _ensure_token(self, force = False):
                               # refresh 60 s early, as the firmware does
      if ((not force) and self.token and
          (time.time() + 60 < self.token_expiry)):
         return
      body = urllib.parse.urlencode({
                "grant_type"   : "client_credentials",
                "client_id"    : self.client_id,
                "client_secret": self.client_secret}).encode("ascii")
      status, reply = http_json(OPENSKY_TOKEN_URL, data = body, headers = {
                         "Content-Type":
                            "application/x-www-form-urlencoded"})
      if ((status != 200) or (not reply) or
          (not reply.get("access_token"))):
         raise(FetchError(f"OpenSky token request failed (HTTP {status})"))
      self.token        = reply["access_token"]
      self.token_expiry = time.time() + float(reply.get("expires_in", 1800))
      Log.line("OpenSkyFetcher: token refreshed", 1)

   ########################################################################

   def fetch_state_vectors(self, lat, lon, radius_km):
      self._ensure_token()
      lat_d = radius_km / 111.0
      lon_d = radius_km / (111.0 * max(0.01, math.cos(math.radians(lat))))
      url   = (f"{OPENSKY_BASE_URL}/api/states/all"
               f"?lamin={lat - lat_d:.4f}&lomin={lon - lon_d:.4f}"
               f"&lamax={lat + lat_d:.4f}&lomax={lon + lon_d:.4f}")
      for attempt in (1, 2):
         status, reply = http_json(url, headers = {
                            "Authorization": "Bearer " + self.token})
         if ((status == 401) and (attempt == 1)):
            self._ensure_token(True)
            continue
         break
      if (status != 200):
         raise(FetchError(f"OpenSky states/all HTTP {status}"))
      out = []
      for a in (reply or {}).get("states") or []:
         if ((not isinstance(a, list)) or (len(a) < 17) or
             (a[5] is None) or (a[6] is None)):
            continue
         s = StateVector(
                icao24         = a[0] or "",
                callsign       = (a[1] or "").strip(),
                origin_country = a[2] or "",
                lon            = a[5],
                lat            = a[6],
                baro_altitude  = math.nan if (a[7] is None) else a[7],
                on_ground      = bool(a[8]),
                velocity       = math.nan if (a[9] is None) else a[9],
                heading        = math.nan if (a[10] is None) else a[10],
                vertical_rate  = math.nan if (a[11] is None) else a[11],
                squawk         = a[14] or "")
         s.distance_km = haversine_km(lat, lon, s.lat, s.lon)
         if (s.distance_km > radius_km):
            continue
         s.bearing_deg = bearing_deg(lat, lon, s.lat, s.lon)
         out.append(s)
      return(out)

###########################################################################

class AdsbFetcher:
   """
      name: AdsbFetcher
      use:  free, keyless stand-in for OpenSky: airplanes.live point query
            (adsb.lol as fallback); converts to OpenSky's SI units
   """
   def __init__(self):
      self.last = 0.0

   ########################################################################

   def _query(self, path):
      """
         post: returns the feed's aircraft list; raises FetchError when
               no feed answers
      """
      errors = []
      for base in ADSB_FEEDS:
                               # the feeds ask for at most 1 request/s
         time.sleep(max(0.0, self.last + 1.1 - time.time()))
         self.last = time.time()
         try:
            status, reply = http_json(base + path)
         except FetchError as error:
            errors.append(str(error))
            continue
         if (status != 200):
            errors.append(f"{urllib.parse.urlparse(base).hostname}: "
                          f"HTTP {status}")
            continue
         return((reply or {}).get("ac") or [])
      raise(FetchError("; ".join(errors)))

   ########################################################################

   def fetch_state_vectors(self, lat, lon, radius_km):
      radius_nm = max(1, min(250, int(math.ceil(radius_km / 1.852))))
      return(self._convert(
                self._query(f"/point/{lat:.4f}/{lon:.4f}/{radius_nm}"),
                lat, lon, radius_km))

   ########################################################################

   def fetch_by_ident(self, ident, lat, lon):
      """
         use:  finds one aircraft anywhere by callsign, then tail number
         post: returns a list of zero or more state vectors
      """
      for kind in ("callsign", "reg"):
         found = self._convert(
                    self._query(f"/{kind}/{urllib.parse.quote(ident)}"),
                    lat, lon, None)
         if (found):
            return(found)
      return([])

   ########################################################################

   @staticmethod
   def _convert(aircraft, lat, lon, radius_km):
      out = []
      for ac in aircraft:
         if ((ac.get("lat") is None) or (ac.get("lon") is None)):
            continue
         alt = ac.get("alt_baro")
         gs  = ac.get("gs")
         vr  = ac.get("baro_rate", ac.get("geom_rate"))
         s   = StateVector(
                  icao24        = (ac.get("hex") or "").lower(),
                  callsign      = (ac.get("flight") or "").strip(),
                  lat           = ac["lat"],
                  lon           = ac["lon"],
                  on_ground     = (alt == "ground"),
                  baro_altitude = (     alt * 0.3048
                                   if (isinstance(alt, (int, float)))
                                   else math.nan),
                  velocity      = math.nan if (gs is None) else gs * 0.514444,
                  heading       = ac.get("track", math.nan),
                  vertical_rate = math.nan if (vr is None) else vr * 0.00508,
                  squawk        = ac.get("squawk") or "",
                  type_code     = ac.get("t") or "",
                  registration  = ac.get("r") or "")
         s.distance_km = haversine_km(lat, lon, s.lat, s.lon)
                               # no radius: a tracked flight, any distance
         if ((radius_km is not None) and (s.distance_km > radius_km)):
            continue
         s.bearing_deg = bearing_deg(lat, lon, s.lat, s.lon)
         out.append(s)
      return(out)

###########################################################################
###
### f l i g h t    i n f o    f e t c h e r s
###
###########################################################################

class AeroAPIFetcher:
   """
      name: AeroAPIFetcher
      use:  adapters/AeroAPIFetcher: GET /flights/{ident} with x-apikey
      imp:  AeroAPI is billed per call while the firmware asks again
            for every aircraft on every fetch; answers are cached here
   """
   def __init__(self, key, ttl = 1800):
      self.key   = key
      self.ttl   = ttl
      self.cache = TtlCache()

   ########################################################################

   def fetch_flight_info(self, state):
      ident     = state.callsign or state.registration
      hit, info = self.cache.get(ident)
      if (hit):
         return(info)
      try:
         status, reply = http_json(
                            f"{AEROAPI_BASE_URL}/flights/"
                            f"{urllib.parse.quote(ident)}",
                            headers = {"x-apikey": self.key})
      except FetchError as error:
                               # one slow lookup must not sink the
                               # whole fetch; try again in a minute
         Log.line(f"AeroAPIFetcher: {error}")
         self.cache.put(ident, None, 60)
         return(None)
      info = None
      if ((status == 200) and reply and reply.get("flights")):
         info = self._parse(self._pick(reply["flights"]))
      else:
         Log.line(f"AeroAPIFetcher: HTTP {status} for flight {ident}")
      self.cache.put(ident, info, self.ttl if (info) else 600)
      return(info)

   ########################################################################

   @staticmethod
   def _pick(flights):
      """
         imp: the firmware takes flights[0], which can be tomorrow's
              departure; prefer the one in the air now
      """
      for f in flights:
         if (f.get("actual_off") and not f.get("actual_on")):
            return(f)
      return(flights[0])

   ########################################################################

   @staticmethod
   def _parse(f):
      def airport(raw):
         raw = raw if (isinstance(raw, dict)) else {}
         return(AirportInfo(code_icao = raw.get("code_icao") or "",
                            code_iata = raw.get("code_iata") or ""))
      def when(text):
                               # AeroAPI times are ISO 8601 in UTC
         try:
            return(datetime.datetime.fromisoformat(
                      text.replace("Z", "+00:00")).timestamp())
         except (AttributeError, ValueError):
            return(0.0)
      progress = f.get("progress_percent")
      return(FlightInfo(
                ident         = f.get("ident") or "",
                ident_icao    = f.get("ident_icao") or "",
                ident_iata    = f.get("ident_iata") or "",
                operator_code = f.get("operator") or "",
                operator_icao = f.get("operator_icao") or "",
                operator_iata = f.get("operator_iata") or "",
                aircraft_code = f.get("aircraft_type") or "",
                origin        = airport(f.get("origin")),
                destination   = airport(f.get("destination")),
                progress      = (     float(progress)
                                 if (isinstance(progress, (int, float)))
                                 else math.nan),
                off_time      = when(f.get("actual_off") or
                                     f.get("estimated_off")),
                eta           = when(f.get("actual_on") or
                                     f.get("estimated_on") or
                                     f.get("scheduled_on"))))

###########################################################################

class AdsbdbFetcher:
   """
      name: AdsbdbFetcher
      use:  free, keyless stand-in for AeroAPI using adsbdb.com
   """
   def __init__(self):
      self.cache = TtlCache()

   ########################################################################

   def _get(self, path, key):
      hit, value = self.cache.get(key)
      if (hit):
         return(value)
      try:
         status, reply = http_json(ADSBDB_BASE_URL + path, timeout = 8)
      except FetchError as error:
         Log.line(f"AdsbdbFetcher: {error}")
         return(None)
      value = None
      if ((status == 200) and reply and
          isinstance(reply.get("response"), dict)):
         value = reply["response"]
      self.cache.put(key, value, 6 * 3600 if (value) else 3600)
      return(value)

   ########################################################################

   @staticmethod
   def _airport(raw):
      def number(v):
         return(float(v) if (isinstance(v, (int, float))) else math.nan)
      elev = number(raw.get("elevation"))
      return(AirportInfo(code_icao = raw.get("icao_code") or "",
                         code_iata = raw.get("iata_code") or "",
                         lat       = number(raw.get("latitude")),
                         lon       = number(raw.get("longitude")),
                         elev_ft   = 0.0 if (math.isnan(elev)) else elev))

   ########################################################################

   def callsign_for(self, flight_number):
      """
         use:  ICAO callsign for an airline flight number (UA1179 ->
               UAL1179), if adsbdb knows the route
      """
      route = self._get("/callsign/" + urllib.parse.quote(flight_number),
                        "r:" + flight_number)
      if (route and isinstance(route.get("flightroute"), dict)):
         return(route["flightroute"].get("callsign_icao") or "")
      return("")

   ########################################################################

   def fetch_flight_info(self, state):
      callsign = state.callsign
                               # a callsign that is just the tail number
                               # (private/GA) has no route to look up
      if ((not callsign) or
          (callsign.replace("-", "") ==
           state.registration.replace("-", ""))):
         return(None)
      route = self._get("/callsign/" + urllib.parse.quote(callsign),
                        "r:" + callsign)
      if ((not route) or (not isinstance(route.get("flightroute"),
                                         dict))):
         return(None)
      raw     = route["flightroute"]
      airline = raw.get("airline") or {}
      org     = raw.get("origin") or {}
      dst     = raw.get("destination") or {}
      info    = FlightInfo(
                   ident         = callsign,
                   ident_icao    = raw.get("callsign_icao") or callsign,
                   ident_iata    = raw.get("callsign_iata") or "",
                   operator_code = airline.get("icao") or "",
                   operator_icao = airline.get("icao") or "",
                   operator_iata = airline.get("iata") or "",
                   aircraft_code = state.type_code,
                   airline_display_name_full = airline.get("name") or "",
                   origin        = self._airport(org),
                   destination   = self._airport(dst))
      if ((not info.aircraft_code) and state.icao24):
         ac = self._get("/aircraft/" + state.icao24, "a:" + state.icao24)
         if (ac and isinstance(ac.get("aircraft"), dict)):
            info.aircraft_code = ac["aircraft"].get("icao_type") or ""
      return(info)

###########################################################################

class FlightWallFetcher:
   """
      name: FlightWallFetcher
      use:  adapters/FlightWallFetcher: display names from the CDN
   """
   def __init__(self):
      self.cache = TtlCache()

   ########################################################################

   def _get(self, kind, code):
      key        = f"{kind}:{code}"
      hit, value = self.cache.get(key)
      if (hit):
         return(value)
      try:
         status, reply = http_json(f"{FLIGHTWALL_CDN}/oss/lookup/{kind}/"
                                   f"{urllib.parse.quote(code)}.json",
                                   timeout = 8)
         value = reply if ((status == 200) and
                           isinstance(reply, dict)) else {}
         self.cache.put(key, value, 24 * 3600)
      except FetchError as error:
         Log.line(f"FlightWallFetcher: {error}", 1)
         value = {}
         self.cache.put(key, value, 300)
      return(value)

   ########################################################################

   def airline_name(self, icao):
      return(self._get("airline", icao).get("display_name_full") or "")

   ########################################################################

   def aircraft_name(self, code):
      return(self._get("aircraft", code).get("display_name_short") or "")

###########################################################################
###
### l o g o s
###
###########################################################################

LOGO_SIZE = 26
GREEN     = (40, 230, 70)
DIM_GREEN = (20, 80, 30)

def png_decode(data):
   """
      use:  minimal PNG reader so no imaging package is needed
      post: returns (width, height, rows of (r, g, b, a) tuples)
      imp:  handles every non-interlaced colour type and bit depth;
            raises ValueError for interlaced or damaged files
   """
   if (data[:8] != b"\x89PNG\r\n\x1a\n"):
      raise(ValueError("not a PNG"))
   pos    = 8
   idat   = []
   plte   = b""
   trns   = None
   header = None
   while (pos + 8 <= len(data)):
      length = struct.unpack(">I", data[pos:pos + 4])[0]
      kind   = data[pos + 4:pos + 8]
      body   = data[pos + 8:pos + 8 + length]
      pos   += 12 + length
      if (kind == b"IHDR"):
         header = struct.unpack(">IIBBBBB", body)
      elif (kind == b"PLTE"):
         plte = body
      elif (kind == b"tRNS"):
         trns = body
      elif (kind == b"IDAT"):
         idat.append(body)
      elif (kind == b"IEND"):
         break
   if (header is None):
      raise(ValueError("no IHDR"))
   width, height, depth, ctype, comp, filt, interlace = header
   if (interlace or (ctype not in (0, 2, 3, 4, 6)) or
       (depth not in (1, 2, 4, 8, 16))):
      raise(ValueError("unsupported PNG layout"))
   channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
   bits     = channels * depth
   bpp      = max(1, bits // 8)
   stride   = (width * bits + 7) // 8
   raw      = zlib.decompress(b"".join(idat))
   if (len(raw) < height * (stride + 1)):
      raise(ValueError("truncated PNG"))
   prev = bytearray(stride)
   rows = []
   for y in range(height):
      start = y * (stride + 1)
      ftype = raw[start]
      line  = bytearray(raw[start + 1:start + 1 + stride])
                               # undo the per-row filter
      for i in range(stride):
         a = line[i - bpp] if (i >= bpp) else 0
         b = prev[i]
         c = prev[i - bpp] if (i >= bpp) else 0
         if (ftype == 1):
            line[i] = (line[i] + a) & 255
         elif (ftype == 2):
            line[i] = (line[i] + b) & 255
         elif (ftype == 3):
            line[i] = (line[i] + ((a + b) >> 1)) & 255
         elif (ftype == 4):
            p  = a + b - c
            pa = abs(p - a)
            pb = abs(p - b)
            pc = abs(p - c)
            line[i] = (line[i] + (     a if ((pa <= pb) and (pa <= pc))
                                  else b if (pb <= pc)
                                  else c)) & 255
      prev = line
                               # split into samples, scaled to 0..255
      if (depth == 16):
         samples = [line[i] for i in range(0, stride, 2)]
      elif (depth == 8):
         samples = list(line)
      else:
         mask    = (1 << depth) - 1
         samples = [(line[i * depth // 8] >>
                     (8 - depth - (i * depth) % 8)) & mask
                    for i in range(width * channels)]
         if (ctype == 0):
            samples = [s * 255 // mask for s in samples]
      pixels = []
      for x in range(width):
         s = samples[x * channels:(x + 1) * channels]
         if (ctype == 3):
            idx   = s[0]
            rgb   = tuple(plte[idx * 3:idx * 3 + 3]) or (0, 0, 0)
            alpha = (     trns[idx] if (trns and (idx < len(trns)))
                     else 255)
            pixels.append(rgb + (alpha,))
         elif (ctype == 0):
            pixels.append((s[0], s[0], s[0], 255))
         elif (ctype == 4):
            pixels.append((s[0], s[0], s[0], s[1]))
         elif (ctype == 2):
            pixels.append((s[0], s[1], s[2], 255))
         else:
            pixels.append(tuple(s))
      rows.append(pixels)
   return(width, height, rows)

###########################################################################

def logo_pixels(width, height, rows, size = LOGO_SIZE):
   """
      use:  shrinks an image to size x size LEDs by averaging, on black
      post: rows of (r, g, b), or None where the LED would be (near) off
   """
   scale = max(width, height) / float(size)
   out   = []
   for ty in range(size):
      line = []
      for tx in range(size):
         x0 = int(tx * scale)
         x1 = max(x0 + 1, int((tx + 1) * scale))
         y0 = int(ty * scale)
         y1 = max(y0 + 1, int((ty + 1) * scale))
         r = g = b = n = 0
         for y in range(y0, min(y1, height)):
            for x in range(x0, min(x1, width)):
               pr, pg, pb, pa = rows[y][x]
               r += pr * pa
               g += pg * pa
               b += pb * pa
               n += 1
         if (n == 0):
            line.append(None)
            continue
         rgb = (r // (n * 255), g // (n * 255), b // (n * 255))
         line.append(rgb if (max(rgb) >= 24) else None)
      out.append(line)
   return(out)

###########################################################################

class LogoFetcher:
   """
      name: LogoFetcher
      use:  airline logos, from the "logos" folder beside the script or
            the logo_url template, decoded and shrunk for the panel
   """
   def __init__(self, url_template, folder):
      self.url_template = url_template
      self.folder       = folder
      self.cache        = TtlCache()

   ########################################################################

   def get(self, info):
      icao = info.operator_icao.upper()
      iata = info.operator_iata.upper()
      key  = icao or iata
      if (not key):
         return(None)
      hit, value = self.cache.get(key)
      if (hit):
         return(value)
      data = None
      for code in (icao, iata):
         path = os.path.join(self.folder, code + ".png")
         if (code and os.path.isfile(path)):
            with open(path, "rb") as handle:
               data = handle.read()
            break
      ok = True
      if ((data is None) and self.url_template and
          ((iata or "{iata}" not in self.url_template) and
           (icao or "{icao}" not in self.url_template))):
         url = (self.url_template.replace("{iata}", iata)
                                 .replace("{icao}", icao))
         request = urllib.request.Request(url, headers = {
                      "User-Agent": USER_AGENT})
         try:
            with urllib.request.urlopen(request, timeout = 8) as reply:
               data = reply.read()
         except urllib.error.HTTPError:
            pass
         except (urllib.error.URLError, OSError) as error:
            Log.line(f"LogoFetcher: {error}")
            ok = False
      value = None
      if (data):
         try:
            value = logo_pixels(*png_decode(data))
         except (ValueError, zlib.error, struct.error,
                 IndexError) as error:
            Log.line(f"LogoFetcher: {key}: {error}")
      self.cache.put(key, value, (     7 * 86400 if (value)
                                  else 86400     if (ok)
                                  else 300))
      return(value)

###########################################################################

def demo_logo(color, style):
   """
      use:  invented logo art for demo mode (no real airline's logo)
   """
   size = LOGO_SIZE
   art  = Matrix(size, size)
   half = (size - 1) / 2.0
   for y in range(size):
      for x in range(size):
         if (style == 0):
                               # tail fin on a white tile
            inside = (min(x, size - 1 - x, y, size - 1 - y) >= 0)
            corner = (math.hypot(max(0, abs(x - half) - 9),
                                 max(0, abs(y - half) - 9)) > 3.5)
            if (inside and not corner):
               art.pixel(x, y, (255, 255, 255))
            if ((5 <= y <= 22) and (x >= 6 + (22 - y) * 0.5) and
                (x <= 12 + (22 - y) * 0.6)):
               art.pixel(x, y, color)
            if ((9 <= y <= 15) and
                (abs(x - (11 + (22 - y) * 0.55)) < 1.2)):
               art.pixel(x, y, (250, 180, 20))
         else:
                               # disc with light arcs
            r = math.hypot(x - half, y - half)
            if (r <= half + 0.3):
               art.pixel(x, y, color)
               for arc in (6.5, 9.5, 12.5):
                  if ((abs(math.hypot(x - half, y - half - 6) - arc)
                       < 0.8) and (y < half + 2)):
                     art.pixel(x, y, (230, 245, 245))
   return(art.pixels)

###########################################################################
###
### d e m o
###
###########################################################################

                               # callsign, airline ICAO, IATA, name, type,
                               # type name, origin, destination, colour,
                               # logo style, what the flight is doing
DEMO = [
   ("UAL1179", "UAL", "UA", "United", "B38M", "737 MAX 8",
    ("KEWR", "EWR", 40.69, -74.17, 18), ("KDEN", "DEN", 39.86, -104.67,
                                         5434),
    (40, 60, 170), 0, "cruise"),
   ("DAL402",  "DAL", "DL", "Delta", "B739", "737-900",
    ("KATL", "ATL", 33.64, -84.43, 1026), ("KBOS", "BOS", 42.36, -71.01,
                                           20),
    (0, 120, 120), 1, "approach"),
   ("SWA2145", "SWA", "WN", "Southwest", "B737", "737-700",
    ("KMDW", "MDW", 41.79, -87.75, 620), ("KPIT", "PIT", 40.49, -80.23,
                                          1203),
    (200, 90, 20), 1, "landing"),
   ("ASA331",  "ASA", "AS", "Alaska", "A320", "A320",
    ("KSEA", "SEA", 47.45, -122.31, 433), ("KSFO", "SFO", 37.62, -122.38,
                                           13),
    (20, 110, 60), 0, "landed"),
   ("UAL2291", "UAL", "UA", "United", "A320", "A320",
    ("KPIT", "PIT", 40.49, -80.23, 1203), ("KORD", "ORD", 41.97, -87.91,
                                           672),
    (40, 60, 170), 0, "departing"),
   ("N512GA",  "",    "",   "", "GLF5", "G550", None, None, None, 0,
    "cruise")]

DEMO_PLACES = ["Over Davenport, IA", "Over Moon Twp, PA",
               "Near Erie, PA", "Over Mansfield, OH", "Over Peoria, IL"]

class DemoPlaces:
   """
      name: DemoPlaces
      use:  made-up "Over <town>" text for demo mode
   """
   @staticmethod
   def describe(lat, lon):
      return(DEMO_PLACES[int(abs(lat * 7 + lon * 3)) % len(DEMO_PLACES)])

###########################################################################

class DemoFetcher:
   """
      name: DemoFetcher
      use:  made-up flights in every phase (cruise, departing,
            approaching, landing, landed) for trying the display with no
            network; serves as state vector and flight info fetcher
   """
   def _state(self, row, lat, lon):
      org   = row[6]
      dst   = row[7]
      phase = row[10]
      s     = StateVector(callsign      = row[0],
                          icao24        = "d" + row[0][-5:].lower(),
                          type_code     = row[4],
                          registration  = "" if (org) else row[0])
      if (org is None):
         s.lat, s.lon = lat + 0.03, lon - 0.04
         s.baro_altitude, s.velocity = 12497.0, 246.0
         s.heading, s.vertical_rate = 92.0, 0.0
      else:
                               # distance along the route by phase
         total = haversine_km(org[2], org[3], dst[2], dst[3])
         along = {"cruise"   : 0.48 * total,
                  "departing": 18.0,
                  "approach" : total - 45.0,
                  "landing"  : total - 7.0,
                  "landed"   : total}[phase]
         f     = along / total
         s.lat = org[2] + (dst[2] - org[2]) * f
         s.lon = org[3] + (dst[3] - org[3]) * f
         s.heading = (     bearing_deg(s.lat, s.lon, dst[2], dst[3])
                      if (phase != "landed")
                      else 130.0)
         s.baro_altitude, s.velocity, s.vertical_rate = {
            "cruise"   : (10972.8, 230.0, 0.0),
            "departing": (2560.0,  149.0, 11.7),
            "approach" : (1981.0,  128.6, -6.1),
            "landing"  : (dst[4] * 0.3048 + 335.0, 73.0, -3.6),
            "landed"   : (dst[4] * 0.3048, 9.3, 0.0)}[phase]
         s.on_ground = (phase == "landed")
      s.distance_km = haversine_km(lat, lon, s.lat, s.lon)
      s.bearing_deg = bearing_deg(lat, lon, s.lat, s.lon)
      return(s)

   ########################################################################

   def fetch_state_vectors(self, lat, lon, radius_km):
      return([self._state(row, lat, lon) for row in DEMO])

   ########################################################################

   def fetch_by_ident(self, ident, lat, lon):
      return([self._state(row, lat, lon) for row in DEMO
              if (ident == row[0])])

   ########################################################################

   def callsign_for(self, flight_number):
      for row in DEMO:
         if (row[2] and (flight_number == row[2] + row[0][3:])):
            return(row[0])
      return("")

   ########################################################################

   def fetch_flight_info(self, state):
      for row in DEMO:
         if ((row[0] == state.callsign) and row[1]):
            org, dst = row[6], row[7]
            ap = lambda a: AirportInfo(a[0], a[1], a[2], a[3], a[4])
            flown = {"cruise": 120, "departing": 6, "approach": 130,
                     "landing": 75, "landed": 140}[row[10]]
            return(FlightInfo(
                      ident = row[0], ident_icao = row[0],
                      ident_iata = row[2] + row[0][3:],
                      operator_code = row[1], operator_icao = row[1],
                      operator_iata = row[2], aircraft_code = row[4],
                      airline_display_name_full   = row[3],
                      aircraft_display_name_short = row[5],
                      origin      = ap(org),
                      destination = ap(dst),
                      off_time    = time.time() - flown * 60,
                      logo        = demo_logo(row[8], row[9])))
      return(None)

###########################################################################
###
### o r c h e s t r a t i o n
###
###########################################################################

class FlightDataFetcher:
   """
      name: FlightDataFetcher
      use:  core/FlightDataFetcher: state vectors -> flight info ->
            display names
   """
   def __init__(self, states, flights, cdn, settings, logos = None,
                resolver = None, places = None, airports = None):
      self.states   = states
      self.flights  = flights
      self.cdn      = cdn
      self.settings = settings
      self.logos    = logos
      self.resolver = resolver
      self.places   = places
      self.airports = airports

   ########################################################################

   def fetch_flights(self):
      """
         post: returns (state vectors, enriched flights) nearest first
      """
      st     = self.settings
      states = self.states.fetch_state_vectors(st["lat"], st["lon"],
                                               st["radius_km"])
      states.sort(key = lambda s: s.distance_km)
      flights = []
      for s in states:
         if (not s.callsign):
            continue
         info = self.flights.fetch_flight_info(s)
         if ((info is None) and st["show_unidentified"]):
            info = FlightInfo(ident = s.callsign,
                              aircraft_code = s.type_code)
         if (info is None):
            continue
         flights.append(self._enrich(info, s))
      return(states, flights)

   ########################################################################

   def fetch_tracked(self, idents):
      """
         use:  -t: looks up each chosen flight wherever it is
         post: returns (state vectors, flights); a flight that is not
               airborne or not found comes back with missing = True
      """
      st      = self.settings
      states  = []
      flights = []
      for ident in idents:
         found = self.states.fetch_by_ident(ident, st["lat"], st["lon"])
         if ((not found) and self.resolver and
             re.match(r"^[A-Z0-9]{2}[0-9]{1,4}[A-Z]?$", ident)):
                               # an airline flight number such as UA1179:
                               # look for its ICAO callsign (UAL1179)
            callsign = self.resolver.callsign_for(ident)
            if (callsign and (callsign != ident)):
               found = self.states.fetch_by_ident(callsign, st["lat"],
                                                  st["lon"])
         if (not found):
            flights.append(FlightInfo(ident = ident, missing = True))
            continue
         s = found[0]
         states.append(s)
         info = None
         if (s.callsign):
            info = self.flights.fetch_flight_info(s)
         if (info is None):
            info = FlightInfo(ident = s.callsign or s.registration or ident,
                              aircraft_code = s.type_code)
         flights.append(self._enrich(info, s))
      return(states, flights)

   ########################################################################

   def _enrich(self, info, s):
      """
         use:  attaches the position, display names, logo and progress
      """
      info.state = s
      if (self.cdn):
         if (info.operator_icao):
            name = self.cdn.airline_name(info.operator_icao)
            if (name):
               info.airline_display_name_full = name
         if (info.aircraft_code):
            name = self.cdn.aircraft_name(info.aircraft_code)
            if (name):
               info.aircraft_display_name_short = name
      if (self.logos and (info.logo is None)):
         info.logo = self.logos.get(info)
      if (self.airports):
                               # AeroAPI gives codes only; positions and
                               # elevations come from OurAirports
         for ap in (info.origin, info.destination):
            if (math.isnan(ap.lat) and ap.code_icao):
               found = self.airports.icao(ap.code_icao)
               if (found):
                  ap.lat     = found["lat"]
                  ap.lon     = found["lon"]
                  ap.elev_ft = found["elev"]
      if (self.places and (not math.isnan(s.lat))):
         info.where = self.places.describe(s.lat, s.lon)
      self._progress(info)
      return(info)

   ########################################################################

   @staticmethod
   def _progress(info):
      """
         post: fills progress and eta from the airports' positions when
               the source (adsbdb) gave them; AeroAPI's own values stay
      """
      s   = info.state
      org = info.origin
      dst = info.destination
      if (math.isnan(org.lat) or math.isnan(dst.lat) or math.isnan(s.lat)):
         return
      done = haversine_km(org.lat, org.lon, s.lat, s.lon)
      left = haversine_km(s.lat, s.lon, dst.lat, dst.lon)
      if (done + left <= 0):
         return
      if (math.isnan(info.progress)):
         info.progress = 100.0 * done / (done + left)
      if ((not info.eta) and (not s.on_ground) and
          (not math.isnan(s.velocity)) and (s.velocity > 25)):
         info.eta = time.time() + left * 1000.0 / s.velocity

###########################################################################
###
### m a t r i x
###
###########################################################################

class Matrix:
   """
      name: Matrix
      use:  frame buffer with the few Adafruit GFX calls the firmware
            uses: fillScreen, drawRect, setCursor + write (classic font)
   """
   def __init__(self, width, height):
      self.width  = width
      self.height = height
      self.pixels = [[None] * width for row in range(height)]

   ########################################################################

   def fill_screen(self):
      for row in self.pixels:
         row[:] = [None] * self.width

   ########################################################################

   def pixel(self, x, y, color):
      if ((0 <= x < self.width) and (0 <= y < self.height)):
         self.pixels[y][x] = color

   ########################################################################

   def draw_rect(self, x, y, w, h, color):
      for i in range(x, x + w):
         self.pixel(i, y, color)
         self.pixel(i, y + h - 1, color)
      for j in range(y, y + h):
         self.pixel(x, j, color)
         self.pixel(x + w - 1, j, color)

   ########################################################################

   def draw_text(self, x, y, text, color):
      """
         imp: 5x7 glyph plus one blank column = 6 px per character;
              text wrap is off, so anything past the edge is clipped
      """
      for ch in text:
         code = ord(ch)
         if (ch in FONT_EXTRA):
            glyph = FONT_EXTRA[ch]
         else:
            if (not (32 <= code <= 126)):
               code = ord("?")
            glyph = FONT_5X7[(code - 32) * 5:(code - 32) * 5 + 5]
         for col, bits in enumerate(glyph):
            for row in range(8):
               if ((bits >> row) & 1):
                  self.pixel(x + col, y + row, color)
         x += 6

   ########################################################################

   def draw_tiny(self, x, y, text, color):
      """
         use:  3x5 font for the takeoff/landing times beside the bar
         usage: digits, ":", "A" and "P" only
      """
      for ch in text:
         if (ch == ":"):
            self.pixel(x, y + 1, color)
            self.pixel(x, y + 3, color)
            x += 2
            continue
         bits = TINY_3X5.get(ch)
         if (bits):
            for row in range(5):
               for col in range(3):
                  if (bits[row * 3 + col] == "1"):
                     self.pixel(x + col, y + row, color)
         x += 4

   ########################################################################

   @staticmethod
   def tiny_width(text):
      return(sum(2 if (ch == ":") else 4 for ch in text) - 1)

   ########################################################################

   def draw_image(self, x, y, rows):
      """
         usage: rows of (r, g, b) or None (unlit), as logo_pixels gives
      """
      for j, line in enumerate(rows):
         for i, color in enumerate(line):
            if (color is not None):
               self.pixel(x + i, y + j, color)

   ########################################################################

   def key(self):
      return(tuple(tuple(row) for row in self.pixels))

###########################################################################

def truncate_to_columns(text, max_columns):
   """
      use: NeoMatrixDisplay::truncateToColumns
   """
   if (len(text) <= max_columns):
      return(text)
   if (max_columns <= 3):
      return(text[:max_columns])
   return(text[:max_columns - 3] + "...")

###########################################################################

class NeoMatrixDisplay:
   """
      name: NeoMatrixDisplay
      use:  adapters/NeoMatrixDisplay: the firmware's bordered
            three-line card (layout = classic) or the 128x64 wall layout,
            plus loading screen and one-line messages
   """
   def __init__(self, width, height, color, settings):
      self.matrix   = Matrix(width, height)
      self.color    = color
      self.settings = settings

   ########################################################################

   def card_lines(self, f):
      """
         post: returns the three lines the firmware draws: airline,
               origin>destination, aircraft
      """
      airline = (f.airline_display_name_full or f.operator_iata or
                 f.operator_icao or f.operator_code or f.ident)
      if (self.settings["route_codes"] == "iata"):
         org = f.origin.code_iata or f.origin.code_icao
         dst = f.destination.code_iata or f.destination.code_icao
      else:
         org = f.origin.code_icao
         dst = f.destination.code_icao
      route    = (org + ">" + dst) if (org or dst) else ""
      aircraft = f.aircraft_display_name_short or f.aircraft_code
      return([airline, route, aircraft])

   ########################################################################

   def readings(self, f):
      """
         post: returns {"alt", "spd", "trk", "vr"} as display strings in
               the configured units; "--" when the feed has no value
      """
      s   = f.state
      st  = self.settings
      out = {}
      if (s.on_ground):
         out["alt"] = "GND"
      elif (math.isnan(s.baro_altitude)):
         out["alt"] = "--"
      elif (st["altitude_unit"] == "m"):
         out["alt"] = f"{s.baro_altitude:,.0f}M"
      else:
                               # ADS-B reports in 25 ft steps; rounding
                               # hides the metres round trip (OpenSky)
         feet       = round(s.baro_altitude / 0.3048 / 25.0) * 25
         out["alt"] = f"{feet:,.0f}FT"
      v    = s.velocity
      unit = st["speed_unit"]
      if (math.isnan(v)):
         out["spd"] = "--"
      elif (unit == "mach"):
                               # sea-level speed of sound; close enough
         out["spd"] = f"M{v / 340.29:.2f}"
      else:
         factor, label = {"kt" : (1.943844, "KT"),
                          "mph": (2.236936, "MPH"),
                          "kmh": (3.6,      "KM/H"),
                          "mps": (1.0,      "M/S")}[unit]
         out["spd"] = f"{v * factor:,.0f}{label}"
      t = s.heading
      if (math.isnan(t)):
         out["trk"] = "--"
      else:
         card = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][
                   int(((t % 360) + 22.5) // 45) % 8]
         out["trk"] = {"deg" : f"{t % 360:03.0f}°",
                       "card": card,
                       "both": f"{t % 360:03.0f}° {card}"}[st["track_unit"]]
      r = s.vertical_rate
      if (s.on_ground):
         out["vr"] = ""
      elif (math.isnan(r)):
         out["vr"] = "--"
      else:
                               # under ~64 ft/min is just noise: level
         arrow = (     "↑" if (r > 0.33)
                  else "↓" if (r < -0.33)
                  else "")
         if (not arrow):
            out["vr"] = "LEVEL"
         elif (st["vertical_rate_unit"] == "mps"):
            out["vr"] = f"{arrow}{abs(r):.1f}M/S"
         else:
            out["vr"] = f"{arrow}{abs(r) * 196.8504:,.0f}FPM"
      return(out)

   ########################################################################

   @staticmethod
   def lr(left, right, width):
      """
         post: left and right ends of one line, padded to width
      """
      left = left[:max(0, width - len(right) - 1)] if (right) else left
      return(left + " " * max(1, width - len(left) - len(right)) + right
             if (right) else left[:width])

   ########################################################################

   def code(self, ap):
      """
         post: an airport code in the configured style (KPIT or PIT)
      """
      if (self.settings["route_codes"] == "iata"):
         return(ap.code_iata or ap.code_icao)
      return(ap.code_icao or ap.code_iata)

   ########################################################################

   def flight_label(self, f):
      """
         post: the flight number (UA1179) if known, else the callsign,
               else the airline
      """
      return(f.ident_iata or f.ident or f.ident_icao or
             f.airline_display_name_full or f.state.registration)

   ########################################################################

   def route_text(self, f):
      org = self.code(f.origin)
      dst = self.code(f.destination)
      return(f"{org}>{dst}" if (org and dst) else "")

   ########################################################################

   @staticmethod
   def clock(epoch):
      """
         post: "6:52P"-style local time, or "" when unknown
      """
      if (not epoch):
         return("")
      t = time.localtime(epoch)
      return(f"{t.tm_hour % 12 or 12}:{t.tm_min:02d}"
             f"{'A' if (t.tm_hour < 12) else 'P'}")

   ########################################################################

   def phase(self, f):
      """
         use:  what the plane is doing, for the bottom line
         post: returns (text, green); green for approaching, landing
               and landed
      """
      s   = f.state
      org = f.origin
      dst = f.destination
      vr  = 0.0 if (math.isnan(s.vertical_rate)) else s.vertical_rate
      to_dst = (     None if (math.isnan(dst.lat) or math.isnan(s.lat))
                else haversine_km(s.lat, s.lon, dst.lat, dst.lon))
      to_org = (     None if (math.isnan(org.lat) or math.isnan(s.lat))
                else haversine_km(s.lat, s.lon, org.lat, org.lon))
      if (s.on_ground):
         if ((to_dst is not None) and (to_dst <= 8)):
            return(f"Landed at {self.code(dst)}", True)
         if ((to_org is not None) and (to_org <= 8)):
            return(f"At {self.code(org)}", False)
         return("On the ground", False)
      if (to_dst is not None):
                               # height above the destination's runways
         agl = (     None if (math.isnan(s.baro_altitude))
                else s.baro_altitude / 0.3048 - dst.elev_ft)
         if ((to_dst <= 15) and (agl is not None) and (agl <= 3000) and
             (vr <= 0.5)):
            return(f"Landing at {self.code(dst)}", True)
                               # descending faster than ~200 ft/min
         if ((to_dst <= 60) and (vr < -1.0)):
            return(f"Approaching {self.code(dst)}", True)
      if ((to_org is not None) and (to_org <= 40) and (vr > 1.0)):
         return(f"Departing {self.code(org)}", False)
      if (f.where):
         return(f.where, False)
      if (to_dst is not None):
         return(f"En route to {self.code(dst)}", False)
      return("", False)

   ########################################################################

   def pages(self, f):
      """
         post: the pages shown for flight f: "wall" (the 128x64 layout),
               "card" (the firmware's) or "missing"
      """
      if (f.missing):
         return(["missing"])
      return(["card"] if (self.settings["layout"] == "classic")
             else ["wall"])

   ########################################################################

   def text_lines(self, f):
      """
         use:  the flight as plain text, for -o and the text style
         post: the first three lines are the top lines (any may be ""),
               then readings, times and what the plane is doing
      """
      if (f.missing):
         return([f.ident, "not airborne or not found", ""])
      if (self.settings["layout"] == "classic"):
         return(self.card_lines(f))
      r     = self.readings(f)
      pct   = "" if (math.isnan(f.progress)) else f" {f.progress:.0f}%"
      route = self.route_text(f)
      lines = [self.flight_label(f),
               (route + pct) if (route) else "Route unknown",
               f.aircraft_display_name_short or f.aircraft_code,
               (f"{r['alt']}  {r['spd']}  {r['trk']}  {r['vr']}")
                  .replace("↑", "+").replace("↓", "-")
                  .replace("°", "").strip()]
      off = self.clock(f.off_time)
      on  = self.clock(f.eta)
      if (route and (off or on)):
         lines.append(f"OFF {off or '--'}  ON {on or '--'}")
      lines.append(self.phase(f)[0])
      return(lines)

   ########################################################################

   def show_flight(self, f, page = "wall"):
      m = self.matrix
      w = m.width
      h = m.height
      m.fill_screen()
      m.draw_rect(0, 0, w, h, self.color)
      if (page == "wall"):
         self.show_wall(f)
         return
      padding  = 2
      inner_w  = w - 2 - 2 * padding
      inner_h  = h - 2 - 2 * padding
      max_cols = inner_w // 6
      total    = 3 * 8 + 2 * 1
      y        = 1 + padding + (inner_h - total) // 2
      lines    = (     [f.ident, "NOT FOUND", "OR NOT AIRBORNE"]
                  if (page == "missing")
                  else self.card_lines(f))
      for line in lines:
         m.draw_text(1 + padding, y, truncate_to_columns(line, max_cols),
                     self.color)
         y += 8 + 1

   ########################################################################

   def show_wall(self, f):
      """
         use:  the 128x64 layout: logo, flight / route and % / aircraft
               beside it; green progress bar with takeoff and landing
               times; altitude and speed; track and vertical rate; what
               the plane is doing
      """
      m = self.matrix
      w = m.width
      x = 3
      if (self.settings["show_logo"] and f.logo):
         m.draw_image(3, 3, f.logo)
         x = 3 + LOGO_SIZE + 3
      cols  = (w - 3 - x) // 6
      route = self.route_text(f)
      pct   = "" if (math.isnan(f.progress)) else f"{f.progress:.0f}%"
      lines = [self.flight_label(f),
               self.lr(route, pct, cols) if (route) else "Route unknown",
               f.aircraft_display_name_short or f.aircraft_code]
      for i, line in enumerate(lines):
         m.draw_text(x, 3 + 9 * i, truncate_to_columns(line, cols),
                     self.color)
      if (route):
         self.draw_bar(32, f)
      full = (w - 6) // 6
      r    = self.readings(f)
      text, green = self.phase(f)
      m.draw_text(3, 36, self.lr(r["alt"], r["spd"], full), self.color)
      m.draw_text(3, 45, self.lr(r["trk"], r["vr"], full), self.color)
      m.draw_text(3, 54, truncate_to_columns(text, full),
                  GREEN if (green) else self.color)

   ########################################################################

   def draw_bar(self, y, f):
      """
         use:  3 px progress bar: dim green track, green flown part, a
               3x3 marker for the plane; small takeoff and landing
               times at the ends when known (room is kept for them so
               the bar is the same length on every flight)
      """
      m   = self.matrix
      off = self.clock(f.off_time)
      on  = self.clock(f.eta)
      x0  = 3
      x1  = m.width - 4
      if (off or on):
         x0 += 22
         x1 -= 22
      if (off):
         m.draw_tiny(3, y - 2, off, self.color)
      if (on):
         m.draw_tiny(m.width - 4 - m.tiny_width(on), y - 2, on, self.color)
      for i in range(x0, x1 + 1):
         m.pixel(i, y, DIM_GREEN)
      if (math.isnan(f.progress)):
         return
      fill = int((x1 - x0 - 2) * max(0.0, min(100.0, f.progress)) / 100.0)
      for i in range(x0, x0 + fill):
         m.pixel(i, y, GREEN)
      for j in range(y - 1, y + 2):
         for i in range(x0 + fill, x0 + fill + 3):
            m.pixel(i, j, GREEN)

   ########################################################################

   def show_loading(self):
      m = self.matrix
      m.fill_screen()
      m.draw_rect(0, 0, m.width, m.height, (255, 255, 255))
      m.draw_text((m.width - 3 * 6) // 2, (m.height - 8) // 2 - 2, "...",
                  self.color)

   ########################################################################

   def show_message(self, message):
      m = self.matrix
      m.fill_screen()
      m.draw_text(0, (m.height - 6) // 2,
                  truncate_to_columns(message, m.width // 6), self.color)

###########################################################################
###
### t e r m i n a l
###
###########################################################################

def enable_ansi():
   """
      use:  lets cmd.exe/conhost understand ANSI colour codes
      post: returns False when the console refuses (very old Windows)
      imp:  Windows 10 1511+ supports them once
            ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) is set
   """
   try:
      sys.stdout.reconfigure(encoding = "utf-8")
   except (AttributeError, ValueError):
      pass
   if (os.name != "nt"):
      return(True)
   import ctypes
   kernel32 = ctypes.windll.kernel32
   handle   = kernel32.GetStdHandle(-11)
   mode     = ctypes.c_uint32()
   if (not kernel32.GetConsoleMode(handle, ctypes.byref(mode))):
      return(False)
   return(bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004)))

###########################################################################

def box_weights(n_src, n_dst):
   """
      post: for each of n_dst cells, [(source index, weight)] covering
            its share of the n_src cells; weights add up to 1
   """
   ratio = n_src / float(n_dst)
   out   = []
   for t in range(n_dst):
      a     = t * ratio
      b     = (t + 1) * ratio
      parts = []
      i     = int(math.floor(a))
      while ((i < b) and (i < n_src)):
         w = min(b, i + 1) - max(a, i)
         if (w > 1e-9):
            parts.append((i, w / ratio))
         i += 1
      out.append(parts)
   return(out)

###########################################################################

def shrink_pixels(pixels, width, height, new_width, new_height):
   """
      use:  scales a frame down by area-averaging, so thin strokes fade
            rather than vanish; a mild boost keeps text bright
      post: rows of (r, g, b), or None where the LED would be (near) off
   """
   wx  = box_weights(width, new_width)
   wy  = box_weights(height, new_height)
   out = []
   for ty in range(new_height):
      row = []
      for tx in range(new_width):
         r = g = b = 0.0
         for sy, ay in wy[ty]:
            line = pixels[sy]
            for sx, ax in wx[tx]:
               c = line[sx]
               if (c is not None):
                  k  = ay * ax
                  r += c[0] * k
                  g += c[1] * k
                  b += c[2] * k
         rgb = (min(255, int(r * 1.35)), min(255, int(g * 1.35)),
                min(255, int(b * 1.35)))
         row.append(rgb if (max(rgb) >= 20) else None)
      out.append(row)
   return(out)

###########################################################################

class TerminalRenderer:
   """
      name: TerminalRenderer
      use:  paints the matrix with "upper half block" characters: one
            terminal cell = two vertically stacked LEDs, so a 128x64
            panel needs 128 columns by 32 rows (plus a status line)
   """
   OFF = (22, 22, 22)

   def __init__(self, brightness, min_scale = 0.75):
      self.scale     = max(0, min(255, brightness)) / 255.0
      self.min_scale = max(0.1, min(1.0, min_scale))
      self.last      = None

   ########################################################################

   def start(self):
                               # alternate screen, hide cursor, clear
      sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
      sys.stdout.flush()

   ########################################################################

   def stop(self):
      sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
      sys.stdout.flush()

   ########################################################################

   def _rgb(self, color):
      if (color is None):
         return(self.OFF)
      return(tuple(max(self.OFF[i], int(color[i] * self.scale))
                   for i in range(3)))

   ########################################################################

   def draw(self, matrix, status):
      """
         use:  paints the panel centred in the terminal
         imp:  a smaller window shrinks the panel to fit, keeping its
               shape, but not below min_scale (where the text would
               become unreadable); below that the panel sits at the top
               left and its right and/or bottom edge is cut off. It never
               grows past full size.
      """
      size = shutil.get_terminal_size((80, 24))
      key  = (matrix.key(), status, size)
      if (key == self.last):
         return
      self.last = key
                               # one row is kept for the status line; a
                               # terminal row holds two LED rows
      fit    = min(1.0, size.columns / matrix.width,
                   2.0 * max(1, size.lines - 1) / matrix.height)
      scale  = max(self.min_scale, fit)
      width  = max(1, int(round(matrix.width * scale)))
      height = max(2, int(round(matrix.height * scale)))
      if ((width, height) == (matrix.width, matrix.height)):
         pixels = matrix.pixels
      else:
         pixels = shrink_pixels(matrix.pixels, matrix.width,
                                matrix.height, width, height)
      rows  = (height + 1) // 2
      left  = max(0, (size.columns - width) // 2)
      top   = max(0, (size.lines - rows - 1) // 2)
      shown = min(rows, size.lines)
      cols  = min(width, size.columns)
      out   = ["\x1b[0m\x1b[2J"]
      for r in range(shown):
         out.append(f"\x1b[{top + r + 1};{left + 1}H")
         upper = pixels[2 * r]
         lower = (     pixels[2 * r + 1]
                  if (2 * r + 1 < height)
                  else [None] * width)
         prev  = None
         for x in range(cols):
            fg = self._rgb(upper[x])
            bg = (     (0, 0, 0) if (2 * r + 1 >= height)
                  else self._rgb(lower[x]))
            if ((fg, bg) != prev):
               out.append("\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm"
                          % (fg + bg))
               prev = (fg, bg)
            out.append("▀")
         out.append("\x1b[0m")
      if (top + rows < size.lines):
         out.append(f"\x1b[{top + rows + 1};{left + 1}H\x1b[0m\x1b[2m"
                    + status[:cols] + "\x1b[0m")
      sys.stdout.write("".join(out))
      sys.stdout.flush()

###########################################################################

class TextRenderer:
   """
      name: TextRenderer
      use:  plain output for terminals without colour, or ssh/logging:
            prints the card's three lines whenever the card changes
   """
   def __init__(self, display):
      self.display = display
      self.last    = None

   def start(self):
      pass

   def stop(self):
      pass

   ########################################################################

   def draw_card(self, lines, status):
      if (lines == self.last):
         return
      self.last = lines
      print(time.strftime("%H:%M:%S ") +
            " | ".join(l for l in lines if (l)) + f"   [{status}]",
            flush = True)

###########################################################################
###
### s t a r t    o f    e x e c u t i o n
###
###########################################################################

class MAIN:
   """
      name: MAIN
      use:  reads settings, starts the fetch thread and runs the display
            loop (the firmware's setup() and loop())
   """
   def __init__(self, exec_name):
      self.self_name = os.path.basename(exec_name)
      self.flights   = []
      self.status    = "starting"
      self.lock      = threading.Lock()
      self.stop      = threading.Event()
      self.fetched   = threading.Event()
      self.track     = []

   ########################################################################

   def usage(self):
      print(f"""{self.self_name} {REVISION}
usage: {self.self_name} [options] [place]
where options are:
   -xLAT,LON    centre of the area, e.g. -x40.484,-80.037
   -rKM         radius in km
   -fIDENTS     follow up to 5 flights anywhere instead of an area:
                callsigns, flight numbers or tail numbers, comma-
                separated, e.g. -fUAL1179,DL402,N512GA
   -cFILE       settings file (default flightwall.ini beside the
                script; created with defaults on first run)
   -sSTYLE      half (LED matrix, default) or text (plain lines)
   -gWxH        panel size in pixels, e.g. -g128x64
   -lFILE       write the firmware's serial log to FILE
   -o           fetch once, print the flights as text and exit
   -d           demo data, no network needed
   -v           verbose log; more -v for more verbosity
   -h           help
and place is where to centre the area instead of -x: a three-letter
airport code (PIT), or a place name in quotes ("brighton heights, pa").
Three letters are tried as an airport code first.

Place names are looked up with OpenStreetMap's Nominatim service (data
(c) OpenStreetMap contributors, ODbL), once per name, and remembered in
flightwall_places.json. Usage policy:
https://operations.osmfoundation.org/policies/nominatim/
"Over <town>" uses GeoNames (CC BY 4.0) and airport codes OurAirports,
both downloaded once beside the script.

Ctrl+C stops it.""")

   ########################################################################

   def load_settings(self, path):
      """
         post: returns a flat dict of settings; writes the default file
               first when it does not exist
      """
      if (not os.path.isfile(path)):
         try:
            with open(path, "w", encoding = "utf-8") as handle:
               handle.write(DEFAULT_INI)
            print(f"{self.self_name}: wrote default settings to {path}",
                  file = sys.stderr)
         except OSError as error:
            print(f"{self.self_name}: cannot write {path}: {error}",
                  file = sys.stderr)
      ini = configparser.ConfigParser()
      ini.read_string(DEFAULT_INI)
      try:
         ini.read(path, encoding = "utf-8")
         loc  = ini["location"]
         api  = ini["api"]
         tim  = ini["timing"]
         dsp  = ini["display"]
         hw   = ini["hardware"]
         return({
            "lat"              : loc.getfloat("center_lat"),
            "lon"              : loc.getfloat("center_lon"),
            "radius_km"        : loc.getfloat("radius_km"),
            "track"            : [t for t in re.split(
                                     r"[,\s]+",
                                     ini["tracking"].get("flights").upper())
                                  if (t)],
            "opensky_id"       : api.get("opensky_client_id").strip(),
            "opensky_secret"   : api.get("opensky_client_secret").strip(),
            "aeroapi_key"      : api.get("aeroapi_key").strip(),
            "state_source"     : api.get("state_source").strip().lower(),
            "flight_source"    : api.get("flight_source").strip().lower(),
            "use_cdn"          : api.getboolean("use_flightwall_cdn"),
            "fetch_interval"   : max(5, tim.getint(
                                           "fetch_interval_seconds")),
            "cycle_seconds"    : max(1, tim.getint(
                                           "display_cycle_seconds")),
            "brightness"       : dsp.getint("brightness"),
            "text_color"       : dsp.get("text_color").strip().lstrip("#"),
            "route_codes"      : dsp.get("route_codes").strip().lower(),
            "show_unidentified": dsp.getboolean("show_unidentified"),
            "style"            : dsp.get("style").strip().lower(),
                               # "full" was the 1.1 name of the new layout
            "layout"           : self.choice(dsp, "layout",
                                             ("wall", "classic", "full")
                                             ).replace("full", "wall"),
            "panel_size"       : self.size(dsp.get("panel_size")),
            "min_scale"        : dsp.getfloat("min_scale"),
            "show_logo"        : dsp.getboolean("show_logo"),
            "logo_url"         : dsp.get("logo_url").strip(),
            "speed_unit"       : self.choice(dsp, "speed_unit",
                                             ("kt", "mph", "kmh", "mps",
                                              "mach")),
            "altitude_unit"    : self.choice(dsp, "altitude_unit",
                                             ("ft", "m")),
            "vertical_rate_unit": self.choice(dsp, "vertical_rate_unit",
                                              ("fpm", "mps")),
            "track_unit"       : self.choice(dsp, "track_unit",
                                             ("deg", "card", "both")),
            "width"            : (hw.getint("tile_pixel_w") *
                                  hw.getint("tiles_x")),
            "height"           : (hw.getint("tile_pixel_h") *
                                  hw.getint("tiles_y"))})
      except (configparser.Error, ValueError, KeyError) as error:
         raise(ExceptionClass(f"bad settings in {path}: {error}",
                              self.self_name))

   ########################################################################

   @staticmethod
   def size(text):
      """
         post: returns (width, height) from "128x64"; raises ValueError
      """
      w, h = [int(v) for v in text.strip().lower().split("x")]
      if (not ((24 <= w <= 1000) and (8 <= h <= 200))):
         raise(ValueError(f"panel size {text} out of range"))
      return(w, h)

   ########################################################################

   @staticmethod
   def choice(section, key, allowed):
      """
         post: returns the lower-cased value; raises ValueError naming
               the allowed values when it is not one of them
      """
      value = section.get(key).strip().lower()
      if (value not in allowed):
         raise(ValueError(f"{key} = {value}; use one of "
                          f"{', '.join(allowed)}"))
      return(value)

   ########################################################################

   def build_fetcher(self, st, demo):
      wall = (st["layout"] == "wall")
      if (demo):
         fake = DemoFetcher()
         return(FlightDataFetcher(fake, fake, None, st, None, fake,
                                  DemoPlaces if (wall) else None),
                "demo")
      want = st["state_source"]
      if (st["mode"] == "track"):
                               # OpenSky can only find a callsign by
                               # downloading the whole world (4 credits a
                               # time), so tracking always uses the feed
         states = AdsbFetcher()
         names  = ["airplanes.live"]
      elif ((want == "opensky") or
          ((want == "auto") and st["opensky_id"] and st["opensky_secret"])):
         if (not (st["opensky_id"] and st["opensky_secret"])):
            raise(ExceptionClass("state_source = opensky needs "
                                 "opensky_client_id and "
                                 "opensky_client_secret", self.self_name))
         states = OpenSkyFetcher(st["opensky_id"], st["opensky_secret"])
         names  = ["opensky"]
      else:
         states = AdsbFetcher()
         names  = ["airplanes.live"]
      want = st["flight_source"]
      if ((want == "aeroapi") or ((want == "auto") and st["aeroapi_key"])):
         if (not st["aeroapi_key"]):
            raise(ExceptionClass("flight_source = aeroapi needs "
                                 "aeroapi_key", self.self_name))
                               # tracked arrival times go stale; refresh
                               # every 10 minutes (each refresh is billed)
         flights = AeroAPIFetcher(st["aeroapi_key"],
                                  600 if (st["mode"] == "track") else 1800)
         names.append("aeroapi")
      else:
         flights = AdsbdbFetcher()
         names.append("adsbdb")
      cdn   = FlightWallFetcher() if (st["use_cdn"]) else None
      logos = None
      if (st["show_logo"] and wall):
         logos = LogoFetcher(st["logo_url"],
                             os.path.join(os.path.dirname(
                                os.path.abspath(__file__)), "logos"))
                               # town names and airport positions for the
                               # bottom line (wall layout only)
      return(FlightDataFetcher(states, flights, cdn, st, logos,
                               AdsbdbFetcher(),
                               PlaceIndex if (wall) else None,
                               AirportDB  if (wall) else None),
             "+".join(names))

   ########################################################################

   def locate(self, place):
      """
         use:  the place argument -> where to centre the area
         post: returns (lat, lon, label); three letters are tried as an
               airport code before they are tried as a place name
      """
      if (re.fullmatch(r"[A-Za-z]{3}", place)):
         if (not os.path.isfile(data_path("airports.csv"))):
            print(f"{self.self_name}: downloading the airport list "
                  "(once, about 12 MB)", file = sys.stderr)
         found = AirportDB.iata(place)
         if (found):
            return(found["lat"], found["lon"],
                   f"{found['name']} ({place.upper()})")
      try:
         hit = geocode(place)
      except FetchError as error:
         raise(ExceptionClass(f"cannot look up {place}: {error}",
                              self.self_name))
      if (hit is None):
         raise(ExceptionClass(f"cannot find a place called {place}",
                              self.self_name, E_USAGE))
      return(hit)

   ########################################################################

   def fetch_once(self, fetcher, source):
      try:
         if (self.track):
            states, flights = fetcher.fetch_tracked(self.track)
         else:
            states, flights = fetcher.fetch_flights()
      except FetchError as error:
         Log.line(f"fetch failed: {error}")
         with self.lock:
            self.status = f"{source}  fetch failed: {error}"
         return
      Log.line(f"state vectors: {len(states)}")
      for s in states:
         Log.line(f" {s.callsign or s.icao24} @ {s.distance_km:.1f}km "
                  f"bearing {s.bearing_deg:.1f}", 1)
      Log.line(f"enriched flights: {len(flights)}")
      for f in flights:
         Log.line(f" {f.ident} {f.operator_icao} {f.aircraft_code} "
                  f"{f.origin.code_icao}>{f.destination.code_icao}", 1)
      with self.lock:
         self.flights = flights
         self.status  = (     f"{source}  tracking {len(flights)}, "
                              f"{len(states)} found  "
                         if (self.track)
                         else f"{source}  {len(states)} aircraft, "
                              f"{len(flights)} identified  ")
         self.status += time.strftime("%H:%M:%S")
      self.fetched.set()

   ########################################################################

   def fetch_loop(self, fetcher, source, interval):
      while (not self.stop.is_set()):
         self.fetch_once(fetcher, source)
         self.stop.wait(interval)

   ########################################################################

   def execute(self, args):
      try:
         opts, rest = getopt.gnu_getopt(args, "x:r:f:c:s:g:l:odvh")
      except getopt.GetoptError as error:
         self.usage()
         raise(ExceptionClass(str(error), self.self_name, E_USAGE))
                               # a place name forgotten in quotes arrives
                               # as several words; put them back together
      place = " ".join(rest).strip()
      here     = os.path.dirname(os.path.abspath(__file__))
      ini_path = os.path.join(here, "flightwall.ini")
      override = {}
      log_path = None
      once     = False
      demo     = False
      for option, value in opts:
         try:
            if (option == "-h"):
               self.usage()
               return(E_SUCCESS)
            elif (option == "-x"):
               lat, lon = [float(v) for v in value.split(",")]
               if (not ((-90 <= lat <= 90) and (-180 <= lon <= 180))):
                  raise(ValueError(value))
               override.update({"lat": lat, "lon": lon})
            elif (option == "-r"):
               override["radius_km"] = float(value)
            elif (option == "-f"):
               override["track"] = [t for t in re.split(r"[,\s]+",
                                                        value.upper())
                                    if (t)]
               if (not override["track"]):
                  raise(ValueError(value))
            elif (option == "-c"):
               ini_path = value
            elif (option == "-s"):
               if (value not in ("half", "text")):
                  raise(ValueError(value))
               override["style"] = value
            elif (option == "-g"):
               override["size"] = self.size(value)
            elif (option == "-l"):
               log_path = value
            elif (option == "-o"):
               once = True
            elif (option == "-d"):
               demo = True
            elif (option == "-v"):
               Log.verbose += 1
         except ValueError:
            raise(ExceptionClass(f"bad value for {option}: {value}",
                                 self.self_name, E_USAGE))

      if (place and ("lat" in override)):
         self.usage()
         raise(ExceptionClass("give either -x or a place, not both",
                              self.self_name, E_USAGE))
      st = self.load_settings(ini_path)
      st.update(override)
      if (place):
         st["lat"], st["lon"], label = self.locate(place)
         print(f"{self.self_name}: {place} = {label} "
               f"({st['lat']:.4f},{st['lon']:.4f})", file = sys.stderr)
                               # -g, else the layout's own panel size
      if ("size" in st):
         st["width"], st["height"] = st["size"]
      elif (st["layout"] == "wall"):
         st["width"], st["height"] = st["panel_size"]
      if (len(st["track"]) > 5):
         raise(ExceptionClass("track at most 5 flights", self.self_name,
                              E_USAGE))
      self.track = st["track"]
      st["mode"] = "track" if (self.track) else "area"
      try:
         color = tuple(int(st["text_color"][i:i + 2], 16)
                       for i in (0, 2, 4))
      except ValueError:
         raise(ExceptionClass(f"bad text_color {st['text_color']}",
                              self.self_name))
      if (log_path):
         try:
            Log.handle = open(log_path, "a", encoding = "utf-8")
         except OSError as error:
            raise(ExceptionClass(f"cannot open {log_path}: {error}",
                                 self.self_name))
      elif (once or (st["style"] == "text")):
         Log.handle = sys.stderr if (Log.verbose) else None

      fetcher, source = self.build_fetcher(st, demo)
      if ((st["layout"] == "wall") and (not demo)):
                               # one-time downloads, done now rather than
                               # inside the first fetch behind the panel
         for what, path, loader, needed in (
                ("town list (GeoNames, about 10 MB)", "cities1000.zip",
                 PlaceIndex._load, True),
                ("airport list (OurAirports, about 12 MB)", "airports.csv",
                 AirportDB._load, ("aeroapi" in source))):
            if (not needed):
               continue
            if (not os.path.isfile(data_path(path))):
               print(f"{self.self_name}: downloading the {what}, once",
                     file = sys.stderr)
            if (not loader()):
               print(f"{self.self_name}: no {what}; the bottom line will "
                     "say less", file = sys.stderr)
      display         = NeoMatrixDisplay(st["width"], st["height"],
                                         color, st)
      Log.line(     f"FlightWall {source} tracking "
                    f"{', '.join(self.track)}" if (self.track)
               else f"FlightWall {source} at {st['lat']},{st['lon']} "
                    f"radius {st['radius_km']} km")

      if (once):
         self.fetch_once(fetcher, source)
         print(self.status)
         for f in self.flights:
            lines = display.text_lines(f)
            ident = f.ident or f.ident_icao
            print(f"  {ident:<8} " + " | ".join(l for l in lines[:3]
                                                 if (l)))
            for line in lines[3:]:
               print(f"  {'':<8} {line}")
         return(E_SUCCESS)

      if ((st["style"] == "half") and (not enable_ansi())):
         print(f"{self.self_name}: this console cannot show colours; "
               "using -stext", file = sys.stderr)
         st["style"] = "text"
      renderer = (     TerminalRenderer(st["brightness"], st["min_scale"])
                  if (st["style"] == "half")
                  else TextRenderer(display))

      threading.Thread(target = self.fetch_loop, daemon = True,
                       args = (fetcher, source,
                               st["fetch_interval"])).start()
      renderer.start()
      try:
         self.run(renderer, display, st)
      except KeyboardInterrupt:
         pass
      finally:
         self.stop.set()
         renderer.stop()
      return(E_SUCCESS)

   ########################################################################

   def run(self, renderer, display, st):
      """
         use:  display loop; the firmware only redraws after a fetch,
               here cards cycle every display_cycle_seconds as intended
      """
      half  = isinstance(renderer, TerminalRenderer)
      start = time.time()
      index = 0
      page  = 0
      shown = time.time()
      while (True):
         with self.lock:
            flights = list(self.flights)
            status  = self.status
         now = time.time()
         if (flights):
            index = index % len(flights)
         if (flights and (now - shown >= st["cycle_seconds"])):
                               # every page of a flight, then the next
            page += 1
            if (page >= len(display.pages(flights[index]))):
               page  = 0
               index = (index + 1) % len(flights)
            shown = now
         if (flights):
            pages = display.pages(flights[index])
            page  = min(page, len(pages) - 1)
            if (len(flights) > 1):
               status = f"{index + 1}/{len(flights)}  " + status
         if (now - start < 1.5):
                               # the firmware's boot message
            display.show_message("FlightWall")
            lines = ["FlightWall"]
         elif (flights):
            display.show_flight(flights[index], pages[page])
            lines = display.text_lines(flights[index])
         else:
            display.show_loading()
            lines = ["..."]
         if (half):
            renderer.draw(display.matrix, status)
         else:
            renderer.draw_card(lines, status)
         time.sleep(0.25)

if ( __name__ == "__main__" ):
   try:
      sys.exit(MAIN(sys.argv[0]).execute(sys.argv[1:]))
   except ExceptionClass as exception:
      exception.print_error()
