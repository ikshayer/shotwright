# Spike23 recovery review - 2026-11-11

The Windows exit-race correction is implemented, tested and reviewed. A fresh five-second smoke and the complete seven-window joint/audio-only comparison finished successfully. Model descriptions remain provisional and disagree in several intervals.

The previous session's 63-token EOS claim was accurate. Its worker returned a result, but memory_full_info raised AccessDenied during process exit. The helper recorded this as a permanent failure even after all owned identities were absent. That failed run remains unchanged.

The corrected helper accepts only fresh proof that the original PID/create-time identity is gone, replaced or zombie after a sampling denial. Live, inaccessible and unknown identities remain failures. Partial memory rows cannot enter aggregate totals, and benign exit observations are retained separately from failures. Existing model, precision, prompts, EOS settings, seed and resource guards were preserved.

Verification performed this resume:

- 16 independent exit-fault and real Windows launcher/child/sentinel tests, rerun by parent: passed.
- 121 existing CPU, serialization, generation and supervisor checks: passed.
- Ruff on helper/analyzer/new verifier: passed. No new full production pytest run; the earlier 1,744-pass result is historical.
- Fresh supervised smoke: success, 63 tokens ending EOS, empty loading issues, 196 NF4 and 420 unquantized Linear modules, real joint tensors and verified owned cleanup.
- Both seven-window batches: exit0; all14 answers ended EOS below the384-token cap. All primary receipts match worker output and retain correct modality evidence.
- Existing independent receipt verifier:182 passes,0 failures,14 explicit post-exit scope limitations. These do not establish semantic accuracy, repeat inference, independently reload parameters, or rehash payloads in this verification mode. The worker hashes all selected shards before loading.
- Independent source and actual-artifact review: no remaining blocking inconsistency. All owned analyzer identities exited; no further run is queued.

One fresh attempt first failed safely before weights because RAM dropped below6GiB. After the creator freed memory, the next smoke succeeded. The guard was not lowered. No files were deleted, installed environments modified, unrelated applications stopped, or checkpoints downloaded.

## Measured run observations

| Attempt | Supervision time | Minimum available RAM | Peak whole-device GPU |
|---|---:|---:|---:|
| Corrected smoke |135.312s|6.861GiB|9,840MiB|
| Joint seven windows |152.531s|8.154GiB|9,940MiB|
| Audio-only seven windows |242.203s|8.111GiB|9,708MiB|

These include imports, hashing/loading and cleanup. They are observations of this desktop session, not minimum model requirements or per-window inference timings. No actual benign exit-race event occurred in these successful runs; controlled CPU tests exercised that branch.

## Caption-quality finding

These are Qwen-generated descriptions of the same source intervals, not verified listening observations by the orchestrator:

| Source interval | Joint AV description | Audio-only description |
|---|---|---|
|23:17-23:22|Reverberant chant|Singing/chant/choir|
|23:22-23:27|Faint chant|Melodic chant|
|23:27-23:32|Steady hum|Hum, beep, fade|
|23:32-23:37|Low hum|Tone, mechanical sounds, impact|
|23:37-23:42|Audio omitted|Quoted male speech|
|23:42-23:47|Specific speech quotation|Woman speaking; content unclear|
|23:47-23:52|Childlike voice then male shout|Similar vocal contrast and shout|

Agreement is not independent ground truth. The descriptions of religion, machinery, speaker age/gender, intent and precise words are hypotheses. Prompt wording also differs between modes, so disagreements do not isolate visual conditioning as the cause. Do not use the quoted speech as an accepted transcript. Next work is targeted caption-quality validation, particularly23:27-23:47, before using inferred sound sources to author a production scene.

The model receives16kHzmono audio and four video frames per5seconds. Native48kHzstereo source is preserved, but fine stereo/high-frequency detail and fast visual changes are outside this sampled model input's evidence.

## Evidence and source binding

- Corrected helper: `spikes/22_minimax_h3_workflows/local_h3.py`, SHA256 `0ee803b5bc16e133222bc73eecbc16a2179f214af58cd7fe4704f90d230cffb4`.
- Analyzer unchanged: SHA256 `831e776d955284e3be6fc5a7924cfdf8ae11c5223445e15474f4a4abafbb0a3b`.
- Test verifier: `verify_exit_race.py`, SHA256 `3727a583b419221e29533b3a02420b7831bfcf1d85c276e8e4d8c3e350a08e02`.
- Frozen helper/analyzer/contract archives: `_work/exit_race_resume_20261111T1809Z/frozen/`.
- Parent fault tests: `_work/verification/exit_race/20261111T181403Z-d3d44c207e/report.json`.
- Parent121checks: `_work/verification/20261111T181422Z_ab7526a3/report.json`.
- RAM-blocked attempt: `_work/runs/20261111T181503Z_2245bac5/`.
- Successful smoke: `_work/runs/20261111T182233Z_a01748ab/`.
- Joint: `_work/runs/20261111T182718Z_c7f5c9d3/window_01.json` through07.
- Audio-only: `_work/runs/20261111T182954Z_9e46fe2a/window_01.json` through07.
- Raw cross-mode output/commands/source binding and verification: `_work/exit_race_resume_20261111T1809Z/comparison_20261111T182717Z/` (`comparison.json`, `parent_all_receipt_verification.json`, `review_summary.json`).

Everything remains local and analysis-only. Earlier failures and unrelated work are retained. No production approval, commit, push or publication.
