# Local Qwen adapter ready (2026-11-12)

The tested analyzer is now wrapped by `python -B -m orchestrator.adapters.qwen analyze|review|inspect`. User defaults:25-second overlapping windows,2-second minimum overlap,500tokens; generated-audio review requires an intent brief. Read [adapter README](../../orchestrator/adapters/qwen/README.md) and [validation evidence](../../orchestrator/adapters/qwen/VALIDATION.md).

89 independent adapter tests and full repository1833tests passed (50skipped); 121 existing native CPU/supervision checks passed. Actual35s reference and8s generatedreview CLI runs completed, all413/383/320tokens endedEOS with verified cleanup. Parent PCM/frame/source/raw-output audits passed. Reports remain advisory: veryquiet generated audio still received optimistic Qwen feedback, so semantic QA is not certified. No active inference remains and no production approvals changed. Earlier experiments below remain historical.

---

# Explicit detail request and500-token cap update

The revised prompt requested400?500tokens; Qwen returned450tokensEOS below500. Moretext included unsupported male/femaleconversation and finalclosed-door/zoom claims; accuracy remains unverified. See [DETAIL_500_TOKEN_RUN.md](DETAIL_500_TOKEN_RUN.md) for complete rawresponse, matchedconfiguration and cleanup/memory evidence. All workers exited; no inference remains queued.

---

# Annotation-guided probe update

One neutral focused25s probe returned266tokensEOS and cleaned up correctly, but Qwen claimed clear/undistorted speech and smoothcamera motion, contradicting creator annotations and previous model outputs. It supplied no dialogue quotation or specific entrance/head-change account. See [ANNOTATION_PROBE.md](ANNOTATION_PROBE.md) for full rawresponse and evidence. Creator observations remain separately attributed; finecamera shake unresolved at.8fps. No further inference remains active.

---

# Detailed25-second context update

The same detailed prompt on25seconds returned277tokensEOS below384. Qwen again reports a figure and movement, plus static/muffledvoices, while still omitting an explicitshout and uncertaintiessection. Claims remain unverified. See [DETAILED_25S_CONTEXT.md](DETAILED_25S_CONTEXT.md) for complete rawresponse, measuredresources, cleanworkerexit and the legacyvalidator custom-prompt limitation. No inference remains active.

---

# Prompt comparison update: detailed15s response retained

See [PROMPT_COMPARISON.md](PROMPT_COMPARISON.md) for the complete raw answer and controlled comparison. Output increased63->212tokens and endedEOS below384, but Qwen contradicted baseline shout/approaching-figure claims and omitted the requested uncertainties section. Added detail remains unverified. Identical media/configuration and clean worker exit were independently checked. No inference remains active. This supersedes older next-work statuses while preserving the completed longer-fit results below.

---

# Longer fit update, 2026-11-11: 15s and25s verified

Both prepared longer joint tests passed. See [LONGER_CLIP_FIT.md](LONGER_CLIP_FIT.md) for results and evidence.25s used20frames,83tokensEOS and peaked at11728MiB whole-device GPU, with only217MiB free at the tightest sample. Cleanup and runtime evidence passed; semantic accuracy remains unverified. Analyzer maximum is now25s, default8 unchanged. No35s inference ran and no analyzer job remains active. This update supersedes older pending statuses below.

---

# Medium fit update, 2026-11-11: session wrapped

See latest HANDOFF top block for exact evidence and continuation. Actual supervised10s joint run `20261111T233008Z_9d462e15` passed8frames/.8fps,86tokensEOS,emptyloading,196/420coverage andcleanownedcleanup. Wall216.844s,generation14.875s,minsampledRAM6.639GiB,wholeGPUpeak10668MiB. Parent actualreceipt audit `_work/medium_fit_20261111/parent_10s_runtime_audit.json`.

15s and25s media are prepared and independently PCM/framehash/PTS verified; **NO15/25 model runs yet**. Analyzer stillmax15/default8, SHA3ab759d9?507ffb9. Pending25authorized addendum `_work/fit25_20261111/CONTRACT_25.md` notimplemented/maincontractnotappended. Creator requested handoff; no further inference queued.

Existing121checks passed. Corrected independent medium46checks passed parent report `_work/verification/medium/20261111T233418Z_32e8652439/report.json`. Previous four fake-processorCPU-only claims were withdrawn and harnesscorrected; archive correction underfit25/verifier_archive. Finalshared-verifier additions andnewblindverify_fit25.py were onlysyntaxparsed AFTERthosepasses: review/rerunfinalfilesnextsession. No fullproductionpytest thissession. Semanticaccuracy remainsunverified; longeroutputomitteddetails,notprovenbetter. Olderfindingsbelow retainedhistory.

---

# Resume2026-11-11 complete: runtime recovered, caption quality provisional

