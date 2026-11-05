# FR-06 ComfyUI still-image adapter contract

Orchestrator-authored before implementation/tests, 2026-11-05. PRD FR-06,
FR-12/16/25/26/27, 8.2-8.4 are normative. Promote measured spikes 03/04;
do not modify Blender, API, service, store, settings, taxonomy or spikes.

## Scope

P0 TEXT-TO-IMAGE only using FLUX.2 klein 4B. No arbitrary caller graph,
image input (including reference media), downloads, video/3D, API integration,
entity mutations/approval, ControlNet, ReferenceLatent or continuity claims.
Tiled decode is mandatory. Own a resident server; never adopt another server.
No invented resource demands: caller supplies Job declarations. Existing
JobQueue lacks startup grace; require throughput_floor_per_second=0 BEFORE
writes. Hard wall deadline/cancellation remain enforced. Calibration jobs with
zero demand are explicitly not production resource-admission validation.

## Files and public interfaces

`orchestrator/adapters/comfyui/workflow.py`: pure Pydantic v2 models, frozen,
extra-forbid, reject nonfinite numbers, with revalidation at adapter entry.

- `ModelPin(role: Literal['diffusion','text_encoder','vae'], path: Path,
  sha256: str, source: str, license_id: str)`. Path absolute, safetensors only;
  hash exactly 64 lowercase hex; source nonempty HTTPS URL (provenance, never
  fetched), licence nonblank. These fields are trusted deployment configuration,
  never filesystem destinations. Missing licence raises validation error.
- `ImageRequest(shot_id: str, shot_version: int, run_id: str, prompt: str,
  degrade_seed: int, models: tuple[ModelPin,...], width: int=1280,
  height: int=960, steps: int=4, precision: Literal['default','fp8_e4m3fn','fp8_e5m2']='default',
  memory_mode: Literal['normal','lowvram','novram']='normal', gpu_device: int=0)`.
  IDs [A-Za-z0-9_-]+; version>=1; seed>=0; prompt nonblank; exactly one pin
  per role. Width/height multiples of 16, 64..2048 inclusive, steps 1..100;
  GPU index>=0. Only measured model names: diffusion flux-2-klein-4b.safetensors,
  encoder qwen_3_4b_fp4_flux2.safetensors or qwen_3_4b.safetensors,
  VAE flux2-vae.safetensors. Other geometry/precision is supported configuration,
  not a claimed measured completion envelope. No silent lower-memory fallback.
- `build_workflow(request: ImageRequest, object_info: dict, *, output_prefix: str)
  -> dict[str, dict]`. Promote spike03 nodes 1..13, replacing node 12 with
  VAEDecodeTiled (tile_size=512, overlap=64; remaining required tiling defaults
  read explicitly from object_info). Derive noise seed with existing derive_seed
  using stage='comfyui', max_value=2**64-1. CFG 1.0, euler, CLIP type flux2,
  one image; Flux2Scheduler has NO model input. Output prefix is safe project-
  relative subpath without interpolation, absolute paths or traversal.
  Inspect all graph node signatures in supplied /object_info. Missing node,
  unknown supplied input, invalid combo/range, or required input with no declared
  default raises JobError(INVALID_MANIFEST); missing named model combo ->
  MISSING_MODEL_ARTIFACT. Explicit documented defaults may fill required inputs;
  never choose an arbitrary first combo. Return deterministic JSON-compatible graph,
  without mutating request/object_info; runner records graph and schema/defaults.

`orchestrator/adapters/comfyui/runtime.py`: stdlib HTTP and owned server control.

- `HttpTransport(port: int)` with `request(method: str, path: str,
  payload: dict | None=None, *, timeout: float=5.0) -> dict`.
  Only 127.0.0.1/settings port and relative API paths; no proxy inheritance,
  redirects, caller URL/host or unbounded network timeouts. Empty response -> {}.
  Invalid JSON/non-object -> CORRUPT_ARTIFACT. HTTP 400/other 4xx ->
  PROVIDER_HTTP_4XX, 429 -> PROVIDER_RATE_LIMIT, 5xx/network -> WORKER_UNREACHABLE.
- `build_command(settings: Settings, *, runtime_dir: Path, model_config: Path,
  gpu_device: int, memory_mode: str) -> list[str]` pure argv, shell=False:
  portable python_embeded/python.exe -s -B ComfyUI/main.py; explicit loopback,
  configured port, --disable-auto-launch --preview-method none --disable-all-custom-nodes,
  --cuda-device INDEX, --base-directory runtime, --input-directory runtime/input,
  --output-directory runtime/output, --temp-directory runtime/temp,
  --user-directory runtime/user, --extra-model-paths-config model_config,
  --cache-none; --lowvram/--novram only when chosen. P0 uses core nodes only;
  whitelist is explicitly empty, no custom nodes directory from ambient install.
  Do not use windows-standalone-build if it downloads/updates frontend assets.
  All managed runtime/cache/temp/config/log writes contained, bytecode disabled,
  offline HF/transformers environment. No install-tree mutation or installations.
