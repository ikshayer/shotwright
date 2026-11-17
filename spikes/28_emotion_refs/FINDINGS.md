# Spike 28 — emotion references for IndexTTS-2: own performance, CREMA-D, synthetic

**Status:** measured. Which source works depends on the *kind* of emotion.

## The question

IndexTTS-2's `reference` emotion mode (CONTRACT 3.4, 2026-11-16) takes the delivery from a
recorded performance while the voice stays the speaker's. The creator is not a voice
actor. Where can performances come from, and do they work?

## Sources considered (licences read at the source, 2026-11-17)

| Source | Fear? | Licence | Usable |
|---|---|---|---|
| The creator's own recordings | as performed | own | yes: `creator_own_voice` |
| **CREMA-D:** 91 actors, 6 emotions, 4 intensity levels | yes | **ODbL 1.0 / DbCL 1.0** (repository `LICENSE.txt`) | yes, with attribution |
| Expresso (Meta) | some | CC BY-NC 4.0 | non-commercial only |
| RAVDESS | yes | CC BY-NC-SA 4.0 | non-commercial only |

Several Hugging Face mirrors tag these datasets Apache or MIT, contradicting the originals.
The originals govern.

**Consent.** CREMA-D's actors licensed their recordings for reuse, not for cloning. The
creator approved a fourth consent value, `licensed_recording` (CONTRACT 5.2, 2026-11-17):
such a clip may be an emotion reference only, and the pipeline refuses it as a speaker.

## Selecting clips

`summaryTable.csv` records which emotion raters heard from the **audio alone**
(`VoiceVote`) and how intense it was (`VoiceLevel`). `select_crema.py` ranks actors by how
many of their Fear (Sad) clips were heard as fear (sadness), then joins each chosen actor's
most intense clips (2–3 s each, 16 kHz) into one ~14 s reference with 0.35 s gaps.

| Reference | Actor | Clips | Length |
|---|---|---|---|
| fear, male | 1036 (10 recognised, mean intensity 73) | 5 | 13.7 s |
| fear, female | 1029 (10 recognised) | 5 | 14.1 s |
| sad, male | 1059 (8 recognised) | 4 | 13.1 s |
| sad, female | 1028 (11 recognised, mean intensity 71) | 4 | 13.0 s |

Each is registered as a `licensed_recording` asset recording the licence, the source URL,
the paper attribution and the exact clip list.

## Measured (IndexTTS-2, Ming-designed speakers from spike 27)

| Take | Speaker | Emotion reference | Duration | RMS | Median F0 |
|---|---|---|---|---|---|
| Hiding | frightened-whisper voice | creator's whisper | 14.7 s | −25.1 dBFS | 103 Hz |
| Hiding | frightened-whisper voice | CREMA-D fear, male | 11.7 s | −16.2 dBFS | 281 Hz |
| Hiding | frightened-whisper voice | CREMA-D fear, female | 7.7 s | −14.8 dBFS | 334 Hz |
| **Panic** | calm designed voice | **CREMA-D fear, male** | 9.4 s | −15.5 dBFS | 286 Hz |
| Crying | calm designed voice | CREMA-D sad, male | 10.0 s | −21.9 dBFS | 133 Hz |

## Result (the creator's ear)

- **CREMA-D fear works for the panic line.**
- **CREMA-D fear does not work for hiding.** Its fear is *loud* fear, acted near shouting;
  it pulled the whisper out of the hiding line (+9 dB, roughly 2.7× the pitch).
- **CREMA-D sadness (crying) does not work.**
- For quiet fear, the creator's own whisper remains the only working source. CREMA-D
  contains no whispering.

Next source if one is needed: a hired voice actor recording a directed emotion library
under a signed consent release.
