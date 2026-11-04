"""Spike 05d - IndexTTS-2: does emotion come without wrecking the voice?

The measured failure of Chatterbox was specific: its only intensity control
(`exaggeration`) is effectively a pitch dial. Swept on one fragment it gave
154 / 115 / 175 / 300 Hz median F0 at 0.50 / 0.60 / 0.75 / 0.95, so the
setting that sounded most distressed also stopped sounding like a man.

IndexTTS-2 separates the two: `spk_audio_prompt` fixes timbre, while emotion
arrives through an independent 8-float vector (happy, angry, sad, AFRAID,
disgusted, melancholic, surprised, calm), an emotion reference clip, or a text
description. If the disentanglement is real, median F0 should stay in the
speaker's band while the delivery changes.

That is the single measurement this spike exists to make. Run from the
IndexTTS repo's own environment:

  cd D:\\Work\\index-tts
  uv run python D:\\Work\\video_pipeline\\spikes\\05_voice\\indextts2_test.py
"""

import json
import os
import sys
import time

import numpy as np

REPO = r"D:\Work\index-tts"
OUT = r"D:\Work\video_pipeline\spikes\05_voice\_work"
SPK = os.path.join(OUT, "ref_male_piper.wav")

LINE = "Please, just say something."
LONG = ("Is somebody there? I can hear you walking around upstairs. "
        "Please, just say something.")

# [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
VARIANTS = [
    ("calm",          dict(emo_vector=[0, 0, 0, 0.0, 0, 0, 0, 1.0])),
    ("afraid-40",     dict(emo_vector=[0, 0, 0, 0.4, 0, 0, 0, 0.4])),
    ("afraid-70",     dict(emo_vector=[0, 0, 0, 0.7, 0, 0, 0, 0.2])),
    ("afraid-100",    dict(emo_vector=[0, 0, 0, 1.0, 0, 0, 0, 0.0])),
    ("afraid-sad",    dict(emo_vector=[0, 0, 0.4, 0.8, 0, 0.2, 0, 0.0])),
    ("text-terrified", dict(use_emo_text=True, emo_alpha=0.6,
                            emo_text="terrified, voice shaking, barely able to speak")),
]


def f0_stats(path):
    import soundfile as sf
    x, sr = sf.read(path)
    if x.ndim > 1:
        x = x.mean(axis=1)
    win, hop = int(0.04 * sr), int(0.01 * sr)
    vals = []
    for i in range(0, len(x) - win, hop):
        seg = x[i:i + win]
        if np.sqrt((seg ** 2).mean()) < 0.015:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, "full")[win - 1:]
        lo, hi = int(sr / 400), int(sr / 60)
        if hi >= len(ac):
            continue
        pk = int(np.argmax(ac[lo:hi])) + lo
        if ac[pk] > 0.3 * ac[0]:
            vals.append(sr / pk)
    if not vals:
        return {}
    v = np.array(vals)
    return {"f0_median": round(float(np.median(v))),
            "f0_p10": round(float(np.percentile(v, 10))),
            "f0_p90": round(float(np.percentile(v, 90))),
            "duration_s": round(len(x) / sr, 2)}


def main():
    sys.path.insert(0, REPO)
    os.chdir(REPO)
    from indextts.infer_v2 import IndexTTS2

    t0 = time.time()
    tts = IndexTTS2(cfg_path=os.path.join(REPO, "checkpoints", "config.yaml"),
                    model_dir=os.path.join(REPO, "checkpoints"),
                    use_fp16=True)
    print("model loaded in %.0f s" % (time.time() - t0))

    results = []
    for name, kw in VARIANTS:
        for label, text in (("short", LINE), ("long", LONG)):
            if label == "long" and name not in ("calm", "afraid-70", "text-terrified"):
                continue
            dst = os.path.join(OUT, "ix2_%s_%s.wav" % (label, name))
            t = time.time()
            tts.infer(spk_audio_prompt=SPK, text=text, output_path=dst, **kw)
            gen = time.time() - t
            st = f0_stats(dst)
            st.update({"variant": name, "line": label, "seconds": round(gen, 1),
                       "file": os.path.basename(dst), **{k: str(v) for k, v in kw.items()}})
            results.append(st)
            print("%-15s %-5s  F0 med %3s (p10 %3s p90 %3s)  %4.1f s audio  gen %5.1f s"
                  % (name, label, st.get("f0_median", "?"), st.get("f0_p10", "?"),
                     st.get("f0_p90", "?"), st.get("duration_s", 0), gen))

    with open(os.path.join(os.path.dirname(OUT), "indextts2_results.json"), "w") as fh:
        json.dump({"speaker_reference": os.path.basename(SPK),
                   "speaker_ref_f0_median": 147,
                   "runs": results}, fh, indent=2, sort_keys=True)
    print("\nspeaker reference median F0 was 147 Hz; male band is roughly 85-180 Hz.")
    print("Disentanglement holds if the afraid variants stay in that band.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
