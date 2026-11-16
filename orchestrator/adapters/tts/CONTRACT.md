# Contract — text-to-speech adapters: Piper (CPU) and IndexTTS-2 (GPU)

Drafted 2026-11-16, before code, so that the implementer and a blind test author work
from the same statement. Where this file and the PRD disagree, the PRD wins; say so.

## 0. Why, and what is out of scope

PRD FR-08 names two voice classes because one engine cannot serve both (spike 05):

- **Processed-source voice** (PA, answering machine, broadcast): flat delivery is correct.
  **Piper 1.8.0**, CPU only, never takes the GPU lease.
- **Diegetic voice** (someone the recording caught speaking): prosody is the point.
  **IndexTTS-2**, GPU, separates speaker timbre (a reference clip) from emotion.

This contract adds **synthesis only**: each operation writes one WAV take and registers
it as a voice `Asset` with full provenance. Out of scope, and must not be started:
mixing, `PerformanceEdit`, `AudioPostProfile` (FR-32), loudness normalisation,
resampling, attaching takes to a Shot (the agent does that with the existing
`revise_shot`, by adding the asset id to `audio_refs`), and `emo_audio_prompt` (untested,
spike 05 "Still open").

## 1. Runtime layout (fixed, repository-relative, like H3)

Both runtimes live under the gitignored `spikes/05_voice/_work/`. Paths are resolved from
the repository root, exactly as `orchestrator/adapters/minimax_h3/native.py` does, and are
not settings.

| Item | Path |
|---|---|
| Piper interpreter | `spikes/05_voice/_work/piper_env/Scripts/python.exe` (a venv built from `orchestrator/adapters/tts/piper_requirements.txt`) |
| Piper voices | `spikes/05_voice/_work/<voice_id>.onnx` and `<voice_id>.onnx.json` |
| IndexTTS-2 repo | `spikes/05_voice/_work/index-tts` (git commit `d9e41aac89fd00b3d71497fddb287b7f24613712`) |
| IndexTTS-2 interpreter | `spikes/05_voice/_work/index-tts/.venv/Scripts/python.exe` |
| IndexTTS-2 checkpoints | `spikes/05_voice/_work/index-tts/checkpoints` (`config.yaml` SHA-256 `ea9c2815ecc3874577c7ac158b97248c027250e5b05972fbbb1216b6d6539081`) |

The adapters must never install, download, or modify anything in these trees.

### 1.1 Piper voice catalog

A committed catalog, `orchestrator/adapters/tts/piper_voices.json`, is the only source of
allowed voices. Each entry: `voice_id`, `gender`, `license`, `license_source`, `onnx_sha256`,
`onnx_bytes`, `config_sha256`, `sample_rate`. It starts with exactly one voice:

| voice_id | gender | license | onnx SHA-256 | onnx bytes | config SHA-256 | rate |
|---|---|---|---|---|---|---|
| `en_US-ryan-high` | male | `MIT` | `b3990d7606e183ec8dbfba70a4607074f162de1a0c412e0180d1ff60bb154eca` | 120786792 | `c6d3b98f08315cb4bebf0d49d50fc4ff491b503c64b940cd3d5ca28543b48011` | 22050 |

`license_source` for it is `"PRD v1.2 §8.4, measured 2026-11-04 (spike 05)"`. Voices are
added only with a licence verified from the voice's own model card text (FR-16); the
`.onnx.json` files carry no licence field, so it cannot be read from them. `en_US-lessac-medium`
is present on disk but is **not** in the catalog: its licence is unverified, and it is female
(voice gender is a recorded property, never assumed — §8.4).

Before every Piper run the adapter verifies the chosen voice's `.onnx` size and SHA-256 and
the `.onnx.json` SHA-256 against the catalog, and refuses on any mismatch.

## 2. Settings

Two new top-level sections in `orchestrator/platform/settings.py`, both `_Section`
subclasses (so unknown keys are rejected), both **disabled by default**:

```toml
[piper]
enabled = false
timeout_seconds = 120.0          # > 0; wall clock for one synthesis subprocess

[indextts2]
enabled = false
required_vram_gb = 8.0           # required when enabled; strict finite positive numbers
required_ram_gb = 12.0
required_disk_gb = 1.0
startup_allowance_seconds = 300.0  # > 0; model load (measured 42-53 s, spike 05)
execution_timeout_seconds = 900.0  # > 0
use_fp16 = true
```

