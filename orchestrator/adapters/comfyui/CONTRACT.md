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

## Amendment 4 - ComfyUI 0.44.0, the Z-Image family, RealGen, and pinned profiles (binding, later wins)

Orchestrator-authored 2026-11-20, before implementation and tests. Creator decision:
the pipeline's still-image runtime moves to ComfyUI **0.44.0**, Z-Image Base is added as
a second model family beside FLUX.2 klein 4B, and **Z-Image Base + RealGen V2 at 0.6 is
the preferred default** whenever a caller does not choose models. FLUX stays **FLUX.2
klein 4B** (never FLUX.1 schnell). Everything in the base contract and Amendments 1-3
that this amendment does not change still binds; Scope's "FLUX.2 klein only" and the
measured-model list are superseded by A4.2.

### A4.1 Runtime

- The required server version is `0.44.0` everywhere the adapter checks it (boot
  readiness, endpoint ownership and post-completion `/system_stats`). Hold it in ONE
  module constant; no second literal.
- `Settings.binaries.comfyui_root` default becomes
  `D:\ComfyUI_0.44.0\ComfyUI_windows_portable` (official v0.44.0
  `ComfyUI_windows_portable_nvidia.7z`, SHA-256 `7170c2db49c182a81adcc051e80234f99dd891656fb200737f87797876348f96`).
  It is a portable layout, so `build_command` is unchanged apart from what A4.4 adds.
  The old 0.21.1 tree is left in place and untouched.
- Every launch flag of the base contract and Amendments 1-2 is kept; the parent has
  confirmed each against the 0.44.0 CLI before dispatch (see A4.8).

### A4.2 Families and measured models

`ImageRequest` gains:

- `family: Literal["flux2_klein", "z_image"] = "flux2_klein"` (the default keeps every
  existing explicit FLUX request valid unchanged).
- `steps: int | None = None`, range 1..100 when given. After validation it is ALWAYS an
  int: `None` resolves to the family default (flux2_klein 4, z_image 25). Implement with
  a before-validator; the frozen model never exposes `None`.
- `lora_strength: float | None = None`. Required (finite, `0 < x <= 2`) exactly when a
  `lora` pin is present, and must be `None` otherwise.

`ModelPin.role` gains `"lora"`. Per family, exactly one pin per listed role, no others:

| family | required roles | optional | measured file names |
|---|---|---|---|
| flux2_klein | diffusion, text_encoder, vae | none | as before: `flux-2-klein-4b.safetensors`; `qwen_3_4b_fp4_flux2.safetensors` or `qwen_3_4b.safetensors`; `flux2-vae.safetensors` |
| z_image | diffusion, text_encoder, vae | lora | `z_image_int8_convrot.safetensors`; `qwen_3_4b.safetensors`; `ae.safetensors`; lora `realgen_v2.safetensors` |

`z_image` requires `precision == "default"` (the INT8 ConvRot checkpoint carries its own
quantisation; fp8 recasting is unmeasured). A FLUX request with a lora pin, a z_image
request with a FLUX file name, or any cross-family mix is a validation error.

### A4.3 Z-Image graph

`build_workflow` dispatches on `family`; the FLUX graph is byte-for-byte unchanged. The
Z-Image graph promotes spike 30's retained Base + RealGen graph (Comfy-Org official
`text_to_image_z_image_base` blueprint), with tiled decode kept mandatory:

| id | class_type | inputs |
|---|---|---|
| 1 | UNETLoader | unet_name = diffusion file, weight_dtype = "default" |
| 2 | CLIPLoader | clip_name = text encoder file, type = "lumina2", device = "default" |
| 3 | VAELoader | vae_name = vae file |
| 4 | CLIPTextEncode | text = prompt, clip = ["2",0] |
| 5 | CLIPTextEncode | text = "", clip = ["2",0] |
| 6 | LoraLoaderModelOnly | **only when a lora pin exists**: model = ["1",0], lora_name = lora file, strength_model = lora_strength |
| 7 | ModelSamplingAuraFlow | model = ["6",0] if node 6 exists else ["1",0], shift = 3.0 |
| 8 | EmptySD3LatentImage | width, height, batch_size = 1 |
| 9 | KSampler | model ["7",0], positive ["4",0], negative ["5",0], latent_image ["8",0], seed = derive_seed(shot_id, degrade_seed, "comfyui", 2**64-1), steps, cfg = 4.0, sampler_name "res_multistep", scheduler "simple", denoise 1.0 |
| 12 | VAEDecodeTiled | samples ["9",0], vae ["3",0], tile_size 512, overlap 64 (other required inputs from declared defaults, as today) |
| 13 | SaveImage | images ["12",0], filename_prefix = output_prefix |

