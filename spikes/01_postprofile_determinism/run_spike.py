"""Phase 0 spike 01 - PostProfile determinism and overlay clock model.

Validates the premise behind FR-21 and AT-05: that re-applying a versioned
PostProfile to the same master, with the same seed and the same pinned
binaries, reproduces a byte-identical result.

Tests three layers separately, because they fail independently:

  filter_graph_hash  the canonical graph string (pure function of the profile)
  frames_hash        framemd5 over the FILTERED frames, before any encoder
  encode_hash        sha256 of the final encoded file

Negative control: changing only degrade_seed must change frames_hash. Without
it, a filter that silently ignores its seed would pass for the wrong reason.

v0.2 also validates the overlay clock model (FR-21):

  - the burned-in clock JUMPS across a dropped-frame window
  - the clock KEEPS TICKING across a freeze, over a static image
  - the overlay precedes the degradation stages, so the text is degraded too
  - schedule windows outside the decimated range are REJECTED at load

Freeze semantics: a DVR freeze repeats an image while time advances, so the
source frames in that window are REPLACED, not added. A freeze is therefore
length-neutral, and the clock stays continuous through it.

No GPU, no model downloads. Needs ffmpeg + ffprobe on PATH.
"""

import hashlib
import json
import re
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "_work")
MASTER = os.path.join(WORK, "master.mkv")
FONT_SRC = r"C:\Windows\Fonts\consola.ttf"

MASTER_SECONDS = 5
MASTER_FPS = 60
MASTER_SIZE = "1280x960"


class ProfileRejected(Exception):
    pass


def sh(args, cwd=None):
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        sys.stderr.write("\nCOMMAND FAILED: %s\n%s\n" % (" ".join(args), proc.stderr[-4000:]))
        raise SystemExit(1)
    return proc.stdout


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# schedule simulation - the single source of truth for both the filter graph
# and the predictions the output is asserted against
# --------------------------------------------------------------------------

def windows(profile, kind):
    return sorted((e for e in profile["artifact_schedule"] if e["type"] == kind),
                  key=lambda e: e["start_frame"])


def simulate(profile):
    """Walk the decimated timeline and return the output plan.

    Returns (n_out, clock_steps, freeze_runs) where
      clock_steps  [(output_frame, cumulative_dropped)] step function
      freeze_runs  [(output_start, length)] runs of identical images
    """
    fps = profile["output_fps"]
    n_src = int(round(MASTER_SECONDS * fps))  # MASTER_SECONDS may be fractional
    drops = windows(profile, "drop")
    freezes = windows(profile, "freeze")

    for w in drops + freezes:
        end = w["start_frame"] + w["count"] - 1
        if w["start_frame"] < 0 or end >= n_src:
            raise ProfileRejected(
                "%s window frames %d-%d falls outside the decimated range 0-%d "
                "(%d fps x %d s). Profile rejected at load."
                % (w["type"], w["start_frame"], end, n_src - 1, fps, MASTER_SECONDS))
    if freezes and any(f["start_frame"] == 0 for f in freezes):
        raise ProfileRejected("a freeze cannot start at frame 0: no previous frame to hold")

    removed = set()
    for w in drops + freezes:
        for i in range(w["start_frame"], w["start_frame"] + w["count"]):
            removed.add(i)

    # survivors in source order, then freeze duplicates re-inserted
    survivors = [i for i in range(n_src) if i not in removed]
    out_to_src = list(survivors)
    freeze_runs = []
    for f in freezes:
        held_src = f["start_frame"] - 1
        pos = out_to_src.index(held_src)
        for _ in range(f["count"]):
            out_to_src.insert(pos + 1, held_src)
        freeze_runs.append((pos, f["count"] + 1))

    # clock advances one frame interval per OUTPUT frame, and jumps by the
    # dropped count at each drop boundary
    clock_steps = []
    cum = 0
    for w in drops:
        first_after = w["start_frame"] + w["count"]
        # the next frame that actually survived, in case windows are adjacent
        boundary = next((i for i, src in enumerate(out_to_src) if src >= first_after), None)
        if boundary is None:
            raise ProfileRejected(
                "drop window ending at frame %d leaves no surviving frames after it"
                % (first_after - 1))
        cum += w["count"]
        clock_steps.append((boundary, cum))
    return len(out_to_src), clock_steps, freeze_runs