- `[indextts2]` follows `MinimaxH3Settings` exactly: when `enabled`, every reservation must
  be set; reservations reject booleans, strings, non-finite and non-positive values.
- The numbers above are **example operator policy, not measured minima**. No VRAM figure
  for IndexTTS-2 exists; the only measured fact is spike 05 Finding 5 (load failed with
  6.0 GB physical RAM free, succeeded with 13.6 GB free). The README must say so.
- A disabled engine refuses its operation with an error that names the settings section,
  the same way a disabled `[minimax_h3]` does.

## 3. Operations

Added to `OPERATIONS` in `orchestrator/api/routes.py`. HTTP, CLI and MCP then follow
automatically. Both generation operations join `ASYNC_ADAPTER_OPERATIONS` (HTTP 202).

| name | method | path | async |
|---|---|---|---|
| `inspect_tts` | GET | `/models/tts` | no |
| `generate_piper_speech` | POST | `/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/piper` | yes |
| `generate_indextts2_speech` | POST | `/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/indextts2` | yes |

Summaries must say enough for an agent to choose correctly, e.g. *"Synthesise a
processed-source voice take (PA, answering machine, broadcast) with Piper on the CPU"* and
*"Synthesise a diegetic voice take with IndexTTS-2 on the GPU, cloning a consented speaker
reference and steering emotion"*.

### 3.1 `inspect_tts`

Model-free, like `inspect_h3`: it must not import torch or onnxruntime, start either
runtime, or hash large files. It returns, per engine: `enabled`, `runtime_present`
(interpreter file exists), and for Piper the catalog voices each with `present`; for
IndexTTS-2 `checkpoints_present` (config.yaml exists). Plus a `limitations` list.

### 3.2 Common request rules (both generation bodies)

- `extra="forbid"`; pydantic models declared next to the service, as `H3GenerateBody` is.
- `expected_version`: the Shot's current mutation token (`StrictInt`). A stale value is
  refused exactly as `generate_h3_candidate` refuses it.
- **Exactly one** of `text` or `text_file`. `text_file` is a project-relative UTF-8 file
  read through `ProjectRoot` (containment applies). The text is used byte-for-byte after
  UTF-8 decoding; whitespace and line endings are preserved, not normalised.
- The text must contain a non-whitespace character and be at most **8,192 UTF-8 bytes**.
- Validation happens synchronously, before a job is submitted; an invalid request creates
  no job and writes no files.

### 3.3 `generate_piper_speech` body

| field | type | rule |
|---|---|---|
| `expected_version` | int | see 3.2 |
| `text` / `text_file` | str | see 3.2 |
| `voice_id` | str | must be a catalog `voice_id` |
| `length_scale` | float, optional | 0.5 to 2.0 inclusive; omitted means the voice default |
| `sentence_silence_seconds` | float, optional | 0.0 to 2.0 inclusive |

### 3.4 `generate_indextts2_speech` body

| field | type | rule |
|---|---|---|
| `expected_version` | int | see 3.2 |
| `text` / `text_file` | str | see 3.2 |
| `speaker_asset_id` | str | a registered voice reference, section 5.2 |
| `emotion` | object | discriminated on `mode`, below |

`emotion` is one of:

- `{"mode": "none"}`
- `{"mode": "vector", "happy": f, "angry": f, "sad": f, "afraid": f, "disgusted": f,
  "melancholic": f, "surprised": f, "calm": f}` — all eight keys required, each finite and
  in [0.0, 1.0], at least one greater than 0. Passed to IndexTTS-2 as `emo_vector` in
  exactly that order.
- `{"mode": "text", "description": str}` — non-blank, at most 500 characters. Passed as
  `use_emo_text=True, emo_text=description`.

No seed field: IndexTTS-2 sampling is not deterministic, and the report must say so.

## 4. Execution

### 4.1 Jobs

- Both operations bind the job to the Shot (budgets and the job journal key on the Shot),
  through the existing `JobRunner`. Two new `AdapterKind` members: `TTS_PIPER` and
  `TTS_INDEXTTS2`.
