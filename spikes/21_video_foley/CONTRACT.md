# Spike 21: local video-conditioned Foley contract

User authorizes testing HunyuanVideo-Foley with our accepted 15-second shot. This is an isolated feasibility spike, not promotion of a production audio adapter. User also states a likely post-Phase1 GUI built on an open-source video editor; preserve editor-neutral interchange now without selecting an editor or building a GUI.

## Inputs and integrity
- Worktree only: D:/Work/video_pipeline/.claude/worktrees/api-conformance. Shared production interpreter may bootstrap ProjectRoot but its packages must remain unchanged. No writes to primary tracked files or ComfyUI. Every controlled write/output/cache/temp/install target passes through ProjectRoot containment. Third-party subprocesses get only contained output/cache/temp/environment roots.
- Input is OUR generated master from spikes/20_overnight_production/_work/20261108T001855_03bdf358/projects/scripted_03bdf358/renders/s1/v0002/3859f220afd54b6f9bcee4a0743336c9/attempt-0001/master.mkv, SHA256 9dbb789b260bfde899a549eaf18cfc20c4d8feed050f59edfeb2f25848f61389. Accepted delivery is post/s1/v0002/4cacb702506344519e0728ef067f41ff/degraded.mp4, SHA256 9860e9ab850b52fb19d03aa9b73aa5b4330f7bc998ec06318ebacae6ea9a8317. Do not mutate source project, original media, approval records or receipts. Creator said the visual passes; record that exact scope in docs, not Phase1 acceptance.
- No reference images/video go to any model. Use a CPU-made clean video proxy from our master, explicit frame rate/time base and verified 15s duration. Preserve original images, video and generated sources.

## Acquisition and isolated execution
- Review official source and actual licences before running. Pin repository revision and every model/component revision; acquire from official repos with authoritative LFS SHA256 or equivalent hash, verify before loading. No automatic unpinned checkpoint downloads. Prefer safetensors; pickle-based tensor state must load with weights_only=True, no trust_remote_code. If unavailable report concretely, do not silently disable verification.
- Separate contained runtime environment under spikes/21_video_foley/_work; existing environment immutable. Detect CUDA/sm_120 compatibility and NVIDIA device. Do not inherit generic CUDA wheel advice for this Blackwell GPU.
- Start with XL + offload, batch1; no other GPU work concurrently. Check live free RAM/VRAM/disk. First bounded short smoke (3-4s); then full15s if feasible, stop on concrete OOM, unsafe low-memory or stalls. Record errors and remediation, not silent fallback to another model. No unbounded retries or OOM loops.
- Runtime adapter may patch isolated vendored source only where required for safety/compatibility, with exact diff and reason. No production files changed.
- Record prompt, negative prompt, seed, steps, precision, offload, source/model hashes, device, versions, wall time and sampled whole-device VRAM/RAM. Label whole-device observations, not attributable requirement. Generation determinism must not be asserted without measured repeatability.

## Review deliverables and interoperability
- Generate at least one video+text conditioned full15s take if machine supports it. Prefer restrained natural residential room tone/ventilation and subtle building creaks; no requested speech/music or mandatory jump scare. Video input affects timing; text is not a promise of exact event placement.
- Retain untouched generated native WAV and, separately, a 48kHz PCM WAV for editing. If resampling/channel conversion/trim/pad/normalization is needed, report explicit transformation and hashes; retain raw. No silent duration repair. Time-aligned start at source video zero.
- Produce an audition MP4 using accepted delivered video with video stream copy, audio encode only. Preserve original video. Do not overwrite anything. Record exact streams/duration/sample count, clipping/loudness/true peak. No human audio approval fabricated.
- Editor-neutral JSON manifest: schema version, shot_id s1, shot_version2, source/master/delivery identities, source frame rate60, delivery rate10, rational timebase or integer sample offsets, relative media paths, SHA256, sample rate/channels/sample count/start sample0, transforms, generation metadata and licence/source provenance. This is a spike interchange proposal, not a new authoritative production schema. WAV+MP4+JSON must reopen without model runtime. Original/generated/processed audio remain separate so a later GUI can adjust/edit without rerendering/regenerating.
- Editor-specific project export (MLT/OTIO/etc), GUI selection and production API promotion are outside this test; record future requirement.

## File ownership and verification
- Parent owns CONTRACT.md, FINDINGS.md, HANDOFF.md and adjudication.
- Implementer owns setup/run harness, runtime patches/manifests and actual execution under spike21 only, no tests.
- Independent verification author owns verify_outputs.py under spike21 only, from this contract; do not read run/setup implementations or fix them. Verify actual media, input/output hashes, source immutability, manifests, no video re-encode, duration/sample/frame counts. Report findings and what could not be verified.
- Read-only reviewer inspects pinned upstream loaders/dependencies/licences and proposed setup, reports without fixing or running GPU. Parent independently reruns output verification and reviews evidence.
- All failures/evidence retained. No commit/push/merge/publication or licence-clearance claims.

## Amendment 1: execution ownership after delegated usage limits

The runtime worker and independent verification author reached provider usage limits after source acquisition/review and verifier authoring. Parent continues operational setup/inference from their saved artifacts because those agents cannot execute further. Parent may make narrow verifier corrections (contained report write, bounded full-master probe timeout, honest native duration reporting) while preserving independently authored checks. No production implementation/tests are being merged; reviewer findings and independent verifier remain evidence.

## Amendment2: bounded duration diagnostic after near-silent15s takes

Both full15s takes ran but produced below-gate near-silent output, retained unchanged. Official maintainer issue23 recommends5-10s single-scene clips and brief prompts; paper describes8s training segments (releasedXL recipe not proven identical). Add one8s movement-window test (global7..15s), explicit source offset7s, brief concrete sound prompt. This is a measured comparison, not a root-cause claim or successful-quality assertion. Native and derivative outputs still separated. If usable, a review-only audition may keep full accepted video with an explicitly declared audio entry at7s; full-episode audio acceptance remains outside spike. No hidden padding/gain to manufacture a passed full15s take.

## Amendment3: diagnostic outcome, not successful audio acceptance

Hardware inference succeeded but tested15s room/fan takes were below gated loudness measurement;8s outcome also too weak for normal audition. Do not manufacture a positive result by normalizing noise. Permit explicit --diagnostic-unprocessed export: native retained, editing48kPCM24 at0dB gain, fullvideo streamcopy+AAC audition, exact15s alignment, standalone editor bundle with quality-target failure labels. This materialization demonstrates interchange, not production-quality audio. Independent verifier must distinguish structural/media integrity from audio-quality acceptance and report overall failure when -18LUFS/TP<=-1 targets are not met. Signed infinite LUFS is represented as a string in strictJSON, compared accurately, never coerced to finite successful levels. No further GPU takes in this increment.
