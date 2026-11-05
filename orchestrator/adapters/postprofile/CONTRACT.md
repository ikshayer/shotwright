# FR-21 PostProfile adapter contract

Written before implementation. Normative PRD: FR-21, §4.4, FR-25/27,
FR-32, §8.4, AT-05 and AT-14. Promote `spikes/01_postprofile_determinism`;
do not alter the measured behavior without a reproduced measurement and a written
adjudication. API/service/store integration is owned elsewhere.

## Scope and public interfaces

`orchestrator.adapters.postprofile.profile` provides pure validation and graph building:

- `profile.py` defines `PostProfileSpec`, `OverlaySpec`, `EncodeSpec`, and
  `ValidatedPostProfile`. `PostProfileSpec` is a frozen, extra-forbid adapter value
  model that carries the domain
  `PostProfile` as `identity: PostProfile`, plus the fields absent from that domain type:
  `output_fps: int`, `storage_resolution: tuple[int,int]`,
  `sample_aspect_ratio: str`, `artifact_schedule: tuple[ArtifactScheduleEntry,...]`,
  `overlay: OverlaySpec | None`, and `encode: EncodeSpec`. `OverlaySpec` has
  `camera_id`, `clock_epoch`, `fontsize`, `x`, `y`, and
  `placement: Literal['in_recording','on_playback']`. `EncodeSpec` has `codec`, `preset`,
  `crf`, `pix_fmt`, and `threads`. The domain profile supplies name, semver, ordered
  `PostStage` list, pinned binaries and filter graph hash; adapter config supplies only
  values not represented there. `supersedes: str | None` records the previous profile
  semver when creating a new version; it does not mutate the domain entity.
- `validate_profile(profile: PostProfileSpec, camera: CameraProfile, *, master_frames: int,
  master_rate: Fraction)
  -> ValidatedPostProfile`. Revalidate at call time. Reject unknown filters/options,
  malformed values, duplicate stage names, invalid profile semver, invalid frame
  schedule windows, a freeze at frame zero, overlapping drop/freeze windows, schedules
  with no surviving frame after a drop, and any window beyond the decimated frame count.
  `master_frames` and rational `master_rate` are the input frame count and rate obtained
  by the runner's FFprobe call. Derive source duration as their exact rational quotient
  and output count using FFmpeg `fps` nearest-frame behavior (ties away from zero),
  matching the spike's round-to-nearest simulation. The result contains
  the normalized stage order, predicted frame count, clock-step schedule, and emitted
  `ArtifactScheduleEntry` values. Do not silently discard a schedule event.
  Require `scale`, `fps`, and `select` stages; allow no schedule, in which case select
  passes every frame. Require the measured two `setpts` stages in order: the clock stage
  before `drawtext`, then output `setpts` after `drawtext`. A nonempty drop schedule
  requires the clock stage to carry `from_schedule='clock'`; a freeze schedule requires
  exactly one `loop` stage and the output count remains length-neutral. Duplicate `loop`
  stages are invalid because each would replay every freeze window. A nonempty schedule without the
  corresponding graph stages is invalid at profile load. `drawtext` and overlay config
  must either both be present or both absent; config without a matching stage is rejected.
- `ValidatedPostProfile` is a frozen dataclass with fields `profile: PostProfileSpec`,
  `camera: CameraProfile`, `master_frames: int`, `source_rate: Fraction`,
  `output_rate: Fraction`, `decimated_frames: int`, `predicted_frames: int`,
  `clock_steps: tuple[tuple[int,int],...]`, and
  `schedule: tuple[ArtifactScheduleEntry,...]`.
- `build_filter_graph(profile: ValidatedPostProfile, *, shot_id: str, degrade_seed: int,
  font_path: str) -> str`. Pure and deterministic. Derive each seeded stage via existing
  `orchestrator.domain.seeds.derive_seed(shot_id, degrade_seed, stage_name, max_value)`;
  accepted seed range is declared per stage and defaults to the measured `noise`
  `all_seed` ceiling `INT32_MAX`. Never mask/wrap an out-of-range value. Preserve spike
  ordering and semantics: scale, decimate, schedule restructuring, explicit `settb`
  and clock `setpts`, in-recording overlay, then degradation and SAR. The overlay's
  schedule-aware clock jumps across drops and advances during image freezes. Use the
  measured loop `start=position+1` compensation. Encode `font_path` as a safely escaped
  FFmpeg filter value; callers must provide an already-contained project-relative font.
  Unknown or unavailable filters/options are errors, never omitted stages.
