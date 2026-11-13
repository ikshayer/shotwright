# Spike 17 ? a registered asset reaches a real delivered shot

Run on 2026-11-07. The creator confirmed the handoff was current, authorised the existing living-room fixture, and has not reviewed the visual assessment. **Phase 1 remains unaccepted.**

## Result

The existing generated entity was registered through FR-03, bound to a figure anomaly in a low-key Location, rendered through the real CLI/queue/lease path, assembled into a master, and degraded into a delivered 15-second clip. No production record was patched directly.

Successful full run: `_work/20261107T212741_9657c86e`.

- [Delivered clip](_work/20261107T212741_9657c86e/projects/asset_roundtrip/post/s1/v0001/e0e0e9cd14d24c6f81dafaa4b6555518/degraded.mp4)
- [Review sheet](_work/20261107T212741_9657c86e/review-sheet.png), actual decoded delivery frames with SAR 12:11 display correction; untouched native samples and decoded RGB hashes remain under that run.
- [Evidence](_work/20261107T212741_9657c86e/evidence.json) and [independent verification log](_work/20261107T212741_9657c86e/verification.log).

| CLI stage | Measured elapsed |
|---|---:|
| build-scene | 2.078 s |
| render-master, including assembly | 1047.172 s |
| degrade-media | 186.859 s |

Independently probed delivery: **H.264, 352?288, SAR 12:11, 10 fps, 150 decoded frames, 15.000000 s, 156739 bytes**. Master: **1280?960, 60 fps, 900 decoded frames**. All three job journals resolve to completed, have no terminal error, and have no remaining lease holder. Artifact hashes, scene/worker/renderer-plan lineage, master assembly argv, profile identity, actual stream timing and retained degradation metadata passed independent checks.

**This is functional integration under explicit run settings, not a production resource acceptance result.** The run used a locally derived positive throughput floor and a 3600 s wall-clock budget. Render time exceeds the unchanged 900 s default wall-clock budget. Resource demand declarations remain zero, as in earlier functional probes; this does not establish AT-15 or unattended admission acceptance.

## The real defect found and fixed

First probe `_work/20261107T211309_a21fb90b` registered the asset and persisted its scene, then render job `51ef420ba5d3414e92ed7a367de5ff2a` failed before Blender launched. Its journal records internal/adapter and `AttributeError: 'NoneType' object has no attribute 'strip'`. The lease released.

Imported figures omit a material override to preserve their own texture. ObjectSpec defaulted material to None, but a shared name/material validator assumed str. Initial omitted-default validation passed; the renderer revalidated RenderRequest.model_dump(mode="python"), supplied explicit None, and crashed. Parent independently reproduced the failure from the persisted scene.

The minimal fix separates name/material validators: names and non-null material strings remain nonblank; material=None passes unchanged. No fake override or revalidation bypass was added. An independent test author wrote the regression before implementation. Parent's red baseline was **3 failed, 4 passed**; the fixed focused suite was **61 passed**. The regression registers a tiny asset, composes Location/anomaly, persists the actual scene, reopens the store, recomposes with require_built_scene=True, and revalidates the whole request through Python and JSON.

