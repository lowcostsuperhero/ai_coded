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

import configparser
import getopt
import json
import math
import os
import random
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

E_SUCCESS = 0
E_WARN    = 1
E_FAIL    = 2
E_USAGE   = 3

REVISION   = "1.0"
USER_AGENT = "flightwall/1.0 (personal, non-commercial)"

OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
                     "opensky-network/protocol/openid-connect/token")
OPENSKY_BASE_URL  = "https://opensky-network.org"
AEROAPI_BASE_URL  = "https://aeroapi.flightaware.com/aeroapi"
FLIGHTWALL_CDN    = "https://cdn.theflightwall.com"
ADSB_FEEDS        = ["https://api.airplanes.live/v2",
                     "https://api.adsb.lol/v2"]
ADSBDB_BASE_URL   = "https://api.adsbdb.com/v0"

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

[hardware]
# HardwareConfiguration.h: tile size and arrangement -> 160 x 32
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
                               # position, when the source knows it
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
         if (s.distance_km > radius_km):
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
      return(FlightInfo(
                ident         = f.get("ident") or "",
                ident_icao    = f.get("ident_icao") or "",
                ident_iata    = f.get("ident_iata") or "",
                operator_code = f.get("operator") or "",
                operator_icao = f.get("operator_icao") or "",
                operator_iata = f.get("operator_iata") or "",
                aircraft_code = f.get("aircraft_type") or "",
                origin        = airport(f.get("origin")),
                destination   = airport(f.get("destination"))))

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

###########################################################################
###
### d e m o
###
###########################################################################

                               # callsign, airline ICAO, IATA, name, type,
                               # type name, origin, destination, colour,
                               # (unused) logo style, what the flight is
                               # doing
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

   def fetch_flight_info(self, state):
      for row in DEMO:
         if ((row[0] == state.callsign) and row[1]):
            org, dst = row[6], row[7]
            ap = lambda a: AirportInfo(a[0], a[1], a[2], a[3], a[4])
            return(FlightInfo(
                      ident = row[0], ident_icao = row[0],
                      ident_iata = row[2] + row[0][3:],
                      operator_code = row[1], operator_icao = row[1],
                      operator_iata = row[2], aircraft_code = row[4],
                      airline_display_name_full   = row[3],
                      aircraft_display_name_short = row[5],
                      origin      = ap(org),
                      destination = ap(dst)))
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
   def __init__(self, states, flights, cdn, settings):
      self.states   = states
      self.flights  = flights
      self.cdn      = cdn
      self.settings = settings

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

   def _enrich(self, info, s):
      """
         use:  attaches the position and display names
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
      return(info)

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
         if (not (32 <= code <= 126)):
            code = ord("?")
         glyph = FONT_5X7[(code - 32) * 5:(code - 32) * 5 + 5]
         for col, bits in enumerate(glyph):
            for row in range(8):
               if ((bits >> row) & 1):
                  self.pixel(x + col, y + row, color)
         x += 6

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
            three-line card, plus loading screen and one-line messages
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

   def pages(self, f):
      """
         post: the pages shown for flight f; the firmware has one card
      """
      return(["card"])

   ########################################################################

   def text_lines(self, f):
      """
         use:  the flight as plain text, for -o and the text style
      """
      return(self.card_lines(f))

   ########################################################################

   def show_flight(self, f, page = "card"):
      m = self.matrix
      w = m.width
      h = m.height
      m.fill_screen()
      m.draw_rect(0, 0, w, h, self.color)
      padding  = 2
      inner_w  = w - 2 - 2 * padding
      inner_h  = h - 2 - 2 * padding
      max_cols = inner_w // 6
      total    = 3 * 8 + 2 * 1
      y        = 1 + padding + (inner_h - total) // 2
      for line in self.card_lines(f):
         m.draw_text(1 + padding, y, truncate_to_columns(line, max_cols),
                     self.color)
         y += 8 + 1

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

class TerminalRenderer:
   """
      name: TerminalRenderer
      use:  paints the matrix with "upper half block" characters: one
            terminal cell = two vertically stacked LEDs, so a 128x64
            panel needs 128 columns by 32 rows (plus a status line)
   """
   OFF = (22, 22, 22)

   def __init__(self, brightness):
      self.scale = max(0, min(255, brightness)) / 255.0
      self.last  = None

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
         imp:  a window too small for the panel shows its top left
               part; the rest is cut off
      """
      size = shutil.get_terminal_size((80, 24))
      key  = (matrix.key(), status, size)
      if (key == self.last):
         return
      self.last = key
      pixels = matrix.pixels
      width  = matrix.width
      height = matrix.height
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

   ########################################################################

   def usage(self):
      print(f"""{self.self_name} {REVISION}
usage: {self.self_name} [options]
where options are:
   -aLAT,LON    centre of the area, e.g. -a40.484,-80.037
   -rKM         radius in km
   -cFILE       settings file (default flightwall.ini beside the
                script; created with defaults on first run)
   -sSTYLE      half (LED matrix, default) or text (plain lines)
   -gWxH        panel size in pixels, e.g. -g160x48
   -lFILE       write the firmware's serial log to FILE
   -o           fetch once, print the flights as text and exit
   -d           demo data, no network needed
   -v           verbose log; more -v for more verbosity
   -h           help

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
      if (demo):
         fake = DemoFetcher()
         return(FlightDataFetcher(fake, fake, None, st), "demo")
      want = st["state_source"]
      if ((want == "opensky") or
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
         flights = AeroAPIFetcher(st["aeroapi_key"])
         names.append("aeroapi")
      else:
         flights = AdsbdbFetcher()
         names.append("adsbdb")
      cdn = FlightWallFetcher() if (st["use_cdn"]) else None
      return(FlightDataFetcher(states, flights, cdn, st), "+".join(names))

   ########################################################################

   def fetch_once(self, fetcher, source):
      try:
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
         self.status  = (f"{source}  {len(states)} aircraft, "
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
         opts, rest = getopt.getopt(args, "a:r:c:s:g:l:odvh")
      except getopt.GetoptError as error:
         self.usage()
         raise(ExceptionClass(str(error), self.self_name, E_USAGE))
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
            elif (option == "-a"):
               lat, lon = [float(v) for v in value.split(",")]
               if (not ((-90 <= lat <= 90) and (-180 <= lon <= 180))):
                  raise(ValueError(value))
               override.update({"lat": lat, "lon": lon})
            elif (option == "-r"):
               override["radius_km"] = float(value)
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

      st = self.load_settings(ini_path)
      st.update(override)
      if ("size" in st):
         st["width"], st["height"] = st["size"]
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
      display         = NeoMatrixDisplay(st["width"], st["height"],
                                         color, st)
      Log.line(f"FlightWall {source} at {st['lat']},{st['lon']} "
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
      renderer = (     TerminalRenderer(st["brightness"])
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
