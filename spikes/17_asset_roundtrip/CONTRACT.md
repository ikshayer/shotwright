# Contract ? real generated asset round trip

User confirmed the handoff is current, use the existing living-room fixture, and visual ratification remains pending. Scope: prove registration ? persisted scene ? real Blender master ? degradation with the existing generated entity before provider work. Do not claim Phase 1 acceptance.

Implementation author owns run_spike.py only. Independent evidence/test author owns verify_run.py only; neither reads the other script. Both may read existing production code and prior spikes. No production changes initially. Report gaps rather than hiding them. Never edit existing projects or main, never access credentials, and do not commit/push.

## Runtime script
- New append-only run directory under spikes/17_asset_roundtrip/_work/<run_id>; refuse reuse. Bootstrap with ProjectRoot(REPO). All writes, including logs, temp/cache paths, font copies and extracted PNGs, pass through ProjectRoot (resolve for subprocess outputs, write_with/write_stream where needed). Redirect TMP/TEMP and Blender config/cache beneath the run. Do not copy raw mkdir/shutil violations from spike 15.
- Reuse the low_key american_living_room fixture with low-light-bw camera/post profile. All mutations through the real CLI main(argv, settings=...), never CliRunner; capture operation JSON and timings into contained files. Set a reasonable nonzero throughput floor; retain existing defaults unless evidence demands a change.
- Register D:/Work/comfyui-trellis/ComfyUI/output/3D/entity_00001_.glb via create-asset, generated=true, fixed asset id entity. Hash and retain source generator PROVENANCE.json. Do not invent CC0/MIT rights for the output: license records restricted internal evaluation only, not cleared for redistribution; model_or_voice_license records the full caveat from PROVENANCE.json (quantisation undocumented, verify before distribution). This scope is only local validation of the existing generated asset; no publication or fresh generation.
- Create a Location (low_key shell) and bind the Shot location_id. Name the registered asset with figure_asset_id on the linear_walk_path anomaly. Preserve the spike15 walk endpoints [1.5,-1,0] to [1.5,1.5,0] for the first integration probe; short probe compresses timing only. Do not silently improve staging before a baseline.
- Default probe duration=0.1s (6 frames at 60fps), event=1/60s and anomaly duration=3/60s: neutral and anomaly are both exercised cheaply. Allow --duration 15 to run full fixture with event8s, duration4s after probe succeeds. Other durations should be explicit and validated or refused. This probe is not an acceptance clip.
- CLI steps: create-project, create-episode, draft-script, create-asset, create-location, propose-shots, build-scene, render-master, degrade-media, report-jobs. Font copied through helper from installed Consolas, record internal-only usage. Read-only use of ProjectStore to locate receipts is fine. Do not use direct worker subprocess as substitute for queue/CLI.
- Record evidence.json schema_version1 containing run_id, run_relative, project_relative, duration_sec, expected_master_frames, source asset path/hash, asset_id, source generator caveat, actual step statuses/timings and receipt references. Fail loudly and retain failure output. Check master frame count and full provenance from actual artifacts. Extract neutral/mid/last master and delivered samples (when unique frames exist), through contained FFmpeg output paths; preserve raw decode hashes. No claims based on EXR file hashes alone. Store sample filenames and observed dimensions/frame counts from ffprobe. Keep small evidence data in run dir, no media committed.

## Independent verifier
- verify_run.py accepts --run-relative path beneath repo; independently loads persisted entities, scene/master/degraded receipts and files, never trusts evidence.json as proof of correctness.
- Assert asset checksum matches registered/source bytes, generated metadata and internal-only caveat persisted, shot references Location and figure asset, persisted scene has exactly one FR20AnomalyFigure asset with checksum/path and no proxy parts, master/degraded receipts bind the current shot version and their hashes match.
- ffprobe actual master/degraded width,height,rates,duration,frame counts, compare to actual profile/plan where meaningful. For probe 6 master frames, low-light output one frame may be expected: report temporal coverage limits honestly. Confirm decoded master neutral/anomaly frames differ; do not confuse metadata differences with image changes.
- Inspect jobs for completed status, terminal error fields and released leases. Report what cannot be verified. All any verifier writes through ProjectRoot. No GPU renders in verifier.

