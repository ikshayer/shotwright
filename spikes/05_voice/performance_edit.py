"""Spike 05f - PerformanceEdit: structure by editing, not by fragmenting.

Fragmenting generation was wrong. Autoregressive TTS degrades on very short
inputs, so generating "Is" or "Is somebody there?" as standalone utterances
produced worse speech than one long take - which the creator heard immediately.

This inverts it. Generate ONE committed take, then apply structure as an edit:
widen the pauses the take already has, drop procedural breaths into them,
shape levels, and build the false start by reusing the take's own first word.
No speech sample is regenerated, so the voice quality is exactly the quality
that was approved.

The edit list is the artifact - seeded, versioned and reproducible, the same
discipline as PostProfile and AudioPostProfile.
"""

import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "_work")

EDIT = {
    "schema": "PerformanceEdit/0.3",
    "take": "ix2_long_afraid-70.wav",
    "seed": 20051114,
    "false_start": {"copy_ms": 300, "gain_db": -3.0, "gap_after_ms": 200},
    # index into the take's detected pauses, excluding the leading one
    "pauses": [
        {"after_phrase": "Is somebody there?", "target_ms": 1100,
         "breath": {"duration_ms": 420, "gain_db": -15.0}},
        {"after_phrase": "walking around upstairs.", "target_ms": 760,
         "breath": {"duration_ms": 330, "gain_db": -18.0}},
        {"after_phrase": "Please,", "target_ms": 470, "breath": None},
    ],
    "segment_gain_db": [0.0, -1.0, -3.0, -4.5],
    "jitter": {"gap_ms_range": [-80, 110], "gain_db_range": [-0.6, 0.6]},
}


