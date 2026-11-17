# Spike 27 — Ming-3.0-tts-0.5B: voice design, and better speaker references for IndexTTS-2

**Status:** measured. Ming is useful as a **voice designer**, not as the performer.

## The question

`inclusionAI/Ming-3.0-tts-0.5B` offers voice design from a natural-language description,
free-form delivery direction, an emotion template, 14 inline paralinguistic tags and
zero-shot cloning. Does it perform fear convincingly, and can its designed voices replace
the flat Piper references every earlier test cloned?

## Licence and runtime

- Model: **MIT** (inclusionAI). GGUF conversion `audio-cpp/Ming-3.0-TTS-0.5B-GGUF`, MIT,
  pinned to revision `2eb8d37`; `ming-3.0-tts-0.5b-bf16.gguf` (2.86 GiB, SHA-256
  `43f108a4…5312`) verified.
- Runtime: the audio.cpp v0.9.5 build from spike 26 (family `ming3_tts`: TTS, Clone,
  Design, Ctrl). No Python and no upstream `custom_code` runs.
- About 7–8 s per take on the RTX 5070 Ti, model load included.

Its own stated limits matter here: emotion control is weaker "when the words carry no
emotional signal" (its CV3 benchmark covers happy/sad/angry only, not fear; text-unrelated
sad 36%); several tags in one sentence are less stable; no gasp, whisper or sob tag
(`[Breathing]`, `[Sigh]`, `[Surprise-ah]` and others exist).

## Usage that worked

```text
voice design  --task vdes --request-option instruct="<voice description>"
freeform      --task tts|clon --request-option template_name=freeform --request-option instruct="<direction>"
emotion       --task clon --request-option template_name=emotion --request-option emotion=恐惧
```

`run_tests.ps1` reproduces the four direct takes. The designed voice's description is in
`ming_male_voice.description.txt`.

**PowerShell 5.1 garbles non-ASCII literals in a BOM-less script.** The first "fearful"
takes received a mojibake label. Proven deterministically: with seed and temperature fixed,
the output was byte-identical to a run given a deliberately garbled label, and differed from
both the correct label and `中性` (neutral). The script now builds the label from code
points (`[char]0x6050 + [char]0x60E7`).

## Measured

| Take | Duration | RMS | Voiced share | Median F0 |
|---|---|---|---|---|
| Hiding, freeform (no reference; the instruction chose the voice) | 8.0 s | −22.5 dBFS | 0.53 | 149 Hz |
| Panic, freeform | 7.6 s | −20.4 dBFS | 0.70 | 261 Hz |
| Hiding, `emotion=恐惧`, cloning the Piper male | 7.1 s | −14.9 dBFS | 0.69 | 201 Hz |
| Panic, `emotion=恐惧`, cloning the Piper male | 4.8 s | −15.0 dBFS | 0.64 | 206 Hz |
| **Designed voice** (calm reading, `vdes`) | 11.2 s | −23.4 dBFS | 0.55 | 112 Hz |
| **Same voice directed to whisper in terror** (`clon` of the designed voice + freeform) | 9.2 s | −27.6 dBFS | 0.40 | 91 Hz |

## Result: Ming as the speaker reference for IndexTTS-2

Used as IndexTTS-2 speaker references (registered as `synthetic` assets, `reuse_scope`
`project`, since Ming is MIT), the designed voices changed IndexTTS-2's results. Every
earlier test had cloned a monotone Piper clip, and the speaker clip carries pacing and
intonation as well as timbre.

The combination the creator called **"more realistic"**:

- **speaker:** the designed voice re-rendered as a frightened whisper;
- **emotion reference:** the creator's own whispered performance (IndexTTS-2 `reference`
  mode, strength 1.0);
- **text with broken phrasing:** "Don't... don't move. Please, don't make a sound…"

That take ran 14.7 s against 9.1 s for the same speaker with no emotion reference: the
performance mainly slowed the delivery and lengthened the pauses. The creator still did not
hear it as convincingly scared until it was degraded (spike 29).
