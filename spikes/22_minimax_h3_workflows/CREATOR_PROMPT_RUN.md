# MiniMax H3 creator prompt run - 2026-11-12

## Result

Creator acceptance: on 2026-11-12, after reviewing this exact candidate, the creator said, "Great, we can probably conclude the MiniMax H3 test as successful". This concludes the authorized feasibility/prompt test only; no production approval record was created. The artifact SHA-256 is `481913590fae97e83d7a84e33f16eace951063f0c1386be138351ed99e08a215`. No further inference is queued, and this is not overall Phase 1/episode approval, publication clearance, unattended-resource acceptance, or default-backend promotion.

A real ComfyUI Easy-route generation executed the creator-supplied prompt in `spikes/22_minimax_h3_workflows/_work/direct_comfy/20261112T163148Z_22ff9186/prompt.txt` (SHA-256 `83b2fe747ebe4cade53654be271dd22ab81c3fa9433a80616eb2c2603c9989b1`, exact bytes without a trailing newline). ComfyUI history prompt ID: `b13915a8-2162-4e49-88de-997a755f2a25`. The graph matches the preceding actual Easy-route graph except for the prompt and output prefix; seed `8263381764361378`, 384x288, 192 frames at 24 fps, `res_multistep` 20, `simple`, denoise 1.0, and the four pinned NVFP4/INT8/FP32 components are recorded in `prepared.json` and `frozen_provenance.json`. This Easy graph is not in the production workflow registry.

The native MP4 is `spikes/22_minimax_h3_workflows/_work/direct_comfy/20261112T163148Z_22ff9186/output/creator_prompt_easy_00001_.mp4` (8.000 s H.264/AAC). Its separately decoded float WAV is `spikes/22_minimax_h3_workflows/_work/direct_comfy/20261112T163148Z_22ff9186/media/creator_prompt_audio.wav`. The parent output verification reports full AV decode, finite/unclipped audio samples, and no gain applied. The result status is `generated_candidate_unapproved`; the queue reported no cache hits and no automatic retries.

## Audio measurement

Decoded AAC first-seven-second RMS is **-48.6627 dBFS**, versus **-69.1175 dBFS** for the prior actual Easy-route candidate: an increase of **20.4548 dB** over that interval. Whole-clip RMS is **-47.2066 dBFS**, integrated loudness **-45.7 LUFS**, and peak **-17.3692 dBFS**. This is a clear measured increase over the prior run, while remaining quiet overall. These measurements do not establish that the requested hiss/creak mix or negative instructions were perceptually satisfied.

No firsthand listening or semantic-quality assessment was performed. The creator's statement that the supplied example has sounds remains a creator report. Creator accepted this test; no production approval record was created.

## Runtime and cleanup

Native actual execution was **572.865 s**; parent queue elapsed time was **575.093 s**. Minimum sampled available RAM was **2.40656 GiB**. Peak whole-device GPU memory was **11,744.273 MiB**, including foreign usage; it is not this job's attributable demand. The requested stop completed, the watchdog joined, the three independently tracked owned identities were absent afterward, and port 8298 was closed.

Parent independently verified output and cleanup. An independent read-only review recomputed the PCM measurements and checked the actual history graph diff, clearing the run operationally. A Qwen audio-only review covered 0-8 s and returned 358 tokens with EOS and no errors/cap (`_work/qwen_helper/20261112T165019Z_59846383/report.md`; raw JSON beside it). Parent confirmed the Qwen-prepared native-rate PCM matched the generated MP4 AAC decoded samples byte-for-byte (2,048,000 bytes; `parent_final_audit.json`). The intent brief matched exactly, all 1,332 pinned native files and runner code are unchanged, and the three Qwen-owned process identities are absent. Qwen described continuous hiss and occasional creaks, but also echoed visual lighting despite the audio-only task and suggested unsupported traffic/ticking and visual flicker; treat these as model claims, not semantic confirmation. No automatic changes were made. Qwen native review time was 225.844 s; its own NVML scope recorded 1,293/10,651/1,318 MiB baseline/peak/post and minimum sampled available RAM of 11.418 GiB. This is audio-only and is not a full motion review.

## Retained evidence

All run inputs, outputs, history, graph, resource samples, logs, and verification reports remain under `spikes/22_minimax_h3_workflows/_work/direct_comfy/20261112T163148Z_22ff9186/`. No prior files were replaced. No commit, publication, model installation, or production approval occurred. This records one actual data run; no full test suite was run for it.
