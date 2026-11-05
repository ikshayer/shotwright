# FR-06 adjudication and verification - 2026-11-05

Implemented on worktree-api-conformance, based on 74c7aeb. FR-04 was already
committed and was not restarted. Changes are the standalone ComfyUI adapter,
independent tests, Pillow dependency and continuation documentation. API, service,
store, jobs, settings, Blender, PRD and spikes remain unchanged.

## Scope and integration

P0 text-to-image FLUX.2 klein 4B only. The graph promotes spikes 03/04 with mandatory
tiled VAE decode, explicit seed derivation, actual /object_info validation and
recorded schema/defaults. Image inputs, arbitrary graphs, downloads, Wan/3D,
ControlNet, reference-media conditioning and API/service wiring are excluded.
No domain entity revision or approval happens in this adapter.

Instantiate ComfyAdapter(root, settings), then GpuLease with adapter.worker and
JobQueue with the SAME ProjectRoot/Settings/worker. Call
adapter.generate(ImageRequest(...), job, queue=queue), and adapter.close() at
orchestrator shutdown. Supply explicit ModelPin entries and declared Job resources.
The lease PID is the live orchestrator supervisor; the actual child PID is separate
provenance. A resident unloaded server can survive successful measured release.
Respawn after CUDA error terminates the old context immediately and defers actual
new startup until the next owned attempt.

All managed output/temp/user/frontend/cache/config/log paths are contained.
ComfyUI's portable install is read-only; no package/model download or install change
was made. The launch uses a contained base directory, an in-memory ComfyUI internal
database, disabled API nodes, disabled custom nodes with an empty whitelist,
loopback binding and an explicit CUDA device. Model search directories come only
from hashed pins. Readiness and mutating controls verify the responding server's
version and unique launch argv, avoiding adoption of an unrelated listener.

GenerationRun, workflow, model manifest, node schema, output hash and decoded PNG
are retained under generated/<shot>/vNNNN/<run>/attempt-NNNN/. Failed attempts remain;
user cancellation quarantines the run and its separate runtime output directory.

## Independent authorship and adjudication

The parent wrote CONTRACT.md and skeleton before dispatch. One gpt-6-luna agent
implemented; a DIFFERENT Luna authored tests in an isolated filesystem snapshot
under _work/fr06-tests with the implementation physically absent. It was a contained
snapshot, not a second Git checkout. The test author never inspected implementation.
A third Luna authored the manual smoke harness from public contracts; the parent
alone ran hardware. gpt-6.1-sol reviewed read-only, reporting rather than fixing.
The parent inspected source, adjudicated findings and reran verification.

Review corrected: unsafe startup/release ownership, work and monitor callbacks
outliving queue deadlines, late callbacks after unload/before token release,
atomic thread admission, immutable lease-token fencing, stopped-token reuse,
unknown-prompt cleanup, ignored termination failure, work/monitor dependency joins,
final telemetry/publication races, early failure evidence, retained runtime partials,
model-resolution ambiguity and malformed output/schema handling. Success publication
is supervised, with deadline/stop checks between writes; unsafe child stop retains
rather than drops the lease. The final static review reported no remaining HIGHs.

Test-fixture adjudications preserved the contract rather than weakening it: actual
ComfyUI node/queue schema shapes replaced invented ones; invalid declared defaults
were truly outside bounds; missing model-menu membership was distinguished from
invalid request filenames; Python script position respected normal argv order;
quarantine checks respected its existing indexed names; hash failures retained
attempt evidence; unknown-ID cancellation waits for child death before releasing a
late response; an unloaded idle server is allowed after a pre-prompt deadline; and
publication tests prohibit release completion while a write is blocked, while allowing
unload to begin and join that write.

## Parent verification actually run

- Full suite, after the final source fix: **1061 passed, 28 skipped, 6 existing
  warnings in 151.63 s**. Includes all **70** independently authored FR-06 cases.
- Whole orchestrator/tests Ruff: **All checks passed** (cache disabled).
- git diff --check: clean; staged check is repeated before commit.
- Shared interpreter D:/Work/video_pipeline/.venv/Scripts/python.exe; TMP/TEMP/
  PYTEST_DEBUG_TEMPROOT point to a ProjectRoot-created _work/fr06 directory.
  Bytecode and pytest cache writes were disabled for the parent's final suite.

