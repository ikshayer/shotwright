# FR-04 adjudication and verification - 2026-11-05

Implemented on worktree-api-conformance, based on f0a7116. Main checkout and stabilized
API/service/store/spikes were excluded. Contract and package skeleton preceded work.

## Authorship and adjudication

Separate gpt-6-luna agents owned scene/runner and standalone Blender worker. A different
Luna test author worked in _work/fr04-tests, a detached git worktree with implementation
physically absent. The parent copied tests, adjusted import ordering only, and ran them.
gpt-6.1-sol reviewed read-only; the parent reproduced/adjudicated reports. Final source
review reported no remaining HIGH/MED findings; this is not a guarantee of no defects.

Review fixed exclusive run reservation, same-adapter admission, prelaunch and
post-validation cleanup races, late queue callbacks, and the gap between unload and
lease release. Supervisor epochs plus blocked persisted lease tokens prevent a late
callback from rearming a stopped token; only a genuinely fresh retry token can rearm.
Independent tests gate callbacks before entry, before Popen and during validation,
including a reused adapter while unload has completed but lease release is held.
Cancellation and deadlines retain distinct taxonomy values, and cancellation uses the
existing real ProjectQuarantine. Failure records carry job/shot/run IDs and diagnostics. A secondary failure-record
write error is also surfaced through JobError (including ENOSPC), retaining the
original render failure in its message and exception chain; an independent regression
injects that exact failure.

Other adjudicated fixes: relative saved .blend output destinations; complete metadata
identity/output checks; literal engine/device readback; supported camera limits;
float32-safe readback tolerance; EXR exposure applied in compositor; explicit grayscale
conversion; seed application; and contained launch/runtime paths.

Test corrections were contract adjudications, not implementation-driven relaxation:
unspecified rejection names were pinned, quarantine's existing index prefix respected,
failed-timeout attempts retained, and race clocks became event-driven instead of
assuming the number/order of monotonic calls. Behavioral rejection and cleanup checks
were retained. The running-child cancellation fixture now waits for its own child-start
signal before requesting cancellation, instead of assuming an immediate cancel must
launch a child. API tests were unchanged.

## Actual parent verification

- Whole suite: 991 passed, 28 skipped, 6 warnings in 133.95s. This includes 45 adapter
  cases and the existing 946 tests. The warnings are the existing AnyIO deprecation
  and deliberately malformed Pydantic-payload warnings.
- Whole orchestrator/tests Ruff check passed. The final whole-suite run includes
  the last telemetry-field completion and secondary failure-log ENOSPC regression.
- git diff --check passed.
- Shared interpreter: D:/Work/video_pipeline/.venv/Scripts/python.exe. Tests ran from
  this worktree with TMP/TEMP/PYTEST_DEBUG_TEMPROOT under ProjectRoot-created _work.

## Actual Blender runs (not a throughput benchmark)

Blender 5.2.0 LTS, build fbe6228777e7; NVIDIA GeForce RTX 5070 Ti Laptop GPU explicitly
verified. The test scene has the measured spike's room, 22 seeded props and practicals,
with the documented size=1 cube half-scale bug corrected to actual 6x4x2.7m extents.
Samples/geometry/lighting promotion changes are inspectable in scene.py and worker.py.
No external asset or reference media was used. Color master and monochrome masters
are RGB16 ZIP EXR sequences at 1280x960/60; each listed master has TWO frames only.

| Run | Full adapter wall s | Baseline MiB | Device peak MiB |
|---|---:|---:|---:|
| Eevee color master | 43.407 | 2497.613 | 3682.773 |
| Eevee exposure +1 EV | 42.844 | 2466.957 | 3632.180 |
| Eevee monochrome, explicit RGBToBW | 45.594 | 2523.266 | 3613.570 |
| Workbench one-frame preview | 5.844 | 2517.766 | 2555.582 |

These values include startup/assembly/save/render/verification and cleanup as observed
by the parent harness, not sustained frame cost. Do not extrapolate them to AT-15.
Source records, exact argv, scripts, hashes, metadata, logs and outputs are retained at:

- _work/fr04/smoke-project/verification-smoke-master-1793854932495654800.json
- _work/fr04/smoke-project/verification-smoke-exposure1-1793855189385697900.json
- _work/fr04/smoke-project/verification-smoke-mono-1793855582066302100.json
- _work/fr04/smoke-project/verification-smoke-preview-1793855474821483900.json