def stage_seed(profile, stage_name, max_value=0x7FFFFFFF):
    """Per-stage seed derived from (shot_id, degrade_seed) - FR-21.

    Masked to the filter's accepted range: FFmpeg's noise filter caps
    all_seed at INT32_MAX and errors out above it, so derivation must be
    range-aware per filter rather than "any 32-bit hash".
    """
    material = "%s|%s|%s" % (profile["shot_id"], profile["degrade_seed"], stage_name)
    return int(hashlib.sha256(material.encode()).hexdigest()[:8], 16) & max_value


def clock_offset_expr(clock_steps):
    """Piecewise source-clock offset, in frames, as a setpts expression."""
    expr = "0"
    for boundary, cum in clock_steps:
        expr = "if(gte(N,%d),%d,%s)" % (boundary, cum, expr)
    return expr


def build_graph(profile, font_ref, upto=None):
    _, clock_steps, _ = simulate(profile)
    parts = []
    for stage in profile["stages"]:
        f = stage["filter"]
        p = stage.get("params", {})

        if f == "scale":
            parts.append("scale=%d:%d:flags=%s" % (p["w"], p["h"], p["flags"]))

        elif f == "fps":
            parts.append("fps=%d" % p["fps"])

        elif f == "select":
            terms = []
            for kind in ("drop", "freeze"):
                for w in windows(profile, kind):
                    terms.append("between(n,%d,%d)"
                                 % (w["start_frame"], w["start_frame"] + w["count"] - 1))
            parts.append("select='not(%s)'" % "+".join(terms))

        elif f == "loop":
            for w in windows(profile, "freeze"):
                held = w["start_frame"] - 1
                dropped_before = sum(d["count"] for d in windows(profile, "drop")
                                     if d["start_frame"] < held)
                # FFmpeg's loop filter starts looping AFTER `start`, so the
                # frame repeated is at position start-1. Verified empirically:
                # start=41 repeats the frame at 0-based position 40.
                parts.append("loop=loop=%d:size=1:start=%d"
                             % (w["count"], held - dropped_before + 1))

        elif f == "setpts":
            # Set the timebase EXPLICITLY and index frames by N. The expression
            # variables FRAME_RATE and TB resolve from upstream filter metadata,
            # which fps/select/loop leave in a state that produced nonsense PTS
            # (0,1,2,2,4,5,5,6 at 10 fps). Measured in spike 01.
            fps = profile["output_fps"]
            if stage.get("from_schedule") == "clock":
                parts.append("settb=1/%d,setpts='N+%s'"
                             % (fps, clock_offset_expr(clock_steps)))
            else:
                parts.append("settb=1/%d,setpts=N" % fps)

        elif f == "drawtext":
            # Applied AFTER drop/freeze restructuring so every output frame can
            # carry its own clock value, computed from the schedule-aware source
            # clock rather than output PTS, and BEFORE the degradation stages so
            # the burned-in text is compressed and noised with the image.
            parts.append(
                "drawtext=fontfile=%s:text='%s  %%{pts\\:gmtime\\:%d}'"
                ":x=%s:y=%s:fontsize=%d:fontcolor=white:box=1:boxcolor=black@0.4"
                % (font_ref, p["camera_id"], p["clock_epoch"], p["x"], p["y"], p["fontsize"]))

        elif f == "eq":
            parts.append("eq=brightness=%s:contrast=%s:saturation=%s"
                         % (p["brightness"], p["contrast"], p["saturation"]))

        elif f == "noise":
            parts.append("noise=alls=%d:allf=%s:all_seed=%d"
                         % (p["alls"], p["allf"], stage_seed(profile, stage["name"])))

        elif f == "unsharp":
            parts.append("unsharp=%d:%d:%s"
                         % (p["luma_msize_x"], p["luma_msize_y"], p["luma_amount"]))

        else:
            raise SystemExit("unknown filter in profile: %s" % f)

        if upto is not None and stage["name"] == upto:
            break

    sar = profile.get("sample_aspect_ratio", "1/1")
    if upto is None and sar not in ("1/1", "1:1"):
        parts.append("setsar=%s" % sar)
    return ",".join(parts)


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

def make_master():
    if os.path.exists(MASTER):
        return
    sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
        "-f", "lavfi", "-i",
        "testsrc2=size=%s:rate=%d:duration=%d" % (MASTER_SIZE, MASTER_FPS, MASTER_SECONDS),
        "-c:v", "ffv1", "-level", "3", "-pix_fmt", "yuv420p",
        "-flags", "+bitexact", MASTER])