- `TTS_PIPER` is a **CPU** kind: `preflight_cpu`, no GPU lease, never under the GPU lock.
- `TTS_INDEXTTS2` is a **GPU** kind: `queue.run` with the lease, budget checks and
  watchdog, reservations taken from `[indextts2]`, retries per shot **zero** (no automatic
  retries, same as H3).
- Neither modifies the Shot, its status, its approval, or any `ShotOutput`. They behave
  like H3 candidates in that respect: a take is an input the agent may choose to attach.

### 4.2 Piper subprocess

- argv list, never a shell string: `[piper_python, "-I", "-m", "piper", "-m", <onnx>,
  "-c", <onnx.json>, "-f", <tmp wav>]` plus `--length-scale` / `--sentence-silence` when
  given. The text goes in on **stdin** as UTF-8, never as an argument.
- Timeout from `[piper].timeout_seconds`; on timeout or cancel the process is killed and
  the job fails with the timeout or cancelled error kind.

### 4.3 IndexTTS-2 subprocess

- One subprocess per job: `[indextts_python, "-B", "-I", <helper>, <request.json>]` with
  cwd set to the IndexTTS-2 repo. The helper is a committed, stdlib-plus-`indextts` script,
  `orchestrator/adapters/tts/indextts2_worker.py`; it must not import `orchestrator`.
- Environment adds `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`. All auxiliary models
  load from `checkpoints/` (local folders with `local_files_only=True`); a run that would
  need the network must fail, never download (FR-16).
- The helper constructs `IndexTTS2(cfg_path=..., model_dir=..., use_fp16=<setting>)`,
  calls `infer(spk_audio_prompt=<reference wav>, text=..., output_path=<tmp wav>, ...)`,
  and writes a JSON result file: load seconds, generation seconds, and
  `torch.cuda.max_memory_allocated()` / `max_memory_reserved()` in bytes. These are the
  first VRAM measurements of IndexTTS-2 in this project; they go into the report.
- The process must be killable by the lease and watchdog: whatever worker object the GPU
  lease requires must expose this subprocess (its pid, terminate), so cancellation, the
  startup allowance and the execution timeout all end the process and release the GPU.
  The implementer reads `orchestrator/jobs/lease.py` and the H3 worker to satisfy this.

### 4.4 Output validation (both)

