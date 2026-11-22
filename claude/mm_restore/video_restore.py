#!/usr/bin/python
# name: $Id: video_restore.py 2.main-2 11:47:04 22-Nov-2026 claude-opus-5-5 $
# use: enhance videos locally: remove grain and noise, upscale, steady
#      and sharpen
"""
   name: video_restore.py
   use: ai video enhancement that runs entirely on this computer, the
        moving-picture companion of image_restore.py; for each
        frame:
           1. removes grain by averaging it with its neighbouring
              frames, lined up by optical flow: the picture lines up
              and is kept, the grain differs frame to frame and is
              averaged away
           2. removes what noise is left and upscales (real-esrgan)
           3. steadies the result against the previous frame, so the
              detail the ai adds does not flicker
           4. sharpens
        the soundtrack is copied across
   usage: video_restore.py [options] video [video ...]
          video_restore.py -h for the option list
          mm_restore.py takes images and videos together
          examples:
             video_restore.py home_movie.mp4
             video_restore.py -p30 -l5 old.avi     (try 5s from 0:30)
             video_restore.py -s1 -mnone hd.mp4    (fast: no ai, for
                                                    hd sources)
   pre: python 3.9 or later, and
           pip install torch spandrel pillow numpy opencv-python
                       imageio-ffmpeg
        imageio-ffmpeg brings its own ffmpeg, so nothing else is needed
        for an nvidia gpu, install torch from the pytorch.org index for
        your cuda version FIRST (see https://pytorch.org/get-started)
   post: writes <name>_restored.mp4 (h.264 video, aac audio) beside
         each input (or into -o)
         if interrupted, rerunning the same command carries on from
         the last saved chunk; videos already finished are skipped
         -a writes <name>-preview.mp4 from the part done so far
   imp: model weights are shared with image_restore.py, in its cache
        directory (%LOCALAPPDATA%\\restore on windows,
        ~/.cache/restore elsewhere)
        ai upscaling is slow without a gpu: about 2 seconds per
        480x320 frame on a 2-core cpu, so an hour of video takes days;
        try a short piece with -p/-l first; -mnone skips the ai and
        runs at many frames a second
        interlaced sources (old camcorder, tv captures) should be
        deinterlaced first; the motion lining-up assumes whole frames
"""

import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import textwrap
import time
import warnings

E_SUCCESS = 0
E_WARN    = 1
E_FAIL    = 2
E_USAGE   = 3

SELF       = os.path.splitext(os.path.basename(sys.argv[0]))[0]
SELFPREFIX = os.path.dirname(os.path.abspath(sys.argv[0]))
REVISION   = "$Revision: 2.main-2 $"

ESRGAN = "https://github.com/xinntao/Real-ESRGAN/releases/download/"
                                # photo:   RRDB net, best quality, heavy
                                # general: compact net, fast, with an
                                #          adjustable denoise strength
                                # anime:   RRDB net tuned for cartoons
WEIGHTS = {
   "photo"       : ESRGAN + "v0.1.0/RealESRGAN_x4plus.pth",
   "general"     : ESRGAN + "v0.2.5.0/realesr-general-x4v3.pth",
   "general-wdn" : ESRGAN + "v0.2.5.0/realesr-general-wdn-x4v3.pth",
   "anime"       : ESRGAN + "v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth",
}

                                # optical flow is worked out at no more
                                # than this size: plenty for lining
                                # frames up, and much faster
FLOW_SIZE = 960

                                # work is saved in chunks, each closed
                                # after this much processing time or
                                # this many frames, whichever is first;
                                # an interruption loses at most one
CHUNK_SECONDS = 60
CHUNK_FRAMES  = 600

USAGE = f"""{SELF} {REVISION}
usage: {SELF} [options] video [video ...]
where options are:
   -sScale    output scale factor, 1 or more (default 1: restore
              without enlarging); output sizes are rounded to even
              numbers, as h.264 needs
   -dStrength grain and noise removal 0.0 to 1.0 (default 0.5); higher
              uses more neighbouring frames
   -fStrength flicker reduction 0.0 to 1.0 (default 0.5)
   -kStrength sharpen strength 0.0 to 1.0 (default 0.3)
   -mModel    auto, photo, general, anime or none (default auto: photo
              on a capable gpu, general otherwise); none skips the ai
              and enlarges with lanczos, many times faster
   -pStart    start this many seconds in (default 0)
   -lLength   only do this many seconds (default all)
   -qQuality  h.264 crf, lower is better and bigger (default 18)
   -x         redo: start afresh, even when a video is already done or
              partly done (without -x, finished videos are skipped and
              unfinished ones carry on where they stopped)
   -a         assemble: restore nothing, but join the frames finished
              so far into <name>-preview.mp4, to see how it is going;
              the unfinished work is left as it is, to carry on later;
              safe to use while a restoration is running; give the
              same -o as the restoration
   -tTile     tile size in pixels; 0 = choose from available memory
   -oOutput   output directory, or output file for a single video
   -c         use the cpu even if a gpu is present
   -v         verbose; more -v for more verbosity
   -n         do not execute; show what would be done
   -h         help
"""

###########################################################################
###
### c l a s s e s
###
###########################################################################

class ExceptionClass(Exception):
   """
      use: error carrying a message, the program name and an exit code
   """
   def __init__(self, message, exec_name = SELF, code = E_FAIL):
      super().__init__(message)
      self.exec_name = exec_name
      self.code      = code
      self.message   = (     f"{exec_name}: {message}" if (exec_name)
                        else f"{message.capitalize()}")

