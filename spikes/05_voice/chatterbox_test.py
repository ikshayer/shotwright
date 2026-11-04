"""Spike 05b - does an expressive model fix what band-limiting could not?

Piper's delivery was judged flat, and correctly: degradation removes timbral
tells but leaves prosody untouched. Pitch contour, stress and pacing survive
an 8 kHz mu-law channel intact, so a flat read stays flat.

Chatterbox (Resemble AI, MIT, 0.5B) exposes an explicit emotion-exaggeration
parameter plus cfg_weight for pacing. This generates the same lines across the
range so the delivery can be judged by ear, and records CPU timing - FR-08's
"CPU-only, never contends for the GPU lease" depends on that being viable.
"""

import json
import os
import sys
import time

import torch
import torchaudio
from chatterbox.tts import ChatterboxTTS

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "_work")
SEED = 20051114

LINES = {
    "diegetic": ("Is somebody there? I can hear you walking around upstairs. "
                 "Please, just say something."),
    "announce": ("If you are hearing this recording, the building has already been "
                 "evacuated. Do not attempt to return to the third floor."),
}

# exaggeration 0.5 is neutral; higher is more dramatic and speeds delivery up,
# which lower cfg_weight compensates for.
SETTINGS = [
    ("neutral", dict(exaggeration=0.5, cfg_weight=0.5)),
    ("expressive", dict(exaggeration=0.7, cfg_weight=0.3)),
    ("distressed", dict(exaggeration=1.0, cfg_weight=0.3)),
]


def main():
    os.makedirs(WORK, exist_ok=True)
    t0 = time.time()
    model = ChatterboxTTS.from_pretrained(device="cpu")
    load_s = time.time() - t0
    print("model loaded in %.1f s (cpu)" % load_s)

    results = []
    for line_key, text in LINES.items():
        for name, kw in SETTINGS:
            if line_key == "announce" and name == "distressed":
                continue  # a tannoy does not emote
            torch.manual_seed(SEED)
            t = time.time()
            wav = model.generate(text, **kw)
            gen_s = time.time() - t
            dst = os.path.join(WORK, "cb_%s__%s.wav" % (line_key, name))
            torchaudio.save(dst, wav, model.sr)
            dur = wav.shape[-1] / float(model.sr)
            rtf = gen_s / dur if dur else 0
            results.append({
                "line": line_key, "setting": name, **kw,
                "seconds": round(gen_s, 1), "audio_seconds": round(dur, 2),
                "realtime_factor": round(rtf, 2),
                "file": os.path.basename(dst),
            })
            print("%-9s %-11s exag=%.1f cfg=%.1f  %5.1f s for %4.1f s audio  (RTF %.1f)"
                  % (line_key, name, kw["exaggeration"], kw["cfg_weight"],
                     gen_s, dur, rtf))

    with open(os.path.join(HERE, "chatterbox_results.json"), "w") as fh:
        json.dump({"device": "cpu", "model_load_seconds": round(load_s, 1),
                   "sample_rate": model.sr, "seed": SEED, "runs": results},
                  fh, indent=2, sort_keys=True)
    print("\nwrote chatterbox_results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
