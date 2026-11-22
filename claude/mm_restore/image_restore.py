#!/usr/bin/python
# name: $Id: image_restore.py 2.main-2 11:47:04 22-Nov-2026 claude-opus-5-5 $
# use: restore old or damaged photos locally: remove scratches and dust,
#      deblur, denoise, upscale, rebuild faces and sharpen
"""
   name: image_restore.py
   use: ai photo restoration that runs entirely on this computer,
        in the spirit of the online "enhance" tools; for each image:
           1. finds scratches and dust and paints over them (lama)
           2. optionally removes focus blur (restormer)
           3. removes grain/jpeg noise and upscales (real-esrgan)
           4. finds faces and rebuilds them in detail (gfpgan)
           5. optionally sharpens
        device (cuda, apple mps, cpu), model, precision and tile size
        are chosen to suit the computer it is running on
   usage: image_restore.py [options] image [image ...]
          image_restore.py -h for the option list
          mm_restore.py takes images and videos together
          examples:
             image_restore.py grandma.jpg
             image_restore.py -s2 -r0.7 -w scan.png  (more scratch
                                                      removal, save
                                                      mask to check)
             image_restore.py -imask.png scan.png    (paint your own
                                                      mask)
             image_restore.py -r0 -f0 photo.jpg      (plain upscale)
             image_restore.py -b0.8 -d0.3 blurry.jpg (out of focus)
   pre: python 3.9 or later, and
           pip install torch spandrel pillow numpy opencv-python
        and for -b only
           pip install spandrel_extra_arches
        for an nvidia gpu, install torch from the pytorch.org index for
        your cuda version FIRST (see https://pytorch.org/get-started),
        otherwise pip installs a build that may not use the gpu
   post: writes <name>_restored.png beside each input (or into -o)
         each image is written under a temporary name and renamed when
         complete, so if a batch is interrupted, rerunning the same
         command skips the images already done and carries on
   imp: model weights (~600 MB in all, ~700 MB with -b) download once
        into the cache
        directory (%LOCALAPPDATA%\\restore on windows,
        ~/.cache/restore elsewhere), shared with video_restore.py
        16-bit images are processed as 8-bit
        gfpgan rebuilds faces from what it learned, so a badly damaged
        face can come back looking slightly unlike the person; lower -f
        to keep more of the original
        restormer, the deblur model, is licensed for non-commercial
        use only, which is why spandrel ships it in a separate package
"""

import argparse
import glob
import math
import os
import sys
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
                                #          (blend of the two "general"
                                #          weight sets)
                                # anime:   RRDB net tuned for line art
                                # yunet is stored in git lfs, so the
                                # media host is used rather than /raw/
WEIGHTS = {
   "photo"       : ESRGAN + "v0.1.0/RealESRGAN_x4plus.pth",
   "general"     : ESRGAN + "v0.2.5.0/realesr-general-x4v3.pth",
   "general-wdn" : ESRGAN + "v0.2.5.0/realesr-general-wdn-x4v3.pth",
   "anime"       : ESRGAN + "v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth",
   "gfpgan"      : "https://github.com/TencentARC/GFPGAN/releases/"
                   "download/v1.3.0/GFPGANv1.4.pth",
   "lama"        : "https://github.com/Sanster/models/releases/"
                   "download/add_big_lama/big-lama.pt",
   "deblur"      : "https://github.com/swz30/Restormer/releases/"
                   "download/v1.0/single_image_defocus_deblurring.pth",
   "yunet"       : "https://media.githubusercontent.com/media/opencv/"
                   "opencv_zoo/main/models/face_detection_yunet/"
                   "face_detection_yunet_2023mar.onnx",
}

                                # eye, eye, nose, mouth corner, mouth
                                # corner positions in a 512x512 ffhq
                                # face, the layout gfpgan was trained on
FACE_SIZE     = 512
FACE_TEMPLATE = [[192.98138, 239.94708],
                 [318.90277, 240.19360],
                 [256.63416, 314.01935],
                 [201.26117, 371.41043],
                 [313.08905, 371.15118]]