The temporary WAV is opened with the stdlib `wave` module and must be PCM, 1 or 2
channels, a positive sample rate, and more than zero frames. Only then is it renamed to
its final name. Native sample rate and channels are kept: no resampling, no loudness
change (FR-08's -18 LUFS target applies to the final mix, later).

## 5. Provenance, consent and registration

### 5.1 Files

A successful run writes, through `ProjectRoot` only:

```
generated/tts/{shot_id}/v{shot_version:04d}/{run_id}/
    take.wav
    text.txt          the exact text, UTF-8
    report.json
    logs/stderr.txt   the subprocess stderr
```

`report.json` records at least: `engine` (`piper` | `indextts2`), engine version (Piper's
`piper-tts` distribution version; IndexTTS-2's git commit and `config.yaml` SHA-256), voice
or speaker identity (Piper: catalog entry and verified hashes; IndexTTS-2:
`speaker_asset_id`, its checksum, its `voice_consent`), the request settings, `text_sha256`,
the output's `sha256`, `sample_rate`, `channels`, `duration_seconds`, timings, and for
IndexTTS-2 the VRAM figures from 4.3 and `"deterministic": false`. A `GenerationRun` is
saved for every run, successful or not; a failed or cancelled run leaves no `take.wav`
and no registered Asset. *(Amended 2026-11-16.)* A job cancelled before dispatch never
started a run, so it has no `GenerationRun`; the job journal records its cancellation. A
cancel that arrives as a run completes yields exactly one outcome: completed with its
Asset, or cancelled with neither Asset nor `take.wav`.

### 5.2 Voice consent on Assets (FR-16)

FR-16: *"Voice identity requires consent, not merely a licence ... the two are recorded
separately on the asset."* Two optional fields are added to `Asset`:

- `voice_consent`: `"synthetic"` | `"creator_own_voice"` | `"licensed_for_cloning"` | `None`
- `voice_consent_basis`: free text — who consented, how, when, or which licence permits
  cloning. Required, non-blank, whenever `voice_consent` is set to a non-`synthetic` value.

Both default to `None`, so existing registry records still load (FR-28).

An IndexTTS-2 `speaker_asset_id` is accepted only if the Asset exists, its `type` is
`voice` or `audio`, its file is a `.wav` whose bytes match its registered checksum, and
`voice_consent` is one of the three values above. Anything else is refused before a job is
submitted, with a message that names the missing consent.

### 5.3 Registering a take

On success the take is registered as an `Asset` in place (no copy):

| field | Piper | IndexTTS-2 |
|---|---|---|
| `asset_id` | `tts-{run_id}` | `tts-{run_id}` |
| `type` | `voice` | `voice` |
| `generated` | `True` | `True` |
| `provider` | `piper` | `indextts2` |
| `file_path` / `checksum` | the take | the take |
| `model_or_voice_license` | the catalog voice licence | `"bilibili Model Use License (IndexTTS-2; royalty-free below 100M MAU / RMB 1bn)"` |
| `voice_consent` | `synthetic` | inherited from the speaker reference |
| `voice_consent_basis` | `None` | inherited from the speaker reference |
| `license` | see below | see below |
| `reuse_scope` | `project` | `project` |

**`license` is an open creator decision, not settled here.** `credits.py` treats any
licence other than CC0-1.0 / CC-BY-4.0 as a publication blocker. Until the creator decides
how generated speech is classified, takes record `license = "Generated speech; rights
classification pending creator decision"`, which the credits check will report as a
blocker. That is deliberate: it keeps the question visible instead of guessing.

A Piper take is a valid IndexTTS-2 speaker reference (it is `synthetic`), which is how spike
05 avoided cloning any real person.

### 5.4 Uploading a human reference clip

`create_asset` (the multipart upload) gains audio: `.wav` files whose bytes start with
`RIFF....WAVE`, `type` `voice` or `audio`, stored under `assets/audio/{asset_id}/`, with
their own size limit in `UploadLimits`. The registration form accepts `voice_consent` and
`voice_consent_basis` with the rule in 5.2; a `type=voice` upload without `voice_consent` is
refused. *(Amended 2026-11-16.)* `source_url` is not required when `voice_consent` is
`creator_own_voice`: the recording is the creator's own act and has no URL, and the basis
records its provenance. Every other non-generated asset still requires it. TTS failures map
to their own error codes (`validation_error` with `field="speech_request"`, 422, and
`tts_setup_unavailable`, 503), not H3's. That `field` applies to semantic refusals made by
the TTS service (a missing `text_file`, missing consent, an unknown speaker); a body the
request schema itself rejects (blank text, unknown keys, wrong types) gets the shared
request-validation envelope that every operation uses. Multipart operations are not MCP tools, so a human voice reference can only enter
through HTTP or the CLI. That is acceptable: it is the creator's act.

## 6. Errors

Use the existing `JobError` taxonomy and the H3 error mapping as the model: validation
failures are client errors before any job exists; a missing runtime or catalog mismatch is
a setup error naming the path; timeouts, cancellation and subprocess failures are job
failures recorded on the `GenerationRun`. Subprocess stderr goes to the log file, and the
error message carries its last few lines.

## 7. Documentation

`orchestrator/adapters/tts/README.md`: what each engine is for, the settings block with
the "example policy, not measured minima" warning, the runtime layout, how the Piper env
is rebuilt from `piper_requirements.txt`, the consent rules, and the open licence decision.

## 8. Rules for the test author

- Test against this contract and the existing public surfaces only. Do not read the
  implementation.
- No test may start the real IndexTTS-2. One Piper test may run the real runtime, and must
  skip itself cleanly when `spikes/05_voice/_work/piper_env` or the catalog voice is
  absent.
- Fake the subprocess boundary (injectable `popen`, as the PostProfile and H3 tests do).
- Report contract ambiguities as findings; never resolve them by guessing in a test.

## 9. The implementer must report

Every place the code had to differ from this contract and why; what in the lease/worker
protocol was awkward for a per-job subprocess; anything left unimplemented.
