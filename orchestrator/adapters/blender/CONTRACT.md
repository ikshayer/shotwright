# FR-04 Blender adapter contract

Written by the orchestrator before implementation. Tests have a separate author in
an isolated copy without implementation. Normative PRD: FR-04, 4.4, 8.3, 8.4.
This package promotes spikes/02_render_budget/{synthetic_interior,render_probe}.py;
spike 04 consumes the same greybox geometry. Do not modify spikes, API, service or store.

## Scope and interfaces (binding)

`orchestrator.adapters.blender.scene` contains Pydantic v2 frozen extra-forbid models
(all numeric fields reject NaN/infinity), pure planning, no bpy and no filesystem I/O:

- `MaterialSpec(name: str, color: tuple[float,float,float,float], roughness: float=0.7)`.
  Color channels and roughness in [0,1]. Names unique, nonempty.
- `KeyframeSpec(frame: int, location: tuple[float,float,float])`, frame >= 1.
- `ObjectSpec(name: str, kind: Literal['box','cylinder'], location: Vec3,
  dimensions: Vec3, material: str, keyframes: tuple[KeyframeSpec,...]=())`.
  Dimensions strictly positive; duplicate keyframes rejected.
- `LightSpec(name: str, location: Vec3, energy: float, color: RGB,
  size: float=1.1)`, nonnegative energy, positive size, channels [0,1].
- `SceneSpec(materials: tuple[MaterialSpec,...], objects: tuple[ObjectSpec,...],
  lights: tuple[LightSpec,...], camera_location: Vec3, camera_target: Vec3)`.
  Validate material references, globally unique object/light names, noncoincident
  target and camera. Geometry explicit, editable and serializable.
- `RenderRequest(shot_id: str, shot_version: int, run_id: str, degrade_seed: int,
  motion_seed: int=0, motion_model_semver: str='0.1.0',
  duration_sec: float, camera_profile: CameraProfile, scene: SceneSpec,
  mode: Literal['master','preview']='master', engine: Literal['eevee','cycles']='eevee',
  samples: int=32, gpu_device: str='NVIDIA GeForce RTX 5070 Ti Laptop GPU',
  master_resolution: tuple[int,int]=(1280,960))`.
  IDs match [A-Za-z0-9_-]+, version >=1, both seeds >=0, motion model semver is
  MAJOR.MINOR.PATCH, duration finite >0,
  samples >=1, GPU name nonempty. Master resolution positive integer 4:3, at least
  1280x960, independent of CameraProfile. Preview is always Eevee and one frame.
- `build_scene_plan(request: RenderRequest) -> dict[str, object]`: deterministic JSON
  compatible payload with schema_version=1, shot_id, shot_version, run_id, seed,
  engine, mode, width, height, fps, frame_start, frame_end, samples, gpu_device,
  scene (serialized SceneSpec), camera (rebuild fields only).
  Use derive_seed(shot_id,degrade_seed,'blender',INT32_MAX). Master 1280x960 MINIMUM,
  4:3 square pixels, 60 fps, max(1,ceil(duration_sec*60)) frames. Dimensions come ONLY
  from request.master_resolution, never CameraProfile post fields. Caller chooses a
  master capacity >=2x the largest delivery profile BEFORE rendering (default covers
  all PRD 4.2 profiles). Preview 640x480, 60 fps, one frame. No post fields in camera
  or elsewhere in plan, including storage_resolution. Changing ANY post-only field
  gives identical scene plan. Scene plan is not an invalidation engine.
- `synthetic_interior() -> SceneSpec`: promote the spike fixture (room surfaces,
  22 clutter props with local seeded RNG and camera clearance, practical panels,
  muted materials, high corner camera). Explicitly document the spike's size=1 cube
  half-size bug if fixed: dimensions must describe the stated 6x4x2.7m room.

