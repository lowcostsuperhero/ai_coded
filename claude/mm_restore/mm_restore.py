#!/usr/bin/python
# name: $Id: mm_restore.py 2.main-2 11:47:04 22-Nov-2026 claude-opus-5-5 $
# use: restore images and videos alike, handing each file to
#      image_restore.py or video_restore.py
"""
   name: mm_restore.py
   use: multimedia restore: one front end for image_restore.py and
        video_restore.py; sorts the files given into images and videos
        by what they contain (falling back on the extension), then
        runs each script once over its own files with the options that
        apply to it
   usage: mm_restore.py [options] file [file ...]
          mm_restore.py -h for the option list
          examples:
             mm_restore.py -oC:\\restored C:\\scans\\*.*
             mm_restore.py -s2 -k0.3 grandma.jpg wedding.avi
             mm_restore.py -n C:\\family\\*   (show what would be done)
   pre: image_restore.py and video_restore.py sit in the same directory
        as this script, with their own prerequisites installed
   post: each image and video is restored exactly as its own script
         would, with the same output names
   imp: needs nothing beyond python itself, so it starts instantly
        and only the scripts actually needed load torch
        -f is passed to both scripts as given, and means what it
        means to each: face restoration for images, flicker
        reduction for videos
        exit status is the worst of the scripts run
        to resume after an interruption, run the same command again
"""

import argparse
import glob
import mimetypes
import os
import subprocess
import sys

E_SUCCESS = 0
E_WARN    = 1
E_FAIL    = 2
E_USAGE   = 3

SELF       = os.path.splitext(os.path.basename(sys.argv[0]))[0]
SELFPREFIX = os.path.dirname(os.path.abspath(sys.argv[0]))
REVISION   = "$Revision: 2.main-2 $"

IMAGE_SCRIPT = os.path.join(SELFPREFIX, "image_restore.py")
VIDEO_SCRIPT = os.path.join(SELFPREFIX, "video_restore.py")

                                # option letter: (applies to, letter
                                # in the target script, takes a value)
                                # "both", "image" or "video"
OPTIONS = {
   "s" : ("both",  "s", True),
   "d" : ("both",  "d", True),
   "k" : ("both",  "k", True),
   "m" : ("both",  "m", True),
   "t" : ("both",  "t", True),
   "o" : ("both",  "o", True),
   "c" : ("both",  "c", False),
   "n" : ("both",  "n", False),
   "x" : ("both",  "x", False),
   "r" : ("image", "r", True),
   "i" : ("image", "i", True),
   "w" : ("image", "w", False),
   "f" : ("both",  "f", True),
   "b" : ("image", "b", True),
   "p" : ("video", "p", True),
   "l" : ("video", "l", True),
   "q" : ("video", "q", True),
   "a" : ("video", "a", False),
}

                                # extensions python's mimetypes table
                                # misses or gets wrong on some systems
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp",
                    ".gif", ".tif", ".tiff", ".webp", ".ppm", ".pgm",
                    ".pbm", ".pnm", ".tga", ".ico", ".dib", ".pcx"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm",
                    ".wmv", ".asf", ".flv", ".mpg", ".mpeg", ".mpe",
                    ".m2v", ".vob", ".ts", ".mts", ".m2ts", ".3gp",
                    ".3g2", ".ogv", ".dv", ".mxf", ".rm", ".rmvb"}