USAGE = f"""{SELF} {REVISION}
usage: {SELF} [options] image [image ...]
where options are:
   -sScale    output scale factor, 1 or more (default 1: restore
              without enlarging)
   -rStrength scratch and dust detection 0.0 to 1.0 (default 0.5);
              0 turns automatic detection off
   -iMask     your own repair mask: an image the same shape as the
              input, white where it should be repaired; with one image
              only; combined with automatic detection unless -r0
   -w         also write the repair mask as <name>_mask.png, to see
              what was repaired
   -fWeight   face restoration 0.0 to 1.0 (default 0.7); 0 is off,
              1 uses the rebuilt face entirely
   -dStrength denoise strength 0.0 to 1.0
              general model: blends weak/strong denoise (default 0.5)
              photo/anime:   extra denoise pass before upscaling
                             (default off; the model already denoises)
   -bStrength deblur strength 0.0 to 1.0 for out-of-focus photos
              (default 0, off); grain is removed first, as deblurring
              needs a clean image; needs spandrel_extra_arches; slow
              on a cpu
   -kStrength sharpen strength 0.0 to 1.0, applied last (default 0)
   -mModel    auto, photo, general or anime (default auto: photo on a
              capable gpu, general on cpu or a small gpu)
   -tTile     tile size in pixels; 0 = choose from available memory
   -oOutput   output directory, or output file for a single image
   -c         use the cpu even if a gpu is present
   -x         redo: restore images even when their output already
              exists (without -x they are skipped, so an interrupted
              batch carries on where it stopped)
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
      use: holds the device and the loaded models, loading each model
           the first time it is needed
   """
   def __init__(self, opts):
      self.opts = opts
      self.device, self.description, self.free = pick_device(opts.c)
      self.model_name = pick_model(opts.m, self.device, self.free)
      self.tile       = pick_tile(opts.t, self.device, self.free,
                                  self.model_name)
      self.strength   = 0.5 if (opts.d is None) else opts.d
      self.models     = {}

###########################################################################

   def describe(self):
      opts  = self.opts
      parts = [f"model {self.model_name}",
               f"tile {self.tile if (self.tile) else 'whole image'}",
               f"scale {opts.s:g}",
               f"scratches {opts.r:g}" + (" + mask" if (opts.i) else ""),
               f"faces {opts.f:g}"]
      if (self.model_name == "general"):
         parts.append(f"denoise {self.strength:g}")
      elif (opts.d):
         parts.append(f"pre-denoise {opts.d:g}")
      if (opts.b):
         parts.append(f"deblur {opts.b:g}")
      if (opts.k):
         parts.append(f"sharpen {opts.k:g}")
      log(0, 1, f"device {self.description}")
      log(0, 1, ", ".join(parts))

###########################################################################

   def get(self, name):
      """
         post: returns the named model (upscaler, lama, gfpgan, yunet),
               loading it on first use
      """
      if (name not in self.models):
         loader = {"upscaler" : self.load_upscaler,
                   "lama"     : self.load_lama,
                   "gfpgan"   : self.load_gfpgan,
                   "deblur"   : self.load_deblur,
                   "yunet"    : self.load_yunet}[name]
         self.models[name] = loader()
      return(self.models[name])

###########################################################################

   def load_upscaler(self):
      """
         post: returns (model descriptor, dtype)
      """
      verbose = self.opts.v
      if (self.model_name == "general"):
         strong = load_state(fetch_weights("general",     verbose))
         weak   = load_state(fetch_weights("general-wdn", verbose))
                                # deep network interpolation, as done
                                # by real-esrgan's own -dn option
         s      = self.strength
         state  = {key: s * strong[key] + (1 - s) * weak[key]
                   for key in strong}
      else:
         state  = load_state(fetch_weights(self.model_name, verbose))
      model = spandrel.ModelLoader().load_from_state_dict(state)
      return(self.place(model))

###########################################################################

   def load_lama(self):
      path = fetch_weights("lama", self.opts.v)
      return(self.place(spandrel.ModelLoader().load_from_file(path)))

###########################################################################

   def load_gfpgan(self):
      path = fetch_weights("gfpgan", self.opts.v)
      return(self.place(spandrel.ModelLoader().load_from_file(path)))

###########################################################################

   def load_deblur(self):
      """
         imp: restormer lives in spandrel_extra_arches, which has to
              register its architectures with spandrel before loading
      """
      try:
         import spandrel_extra_arches
      except ImportError:
         raise(ExceptionClass("-b needs spandrel_extra_arches; run\n"
                              "   pip install spandrel_extra_arches"))
      spandrel_extra_arches.install(ignore_duplicates = True)
      path = fetch_weights("deblur", self.opts.v)
      return(self.place(spandrel.ModelLoader().load_from_file(path)))

###########################################################################

   def load_yunet(self):
      path = fetch_weights("yunet", self.opts.v)
      return(cv2.FaceDetectorYN.create(path, "", (320, 320), 0.7, 0.3,
                                       5000))

