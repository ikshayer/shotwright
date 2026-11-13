# Hunyuan video-conditioned Foley: completed local feasibility test

2026-11-09. **Hardware feasibility passed with an isolated loading workaround; audio-quality acceptance failed for the tested scene/prompts.** No production audio adapter, GUI, commit, push, merge or publication. Contract.md with Amendments1-3 records scope and adjudication.

## Creator acceptance and GUI direction

Creator said "I would say this passes" about spike20 scripted_03bdf358 / s1 content version2 / delivery SHA256 9860e9ab850b52fb19d03aa9b73aa5b4330f7bc998ec06318ebacae6ea9a8317. This is conversational visual acceptance of that exact shot, not overall Phase1 acceptance, an audio approval or a claim that an API review record was created.

Creator expects to build a GUI after Phase1, probably on an open-source video editor. Preserve separate native/processed WAV and source video, integer sample timing and rational frame rates, relative media paths/hashes and provenance JSON. Project-local production manifests/API stay authoritative; a future editor project can be an import/export view. No editor selected or application-specific integration tested.

## Actual execution

All artifacts are contained in this worktree; production .venv and ComfyUI installation were not modified. No Blender rerender or new visual generation. Our own clean 15s master was independently hash-checked, CPU-encoded to512x384/25fps (375frames) at `_work/run02/clean_proxy.mp4`. No reference media was supplied to a model. Separate3s smoke and8s movement-window (global7..15s) proxies were derived from this same production master.

The first setup was held when available RAM dropped1.932->0.54->0.287GiB. Parent stopped only its own sessions and asked creator to free memory. On creator's recheck:12.245GiB RAM available, GPU1402/12227MiB used, disk102.663GiB free. Setup then resumed successfully. This resource hold was not a model OOM. Initial incomplete download/proxy/cache evidence remains; `_work/preflight_status.json` is the historical hold snapshot, superseded by this report.

Actual runtime: Python3.11.9, Torch2.7.1+cu128, torchvision0.22.1+cu128, torchaudio2.7.1+cu128, transformers4.51.3, diffusers0.33.1, NumPy1.26.4, descript-audiotools0.7.2, PyAV15.1.0. An actual kernel and BF16 random/matmul operations passed on NVIDIA RTX5070TiLaptop, CUDA device0, capability(12,0). `_work/cuda_preflight.log` and `main_tensor_diagnostic.log` retain evidence. PyAV14.4 attempted a failed Windows source build; selected the official15.1 cp311 Windows wheel instead. Failed build log retained.

Code pinned to Tencent-Hunyuan/HunyuanVideo-Foley df7b005b5023df2a9b73e1d66dd51d452799884e. Official HF model revision3abd4e833b95b8db0fc9c687afc52483a48e9a97; SigLIP2 a89f5c5093f902bf39d3cd4d81d2c09867f0724b; CLAP ada0c23a36c4e8582805bb38fec3905903f18b41. Selected22 model/component files total10,608,258,234bytes. Authoritative LFS SHA256 or Git blob hash was verified before loading; downloaded identities are retained in `_work/{hf_metadata,download-lock,verified-models}.json`. HF XL and repository YAML configurations compare equal.

## Safety/compatibility workarounds and failure evidence

Read-only reviewer identified unsafe main/Sync weights_only=False loads, DAC's inherited package-unpickling loader, implicit unpinned encoder downloads, unbounded feature batches, avoidable main/decoder overlap, unused audio_length CLI flag and upstream channel-zero duplication. Runtime uses strict verified tensor-only loading, local/offline pinned encoders, bounded SigLIPbatch4/Syncbatch2 and explicit NVIDIA device. DAC metadata kwargs and all298 state keys were independently inspected in tensor-only mode; no unsafe globals found.

Initial smoke `_work/run02/smoke03` exited0xC0000005 while loading the main model, before denoising. RAM stayed above9.341GiB; watchdog did not stop it. Exact native crash cause remains unproven. A separately reviewed workaround constructs main model on meta device, strictly assigns exact checkpoint tensors, verifies no meta parameters/buffers remain, then transfers BF16 weights to CUDA. Safe CPU main mapping (733keys) and BF16 GPU controls passed independently. One bounded retry succeeded; all subsequent takes used this workaround. Do not claim stock upstream Windows inference passed.

Model is released before decoder loading. An independent child-process watchdog admits only above6GiB available RAM and stops sustained<2GiB,240s log stalls or900/1200s deadline. These are conservative diagnostic policies, not measured reservation requirements. No watchdog stop or OOM occurred in successful runs. Loader/precision changes stay isolated under spike21; no production retry taxonomy or admission defaults changed.

## Measured takes

Run root: `_work/run02/`. All successful takes used XL/offload, seed210915,50steps, guidance4.5, BF16 main model, FP32 encoders/decoder. Prompts, versions, native hashes, sampled memory and timings are in each generation.json and watchdog.json; `comparison_results_final.json` consolidates them.

| Run | Native duration | Generation stage | Whole process | Peak whole-device VRAM |
|---|---:|---:|---:|---:|
| smoke03_meta |2.96s|48.500s|94.125s|7437MiB|
| full15, quiet room prompt |15.00s|60.313s|109.125s|7707MiB|
| full15_fan, concrete fan prompt |15.00s|66.891s|118.140s|7882MiB|
| movement8, brief fan/creak prompt |7.96s|51.797s|100.110s|7682MiB|