The same `/object_info` validation applies to every node; `lora_name` joins
`unet_name`/`clip_name`/`vae_name` as a model combo (absent -> MISSING_MODEL_ARTIFACT).
SaveImage stays node `"13"` in both families. The runner must not read a family-specific
seed input name directly: `workflow.py` exposes `graph_seed(graph) -> int` (FLUX reads
`9.noise_seed`, Z-Image `9.seed`) and `family_sampler(family) -> str`
(`"euler"` / `"res_multistep"`).

### A4.4 Runner provenance

- The model config adds category `loras` -> the lora pin's parent directory, only when a
  lora pin exists. Hash-before and hash-after cover every pin, including the lora.
- `GenerationRun.seed` = `graph_seed(graph)`. `settings` keeps every existing key and adds
  `family`, `cfg`, and `lora_strength` (only when present); `sampler` is
  `family_sampler(family)`. Role-keyed `*_id`/`*_sha256`/`*_license` keys now include
  `lora_*` when present. `metadata.json` likewise records `family`.
- Startup allowance becomes per family: FLUX keeps 128.4 s. Z-Image pre-progress work
  hashes 14.90 GB of pins (lora included) against FLUX's 11.94 GB, so its derived upper bound is
  128.4 x 14.90 / 11.94, rounded up: **161 s**, documented beside the FLUX constant as
  DERIVED, to be replaced by the A4.8 measurement.

### A4.5 Pinned model profiles (the machine-readable manifest)

New module `orchestrator/adapters/comfyui/profiles.py`, pure and stdlib/Pydantic only. It
is the first machine-readable pinned-model manifest the COMPOSITION and PROGRESS
contracts said `generate_media` was blocked on. Every hash below was measured on the
local file AND matched upstream LFS metadata, so no provenance is invented (FR-25).

```
ProfileName = Literal["z_image_realgen", "z_image", "flux2_klein"]
DEFAULT_PROFILE: ProfileName = "z_image_realgen"
```

A frozen `ModelProfile(name, family, pins: tuple[PinSpec, ...], steps, lora_strength,
required_vram_gb, summary)`; `PinSpec(role, category, file_name, sha256, source,
license_id)` where `category` is the ComfyUI folder (`diffusion_models`,
`text_encoders`, `vae`, `loras`). `PROFILES: Mapping[ProfileName, ModelProfile]`
(read-only), exactly:

- **flux2_klein**: family flux2_klein, steps 4, no lora, required_vram_gb 0.0 (FLUX
  reservations remain unmeasured, as before), pins:
  - diffusion `flux-2-klein-4b.safetensors` `ec3d4e733a771f61c052fb4856c48b336c55eaf2c65487c2a1faeb9bbda7a343`, https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/blob/main/flux-2-klein-4b.safetensors, `Apache-2.0`
  - text_encoder `qwen_3_4b_fp4_flux2.safetensors` `3eab03a77adb0ee5304a4e677d5c10ac22f9049c1d7c894adca4f8bb39206ca8`, https://huggingface.co/Comfy-Org/Flux-2-Klein-4B/blob/1f52fa6a526ff84b01520be049b63f7835fe059f/split_files/text_encoders/qwen_3_4b_fp4_flux2.safetensors, `Apache-2.0`
  - vae `flux2-vae.safetensors` `868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3`, https://huggingface.co/Comfy-Org/Flux-2-Klein-4B/blob/a9e4ca87c16db4c4e1a16406a9ddb300ab0ae246/split_files/vae/flux2-vae.safetensors, `Apache-2.0`
