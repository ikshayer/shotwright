# Spike 25 — voice conversion (Seed-VC): a real performance in another voice

**Status:** measured; rejected by ear for now. Runtime kept for later use.

## The question

Generated speech kept failing the creator's ear for emotion (IndexTTS-2, spike 26,
spike 27). The alternative is to keep a *human* performance and change only the voice:
speech-to-speech voice conversion. Does Seed-VC carry a frightened whispered performance
into a synthetic voice convincingly?

## Licences (read 2026-11-16)

| Component | Licence |
|---|---|
| Seed-VC code (`Plachtaa/seed-vc`) | GPL-3.0 |
| Seed-VC weights (`Plachta/Seed-VC`) | GPL-3.0 (Hugging Face metadata; the card itself is empty) |
| Whisper-small (content encoder) | Apache-2.0 |
| CAMPPlus (speaker encoder) | Apache-2.0 |
| BigVGAN v2 22 kHz (vocoder) | MIT |

GPL-3.0 permits commercial use. The orchestrator would run Seed-VC as a separate process
and never distribute it, so no copyleft obligation reaches this repository; its outputs
are not covered by the GPL. Chatterbox's VC mode was the alternative: MIT, but retired
("As of August 2026, Chatterbox is retired research") and every output carries a
mandatory PerTh watermark with no documented opt-out.

## Setup

Under the gitignored `spikes/25_voice_conversion/_work/`:

- `seed-vc` cloned at commit `51383ef`.
- `env`: Python 3.11 venv. The repository's `requirements.txt` pins `torch==2.4.0`
  alongside nightly cu126 builds, and neither supports the RTX 5070 Ti (sm_120), so torch
  **2.8.0+cu128** was installed instead (the build IndexTTS-2 already uses here), plus only
  what inference imports: scipy 1.13.1, librosa 0.10.2, huggingface-hub, munch 4.0.0,
  einops 0.8.0, transformers 4.46.3, soundfile 0.12.1, numpy 1.26.4, pyyaml, pydub 0.25.1,
  tqdm, and **descript-audio-codec 1.0.0** (imported by `modules/length_regulator.py`; the
  first run failed without it).
- `HF_HOME` pointed at `_work/hf_cache`, so every downloaded model stays inside the spike.
  After the first run, inference works with `HF_HUB_OFFLINE=1`.

```powershell
$env:HF_HOME = "<spike>\_work\hf_cache"
python inference.py --source <performance.wav> --target <voice_ref.wav> --output <dir> `
  --diffusion-steps 25 --inference-cfg-rate 0.7 --f0-condition False --fp16 True
```

## Measured

Source: the creator's own 27.2 s whispered "hiding" performance. Targets: 13.5 s (male,
Piper `en_US-ryan-high`) and 11.9 s (female, Piper `en_US-lessac-medium`) synthetic clips.

| | Duration | RMS | Voiced share | RTF |
|---|---|---|---|---|
| Source performance | 27.2 s | −44.0 dBFS | 0.47 | — |
| → male voice | 27.19 s | −18.7 dBFS | 0.54 | 0.34 |
| → female voice | 27.19 s | −20.8 dBFS | 0.62 | 0.26 |

Timing is preserved frame for frame. The first run took 188 s including the one-time
model download; the second, 32 s.

## Result

**The creator judged the conversions "not particularly good".** One measurable cause: the
voiced share rose from 0.47 to 0.54–0.62, so part of the whisper was converted into
voiced speech. Seed-VC's own benchmarks include a whisper-to-normal task, so this is
consistent with its training rather than a setup fault.

## Still open

- `--f0-condition True`, and a normal-voice (not whispered) performance as the source.
- A trained voice actor's performance rather than the creator's. Voice conversion only
  carries what the performance contains.