Tests use real JobQueue/GpuLease plus process/transport fakes, and real loopback HTTP
for transport checks. They cover schema/seeds/launch, retained outputs/reopen,
model hashes, endpoint ownership, malformed media/history, retry/respawn,
cancellation/quarantine, unconfirmed stop/lease retention, whole deadlines and
late callbacks including final telemetry publication. They do not certify hardware.

## Actual ComfyUI hardware probes

ComfyUI 0.21.1, PyTorch 2.12.0.dev20260408+cu128, CUDA device 0 explicitly identified
in the server log as NVIDIA GeForce RTX 5070 Ti Laptop GPU. Core-only launch, fp4
encoder, default precision, normal memory mode, Euler/CFG 1.0, four steps, 1280x960,
VAEDecodeTiled. Original text prompt described an empty service room/CCTV view.
No reference image was supplied. The parent inspected the resulting PNG visually.

| Probe | adapter generate seconds | device baseline MiB | device peak MiB | after release MiB | after close MiB |
|---|---:|---:|---:|---:|---:|
| Successful still | 189.391 | 2749.098 | 11022.941 | 2812.105 | 2554.820 |
| Cancel after accepted prompt | 51.547 | 2653.883 | not retained as a production estimate | 2790.668 | 2653.570 |

These are functional calibration probes, NOT production memory reservations or
render-time promises. Both Jobs deliberately declared zero VRAM/RAM/disk demand.
The 300-second smoke ceiling, 120-second cancellation safety ceiling and configured
1024 MiB release margin are operational settings, not newly measured requirements.
The successful graph's server log separately reports **60.95 s prompt execution**;
the full adapter time includes cold startup, hashing and validation. Do not compare
that full time directly to spike03's already-started-server figures.

Success: actual retained PNG is 1280x960, SHA256
`ec2c8a76b97f0cdd740ecf19a33af9b14283db66ddbcf6ee5da15765516c2b0c`.
After server shutdown the parent fully decoded/verified the PNG, checked its hash
against metadata and reopened GenerationRun from JSON. Model hashes matched before
and after generation. Lease file was absent after return; process/port checks showed
the child gone. Separate validation is _work/fr06/verification-success.json.

Cancellation: the trigger was job.progress >= 0.1 after submission, NOT the safety
ceiling. The server recorded an accepted prompt, Global interrupt and Processing
interrupted (prompt execution 0.35 s). JobError kind/state were cancelled; run and
runtime output directory were quarantined; no PNG remained outside quarantine.
Lease was absent after return, close succeeded and the child Popen had an exit code.
The harness's full elapsed 72.406 s includes additional model rehash/shutdown checks.

### Diagnostic failure preserved, not hidden

The first success harness report says error although the Job completed: its shutdown
assertion used the existing jobs.lease.pid_is_alive helper. On Windows that helper
only checks OpenProcess handle existence and can report an exited process as alive.
A separate CPU-only probe reproduced exit_code=0, Popen.poll=0,
legacy_pid_is_alive=true and psutil.NoSuchProcess. The adapter itself confirms death
through its owned Popen poll/wait. No production jobs helper was changed in this scope.

The original report is retained unchanged. The parent independently reopened the
artifact after shutdown and wrote verification-success.json; the corrected cancellation
harness uses Popen exit proof and separate psutil diagnostics. Its serialized
NoSuchProcess diagnostic is evidence of absence, not a worker failure. Do not claim
that the original harness report passed or that stale-lease PID detection was repaired.

Local evidence (ignored/disposable):

- _work/fr06/smoke.py, model-hashes.json, documentation-sources.json
- _work/fr06/verification-success.json and pid-helper-probe.json
- _work/fr06/smoke-project-20261105T061008Z-d1bad529/reports/success-20261105T061008Z-a4e3ae.json
- _work/fr06/smoke-project-20261105T061548Z-f6418616/reports/cancel-20261105T061548Z-279a31.json
- Each project's retained runtime server.log, workflow/schema/model manifest and job evidence.

## Pinned model identities and primary sources

All three local file hashes were actually computed and matched upstream LFS metadata.
Their recorded licences are Apache-2.0; Qwen's licence text and the original FLUX
licence were inspected. The VAE's component licence is stated separately by BFL,
not inferred from the FLUX.2-dev repository's overall tag.