###########################################################################

   def place(self, model):
      """
         use: moves a model to the device, in half precision on cuda
              when the architecture allows it
         post: returns (model, dtype)
      """
      model.eval().to(self.device)
      dtype = torch.float32
      if ((self.device.type == "cuda") and model.supports_half):
         model.half()
         dtype = torch.float16
      log(2, self.opts.v, f"loaded {model.architecture.name}, {dtype}")
      return(model, dtype)

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
            if (self.device.type == "cuda"):
               torch.cuda.empty_cache()

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

def cache_dir():
   """
      post: returns the weights cache directory, creating it if needed
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
           compact general model, so on a cpu or a gpu under ~3 GiB
           free the general model is the better trade
   """
   if (requested != "auto"):
      return(requested)
   if (device.type == "mps"):
      return("photo")
   if ((device.type == "cuda") and (free >= 3 * 2**30)):
      return("photo")
   return("general")

###########################################################################

def pick_tile(requested, device, free, model_name):
   """
      post: returns the upscaling tile size, 0 meaning the whole image
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
                                # cpu and mps: memory is rarely the
                                # limit, but moderate tiles keep ram use
                                # and swap in check on large images
      tile = 512
   return(tile if (heavy) else tile * 2)

###########################################################################

def to_tensor(rgb):
   """
      post: returns a (1,3,h,w) float tensor in 0..1 from uint8 h,w,3
   """
   tensor = torch.from_numpy(numpy.ascontiguousarray(rgb))
   return(tensor.permute(2, 0, 1).unsqueeze(0).float().div_(255))

###########################################################################

def to_array(tensor):
   """
      post: returns uint8 h,w,3 from a (1,3,h,w) float tensor in 0..1
   """
   return(tensor.squeeze(0).permute(1, 2, 0).clamp(0, 1).mul(255)
          .round().byte().numpy())

###########################################################################
###
### s c r a t c h e s
###
###########################################################################

def line_kernels(length, count = 16):
   """
      post: returns count normalised 1px line kernels of the given odd
            length, at evenly spaced angles over 180 degrees
   """
   kernels = []
   centre  = (length - 1) / 2
   for number in range(count):
      angle  = math.pi * number / count
      dx, dy = math.cos(angle) * centre, math.sin(angle) * centre
      kernel = numpy.zeros((length, length), numpy.float32)
      cv2.line(kernel, (round(centre - dx), round(centre - dy)),
                       (round(centre + dx), round(centre + dy)), 1, 1)
      kernels.append(kernel / kernel.sum())
   return(kernels)

###########################################################################

def detect_scratches(rgb, strength, eyes, verbose):
   """
      use: finds scratches, cracks and hairs (thin bright or dark lines,
           straight or gently curved) and dust specks
      pre: eyes is a list of (x, y, radius) areas where dust is not
           looked for
      post: returns a uint8 mask the size of rgb, 255 where repair is
            wanted
      imp: 1. top-hat/black-hat keeps only features narrower than a
              small kernel, brighter/darker than their surroundings
           2. averaging that along short lines at 16 angles, less the
              local average, scores continuous lines high and grain or
              texture low (the retinal-vessel "line detector")
           3. a probabilistic hough transform joins the broken pieces
              of each scratch into segments; a segment is kept when
              most of its length really was detected
           straight features in the picture (poles, straps, stripes)
           can be taken for scratches; painting over them usually
           changes little, but -w shows the mask so -r can be tuned
           work is done at 1600px at most, for speed and so wide
           scratches in big scans look thin
   """
   h, w   = rgb.shape[:2]
   factor = min(1.0, 1600 / max(h, w))
   small  = cv2.resize(rgb, (max(1, round(w * factor)),
                             max(1, round(h * factor))),
                       interpolation = cv2.INTER_AREA)
   sh, sw = small.shape[:2]
   gray   = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
   size   = max(7, round(min(sh, sw) / 150)) | 1
   length = max(15, round(min(sh, sw) / 40)) | 1
   kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
   lines  = line_kernels(length)

   score = numpy.zeros((sh, sw), numpy.float32)
   thin  = numpy.zeros((sh, sw), numpy.float32)
   for operation in (cv2.MORPH_TOPHAT, cv2.MORPH_BLACKHAT):
      response = cv2.morphologyEx(gray, operation, kernel)
      response = response.astype(numpy.float32)
      along    = numpy.max([cv2.filter2D(response, -1, line)
                            for line in lines], axis = 0)
      score    = numpy.maximum(score, along - cv2.blur(response,
                                                       (length, length)))
      thin     = numpy.maximum(thin, response)

                                # robust spread: median absolute
                                # deviation, so busy pictures do not
                                # raise the bar as std would
   median = float(numpy.median(score))
   spread = float(numpy.median(numpy.abs(score - median))) * 1.4826 + 1e-6
   binary = (score > median + (7 - 4 * strength) * spread)
   binary = binary.astype(numpy.uint8)
   near   = cv2.dilate(binary, numpy.ones((3, 3), numpy.uint8))
   found  = cv2.HoughLinesP(binary * 255, 1, math.pi / 360,
                            threshold     = round(0.7 * length),
                            minLineLength = 3 * length,
                            maxLineGap    = 3 * length)

   def fill(xs, ys, nx, ny, offset):
      xx = (xs + nx * offset).round().astype(int).clip(0, sw - 1)
      yy = (ys + ny * offset).round().astype(int).clip(0, sh - 1)
      return(near[yy, xx].mean())

                                # a scratch is a lone line: most of the
                                # segment detected, and clearly more
                                # than in parallel strips either side
                                # (wood grain, fabric, hair fail this)
   standout = 0.45 - 0.3 * strength
   mask     = numpy.zeros((sh, sw), numpy.uint8)
   kept     = 0
   for x1, y1, x2, y2 in ([] if (found is None) else found.reshape(-1, 4)):
      span   = math.hypot(x2 - x1, y2 - y1)
      steps  = max(abs(x2 - x1), abs(y2 - y1)) + 1
      xs     = numpy.linspace(x1, x2, steps)
      ys     = numpy.linspace(y1, y2, steps)
      nx, ny = -(y2 - y1) / span, (x2 - x1) / span
      on     = fill(xs, ys, nx, ny, 0)
      side   = max(fill(xs, ys, nx, ny, offset * size)
                   for offset in (-2.5, -1.5, 1.5, 2.5))
      if ((on >= 0.5) and (on - side >= standout)):
         cv2.line(mask, (int(x1), int(y1)), (int(x2), int(y2)), 255, 1)
         kept += 1
                                # widen each segment to the scratch's
                                # real width: detected thin pixels near
                                # the segment
   band = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                     (size, size)))
   mask = numpy.maximum(mask, numpy.where((band > 0) & (binary > 0),
                                          255, 0).astype(numpy.uint8))

                                # dust: small, very strong specks, not
                                # in eyes, as catchlights look exactly
                                # like dust
                                # judged against the neighbourhood, so
                                # one grain among many is not dust
   window = (5 * size, 5 * size)
   local  = cv2.blur(thin, window)
   spread = numpy.sqrt(numpy.maximum(cv2.blur(thin * thin, window) -
                                     local * local, 0))
   specks = ((thin > local + (8 - 6 * strength) * spread) &
             (thin > 40)).astype(numpy.uint8)
   count, labels, stats, _ = cv2.connectedComponentsWithStats(specks, 8)
   dust = 0
   for label in range(1, count):
      x, y, bw, bh, area = stats[label]
      if ((area > size * size) or (max(bw, bh) > size)):
         continue
      cx, cy = (x + bw / 2) / factor, (y + bh / 2) / factor
      if any(math.hypot(cx - ex, cy - ey) <= radius
             for ex, ey, radius in eyes):
         continue
                                # a wider margin than scratches get:
                                # any halo left showing gets continued
                                # back into the hole
      x0, y0 = max(x - 1, 0), max(y - 1, 0)
      mask[y0:y + bh + 1, x0:x + bw + 1] = 255
      dust += 1

                                # grow to cover each mark's soft edges,
                                # then back to full size
   grow = max(1, size // 4)
   mask = cv2.dilate(mask, cv2.getStructuringElement(
                              cv2.MORPH_ELLIPSE, (2 * grow + 1,) * 2))
   if (factor < 1):
      mask = cv2.resize(mask, (w, h), interpolation = cv2.INTER_LINEAR)
      mask = numpy.where(mask > 0, 255, 0).astype(numpy.uint8)
   log(1, verbose, f"scratch detection: {kept} scratch segment(s), "
                   f"{dust} dust speck(s), "
                   f"{100 * (mask > 0).mean():.2f}% of the image")
   return(mask)

###########################################################################

def inpaint(engine, rgb, mask):
   """
      use: paints over the masked areas with lama, in tiles around
           the damage so large images fit in memory
      post: returns the repaired uint8 h,w,3 array; unmasked pixels are
            unchanged
   """
   model, dtype = engine.get("lama")
   h, w   = rgb.shape[:2]
   result = rgb.copy()

   def run(tile):
      tile = tile if (tile > 0) else max(h, w)
      pad  = 128 if (tile < max(h, w)) else 0
      for y in range(0, h, tile):
         for x in range(0, w, tile):
            ex, ey = min(x + tile, w), min(y + tile, h)
            if (not mask[y:ey, x:ex].any()):
               continue
            x0, y0 = max(x - pad, 0), max(y - pad, 0)
            x1, y1 = min(ex + pad, w), min(ey + pad, h)
            image  = to_tensor(rgb[y0:y1, x0:x1])
            hole   = torch.from_numpy(
                        (mask[y0:y1, x0:x1] > 0).astype(numpy.float32))
            with torch.inference_mode():
               out = model(image.to(engine.device, dtype),
                           hole[None, None].to(engine.device, dtype))
            out    = to_array(out.float().cpu())
            core   = (slice(y - y0, ey - y0), slice(x - x0, ex - x0))
            keep   = mask[y:ey, x:ex] > 0
            result[y:ey, x:ex][keep] = out[core][keep]
      return(result)

                                # lama was trained on ~512px images; it
                                # copes with more, but tiles keep memory
                                # in hand on big scans
   tile = 1024 if (max(h, w) > 1536) else 0
   return(engine.oom_retry(run, tile, max(h, w)))

###########################################################################
###
### u p s c a l i n g
###
###########################################################################

def tile_ramps(size, tile, overlap, scale):
   """
      use: lays tiles along one axis of an image
      post: returns [(start, end, weights)]: tile spans in input pixels
            and, at output scale, a weight per output pixel; toward
            each neighbour the weight is 0 for the outermost margin
            pixels, which see too little of the picture to be
            trusted, then ramps up to 1 across the rest of the overlap
      imp: the last tile is placed flush with the far edge, so it may
           overlap its neighbour by more than overlap; the weights are
           normalised afterwards, so that only widens the blend
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

