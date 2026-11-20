mm_restore - multimedia restore: clean up old photos and videos locally
=======================================================================

Restores photos and videos with AI models that run entirely on your
machine. Photos have scratches and dust painted out, grain and JPEG
noise removed, focus blur reduced, faces rebuilt and can be enlarged.
Videos have grain removed by lining up neighbouring frames, are cleaned
and can be enlarged, are steadied so the AI detail does not flicker,
and keep their soundtrack. Nothing is uploaded anywhere; the only
network traffic is a one-time download of the model weights.

Three scripts, kept together in one folder:

   mm_restore.py      takes photos and videos together, tells them
                      apart by their contents and hands each to the
                      right script below
   image_restore.py   photos
   video_restore.py   videos

Needs Python 3.9 or later, plus:

   pip install torch spandrel pillow numpy opencv-python imageio-ffmpeg

and, only for deblurring photos (-b):

   pip install spandrel_extra_arches

For an NVIDIA GPU, install torch from the pytorch.org index for your
CUDA version FIRST (https://pytorch.org/get-started); otherwise pip
installs a build that may not use the GPU. imageio-ffmpeg brings its
own ffmpeg, so nothing else needs installing.

Model weights (about 700 MB in all) download on first use into
%LOCALAPPDATA%\restore on Windows, ~/.cache/restore elsewhere, and are
shared by both scripts.

Run
---
   python mm_restore.py grandma.jpg wedding.avi
   python mm_restore.py -s2 -oC:\restored C:\family\*.*
   python mm_restore.py -n C:\family\*        show what would be done

   python image_restore.py -s2 -r0.6 -f0.5 -d0.6 -k0.3 -w old_photo.jpg
   python image_restore.py -b0.8 -d0.4 -f0.4 blurry.jpg

   python video_restore.py -p60 -l5 home_movie.mp4    try 5s from 1:00
   python video_restore.py -s1 -mnone hd_clip.mp4     fast, no AI
   python video_restore.py -a home_movie.mp4          preview so far

   python <script> -h                                 all options

Output goes beside each input as <name>_restored.png (photos) or
<name>_restored.mp4 (videos, H.264 with AAC audio), or into -o.

Options common to both
----------------------
   -sScale      output scale factor, 1 or more (default 1: restore
                without enlarging)
   -dStrength   denoise / grain removal 0.0 to 1.0
   -fStrength   photos: face restoration (default 0.7)
                videos: flicker reduction (default 0.5)
   -kStrength   sharpen 0.0 to 1.0 (default: 0 photos, 0.3 videos)
   -mModel      auto, photo, general or anime; videos also take none,
                which skips the AI and is many times faster
   -tTile       tile size in pixels; 0 = choose from available memory
   -oOutput     output directory, or output file for a single input
   -c           use the CPU even if a GPU is present
   -x           redo files already done (see Resuming, below)
   -v           verbose; more -v for more verbosity
   -n           do not execute; show what would be done
   -h           help

Photos only
-----------
   -rStrength   scratch and dust detection (default 0.5; 0 is off)
   -iMask       your own repair mask, white where to repair
   -w           also write the repair mask, <name>_mask.png
   -bStrength   deblur for out-of-focus photos (default 0, off)

Videos only
-----------
   -pStart      start this many seconds in
   -lLength     only do this many seconds
   -qQuality    H.264 CRF, lower is better and bigger (default 18)
   -a           assemble: restore nothing, but join the part done so
                far into <name>-preview.mp4; safe to run while a
                restoration is still going

How photos are restored
-----------------------
   1. scratches, cracks and dust are found and painted over (LaMa)
   2. optionally, focus blur is removed (Restormer, -b)
   3. grain and JPEG noise are removed and the photo enlarged
      (Real-ESRGAN)
   4. faces are found and rebuilt in detail (GFPGAN)
   5. optionally, the result is sharpened (-k)

Check the repair mask with -w: thin straight features in the picture
(poles, straps, a cup's rim) can be taken for scratches. Lower -r, or
paint your own mask and pass it with -i. GFPGAN rebuilds faces from
what it has learned, so a badly damaged face can come back looking
slightly unlike the person; lower -f to keep more of the original.

How videos are restored
-----------------------
   1. grain is removed by averaging each frame with its neighbours,
      lined up by optical flow: the picture lines up and is kept, the
      grain differs from frame to frame and averages away
   2. remaining noise is removed and the frame enlarged (Real-ESRGAN)
   3. the frame is steadied against the one before, so the detail the
      AI adds does not flicker
   4. the result is sharpened, and the soundtrack copied across

Video is slow without a GPU: about 2 seconds a frame for a small
480x320 clip on a 2-core CPU, so try a few seconds with -p and -l
before a whole film. Interlaced sources (camcorder tapes, TV captures)
should be deinterlaced first.

Resuming
--------
Run the same command again after an interruption (Ctrl-C, a crash, a
reboot) and the work carries on. Photos are written under a temporary
name and renamed when complete, so finished photos are skipped. Videos
are saved in chunks, about one a minute, in a <name>_restored.mp4.parts
folder beside the output; a rerun carries on after the last chunk and
the folder is removed once the video is finished. Work saved with other
settings, or from a changed source file, is thrown away and started
again. Use -x to redo files that are already done.

Notes
-----
Restormer (the -b deblur model) is licensed for non-commercial use
only. The other models are free to use, but check each project's
licence before any commercial use.