`orchestrator.adapters.blender.runner`:
- `build_command(blender_exe: Path, script_path: Path, plan_path: Path) -> list[str]`:
  argv only, no shell: exe, --background, --factory-startup, --disable-autoexec,
  --python-exit-code, 1, --python, script_path, --, plan_path.
- `BlenderAdapter(root: ProjectRoot, settings: Settings, *, popen=subprocess.Popen)`.
  `.worker` implements existing jobs.lease.WorkerControl. Its idle PID is the live
  Python supervisor PID; lease belongs to that supervisor before Blender GPU startup.
  Child PID is recorded separately in provenance. No dummy GPU worker process.
- `.render(request: RenderRequest, job: Job, *, queue: JobQueue,
  cancel: Callable[[],bool] | None=None) -> RenderResult`.
  Require queue.root path == root, queue.lease.worker is adapter.worker, same settings,
  and correlated job.shot_id/run_id matching request; reject before queue.run/writes
  otherwise. Actual GPU execution ONLY via queue.run (preflight, persisted lease,
  heartbeat, retry/budgets, measured release). Adapter must not bypass or recreate queue.
  Caller supplies measured resource reservations in Job; package invents no footprint.
  Queue starts the supervisor lease before child launch. One active render per adapter;
  concurrent reuse rejected. Domain entity revisions/approvals never occur here.
- `RenderResult` frozen dataclass: `blend_path: str, frame_paths: tuple[str,...],
  metadata_path: str, log_path: str, generation_run: GenerationRun`.
  Paths relative to project; run includes version/build identity, script SHA256,
  plan digest, seed, exact argv, input/output refs, actual elapsed time, child PID,
  device/baseline/peak VRAM when probe allows. Record version/build even on failure
  in worker metadata emitted before assembly. Failure never returns RenderResult.

## Containment and retained runs

Output base: renders/<shot_id>/v<shot_version:04d>/<run_id>. Reject an existing run
before queue execution; never overwrite an older run or artifact. Automatic queue
retries use attempt-0001, attempt-0002 etc below that new base, preserving all attempts.
Copy the worker source into each attempt as a retained script; record SHA256.
Plan, metadata, logs, .blend, every frame and Blender runtime/cache/temp directories
are resolved through ProjectRoot. Use write_with for every external output and pass
ONLY those authorized absolute forward-slash paths to the subprocess plan. Do not
compose unchecked output paths inside Blender. No OS tempfiles or shell redirection.
Root.make_dir handles all intermediate dirs. Environment redirects TEMP/TMP/TMPDIR,
Blender user config/scripts/datafiles/extensions, CUDA/OptiX shader caches under attempt.
Disable bytecode/autosave/backups. Worker has only stdlib + Blender dependencies; do not
import orchestrator (Pydantic is absent inside Blender) or install bpy in orchestrator.
The write_with boundary authorizes external Blender writes, as its docstring intends.
A failed attempt is retained and labeled failed, never published as successful.

## Blender worker (standalone worker.py)

No bpy imported by package imports. Executed in Blender using the retained plan.
Adapt measured construction/target quaternion and engine assign-and-read-back logic.
Try BLENDER_EEVEE then BLENDER_EEVEE_NEXT; no silent CPU/engine fallback.
For Cycles require OPTIX, enumerate devices, disable ALL others incl CPU/iGPU, select
only exact requested name, assert selection. For Eevee check gpu.platform.renderer_get()
against requested device (normalize NVIDIA GeForce/RTX branding where needed); refuse
an iGPU. Record actual device, bpy.app.version and build_hash on every run.

