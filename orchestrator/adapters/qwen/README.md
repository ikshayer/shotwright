# Local Qwen helper

This package provides a standalone, local Qwen analysis helper for reference-media notes and generated-audio review. Its output is advisory text: a creator must review it, and it never grants approval or updates project entities.

The helper uses the repository's existing pinned Qwen model and isolated runtime under `spikes/23_joint_av_analyzer/_work`. It does not install packages or modify that runtime. The helper is deliberately outside the production CLI, HTTP API, job registry, and production GPU lease. It does not stop applications or take over their GPU memory.

## Requirements and paths

Run from the repository root. The default project root is the repository containing this adapter and the existing spike/runtime. Media and brief-file arguments must be existing project-relative paths inside that root; URLs, absolute paths, and paths escaping the root are rejected. Adapter reports, temporary files, prepared media, logs, and caches are written under `_work/qwen_helper/` through the project's containment helper. The existing model worker retains its own contained cache/temp directories under `spikes/23_joint_av_analyzer/_work`. Each invocation gets a unique report directory; reports are retained.

Use the repository's orchestrator environment, not ComfyUI's embedded Python:

```powershell
D:/Work/video_pipeline/.venv/Scripts/python.exe -B -m orchestrator.adapters.qwen inspect
D:/Work/video_pipeline/.venv/Scripts/python.exe -B -m orchestrator.adapters.qwen analyze --media references/my_clip.mkv --dry-run
D:/Work/video_pipeline/.venv/Scripts/python.exe -B -m orchestrator.adapters.qwen analyze --media references/my_clip.mkv
D:/Work/video_pipeline/.venv/Scripts/python.exe -B -m orchestrator.adapters.qwen review --media references/generated_take.wav --mode audio --brief "Intent: restrained, distant security-room speech. Evaluate whether the voice and recording feel appropriately degraded; hiss and distortion may be intentional."
```

`inspect` reports the pinned model revision and whether the existing analyzer, isolated Python, and model files are present. It does not load CUDA or the model. `--dry-run` probes the media, validates the requested interval, plans windows, and writes a planned report without preparing clips or running inference.

`analyze` creates reference notes and accepts an optional brief. `review` is for generated-audio review and requires a nonempty intent brief, supplied with `--brief` or `--brief-file` (mutually exclusive). The brief is preserved as the creator's intent and shown separately from Qwen's observations. For audio review, describe what the take is meant to sound like, including whether hiss, distortion, narrow bandwidth, or other degradation is desired. Qwen should judge fit to that intent rather than assume clean studio fidelity; its suggestions remain recommendations.

## Windows and time offsets

Defaults are a 25-second window, 2-second overlap, and a maximum of 500 generated tokens per window. The window size can be reduced, but cannot exceed 25 seconds; overlap must be nonnegative and shorter than the window. Long requested intervals are decoded into overlapping windows to preserve coverage. The final window may overlap the preceding window by more than the requested overlap so the end is covered without leaving a gap. Inputs requiring more than 256 windows are rejected with guidance to request a shorter interval.

`--start` and `--duration` select an interval in the media file's timeline. `--source-offset` adds a caller-provided offset to the reported source timestamps; it changes timestamp metadata, not which media samples are decoded. For example, a clip beginning 12 seconds into a larger source can be analyzed with `--source-offset 12`; use `--start` separately to select a position inside the supplied clip.

```powershell
D:/Work/video_pipeline/.venv/Scripts/python.exe -B -m orchestrator.adapters.qwen analyze `
  --media references/long_clip.mkv --start 30 --duration 55 `
  --source-offset 120 --window-seconds 25 --overlap-seconds 2 --max-new-tokens 500
```

The helper reports requested and completed coverage, uncovered intervals, per-window status, and partial failures. It does not present a clean overall conclusion when some windows failed. A response reaching the token cap is retained and marked truncated; it is not silently treated as a complete answer.