def frame_hashes(graph):
    """Per-frame md5 of the filtered frames, encoder excluded."""
    out = sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
              "-fflags", "+bitexact", "-i", MASTER,
              "-vf", graph, "-f", "framemd5", "-"], cwd=WORK)
    rows = [l for l in out.splitlines() if l and not l.startswith("#")]
    return [r.split(",")[-1].strip() for r in rows]


def clock_pts(graph):
    """PTS as seen by the overlay stage, read straight from the filter chain.

    Deliberately not routed through a container: Matroska quantises
    timestamps to milliseconds, which destroys the 1/12 s distinction this
    assertion depends on.
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-fflags", "+bitexact", "-i", MASTER,
         "-vf", graph + ",showinfo", "-f", "null", "-"],
        cwd=WORK, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-3000:])
        raise SystemExit(1)
    return [float(m) for m in re.findall(r"pts_time:([0-9]+\.?[0-9]*)", proc.stderr)]


def encode(graph, profile, out_name, threads=None):
    enc = profile["encode"]
    path = os.path.join(WORK, out_name)
    sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-fflags", "+bitexact", "-i", MASTER, "-vf", graph,
        "-c:v", enc["codec"], "-preset", enc["preset"], "-crf", str(enc["crf"]),
        "-pix_fmt", enc["pix_fmt"],
        "-threads", str(enc["threads"] if threads is None else threads),
        "-flags", "+bitexact", "-fflags", "+bitexact", path], cwd=WORK)
    return path


def probe(path):
    out = sh(["ffprobe", "-hide_banner", "-loglevel", "error", "-select_streams", "v:0",
              "-show_streams", "-of", "json", path])
    st = json.loads(out)["streams"][0]
    raw = sh(["ffprobe", "-hide_banner", "-loglevel", "error", "-select_streams", "v:0",
              "-show_entries", "frame=pts_time", "-of", "csv=p=0", path]).split()
    times = [float(x.rstrip(",")) for x in raw if x.rstrip(",") not in ("", "N/A")]
    dist = {}
    for i in range(len(times) - 1):
        d = round(times[i + 1] - times[i], 6)
        dist[d] = dist.get(d, 0) + 1
    return st, len(times), dist


def main():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("ffmpeg/ffprobe not found on PATH")
    os.makedirs(WORK, exist_ok=True)
    if not os.path.exists(FONT_SRC):
        raise SystemExit("font not found: %s" % FONT_SRC)
    font_local = os.path.join(WORK, "overlay.ttf")
    if not os.path.exists(font_local):
        shutil.copyfile(FONT_SRC, font_local)

    banner = sh(["ffmpeg", "-hide_banner", "-version"]).splitlines()[0]
    profile_file = sys.argv[1] if len(sys.argv) > 1 else "post_profile.json"
    profile = json.load(open(os.path.join(HERE, profile_file)))

    print("ffmpeg          : %s" % banner)
    print("profile         : %s  %s v%s" % (profile_file, profile["profile_id"],
                                            profile["profile_version"]))
    try:
        n_pred, clock_steps, freeze_runs = simulate(profile)
    except ProfileRejected as exc:
        print("\nPROFILE REJECTED AT LOAD: %s" % exc)
        print("  [PASS] invalid schedule rejected before rendering")
        return 0

    graph = build_graph(profile, "overlay.ttf")
    graph_hash = sha256_bytes(graph.encode())
    print("filter_graph    : %s" % graph)
    print("graph_hash      : %s" % graph_hash)
    print("predicted frames: %d   clock steps: %s   freeze runs: %s"
          % (n_pred, clock_steps, freeze_runs))
    print()

    make_master()
    print("master          : sha256 %s" % sha256_file(MASTER)[:16])

    fh1 = frame_hashes(graph)
    fh2 = frame_hashes(graph)
    frames_hash = sha256_bytes("".join(fh1).encode())
    print("frames_hash #1  : %s  (%d frames)" % (frames_hash, len(fh1)))
    print("frames_hash #2  : %s  (%d frames)"
          % (sha256_bytes("".join(fh2).encode()), len(fh2)))

    alt = json.loads(json.dumps(profile))
    alt["degrade_seed"] = profile["degrade_seed"] + 1
    fh_alt = frame_hashes(build_graph(alt, "overlay.ttf"))
    print("frames_hash alt : %s  (seed+1)" % sha256_bytes("".join(fh_alt).encode()))

    e1 = sha256_file(encode(graph, profile, "out_a.mp4"))
    e2 = sha256_file(encode(graph, profile, "out_b.mp4"))
    t1 = sha256_file(encode(graph, profile, "out_mt_a.mp4", threads=0))
    t2 = sha256_file(encode(graph, profile, "out_mt_b.mp4", threads=0))
    print("encode_hash     : %s (threads=1, reproducible=%s)" % (e1, e1 == e2))
    print("encode_hash mt  : %s (threads=auto, reproducible=%s)" % (t1, t1 == t2))

    # --- overlay clock model ---
    fps = profile["output_fps"]
    tick = round(1.0 / fps, 6)
    cpts = clock_pts(build_graph(profile, "overlay.ttf", upto="clock_timebase"))
    cdelta = [round(cpts[i + 1] - cpts[i], 6) for i in range(len(cpts) - 1)]
    jumps = {}
    for i, d in enumerate(cdelta):
        # Tolerance of half a frame interval, not equality: rational timebase
        # rounding alternates the nominal tick between 0.083333 and 0.083334.
        # This is the same trap 12.2 warns about, and the first version of this
        # assertion fell straight into it.
        if abs(d - tick) > tick * 0.5:
            jumps[i + 1] = d
    expected_jumps = {}
    prev = 0
    for boundary, cum in clock_steps:
        expected_jumps[boundary] = round((1 + cum - prev) / float(fps), 6)
        prev = cum
    print("clock jumps     : %s   expected %s" % (jumps, expected_jumps))

    # image staticness across the freeze, measured WITHOUT the overlay
    pre = frame_hashes(build_graph(profile, "overlay.ttf", upto="hold_frozen"))
    static_ok, ticking_ok = True, True
    for start, length in freeze_runs:
        run_pre = pre[start:start + length]
        run_post = fh1[start:start + length]
        if len(set(run_pre)) != 1:
            static_ok = False
        if len(set(run_post)) != length:
            ticking_ok = False
        print("freeze run      : frames %d-%d  image identical pre-overlay=%s  "
              "all differ post-overlay=%s"
              % (start, start + length - 1, len(set(run_pre)) == 1,
                 len(set(run_post)) == length))

    st, nframes, dist = probe(os.path.join(WORK, "out_a.mp4"))
    print()
    print("ffprobe         : %sx%s sar=%s dar=%s r_frame_rate=%s"
          % (st.get("width"), st.get("height"), st.get("sample_aspect_ratio", "1:1"),
             st.get("display_aspect_ratio", "n/a"), st.get("r_frame_rate")))
    print("output pts dist : %s" % dist)

    checks = {
        "frames reproducible": fh1 == fh2,
        "seed actually changes output": fh_alt != fh1,
        "encode reproducible (threads=1)": e1 == e2,
        "encode reproducible (threads=auto)": t1 == t2,
        "frame count matches artifact_schedule": nframes == n_pred,
        "display AR is 4:3": st.get("display_aspect_ratio") == "4:3",
        "clock jumps exactly at drop boundaries": jumps == expected_jumps,
        "image is static across freeze": static_ok,
        "clock keeps ticking across freeze": ticking_ok,
        "output file is CFR": len(dist) <= 2,
    }
    print()
    for name, ok in checks.items():
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))

    results_name = "results_%s.json" % profile["profile_id"]
    with open(os.path.join(HERE, results_name), "w") as fh:
        json.dump({
            "ffmpeg": banner,
            "profile_id": profile["profile_id"],
            "profile_version": profile["profile_version"],
            "filter_graph": graph,
            "filter_graph_hash": graph_hash,
            "frames_hash": frames_hash,
            "encode_hash_threads1": e1,
            "encode_hash_threadsauto": t1,
            "frame_count": nframes,
            "predicted_frame_count": n_pred,
            "clock_jumps": {str(k): v for k, v in jumps.items()},
            "freeze_runs": freeze_runs,
            "output_pts_delta_distribution": {str(k): v for k, v in dist.items()},
            "checks": checks,
        }, fh, indent=2, sort_keys=True)
    print("\nwrote %s" % results_name)
    return 0 if all(checks.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