- **z_image**: family z_image, steps 25, no lora, required_vram_gb 7.5 (the 7,680 MiB
  reservation every spike 30 Z-Image run was admitted under), pins (all
  `Apache-2.0`, Comfy-Org/Z-Image revision `eaf9ea44e47ed9778f02d8c92efdc398b6cccea3`):
  - diffusion `z_image_int8_convrot.safetensors` `eb2b470694fbd9778f15efd309000ea0091312c018c10a352c120b1da9bf4279`, https://huggingface.co/Comfy-Org/Z-Image/blob/eaf9ea44e47ed9778f02d8c92efdc398b6cccea3/split_files/diffusion_models/z_image_int8_convrot.safetensors
  - text_encoder `qwen_3_4b.safetensors` `6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a`, https://huggingface.co/Comfy-Org/Z-Image/blob/eaf9ea44e47ed9778f02d8c92efdc398b6cccea3/split_files/text_encoders/qwen_3_4b.safetensors
  - vae `ae.safetensors` `afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38`, https://huggingface.co/Comfy-Org/Z-Image/blob/eaf9ea44e47ed9778f02d8c92efdc398b6cccea3/split_files/vae/ae.safetensors
- **z_image_realgen**: the z_image pins plus lora `realgen_v2.safetensors`
  `c50a3437f85592512f92a2f956d699c6fc524e2b05cc77a05dc47a3057133973`,
  https://huggingface.co/nynxz/RealGen-V2/blob/ebf1c59d130a438153a675fb4a7125b2b06e7dcf/realgen_v2.safetensors,
  `Apache-2.0`; lora_strength **0.6** (spike 30's selected strength, used for all 79
  retained apartment images); steps 25; required_vram_gb 7.5.

`compose_image_request(*, profile: ProfileName, weights_dir: Path, shot_id: str,
shot_version: int, degrade_seed: int, run_id: str, prompt: str, width: int | None = None,
height: int | None = None) -> ImageRequest`: pins are
`weights_dir / category / file_name` with the profile's hash/source/licence; family,
steps and lora_strength from the profile; width/height default to the ImageRequest
defaults when `None`. It does no I/O (the adapter already hashes and fails missing or
changed files as MISSING_MODEL_ARTIFACT/CORRUPT_ARTIFACT). Unknown profile -> ValueError.

### A4.6 Settings

New section `[image_models]` -> `ImageModelSettings(_Section)` on `Settings` as
`image_models`: `weights_dir: Path = D:\Work\pipeline-models` (absolute) and
`default_profile: ProfileName = "z_image_realgen"`. Follow the existing sections' style
(descriptions, extra-forbid). `verify_paths` reports a missing `weights_dir` like the
other path checks. No other settings change besides A4.1's default.

### A4.7 `generate_media` composes when models are omitted (the MCP default)

In `orchestrator/api/app.py`'s `generate_media` branch, after the Shot is loaded:

- If the body's `request` object has NO `models` key, the server composes the request
  with `compose_image_request`: `profile` = body `profile` if given, else
  `settings.image_models.default_profile`; `shot_id`/`shot_version`/`degrade_seed` from
  the loaded Shot (`shot.version`, `shot.degrade_seed`); a fresh `uuid4().hex` run_id
  unless the request gives one; `prompt` (required, nonblank) and optional
  `width`/`height` from the request. Any other key in that request object is
  `AdapterRequestInvalid` (no silent drop).
- If `request.models` IS present, behaviour is exactly as today (explicit pins win), and
  a body `profile` alongside it is `AdapterRequestInvalid`.
- An unknown `profile` -> `AdapterRequestInvalid` naming the valid profiles.
- When the request was composed and the caller supplied no `required_vram_gb`
  (top-level or under `resources`), it defaults to the profile's `required_vram_gb`.
  Explicit caller values still win; the existing numeric validation still applies.
- `record_version` for a composed request is `shot.version`.

The `generate_media` route summary (`orchestrator/api/routes.py`) - which the MCP
adapter serves as the tool description - must say, in one or two sentences, that
omitting `request.models` uses the pinned default profile (Z-Image Base + RealGen V2 at
0.6), that `profile` may be `z_image_realgen`, `z_image` or `flux2_klein` (FLUX.2 klein
4B), and that `request.prompt` is required. Do not change `mcp.py`.

