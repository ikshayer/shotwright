"""Voice degradation profiles tried in spike 29, for listening tests.

Each profile is one FFmpeg filter_complex: voice chain -> mixed with seeded noise and 60/180 Hz
hum -> optional dropouts or clipping -> loudness normalised to -18 LUFS (FR-08's target) ->
encoded. The creator's pick (2026-11-17) is "overdriven".

Usage: python degrade.py <input.wav> [<input.wav> ...] --out <dir> [--profiles a,b]
"""

import argparse
import os
import subprocess

PROFILES = {
    "cctv-heavy": dict(
        voice=("aecho=0.8:0.5:28|47|71:0.35|0.25|0.15,highpass=f=450:p=2,lowpass=f=2600:p=2,"
               "acompressor=threshold=0.02:ratio=20:attack=5:release=600:makeup=6,"
               "acrusher=level_in=1:level_out=0.9:bits=8:mode=log:aa=0.3"),
        noise=("pink", 0.035), hum=0.02, post="", encode=("pcm_mulaw", 8000)),
    "cctv-wrecked": dict(
        voice=("aecho=0.8:0.5:28|47|71:0.35|0.25|0.15,highpass=f=500:p=2,lowpass=f=2400:p=2,"
               "acompressor=threshold=0.02:ratio=20:attack=5:release=600:makeup=8,"
               "aresample=6000,aresample=22050,"
               "acrusher=level_in=1.2:level_out=0.8:bits=6:mode=log:aa=0.1"),
        noise=("pink", 0.05), hum=0.03,
        # ~3% of 40 ms frames drop out entirely; then hard clipping.
        post=",asetnsamples=n=320,volume='if(lt(random(1),0.03),0,1)':eval=frame,asoftclip=type=hard:threshold=0.6",
        encode=("pcm_mulaw", 8000)),
    "tape-heavy": dict(
        voice=("lowpass=f=3000:p=2,highpass=f=120:p=1,vibrato=f=0.9:d=0.25,vibrato=f=8:d=0.06,"
               "asoftclip=type=tanh:threshold=0.5,"
               "acompressor=threshold=0.05:ratio=8:attack=15:release=350:makeup=4"),
        noise=("white", 0.06), hum=0.01, post="", encode=("pcm_s16le", 22050)),
    "overdriven": dict(
        voice=("highpass=f=350:p=2,volume=24dB,asoftclip=type=hard:threshold=0.25,"
               "lowpass=f=2800:p=2,highpass=f=450:p=2,"
               "aecho=0.8:0.45:28|47:0.3|0.2,"
               "acompressor=threshold=0.03:ratio=12:attack=5:release=500:makeup=4"),
        noise=("pink", 0.035), hum=0.02, post="", encode=("pcm_mulaw", 8000)),
    "crushed": dict(
        voice=("highpass=f=400:p=2,lowpass=f=3000:p=2,volume=12dB,"
               "acrusher=level_in=1:level_out=0.9:bits=4:mode=lin:aa=0:samples=5,"
               "asoftclip=type=hard:threshold=0.4,"
               "acompressor=threshold=0.03:ratio=12:attack=5:release=500:makeup=4"),
        noise=("pink", 0.03), hum=0.02, post="", encode=("pcm_mulaw", 8000)),
    "broken-radio": dict(
        voice=("highpass=f=500:p=2,volume=20dB,asoftclip=type=hard:threshold=0.3,"
               "lowpass=f=2400:p=2,tremolo=f=11:d=0.6,vibrato=f=4:d=0.12,"
               "acrusher=level_in=1:level_out=0.9:bits=6:mode=log:aa=0.2,"
               "acompressor=threshold=0.03:ratio=12:attack=5:release=500:makeup=4"),
        noise=("pink", 0.045), hum=0.025,
        post=",asetnsamples=n=320,volume='if(lt(random(1),0.04),0,1)':eval=frame",
        encode=("pcm_mulaw", 8000)),
}


def degrade(src: str, dst: str, profile: dict) -> None:
    color, amp = profile["noise"]
    graph = (f"[0:a]aformat=channel_layouts=mono,aresample=22050,{profile['voice']}[v];"
             f"anoisesrc=color={color}:amplitude={amp}:seed=4242:sample_rate=22050[n];"
             f"sine=f=60:sample_rate=22050,volume={profile['hum']}[h1];"
             f"sine=f=180:sample_rate=22050,volume={profile['hum'] / 3:.4f}[h2];"
             f"[v][n][h1][h2]amix=inputs=4:duration=first:normalize=0{profile['post']},"
             f"loudnorm=I=-18:TP=-1:LRA=11[out]")
    codec, rate = profile["encode"]
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                    "-filter_complex", graph, "-map", "[out]",
                    "-c:a", codec, "-ar", str(rate), "-ac", "1", dst], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--out", required=True)
    parser.add_argument("--profiles", default=",".join(PROFILES))
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for src in args.inputs:
        stem = os.path.splitext(os.path.basename(src))[0]
        for name in args.profiles.split(","):
            dst = os.path.join(args.out, f"{stem}__{name}.wav")
            degrade(src, dst, PROFILES[name])
            print(dst)