Whole-device samples include other applications (baselines1473-1505MiB), not attributable model demand. Torch allocator peaks are5704-5849MiB. Minimum available RAM in these generation samples ranged4.448-6.248GiB. No repeatability/bitwise-determinism or default queue/admission acceptance claim follows.

Both15s takes contain exactly720000 mono samples at48kHz, but integrated loudness was below FFmpeg's measurement gate (-inf): native true peaks -56.14dBFS (quiet) and -42.00dBFS (fan). They are nearly silent, not accepted ambience. The8s movement-window take is7.96s/382080samples, measured -69.72LUFS / -41.77dBFS true peak, also too weak for normal use. The3s smoke had a large DC component (mean~0.324), so generation success is not sound-quality success. These are numerical observations; parent did not claim to hear or approve the sound or verify semantic synchronization.

Maintainer recommends5-10s single-scene input and brief prompts: https://github.com/Tencent-Hunyuan/HunyuanVideo-Foley/issues/23 . Paper describes8s training segments: https://arxiv.org/html/2508.16930v1 . Published architecture differs from releasedXL, so exactXL training recipe is not proven. Eight-second diagnostic is justified; neither duration nor prompt is established as the silence's cause. Do not label this an inherent model defect without further positive controls. No further GPU takes were run after this bounded comparison.

## Review and editor interchange artifacts

Selected diagnostic directory: `_work/run02/full15_fan/diagnostic/`.

- `audition.mp4`: original accepted full15s video stream copied unchanged, AAC audio encoded from unprocessed diagnostic take. Almost silent by measurement; not production-ready audio.
- `edit_48k.wav`: separate48kHz mono PCM24 WAV,720000samples, start0. Native float WAV retained one directory above. No gain, loudness normalization or duration repair applied. Normalization target failure is explicit, not hidden by boosting noise.
- `editor_manifest.json`: worktree-root-relative source/asset hashes, exact generation/configuration, integer sample timeline, model/licence provenance, current producer scripts and executing runtime snapshot identities.
- `editor_bundle/project.json`, `source_video.mp4`, `native.wav`, `edit_48k.wav`: portable manifest-directory-relative media. Model/cache files are not required to open these assets. Source video is a byte-identical copy of the accepted delivery. This is internal diagnostic interchange, not production export/AT09 acceptance.

A first loudness JSON parser incorrectly included trailing FFmpeg stderr; fixed with single-object parsing, failure retained. A subsequent attempted normalization encountered unmeasurable levels; native remained unchanged. Processor now explicitly refuses nonfinite normalization gain. Diagnostic export requires an explicit flag, applies0dB gain, and labels quality targets unmet. No apparently successful normalized ambience was manufactured.

## Independent verification and limits

Implementation, read-only review and the original verifier have separate authors. Runtime/verifier workers reached provider usage limits after initial preparation; parent operational takeover is documented in Amendment1. Reviewer later independently authored narrow verifier fixes for signed-infinite LUFS and separate quality reporting; parent reran actual verification.

`parent_verification.json`: **technical_passed=true, audio_quality_passed=false, overall_passed=false**, expected exit1. No structural failures. Actual source hashes,900-frame60fps master,150-frame10fps delivery, full15s720000-sample native/editor audio, source/model/script hashes and identical compressed video packet hash all verified. Quality fails -18+/-0.5LUFS requirement; true peak/no-clipping checks pass. Semantic timing, voice/music absence, perceptual quality and human acceptance remain unverified.

`bundle_verification.json`: portable relative media hashes and FFprobe reading passed without invoking model runtime; no specific video editor tested. Prior orchestrator/tests source hashes still match the1744-test frozen snapshot (`_work/production_source_check.json`). No new full production pytest run was claimed or needed for isolated spike files. Final Ruff spike21 and diff whitespace checks passed; primary tracked checkout remains clean. Work remains uncommitted.

## Rights and decision

Actual Tencent LICENSE restricts Territory excluding EU,UK,SouthKorea; section5(c) expressly restricts Output/results outside Territory. Its output-ownership disclaimer does not remove that restriction. Code/weights licence and original figure/font internal-evaluation limitations remain in sidecars. SigLIP/CLAP model cards declare Apache2; separate actual licence files were not supplied. No worldwide publication clearance or production model adoption.

Result: local inference is feasible with the recorded workaround and memory headroom. Our tested configurations did not yield usable ambient audio, so Hunyuan is not promoted as a default. Preserve diagnostics for creator inspection; compare another engine or debug against an authorized positive control before adopting. Future GUI boundaries are demonstrated through separate standard media/portable JSON; no GUI build or editor choice occurred.

## Reproduction entry points

Use the shared orchestrator Python with -B for setup_runtime.py (separate --env / --download), launch_runtime.py and verify_outputs.py; use spike21/_work/env/Scripts/python.exe -B for finish_outputs.py. Exact successful inference commands are retained in each watchdog.json. Use a fresh contained output directory for every take; never overwrite existing run artifacts. launch_runtime.py --proxy <worktree-relative-proxy> --out <fresh-relative-directory> --seconds 3|8|15 [--prompt <brief-description>] owns the bounded runtime process. This report does not authorize additional blind GPU reruns.

To recheck the completed diagnostic without generation: shared Python -B spikes/21_video_foley/verify_outputs.py spikes/21_video_foley/_work/run02/full15_fan/diagnostic/editor_manifest.json --report <fresh-contained-report.json>. Expected exit1 is the honest loudness-quality failure; technical_passed must remain true. No model runtime is invoked by this verifier.
