# MiniMax H3 candidate adapter (opt-in)

This adapter generates an audiovisual **candidate** from a project-local video and prompt using the pinned local MiniMax H3 Easy route. It is an opt-in addition to the pipeline. The default image/video routing and production master/degraded outputs are unchanged.

The supported input mode is video plus prompt only. Source audio is not reused: the adapter builds a silent video proxy, generates audio, and writes a native-rate float WAV decoded from the generated MP4's AAC track without gain, normalization, or retiming.

## Enablement and resource policy

H3 is disabled unless `[minimax_h3]` is explicitly enabled with positive operator admission reservations and a positive throughput floor. For example:

```toml
[minimax_h3]
enabled = true
required_vram_gb = 8.0
required_ram_gb = 6.0
required_disk_gb = 8.0
throughput_floor_per_second = 0.0001
startup_allowance_seconds = 600.0
execution_timeout_seconds = 1800.0
wall_clock_seconds = 2400.0
```

The `8 / 6 / 8` reservations and `0.0001` throughput floor above are example operator policy values, not measured model minima, capacity guarantees, or a recommendation for every machine. Configure values for the host and workload. H3 inherits the global job, episode, disk, and wall-clock ceilings; it sets retries per shot to zero. The queue continues to recompute remaining GPU/time budget and account actual spend; reservations declare demand, not available capacity. The `experimental` request flag does not relax admission or runtime guards.

Do not install packages, download weights, or modify the ComfyUI/model environment for this adapter. Runtime, model, helper, controller, and plugin identities are checked against the retained local pins before inference.

## Request rules

- `video_path` must be the project-relative path of a `MASTER` or `DEGRADED` ShotOutput receipt for the requested historical `shot_version`. The file hash must match that receipt. Reference media and unbound files are rejected.
- Supply exactly one of `prompt` or `prompt_file`. `prompt_file` is a UTF-8 file relative to the project root. Its text, including whitespace and line endings after UTF-8 decoding, is preserved; the nonblank prompt limit is 65,536 UTF-8 bytes.
- `expected_version` is the current Shot mutation token used for concurrency control. `shot_version` is the historical Shot content version whose output receipt is used as the source. They are separate values.
- Defaults are 8 seconds, 384x288, 24 fps, and 20 steps. The graph uses the `simple` scheduler, `res_multistep` sampler, and denoise 1.0. The seed is derived from `(shot_id, degrade_seed, "minimax_h3_ambience")` and bounded by the live RandomNoise schema.
- Any non-default duration, dimensions, fps, or steps requires `experimental = true`. The adapter also checks installed schema bounds, exact frame-grid arithmetic, resources, and normal job budgets. Experimental settings are labeled in receipts; they are not treated as tested.

Duration is limited to 0.2 through 30 seconds in 0.1-second increments; dimensions must be multiples of 32 from 32 through 16384; fps is 1 through 120; steps are 1 through 10000. Duration times fps must be an exact integer of at least five frames. These input bounds do not promise that the installed runtime or hardware can complete a setting.

## CLI

Run commands from the active repository worktree. Use the repository's existing Python by absolute path so the command imports this checkout; do not rely on an installed `cctv-pipeline` entry point. Put the prompt in a small project-relative UTF-8 file so it does not travel in a shell argument. `video_path` must be copied from the matching ShotOutput receipt.

```powershell
D:\Work\video_pipeline\.venv\Scripts\python.exe -B -m orchestrator.api.cli --settings settings.toml --project-root projects generate-h3-candidate --project-id apartment --episode-id ep01 --shot-id s1 --data '{"expected_version":3,"shot_version":2,"video_path":"generated/source/master.mp4","prompt_file":"prompts/h3.txt"}' --wait
```

Replace IDs, mutation token, Shot version, and source path with values from the project. The CLI waits by default and returns `job_id`, `run_id`, `av_path`, `wav_path`, and `report_path` after success. H3 `--no-wait` is rejected because the local CLI process owns cleanup. Ctrl+C requests cancellation and waits for cleanup; it exits nonzero.

Other registry commands:

```powershell
D:\Work\video_pipeline\.venv\Scripts\python.exe -B -m orchestrator.api.cli --settings settings.toml --project-root projects list-h3-candidates --project-id apartment --episode-id ep01 --shot-id s1
D:\Work\video_pipeline\.venv\Scripts\python.exe -B -m orchestrator.api.cli --settings settings.toml inspect-h3
D:\Work\video_pipeline\.venv\Scripts\python.exe -B -m orchestrator.api.cli --settings settings.toml --project-root projects cancel-h3-candidate --project-id apartment --episode-id ep01 --shot-id s1 --job-id JOB_ID --data '{"expected_version":3}'
```

`cancel-h3-candidate` acknowledges a cancellation request; the response alone does not prove that cleanup has finished. Candidate listing reports a computed `stale` flag when the current Shot content version differs or the Shot is `STALE`.

## HTTP operations

- `POST /projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/h3-candidates` submits a job and returns HTTP 202 with `job_id`, `state`, and `run_id`.
- `GET /projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/h3-candidates` lists immutable candidate receipts with computed staleness.
- `POST /projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/h3-candidates/{job_id}/cancel` requests cancellation. Include the current `expected_version` in the body.
- `GET /models/minimax-h3` provides model-free configuration/runtime status and tested defaults; it does not load CUDA or model weights.

For generation, the JSON body contains `expected_version`, `shot_version`, `video_path`, exactly one of `prompt`/`prompt_file`, and optional `degrade_seed`, `start_seconds`, `duration_seconds`, `width`, `height`, `fps`, `steps`, and `experimental`. HTTP callers receive the asynchronous job response and can poll the project job operations.

## Artifacts and lifecycle

Successful media and provenance live below `generated/h3/{shot_id}/v{shot_version:04d}/{run_id}/`. The package includes `candidate.mp4`, `audio.wav`, the exact prompt, `report.json`, event/log records, and a portable bundle manifest with relative links. Candidate metadata and a GenerationRun are immutable project-local JSON records; SQLite is only an index/queue. Outputs are not masters, degraded outputs, approved versions, or automatic timeline inputs. H3 does not edit the source Shot, its approval, or Episode approval and does not promote a candidate. Metadata persistence is multiple writes rather than a transaction; a partial metadata failure is recorded in a sidecar diagnostic.

## Pins and verification status

The retained [creator-prompt run](../../../spikes/22_minimax_h3_workflows/CREATOR_PROMPT_RUN.md), [local model catalog](../../../spikes/22_minimax_h3_workflows/LOCAL_MODEL_CATALOG.json), and [model documentation receipt](../../../spikes/22_minimax_h3_workflows/LOCAL_MODEL_DOCUMENTATION.json) record the evidence and pins. The linked creator-prompt run is a previously accepted Easy-route feasibility run. It does not verify this production CLI/API path, establish a capacity envelope or VRAM minimum, or constitute production approval.

Reported focused verification currently includes 75 production/API tests, 52 core tests, and 8 shared-slot tests. These are not a native GPU run. The full repository suite and a bounded native inference through the new CLI/API operation remain pending. No model-capacity, throughput, or experimental-setting claim is made here.
