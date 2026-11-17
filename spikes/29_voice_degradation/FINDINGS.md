# Spike 29 — degrading generated voices: does it make the emotion believable?

**Status:** the creator picked a direction, **"overdriven"**. It feeds FR-32's AudioPostProfile.

## The question

Generated takes kept failing the creator's ear for emotion (spikes 25–28). Analog horror
voices are rarely heard clean; they come through a camera mic, a phone or a tape. Does
degradation make the frightened takes believable?

## Inputs

Four IndexTTS-2 takes from spikes 27–28: the hiding line with the creator's whisper as the
emotion reference (the creator's favourite), the same speaker with no emotion reference,
the CREMA-D panic take, and the CREMA-D crying take.

## Rounds

All outputs were loudness-normalised to −18 LUFS (FR-08's target), so a quiet whisper ends
up as loud as a panic.

1. **Spike 05's approved profiles, unchanged** (`dvr-mic-2005`, `phone-call`, `tape-dub`,
   built with spike 05's own `run_spike.build_chain`). The creator asked for more.
2. **Heavier** (`cctv-heavy`, `cctv-wrecked`, `tape-heavy` in `degrade.py`): a narrower band,
   20:1 automatic gain that drags breaths and pauses up, 8- or 6-bit crush, more hiss and
   hum, a bigger room; `cctv-wrecked` adds 6 kHz aliasing, random ~14.5 ms dropouts (320 samples at 22.05 kHz) and hard
   clipping. The creator asked for the *voice* to be more distorted.
3. **Distortion in the voice itself** (`overdriven`, `crushed`, `broken-radio`):
   - `overdriven`: +24 dB into hard clipping at 0.25, then a 450–2800 Hz band, a small room
     and 12:1 compression;
   - `crushed`: a 4-bit, 5× sample-rate decimation crush plus clipping;
   - `broken-radio`: overdrive, 11 Hz tremolo, pitch wobble, a 6-bit crush and 4% dropouts.

## Result

**The creator picked `overdriven`** (2026-11-17). Its chain, verbatim:

```text
highpass=f=350:p=2, volume=24dB, asoftclip=type=hard:threshold=0.25,
lowpass=f=2800:p=2, highpass=f=450:p=2, aecho=0.8:0.45:28|47:0.3|0.2,
acompressor=threshold=0.03:ratio=12:attack=5:release=500:makeup=4
+ pink noise 0.035 (seed 4242) + 60 Hz hum 0.02 (+180 Hz at a third)
-> loudnorm I=-18 TP=-1 LRA=11 -> 8 kHz mu-law
```

It is deterministic. `degrade.py --profiles overdriven` reproduced the chosen file byte for
byte.

## What this means

The working voice recipe for quiet fear, end to end:

1. a Ming-designed voice re-rendered as a frightened whisper (spike 27) as the speaker;
2. the creator's own whispered performance as the IndexTTS-2 emotion reference (spike 28);
3. broken phrasing in the text;
4. **`overdriven` degradation** on the take.

For loud fear (panic), CREMA-D fear as the emotion reference works (spike 28).

## Still open

- Tuning: a ladder of drive strengths, with and without room and dropouts.
- Promotion to a versioned, seeded `AudioPostProfile` in the pipeline (FR-32).
- Spike 05's version of this chain is mu-law at 8 kHz; whether the final mix should keep
  that or stay wideband is a decision for the mix stage.

## Dropout rate (2026-11-17)

Rendered on the favourite take at +24 dB drive. Each dropout mutes one 320-sample frame
(~14.5 ms at 22.05 kHz), voice, hiss and hum together, before loudness normalisation.

| Rate | Gaps in 14.7 s | Character |
|---|---|---|
| 3% | 32 | an occasional tick |
| 10% | 90 (~6/s) | a steady crackle |
| 20% | 163 (~11/s) | a fast crackle or stutter |
| 60% | 232, many merged | more silence than voice |

## Decision (2026-11-17)

**`cctv-overdriven` = the `overdriven` chain at +30 dB drive, room echo on, no dropouts**
(`degrade.py` `overdriven` with `volume=30dB`). The creator compared drive levels from
+12 to +36 dB, with and without room echo, and dropout rates of 3%, 10%, 20% and 60%;
dropouts were rejected.