| Role | File | SHA256 |
|---|---|---|
| diffusion | flux-2-klein-4b.safetensors | ec3d4e733a771f61c052fb4856c48b336c55eaf2c65487c2a1faeb9bbda7a343 |
| encoder | qwen_3_4b_fp4_flux2.safetensors | 3eab03a77adb0ee5304a4e677d5c10ac22f9049c1d7c894adca4f8bb39206ca8 |
| VAE | flux2-vae.safetensors | 868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3 |

The Klein VAE hash differs from the similarly named Dev repack; filenames alone are
not identity proof. Source URLs with revisions are in the retained model manifests.

- API: https://docs.comfy.org/development/comfyui-server/comms_routes
- Diffusion: https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/blob/main/flux-2-klein-4b.safetensors
- Encoder: https://huggingface.co/Comfy-Org/Flux-2-Klein-4B/blob/1f52fa6a526ff84b01520be049b63f7835fe059f/split_files/text_encoders/qwen_3_4b_fp4_flux2.safetensors
- VAE: https://huggingface.co/Comfy-Org/Flux-2-Klein-4B/blob/a9e4ca87c16db4c4e1a16406a9ddb300ab0ae246/split_files/vae/flux2-vae.safetensors
- FLUX licence: https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/raw/main/LICENSE.md
- Qwen licence: https://huggingface.co/Qwen/Qwen3-4B/blob/main/LICENSE
- VAE component licence: https://github.com/black-forest-labs/flux2#flux2-autoencoder

Context7 was available and used (/comfy-org/comfyui, /comfy-org/docs and Pillow),
with official primary docs and read-only inspection of the installed CLI, main,
folder_paths, extra_config and server code. The installed CLI exposed the hardcoded
install-tree database default; the adapter overrides it explicitly.

## Limits and open acceptance

- **MED static-review limitation:** a stalled OS filesystem write while publication
  holds its lock cannot be forcibly interrupted; it may delay stop admission beyond
  TTL. Responsive local filesystem/driver behavior is an operational assumption.
  Hard deadlines are checked around managed work and between publication writes.
- Production resource reservations remain UNMEASURED. JobQueue's startup-aware
  throughput watchdog is unfinished; this adapter rejects a nonzero throughput floor
  before writes. No production admission/throughput/AT-13 blanket claim is made.
- Existing Windows PID-helper false positives remain a jobs-layer backlog item;
  actual killed-orchestrator/TTL reclamation was not hardware-tested here.
- No real CUDA OOM/poisoned-context respawn, unkillable OS process, disk exhaustion,
  physical power loss, hostile local endpoint, cross-process writers or driver-managed
  cache containment audit was run. Retry/lease-retention branches use independent fakes.
- Installed onnxruntime still prints the known _ARRAY_API/kornia startup defect. It did
  not block this core-only FLUX.2 path; no preprocessor repair/provider acceptance is claimed.
- No img2img, generated-video, continuity, byte-identical image replay or creative rubric
  acceptance was tested. Phase0's old ControlNet exit still conflicts with spike04/8.2;
  the parent reported it and did not rewrite the PRD.
- API generate_media and Blender routes remain their existing 501 contract. Existing
  upload containment/caps/reference-operation/store-conflict backlog remains open.
  FR-04 operational limits, FR-21 and the finished-shot Phase1 exit remain separate work.

## Startup-aware throughput-floor follow-up — 2026-11-05

The earlier operational-limit statement above is superseded: ComfyUI now accepts a positive configured floor and passes the measured 33.5 s §8.4/spike 03 clean-boot allowance to JobQueue.run (the enforced adapter boot disables custom nodes). Independent tests in 	ests/adapters/comfyui/test_runner_contract.py exercise a positive-floor generation through the real queue and assert the forwarded allowance. The queue suite independently covers silent startup before allowance, startup expiry, post-startup silence, below-floor later progress, confirmed termination and lease retention when stop is unconfirmed.

Final verification after this change: whole suite 1123 passed, 29 skipped, 6 existing warnings; whole orchestrator/tests Ruff check passed. No ComfyUI/GPU hardware was run for this follow-up. Production resource reservations remain unmeasured.