### A4.8 Parent-owned steps (not implementer, not test author)

Before dispatch the parent extracts the verified portable, confirms every launch flag
against the 0.44.0 CLI, and hard-links (same volume, zero copies) the seven pinned files
into `D:\Work\pipeline-models\{diffusion_models,text_encoders,vae,loras}`. After review
the parent runs one real Z-Image + RealGen generation and one real FLUX.2 klein
generation through `ComfyAdapter` on 0.44.0, records pre-progress time, VRAM peak and
release, replaces the 161 s derived bound with the measurement, and updates
`AGENTS.md`'s machine facts. Anything not run is reported as not run.

### A4 file ownership

- Implementer: `orchestrator/adapters/comfyui/{workflow,runner,runtime,profiles}.py`,
  `orchestrator/platform/settings.py` (A4.1 default + A4.6 only),
  `orchestrator/api/app.py` (`generate_media` branch only), `orchestrator/api/routes.py`
  (the `generate_media` summary only). Nothing else; no tests.
- Test author (blind to the implementation): `tests/adapters/comfyui/*.py` and a new
  `tests/api/test_generate_media_profiles.py`. Existing assertions that pin `0.21.1`
  move to `0.44.0`; no other existing assertion is weakened.

### A4 test rules

- No GPU, network or real ComfyUI; existing fakes and `/object_info` fixtures, extended
  with the Z-Image node signatures (`ModelSamplingAuraFlow`, `EmptySD3LatentImage`,
  `KSampler`, `LoraLoaderModelOnly`).
- Cover: family/pin/name/precision/lora_strength validation both ways; steps defaulting;
  the exact Z-Image graph with and without the lora (node set, wiring, constants, seed);
  the FLUX graph unchanged; `graph_seed`/`family_sampler`; `loras` model-config category
  and lora hashing; provenance keys; version `0.44.0` accepted and `0.21.1` refused;
  `PROFILES` contents exactly as A4.5 (hashes, names, sources, licences, strength,
  steps, reservations) and `DEFAULT_PROFILE`; `compose_image_request` paths and fields;
  settings defaults; and `generate_media` composition through the real HTTP app with a
  fake job runner: default profile used when models are omitted, explicit profile,
  unknown profile, profile + models conflict, stray keys, explicit models unchanged,
  VRAM default vs explicit override, run_id/shot fields.
- A test that would pass against a FLUX-only or always-default implementation is not a
  test of A4.

### A4.9 Pre-dispatch evidence (parent, 2026-11-20)

- Portable v0.44.0 NVIDIA: archive SHA-256 matched GitHub's published digest; extracted
  to `D:\ComfyUI_0.44.0\ComfyUI_windows_portable`. Embedded Python 3.13.14, torch
  2.14.1+cu130 (sm_120 present). No install-local `extra_model_paths.yaml`.
- Every launch flag of the base contract and Amendments 1-2 exists in the 0.44.0 CLI.
  `--disable-api-nodes` is deprecated there ("behaves like --offline") but still
  accepted; keep it unchanged in this scope. 0.44.0 defaults `--database-url` to the
  user directory; Amendment 1's explicit `sqlite:///:memory:` still applies.
  `/system_stats.system.argv` is still returned (Amendment 2 holds).
- All seven pinned files in `D:\Work\pipeline-models` hashed locally and match A4.5.
- FLUX.2 klein 4B smoke on 0.44.0 with the adapter's launch flags and the unchanged
  `build_workflow` FLUX graph, 1280x960, 4 steps: two runs, both `success`, one valid PNG
  each. Boot 99.0 s cold / 35.7 s warm; prompt 21.2 s / 19.2 s; GPU-wide VRAM peak
  11,234 / 11,170 MiB of 12,227; 258 MiB after `/free`; 0 after stop. Image descriptors
  in `/history` gain a `metadata` key; the runner ignores unknown descriptor keys.
- 0.44.0 enables pinned host memory (~12.7 GB logged) and DynamicVRAM by default. Host
  RAM admission is therefore unmeasured for 0.44.0; no claim is made.