###########################################################################

   def print_error(self):
      print(self.message, file = sys.stderr)

###########################################################################

class UsageParser(argparse.ArgumentParser):
   """
      use: argparse that reports errors in house style
      imp: argparse already accepts attached values (-s2, -d0.5)
   """
   def error(self, message):
      raise(ExceptionClass(f"{message}\n{USAGE}", code = E_USAGE))

###########################################################################

class Engine:
   """
      use: holds the device and the upscaling model
   """
   def __init__(self, opts):
      self.opts = opts
      self.device, self.description, self.free = pick_device(opts.c)
      self.model_name = pick_model(opts.m, self.device, self.free)
      self.tile       = pick_tile(opts.t, self.device, self.free,
                                  self.model_name)
      self.model      = None

###########################################################################

   def describe(self):
      opts = self.opts
      log(0, 1, f"device {self.description}")
      print(wrapped(f"{SELF}: ", [f"model {self.model_name}",
                                  f"scale {opts.s:g}",
                                  f"denoise {opts.d:g}",
                                  f"flicker {opts.f:g}",
                                  f"sharpen {opts.k:g}",
                                  f"crf {opts.q}"]),
            file = sys.stderr)

###########################################################################

   def upscaler(self):
      """
         post: returns (model descriptor, dtype), loading on first use
         imp: after the frames have been averaged little grain is
              left, so the general model's own denoise is kept light
              to avoid a waxy look
      """
      if (self.model is None):
         verbose = self.opts.v
         if (self.model_name == "general"):
            strong = load_state(fetch_weights("general",     verbose))
            weak   = load_state(fetch_weights("general-wdn", verbose))
                                # deep network interpolation, as done
                                # by real-esrgan's own -dn option
            s      = 0.5 * self.opts.d
            state  = {key: s * strong[key] + (1 - s) * weak[key]
                      for key in strong}
         else:
            state  = load_state(fetch_weights(self.model_name, verbose))
         model = spandrel.ModelLoader().load_from_state_dict(state)
         model.eval().to(self.device)
         dtype = torch.float32
         if ((self.device.type == "cuda") and model.supports_half):
            model.half()
            dtype = torch.float16
         log(2, verbose, f"loaded {model.architecture.name}, {dtype}")
         self.model = (model, dtype)
      return(self.model)

###########################################################################

   def oom_retry(self, run, tile, size):
      """
         use: calls run(tile), halving tile on out-of-memory
      """
      while (True):
         try:
            return(run(tile))
         except (RuntimeError, torch.cuda.OutOfMemoryError) as error:
            if ("out of memory" not in str(error).lower()):
               raise
            tile = (tile if (tile > 0) else size) // 2
            if (tile < 64):
               raise(ExceptionClass("out of memory even with 64px "
                                    "tiles; try -c"))
            log(1, self.opts.v, f"out of memory, retrying with {tile}px"
                                " tiles")
            self.tile = tile
            if (self.device.type == "cuda"):
               torch.cuda.empty_cache()

###########################################################################