Create explicit primitive geometry, materials, lights, camera and location keyframes.
Keyframes use linear interpolation. Render neutral Standard view, no overlays/grade,
4:3 square pixel, master OPEN_EXR RGB 16-bit ZIP sequence (allowed by 8.3).
Preview flat grey surfaces, no surface texture/raytracing, one PNG.
Apply lens mm, camera_position override, exposure_ev (zero is neutral baseline),
barrel distortion via compositor Lens Distortion, light intensity multiplier and
practical light color using temperature multiplier, monochrome_night deliberate
lighting/color response, DOF (positive focus distance meters; fstop fixed documented).
Camera mount is descriptive provenance, placement lives in camera_position/SceneSpec.
Unsupported/invalid values fail explicitly; do not silently drop rebuild fields.
Save editable .blend BEFORE render with final output settings and keyframes. Disable
save-version backup. Output paths enumerated per frame, set scene.frame_set and
render.filepath each time; emit progress to stdout, update job.progress in supervisor.
Metadata includes a stable camera/geometry fingerprint and actual applied settings.
For saved .blend reopening, geometry/camera match is required, pixels are NOT compared.

## Supervision and errors

Thin injectable Popen boundary. Capture stdout+stderr to contained file, never unbounded
PIPE that deadlocks. Poll and sample NVML while alive; stream progress from worker
lines or frame appearance so queue watchdog sees real work. Enforce own deadline:
preview <=10s (including startup, so cold cache may legitimately fail); master <=
settings.budget.per_shot_wall_clock_seconds. Cancellation -> terminate, wait, kill+wait
if necessary. No returning/releasing while child can still write. WorkerControl.unload
must stop/join child even for queue timeout. Failed termination retains the lease.
Queue's one-second throughput watchdog is an existing integration constraint; caller
may configure floor=0 for known startup/warmup rather than fake progress.

Map failures to EXISTING JobErrorKind, correlated job_id/shot_id/run_id:
missing executable/launch failure -> worker_unreachable; timeout -> budget_exceeded;
user cancellation -> cancelled; OOM signatures -> out_of_memory;
ENOSPC/disk-full -> disk_exhausted; unsupported engine/device or invalid scene ->
invalid_manifest; successful exit with missing/empty/malformed output -> corrupt_artifact;
other nonzero exit -> internal. ContainmentError and input ValidationError stay request
failures. Never add taxonomy entries or swallow unknown errors. Verify metadata identity,
expected frame set, .blend magic and frame magic (EXR / PNG), nonempty outputs before
success. Failure JSONL includes JobError.to_log_record and underlying diagnostics.

## Independent negative tests and verification