def run_tiles(engine, model, dtype, source, tile, overlap):
   """
      use: runs model over source, a (1,3,h,w) cpu tensor, in tiles
           that overlap and are cross-faded into each other
      post: returns the (1,3,h*scale,w*scale) cpu result in 0..1
      imp: cutting tiles apart at hard edges leaves visible seams:
           models such as restormer adjust to each tile's overall
           content, so neighbouring tiles differ slightly in tone and
           sharpness; a wide feathered overlap hides that
           the weights are a product of a row and a column ramp, so
           their sum is stored as two vectors, not a full-size image
   """
   scale      = model.scale
   _, _, h, w = source.shape
   rows       = tile_ramps(h, tile, overlap, scale)
   columns    = tile_ramps(w, tile, overlap, scale)
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
   return(out.clamp_(0, 1))

###########################################################################

def upscale(engine, rgb, scale):
   """
      use: denoises and upscales with real-esrgan in overlapping tiles,
           repeating for scales above the model's own
      post: returns the uint8 array at the model's scale ** passes
   """
   model, dtype = engine.get("upscaler")
   factor = model.scale
   tensor = to_tensor(rgb)
   passes = max(1, math.ceil(math.log(scale, factor) - 1e-9))

                                # convolutional: a 16px margin plus a
                                # 32px cross-fade is wider than any
                                # tile-edge effect
   def run_tiled(tile):
      return(run_tiles(engine, model, dtype, tensor, tile, 48))

   for number in range(passes):
      log(1, engine.opts.v, f"upscale pass {number + 1} of {passes}")
      _, _, h, w = tensor.shape
      tensor = engine.oom_retry(run_tiled, engine.tile, max(h, w))
   return(to_array(tensor))

