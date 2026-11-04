"""Phase 0 spike 05 - how realistic does the voice actually need to be?

FR-08 names Piper or Kokoro-82M, CPU-only, so TTS never contends for the GPU
lease. The creator asked for a realistic voice, which on its face argues for a
heavier GPU model (Chatterbox, XTTS, Sesame CSM) and for voice cloning.

Before accepting that cost, test the cheaper hypothesis - the same one that
settled the face question in the reference work. A voice heard through a
camera mic, a handset or a tannoy is band-limited, compressed and noisy. Most
of what betrays synthetic speech (over-smooth sibilance, absent breath, a too-
clean noise floor) lives in exactly the material that treatment destroys.

So: generate with the *cheapest* engine the PRD names, degrade it through an
AudioPostProfile, and measure what survives. If band-limited Piper is
acceptable, a GPU voice model is an optimisation rather than a requirement.

Produces listenable files plus objective spectral measurements.
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "_work")
VOICE = os.path.join(WORK, "en_US-lessac-medium.onnx")
PY = os.path.join(HERE, "..", "..", ".venv", "Scripts", "python.exe")

LINES = {
    "announce": ("If you are hearing this recording, the building has already been "
                 "evacuated. Do not attempt to return to the third floor."),
    "diegetic": ("Is somebody there? I can hear you walking around upstairs. "
                 "Please, just say something."),
}


def sh(args, **kw):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, **kw)
    if p.returncode != 0:
        sys.stderr.write(" ".join(str(a) for a in args) + "\n" + p.stderr[-2500:])
        raise SystemExit(1)
    return p.stdout + p.stderr


def seed_for(profile_id, stage_name, line_key):
    import hashlib
    material = "%s|%s|%s" % (profile_id, stage_name, line_key)
    # masked to INT32_MAX, per spike 01 finding 1
    return int(hashlib.sha256(material.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def build_chain(profile, line_key):
    """Return (filter_complex, input_args). Noise and tone stages need extra
    inputs, so the chain is built as a filter_complex rather than -af."""
    inputs, filters, extra = [], [], 0
    chain = "[0:a]"
    simple = []
    for st in profile["stages"]:
        f, p = st["filter"], st.get("params", {})
        if f == "_noise_mix":
            continue
        if f == "_tone_mix":
            continue
        if f in ("highpass", "lowpass"):
            simple.append("%s=f=%s:p=%s" % (f, p["f"], p.get("poles", 2)))
        elif f == "acompressor":
            simple.append("acompressor=threshold=%s:ratio=%s:attack=%s:release=%s:makeup=%s"
                          % (p["threshold"], p["ratio"], p["attack"], p["release"], p["makeup"]))
        elif f == "aecho":
            simple.append("aecho=%s:%s:%s:%s" % (p["in_gain"], p["out_gain"],
                                                 p["delays"], p["decays"]))
        elif f == "acrusher":
            simple.append("acrusher=level_in=%s:level_out=%s:bits=%s:mode=%s:aa=%s"
                          % (p["level_in"], p["level_out"], p["bits"], p["mode"], p["aa"]))
        elif f == "alimiter":
            simple.append("alimiter=limit=%s" % p["limit"])
        elif f == "vibrato":
            simple.append("vibrato=f=%s:d=%s" % (p["f"], p["d"]))
        else:
            raise SystemExit("unknown audio filter: %s" % f)

    body = ",".join(simple) if simple else "anull"
    graph = chain + body + "[voiced]"
    last = "[voiced]"

    for st in profile["stages"]:
        f, p = st["filter"], st.get("params", {})
        if f == "_noise_mix":
            extra += 1
            idx = extra
            inputs += ["-f", "lavfi", "-i",
                       "anoisesrc=color=%s:amplitude=%s:seed=%d:duration=600"
                       % (p["color"], p["amplitude"], seed_for(profile["profile_id"],
                                                               st["name"], line_key))]
            graph += ";%s[%d:a]amix=inputs=2:duration=first:weights=1 1[mix%d]" % (last, idx, idx)
            last = "[mix%d]" % idx
        elif f == "_tone_mix":
            extra += 1
            idx = extra
            inputs += ["-f", "lavfi", "-i",
                       "sine=frequency=%s:sample_rate=48000:duration=600" % p["frequency"]]
            graph += (";[%d:a]volume=%s[hum%d];%s[hum%d]amix=inputs=2:duration=first"
                      ":weights=1 1[mix%d]" % (idx, p["amplitude"], idx, last, idx, idx))
            last = "[mix%d]" % idx

    # FR-08 targets -18 LUFS / -1 dBTP. Band-limiting and mixing cost ~20 dB,
    # so every audio profile ends in normalisation or the mix arrives silent.
    graph += ";%sloudnorm=I=-18:TP=-1:LRA=11[out]" % last
    return graph, inputs


def measure(path):
    out = sh(["ffmpeg", "-hide_banner", "-nostdin", "-i", path,
              "-af", "ebur128=peak=true,astats=metadata=1", "-f", "null", "-"])
    res = {}
    for key, tag in (("I:", "lufs"), ("Peak:", "peak_db")):
        for line in out.splitlines():
            if line.strip().startswith(key):
                try:
                    res[tag] = float(line.split()[1])
                except Exception:
                    pass
    probe = sh(["ffprobe", "-hide_banner", "-loglevel", "error", "-show_entries",
                "stream=sample_rate,codec_name,duration", "-of", "json", path])
    st = json.loads(probe)["streams"][0]
    res["sample_rate"] = int(st["sample_rate"])
    res["codec"] = st["codec_name"]
    res["nyquist_hz"] = int(st["sample_rate"]) // 2
    return res


def main():
    os.makedirs(WORK, exist_ok=True)
    if not os.path.exists(VOICE):
        raise SystemExit("voice model missing: %s" % VOICE)
    profiles = json.load(open(os.path.join(HERE, "audio_profiles.json")))["profiles"]

    results = []
    for key, text in LINES.items():
        src = os.path.join(WORK, "clean_%s.wav" % key)
        if not os.path.exists(src):
            p = subprocess.run([PY, "-m", "piper", "-m", VOICE, "-f", src],
                               input=text, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if p.returncode != 0:
                raise SystemExit(p.stderr[-2000:])

        for prof in profiles:
            dst = os.path.join(WORK, "%s__%s.wav" % (key, prof["profile_id"]))
            graph, extra_inputs = build_chain(prof, key)
            enc = prof["encode"]
            args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-fflags", "+bitexact", "-i", src] + extra_inputs + [
                    "-filter_complex", graph, "-map", "[out]",
                    "-c:a", enc["codec"], "-ar", str(enc["rate"]), "-ac", "1",
                    "-flags", "+bitexact", dst]
            sh(args)
            m = measure(dst)
            m.update({"line": key, "profile": prof["profile_id"],
                      "intent": prof["intent"],
                      "file": os.path.basename(dst),
                      "bytes": os.path.getsize(dst)})
            results.append(m)
            print("%-10s %-14s %5d Hz  %-10s  %s"
                  % (key, prof["profile_id"], m["sample_rate"], m["codec"],
                     "LUFS %.1f" % m["lufs"] if "lufs" in m else ""))

    with open(os.path.join(HERE, "results.json"), "w") as fh:
        json.dump(results, fh, indent=2, sort_keys=True)
    print("\nwrote results.json; listen to the files in _work/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