### A4.10 Review adjudication (parent, 2026-11-20; binding)

Independent review (gpt-6.1-sol) after implementation; the parent verified each finding.

- **HIGH, accepted - JobRunner minted its own run_id for still-image jobs.** `JobRunner.submit`
  took the run_id only from Render/Degrade payloads, so every `generate_media` job got a fresh
  uuid and `ComfyAdapter.generate` refused it ("job IDs must match request"). This predates A4
  (explicit requests were equally affected) but A4.7 makes it the default MCP path. Fix:
  `orchestrator/service/runner.py::_job_run_id` uses the request's own run_id for an
  `ImageRequest` payload and for an explicit `generate_media` request dict carrying a nonblank
  string `run_id`. Other kinds are unchanged.
- **MEDIUM, accepted - relative `weights_dir`.** A4.6's "(absolute)" is a validation rule:
  `ImageModelSettings.weights_dir` rejects a relative path at settings load.
- **MEDIUM, accepted and widened - combo validation.** The validator checked membership only for
  inline legacy combo lists. ComfyUI 0.44.0's schema-defined nodes declare
  `["COMBO", {"options": [...]}]`; the real 0.44.0 `/object_info` (retained as
  `tests/adapters/comfyui/data/object_info_0_44_0_subset.json`) shows FLUX's
  `KSamplerSelect.sampler_name` in that form, so it was never checked. Both forms are now
  validated: options must be a list of str/int (not bool) or the graph is INVALID_MANIFEST; an
  empty option list is MISSING_MODEL_ARTIFACT for a model input and INVALID_MANIFEST otherwise;
  a value not among the options keeps its existing error kinds.
- **LOW, accepted - FLUX regression needs an independent baseline.** A golden FLUX graph produced
  by `main`'s builder is retained and compared exactly.
- **Parent fixes also applied:** `ProfileName` lives in `orchestrator/platform/settings.py`
  (the platform layer must not import an adapter); the Z-Image pins are one shared tuple; the
  API's unknown-profile message is derived from `PROFILES`. `tests/api/test_adapters.py`'s
  generate_media probe now carries the prompt A4.7 requires.

### A4.11 Hardware evidence (parent, 2026-11-20)

Real `ComfyAdapter.generate` on ComfyUI 0.44.0, real `JobQueue` + `GpuLease` + NVML, requests
composed by `compose_image_request` from the default `Settings` (the `generate_media` path),
1280x960, desktop foreign VRAM ~1.28 GB. Script and reports: `_work/a4-hw/*/a4-hw-report.json`.

| profile | result | wall | pre-progress | GPU-wide VRAM peak | after close |
|---|---|---|---|---|---|
| z_image_realgen (first, cold server) | completed, `tool_version` 0.44.0, 4 pins incl. lora hashed, all provenance keys present | 158.0 s | 72.2 s | 11,475 MiB (adapter NVML 11,507) | 1,282 MiB (baseline) |
| flux2_klein | completed, `tool_version` 0.44.0, sampler euler, cfg 1.0, 4 steps | 146.8 s | 109.5 s | 11,645 MiB (adapter NVML 11,806) | 1,276 MiB (baseline) |

- Both observed pre-progress times are inside their allowances (161 s / 128.4 s). One sample
  each is evidence the bounds hold, not a new bound, so both DERIVED bounds stay as they are.
  A CPU-heavy pytest run overlapped the first ~minute of the Z-Image run.
- **Open risk:** a cold-cache server boot measured 99.0 s on 0.44.0 (A4.9). FLUX's 109.5 s
  pre-progress leaves 19 s of margin under 128.4 s; a first generation after a reboot could
  exceed it and fail BUDGET_EXCEEDED. Not raised without a measurement of that case.
- Z-Image's peak leaves ~750 MiB headroom at ~1.3 GB foreign VRAM; at the 2.7 GB foreign
  baseline AGENTS.md records it will not fit (spike 30 saw the same margin trips).
- Not run: cancellation, OOM/respawn, lowvram modes, host-RAM admission (0.44.0 pins
  ~12.7 GB host memory), any MCP client end to end.