- `camera_profile_impact(before: CameraProfile, after: CameraProfile,
  shots: Iterable[Shot]) -> tuple[Shot, ...]` delegates directly to
  `orchestrator.domain.invalidation.revision_impact`; it must not restate §4.4 tagging.
  The adapter does not mutate entities or invoke a render.

`orchestrator.adapters.postprofile.runner` provides:

- `build_command(ffmpeg_exe: Path, *, master: Path, output: Path,
  filter_graph: str, codec: str, preset: str, crf: int, pixel_format: str,
  threads: int) -> list[str]`. Pure argv builder, no shell. Include the spike's bitexact
  input/output flags and explicit encoder thread count.
- `PostProfileAdapter(root: ProjectRoot, settings: Settings, *, popen=subprocess.Popen,
  ffprobe_exe: Path | None=None)`.
- `.degrade(request: DegradeRequest) -> DegradeResult`. `DegradeRequest` is a frozen,
  extra-forbid typed request carrying `shot_id`, `shot_version`, unique `run_id`,
  `degrade_seed`, `master_relative`, `font_relative`, `profile: PostProfileSpec`, and
  `camera_profile: CameraProfile`. `PostProfileSpec.identity` is the domain
  `PostProfile`. IDs are
  `[A-Za-z0-9_-]+`, version >=1, seed >=0, input/font paths are project-relative.
  `DegradeResult` is frozen and returns project-relative output, log, metadata paths,
  the emitted schedule, filter graph/hash, output SHA256, probed frame count/PTS delta
  distribution, and FFmpeg version/banner identity.

`DegradeResult` is a frozen dataclass with fields
`output_path: str`, `log_path: str`, `metadata_path: str`,
`artifact_schedule: tuple[ArtifactScheduleEntry,...]`, `filter_graph: str`,
`filter_graph_hash: str`, `output_sha256: str`, `frame_count: int`,
`pts_delta_distribution: dict[str,int]`, and `ffmpeg_identity: str`.

`PostProfileSpec` maps to the spike JSON schema (`identity.name`/profile id,
`identity.semver`/profile version, output fps, storage dimensions, SAR, schedule, ordered
stages, encode settings, overlay values). Support the measured `scale`, `fps`, `select`, `loop`,
`setpts`, `drawtext`, `eq`, `noise`, `unsharp`, and `setsar` operations used by the spike.
Reject arbitrary filter graphs, shell fragments, unknown stage keys, stage parameters,
and `from_schedule` values. CameraProfile post-only fields are the source for equivalent
profile settings; a rebuild-triggering-only edit cannot be represented as a post change.
Stage values are closed and typed/range-checked before graph construction: scale width and
height are positive integers and flags are from FFmpeg's enumerated flags; `fps` equals
`output_fps`; noise `alls` is an integer in [0,100], `allf` is one of the installed
filter's declared values, and seed max is in [1,INT32_MAX]; unsharp sizes are odd values
in [3,23] and amount in [-2,5]; every numeric value is finite. SAR is a positive rational
and any explicit `setsar.sar` must equal `PostProfileSpec.sample_aspect_ratio`; if the
`setsar` stage omits `sar`, emit the profile SAR. No string
parameter may add a filter or option; all strings are enum/rational/overlay fields with
filter-grammar escaping or rejection. Render every SAR ratio in canonical `num/den`
form; never pass colon notation to FFmpeg's `setsar` option, where it is parsed as
separate positional arguments.

## Process, errors, containment and retained artifacts

Before creating a run directory, validate the profile and resolve input/font with
`ProjectRoot.resolve`. Probe the master with FFprobe and verify its file exists, has a
video stream, a positive exact frame count and a positive rational frame rate; capture
its actual duration (stream or format duration, or derive it exactly as frame count / frame
rate when both are absent) and time base. Require video `start_time == 0` (within exact stream
time-base precision); the measured spike assumes frame zero is record-time zero, and a
nonzero input offset would shift the schedule and overlay clock. Reject a nonzero start
as `JobError(CORRUPT_ARTIFACT)` before reserving the run.
The master is read-only. Reserve `post/<shot_id>/v<shot_version:04d>/<run_id>`
exclusively through ProjectRoot; never overwrite/reuse an existing run, and never delete
older outputs. Write metadata/logs through ProjectRoot. FFmpeg's destination is supplied
only through `ProjectRoot.write_with`; all intermediate files, including any temporary
encode and probe reports, are under that same reserved run directory. No OS temp files,
shell redirection, or path composition outside the root. A failed run remains retained
with failure metadata/log.

