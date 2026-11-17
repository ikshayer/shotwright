# Spike 26 — Fish Audio Drama 3 (compressed "Drama3-small") on a 12 GB card

**Status:** measured; rejected by ear ("worse than IndexTTS"). Runtime kept: spike 27 uses it.

## The question

Fish Audio Drama 3 (`fishaudio/drama-3`) is built for expressive multi-character drama:
inline event tags, English style directions, voice cloning. Can it run on this machine,
and does it deliver frightened speech better than IndexTTS-2?

## Sizes and licence

- Full model: 35.9B-parameter MoE (256 experts, 8 active, ~3B active per token), FP8
  experts, **38.7 GB**. No quantization fits 12 GB of VRAM: `Jmica/Drama-3-GGUF` (the most
  used conversion) runs from **15.9 GiB (Q2_K)** to 22.9 GiB, and needs audio.cpp's
  disk-backed experts to run in 32 GB of RAM.
- **Drama3-small** (`pentacoxian-dev/drama3-small-GGUF`): experts pruned to 45% of their
  slots, IQ3_XXS, distilled back towards the original. **9.74 GiB**, built for 12 GB cards.
- Licence: **Fish Audio Community License v1.0** — free for research and non-commercial use;
  commercial use permitted while annual revenue is under **USD 500,000** (§III(a)); no
  hosted service without a separate licence (§III(c)); outputs owned by the user
  (§IV(iii)); no imitating an identifiable person without consent (§IV).

## Runtime

audio.cpp (`0xShug0/audio.cpp`, Apache-2.0) **v0.9.5**, prebuilt `windows-x64-cuda13.3`
binaries plus the CUDA 13.3 runtime (SHA-256 checked against the GitHub release digests).
The CUDA 12.4 build would not support sm_120. Drama 3 has been in mainline audio.cpp since
v0.9.3; nothing had to be compiled. Model pinned to revision `4a3078e`, every file
checksum-verified. Everything lives under the gitignored `_work/`.

`run_tests.ps1` (this folder) reproduces the takes. Reference voice: a 13.5 s
`en_US-ryan-high` Piper clip with its exact transcript (`--voice-ref` plus
`reference_text`). `moe_cpu_expert_layers=8` left headroom on the card.

## Measured

| Take | Frames | Duration | RMS | Median F0 |
|---|---|---|---|---|
| Hiding, directed (`[whispering, terrified…]`, `[gasps]`, `<whisper>`) | 226 | 10.5 s | −28.2 dBFS | 114 Hz |
| Panic, directed, `top_k=30` | 528 | 24.5 s | — | — |
| Panic, directed, default `top_k` (4097) | 159 | 7.4 s | −13.2 dBFS | 244 Hz |
| Hiding, plain | — | 7.9 s | −15.2 dBFS | 163 Hz |

Generation ran at roughly 1.85× real time (226 frames in 19.4 s) after a 30–50 s model
load per CLI invocation.

**`top_k=30` stalls.** The Drama3-small model card recommends it, but audio.cpp's own
docs warn it "can stall in long silences", and it did: the speech ended at ~10.4 s and was
followed by 12.5 s of silence. The runtime default removed the stall.

**PowerShell 5.1 note.** `audiocpp_cli` writes normal progress to stderr; with
`$ErrorActionPreference = "Stop"` PowerShell 5.1 treats the first stderr line as an error
and aborts the script.

## Result

The creator judged the takes worse than IndexTTS-2's. Two confounds remain: every take
cloned a flat Piper reference (see spike 27), and Drama3-small is heavily compressed. The
full model was not tried.