Media preparation follows the decoded trim workflow and preserves native audio rate, channels, and amplitude. Joint audio/video analysis scales decoded video to at most 640 pixels wide while retaining aspect ratio. The preparation records conversions and signal measurements. The measurements describe the prepared samples; they do not establish that model-generated semantic descriptions are correct. FFmpeg's trim filters are documented at [trim](https://ffmpeg.org/ffmpeg-filters.html#trim) and [atrim](https://ffmpeg.org/ffmpeg-filters.html#atrim).

## Results and interpretation

Successful CLI invocations print one JSON report to stdout. The report includes `report_path` and `markdown_path`, both project-relative links under the unique `_work/qwen_helper/` run directory. The JSON and Markdown reports retain every raw window response, the separate intent brief, signal metrics, termination reason, token-cap information, coverage, and any errors or limitations. A dry run also produces these reports with planned status. Startup/request errors are JSON on stderr; job failures return a nonzero exit status, and cancellation returns 130.

Qwen responses are unverified model output, not authoritative facts or an approval decision. Review the source media and the full per-window responses, especially where coverage is partial, audio is silent/corrupt, or a response is truncated.

## Runtime and concurrency limits

The helper checks disk, RAM, GPU availability, and any currently recorded live production lease before inference. The configured admission guards are operational checks, not guarantees that another process cannot consume resources afterward. The helper's nonblocking lock serializes helper invocations only. It does not globally serialize GPU use: a production job can start after the lease precheck, and unrelated GPU applications do not honor the helper lock. Resource conditions are rechecked between batches; if admission fails or inference fails, completed window results are retained and later windows are marked accordingly. The helper does not stop competing applications or retry automatically.

The spike23 Qwen runs sampled roughly 200 MiB of free GPU memory at the tightest 25-second measurements on this machine. This is sampled headroom, not a measured memory demand or a guarantee of available memory; other GPU use varies. See the spike findings and the per-run resource telemetry before choosing a workload.

Cancellation runs the existing worker cleanup and retains a report when possible. Cleanup is verified from the worker's recorded process-tree telemetry; uncertain cleanup is treated as a failure and prevents another batch. The existing cleanup wait can be unbounded, so cancellation is not guaranteed to finish within a fixed time.

This helper remains a local CLI/API adapter with no production scheduler integration. Production use would require the model-slot, budget, and lease registration specified by the pipeline PRD.



## Reading results from an agent shell

The complete stdout JSON includes native telemetry and may be large. Claude/Codex can capture it and print only the saved report links, then read the Markdown report for complete model responses and measurements:

```powershell
$qwenResult = & D:/Work/video_pipeline/.venv/Scripts/python.exe -B -m orchestrator.adapters.qwen review --media media/generated.mp4 --brief-file briefs/audio_intent.txt | ConvertFrom-Json
$qwenResult | Select-Object status, report_path, markdown_path | ConvertTo-Json
if ($LASTEXITCODE -ne 0) { throw "Qwen helper failed; inspect its retained report and errors." }
```

Run these commands from `D:/Work/video_pipeline/.claude/worktrees/api-conformance` for this checkout. Examplemedia/brief paths must be replaced with existing contained files.


## Verified status

The adapter passed **89 independent tests**. The full repository suite passed **1,833 tests**, with 50 skipped. Real CLI integration analyzed the 35-second reference in two overlapping windows and reviewed an existing eight-second generated video against an intent brief. All three responses ended at EOS; parent PCM/frame/provenance and fresh process-cleanup checks passed.

See [VALIDATION.md](VALIDATION.md) for exact runs, source identities, limits and the quiet-candidate calibration caveat. These runs verify adapter operation. Qwen's semantic accuracy remains advisory.

Use self-contained local video/audio files. Input protocols are restricted to `file,pipe`; arbitrary nested local references and a separate demuxer-format allowlist are outside this first local CLI version's validated scope. It is not a public upload boundary.