See [RECOVERY_REVIEW.md](RECOVERY_REVIEW.md) for current results/evidence. Pending Windowsmemorysampling exit-race fix is implemented and independently reviewed; parent16fault/realWindowschecks and121existingCPU/serialization/generation/supervisorchecks passed. After one safeRAM-admission failure and creator memoryrelease, five-second smoke succeeded, then all7joint+7audio-only windows completed atEOS withcleanloading/tensors/cleanup. Parent actualreceipt verification:182pass,0fail,14explicitpostexitlimitations. No productionpytest rerun and no firsthandlistening/semanticapproval.

The two modes disagree on sounds/dialogue in23:27-23:47. Agreement elsewhere is stillsame-model inference, not groundtruth. Next is targeted caption-quality validation, not an automaticrerun or newdownload. No analyzer processes remain. All oldfailures retained; originalanalyzer/model/guards unchanged. Detailed frozenhashes, runIDs, timings and semanticcaveats are in RECOVERY_REVIEW and latestHANDOFF. Older status below is retained history.

---

# Local joint audio/video analyzer

Status, 2026-11-11: isolated setup and compatibility checks are complete. **Full model loading and semantic analysis are not yet verified.** The first model load failed with Windows error1455 before loading quantized weights. A scoped allocator experiment has passed independent CPU tests; its full-load retry awaits sufficient available RAM.

## Scope and installed runtime

Work is confined to the `api-conformance` worktree. Existing environments, ComfyUI workflows and production state are preserved. There is no cloud audio/video inference, reference export, production generation, commit or publication.

The model is Qwen2.5-Omni-7B **Thinker only**, revision `ae9e1690543ffd5c0221dc27f79834d0294cba00`. Its actual Apache-2.0 licence is retained. All 1,346 Thinker tensor names are preserved in a derived index; native Transformers conversion handles their prefixes. Four original safetensors shards total 19,937,539,032 bytes. Fourteen acquired model/metadata files total 19,953,432,708 bytes. Their immutable source identities and observed hashes are retained under `_work/model/`.

A new `_work/env` contains Torch 2.11.0+cu130, Transformers 5.22.2, bitsandbytes 0.50.2, Accelerate 1.13.0 and PyAV 17.0.0. Installation used official wheel sources, a complete version/SHA lock and offline wheelhouse installation. Native Torch/vision wheels were reused read-only after hash verification. `pip check` passed. See `_work/install_receipt_20261111T073224642188Z.json`. No prior environment was installed into.

## Actual verification

| Check | Result | Limit |
|---|---|---|
| Existing repository suite | 1,744 passed, 50 skipped, 6 warnings; 433.51 s | No production source changed in this task |
| Pure contract checks | 26 passed; parent reran | CPU only |
| Full model artifact verification | 23 passed; all four full hashes and all 1,346 tensor headers/mappings | Does not load the model |
| Corrected media | 17 hash/PCM checks and seven frame checks passed | YouTube-derived, re-encoded analysis video |
| Decoder | Six tests passed in the actual isolated environment | No model or GPU |
| Native Omni processor | Joint and audio-only inputs passed | No model or GPU |
| CUDA kernel probe | BF16 matmul, NF4 forward and functional NF4 dequantization passed | Tiny kernels, not full-model fit or quality |
| Actual process lifecycle | Low-RAM and deadline branches passed in the isolated environment | Worker argv, policy clock and GPU query substituted; real processes/RSS/cleanup/sentinel used |
| Scoped warmup experiment | 18 independent CPU checks passed; parent reran | Full-load effect unverified |

Whole `orchestrator/`, `tests/` and spike source Ruff checks passed. `git diff --check` passed; Git emitted existing line-ending notices. The full repository suite was not repeated after spike-only changes.

Key evidence:

- `_work/parent_validation/`: actual repository test log.
- `_work/verification/20261111T075200Z_ee67ccc2/report.json`: complete model artifact checks.
- `_work/review_cpu/20261111T073815Z_ff524a5a/`: retained native processor harness, results, flags and hashes.
- `_work/verification/20261111T075241Z_93672f02/report.json`: real isolated-environment supervisor lifecycle checks.
- `_work/probe_receipt_20261111T073349746413Z.json`: actual CUDA/NF4 probe.
- `_work/warmup_verify_20261111T163421Z/` and `_work/parent_warmup_verify_20261111T163851Z/`: independent tests and parent rerun.

## Reference and preprocessing

The reference is **The Mandela Catalogue Vol.333**, upload `1jMFrVk2d_I`, source interval [1397,1432) seconds (23:17?23:52). This is different from repository REF-001/Vol.6. The original verified 48 kHz stereo float WAV remains unchanged. Full aligned PCM and all seven five-second slices match it byte for byte.

