# Positive-control follow-up contract (2026-11-09)

User explicitly asks to run a clear sound-producing-motion control to distinguish a clip-specific failure from the runtime/model. This authorizes this follow-up after prior increment ended. Keep previous evidence unchanged and do not reinterpret diagnostic quality failure as success.

## Scope and inference boundaries
- Worktree D:/Work/video_pipeline/.claude/worktrees/api-conformance ONLY. Shared production Python D:/Work/video_pipeline/.venv/Scripts/python.exe -B bootstraps ProjectRoot. Every controlled write/cache/temp/log/output uses ProjectRoot. No production code, .venv or ComfyUI modifications. No new model weights or runtime packages required. No commit/push/merge/publication.
- Create a new independently owned synthetic8s scene: recognizable orange basketball with black seams repeatedly bouncing on clearly visible wooden floor. Bright lighting, fixed camera, clear floor contact/shadows. Frame25fps,200frames,640x480 or512x384. Use Blender5.2 BLENDER_EEVEE detected by assign/readback, no separate GPU inference concurrently. All geometry/materials procedural and original; no third-party reference media goes to model. Own scene is a diagnostic input, NOT already known-good audio output; if it fails this alone cannot prove generic model failure.
- Authored contact times0.8,2.2,3.6,5.0,6.4,7.8seconds (frame indices20,55,90,125,160,195 at25fps). Ball has visible airborne arc between impacts and contact at floor. Store procedural parameters and exact actual frame positions/contact heights with source hash. Camera must show full ball at apex and impacts; parent visually inspects frames/contact sheet before inference. No animation audio in model input.
- Retain silent video, exact renderer metadata/provenance/frame count, snapshot/contact sheet. Optional independently procedural synthetic impact guide track can serve timing QA only, MUST NOT be fed to model or mixed into generated output.
- Reuse pinned offline XL runtime with safe loaders/meta initializer, same seed210915,50steps,guidance4.5 and watchdog as prior test. Prompt brief: Basketball bouncing on a wooden floor. Run one8s control; if valid and useful, compare. If weak or native error, do not blind-retry. Independent read-only review may identify a concrete implementation/runtime defect; adjudicate targeted verification before changing anything.
- Additional bounded diagnostic only if justified by first control: compare simple text-only conditioning or same8s original movement window with matched prompt/config; record changed variables. No downloaded reference/model-positive claim without verified rights and direct authorization scope.

## Measurement and interpretation
- Retain raw48kfloatWAV with unchanged samples. Measure duration,sample count,DC,RMS,true peak,integratedLUFS,clipping,segment energy over authored event windows. Do not use normalization to classify native success. Do not equate audible loudness with correct or synchronized impacts; human hearing/semantic approval is separate.
- Parent independently rechecks raw metrics/frames/sourcehash. If control is non-silent while CCTV takes near-silent, this supports scene/conditioning sensitivity but not proof that original video alone is at fault (prompt also differs). If both fail, runtime/model issue remains possible; synthetic domain can also be limiting. A true positive baseline requires known-success authorized input/output or independently validated reference implementation. State limits accurately.
- Produce original-control-video+generated-audio audition and separate editWAV/manifest (any gain/filters explicit, raw retained), standardmedia relativepaths and sampletimebase. Existing15s-specific finish/verifier must NOT be forced onto8s control or altered to fabricate acceptance. A dedicated controller/verifier may be added at a real responsibility boundary; reuse common runtime, avoid parallel production abstraction.

## File ownership and separation
- Parent owns this contract, FINDINGS/HANDOFF and GPU orchestration/inference/physical review.
- Implementer owns create_positive_control.py (+worker if real boundary) in spike21 only. Implementer must not write its tests. It may run only the controlled Blender scene build/render after reporting paths; no model inference or production files.
- Independent verifier author owns verify_positive_control.py in spike21 only. From this contract, verify physical video and WAV duration/integrity/levels, timed event-window energy, and audition compressed video packet identity. Do not read implementer code, do not repair it; report findings. No model/GPU execution.
- Read-only runtime reviewer may inspect existing run_foley.py versus pinned upstream for concrete defects that could explain near-silence. Report, do not fix. Parent adjudicates and verifies every claim.
- All agents explicit workdir and ProjectRoot; quota failure allows documented parent operational takeover because unavailable agents cannot write. No unbounded waits, OOM loops or fake human acceptance.

## Amendment1: bounded negative-prompt control and file ownership

Review found earlier defaultnegative includes loud impacts, a potential contradiction forbasketball. Add optionalnegativepromptCLI preservingexactprior default. Run at most2basketballtakes with identical video,positiveprompt,seed,steps,runtime: A priornegative and B neutral speech,music. This is an explicit controlled factor. Ifone is usable, at most1matched neutralnegative rerun of prior8s CCTVmovementwindow is authorized to compare against its existing oldnegative baseline. Do not claim video alone caused differences whenprompt/task also differs. Stop further inference after this bounded matrix.

foley_runtime owns run_foley.py/launch_runtime.py negativeCLIextension and a new prepare_control_audio.py for8s-specific original/processed audio/audition/portablemetadata (real responsibility boundary; do not force15s finish pipeline). It writes no tests. Independentcontrol_verify owns verify_positive_control.py only. Parent executesactualinference andverification. Allotherfilepartitionsunchanged.

## Amendment2: preserve decoder input for bounded diagnosis

Optional --save-latents (defaultoff) saves exact decoder input as a project-contained safetensors artifact with hash/shape/dtype/device in generation.json. Detach/copy only; no noise/RNG/GEMM changes. A/B both use this flag. It permits later component-level diagnosis without repeating full diffusion. No procedural guide sound is mixed into model outputs.

## Amendment3: conditional independent codec positive control

Ifbothbasketballtakesremainweak, one boundedmodel-componentprobe is permitted instead ofmoreblinddiffusionreruns. Independently authoredprobe creates OUR OWN2s sine/percussiontestsignal andround-tripsitthroughpinnedVAEencoder/posterior.mode/decoder. This signal is explicitlyNOT HunyuanFoleygeneration, neverfedtodiffusion, nevermixedintobasketballaudition, neverpresentedasmodel-generatedsound. Itis notthird-partyreferencemedia. OptionalsamecapturedlatentCPU/GPUdecodecomparisoncanisolatebackenddifferenceswithoutnewdiffusiongeneration. Recordhashes, rawmeasurements andlimits; no unsupportedpassmetricsorperceptualclaims. Existingmemory/timewatchdogpolicyapplies. No furtherfullmodelgenerationbeyondAmendment1matrix.
