<!-- START H3 TEST ACCEPTANCE 2026-11-12 -->
# CURRENT: creator accepted the MiniMax H3 test as successful

On 2026-11-12, the creator said, "Great, we can probably conclude the MiniMax H3 test as successful" after reviewing the exact 8-second, 384x288 H3 Easy candidate `spikes/22_minimax_h3_workflows/_work/direct_comfy/20261112T163148Z_22ff9186/output/creator_prompt_easy_00001_.mp4` (SHA-256 `481913590fae97e83d7a84e33f16eace951063f0c1386be138351ed99e08a215`). This concludes the authorized feasibility/prompt test. The exact creator-prompt full run measured first-seven-second RMS 20.4548 dB stronger, with no gain applied; prior measurements remain historical facts. No further inference is queued. This acceptance is not overall Phase 1 or episode approval, a production API approval record, publication clearance, unattended-resource acceptance, or default-backend promotion.
<!-- END H3 TEST ACCEPTANCE 2026-11-12 -->

<!-- START H3 CREATOR PROMPT RUN 2026-11-12 -->
# COMPLETED: actual run of the creator-supplied prompt (2026-11-12)

`CREATOR_PROMPT_RUN.md` records the real Easy-route run under `spikes/22_minimax_h3_workflows/_work/direct_comfy/20261112T163148Z_22ff9186`. The exact prompt SHA-256 is `83b2fe747ebe4cade53654be271dd22ab81c3fa9433a80616eb2c2603c9989b1`; history prompt ID is `b13915a8-2162-4e49-88de-997a755f2a25`. The existing graph/settings and seed were retained apart from prompt/output prefix. Output verification and independent operational review passed. First-seven-second decoded-audio RMS was -48.6627 dBFS, versus -69.1175 dBFS for the prior actual run (+20.4548 dB); whole clip was -45.7 LUFS and remains quiet overall. This does not verify perceived sound quality, intent/negative-instruction compliance, or production readiness. Qwen's audio-only review completed (358 tokens, EOS, full 0-8 s) but echoed unsupported visual/traffic details, so it is not semantic confirmation. No full-motion review is claimed. All prior findings and creator blocks remain intact.
<!-- END H3 CREATOR PROMPT RUN 2026-11-12 -->

<!-- START H3 CREATOR-REPORTED SUCCESS 2026-11-12 -->
# CURRENT CREATOR-REPORTED RESULT: successful H3 audio and exact prompt captured