def sh(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        sys.stderr.write(" ".join(args)[:300] + "\n" + p.stderr[-1500:])
        raise SystemExit(1)
    return p.stdout


def jitter(material, lo, hi):
    h = int(hashlib.sha256(material.encode()).hexdigest()[:8], 16)
    return lo + (h / 0xFFFFFFFF) * (hi - lo)


def detect_pauses(path, noise_db=-38, min_d=0.12):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", path,
                          "-af", "silencedetect=noise=%ddB:d=%s" % (noise_db, min_d),
                          "-f", "null", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", out)]
    pauses = [(s, e) for s, e in zip(starts, ends) if s > 0.05]
    return pauses


def main():
    take = os.path.join(WORK, EDIT["take"])
    if not os.path.exists(take):
        raise SystemExit("take not found: %s" % take)
    sr = int(sh(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate",
                 "-of", "csv=p=0", take]).strip().split(",")[0])
    dur = float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "csv=p=0", take]).strip())

    pauses = detect_pauses(take)
    print("take: %s  %.2f s @ %d Hz" % (EDIT["take"], dur, sr))
    print("detected %d internal pauses: %s" % (len(pauses),
          ", ".join("%.2f-%.2f" % p for p in pauses)))
    if len(pauses) < len(EDIT["pauses"]):
        print("WARNING: fewer pauses than the edit expects; using what exists")

    # cut points at the MIDDLE of each pause, so no speech is clipped
    cuts = [((s + e) / 2.0) for s, e in pauses[:len(EDIT["pauses"])]]
    bounds = [0.0] + cuts + [dur]
    parts = []

    # false start: reuse the take's own opening word, so quality is identical
    fs = EDIT["false_start"]
    if fs:
        p = os.path.join(WORK, "_pe_falsestart.wav")
        sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
            "-i", take, "-af",
            "atrim=start=%.3f:end=%.3f,asetpts=N/SR/TB,afade=t=out:st=%.3f:d=0.05,volume=%.2fdB"
            % (pauses[0][1] if pauses else 0.0,
               (pauses[0][1] if pauses else 0.0) + fs["copy_ms"] / 1000.0,
               fs["copy_ms"] / 1000.0 - 0.05, fs["gain_db"]),
            "-ac", "1", "-ar", str(sr), "-flags", "+bitexact", p])
        parts.append(p)
        gap = fs["gap_after_ms"] + jitter("fs|gap", *EDIT["jitter"]["gap_ms_range"])
        g = os.path.join(WORK, "_pe_gap_fs.wav")
        sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
            "-f", "lavfi", "-i", "anullsrc=r=%d:cl=mono:d=%.3f" % (sr, max(gap, 1) / 1000.0),
            "-ac", "1", "-ar", str(sr), "-flags", "+bitexact", g])
        parts.append(g)
        print("false start: %d ms copied from the take, %+.1f dB, %.0f ms gap"
              % (fs["copy_ms"], fs["gain_db"], gap))

    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        gain = (EDIT["segment_gain_db"][min(i, len(EDIT["segment_gain_db"]) - 1)]
                + jitter("seg%d|gain" % i, *EDIT["jitter"]["gain_db_range"]))
        seg = os.path.join(WORK, "_pe_seg%02d.wav" % i)
        sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
            "-i", take, "-af",
            "atrim=start=%.3f:end=%.3f,asetpts=N/SR/TB,volume=%.2fdB" % (a, b, gain),
            "-ac", "1", "-ar", str(sr), "-flags", "+bitexact", seg])
        parts.append(seg)
        print("  segment %d  %.2f-%.2f s  %+.1f dB" % (i, a, b, gain))

        if i < len(EDIT["pauses"]):
            spec = EDIT["pauses"][i]
            natural_ms = (pauses[i][1] - pauses[i][0]) * 1000.0
            extra = spec["target_ms"] - natural_ms + jitter(
                "pause%d|gap" % i, *EDIT["jitter"]["gap_ms_range"])
            if spec.get("breath"):
                br = spec["breath"]
                bp = os.path.join(WORK, "_pe_breath%02d.wav" % i)
                d = br["duration_ms"] / 1000.0
                sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-fflags", "+bitexact", "-f", "lavfi", "-i",
                    "anoisesrc=color=brown:amplitude=0.9:seed=%d:duration=%.3f:sample_rate=%d"
                    % (int(hashlib.sha256(("breath%d" % i).encode()).hexdigest()[:8], 16)
                       & 0x7FFFFFFF, d, sr),
                    "-af", ("highpass=f=380:poles=2,lowpass=f=2100:poles=2,"
                            "afade=t=in:st=0:d=%.3f:curve=exp,"
                            "afade=t=out:st=%.3f:d=%.3f:curve=qsin,volume=%.2fdB"
                            % (d * 0.62, d * 0.62, d * 0.38, br["gain_db"])),
                    "-ac", "1", "-ar", str(sr), "-flags", "+bitexact", bp])
                parts.append(bp)
                extra -= br["duration_ms"]
            if extra > 0:
                g = os.path.join(WORK, "_pe_gap%02d.wav" % i)
                sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-fflags", "+bitexact", "-f", "lavfi", "-i",
                    "anullsrc=r=%d:cl=mono:d=%.3f" % (sr, extra / 1000.0),
                    "-ac", "1", "-ar", str(sr), "-flags", "+bitexact", g])
                parts.append(g)
            print("    pause after %-26s %3.0f -> %.0f ms%s"
                  % ('"' + spec["after_phrase"][:24] + '"', natural_ms,
                     spec["target_ms"], " + breath" if spec.get("breath") else ""))

    lst = os.path.join(WORK, "_pe_concat.txt")
    with open(lst, "w") as fh:
        for p in parts:
            fh.write("file '%s'\n" % p.replace("\\", "/"))
    out = os.path.join(WORK, "ix2_performance_edit.wav")
    sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
        "-f", "concat", "-safe", "0", "-i", lst,
        "-af", "loudnorm=I=-18:TP=-1:LRA=11", "-ac", "1", "-ar", str(sr),
        "-flags", "+bitexact", out])
    newdur = float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                       "-of", "csv=p=0", out]).strip())
    json.dump(EDIT, open(os.path.join(HERE, "performance_edit.json"), "w"),
              indent=2, sort_keys=True)
    print("\n%.2f s -> %.2f s; wrote ix2_performance_edit.wav" % (dur, newdur))
    for f in os.listdir(WORK):
        if f.startswith("_pe_"):
            os.remove(os.path.join(WORK, f))
    return 0


if __name__ == "__main__":
    sys.exit(main())