Pydantic documents the omitted-default distinction in its [official validators documentation](https://docs.pydantic.dev/latest/concepts/validators/#validation-of-default-values). Context7 tools were unavailable, so official documentation was the fallback; the defect was established from actual local execution.

## Six-frame hardware proof

Run `_work/20261107T211806_25f976ce`: scene 1.88 s, render 65.28 s, degradation 3.30 s. Six decoded 60 fps master frames became one 10 fps delivered frame. Master neutral/anomaly pixels differ. Parent visually inspected the imported figure and its cast shadow. The one delivered frame cannot demonstrate delivered motion.

A read-only Blender load of that run's own scene.blend, with autoexec disabled and no render/save, measured actual figure world bounds **0.5 ? 0.349999994 ? 1.799999952 m**. The figure was hidden on frame 1 and visible on frame 4. Its Principled BSDF base-colour image connection survived import. Log: `_work/20261107T211806_25f976ce/audit/blender_inspection.log`.

## Default watchdog failure and bounded diagnostic override

First full run `_work/20261107T211948_2f2c7900` stopped after 91.91 s at 91/900 frames. Job `6a5eafd9503145589772e5b9054d78e8` records budget_exceeded/job_queue with the message "VRAM thrash suspected"; the lease released. Frames were still being produced, so the classification is not evidence of actual VRAM thrashing.

Measured completed-file intervals:

| Run | Median | Maximum |
|---|---:|---:|
| successful six-frame probe | 0.423176050 s | 2.233363628 s |
| failed first full run | 0.410635591 s | 1.362725019 s |

With 900 frames, the existing per-positive-sample contract and default normalised floor 0.001 permit only **1.111 s** per single-frame update. The full regression suite was concurrently running during the first full attempt; its contribution to latency was not established. The successful six-frame probe already showed a first-visible-mesh interval above 1.111 s.

The sole-workload diagnostic rerun used **0.0002487528445856717**, derived as `1 / (900 * 2 * 2.233363628387451)`: approximately 4.467 s between single-frame samples, twice the worst measured six-frame interval. This is a provisional derived bound, not a measured full-clip throughput target. Independent review agreed to one such rerun with the original failure retained. Production defaults and queue logic were not changed; the watchdog was not disabled and the bound was not widened again. The full run's entrance-frame interval was **3.2833739 s** and completed under that allowance.

## Review and remaining limits

The delivered review sheet was inspected. The real mesh is legible around 8.4?10 s, with a scene-consistent shadow. At 12 s it becomes less distinct in shadow near the shelving. Its motion is rigid translation, not articulated walking. These observations do not constitute creator ratification or a rubric score. The room is still the procedural fixture; Script-driven scene authoring is unmet. Location reuse without rebuilding is still unproven against the PRD.

The generator's exact PROVENANCE.json is retained and matched to the original at D:/Work/trellis2-gguf/PROVENANCE.json. Asset checksum: `8ce785cb432a185a31d4d5467b52d90c250dc24aa9af3df57355edadc77a28f6`. Registration records local generation and restricted internal evaluation, not redistribution clearance. The exact upstream/undocumented-quantisation caveat is stored in model_or_voice_license; CC0 or MIT rights were not invented for the output. Consolas is used locally for internal evaluation. No model generation, provider download, credential access or publication occurred.

Two provenance completeness limits remain explicit: Blender GenerationRun is returned in memory but not persisted as a project record, and post-profile metadata retains the filter graph/hash but not the exact FFmpeg argv. The verifier checks the retained renderer/degradation records rather than inventing missing records.

## Verification and reproduction

Parent independently ran the full suite: **1575 passed, 36 skipped, 6 warnings in 430.74 s**. Warnings were one Starlette deprecation and five deliberately malformed Pydantic serializer payloads. Ruff passed for orchestrator/, tests/, and this spike; git diff --check passed. Existing unchanged tests verify the pinned static-plan hash remains `92de3ed358ecc3bc4ef91127db9c6a12254e86a59f255645c03579b60b03788a`.

Use the shared D:/Work/video_pipeline/.venv/Scripts/python.exe. Run from this worktree:

```
python -B spikes/17_asset_roundtrip/run_spike.py
python -B spikes/17_asset_roundtrip/run_spike.py --duration 15 --throughput-floor 0.0002487528445856717
python -B spikes/17_asset_roundtrip/verify_run.py --run-relative <run-relative>
python -B spikes/17_asset_roundtrip/run_spike.py --samples-only <run-relative> --delivery-only
python -B spikes/17_asset_roundtrip/run_spike.py --review-sheet <run-relative>
```

The full harness's optional derived master-sample export was intentionally stopped after all production steps and its actual 900-frame count had completed: it redundantly decoded this large FFV1 master for every sample. Only the verified task-owned Python exporter and its ffprobe child were stopped; jobs, records and media were untouched. See derived_export_stop.json. Delivery-only sample extraction and sheet creation then completed separately. The independent full-master/media verifier exited 0. Do not mistake the stopped export wrapper's exit 1 for a failed production job or claim the entire original wrapper exited 0.

No source-control commit, push, merge or main-branch change was made. Next sourcing work remains Poly Haven first, then Sketchfab.