class Aligner:
   """
      use: lines frames up with each other using dense optical flow
      imp: flow is worked out on a blurred, shrunk grey copy of each
           frame: blurring stops the grain being tracked as motion
   """
   def __init__(self, width, height):
      self.width  = width
      self.height = height
      self.factor = min(1.0, FLOW_SIZE / max(width, height))
      self.small  = (max(8, round(width  * self.factor)),
                     max(8, round(height * self.factor)))
      self.dis    = cv2.DISOpticalFlow_create(
                       cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
      self.grids  = {}

###########################################################################

   def prepare(self, frame):
      """
         post: returns the grey copy of frame that flow is worked on
      """
      gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
      if (self.factor < 1):
         gray = cv2.resize(gray, self.small, interpolation = cv2.INTER_AREA)
      return(cv2.GaussianBlur(gray, (0, 0), 1.5))

###########################################################################

   def flow(self, here, there):
      """
         post: returns the flow, on the small grid, such that
               here(x) is approximately there(x + flow(x))
      """
      return(self.dis.calc(here, there, None))

###########################################################################

   def warp(self, image, flow):
      """
         use: moves image onto the frame flow was worked out from, at
              image's own size
      """
      h, w = image.shape[:2]
      if ((w, h) not in self.grids):
         self.grids[(w, h)] = numpy.meshgrid(
                                 numpy.arange(w, dtype = numpy.float32),
                                 numpy.arange(h, dtype = numpy.float32))
      gx, gy = self.grids[(w, h)]
      if (flow.shape[:2] != (h, w)):
         sx, sy = w / flow.shape[1], h / flow.shape[0]
         flow   = cv2.resize(flow, (w, h), interpolation = cv2.INTER_LINEAR)
         flow[..., 0] *= sx
         flow[..., 1] *= sy
      return(cv2.remap(image, gx + flow[..., 0], gy + flow[..., 1],
                       cv2.INTER_LINEAR, borderMode = cv2.BORDER_REPLICATE))

###########################################################################

class Checkpoint:
   """
      use: the work folder of an unfinished video, beside its output:
           the chunks finished so far, the settings they were made
           with, and the last frame made, which the next frame is
           steadied against
      imp: state.json is the record of what is finished; it is
           replaced in one step after each chunk is complete, so an
           interruption at any moment leaves either the old record or
           the new one, never a half-written one
   """
   def __init__(self, target, settings):
      self.folder   = target + ".parts"
      self.path     = os.path.join(self.folder, "state.json")
      self.settings = settings
      self.chunks   = []
      self.sigma    = None
      self.previous = None
      self.last     = None

###########################################################################

   def load(self):
      """
         post: returns the number of frames already done, 0 for a fresh
               start; work saved with other settings is thrown away
      """
      try:
         with open(self.path) as stream:
            state = json.load(stream)
      except (OSError, ValueError):
         state = None
      usable = ((state is not None) and
                (state.get("settings") == self.settings) and
                all(os.path.isfile(os.path.join(self.folder, name))
                    for name, _ in state.get("chunks", [])))
      if (not usable):
         if (state is not None):
            log(0, 1, "settings or source changed since the last run; "
                      "starting over")
         self.clear()
         os.makedirs(self.folder, exist_ok = True)
         return(0)
      self.chunks = [tuple(chunk) for chunk in state["chunks"]]
      self.sigma  = state["sigma"]
      self.last   = state["last"]
      if (self.last):
         self.previous = numpy.load(os.path.join(self.folder, self.last))
      return(sum(frames for _, frames in self.chunks))

###########################################################################

   def add(self, name, frames, previous, sigma):
      """
         use: records a finished chunk, which must already be in the
              work folder under name
      """
      last = f"last_{len(self.chunks):05d}.npy"
      numpy.save(os.path.join(self.folder, last), previous)
      self.chunks.append((name, frames))
      state = {"settings" : self.settings,
               "sigma"    : sigma,
               "chunks"   : self.chunks,
               "last"     : last}
      partial = self.path + ".partial"
      with open(partial, "w") as stream:
         json.dump(state, stream, indent = 1)
      os.replace(partial, self.path)
                                # the previous last frame is no longer
                                # referenced
      if (self.last):
         try:
            os.remove(os.path.join(self.folder, self.last))
         except OSError:
            pass
      self.last = last

###########################################################################

   def clear(self):
      shutil.rmtree(self.folder, ignore_errors = True)

###########################################################################
###
### f u n c t i o n s
###
###########################################################################

def log(level, verbose, message):
   """
      use: prints message to stderr when verbosity is at least level
   """
   if (verbose >= level):
      print(f"{SELF}: {message}", file = sys.stderr)

###########################################################################

def wrapped(prefix, items, width = 79):
   """
      use: lays out prefix and a comma-separated list over as many
           lines as needed to stay within width, breaking after a
           comma
      post: returns the text; continuation lines are indented so each
            starts below the first item
   """
   pieces = [item + ("," if (number < len(items) - 1) else "")
             for number, item in enumerate(items)]
   indent = " " * len(prefix)
   lines  = []
   line   = prefix + pieces[0]
   for piece in pieces[1:]:
      if (len(line) + 1 + len(piece) <= width):
         line += " " + piece
      else:
         lines.append(line)
         line = indent + piece
   lines.append(line)
   return("\n".join(lines))

###########################################################################

def say(message, width = 79):
   """
      use: prints "SELF: message" to stderr, broken at spaces to stay
           within width, continuation lines indented to start under
           the message
      imp: a word longer than the line (a long path, say) is left
           whole rather than split
   """
   print(textwrap.fill(message, width = width,
                       initial_indent    = f"{SELF}: ",
                       subsequent_indent = " " * (len(SELF) + 2),
                       break_long_words  = False,
                       break_on_hyphens  = False), file = sys.stderr)

###########################################################################

class Status:
   """
      use: a progress line that rewrites itself in place
      imp: "\\r" only returns to the start of the screen row the cursor
           is on: a line wider than the console wraps onto a new row,
           and every update after it then starts a new row; so the
           line is kept narrower than the console, and padded with
           spaces only as far as the previous update reached, to wipe
           out what a longer update left behind
   """
   def __init__(self):
      self.length = 0

###########################################################################

   def show(self, text):
      try:
         columns = os.get_terminal_size(sys.stderr.fileno()).columns
      except (OSError, ValueError):
         columns = 80
      text = text[:max(20, columns - 1)]
      print("\r" + text + " " * max(0, self.length - len(text)), end = "",
            file = sys.stderr, flush = True)
      self.length = len(text)

###########################################################################

   def end(self):
      """
         use: moves off the progress line, so other output starts on a
              line of its own
      """
      if (self.length):
         print(file = sys.stderr)
         self.length = 0

###########################################################################

def cache_dir():
   """
      post: returns the weights cache directory, shared with
            image_restore.py
   """
   base = (     os.environ.get("LOCALAPPDATA") if (os.name == "nt")
           else os.environ.get("XDG_CACHE_HOME",
                               os.path.expanduser("~/.cache")))
   path = os.path.join(base or SELFPREFIX, "restore")
   os.makedirs(path, exist_ok = True)
   return(path)

###########################################################################

def fetch_weights(key, verbose):
   """
      use: returns the local path of a weights file, downloading it the
           first time
   """
   url  = WEIGHTS[key]
   path = os.path.join(cache_dir(), url.rsplit("/", 1)[1])
   if (not os.path.exists(path)):
      log(0, 1, f"downloading {url}")
      partial = path + ".part"
      torch.hub.download_url_to_file(url, partial,
                                     progress = (verbose > 0))
      os.replace(partial, path)
   return(path)

###########################################################################

def load_state(path):
   """
      post: returns the bare state dict from a real-esrgan .pth file
   """
   state = torch.load(path, map_location = "cpu", weights_only = True)
   for key in ("params_ema", "params"):
      if (key in state):
         return(state[key])
   return(state)

###########################################################################

def pick_device(force_cpu):
   """
      post: returns (device, description, free gpu bytes or None)
   """
   if ((not force_cpu) and torch.cuda.is_available()):
      free, total = torch.cuda.mem_get_info()
      name        = torch.cuda.get_device_name(0)
      return(torch.device("cuda"),
             f"cuda {name}, {free / 2**30:.1f} of "
             f"{total / 2**30:.1f} GiB free", free)
   mps = getattr(torch.backends, "mps", None)
   if ((not force_cpu) and (mps is not None) and mps.is_available()):
      return(torch.device("mps"), "apple mps", None)
   return(torch.device("cpu"), f"cpu, {torch.get_num_threads()} threads",
          None)

###########################################################################

def pick_model(requested, device, free):
   """
      use: resolves "auto" to a concrete upscaling model name
      imp: the photo (rrdb) model is roughly 10x the work of the
           compact general model; across thousands of frames only a
           good gpu makes that bearable
   """
   if (requested != "auto"):
      return(requested)
   if ((device.type == "cuda") and (free >= 3 * 2**30)):
      return("photo")
   return("general")

###########################################################################

def pick_tile(requested, device, free, model_name):
   """
      post: returns the upscaling tile size, 0 meaning the whole frame
      imp: guesses only; oom_retry halves the tile on out-of-memory
   """
   if (requested > 0):
      return(requested)
   heavy = (model_name != "general")
   if (device.type == "cuda"):
      gib = free / 2**30
      tile = (     1024 if (gib >= 10)
              else  768 if (gib >= 6)
              else  512 if (gib >= 3)
              else  256)
   else:
      tile = 512
   return(tile if (heavy) else tile * 2)

###########################################################################

def tile_ramps(size, tile, overlap, scale):
   """
      use: lays tiles along one axis of a frame
      post: returns [(start, end, weights)]: tile spans in input pixels
            and, at output scale, a weight per output pixel; toward
            each neighbour the weight is 0 for the outermost margin
            pixels, then ramps up to 1 across the rest of the overlap
      imp: the same cross-faded tiling as image_restore.py, so tile
           edges never show
   """
   if ((tile <= 0) or (tile >= size)):
      return([(0, size, torch.ones(size * scale))])
   overlap = min(overlap, tile // 2)
   margin  = min(16, overlap // 3) * scale
   step    = tile - overlap
   starts  = list(range(0, size - tile, step)) + [size - tile]
   width   = overlap * scale
   ramp    = torch.zeros(width)
   ramp[margin:] = ((torch.arange(width - margin, dtype = torch.float32)
                     + 0.5) / (width - margin))
   spans   = []
   for start in starts:
      weights = torch.ones(tile * scale)
      if (start > 0):
         weights[:width]  = ramp
      if (start + tile < size):
         weights[-width:] = ramp.flip(0)
      spans.append((start, start + tile, weights))
   return(spans)

###########################################################################

def upscale(engine, frame):
   """
      use: denoises and enlarges one frame with real-esrgan, in
           cross-faded tiles
      post: returns the uint8 frame at the model's scale
   """
   model, dtype = engine.upscaler()
   scale  = model.scale
   source = torch.from_numpy(numpy.ascontiguousarray(frame))
   source = source.permute(2, 0, 1).unsqueeze(0).float().div_(255)
   _, _, h, w = source.shape

   def run(tile):
      rows       = tile_ramps(h, tile, 48, scale)
      columns    = tile_ramps(w, tile, 48, scale)
      out        = torch.zeros((1, 3, h * scale, w * scale))
      row_sum    = torch.zeros(h * scale)
      column_sum = torch.zeros(w * scale)
      for y0, y1, row in rows:
         row_sum[y0 * scale:y1 * scale] += row
      for x0, x1, column in columns:
         column_sum[x0 * scale:x1 * scale] += column
      for y0, y1, row in rows:
         for x0, x1, column in columns:
            patch = source[:, :, y0:y1, x0:x1].to(engine.device, dtype)
            with torch.inference_mode():
               result = model(patch).float().cpu()
            out[:, :, y0 * scale:y1 * scale, x0 * scale:x1 * scale] += \
               result * (row[:, None] * column[None, :])
      out /= row_sum[:, None] * column_sum[None, :]
      return(out)

   out = engine.oom_retry(run, engine.tile, max(h, w))
   return(out.squeeze(0).permute(1, 2, 0).clamp(0, 1).mul(255).round()
          .byte().numpy())

###########################################################################

def noise_level(frame):
   """
      post: returns an estimate of the grain's standard deviation, in
            8-bit levels
      imp: immerkaer's method: a kernel that cancels smooth picture
           content leaves mostly noise
   """
   gray   = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY).astype(numpy.float32)
   kernel = numpy.array([[ 1, -2,  1],
                         [-2,  4, -2],
                         [ 1, -2,  1]], numpy.float32)
   rest   = cv2.filter2D(gray, -1, kernel)[1:-1, 1:-1]
   return(math.sqrt(math.pi / 2) * float(numpy.abs(rest).mean()) / 6)

###########################################################################

def temporal_denoise(aligner, window, centre, strength, sigma):
   """
      use: averages the centre frame with its neighbours, each moved
           onto it by optical flow; a neighbour only counts where it
           really matches, so moving objects and scene cuts are not
           smeared
      pre: window is a list of (frame, flow grey) pairs
      post: returns (cleaned frame, flow from the centre frame to the
            one before it, or None)
      imp: the match test is on a 5x5 average of the difference, set
           from the measured grain: grain alone passes, a real
           difference in the picture does not
   """
   frame  = window[centre][0].astype(numpy.float32)
   here   = window[centre][1]
   total  = frame.copy()
   weight = numpy.ones(frame.shape[:2] + (1,), numpy.float32)
   tau    = (1 + 3 * strength) * max(sigma, 1.0)
   back   = None
   for index, (other, grey) in enumerate(window):
      if (index == centre):
         continue
      flow  = aligner.flow(here, grey)
      if (index == centre - 1):
         back = flow
      moved = aligner.warp(other, flow).astype(numpy.float32)
      apart = cv2.blur(numpy.abs(moved - frame).mean(2), (5, 5))
      match = numpy.exp(-(apart / tau) ** 2)[..., None]
      total  += match * moved
      weight += match
   return(numpy.clip(total / weight, 0, 255).astype(numpy.uint8), back)

###########################################################################

def steady(aligner, frame, previous, back, strength):
   """
      use: blends the frame toward the previous output frame, moved
           onto it, where the two agree; ai detail that would flicker
           settles, while real change and motion pass through
      post: returns the steadied frame
      imp: recursive, so it smooths over several frames; agreement is
           judged after a light blur, so it tests the picture, not the
           invented detail being steadied
   """
   if ((previous is None) or (back is None) or (strength <= 0)):
      return(frame)
   moved  = aligner.warp(previous, back).astype(numpy.float32)
   now    = frame.astype(numpy.float32)
   apart  = numpy.abs(cv2.GaussianBlur(moved, (0, 0), 1.5) -
                      cv2.GaussianBlur(now,   (0, 0), 1.5)).mean(2)
   agree  = numpy.exp(-(cv2.blur(apart, (5, 5)) / 6.0) ** 2)[..., None]
   amount = 0.7 * strength * agree
   return(numpy.clip(now + amount * (moved - now), 0, 255)
          .astype(numpy.uint8))

###########################################################################

def sharpen(frame, strength, scale):
   """
      use: unsharp mask, the same as image_restore.py's -k
      imp: the threshold keeps flat areas from turning gritty
   """
   radius  = (0.8 + 0.7 * strength) * max(1.0, scale / 2)
   percent = round(40 + 160 * strength)
   picture = Image.fromarray(frame)
   return(numpy.asarray(picture.filter(
             ImageFilter.UnsharpMask(radius    = radius,
                                     percent   = percent,
                                     threshold = 2))))

###########################################################################

def windows(frames, radius):
   """
      use: walks a frame stream with up to radius frames either side
      post: yields (window, centre) for every frame, where window is a
            list of items and window[centre] is the current one
   """
   window = []
   centre = 0
   for item in frames:
      window.append(item)
      if (len(window) - 1 - centre >= radius):
         yield(window, centre)
         centre += 1
         if (centre > radius):
            window.pop(0)
            centre -= 1
   while (centre < len(window)):
      yield(window, centre)
      centre += 1
      if (centre > radius):
         window.pop(0)
         centre -= 1

###########################################################################

def output_path(source, output, multiple):
   """
      post: returns where the result for source is written
   """
   stem = os.path.splitext(os.path.basename(source))[0]
   name = f"{stem}_restored.mp4"
   if (not output):
      return(os.path.join(os.path.dirname(source), name))
   if (multiple or os.path.isdir(output) or
       not os.path.splitext(output)[1]):
      os.makedirs(output, exist_ok = True)
      return(os.path.join(output, name))
   return(output)

###########################################################################

def even(value):
   """
      post: returns value rounded to an even number, at least 2
   """
   return(max(2, int(round(value / 2)) * 2))

###########################################################################

def trim_params(start, length):
   """
      post: returns ffmpeg input options selecting the part of the
            video given by -p and -l
   """
   params = []
   if (start > 0):
      params += ["-ss", f"{start:g}"]
   if (length > 0):
      params += ["-t", f"{length:g}"]
   return(params)

###########################################################################

def join_chunks(folder, chunks, source, trim, fps, target):
   """
      use: joins chunks, without re-encoding them, into target, with
           the source's soundtrack (as aac, which every mp4 player
           accepts), trimmed the same way the video was
      pre: chunks lists (file name, frames) in folder, in order
      post: returns the number of frames written
      imp: target is written under a temporary name and renamed when
           complete, so a half-written file never carries its name;
           the listing gets a name of its own, so a -a run and the
           restoration it is looking at do not trip over each other
   """
   total   = sum(frames for _, frames in chunks)
   listing = os.path.join(folder, f"chunks_{os.getpid()}.txt")
   with open(listing, "w") as stream:
      for name, _ in chunks:
         stream.write(f"file '{name}'\n")
   partial = os.path.splitext(target)[0] + ".partial.mp4"
   command = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
              "-f", "concat", "-safe", "0", "-i", listing] + \
             trim + ["-i", source,
              "-map", "0:v", "-map", "1:a?", "-c:v", "copy",
              "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                                # the video's own length: -shortest
                                # would cut frames when the audio track
                                # ends a little early
              "-t", f"{total / fps:.3f}", partial]
   result = subprocess.run(command, capture_output = True, text = True)
   os.remove(listing)
   if (result.returncode != 0):
      if (os.path.exists(partial)):
         os.remove(partial)
      raise(ExceptionClass(f"{source}: ffmpeg could not write {target}:"
                           f"\n{result.stderr.strip()}"))
   os.replace(partial, target)
   return(total)

###########################################################################

def preview_path(target):
   """
      post: returns where -a writes the part of target done so far:
            <name>-preview.mp4 for a target of <name>_restored.mp4,
            else the target's own name with -preview added
      imp: the input's own name is kept exactly, underscores and all:
           "tape_.mp4" gives "tape_-preview.mp4"
   """
   base = os.path.splitext(target)[0]
   if (base.endswith("_restored")):
      base = base[:-len("_restored")]
   return(base + "-preview.mp4")

###########################################################################

def assemble(source, target):
   """
      use: for -a: joins the chunks of target finished so far into
           preview_path(target), leaving the work folder as it is
      post: returns the number of frames written, 0 when no work has
            been saved yet
      imp: only reads the work folder; chunks named in state.json are
           complete and never change, so this is safe while the
           restoration is still running
   """
   folder = target + ".parts"
   try:
      with open(os.path.join(folder, "state.json")) as stream:
         state = json.load(stream)
   except (OSError, ValueError):
      return(0)
   chunks = [tuple(chunk) for chunk in state.get("chunks", [])]
   if (not chunks):
      return(0)
   settings = state["settings"]
   options  = settings["options"]
   return(join_chunks(folder, chunks, source,
                      trim_params(options["p"], options["l"]),
                      settings["fps"] or 25, preview_path(target)))

###########################################################################

def duration(seconds):
   """
      post: returns seconds as days, hours and minutes, or under a day
            as hours, minutes and seconds, each unit attached to its
            number and leading units that are 0 left out:
            "3days 23hr 41min", "1day 0hr 5min", "11hr 20min 13sec",
            "20min 0sec", "45sec"
   """
   total = int(round(seconds))
   if (total >= 24 * 60 * 60):
      minutes        = int(round(seconds / 60))
      days, minutes  = divmod(minutes, 24 * 60)
      hours, minutes = divmod(minutes, 60)
      units          = [(days, "day" if (days == 1) else "days"),
                        (hours, "hr"), (minutes, "min")]
   else:
      hours, rest    = divmod(total, 60 * 60)
      minutes, rest  = divmod(rest, 60)
      units          = [(hours, "hr"), (minutes, "min"), (rest, "sec")]
                                # drop leading zeros, always keeping
                                # the last unit
   while ((len(units) > 1) and (units[0][0] == 0)):
      units.pop(0)
   return(" ".join(f"{number}{unit}" for number, unit in units))

###########################################################################

def open_chunk(folder, number, size, fps, opts):
   """
      use: starts encoding a chunk under a temporary name
      post: returns (final name, temporary path, writer)
   """
   name   = f"chunk_{number:05d}.mp4"
   temp   = os.path.join(folder, f"chunk_{number:05d}.partial.mp4")
   writer = imageio_ffmpeg.write_frames(
               temp, size, fps = fps, codec = "libx264", quality = None,
               macro_block_size = 2, ffmpeg_log_level = "error",
                                # veryfast: much quicker than the
                                # default for little size cost at this
                                # crf; the ai, not the encoder, is
                                # where the time goes
               output_params = ["-crf", str(opts.q), "-preset", "veryfast"])
   writer.send(None)
   return(name, temp, writer)

###########################################################################

def settings_of(engine, source, meta):
   """
      post: returns everything that shapes the output, so work saved
            by an earlier run is only reused when it would come out the
            same
   """
   opts = engine.opts
   info = os.stat(source)
   return({"source"   : os.path.abspath(source),
           "bytes"    : info.st_size,
           "modified" : info.st_mtime_ns,
           "size"     : list(meta["size"]),
           "fps"      : meta["fps"],
           "model"    : engine.model_name,
           "options"  : {name: getattr(opts, name)
                         for name in ("s", "d", "f", "k", "p", "l", "q")},
           "revision" : 1})

###########################################################################

def restore_file(engine, source, target):
   """
      use: enhances one video file, writing target
      imp: frames are encoded in chunks into a work folder beside the
           target (see Checkpoint); at the end ffmpeg joins the chunks
           without re-encoding them and adds the source's soundtrack
           (as aac, which every mp4 player accepts), trimmed the same
           way, then the work folder is removed
           on a rerun, frames already done are decoded but not
           processed again: decoding is quick next to the ai, and is
           the only way to land on exactly the right frame
   """
   opts   = engine.opts
   reader = imageio_ffmpeg.read_frames(source, input_params = trim_params(
                                                                opts.p,
                                                                opts.l))
   meta   = next(reader)
   w, h   = meta["size"]
   fps    = meta["fps"] or 25
   length = opts.l if (opts.l > 0) else max(0, meta["duration"] - opts.p)
   count  = max(1, round(length * fps))
   size   = (even(w * opts.s), even(h * opts.s))
   radius = 1 + round(2 * opts.d)
   log(0, 1, f"{source}: {w}x{h} at {fps:g} fps, about {count} frames")
                                # "->" goes on the next line, placed so
                                # the new size lines up under the old
   column = len(f"{SELF}: {source}: ")
   print(" " * max(0, column - 3) + f"-> {size[0]}x{size[1]}"
         + ("" if (meta.get("audio_codec")) else ", no audio"),
         file = sys.stderr)

   checkpoint = Checkpoint(target, settings_of(engine, source, meta))
   if (opts.x):
      checkpoint.clear()
   done = checkpoint.load()
   if (done):
      say(f"{source}: carrying on after frame {done}; frames already "
          "done are skipped over")
   sigma    = checkpoint.sigma
   previous = checkpoint.previous
   aligner  = Aligner(w, h)

                                # frames are read a few ahead of the
                                # one being worked on, so how many have
                                # been read corrects the estimated
                                # total, and the end of the video gives
                                # the exact one
   reading = {"seen" : 0, "ended" : False}

   def frames():
      for raw in reader:
         frame = numpy.frombuffer(raw, numpy.uint8).reshape(h, w, 3)
         reading["seen"] += 1
         yield((frame, aligner.prepare(frame)))
      reading["ended"] = True

   started  = time.time()
   index    = 0
   made     = 0
   writer   = None
   status   = Status()
   try:
      for window, centre in windows(frames(), radius):
         if (index < done):
            index += 1
            continue
         frame = window[centre][0]
         if (sigma is None):
            sigma = noise_level(frame)
            log(1, opts.v, f"grain level about {sigma:.1f}")
         clean, back = temporal_denoise(aligner, window, centre, opts.d,
                                        sigma)
         if (engine.model_name == "none"):
            big = cv2.resize(clean, size, interpolation = cv2.INTER_LANCZOS4)
         else:
            big = upscale(engine, clean)
            big = cv2.resize(big, size,
                             interpolation = cv2.INTER_AREA
                                             if (big.shape[1] > size[0])
                                             else cv2.INTER_CUBIC)
         big      = steady(aligner, big, previous, back, opts.f)
         previous = big
         out      = sharpen(big, opts.k, opts.s) if (opts.k > 0) else big

         if (writer is None):
            name, temp, writer = open_chunk(checkpoint.folder,
                                            len(checkpoint.chunks), size,
                                            fps, opts)
            in_chunk      = 0
            chunk_started = time.time()
         writer.send(numpy.ascontiguousarray(out).tobytes())
         in_chunk += 1
         index    += 1
         made     += 1
         if ((in_chunk >= CHUNK_FRAMES) or
             (time.time() - chunk_started >= CHUNK_SECONDS)):
            writer.close()
            writer = None
            os.replace(temp, os.path.join(checkpoint.folder, name))
            checkpoint.add(name, in_chunk, previous, sigma)
            if (opts.v > 0):
               status.end()
               log(1, opts.v, f"saved up to frame {index}")

         rate = made / max(time.time() - started, 1e-6)
         if (reading["ended"]):
            count, sign = reading["seen"], ""
         else:
            count, sign = max(count, reading["seen"]), "~"
         left = max(count - index, 0) / max(rate, 1e-6)
         status.show(f"{SELF}: {index} of {sign}{count}, {rate:.2f} fps, "
                     f"{duration(left)} remaining")
      if (writer is not None):
         writer.close()
         writer = None
         os.replace(temp, os.path.join(checkpoint.folder, name))
         checkpoint.add(name, in_chunk, previous, sigma)
   except KeyboardInterrupt:
                                # the chunk in progress is incomplete:
                                # drop it; everything before is kept
      if (writer is not None):
         try:
            writer.close()
         except Exception:
            pass
         try:
            os.remove(temp)
         except OSError:
            pass
      saved = sum(frames for _, frames in checkpoint.chunks)
      status.end()
      say(f"{source}: interrupted; {saved} frames are saved. Run the "
          "same command again to carry on")
      raise
   finally:
      reader.close()
   status.end()

   if (not checkpoint.chunks):
      checkpoint.clear()
      raise(ExceptionClass(f"{source}: no frames could be read"))
   total = join_chunks(checkpoint.folder, checkpoint.chunks, source,
                       trim_params(opts.p, opts.l), fps, target)
   checkpoint.clear()
                                # a -a preview is out of date now
   if (os.path.exists(preview_path(target))):
      os.remove(preview_path(target))
   elapsed = time.time() - started
   log(0, 1, f"{source} -> {target} ({total} frames, {elapsed / 60:.1f} "
             "min this run)")

###########################################################################

def parse(argv):
   """
      post: returns parsed options with -v as a count
   """
   parser = UsageParser(prog = SELF, add_help = False)
   parser.add_argument("-s", type = float, default = 1.0)
   parser.add_argument("-d", type = float, default = 0.5)
   parser.add_argument("-f", type = float, default = 0.5)
   parser.add_argument("-k", type = float, default = 0.3)
   parser.add_argument("-m", default = "auto",
                       choices = ("auto", "photo", "general", "anime",
                                  "none"))
   parser.add_argument("-p", type = float, default = 0.0)
   parser.add_argument("-l", type = float, default = 0.0)
   parser.add_argument("-q", type = int,   default = 18)
   parser.add_argument("-t", type = int,   default = 0)
   parser.add_argument("-o", default = "")
   parser.add_argument("-c", action = "store_true")
   parser.add_argument("-x", action = "store_true")
   parser.add_argument("-a", action = "store_true")
   parser.add_argument("-v", action = "count", default = 0)
   parser.add_argument("-n", action = "store_true")
   parser.add_argument("-h", action = "store_true")
   parser.add_argument("videos", nargs = "*")
   opts = parser.parse_args(argv)

   if (opts.h):
      print(USAGE)
      sys.exit(E_SUCCESS)
   if (not opts.videos):
      raise(ExceptionClass(f"no videos given\n{USAGE}", code = E_USAGE))
   if (opts.s < 1):
      raise(ExceptionClass("-s must be 1 or more", code = E_USAGE))
   for name in ("d", "f", "k"):
      if (not (0 <= getattr(opts, name) <= 1)):
         raise(ExceptionClass(f"-{name} must be 0.0 to 1.0",
                              code = E_USAGE))
   if ((opts.p < 0) or (opts.l < 0)):
      raise(ExceptionClass("-p and -l must be 0 or more",
                           code = E_USAGE))
   if (not (0 <= opts.q <= 51)):
      raise(ExceptionClass("-q must be 0 to 51", code = E_USAGE))
   if (opts.t < 0):
      raise(ExceptionClass("-t must be 0 or more", code = E_USAGE))
   if (opts.a and opts.x):
      raise(ExceptionClass("-a and -x do not go together",
                           code = E_USAGE))

                                # cmd.exe does not expand wildcards
   videos = []
   for pattern in opts.videos:
      videos.extend(sorted(glob.glob(pattern)) or [pattern])
   opts.videos = videos
   return(opts)

def assemble_all(opts, videos, multiple, missing):
   """
      use: carries out -a for each video
      post: returns the exit status: a warning when a video has
            nothing to assemble
   """
   worst = E_WARN if (missing) else E_SUCCESS
   for video in videos:
      target = output_path(video, opts.o, multiple)
      if (os.path.exists(target)):
         say(f"{video}: finished already, as {target}; nothing to "
             "assemble")
         continue
      if (not os.path.isdir(target + ".parts")):
         say(f"{video}: no work saved yet for {target}; nothing to "
             "assemble")
         worst = max(worst, E_WARN)
         continue
      if (opts.n):
         print(f"{video} -> {preview_path(target)} (part done so far)")
         continue
      try:
         frames = assemble(video, target)
      except ExceptionClass as error:
         error.print_error()
         worst = E_FAIL
         continue
      except (OSError, ValueError, KeyError) as error:
         print(f"{SELF}: {video}: {error}", file = sys.stderr)
         worst = E_FAIL
         continue
      if (frames == 0):
         say(f"{video}: no chunk finished yet; nothing to assemble")
         worst = max(worst, E_WARN)
      else:
         say(f"{video}: {frames} frames done so far -> "
             f"{preview_path(target)}")
   return(worst)

###########################################################################
###
### s t a r t    o f    e x e c u t i o n
###
###########################################################################

def main(argv):
   opts = parse(argv)

   global torch, spandrel, numpy, cv2, imageio_ffmpeg
   global Image, ImageFilter
   try:
      import torch
      import spandrel
      import numpy
      import cv2
      import imageio_ffmpeg
      from PIL import Image, ImageFilter
   except ImportError as error:
      raise(ExceptionClass(f"{error.name} is not installed; run\n"
                           "   pip install torch spandrel pillow numpy "
                           "opencv-python imageio-ffmpeg"))
   warnings.filterwarnings("ignore", category = FutureWarning)

   missing = [video for video in opts.videos
              if (not os.path.isfile(video))]
   for video in missing:
      print(f"{SELF}: {video}: no such file", file = sys.stderr)
   videos   = [video for video in opts.videos if (video not in missing)]
   multiple = len(videos) > 1

   if (opts.a):
      return(assemble_all(opts, videos, multiple, missing))

   engine = Engine(opts)
   engine.describe()

   if (opts.n):
      for video in videos:
         target = output_path(video, opts.o, multiple)
         note   = (     "" if (opts.x)
                   else " (done; skipped)" if (os.path.exists(target))
                   else " (partly done; carries on)"
                        if (os.path.isdir(target + ".parts"))
                   else "")
         print(f"{video} -> {target}{note}")
      return(E_WARN if (missing) else E_SUCCESS)

   failed = len(missing)
   for video in videos:
      target = output_path(video, opts.o, multiple)
      if (os.path.exists(target) and not opts.x):
         log(0, 1, f"{video}: {target} already done; skipped (-x to "
                   "redo)")
         continue
      try:
         restore_file(engine, video, target)
      except (OSError, ValueError, RuntimeError) as error:
         print(f"{SELF}: {video}: {error}", file = sys.stderr)
         failed += 1

   return(     E_SUCCESS if (failed == 0)
          else E_FAIL    if (failed == len(opts.videos))
          else E_WARN)

###########################################################################

if ( __name__ == "__main__" ):
   try:
      sys.exit(main(sys.argv[1:]))
   except ExceptionClass as exception:
      exception.print_error()
      sys.exit(exception.code)
   except KeyboardInterrupt:
      print(f"{SELF}: interrupted", file = sys.stderr)
      sys.exit(E_FAIL)