## Verification/adjudication
Orchestrator reviews both scripts and runs probe independently, then independent reviewer inspects evidence and scripts. Report failures, fix production only with separate implementation/test authors and explicit finding. Render full15s only after basic path works, if useful to establish delivered motion and user review. Source package must stay inside new project. Location reuse without rebuild and visual rubric ratification remain unproven.


## Amendment 1 ? measured material round-trip failure
Probe 20261107T211309_a21fb90b registered entity checksum 8ce785cb432a185a31d4d5467b52d90c250dc24aa9af3df57355edadc77a28f6, built the persisted scene, then render job 51ef420ba5d3414e92ed7a367de5ff2a failed before launching Blender. Journal traceback: BlenderAdapter._render_locked revalidates RenderRequest.model_dump(mode="python"); ObjectSpec.nonempty calls .strip() on material=None. Omitted optional material passes initial creation but explicit None in dumped data does not. Lease was released.

Fix contract: material is optional for asset-backed ObjectSpec to preserve imported materials. Explicit None and omitted material must round trip identically through Python and JSON dumps/revalidation, including full RenderRequest loaded from persisted asset-backed scene. Name remains required and nonblank; non-null material stays nonblank; primitive plan hash stays unchanged. Do not suppress revalidation or add a fake material override. Do not broaden primitive validation or change payload serialisation unnecessarily.

Implementation owner may edit ONLY ObjectSpec validation in orchestrator/adapters/blender/scene.py (and owns existing run_spike.py). Independent test author owns new tests/service/test_asset_render_roundtrip.py (and existing verify_run.py); never reads implementation diff or run_spike.py. Regression should reproduce via actual registration+Location/anomaly+build/persist/recompose(require_built_scene=True)+RenderRequest dump/revalidate without GPU, plus minimal ObjectSpec explicit None and empty-name/material checks. Test should fail before fix and pass afterward; parent executes baseline by temporarily isolating source revision if needed. No tests by implementer. Report other defects, do not fix them.


## Amendment 2 ? local nonzero watchdog calibration
The first full15 attempt `_work/20261107T211948_2f2c7900` stopped at91/900frames with job_queue budget_exceeded: a positive progress sample fell below default normalized floor0.001. The lease released. Source artifacts were still advancing; this is not evidence of actual VRAM thrashing.

EXR last-write intervals: successful sixprobe max2.233363628s, median0.423176050s; failed fullprobe max1.362725019s, median0.410635591s. The current per-sample contract allows only1/(900*.001)=1.111s/frame for a900frame shot, shorter than the known first-visible-mesh cost. Full tests were concurrently running; causation is unestablished.

Permit ONE diagnostic full15 rerun after tests finish, with local explicit positive floor1/(900*2*2.233363628387451). This is a derived provisional bound with2x measured worst-sixprobe gap, not a throughput measurement. Do not change production defaults/queue logic, setfloor0, fake progress, or keep widening on subsequent failure. Harness records the override; source failure remains retained. A successful rerun is not default-budget or unattended admission acceptance. Independent read-only reviewer concurred with these limits.


## Amendment 3 ? bounded evidence extraction

All production stages and the original harness's actual 900-frame count completed on the calibrated full run. FFV1 count/decode was expensive, and optional sampling repeated it for every master still. Full-run delivery-only extraction and review-sheet generation are permitted, retaining the six-frame probe's master PNGs and independent full-master decode checks.

The task-owned derived-export Python and ffprobe processes were identity-checked and stopped after production jobs completed; details are recorded in derived_export_stop.json. This was not a production-job cancellation or a wrapper exit 0. Independent media verification exited 0. Native media must never be altered to accelerate evidence creation.
