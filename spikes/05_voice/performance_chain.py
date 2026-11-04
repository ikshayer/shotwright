"""Spike 05c - scripted imperfection, because the model will not supply it.

Chatterbox's exaggeration dial raises intensity uniformly. Real distress is not
uniform: it restarts a word, stops to breathe, loses level, trails off. Those
are events at positions. Tag-based models ([sigh], [breath]) are reported to be
the least reliable exactly where we need them most, so this does not depend on
the model producing them.

Instead each fragment is generated separately - which also stops the model
smoothing prosody across a pause - then spliced with scripted silences,
procedurally synthesised breaths (no third-party rights, per FR-08) and seeded
level jitter.

Output: performance_single.wav (one-shot baseline) and performance_chain.wav.
"""

import hashlib
import json
import os
import subprocess
import sys
import time

import torch
import torchaudio
from chatterbox.tts import ChatterboxTTS

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "_work")
SR = None
# A fixed reference prompt is REQUIRED, not optional: without it Chatterbox
# picks a different speaker per call, so fragment-wise generation drifts
# identity mid-line (measured F0 spread 88-322 Hz). This reference is itself
# synthetic (Piper en_US-ryan-high), so it clones no real person.
REF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_work",
                   "ref_male_piper.wav")


def seeded_uniform(seed_material, lo, hi):
    h = int(hashlib.sha256(seed_material.encode()).hexdigest()[:8], 16)
    return lo + (h / 0xFFFFFFFF) * (hi - lo)


def sh(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        sys.stderr.write(" ".join(args)[:400] + "\n" + p.stderr[-2000:])
        raise SystemExit(1)


def make_breath(path, duration_ms, gain_db, sr, seed):
    """Procedural inhale: band-passed noise under a rising envelope.

    Synthesised rather than sampled, so it carries no third-party rights and
    its length is whatever the script asked for.
    """
    dur = duration_ms / 1000.0
    sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
        "-f", "lavfi", "-i",
        "anoisesrc=color=brown:amplitude=0.9:seed=%d:duration=%.3f:sample_rate=%d"
        % (seed, dur, sr),
        "-af", ("highpass=f=380:poles=2,lowpass=f=2100:poles=2,"
                "afade=t=in:st=0:d=%.3f:curve=exp,"
                "afade=t=out:st=%.3f:d=%.3f:curve=qsin,"
                "volume=%.2fdB" % (dur * 0.62, dur * 0.62, dur * 0.38, gain_db)),
        "-ac", "1", "-ar", str(sr), "-flags", "+bitexact", path])


def make_silence(path, duration_ms, sr):
    sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
        "-f", "lavfi", "-i",
        "anullsrc=r=%d:cl=mono:d=%.3f" % (sr, max(duration_ms, 1) / 1000.0),
        "-ac", "1", "-ar", str(sr), "-flags", "+bitexact", path])


def main():
    os.makedirs(WORK, exist_ok=True)
    script = json.load(open(os.path.join(HERE, "performance_script.json")))
    jit = script["jitter"]

    print("loading model (cpu)...")
    t0 = time.time()
    model = ChatterboxTTS.from_pretrained(device="cpu")
    sr = model.sr
    print("loaded in %.0f s, sr=%d" % (time.time() - t0, sr))

    # --- baseline: the whole line in one pass, as before ---
    whole = " ".join(f["text"] for f in script["fragments"][1:])
    torch.manual_seed(script["seed"])
    t = time.time()
    wav = model.generate(whole, exaggeration=0.9, cfg_weight=0.25,
                         audio_prompt_path=REF)
    torchaudio.save(os.path.join(WORK, "performance_single_ref.wav"), wav, sr)
    print("single-pass baseline: %.1f s" % (time.time() - t))

    # --- scripted: fragment by fragment ---
    parts, total_gen = [], 0.0
    for i, frag in enumerate(script["fragments"]):
        tag = "%s#%d" % (script["line_id"], i)
        torch.manual_seed(script["seed"] + i)
        t = time.time()
        w = model.generate(frag["text"],
                           exaggeration=frag.get("exaggeration",
                                                 script["base"]["exaggeration"]),
                           cfg_weight=frag.get("cfg_weight",
                                               script["base"]["cfg_weight"]),
                           audio_prompt_path=REF)
        total_gen += time.time() - t
        raw = os.path.join(WORK, "_frag%02d_raw.wav" % i)
        torchaudio.save(raw, w, sr)

        gain = frag.get("gain_db", 0.0) + seeded_uniform(
            tag + "|gain", *jit["gain_db_range"])
        shaped = os.path.join(WORK, "_frag%02d.wav" % i)
        sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
            "-i", raw, "-af", "volume=%.2fdB" % gain, "-ac", "1", "-ar", str(sr),
            "-flags", "+bitexact", shaped])
        parts.append(shaped)
        print("  frag %d %-38s exag=%.2f gain=%+.1f dB  (%s)"
              % (i, '"' + frag["text"][:34] + '"',
                 frag.get("exaggeration", 0), gain, frag["intent"][:28]))

        br = frag.get("breath_after")
        if br:
            bp = os.path.join(WORK, "_breath%02d.wav" % i)
            make_breath(bp, br["duration_ms"], br["gain_db"], sr,
                        int(hashlib.sha256((tag + "|breath").encode())
                            .hexdigest()[:8], 16) & 0x7FFFFFFF)
            parts.append(bp)

        gap = frag.get("gap_after_ms", 0)
        if gap:
            gap += seeded_uniform(tag + "|gap", *jit["gap_ms_range"])
            if gap > 0:
                sp = os.path.join(WORK, "_gap%02d.wav" % i)
                make_silence(sp, gap, sr)
                parts.append(sp)

    listfile = os.path.join(WORK, "_concat.txt")
    with open(listfile, "w") as fh:
        for p in parts:
            fh.write("file '%s'\n" % p.replace("\\", "/"))
    out = os.path.join(WORK, "performance_chain_ref.wav")
    sh(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
        "-f", "concat", "-safe", "0", "-i", listfile,
        "-af", "loudnorm=I=-18:TP=-1:LRA=11", "-ac", "1", "-ar", str(sr),
        "-flags", "+bitexact", out])
    print("\nfragments generated in %.0f s total" % total_gen)
    print("wrote performance_chain.wav")
    return 0


if __name__ == "__main__":
    sys.exit(main())
