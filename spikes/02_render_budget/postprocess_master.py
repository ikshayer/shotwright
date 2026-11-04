"""Spike 02 final step: run spike 01's PostProfile over a REAL Blender master.

Until now the degradation chain has only ever seen ffmpeg's testsrc2. This
builds an FFV1 master from the rendered PNG sequence, measures its byte cost
per second of footage (the number FR-26's disk ceiling needs), and runs the
pinned PostProfile over it - producing something viewable and a golden hash
over real geometry.
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SPIKE01 = os.path.abspath(os.path.join(HERE, "..", "01_postprofile_determinism"))
sys.path.insert(0, SPIKE01)

import run_spike as ps  # noqa: E402

FRAMES = os.path.join(HERE, "_work", "master_run", "master")
OUT = os.path.join(HERE, "_work", "post")
MASTER_FPS = 60
os.makedirs(OUT, exist_ok=True)


def sh(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[-3000:])
        raise SystemExit(1)
    return p.stdout


n_frames = len([f for f in os.listdir(FRAMES) if f.endswith(".png")])
duration = n_frames / float(MASTER_FPS)
master = os.path.join(OUT, "master.mkv")

sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
    "-framerate", str(MASTER_FPS), "-i", os.path.join(FRAMES, "f_%04d.png"),
    "-c:v", "ffv1", "-level", "3", "-pix_fmt", "yuv420p", "-flags", "+bitexact", master])

size = os.path.getsize(master)
png_total = sum(os.path.getsize(os.path.join(FRAMES, f))
                for f in os.listdir(FRAMES) if f.endswith(".png"))
print("master frames        : %d  (%.2f s at %d fps)" % (n_frames, duration, MASTER_FPS))
print("PNG sequence         : %.1f MB  (%.1f MB per second of footage)"
      % (png_total / 1e6, png_total / 1e6 / duration))
print("FFV1 master          : %.1f MB  (%.1f MB per second of footage)"
      % (size / 1e6, size / 1e6 / duration))

per_s = size / duration
for shot_s, shots, versions in ((15, 12, 3),):
    total = per_s * shot_s * shots * versions
    print("projection           : %d shots x %d s x %d versions = %.1f GB of masters"
          % (shots, shot_s, versions, total / 1e9))

# --- scale the schedule to this clip and run the pinned profile ---
profile = json.load(open(os.path.join(SPIKE01, "post_profile.json")))
profile["profile_id"] = "dvr-2005-on-render"
out_fps = profile["output_fps"]
n_decimated = int(duration * out_fps)
profile["artifact_schedule"] = [
    {"type": "drop", "start_frame": int(n_decimated * 0.35), "count": 3},
    {"type": "freeze", "start_frame": int(n_decimated * 0.62), "count": 5},
]
print("decimated frames     : %d at %d fps" % (n_decimated, out_fps))

ps.MASTER = master
ps.MASTER_SECONDS = duration
ps.WORK = OUT
font = os.path.join(OUT, "overlay.ttf")
if not os.path.exists(font):
    import shutil
    shutil.copyfile(ps.FONT_SRC, font)

n_pred, clock_steps, freeze_runs = ps.simulate(profile)
graph = ps.build_graph(profile, "overlay.ttf")
print("predicted frames     : %d   clock steps %s   freeze runs %s"
      % (n_pred, clock_steps, freeze_runs))

hashes_a = ps.frame_hashes(graph)
hashes_b = ps.frame_hashes(graph)
final = ps.encode(graph, profile, "dvr2005_from_render.mp4")
st, nframes, dist = ps.probe(final)

checks = {
    "frames reproducible over real footage": hashes_a == hashes_b,
    "frame count matches schedule": nframes == n_pred,
    "display AR is 4:3": st.get("display_aspect_ratio") == "4:3",
}
print()
print("output               : %s (%.2f MB)" % (os.path.basename(final),
                                               os.path.getsize(final) / 1e6))
print("ffprobe              : %sx%s dar=%s r_frame_rate=%s"
      % (st.get("width"), st.get("height"), st.get("display_aspect_ratio"),
         st.get("r_frame_rate")))
print("compression ratio    : %.0f:1 versus the FFV1 master" % (size / os.path.getsize(final)))
for k, v in checks.items():
    print("  [%s] %s" % ("PASS" if v else "FAIL", k))

json.dump({
    "master_frames": n_frames,
    "master_duration_s": duration,
    "png_bytes_per_second": int(png_total / duration),
    "ffv1_bytes_per_second": int(per_s),
    "delivered_bytes": os.path.getsize(final),
    "frames_hash": __import__("hashlib").sha256("".join(hashes_a).encode()).hexdigest(),
    "filter_graph": graph,
    "checks": checks,
}, open(os.path.join(HERE, "post_results.json"), "w"), indent=2, sort_keys=True)
print("\nwrote post_results.json")
sys.exit(0 if all(checks.values()) else 2)