###########################################################################

def deblur(engine, rgb, strength):
   """
      use: removes focus blur with restormer, in overlapping tiles
      post: returns the deblurred uint8 array, blended with the input
            by strength
      pre: rgb has had its grain removed (see restore_file): even
           light grain stops restormer from finding the blur, and it
           then does little
      imp: restormer was trained on whole camera frames, so it works
           on the image at its own size, not enlarged
           tiles overlap by a quarter of their size and are cross-faded
           (see run_tiles), so there are no visible seams
   """
   model, dtype = engine.get("deblur")
   h, w   = rgb.shape[:2]
   source = to_tensor(rgb)

                                # restormer's attention takes in the
                                # whole tile, so tiles differ in tone:
                                # a quarter-tile cross-fade hides it
   def run(tile):
      return(run_tiles(engine, model, dtype, source, tile,
                       max(48, tile // 4)))

                                # restormer's attention needs far more
                                # memory per pixel than the upscalers
   if (engine.device.type == "cuda"):
      gib  = engine.free / 2**30
      tile = 768 if (gib >= 10) else 512 if (gib >= 5) else 256
   else:
      tile = 512
   sharp = engine.oom_retry(run, tile, max(h, w))
   return(to_array(source + strength * (sharp.clamp(0, 1) - source)))

###########################################################################

def pre_denoise(rgb, strength, verbose):
   """
      use: opencv non-local-means denoise, for the rrdb models which
           have no adjustable denoise of their own
   """
   h = 3 + 12 * strength
   log(2, verbose, f"pre-denoise h={h:.1f}")
   return(cv2.fastNlMeansDenoisingColored(rgb, None, h, h, 7, 21))

###########################################################################
###
### f a c e s
###
###########################################################################

def detect_faces(engine, rgb):
   """
      post: returns a list of (box, landmarks) in rgb's pixels, where
            box is (x, y, w, h) and landmarks is a 5x2 float array
      imp: small images are enlarged for detection because yunet
           misses faces much under ~20px
   """
   detector = engine.get("yunet")
   h, w     = rgb.shape[:2]
   factor   = min(4.0, 1280 / max(h, w))
   size     = (max(1, round(w * factor)), max(1, round(h * factor)))
   image    = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), size,
                         interpolation = cv2.INTER_AREA if (factor < 1)
                                         else cv2.INTER_CUBIC)
   detector.setInputSize(size)
   _, found = detector.detect(image)
   faces = []
   for row in ([] if (found is None) else found):
      box       = tuple(int(round(v / factor)) for v in row[0:4])
      landmarks = (row[4:14] / factor).reshape(5, 2)
      if (box[2] >= 12):
         faces.append((box, landmarks))
   log(1, engine.opts.v, f"{len(faces)} face(s) found")
   return(faces)

###########################################################################

def restore_faces(engine, source, target, faces, weight):
   """
      use: for each face, cuts an aligned 512x512 crop out of source,
           rebuilds it with gfpgan and blends it into target
      pre: target is source enlarged to its own size; faces holds
           landmarks in source's pixels
      post: returns target with faces restored
   """
   model, dtype = engine.get("gfpgan")
   sh, sw   = source.shape[:2]
   th, tw   = target.shape[:2]
   sx, sy   = tw / sw, th / sh
   template = numpy.array(FACE_TEMPLATE, dtype = numpy.float32)
   result   = target.astype(numpy.float32)

                                # feathered square: the crop's own edge
                                # fades out so the seam does not show
   edge = numpy.zeros((FACE_SIZE, FACE_SIZE), numpy.float32)
   edge[24:-24, 24:-24] = 1
   edge = cv2.GaussianBlur(edge, (0, 0), 12)

   for _, landmarks in faces:
      matrix, _ = cv2.estimateAffinePartial2D(
                     landmarks.astype(numpy.float32), template,
                     method = cv2.LMEDS)
      if (matrix is None):
         continue
      crop = cv2.warpAffine(source, matrix, (FACE_SIZE, FACE_SIZE),
                            flags       = cv2.INTER_LINEAR,
                            borderMode  = cv2.BORDER_CONSTANT,
                            borderValue = (135, 133, 132))
      with torch.inference_mode():
         face = model(to_tensor(crop).to(engine.device, dtype))
      face = to_array(face.float().cpu()).astype(numpy.float32)

                                # same mapping, but from target pixels:
                                # divide out the enlargement
      scaled = matrix.copy()
      scaled[:, 0] /= sx
      scaled[:, 1] /= sy
      back   = cv2.invertAffineTransform(scaled)

                                # only warp into the face's bounding box
                                # rather than a whole-image canvas
      corners = numpy.array([[0, 0, 1], [FACE_SIZE, 0, 1],
                             [0, FACE_SIZE, 1], [FACE_SIZE, FACE_SIZE, 1]],
                            numpy.float32) @ back.T
      x0, y0 = numpy.floor(corners.min(0)).astype(int).clip(0)
      x1, y1 = numpy.ceil(corners.max(0)).astype(int)
      x1, y1 = min(x1, tw), min(y1, th)
      if ((x1 <= x0) or (y1 <= y0)):
         continue
      shift  = back.copy()
      shift[:, 2] -= (x0, y0)
      region = (x1 - x0, y1 - y0)
      paste  = cv2.warpAffine(face, shift, region, flags = cv2.INTER_CUBIC,
                              borderMode = cv2.BORDER_REPLICATE)
      alpha  = cv2.warpAffine(edge, shift, region,
                              flags = cv2.INTER_LINEAR) * weight
      alpha  = alpha[..., None]
      result[y0:y1, x0:x1] = (result[y0:y1, x0:x1] * (1 - alpha) +
                              paste * alpha)
   return(result.clip(0, 255).round().astype(numpy.uint8))

###########################################################################
###
### f i l e s
###
###########################################################################

def sharpen(picture, strength, scale, verbose):
   """
      use: unsharp mask on the finished rgb image
      imp: done last so grain the model removed is not exaggerated
           first; the radius grows with the scale because edges are
           wider in a more enlarged image; the threshold keeps flat
           areas (sky, skin) from turning gritty
   """
   radius  = (0.8 + 0.7 * strength) * max(1.0, scale / 2)
   percent = round(40 + 160 * strength)
   log(2, verbose, f"sharpen radius={radius:.2f} percent={percent}")
   return(picture.filter(ImageFilter.UnsharpMask(radius    = radius,
                                                 percent   = percent,
                                                 threshold = 2)))

###########################################################################

def output_path(source, output, multiple, suffix = "restored"):
   """
      post: returns where the result for source is written
   """
   stem = os.path.splitext(os.path.basename(source))[0]
   name = f"{stem}_{suffix}.png"
   if (not output):
      return(os.path.join(os.path.dirname(source), name))
   if (multiple or os.path.isdir(output) or
       not os.path.splitext(output)[1]):
      os.makedirs(output, exist_ok = True)
      return(os.path.join(output, name))
   return(output)

###########################################################################

def restore_file(engine, source, target):
   """
      use: runs the whole restoration on one image file
   """
   opts    = engine.opts
   started = time.time()
   picture = ImageOps.exif_transpose(Image.open(source))
   alpha   = None
   if (picture.mode in ("RGBA", "LA", "PA") or
       (picture.mode == "P" and "transparency" in picture.info)):
      alpha = picture.convert("RGBA").getchannel("A")
   rgb  = numpy.asarray(picture.convert("RGB")).copy()
   h, w = rgb.shape[:2]
   size = (max(1, round(w * opts.s)), max(1, round(h * opts.s)))

                                # faces first: dust detection must
                                # leave the catchlights in eyes alone
   faces = detect_faces(engine, rgb) if (opts.f > 0 or opts.r > 0) else []

   mask = numpy.zeros((h, w), numpy.uint8)
   if (opts.r > 0):
      eyes = []
      for _, points in faces:
         radius = 0.35 * math.hypot(*(points[1] - points[0]))
         eyes  += [(points[0][0], points[0][1], radius),
                   (points[1][0], points[1][1], radius)]
      mask = detect_scratches(rgb, opts.r, eyes, opts.v)
   if (opts.i):
      painted = Image.open(opts.i).convert("L").resize((w, h))
      mask    = numpy.maximum(mask,
                              numpy.where(numpy.asarray(painted) > 127,
                                          255, 0).astype(numpy.uint8))
   if ((mask > 0).mean() > 0.10):
      log(0, 1, f"{source}: repair mask covers "
                f"{100 * (mask > 0).mean():.0f}% of the image; if "
                "detail is lost, lower -r")
   if (opts.w):
      Image.fromarray(mask).save(os.path.splitext(target)[0] +
                                 "_mask.png")
   if (mask.any()):
      log(1, opts.v, f"{source}: repairing scratches")
      rgb = inpaint(engine, rgb, mask)

   if (opts.b > 0):
                                # grain off first, using the upscaler
                                # as a denoiser: enlarge, then shrink
                                # back; opencv's denoise was tried and
                                # leaves restormer a painted-looking
                                # image
      log(1, opts.v, f"{source}: denoising before deblur")
      model, _ = engine.get("upscaler")
      clean    = upscale(engine, rgb, model.scale)
      clean    = cv2.resize(clean, (w, h), interpolation = cv2.INTER_AREA)
      log(1, opts.v, f"{source}: removing focus blur")
      rgb = deblur(engine, clean, opts.b)

   if ((engine.model_name != "general") and opts.d):
      rgb = pre_denoise(rgb, opts.d, opts.v)

   log(1, opts.v, f"{source}: denoising and upscaling")
   big = upscale(engine, rgb, opts.s)
   if ((big.shape[1], big.shape[0]) != size):
      big = numpy.asarray(Image.fromarray(big).resize(size,
                                                      Image.LANCZOS))

   if ((opts.f > 0) and faces):
      log(1, opts.v, f"{source}: restoring {len(faces)} face(s)")
      big = restore_faces(engine, rgb, big, faces, opts.f)

   result = Image.fromarray(big)
   if (opts.k > 0):
      result = sharpen(result, opts.k, opts.s, opts.v)
   if (alpha is not None):
      result.putalpha(alpha.resize(size, Image.LANCZOS))
                                # a half-written file is never left
                                # under the real name, where a rerun
                                # would take it for a finished one
   base, extension = os.path.splitext(target)
   partial = f"{base}.partial{extension}"
   try:
      result.save(partial)
      os.replace(partial, target)
   except BaseException:
      if (os.path.exists(partial)):
         os.remove(partial)
      raise
   log(0, 1, f"{source} -> {target} ({size[0]}x{size[1]}, "
             f"{time.time() - started:.1f}s)")

###########################################################################

def parse(argv):
   """
      post: returns parsed options with -v as a count
   """
   parser = UsageParser(prog = SELF, add_help = False)
   parser.add_argument("-s", type = float, default = 1.0)
   parser.add_argument("-r", type = float, default = 0.5)
   parser.add_argument("-i", default = "")
   parser.add_argument("-w", action = "store_true")
   parser.add_argument("-f", type = float, default = 0.7)
   parser.add_argument("-d", type = float, default = None)
   parser.add_argument("-b", type = float, default = 0.0)
   parser.add_argument("-k", type = float, default = 0.0)
   parser.add_argument("-m", default = "auto",
                       choices = ("auto", "photo", "general", "anime"))
   parser.add_argument("-t", type = int,   default = 0)
   parser.add_argument("-o", default = "")
   parser.add_argument("-c", action = "store_true")
   parser.add_argument("-x", action = "store_true")
   parser.add_argument("-v", action = "count", default = 0)
   parser.add_argument("-n", action = "store_true")
   parser.add_argument("-h", action = "store_true")
   parser.add_argument("images", nargs = "*")
   opts = parser.parse_args(argv)

   if (opts.h):
      print(USAGE)
      sys.exit(E_SUCCESS)
   if (not opts.images):
      raise(ExceptionClass(f"no images given\n{USAGE}", code = E_USAGE))
   if (opts.s < 1):
      raise(ExceptionClass("-s must be 1 or more", code = E_USAGE))
   for name in ("r", "f", "d", "b", "k"):
      value = getattr(opts, name)
      if ((value is not None) and not (0 <= value <= 1)):
         raise(ExceptionClass(f"-{name} must be 0.0 to 1.0",
                              code = E_USAGE))
   if (opts.t < 0):
      raise(ExceptionClass("-t must be 0 or more", code = E_USAGE))

                                # cmd.exe does not expand wildcards
   images = []
   for pattern in opts.images:
      images.extend(sorted(glob.glob(pattern)) or [pattern])
   opts.images = images

   if (opts.i and (len(images) > 1)):
      raise(ExceptionClass("-i works with one image only",
                           code = E_USAGE))
   if (opts.i and not os.path.isfile(opts.i)):
      raise(ExceptionClass(f"{opts.i}: no such mask file",
                           code = E_USAGE))
   return(opts)

###########################################################################
###
### s t a r t    o f    e x e c u t i o n
###
###########################################################################

def main(argv):
   opts = parse(argv)

   global torch, spandrel, numpy, cv2, Image, ImageFilter, ImageOps
   try:
      import torch
      import spandrel
      import numpy
      import cv2
      from PIL import Image, ImageFilter, ImageOps
   except ImportError as error:
      raise(ExceptionClass(f"{error.name} is not installed; run\n"
                           "   pip install torch spandrel pillow numpy "
                           "opencv-python"))
                                # quiet the library chatter: torch warns
                                # that the lama file format is old, and
                                # opencv logs dnn backend notes
   warnings.filterwarnings("ignore", category = FutureWarning)
   try:
      cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
   except AttributeError:
      pass

   engine = Engine(opts)
   engine.describe()

   missing = [image for image in opts.images
              if (not os.path.isfile(image))]
   for image in missing:
      print(f"{SELF}: {image}: no such file", file = sys.stderr)
   images   = [image for image in opts.images if (image not in missing)]
   multiple = len(images) > 1

   if (opts.n):
      for image in images:
         target = output_path(image, opts.o, multiple)
         note   = (" (done; skipped)" if (os.path.exists(target) and
                                          not opts.x)
                   else "")
         print(f"{image} -> {target}{note}")
      return(E_WARN if (missing) else E_SUCCESS)

   failed = len(missing)
   done   = 0
   for image in images:
      target = output_path(image, opts.o, multiple)
      if (os.path.exists(target) and not opts.x):
         log(0, 1, f"{image}: {target} already done; skipped (-x to "
                   "redo)")
         done += 1
         continue
      try:
         restore_file(engine, image, target)
         done += 1
      except (OSError, ValueError) as error:
         print(f"{SELF}: {image}: {error}", file = sys.stderr)
         failed += 1
      except KeyboardInterrupt:
         print(f"\n{SELF}: interrupted; {done} of {len(images)} images "
               "done. Run the same command again to carry on",
               file = sys.stderr)
         raise

   return(     E_SUCCESS if (failed == 0)
          else E_FAIL    if (failed == len(opts.images))
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