On 2026-11-12, the creator reported successful MiniMax H3 generations in their ComfyUI setup: explicitly telling H3 in the generation prompt to be loud produced loud output. Setup identity supplied by the creator: [ComfyUI-MiniMaxH3-Easy](https://github.com/nkxx188/ComfyUI-MiniMaxH3-Easy); the link identifies the setup only, while loudness is the creator's observation.

The creator has now supplied an exact walking-footage prompt and states that the example has sounds. [The exact supplied prompt](CREATOR_SUCCESSFUL_PROMPT.md) retains it verbatim. It explicitly requests clearly audible continuous broadband hiss, quieter overlapping wooden creaks, no hiss fade/ducking, and near-silent movement without shoe impacts or breathing. This is an audibility and relative-mix direction, not a request to make every sound loud.

H3 is creator-demonstrated viable; earlier measured quiet runs remain historical rather than the current overall conclusion. Successful workflow JSON/output paths and exact model/runtime/settings are still not supplied. The prompt alone is not an established automated recipe or controlled same-seed proof. No new listening/measurement, visual/reference-retention or negative-instruction compliance verification, or production approval is claimed.
<!-- END H3 CREATOR-REPORTED SUCCESS 2026-11-12 -->

# COMPLETED: known-audio H3 VAE round trip verified (2026-11-10)

The creator-authorized known-audio test is complete. Run `spikes/22_minimax_h3_workflows/_work/local_runs/vae_roundtrip_20261110T211654Z_1bcabfdd` captured and independently verified the native audio-VAE round trip. Do not start another inference automatically. The previous generated soundtrack remains unsuitable/unapproved.

Known input combined-stereo RMS was -24.375 dBFS; raw reconstructed RMS was -24.940 dBFS, a -0.565 dB difference without fitted gain or normalization. Correct principal tones survived: left220/440 Hz and right330/660 Hz. Direct channel correlations were0.907/0.882; bounded best lag0. The final silent second reconstructed at -94.616 dBFS combined-stereo RMS. These measurements establish that this tested checkpoint/native VAE route can carry these synthetic signals. They do not identify the cause of prior diffusion-generated silence or prove a quantization fault; natural-ambience quality remains unapproved.

Only the existing pinned audio VAE was loaded through actual native Comfy main.py/VAELoader, CUDA0/float32/offloadCPU. Exact8sec/256000samples/channel32kHz fixture passed independent sample-byte/native PyAV checks. Native VAEEncodeAudio produced [1,32,2,320] normalized posterior-mean latent. Existing diagnostic called decode once, retained raw/std/divisor/post float32 tensors and full WAVs. Raw/post [1,2,256000] match exactly because divisor1.0; no crop/pad/resample/added gain, no native tiled/OOM fallback. No diffusion/text/video model or speech/reference input. SaveAudio FLAC was only the graph trigger, not comparison authority.

Evidence in the run: input/known_audio.wav; audio_diagnostic/raw_decoded_audio.wav; audio_diagnostic/tensors.safetensors and receipt.json; parent_independent_verification.json; parent_postrun_verification.json; roundtrip.json; graph/history/resources/logs/code/. All1332 native source hashes and exact audio checkpoint match pre/post and independent actual verification. Driver SHA03405b43d8b0150b884ac2f33490eb113a0dc7d669df9916ed1fd0c9722b763c was reviewed, archived and executed from exact once-read hash-checked bytes. Parent independently reran six CPU checks on frozen code; source and actual-run review cleared. Ruff passed. Full production pytest and listening were not performed.

Native prompt executed in6.10s; total controller129.219s includes startup/provenance checks/export. Admission availableRAM7.801GiB, minimum5.757GiB; peak whole-device GPU1586.098MiB. Policies6GiBadmission/2GiB3samplestop/180sstartup/1800sexecution unchanged. Observations are not model minimum demand. Watchdog joined; owned tree cleanup has no survivors/errors. Parent confirmed launcher27760/worker39592 absent and no8293-8297listeners.

Earlier setup `vae_roundtrip_20261110T211345Z_ec19d518` was explicitly stopped before any prompt because a late metadata-only delegated edit broke source/archive binding. Its failed preparation and parent_setup_stop.json are retained; no VAE/model execution occurred. Final frozen setup above performed the only actual queue. This provenance issue was resolved before model work, and no GPU inference retry occurred. Prior runs/pre-existing edits preserved; no checkpoint/runtime/env/production mutations or commit/push/merge/publication.

Next investigation is the generated-latent/sampling path, with quantization still an unisolated hypothesis. No additional experiment is authorized by this completed roundtrip increment.

<!-- END COMPLETED VAE ROUNDTRIP -->

---

# COMPLETED: raw audio diagnostic identifies silence before export (2026-11-10)

The creator-authorized diagnostic completed in `spikes/22_minimax_h3_workflows/_work/local_runs/20261110T162940Z_89b935ab`. Do not launch a duplicate inference. The soundtrack remains unsuitable/unapproved: the first seven seconds of raw VAE output measure -74.959 dBFS mono RMS (-73.818 dBFS combined stereo RMS). The brief stronger transient near 7.87 seconds dominates whole-file loudness.

Captured sampled audio latent, raw decoder waveform, std-times-five, divisor and post-native waveform in `audio_diagnostic/tensors.safetensors`; full float WAVs and hashed receipt are beside it and in the portable editor bundle. Exactly one direct VAE decode was called. Actual std-times-five 0.007408689 clamps to divisor 1.0; raw/post tensors and WAVs match exactly. All 256000 samples/channel at 32 kHz were retained, with no crop or padding. Decoded AAC first-seven-second mono RMS is -74.959 dBFS; full-wave mono correlation 0.999454 and RMS ratio 0.999186, best lag zero. Normalization, cropping and AAC do not explain the near-silence in this run.

This locates near-silence at or before audio VAE.decode output. It does not distinguish diffusion/sampled-latent content from an audio-VAE problem. Nonzero finite latent values do not establish meaningful sound. No louder prompt, gain, new weights/sampler, install/download or automatic retry was used; prompt/seed/quantized models and 8-second 192-frame 24-fps 384x288 staged graph were preserved apart from the diagnostic audio node. Existing safety floors/deadlines remained unchanged.

Parent independently ran actual tensor/WAV/native-math/codec verification (passed), full output verification (all nine checks passed), audited 14-case real-CPU tests (passed), Ruff for five scripts and git diff check (passed). Earlier six staging cases, four-node model-free CPU schema smoke and all 1332 pinned source hashes passed. Independent source and actual-artifact review agreed. Full production pytest and listening were not performed; quality and creator approval are not achieved. See `parent_audio_diagnostic_verification.json`, `parent_independent_output_verification.json` and `parent_postrun_verification.json` in the attempt.

Generation/export took 253.719 seconds; total controller time 350.891 seconds. Minimum observed available RAM 4.564 GiB; whole-device GPU peak 11715.508 MiB, including foreign processes. These are observations, not minimum requirements. Recorded owned-tree cleanup verified no survivors/errors and joined the watchdog. Parent confirmed PIDs 19596/35200/39332/46788 absent and no listeners on 8293-8297. All earlier attempts and pre-existing edits retained; production approvals/state unchanged; no commit/push/merge/publication.

Next causal experiment would independently validate the audio VAE on known audio before attributing the failure to quantization or diffusion. No such experiment has run, and this authorization was fulfilled by the captured diagnostic.

<!-- END COMPLETED RAW AUDIO DIAGNOSTIC -->

---

# Creator audio feedback - 2026-11-10

Creator reports no audible sound. Per-second native and edit PCM analysis confirms first7seconds near-74dBFS RMS; overall-41.7LUFS and peak-20dBFS do not describe sustained ambience because a brief end transient dominates them. See attempt20261110T060837Z_aadcfd60/audibility_assessment.json. Native/edit exact PCM/no-gain integrity still passes; functional generation and memory staging success do not establish useful sound. No creator sound approval. Independent native audio path review found no concrete routing/export-gain defect. No raw decoder waveform/divisor retained, so root cause remains unresolved; next diagnostic should capture those intermediates before another quality claim. No additional inference or amplification.

---

# Staged 384x288 H3 diagnostic - completed 2026-11-10

Both requested changes were implemented and exercised with the existing quantized weights. The complete8s/192frame24fps candidate generated at384x288 with native stereo32kHz audio. Independent verification passed all8checks, including full decode, exact native extracted PCM equality, explicit no-gain48kHzPCM24 conversion, original canonical video packet identity in audition, stage execution/provenance, and19hashed portable bundle records. Creator approval and sound/visual quality remain pending.

Attempt: `spikes/22_minimax_h3_workflows/_work/local_runs/20261110T060837Z_aadcfd60`. See parent_independent_output_verification.json, parent_postrun_verification.json, generation.json, graph.json, history.json, stage_events.jsonl and stage_release.json. The previous failed640x480 attempt remains intact.

## What changed

Project-owned h3_staging extension calls stock CLIPLoader, both VAELoaders and native R2V inside a model-owning scope. It unloads conditioning models, drops references and verifies weakref death. A locally issued single-use release token gates stock diffusion loading; sampled latents gate decode VAE reload. Staged launch whitelists only this extension through contained extra-path YAML. Pinned Comfy runtime source, existing Comfy installation, shared environment, checkpoint catalogue/receipts and original accepted production shot were not changed. No new model/package download or install.

Both the reference proxy and generated canvas are384x288. The entire canonical8seconds remains192frames, with no audio input, timing change or image-only fallback. Actual native input shape was[192,288,384,3]; video reference latent[1,24,57,18,24]. Prompt, derived seed and res_multistep20/simple/denoise1 remain unchanged. Staged output caching retains conditioning/primitive token, never CLIP/VAE wrappers. Native attenuation is disclosed; export gain0.

## Measured release and run

All9model-wrapper/patcher/nn-model weakrefs died before diffusion; loaded-model inventory was empty. Same worker identity and invocation span ordered events. From post-condition/pre-release to immediately before diffusion load, worker private memory fell15.037->4.797GiB, resident RSS5.241->2.341GiB, available RAM rose6.586->9.458GiB and CUDA reserved1728->64MiB. Independent watchdog samples corroborate the drop. These are boundary observations, not absolute model demand. Immediate post_release values are separately retained; allocator reclamation continued until diffusion entry.

Total controller time1080.828s (18m00.828s), queued-to-history968.203s, actual history execution966.688s.807watchdog samples: minimum available RAM5.321GiB; minimum available commit20.989GiB; peak whole-device GPU11723.777MiB; peak owned-tree RSS6.077GiB/private16.663GiB. Minimum Cfree34.306GiB/Dfree60.095GiB. No watchdog stop, inference error or retry. Actual sampling interval mean1.269s/max1.359s, including observer overhead. Both staging and canvas changed, so this is not an isolated whole-run comparison.

## Deliverables and limits

- Generated AV: output/native_h3_00001_.mp4,384x288,192frames,8.000s, H264+AAC32kHz stereo.
- Native decoded WAV: media/native_decoded_audio.wav, float32,32000Hz stereo,256000samples/channel.
- Editing WAV: media/editing_48k_pcm24.wav,48000Hz stereo PCM24,384000samples/channel; trim0,pad0,gain0.
- Audition: media/audition_original_video.mkv, original1024x768 canonical video streamcopy plus editing PCM.
- Portable editor bundle: editor_bundle/project.json and media/provenance/notices/credits. Opens media without H3 installed; weights/reference media absent.

Native decoded audio measures-41.7LUFS,truepeak-20.0dBFS,RMS0.00148076046,zero clipped/nonfinite values. It is quiet; no loudness target or normalization was applied. No listening tool was used. Parent inspected five generated/reference frames: fixed room/furniture remain recognizable, fine edges change, and it still reads as CG. This is not realism/sound-quality acceptance or a full motion inspection. Independent canonical/master/asset provenance hashes match unchanged sources.

## Verification and cleanup

Parent reran6blind staging boundary probes and6Windows ownership probes: passed. Actual isolated model-free CPU schema boot passed all3owned interfaces/model choices and stopped. All1332pinned runtime source hashes match. AST/Ruff pass for changed diagnostic scripts; full production pytest was not run. Focused independent source and actual stage/resource reviews completed. codex-alt shell review could not initialize its app-server due WindowsAccessDenied; no shell review claimed. Early CPU fixture failures and corrected reports are retained under _staging/.

Final cleanup terminated only owned worker39504/launcher21080 after successful export, joined watchdog, and recorded no survivors/unknowns/failures. Parent independently confirmed bothPIDs absent and no8293-8297listeners. Exit15 is intentional post-success shutdown. No creator approval, production mutations, commit/push/merge/publication.

One successful bounded384x288 run does not establish640x480 fit, repeatability, minimum hardware or general unattended acceptance. Review the media before another experiment.

---

# Local H3 diagnostic findings - 2026-11-10

The bounded native H3 attempt stopped at the configured available-RAM floor. It completed conditioning and reached 1/20 sampler steps, but produced no generated video or audio. This is a retained failed diagnostic, not a completed quality/fit test. No automatic retry ran.

Attempt: `spikes/22_minimax_h3_workflows/_work/local_runs/20261110T052109Z_a7bfe4cd`. See `generation.json`, `comfy.log`, `resources.jsonl`, `parent_independent_output_verification.json`, and `parent_postrun_verification.json`. Original metadata/logs are retained unchanged.

## Execution and cause

Silent canonical8s/192frame/24fps1024x768 input SHAa27d7f2a4d56f144f79c4efbb1740ff59a68e1610b9010f5dc764cb7aa3d0a2c was resized into a separate640x480 model proxy. Native R2V used both VAEs, video+text only, res_multistep, 20 steps, simple schedule, denoise 1 and derived seed; no audio references or custom nodes. Isolated pinned Comfy0.44/Torch2.11cu130 ran on NVIDIA device 0 offline. Actual live schema/prompt validation succeeded. Full video entered the video-VAE reference path; Qwen uses native 2 fps sampling.

Started2026-11-10T05:21:09.912157+00:00; queued05:23:19.133017Z; watchdog stopped05:33:16.562328Z; finalized2026-11-10T05:33:19.110174+00:00. Total729.203s, queued-to-watchdog597.429s. Last3RAM samples1.867908/1.949657/1.961174GiB were each below 2 GiB. Causal `watchdog_failure` is MemoryError. Top-level `Owned Comfy process exited:15` is the shutdown consequence, not a separate compatibility diagnosis; future controller error precedence should preserve watchdog cause as primary. The first sampler iteration (211.54 s) includes initialization and is not steady-state throughput.

## Observations, not minimum requirements

- 403 samples. Available RAM baseline11.017GiB, minimum1.868GiB. Minimum available Windowscommit0.313GiB.
- Whole-device GPU baseline2082.297MiB, peak11743.645MiB; foreign processes included.
- Owned launcher+worker aggregate peak RSS8.397GiB and private/VMS17.963GiB. These are observations in this desktop session, not attributable model minimum demand.
- C: free baseline36.996GiB, transient minimum23.078GiB, after36.586GiB; Dfree after60.122GiB. Paging CIM queries were denied, so transient C-space attribution is not established. No cache/user/model files deleted or paging settings changed. Available RAM recovered to10.638GiB at parent check.

## Verification and changes

Parent independently recomputed all 4 full checkpoint SHA256/bytes (30,766,905,991bytes), all 1332 pinned Comfy source hashes, and reran 6 blind-authored CPU lifecycle cases successfully. Focused independent source review cleared this bounded observed-tree diagnostic after repairs. AST and Ruff no-cache passed for local_h3.py, verify_owned_process.py and verify_local_outputs.py. No production code changed this continuation; existing unrelated work preserved. Full production pytest was not run.

Lifecycle repair caches exact PID+creation-time identities, samples actual worker/tree RSS/private/VMS, preserves failed observations, serializes bounded cleanup and retains final evidence. It is observational, not Windows Job Object containment: unobserved children orphaned between samples and identity-to-OS-operation races remain limits. Acquisition scaffold remains separate/unfinished; completed models were not reacquired. Contracts Amendments2-3 settle sampler/native attenuation and artifact schema. Independent output verifier was repaired without weakening media checks; export success path remains unexercised.

Independent actual-run verification passed frozen input/proxy, full model hashes, native reference graph, pinned provenance and frozen source recheck. Generated-media verification correctly failed because attempt status is failed and output directory is empty. No history.json, editor bundle, native AV, WAV or audition exists. Native attenuation/export quality and exact PCM comparisons therefore remain unverified.

Cleanup terminated actual worker 201000 and launcher 203344, recorded zero survivors/unknowns/failures and joined watchdog. Parent independently confirmed both PIDs absent and no listeners on ports 8293-8297. `parent_postrun_verification.json` records those checks. Original accepted production shot/state/approvals remain unchanged. No creator approval, commit/push/merge/publication.

## Next bounded decision

Do not repeat the same attempt blindly or lower the 2 GiB safety floor to relabel failure. Before another attempt, choose and review one explicit resource strategy (for example, supported native offload or streaming changes or more available host memory), recheck both drives/RAM/commit/VRAM, and preserve this failure. No absolute hardware incompatibility, required RAM figure, or runtime/quality promise follows from this single run.

Actual telemetry spacing: mean 1.677 s, median 1.688 s, maximum 1.875 s. The configured one-second wait is followed by observation overhead; samples were not exactly one second apart.
