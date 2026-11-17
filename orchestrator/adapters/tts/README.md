# Text-to-speech adapters

Two engines, because one cannot serve both voice classes (PRD FR-08, spike 05). Both are
**synthesis only**: each operation writes one WAV take and registers it as a voice `Asset`.
There is no mixing, loudness normalisation, resampling, `PerformanceEdit` or
`AudioPostProfile` here. Attaching a take to a Shot is the agent's job: add the asset id to
the Shot's `audio_refs` with `revise_shot`. The contract is `CONTRACT.md`.

| | Piper | IndexTTS-2 |
|---|---|---|
| For | processed-source voice (PA, answering machine, broadcast): flat delivery is correct | diegetic voice (someone the recording caught speaking): prosody is the point |
| Runs on | CPU, never takes the GPU lease | GPU, under the lease, budget and watchdog |
| Operation | `generate_piper_speech` | `generate_indextts2_speech` |
| Inputs | a catalog `voice_id`, optional `length_scale`, `sentence_silence_seconds` | a consented `speaker_asset_id` and an `emotion` (`none`, `vector`, `text`, `reference`) |
| Deterministic | yes for a given text/voice/settings | **no**, there is no seed |

`inspect_tts` (`GET /models/tts`) reports presence only: it starts no process, imports no
torch or onnxruntime and hashes nothing.

## Directing emotion (IndexTTS-2)

- **`reference`** — the most direct control: record yourself (or anyone who consents)
  performing the emotion, register the `.wav` with `voice_consent`, and pass it as
  `emotion_asset_id`. The voice stays the speaker clip's; only the delivery is borrowed.
  Any words will do. IndexTTS-2 reads at most the first 15 s. `strength` (0–1) scales it;
  lower it if the performer's own voice character starts to leak into the take.
- **`vector`** — eight weights. Whatever they leave below a sum of 1 is filled with the
  speaker clip's own delivery, so with a flat speaker clip (any Piper take) use weights summing
  to about 0.9–1.0, e.g. `afraid 0.7, calm 0.2` (spike 05).
- **`text`** — a description such as `"terrified, voice shaking"`.

A raw take is only part of a performance: spike 05's approved result also needed a
`PerformanceEdit` (widened pauses, breaths) and an audio degradation profile (FR-32), which
are not built yet.

## Settings

Both sections are disabled by default. A disabled engine refuses its operation, naming the
section.

```toml
[piper]
enabled = false
timeout_seconds = 120.0            # wall clock for one synthesis subprocess

[indextts2]
enabled = false
required_vram_gb = 8.0             # required when enabled; strict finite positive numbers
required_ram_gb = 12.0
required_disk_gb = 1.0
startup_allowance_seconds = 300.0  # model load
execution_timeout_seconds = 900.0  # generation, counted from the end of the load
use_fp16 = true
```

**The numbers above are example operator policy, not measured minima.** No VRAM figure for
IndexTTS-2 exists. The only measurement is spike 05 Finding 5: model load failed with 6.0 GB
of physical RAM free and succeeded with 13.6 GB free; model load took 42-53 s. Every run
writes `torch.cuda.max_memory_allocated()` / `max_memory_reserved()` into its `report.json`
and the job's `resource_metrics`, so the first real runs measure it.

IndexTTS-2 jobs run with zero automatic retries. Their hard wall clock is
`startup_allowance_seconds + execution_timeout_seconds`; the adapter enforces the two
allowances separately and the lease/watchdog backstops the sum.

## Runtime layout

Fixed and repository-relative (not settings), under the gitignored `spikes/05_voice/_work/`.
The adapters never install, download or modify anything there.

| Item | Path |
|---|---|
| Piper interpreter | `piper_env/Scripts/python.exe` |
| Piper voices | `<voice_id>.onnx` and `<voice_id>.onnx.json` |
| IndexTTS-2 repo | `index-tts` (git commit `d9e41aac89fd00b3d71497fddb287b7f24613712`, checked before every run) |
| IndexTTS-2 interpreter | `index-tts/.venv/Scripts/python.exe` |
| IndexTTS-2 checkpoints | `index-tts/checkpoints` (`config.yaml` SHA-256 `ea9c2815...6539081`, checked before every run) |

