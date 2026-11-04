# Spike 05 — voice: what the creator's ear rejected, and why

Run 2026-11-04. Five iterations, each driven by a specific rejection. The
rejections were more informative than the measurements, and twice they
disproved a hypothesis of mine.

## Result

**Approved configuration:** IndexTTS-2 for diegetic speech, generated as whole
committed takes, structured afterwards by a `PerformanceEdit`, degraded through an
`AudioPostProfile`. Piper remains correct for processed sources (PA, answering
machine, emergency broadcast) where flat delivery is the point.

## Finding 1 — degradation hides timbre, not performance

The opening hypothesis, carried over from the face rule: a voice through a camera mic
is band-limited to ~220–3600 Hz, so synthesis tells should vanish the way geometry
tells vanish under block quantisation.

**Half right, and the wrong half.** Band-limiting does remove timbral artifacts —
sibilance, noise floor, spectral smoothness. It does nothing to prosody. Pitch
contour, stress, pacing and hesitation sit squarely inside the surviving band, so a
flat read stays flat and arguably becomes *more* exposed once the top end is gone.

The face analogy only transfers to the spatial case. Destroying spatial information
hides bad geometry; destroying bandwidth does not hide bad acting.

## Finding 2 — Chatterbox's `exaggeration` is a pitch dial, not an emotion dial

Swept on one fragment, speaker reference held fixed:

| exaggeration | median F0 |
|---|---|
| 0.50 | 154 Hz |
| 0.60 | 115 Hz |
| 0.75 | 175 Hz |
| **0.95** | **300 Hz** |

Adult male speech is roughly 85–180 Hz. The setting that sounded most distressed was
the setting that stopped sounding like a man — which is exactly what the creator
reported ("strangely high pitched for a man"). There is no way to get intensity out of
this control without losing the voice.

A diagnosis of mine was also wrong here: I attributed the chain's 88–322 Hz spread to
speaker drift between fragments. Per-segment measurement showed four of five fragments
at 96–149 Hz and only the exaggeration-0.95 one at 333 Hz. It was one bad fragment, not
drift, and the pinned reference I added to "fix" it changed the median by 1 Hz.

## Finding 3 — IndexTTS-2 separates timbre from emotion, and fear legitimately raises pitch

Speaker reference fixed, emotion varied through the 8-float vector:

| variant | median F0 |
|---|---|
| calm | **108–114 Hz** |
| afraid 0.4 | 172 Hz |
| afraid 0.7 | 206–235 Hz |
| afraid 1.0 | 266 Hz |
| text "terrified, voice shaking" | 204–207 Hz |

The calm baseline is stable and solidly male — speaker identity holds, which Chatterbox
never managed. Emotion still moves pitch, but **this is correct behaviour**: frightened
people do rise in pitch. afraid 0.4–0.7 is plausible for a scared adult male; 1.0 reads
as a yelp.

**This corrected an acceptance criterion I had proposed.** I suggested rejecting
fragments whose median F0 leaves the male band — a rule that would discard genuinely
frightened speech. The correct criterion is that the **calm baseline** sits in the
speaker's band, with emotion-dependent excursion allowed above it.

## Finding 4 — structure must be applied by editing, not by fragmenting generation

The `PerformanceScript` generated each fragment separately to get false starts, gaps
and a level contour. It produced the right structure and worse speech: the creator
identified that everything before "Please," was worse than a single long take.

The cause is that autoregressive TTS degrades on short inputs. Generating `"Is"` as a
standalone utterance is close to a worst case — no context to build prosody from. I was
damaging the thing I most needed to protect in order to add something that could be
added afterwards.

**`PerformanceEdit` inverts it:** generate one committed take, then cut at the *middle*
of pauses the take already contains, widen those silences, drop procedural breaths into
them, shape levels across segments, and build the false start by copying the take's own
first 300 ms. No speech sample is regenerated, so the approved quality is the shipped
quality.

| pause | natural | widened to |
|---|---|---|
| after "Is somebody there?" | 203 ms | 1100 ms + breath |
| after "walking around upstairs." | 146 ms | 760 ms + breath |
| after "Please," | 124 ms | 470 ms |

5.72 s → 8.11 s. This is the audio analogue of `PostProfile`: a versioned, seeded
transform over a master, not a re-render.

**Known limitation:** the edit can only widen pauses the take already has. Where the
script wants a pause the engine did not produce, the prompt must induce one through
punctuation — a note for FR-19.

## Finding 5 — the RAM precondition is load-bearing and was never measured

Loading IndexTTS-2 failed twice on a machine with 31.1 GB of RAM: once with
`DefaultCPUAllocator: not enough memory` on a **2 MB** allocation, once with a
**segmentation fault**. Free physical memory was 6.0 GB, committed 62.8 GB, with ~9.4 GB
held by a browser across 68 processes. After the creator closed it, 13.6 GB free, and
the run succeeded immediately.

Three things follow:

1. **VRAM was measured meticulously and RAM not at all**, yet the failure mode is
   identical in shape — an opaque allocator error caused by *other applications*.
2. **The CPU-side load path matters even for GPU work.** `gpt.pth` is 3.3 GB and loads
   through `torch.load(map_location='cpu')`, so free VRAM is not sufficient to start a
   job.
3. **Failure at this boundary is not graceful.** A misleading 2 MB error and a segfault
   are both uncatchable as "out of memory" without a precondition check.

## Finding 6 — apply the licence rule to the licence, not the README

IndexTTS-2's README says "For commercial usage and cooperation, please contact
indexspeech@bilibili.com", and several secondary sources call the model Apache-2.0. Both
are misleading. The LICENSE file grants a worldwide, non-exclusive, royalty-free licence
(clause 2.1), requiring a separate licence only above **100 M monthly active users or
RMB 1 bn annual revenue** (2.2) — a threshold licence, not a non-commercial one.

I reported it as non-commercial before reading the file, and had to correct it. FR-16's
licence gate must be applied to licence text.

Obligations that do apply: attribution language when distributing a *modified* model
(4.1a), responsibility for third-party data or weights added (4.1b), and a prohibited
high-risk list including large-scale biometric surveillance (4.2) — which bars deploying
it as surveillance, not depicting surveillance in fiction.

## Measured performance

| | model load | generation | notes |
|---|---|---|---|
| Piper (CPU) | ~2 s | RTF ~0.2 | fine for processed sources |
| Chatterbox (CPU) | 196 s | RTF ~6.3 | no GPU contention, batch speed |
| **IndexTTS-2 (GPU)** | **42–53 s** | **RTF ~1.2–3.4** | 5 fragments in 35 s |

IndexTTS-2 uses the GPU, so FR-08's "CPU-only, never contends for the lease" no longer
holds for the diegetic class; it needs a slot in §8.3's serialization chain. It also
logged `Ninja is required to load C++ extensions` and fell back gracefully — the
compiled-extension fragility §8.4 warns about.

## Still open

The emotion arc in the edit is **my** directing guess — wary, forcing composure,
breaking, defeated. That is a creative decision the creator owns (§5) and FR-19 should
produce, not something hand-written in JSON.

`emo_audio_prompt` is untested: IndexTTS-2 can take emotion from a reference clip by a
*different speaker* than the voice, which would carry a specific performance's voice
quality rather than the model's generic notion of afraid. It needs a few seconds of a
genuine performance — the creator's own or licensed, never cloned from a real person
without consent.