USAGE = f"""{SELF} {REVISION}
usage: {SELF} [options] file [file ...]
where options are:
 for images and videos:
   -sScale    output scale factor, 1 or more (default 1)
   -dStrength denoise strength 0.0 to 1.0
   -kStrength sharpen strength 0.0 to 1.0 (default: 0 images, 0.3
              videos)
   -mModel    auto, photo, general or anime; none skips the ai for
              videos (images keep the default)
   -tTile     tile size in pixels; 0 = choose from available memory
   -oOutput   output directory, or output file when only one file is
              given
   -fStrength images: face restoration 0.0 to 1.0 (default 0.7)
              videos: flicker reduction 0.0 to 1.0 (default 0.5)
   -c         use the cpu even if a gpu is present
   -x         redo files already done; without it, finished files are
              skipped and an interrupted video carries on where it
              stopped, so rerunning the same command resumes the work
 for images only:
   -rStrength scratch and dust detection 0.0 to 1.0 (default 0.5)
   -iMask     your own repair mask (one image only)
   -w         also write the repair mask as <name>_mask.png
   -bStrength deblur strength 0.0 to 1.0 (default 0)
 for videos only:
   -pStart    start this many seconds in
   -lLength   only do this many seconds
   -qQuality  h.264 crf, lower is better and bigger (default 18)
   -a         restore nothing; join the part of each video done so far
              into <name>-preview.mp4, leaving the work to carry on
              later (images are left alone)
 general:
   -v         verbose; more -v for more verbosity
   -n         do not execute; show what would be done
   -h         help
see image_restore.py -h and video_restore.py -h for the details
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
###
### f u n c t i o n s
###
###########################################################################

def sniff(path):
   """
      use: tells images from videos by the first bytes of the file
      post: returns "image", "video" or None when unrecognised
      imp: the signatures of the common formats; content is trusted
           over the extension, which is easily wrong
   """
   try:
      with open(path, "rb") as stream:
         head = stream.read(400)
   except OSError:
      return(None)
   riff = head[8:12] if (head[:4] == b"RIFF") else b""
   if (head[:3] == b"\xff\xd8\xff"                 or
       head[:8] == b"\x89PNG\r\n\x1a\n"            or
       head[:6] in (b"GIF87a", b"GIF89a")          or
       head[:2] == b"BM"                           or
       head[:4] in (b"II*\x00", b"MM\x00*")        or
       riff     == b"WEBP"):
      return("image")
                                # mp4/mov/3gp keep "ftyp" at offset 4,
                                # except heic/avif stills, which are
                                # images in an mp4-style box
   if (head[4:8] == b"ftyp"):
      brand = head[8:12]
      if (brand in (b"heic", b"heix", b"mif1", b"msf1", b"avif")):
         return("image")
      return("video")
   if (head[:4] == b"\x1a\x45\xdf\xa3"             or
       riff     == b"AVI "                         or
       head[:4] in (b"\x00\x00\x01\xba",
                    b"\x00\x00\x01\xb3")           or
       head[:3] == b"FLV"                          or
       head[:4] == b"\x30\x26\xb2\x75"             or
       head[:4] == b"OggS"                         or
       head[:4] == b".RMF"                         or
                                # mpeg transport stream: a 0x47 sync
                                # byte every 188 bytes
       ((len(head) > 376) and
        (head[0] == head[188] == head[376] == 0x47))):
      return("video")
   return(None)

###########################################################################

def kind_of(path):
   """
      post: returns "image", "video" or None for a file
   """
   kind = sniff(path)
   if (kind):
      return(kind)
   extension = os.path.splitext(path)[1].lower()
   if (extension in IMAGE_EXTENSIONS):
      return("image")
   if (extension in VIDEO_EXTENSIONS):
      return("video")
   guess = mimetypes.guess_type(path)[0] or ""
   if (guess.startswith("image/")):
      return("image")
   if (guess.startswith("video/")):
      return("video")
   return(None)

###########################################################################

def parse(argv):
   """
      post: returns (options as given, file list); option values are
            kept as text and passed on unchanged, so each script
            checks its own
   """
   parser = UsageParser(prog = SELF, add_help = False)
   for letter, (_, _, valued) in OPTIONS.items():
      if (valued):
         parser.add_argument(f"-{letter}")
      else:
         parser.add_argument(f"-{letter}", action = "store_true")
   parser.add_argument("-v", action = "count", default = 0)
   parser.add_argument("-h", action = "store_true")
   parser.add_argument("files", nargs = "*")
   opts = parser.parse_args(argv)

   if (opts.h):
      print(USAGE)
      sys.exit(E_SUCCESS)
   if (not opts.files):
      raise(ExceptionClass(f"no files given\n{USAGE}", code = E_USAGE))

                                # cmd.exe does not expand wildcards;
                                # a pattern like *.* may also match
                                # directories, which are skipped
   files = []
   for pattern in opts.files:
      matched = sorted(glob.glob(pattern)) or [pattern]
      files.extend(match for match in matched
                   if (not os.path.isdir(match)))
   return(opts, files)

###########################################################################

def arguments(opts, kind):
   """
      post: returns the command-line options for the script handling
            kind, in that script's letters
   """
   args = []
   for letter, (applies, target, valued) in OPTIONS.items():
      if (applies not in ("both", kind)):
         continue
      value = getattr(opts, letter)
                                # -mnone is video-only: images keep
                                # their default model
      if ((letter == "m") and (value == "none") and (kind == "image")):
         continue
      if (valued and (value is not None)):
         args.append(f"-{target}{value}")
      elif ((not valued) and value):
         args.append(f"-{target}")
   if (opts.v):
      args.append("-" + "v" * opts.v)
   return(args)

###########################################################################

def run(script, args, files, verbose):
   """
      use: runs one of the restore scripts with this python
      post: returns its exit status
   """
   command = [sys.executable, script] + args + files
   if (verbose):
      print(f"{SELF}: {' '.join(command)}", file = sys.stderr)
   return(subprocess.call(command))

###########################################################################
###
### s t a r t    o f    e x e c u t i o n
###
###########################################################################

def main(argv):
   opts, files = parse(argv)

   images, videos, unknown = [], [], []
   for path in files:
      if (not os.path.isfile(path)):
         print(f"{SELF}: {path}: no such file", file = sys.stderr)
         unknown.append(path)
         continue
      kind = kind_of(path)
      if (kind == "image"):
         images.append(path)
      elif (kind == "video"):
         videos.append(path)
      else:
         print(f"{SELF}: {path}: not a recognised image or video; "
               "skipped", file = sys.stderr)
         unknown.append(path)

   if (opts.o and (len(images) + len(videos) > 1) and
       os.path.splitext(opts.o)[1] and not os.path.isdir(opts.o)):
      raise(ExceptionClass(f"-o{opts.o} names a file, but there is more "
                           "than one input; give a directory",
                           code = E_USAGE))
   if (opts.i and videos and not images):
      raise(ExceptionClass("-i is for images only", code = E_USAGE))
   if ((opts.m == "none") and images):
      print(f"{SELF}: -mnone applies to videos only; images use the "
            "default model", file = sys.stderr)
   for script, wanted in ((IMAGE_SCRIPT, images), (VIDEO_SCRIPT, videos)):
      if (wanted and not os.path.isfile(script)):
         raise(ExceptionClass(f"{script} not found; it must be beside "
                              f"{SELF}.py"))

   if (opts.v):
      print(f"{SELF}: {len(images)} image(s), {len(videos)} video(s)"
            + (f", {len(unknown)} skipped" if (unknown) else ""),
            file = sys.stderr)

   worst = E_WARN if (unknown) else E_SUCCESS
   if (not (images or videos)):
      return(E_FAIL)
                                # -a only looks at unfinished videos
   if (opts.a and images):
      print(f"{SELF}: -a is for videos only; {len(images)} image(s) "
            "left alone", file = sys.stderr)
      images = []
   if (images):
      worst = max(worst, run(IMAGE_SCRIPT, arguments(opts, "image"),
                             images, opts.v))
   if (videos):
      worst = max(worst, run(VIDEO_SCRIPT, arguments(opts, "video"),
                             videos, opts.v))
   return(worst)

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