- Extend the existing ComfyWorkerControl responsibility (subclass permitted; don't
  change jobs.lease in this scope). Override unload to POST /free with
  {'unload_models':True,'free_memory':True}; readiness and supervision must be real,
  no default no-op launch/stop/respawn. Idle prelaunch PID may be orchestrator's PID
  as Blender does: start child only after real persisted lease ownership.
  WorkerControl PID identifies lease supervisor; child PID recorded separately.
  A resident process may remain only with models unloaded after measured release.
  Initial/restart boot readiness uses /system_stats, requires version match 0.21.1,
  within whole call deadline (startup cap no greater than that). Reject occupied port
  before Popen; no adoption/interrupt/free of unrelated server. Confirm child still
  alive after readiness. Runtime directory unique; outputs/config persist.
  Model config is JSON-as-YAML: only exact pin parent directories for three categories,
  is_default=true, base-directory excludes ambient model/custom-node roots. Reject
  any install-local extra_model_paths.yaml that would silently add ambiguous roots;
  configured settings extra_model_paths/model_roots may not silently override pins.
  Do not copy/link full weights into project. Hash all three actual files before
  submission and after completion, detect change. All paths resolving to pins must
  be unambiguous; filename combo membership alone is insufficient identity proof.
  Reuse resident process only for identical model identities/GPU/memory launch config;
  otherwise stop/reconfigure under lease. Respawn after CUDA errors is mandatory.
  Lease release owns NVML measurement and respawn decision. respawn under release
  may defer actual launch until next owned attempt, returning live supervisor PID;
  it must kill old context now and never resurrect cancelled attempt.

`orchestrator/adapters/comfyui/runner.py`:

- `ImageResult` frozen dataclass fields `image_paths: tuple[str,...]`,
  `workflow_path: str`, `metadata_path: str`, `log_path: str`,
  `generation_run: GenerationRun` (all project-relative).
- `ComfyAdapter(root: ProjectRoot, settings: Settings, *,
  popen=subprocess.Popen, transport=None)`; `transport=None` constructs
  HttpTransport(settings.binaries.comfyui_port). Injected transport has request()
  above. Constructor creates no files/launches. `.worker` is WorkerControl.
- `.generate(request: ImageRequest, job: Job, *, queue: JobQueue,
  cancel: Callable[[],bool] | None=None) -> ImageResult`.
  `.close() -> None` confirms owned process stop; reusable adapter after close is
  allowed if safely rearmed as a NEW epoch. No concurrent generate per adapter.
  queue root/settings identical and lease.worker IS adapter.worker; correlated IDs
  match job. Validate all before writes/queue entry. Cancel already true prevents
  startup/submission, yields CANCELLED and terminal queue state/quarantine.
  Use JobQueue.run, no parallel retry loop. Unique exclusive reservation at
  generated/{shot_id}/v{shot_version:04d}/{run_id}; refuse existing run.
  Each queue attempt retains attempt-NNNN/workflow.json, metadata.json, job.jsonl,
  object-info.json and output images. Run-level job log may be used for all attempts
  if log_path consistently resolves there. No successful overwrite of prior attempts.
  Give SaveImage a per-attempt unique relative prefix under runtime/output;
  authorize output destination directory through ProjectRoot.write_with/make_dir.
  Never trust history filename/subfolder/type: require exactly one PNG from output
  node 13, type='output', within this attempt's authorized output directory,
  no separator in filename, no traversal, symlink escape, absolute/drive/ADS path,
  missing/zero file. Fully decode image via Pillow (explicit dependency) and require
  expected width/height, then retained copy through helper; sha256 in metadata.
  Malformed/incomplete/error history never becomes success. Success requires completed
  true and status_str='success', recorded actual server version and child PID.
  Keep enough workflow/metadata/image data for reopen after server history disappears;
  GenerationRun is returned and serialized as generation-run.json, never stored by
  mutating domain services. tool='comfyui', version observed, invocation exact graph
  JSON, seed derived; settings contain steps/precision/sampler/memory_mode,
  model IDs/hashes/licences (scalar keys), workflow hash, child_pid and prompt_id;
  full model provenance lives in metadata; inputs include workflow/model manifests.
  Outputs include retained images+metadata. Store hashes, timestamps/duration,
  actual /system_stats, and schema/default evidence. Job progress is monotone coarse
  status progress from submission/completion, not fabricated throughput; telemetry
  measures NVML baseline/peak and worker torch stats when available, no footprint claim.

## Race, deadline, failure contract (binding)

Use monotonic whole-generate hard deadline, covering boot/hash/HTTP/output/retries,
plus existing queue ceilings. No unbounded blocking network/Popen joins.
Cancellation/timeout stops accepting launches/submissions, sends /interrupt AND
POST /queue {'delete':[active_prompt_id]} to owned server when ID known, waits for
confirmed quiescence or terminates owned child, joins attempt supervisor before
return/release/quarantine; no background callback may later submit/write success.
If /prompt response is late/ID unknown, killing owned server prevents orphan work.
Termination fallback must confirm death. Retain lease/fail WORKER_UNREACHABLE if
stop cannot be confirmed. Cancellation must complete within lease TTL for responsive
OS/processes; bound cleanup waits. Deadlines surface BUDGET_EXCEEDED, user cancel
CANCELLED. On successful unload do not accidentally stop an idle resident process.
Fence callbacks using both invocation epoch AND persisted lease token, including
before begin/Popen, response, validation, AFTER unload BEFORE token release, reuse
and retries. A released/stopped token cannot rearm; new retry token can. Queue's
watchdog may return without joining its callable, so adapter must quiesce it itself.
No success publication if stop races validation/provenance writing.