Use `_work/media/windows_verified_20261111T/window_01.mkv` through `window_07.mkv`. The retained `_work/media/windows/` files are the failed initial attempt and must not be analyzed. The full clip has 840 frames; each corrected window has 120 frames at 24 fps, timestamps 0?4.958 s. Maximum original/aligned timestamp difference was 0.000333 s. The corrected receipt is `_work/media/receipts/media_verification_corrected.json`; the ready batch manifest is `_work/media/verified_windows_manifest.json`.

PyAV explicitly decodes real embedded audio and video. Native Omni receives 16 kHz mono audio plus four frames sampled at 0, 1.25, 2.5 and 3.75 s. Joint processing uses `use_audio_in_video=True`; audio-only contains no video tensors. Actual joint input had 419 tokens, video patches [936,1176], grid [1,3], temporal spacing 2.5 s, features [1,128,30000], and audio-mask sum 500. All float inputs were finite. The recorded processor rerun took 2.359 s excluding imports.

The built-in embedded-audio helper needs TorchCodec/shared FFmpeg DLLs; this deliberately selected PyAV route avoids that dependency. Four-frame sampling can miss fast visual events. Framewise brightness and native waveform measurements are retained under `_work/media/signal_metrics/`; they measure timing/energy, not semantic sound sources. The analyzer downmixes/resamples its input; retained native source PCM is unchanged.

## First full-load attempt and candidate fix

Attempt `_work/runs/20261111T075452Z_11d9047d/` failed after 69.25 s at native `safe_open` with Windows error1455: paging-file/commit exhaustion. It produced **no semantic output**. Baseline/peak/post device memory was 2,251/2,388/2,251 MiB; peak aggregate worker RSS was 973,246,464 bytes. These describe a failed pre-weight-load attempt, not model requirements. Cleanup verified all three captured identities stopped and no survivors remained.

Installed Transformers already selects `backend=pread` on Windows. Safetensors 0.8.0 still briefly maps a whole shard copy-on-write while parsing its metadata, before returning its pread-backed file handle. A CPU probe opened all four files individually and simultaneously, read a 2.5 KiB BF16 norm, and kept process RSS near 0.5 GiB. This did not reproduce the full-loader error. See `_work/reader_probe_20261111T162552Z/contained_parent_rerun.json`.

The next explicit experiment disables only `transformers.modeling_utils.caching_allocator_warmup` during the private worker's one `from_pretrained` call. The exact original function is restored in `finally`. The policy is recorded in receipts. Independent normal/exception/nested restoration tests and full AST comparison passed: model, NF4/BF16, device map, SDPA, modalities, seed and resource limits are unchanged. **This is not yet a proven fix.** No installed library or system paging setting was changed.

Sources supporting this investigation: [safetensors 0.8.0 reader](https://github.com/huggingface/safetensors/blob/v0.8.0/bindings/python/src/lib.rs#L644), [Transformers loader](https://github.com/huggingface/transformers/blob/v5.22.2/src/transformers/modeling_utils.py). Actual installed-source inspection and local measurements govern the decisions.

## Runtime protection and retained corrections

Preflight requires 6 GiB available RAM, 8 GiB free on GPU0 and 8 GiB disk. These are experimental protection thresholds, not measured model demand. The supervisor reuses the existing spike22 `OwnedProcessTree`, records actual descendant RSS/identities and device/available-memory samples, and verifies cleanup. It stops only its worker tree at a 900 s deadline or after three samples below 2 GiB available RAM. Logs and live `resources.jsonl` are contained and retained. Failure exits nonzero; completed batch windows survive a later failure.

Retained corrections include the initial packet-copy window timing defect, incompatible initial safetensors pin, launcher-only RSS accounting, and stale Windows directory sizes that caused an unnecessary transfer stop. The original 2.6 GB partial is retained; the stop was a monitoring mistake, not a proven network stall. One disposable reader probe initially bypassed `ProjectRoot` for its own assigned-directory artifacts. Its original source/result were retained, future writes were corrected through the helper, and the corrected CPU probe was independently rerun. See its `containment_adjudication.json`.

## Next run

No semantic listening findings exist. Wait for the RAM guard to pass, then run the controlled five-second smoke from this worktree:

```powershell
$analyzerPython = 'spikes/23_joint_av_analyzer/_work/env/Scripts/python.exe'
& $analyzerPython -B spikes/23_joint_av_analyzer/analyze.py run --media spikes/23_joint_av_analyzer/_work/media/windows_verified_20261111T/window_01.mkv --start 1397 --mode joint --max-new-tokens 192 --duration-limit 5
```

Only after successful loading/tensor/quantization/cleanup audits, run all seven windows with `--manifest spikes/23_joint_av_analyzer/_work/media/verified_windows_manifest.json`, then compare audio-only observations where useful. Attribute future semantic descriptions to Qwen, not firsthand hearing by the parent. Creator quality approval remains pending.
