# Longer joint audio/video fit tests

Updated 2026-11-11. The prepared 15-second and 25-second joint tests both completed successfully. This verifies runtime fit for these clips and settings; caption accuracy remains provisional. No analyzer inference remains running or queued.

| Clip | Source interval | Sampled frames | Output tokens | Generation | Supervision total | Peak whole-device GPU | Minimum sampled available RAM |
|---|---|---:|---:|---:|---:|---:|---:|
| 10 s (prior session) | 23:37?23:47 | 8 | 86 | 14.875 s | 216.844 s | 10,668 MiB | 6.639 GiB |
| 15 s | 23:37?23:52 | 12 | 63 | 10.188 s | 106.094 s | 10,888 MiB | 6.626 GiB |
| 25 s | 23:27?23:52 | 20 | 83 | 9.969 s | 128.391 s | 11,728 MiB | 6.505 GiB |

Both new answers ended at EOS151645 below the 384-token cap, with no role continuation. Actual decoded inputs were 360/600 video frames and 240000/400000 mono audio samples at 16 kHz, respectively. Joint tensors were nonempty; input token counts were 1170/1888. Native 48 kHz stereo reference PCM remains retained unchanged. Sampling stayed at0.8fps, with exact 1.25-second selected-frame spacing and zero recorded target errors.

These are five-second telemetry samples, not continuous peak measurements or minimum model requirements. GPU usage includes other applications. The 25-second run had only 217 MiB free GPU memory at its tightest recorded sample. Supervision includes imports, full shard hashing, loading and cleanup; generation timings are individual observations, not a scaling benchmark.

## What changed

The analyzer's explicit duration maximum increased from 15 to 25 seconds after the unchanged-source 15-second run passed. Exactly six literal substitutions changed the function bound/doc/error and CLI bound/help/error. Default 8 seconds, the even frame formula `max(4, 2*ceil(duration*0.4))`, model, quantization, precision, prompts, seed, decoder, EOS/PAD and resource guards stayed unchanged. Main CONTRACT Amendment 8 activates the previously authorized 25-second addendum. No 35-second model test was performed.

Model remains pinned Qwen2.5-Omni-7B Thinker, revision `ae9e1690543ffd5c0221dc27f79834d0294cba00`, NF4 language linears with BF16 audio/vision encoders. Loading diagnostics were empty; audited module names/counts/hashes matched 196 quantized and 420 unquantized linears. Coverage is retained evidence, not independent live encoder-dtype replay.

Current analyzer SHA256: `fb99d5c19c4ad97a1205201750fb79d9dcb02bc5b508f464724e639a9c92972e`. Reused process helper remains `0ee803b5bc16e133222bc73eecbc16a2179f214af58cd7fe4704f90d230cffb4`. Each actual run retained pre/post source hashes and completed without source changes during execution.

## Verification and evidence

- Final medium suite before extension: 46 checks passed, parent report `_work/verification/medium/20261111T234125Z_927824f9af/report.json`.
- Strengthened independent 15-second runtime audit passed before the deliberate source change: `_work/verification/medium/20261111T234539Z_85870cdb06/report.json`.
- Existing CPU/serialization/generation/real supervisor checks: 121 passed, `_work/verification/20261111T234720Z_6ea1c5ff/report.json`.
- Final independent 25-second suite: 43 checks passed, parent report `_work/fit25_20261111/verification/20261111T235153Z_4810430a04/report.json`. Real PyAV decoding and CPU tensors; processor/model wiring uses fakes with explicit CUDA-transfer interception and initialization tripwires. No GPU/model inference in these tests.
- Original25-second runtime audit: 3 checks passed, `_work/fit25_20261111/verification/20261111T235042Z_33b6b5c9a5/report.json`.
- Parent actual worker/primary receipt agreement, hashes, nonempty tensor shapes, resource history and fresh PID/create-time checks: `_work/longer_fit_resume_20261111T234228Z/attempt15/parent_runtime_audit.json` and `attempt25/parent_runtime_audit.json`.
- Scoped Ruff passed for analyzer and both verifier files. An existing unnecessary dict-constructor lint finding in the verifier was corrected after model completion; final 43 checks were rerun. Full repository pytest was not repeated; the earlier 1744 pass/50 skip result is historical.

Independent read-only source review cleared the exact six-substitution extension. Independent actual-artifact review found no acceptance blocker in the25-second run. Both worker trees conclusively exited with no survivors, unverifiable identities or failures; parent fresh checks agreed. Four selected prior-environment fingerprints match their retained before-values. No model/environment install, download, unrelated application stop, source-media mutation, production-state change, deletion, commit or publication occurred.

Primary receipts:

- 15 s: `_work/runs/20261111T234229Z_b9589c00/analysis.json`.
- 25 s: `_work/runs/20261111T234808Z_39d26931/analysis.json`.

Their worker outputs, module coverage, resource JSONL and native logs remain alongside them. Wrapper invocations, logs, source snapshots and pre/post hashes are under `_work/longer_fit_resume_20261111T234228Z/attempt15/` and `attempt25/`. Prepared25-second media and independent native PCM/all-frame/PTS verification remain under `_work/fit25_20261111/`.

## Interpretation limits

Qwen described whispering/shouting and a corridor/figure in both longer clips. The 25-second output also asserted a wall-to-tunnel transition and changing perspective. These are local model interpretations, not parent firsthand listening or independently verified events. The earlier10-second answer omitted voices and described mostly steady ambience. Longer context therefore has not established better semantic accuracy; the clips also differ in endpoint/start, so this is not a controlled quality comparison.

The exact available RAM value at the worker's post-import admission check was not retained. Frozen source enforces 6 GiB before loading; independent periodic samples are separate evidence. Existing admission thresholds 6 GiB RAM/8 GiB GPU free/8 GiB disk and owned-worker stops below 2 GiB for three samples or 900 seconds remained unchanged.

Reference media remains analysis-only and must not enter production generation or export. No creator approval or accepted transcript is implied. The 25-second fit has little observed GPU headroom; 35 seconds remains untested.