Tests author may read this contract, preexisting domain/platform/jobs, spikes and PRD,
not adapter implementation. Own tests/adapters/blender/** only. Test pure plan,
post-field independence, seeds/ranges, dimensions, validation, command/paths, append-only
runs/retries, fake process failure/cancel/timeout, supervision cleanup, queue/lease
wiring and output verification. Fake process must honor real Popen signature/file logs;
do not fake away lease acquisition, resource preflight or running-child cleanup.
Optional real Blender smoke is opt-in and isolated, never a mandatory GPU suite test.
Review is read-only; report findings without fixes, and always state what was unverified.
Parent reruns whole pytest, ruff and diff check. Real smoke should run tiny master and
preview, then reopen .blend to compare geometry/camera and record wall time/peak VRAM
against measured foreign baseline. This smoke is NOT AT-15's >=120-frame budget run.
No API/CLI wiring, creative approval or Phase 1 acceptance is claimed by this package.

## Amendment 1 - contract review adjudication (binding)

1. Preview requests with engine='cycles' produce an Eevee preview plan (coercion is
   explicit by preset, not a runtime fallback). A master cycles request never falls back.
2. Queue timeout can fire before Popen or after child exit. WorkerControl.unload and
   terminate must signal a persistent stop condition and wait for the entire attempt
   supervisor to become quiescent, including its prelaunch/final-publication sections.
   No attempt thread may subsequently launch a process or write after release returns.
   Avoid self-join deadlocks when cleanup originates from the attempt thread itself.
   Race tests must cover timeout immediately before launch and before publication.
3. Actual subprocess render destinations are authorized absolute paths. The PERSISTED
   .blend must save scene.render.filepath using Blender // paths relative to itself,
   so copied projects never target the original root. Relocation verification must
   inspect saved destinations and reopen the relocated .blend. No absolute external
   references are introduced by the procedural scene.
4. Until existing JobQueue has a startup grace interval, BlenderAdapter.render requires
   settings.resources.throughput_floor_per_second == 0, rejected before writes/launch
   otherwise. This is an explicit current integration limitation, not a fulfilled
   throughput-watchdog claim. Own hard deadlines/cancellation remain active. No fake
   progress to defeat the watchdog. API/settings/jobs internals stay unchanged.
5. Pass the reserved run directory to queue.run(partial_artifacts=...) so cancelled
   outputs move through existing ProjectQuarantine. Cancellation records retained
   inside that quarantined tree are not successful runs.

## Amendment 2 - subprocess data schema and cache (binding)

Executable worker plan is the pure scene plan plus `outputs` with exactly named
`metadata: str`, `blend: str`, `frames: list[str]` authorized absolute paths, and
`runtime` with authorized directory paths including `attempt_dir`, `temp_dir`,
`cache_dir`. Additional runtime directories may be present. No runtime path affects
scene determinism. Fake subprocess tests read this schema from argv[-1].

Worker metadata has `schema_version: 1`, `shot_id`, `shot_version`, `run_id`,
`tool_version: str` (Blender version), `build_hash: str`, `actual_device: str`,
`applied_settings: dict` including `engine`, `width`, `height`, `fps`, `samples`,
`geometry_fingerprint: str` (SHA256), `child_pid: int`, `elapsed_seconds: float`,
`status: 'running'|'completed'|'failed'` and `error` when failed. Worker writes initial
identity metadata before scene construction, then terminal metadata. Supervisor
requires matching schema/run/shot/version and child_pid, nonempty version/build/device,
correct applied preset dimensions/fps/samples/engine, valid fingerprint and completed
status for success. Real Eevee engine values are BLENDER_EEVEE or BLENDER_EEVEE_NEXT;
Cycles is CYCLES. Requested device match is also verified. Failure metadata retains
known identity even when assembly fails. GenerationRun enrichment uses these fields;
it may add telemetry/provenance keys to metadata through contained writes.

Revalidation at render entry reconstructs request from serialized fields (model_copy
and nested mutable profiles cannot bypass checks), snapshots the plan once, and checks
invalid rebuild values before writes. No nonfinite numeric values, DOF <=0, invalid
resolution etc may reach Blender silently.

Shader caches may be SHARED within ProjectRoot under runtime/blender/<identity>/
instead of per-attempt. Identity includes executable file content digest, worker source
digest and requested device. This permits the measured one-time warmup cost to amortize
across renders. Record cache identity and actual Blender build/device; shader/driver
native caches retain their own version checks. Temp, config and output remain per attempt.
This corrects the initial per-attempt-cache instruction, which could prevent every
preview from warming. Every cache directory still passes ProjectRoot.make_dir.

## Amendment 3 - independent-test adjudication (binding)

Duplicate/preexisting run directories raise FileExistsError; simultaneous use of one
adapter raises RuntimeError before writes. Queue/request mismatches and unsupported
watchdog settings raise ValueError. These exception names were unspecified initially;
behavioral rejection/retention requirements are unchanged. Run reservation must use
exclusive creation of the directory via ProjectRoot.write_with, never adopt an existing
empty/unmarked directory.

Timeout preserves the failed run/attempt and reports budget_exceeded. It does not
require deleting renders/, and it never becomes cancelled merely because the supervisor
stops a child. Only user cancellation uses existing quarantine. ProjectQuarantine names
its moved entry with an index prefix (0000-<run_id>); test the existing implementation's
actual layout. Race tests advance a controlled queue clock at the intended barrier,
so they test that phase rather than whichever phase happens to take one millisecond.

Adapter supports practical color temperature [1000,40000] K (the chosen conversion's
supported range) and radial distortion [-1,1]. Other values explicitly fail before
writes; these are supported-input limits, not measured hardware figures. Position
overrides coincident with scene target also fail before writes. Blender worker must
verify assigned socket/property values read back unchanged (within float tolerance).

## Amendment 4 - measured framing preset correction (binding)

Actual supervised 5.2.0 renders: two-frame Eevee master 43.407s, monochrome master
43.984s; first one-frame Eevee preview exceeded the hard 10s budget. Shared runtime
cache held no shader artifacts; do not claim a warmup/cache solution was measured.
A leased Workbench probe reopened the SAME saved geometry/camera in a relocated
project, retained the fingerprint and linear animation, rendered 640x480 PNG with
visible barrel distortion, and exited in 5.422s. Baseline2472.145MiB, peak2515.523MiB.
Evidence: _work/fr04/workbench-probe-report.json and relocated-project-1793855273670430700.
These are short functional probes, NOT AT-15 measurements or production reservations.

PRD8.3 defines a separate no-material flat framing preset; only FINAL renders mandate
Eevee as default. Therefore supersede earlier 'preview always Eevee' instructions:

- build_scene_plan(request).engine is 'workbench' for preview, regardless of requested
  master engine (eevee/cycles). Master engine behavior and request field enum unchanged.
- Worker selects BLENDER_WORKBENCH by assign/readback, verifies requested GPU explicitly,
  and records actual engine. No runtime fallback. No unavailable-engine substitution.
- Preview uses studio SINGLE grey shading, shadows/outlines and FXAA (single draw pass),
  640x480 PNG, one frame; no texture/PBR/material or ray-tracing evaluation. Record
  preview shading/AA semantics in applied_settings. 'samples'=1 denotes this one-pass
  framing preset, not a Cycles/Eevee ray-sample measurement.
- Preserve geometry, lens, camera position/target, aspect, action timing and compositor
  barrel distortion. The flat/deep-focus framing preview does not promise the final
  physical lighting, exposure appearance or DOF blur; those are checked on the master.
  Requested settings remain recorded and applied to the saved camera/scene, but preview
  appearance is explicitly a geometry/framing check, not look approval.
- Parent must actually run the final adapter preview (not only this probe) under10s,
  inspect the image, and verify retained relative destinations/fingerprint.
- Existing10s deadline stays hard. No progress faking or budget extension.

Actual runtime fixes also binding: gpu.init() before renderer query; Blender save
compress=False plus preferences.use_file_compression=False; file_preview_type='NONE'
to suppress per-user thumbnails; XDG_CACHE_HOME contained at launch. The default
compressed5.2 blend began with Zstd rather than BLENDER magic, so compression is
explicitly disabled instead of teaching tests to accept arbitrary headers.

## Amendment 5 — startup-aware throughput floor (binding)

The zero-only throughput-floor limitation in Amendment 1 item 4 is superseded. BlenderAdapter.render accepts a positive configured floor and passes startup_allowance_seconds=74.0 to queue.run, grounded in PRD §8.4’s measured ~74 s one-time cold shader/kernel cache cost. Queue watchdog behavior, hard deadline, progress reporting, cancellation, supervision, fencing, lease handling and output behavior remain unchanged. Do not fake progress or claim measured production resource reservations.

## Amendment 6 — artifact-counted progress and derived startup bound

Amendment 5's 74-second startup allowance is superseded. BlenderAdapter reports progress
from completed expected frame files divided by the plan's `frame_end`; it samples the
contained frames directory on each process-monitor iteration and never parses stdout for
progress. The 150-second startup allowance is a derived bound just over twice the measured
~74-second cold-cache cost in PRD §8.4, not a measurement. It only needs to cover startup
until the first frame appears. Under `JobRunner`'s managed queue, a preview's hard deadline
is that configured startup allowance plus the existing 10-second render/verification
window. Direct adapter previews retain their existing 10-second hard deadline. Scene
assembly is a separate no-process, no-lease operation and is not part of
`BlenderAdapter.render`.