Use injectable Popen with argv and `shell=False`; no GPU or renderer path. Drain/capture
both streams to a contained log without deadlock, wait for the process, and check return
code. Any process launch failure maps to `JobError(WORKER_UNREACHABLE)`, nonzero FFmpeg
exit with disk-full diagnostics to `JobError(DISK_EXHAUSTED)`, other nonzero exits to
`JobError(INTERNAL)` with retained diagnostics, missing filters/options to
`JobError(INVALID_MANIFEST)`, invalid profile/schedule to `JobError(INVALID_MANIFEST)`,
missing master/font/FFmpeg executable to the appropriate existing
`MISSING_MODEL_ARTIFACT` / `WORKER_UNREACHABLE`, disk-full to `DISK_EXHAUSTED`, and
malformed/missing/empty output or invalid probe results to `CORRUPT_ARTIFACT`. Preserve
the cause and job/shot/run correlation where available. Validate that every reported
output frame has one finite, ordered PTS; malformed/incomplete FFprobe timing evidence or
a non-object JSON root is `CORRUPT_ARTIFACT`. Do not add JobErrorKind values, swallow errors or silently fall
back. Project containment and Pydantic request
validation exceptions remain their existing request/startup families, not JobError.

Record the FFmpeg version banner and pinned binary identity, profile semver and
`supersedes`, shot/version/run/seed, canonical graph/hash, source/output refs, output
hash, schedule, predicted/actual frame count and PTS-delta distribution. Verify output
nonempty and frame count equals prediction. Prior artifacts are immutable.

`identity.pinned_binaries['ffmpeg']` is the exact full UTF-8 output from the installed
`ffmpeg -hide_banner -version` command, with only one final newline normalized away.
Compare the complete identity byte-for-byte before run reservation; a version substring
is not a pin. The measured §8.4 FFmpeg banner hash/x264 build row remains unfilled, so
this adapter enforces the profile's declared full version output and does not claim the
machine-wide §8.4 binary hash has been recorded.

## Negative tests and evidence

Test author works from this contract and repository domain/platform/spike/spec only;
implementation must be physically absent from their test workspace. Tests cover pure
profile acceptance/rejection, exact graph ordering and escaping, deterministic stage
seed range, independent profile/seed behavior, schedule boundaries and drop/freeze clock
semantics, delegation of CameraProfile impact, argv without shell, input immutability,
containment/traversal and symlink rejection, append-only run collisions, fake Popen
success/nonzero/launch failure, missing filters, malformed/empty output and probe
failure, disk errors, metadata, and no render invocation. Test doubles must not replace
ProjectRoot or bypass artifact containment.

The installed machine is measured at FFmpeg 8.1.1-full (Gyan). Required filter names,
`noise.all_seed` range, `loop.start` offset, drawtext support, encoder name and options
must be checked against this binary and recorded by implementation. The spike's existing
results files are measured evidence and are not to be regenerated or changed.
Parent runs the full pytest and Ruff commands in the task brief and reports results.
Running FFmpeg and byte-identical repeated full output hashes is required for an AT-05
claim; do not claim AT-05 if only pure tests or a single encode ran. Golden AT-14 is
separate unless committed two-second master/profile hashes are added (out of scope here).

## Known contract ambiguities to report

The PRD schedule schema uses seconds/durations and includes timestamp-unstable intervals;
the measured spike and existing `ArtifactScheduleEntry` use frame indices/counts and only
drop/freeze. Promotion uses the measured frame-index drop/freeze model and reports that
it does not implement timestamp-unstable windows. Domain `PostProfile` has semver but no
`supersedes`; the adapter records the request's optional `supersedes` metadata without
changing domain entities. FR-21 specifies full dual overlay positions (`in_recording`
and `on_playback`); the spike implements only `in_recording`. This pass preserves the
measured in-recording stage and reports `on_playback` as unsupported rather than
silently misplacing it. Font provenance/licensing and the mapping from CameraProfile's
loose post fields into `PostProfileSpec` remain caller/integration inputs. The runner
must reject a profile whose declared pinned FFmpeg identity differs from the actual
executable identity.
