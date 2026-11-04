"""Spike 05e - the PerformanceScript driving IndexTTS-2.

Structure and voice were solved separately and then tested separately, which
is why the single-pass IndexTTS-2 takes lost the pauses. The script owns
structure (false start, gaps, breaths, level contour); the engine owns voice
and emotion. Swapping the engine should change nothing about the script, and
that claim is what this run tests.

PerformanceScript 0.2 adds a per-fragment emotion vector, so the line carries
an arc - wary, forcing composure, breaking, defeated - rather than one
constant setting. Chatterbox could not express that; IndexTTS-2's disentangled
emotion input can.

Run in the IndexTTS repo environment:
  cd D:\\Work\\index-tts
  uv run python D:\\Work\\video_pipeline\\spikes\\05_voice\\performance_chain_ix2.py
"""

import hashlib
import json
import os
import subprocess
import sys
import time

REPO = r"D:\Work\index-tts"
SPIKE = r"D:\Work\video_pipeline\spikes\05_voice"
WORK = os.path.join(SPIKE, "_work")


def sh(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        sys.stderr.write(" ".join(args)[:300] + "\n" + p.stderr[-1500:])
        raise SystemExit(1)


def seeded_uniform(material, lo, hi):
    h = int(hashlib.sha256(material.encode()).hexdigest()[:8], 16)
    return lo + (h / 0xFFFFFFFF) * (hi - lo)


def make_breath(path, duration_ms, gain_db, sr, seed):
    """Procedural inhale - synthesised, so it carries no third-party rights."""
    dur = duration_ms / 1000.0
    sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
        "-f", "lavfi", "-i",
        "anoisesrc=color=brown:amplitude=0.9:seed=%d:duration=%.3f:sample_rate=%d"
        % (seed, dur, sr),
        "-af", ("highpass=f=380:poles=2,lowpass=f=2100:poles=2,"
                "afade=t=in:st=0:d=%.3f:curve=exp,afade=t=out:st=%.3f:d=%.3f:curve=qsin,"
                "volume=%.2fdB" % (dur * 0.62, dur * 0.62, dur * 0.38, gain_db)),
        "-ac", "1", "-ar", str(sr), "-flags", "+bitexact", path])


def make_silence(path, duration_ms, sr):
    sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
        "-f", "lavfi", "-i", "anullsrc=r=%d:cl=mono:d=%.3f" % (sr, max(duration_ms, 1) / 1000.0),
        "-ac", "1", "-ar", str(sr), "-flags", "+bitexact", path])


def main():
    sys.path.insert(0, REPO)
    os.chdir(REPO)
    from indextts.infer_v2 import IndexTTS2

    script = json.load(open(os.path.join(SPIKE, "performance_script_ix2.json")))
    jit = script["jitter"]
    spk = os.path.join(WORK, script["speaker_reference"])

    t0 = time.time()
    tts = IndexTTS2(cfg_path=os.path.join(REPO, "checkpoints", "config.yaml"),
                    model_dir=os.path.join(REPO, "checkpoints"), use_fp16=True)
    print("model loaded in %.0f s" % (time.time() - t0))

    parts, sr, total = [], None, 0.0
    for i, frag in enumerate(script["fragments"]):
        tag = "%s#%d" % (script["line_id"], i)
        raw = os.path.join(WORK, "_ix2frag%02d_raw.wav" % i)
        t = time.time()
        tts.infer(spk_audio_prompt=spk, text=frag["text"], output_path=raw,
                  emo_vector=frag["emo_vector"])
        total += time.time() - t

        if sr is None:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                  "stream=sample_rate", "-of", "csv=p=0", raw],
                                 stdout=subprocess.PIPE, text=True).stdout
            sr = int(out.strip().split(",")[0])

        gain = frag.get("gain_db", 0.0) + seeded_uniform(tag + "|gain", *jit["gain_db_range"])
        shaped = os.path.join(WORK, "_ix2frag%02d.wav" % i)
        sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
            "-i", raw, "-af", "volume=%.2fdB" % gain, "-ac", "1", "-ar", str(sr),
            "-flags", "+bitexact", shaped])
        parts.append(shaped)
        print("  frag %d %-42s afraid=%.2f gain=%+.1f  (%s)"
              % (i, '"' + frag["text"][:38] + '"', frag["emo_vector"][3], gain,
                 frag["intent"][:30]))

        br = frag.get("breath_after")
        if br:
            bp = os.path.join(WORK, "_ix2breath%02d.wav" % i)
            make_breath(bp, br["duration_ms"], br["gain_db"], sr,
                        int(hashlib.sha256((tag + "|breath").encode()).hexdigest()[:8], 16)
                        & 0x7FFFFFFF)
            parts.append(bp)

        gap = frag.get("gap_after_ms", 0)
        if gap:
            gap += seeded_uniform(tag + "|gap", *jit["gap_ms_range"])
            if gap > 0:
                sp = os.path.join(WORK, "_ix2gap%02d.wav" % i)
                make_silence(sp, gap, sr)
                parts.append(sp)

    listfile = os.path.join(WORK, "_ix2concat.txt")
    with open(listfile, "w") as fh:
        for p in parts:
            fh.write("file '%s'\n" % p.replace("\\", "/"))
    out = os.path.join(WORK, "ix2_performance_chain.wav")
    sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
        "-f", "concat", "-safe", "0", "-i", listfile,
        "-af", "loudnorm=I=-18:TP=-1:LRA=11", "-ac", "1", "-ar", str(sr),
        "-flags", "+bitexact", out])
    print("\ngeneration %.0f s total; wrote ix2_performance_chain.wav" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