### Rebuilding the Piper environment

```powershell
py -3.11 -m venv spikes\05_voice\_work\piper_env
spikes\05_voice\_work\piper_env\Scripts\python.exe -m pip install -r orchestrator\adapters\tts\piper_requirements.txt
```

Voices are downloaded separately and only become usable once they are in
`piper_voices.json`. The `.onnx.json` files carry no licence, so a voice is added only with a
licence verified from its own model card (FR-16). Before every run the adapter verifies the
voice's `.onnx` size and SHA-256 and the `.onnx.json` SHA-256 against the catalog.

Both catalogued voices are **non-commercial** by their model cards: `en_US-ryan-high` (male)
is CC BY-NC-SA 4.0 and `en_US-lessac-medium` (female) is under a research-only Blizzard 2013
licence. They are allowed for internal evaluation only: each take records
`reuse_scope = "internal evaluation only"`, which episode credits report as a publication
blocker, and an IndexTTS-2 take cloned from such a take inherits it. A publishable episode
needs a commercially licensed voice, verified from its card, added to the catalog.

## IndexTTS-2 supervision

Each job spawns one `indextts2_worker.py` process (`-B -I`, cwd = the IndexTTS-2 repo,
`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`). The worker never calls
`ensure_models_available`; every auxiliary model is passed as an explicit local path and a
missing one is an error, not a download. `python indextts2_worker.py --check <repo>` imports the
runtime and prints versions without loading a model.

The GPU lease needs a stable worker identity before any process exists, so
`IndexTTS2WorkerControl` reports this process's pid (as H3 does) and its `terminate()` /
`unload()` kill the current attempt's process tree. The watchdog deadline, the startup
allowance, the execution timeout and job cancellation all end the process.

## Voice consent (FR-16)

Voice identity needs consent, not merely a licence; the two are recorded separately on the
`Asset` as `voice_consent` (`synthetic`, `creator_own_voice`, `licensed_for_cloning`) and
`voice_consent_basis` (who consented and how, or which licence permits cloning; required for
every value except `synthetic`).

- An IndexTTS-2 `speaker_asset_id` must be an Asset of type `voice` or `audio`, a `.wav`
  whose bytes match its registered checksum, with `voice_consent` set. Anything else is refused
  before a job is submitted.
- A Piper take is `synthetic` and is therefore a valid speaker reference.
- A human reference clip enters through `create_asset` (multipart: HTTP or the CLI, not MCP):
  `.wav` files starting `RIFF....WAVE`, stored under `assets/audio/{asset_id}/`, limited by
  `[upload] max_audio_bytes` (default 50 MiB). A `type=voice` upload without `voice_consent` is
  refused.
- An IndexTTS-2 take inherits the consent and basis of its speaker reference.

## Files and provenance

```
generated/tts/{shot_id}/v{shot_version:04d}/{run_id}/
    take.wav  text.txt  report.json  logs/stderr.txt  logs/stdout.txt (IndexTTS-2 only)
```

`report.json` records the engine and version, the voice or speaker identity, the request
settings, `text_sha256`, the output `sha256`, `sample_rate`, `channels`, `duration_seconds` and
timings (and for IndexTTS-2 the VRAM figures and `"deterministic": false`). A `GenerationRun`
is saved for every run that reaches an adapter; a failed or cancelled run leaves no `take.wav`
and no Asset. The take is registered in place as `tts-{run_id}`. Native sample rate and
channels are kept.

## Open decision: licence classification

`credits.py` treats any licence other than CC0-1.0 or CC-BY-4.0 as a publication blocker.
Until the creator decides how generated speech is classified, takes record
`license = "Generated speech; rights classification pending creator decision"`, which the
credits check reports as a blocker. That is deliberate: it keeps the question visible.