Errors use closed JobError kinds only. CUDA out-of-memory/bad allocation ->
OUT_OF_MEMORY; any other 'CUDA error' message must survive so JobQueue triggers
respawn (INTERNAL acceptable). Missing weights -> MISSING_MODEL_ARTIFACT, hash
mismatch/decoded bad output -> CORRUPT_ARTIFACT, invalid graph -> INVALID_MANIFEST,
network/child failures -> WORKER_UNREACHABLE, ENOSPC -> DISK_EXHAUSTED. Unknown
failures -> INTERNAL with cause. Each failed attempt records correlated structured
JSONL JobError + retained metadata; no silent fallback or swallowed cleanup/log error.
Secondary log ENOSPC must surface and retain original failure in cause/message.
Cancelled partial outputs (including runtime attempt output) go through real queue
ProjectQuarantine; prior completed runs unaffected. Do not quarantine resident runtime
or all runs. Report any failure of measured release and keep the persisted lease
when termination is unconfirmed. Success artifacts retained if release itself fails,
but do not return a successful result for a failed queue call.

## Independent verification responsibilities

Implementation author owns comfyui/*.py and only pyproject dependency changes
(Pillow declaration); does not read/write tests. Blind test author owns
 tests/adapters/comfyui/*.py in isolated contract-only snapshot without implementation;
reports failures, never fixes implementation. Independent Sol review reports only.
Cover pure graph/schema/seed/model/input validation; real HttpTransport via local
HTTP server (no external network); command/env/contained writes; queue protocol,
real JobQueue/GpuLease release/cancel/retry paths with meaningful process fakes;
malformed history/path/image, per-run retention/reopen and occupied port; late callback
races and shutdown errors. Use explicit test fixtures/contracts rather than assuming
new APIs. Tests cannot claim actual GPU behavior. Parent reruns tests/Ruff/diff,
adjudicates and commits this branch. Do not weaken assertions to suit implementation.

Hardware smoke (parent only, after review): detect occupied port/process before start;
use installed pinned weights without downloads or install-tree modifications. Retain
at least one actual 1280x960 generated PNG/graph/model hashes/provenance, measure lease
release and owned shutdown, attempt one real cancellation if feasible. Resource demand
is unmeasured; zero-demand smoke explicitly calibration only. No blanket AT-07/13 or
production footprint claim from a successful still. Record exactly what was not run.

Known outside-scope PRD inconsistency: Phase0 exit still asks for depth ControlNet via
controlnet_aux, but spike04/8.2 establishes FLUX.2 has no such path. This adapter does
not certify that obsolete acceptance condition. FR-04 operational/API gaps remain open.

## Amendment 1 - inspected startup containment (binding, later wins)

Installed 0.21.1 comfy/cli_args.py:236-239 hardcodes database default beneath
the INSTALL tree, independently of base/user directory. build_command MUST pass
--database-url sqlite:///:memory: (this is ComfyUI internal cache, never project
source of truth). Also pass --disable-api-nodes and --front-end-root pointing
to runtime_dir/frontend, an authorized contained directory created before launch;
headless operation needs no downloaded frontend. This prevents implicit frontend
fetch/install writes. Cache/temp environment still contained. Internal owned control
class has no promised public name: test only ComfyAdapter.worker and public
adapter/injected transport+popen.

## Amendment 2 - endpoint ownership evidence (binding)

A port probe before Popen is necessary but has a check/start race. Readiness must
verify the responding /system_stats.system.argv matches the owned launch's unique
contained --base-directory and configured --port (and explicit GPU selection),
before any /prompt or mutating control request. The installed 0.21.1 server
returns sys.argv in this field. An unrelated/mismatched endpoint is never freed,
interrupted or submitted to; stop only the child actually launched by this adapter
and fail WORKER_UNREACHABLE. Mark the endpoint untrusted before each new launch and
reset it on stop/reconfiguration. Tests' process fakes should reflect argv from the
actual injected Popen call, not invent another ownership API. Child PID remains
separately verified alive. This covers accidental port takeover; no malicious local
process trust boundary or cross-process orchestrator support is claimed.

## Amendment 3 — startup-aware throughput floor (binding)

The initial zero-only throughput-floor limitation is superseded. ComfyAdapter.generate accepts a positive configured floor and passes startup_allowance_seconds=33.5 to queue.run, grounded in PRD §8.4 and spike 03’s measured clean boot with the adapter’s disabled custom-node set. The 150.5 s ambient-node boot is not applicable. Queue watchdog behavior, whole-generate hard deadline, cancellation, endpoint/child ownership, callback fencing, cleanup, lease handling and output behavior remain unchanged. Do not fake progress or claim measured production resource reservations.