Native Blender floating-point EXR inspection measured exposure +1 / exposure 0 = 2.0
and maximum monochrome channel spread = 0.0. See _work/fr04/reopen-report.json.
An FFmpeg conversion path gave different numeric results and was not used to certify
scene-linear exposure; native Blender reads settled this. No pixel equality across
render replays is asserted.

Relocation: copied the retained run into a new project. Reopened .blend matches the
recorded geometry/camera/topology/keyframe fingerprint, keeps // destinations inside
the relocated project, and preserves LINEAR keys with the expected 0.05m displacement.
A relocated Workbench re-render completed in 5.422s (baseline 2472.145, peak 2515.523 MiB),
and a relocated Eevee re-render wrote its EXR and passed the post-render fingerprint
check. That Eevee check's later overly strict pixel assertion failed; its wall/peak
report was not retained. Native inspection subsequently passed. Evidence includes
_work/fr04/relocated-project-1793855386066512100/verification/{inspect.py,reopen.log,rerender.exr}
and _work/fr04/workbench-probe-report.json. Final inspect-only reopen report explicitly
says re_rendered=false; do not confuse that later inspection with the earlier renders.

## Measured corrections, not hidden fallbacks

The original Eevee framing preview hit its 10s hard deadline. A separate leased
Workbench probe demonstrated the same framing geometry/barrel distortion in 5.422s;
then the actual adapter preview completed in 5.844s. Amendment 4 makes Workbench an
explicit flat framing preset; finals still default to Eevee with Cycles opt-in. The
flat studio/deep-focus preview is not look approval for physical lighting or DOF.

Actual Blender runs exposed two startup/save defects not caught by fakes: drawing GPU
identity needs gpu.init(), and default Blender 5.2 saves are Zstd-compressed. Both were fixed.
An earlier run also attempted an external per-user .thumbnails write (denied), a real
containment defect. file_preview_type=NONE, explicit uncompressed saves and contained
XDG_CACHE_HOME removed that attempted write in subsequent logs. All managed output,
log, temp, config and cache paths use ProjectRoot; no new containment exception is
accepted. Transitive Windows/NVIDIA driver cache activity was not audited.

## Operational limits and unfinished acceptance

- JobQueue's existing one-second stall watchdog is incompatible with Blender startup.
  Adapter requires throughput_floor_per_second=0 before any writes. Hard deadlines,
  cancellation, token-fenced supervision and measured lease release remain active.
  A startup-aware throughput watchdog remains unimplemented; do not claim FR-12 fully
  validated by these adapter tests.
- Calibration smoke Jobs declared zero resource demand because no measured Blender
  reservation existed. They verify execution/provenance, not production demand admission.
  Device peak is NOT a measurement of a job's required memory. Obtain production job
  reservations separately; no invented footprint is supplied by this package.
- Cycles OptiX selection is implemented/read back, but no actual Cycles render ran.
- AT-15 sustained >=120-frame performance, a finished15-30 second shot/rubric, arbitrary
  scene cost, forced machine crash/power loss and real resource exhaustion were not run.
- Driver-managed cache writes are unverified; shared runtime directories being empty
  is not proof that Eevee cache reuse or driver cache containment works.
- API build_scene/render_preview remain 501: API files were explicitly off limits.
  Service mutation/approval integration must be designed separately; this adapter
  never revises/approves domain records or bypasses their mutation tokens.

Outside the requested blender/ directory, changes are only the adapters package
initializer, independent tests/adapters/blender and HANDOFF.md. No taxonomy, settings,
service, store, API, PRD or spike internals changed. Context7 tools were unavailable;
Blender 5 API details were checked using official release notes and local 5.2 reflection:
https://developer.blender.org/docs/release_notes/5.0/python_api/
https://developer.blender.org/docs/release_notes/5.0/migration/compositor_migration/

## Startup-aware throughput-floor follow-up — 2026-11-05

The earlier operational-limit statement above is superseded: Blender now accepts a positive configured floor and passes the measured 74.0 s §8.4 cold shader/kernel allowance to JobQueue.run. Independent tests in 	ests/adapters/blender/test_supervision_contract.py exercise a positive-floor render through the real queue and assert the forwarded allowance. The queue suite independently covers silent startup before allowance, startup expiry, post-startup silence, below-floor later progress, confirmed termination and lease retention when stop is unconfirmed.

Final verification after this change: whole suite 1123 passed, 29 skipped, 6 existing warnings; whole orchestrator/tests Ruff check passed. No Blender/GPU hardware was run for this follow-up. Production resource reservations remain unmeasured.
