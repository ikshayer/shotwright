# Perception contract (2026-11-21)

The step after cut assembly in `GAP_REVIEW.md`'s fix order, agreed with the creator on
2026-11-21. `inspect_output` (`FINISHING_STEPS_CONTRACT.md` R6) lets an agent see one
frame. One frame cannot show motion, flicker or loudness, which are exactly what the
§4.3 rubric (criteria 3 and 4) and FR-08's audio targets test. This contract adds
read-only operations that give an agent pictures across time and measured numbers.

All operations here are **read-only** (they write nothing to the project), are not
creator-only, and reuse `orchestrator/service/inspect.py`'s hardening for every FFmpeg
read: receipt lookup through `store.find_output`, SHA-256 verification first, the
`_VIDEO_DEMUXERS` allowlist with a named demuxer, `-protocol_whitelist file`, no format
probing, fixed-point timestamps, timeouts with kill. Shared helpers may be moved out of
`inspect.py` into the new module; behaviour of `inspect_output` must not change.

## R1 — Contact sheets

| Operation | Method and path | Query | Returns |
|---|---|---|---|
| `contact_sheet` | `GET /projects/{project_id}/episodes/{episode_id}/outputs/{output_id}/contact-sheet` | `frames: int = 9` (2–16), `times: str \| None` (comma-separated seconds, 2–16 values, finite, >= 0, ascending), `mode: "frames" \| "difference" = "frames"` | one PNG |

- Only **video** outputs (master, degraded, a generated video, cuts). Image or scene
  outputs answer 422 pointing to `inspect_output`.
- Without `times`, sample `frames` instants evenly across the output:
  `t_i = duration * (i + 0.5) / frames`. Duration comes from the receipt
  (`frame_count / output_fps`) where it has them, else from ffprobe of the verified file.
  A `times` value at or past the end answers 422.
- `mode=frames`: a grid of the sampled frames, row-major, as close to square as
  possible, each tile labelled with its timestamp (`mm:ss.s`) drawn with Pillow's
  built-in default font (no font file needed).
- `mode=difference`: each tile is the absolute difference between consecutive samples
  (so `n` samples give `n - 1` tiles), contrast-stretched so small changes are visible,
  labelled `t_a→t_b`. This makes flicker, swimming surfaces and unexplained camera
  movement visible.
- The sheet's long edge is at most 1536 px and its PNG at most 4 MiB (else
  `CORRUPT_ARTIFACT`). Tiles are downscaled, never upscaled.

## R2 — The deterministic rubric sample (FR-22, AT-03)

| Operation | Method and path | Returns |
|---|---|---|
| `rubric_sample` | `GET /projects/{project_id}/episodes/{episode_id}/cuts/{output_id}/rubric-sample` | JSON sample plan |

For one cut (EpisodeOutput), the §4.3 sample: **one frame every 3 seconds** of the cut,
plus **two fixed sequences**, the first anomaly beat and the longest neutral interval.

- **Every 3 s:** `t = 0, 3, 6, …` while `t` is before the narrative end (credit cards
  are excluded; the narrative duration is the Timeline's `total_duration_sec`).
- **Interpretation (flagged for the creator; the PRD does not define "sequence"):** a
  fixed sequence is **5 frames 0.5 s apart, centred on its moment**, clamped inside the
  narrative.
  - *First anomaly beat:* the earliest Timeline shot entry whose Shot has an `anomaly`
    and an `event_sec`; its moment is `entry.start_sec + shot.event_sec`. The Shot
    version is the one the cut pinned (from the cut's credit manifest `shot_versions`
    when present, else the current version, and the response says which).
  - *Longest neutral interval:* the longest span of the narrative between anomaly
    moments (and the start and end), centred at its midpoint. With no anomalies, it is
    the whole narrative.
  - If the cut has no anomaly, the anomaly sequence is omitted and the response says so.
- Response: `{output_id, narrative_duration_sec, shot_versions_source, samples: [{index,
  time_sec, reason: "every_3s" | "first_anomaly" | "longest_neutral", shot_id}],
  interpretation}`. `index` is stable and dense from 0. The same cut always yields the
  same plan.

| Operation | Method and path | Body | Returns |
|---|---|---|---|
| `evaluate_rubric` | `POST /projects/{project_id}/episodes/{episode_id}/cuts/{output_id}/rubric-evaluate` | `{scores: [{index, scores: RubricScores}]}` (extra forbidden) | verdict JSON |

- `scores` must contain exactly one entry per sample index of that cut's plan (else 422
  naming the missing or extra indices).
- Verdict, per §4.3: `passed` is true when at least 80% of samples satisfy at least 4 of
  the 5 criteria **and** `no_unmotivated_movement` holds on **every** sample. Return
  `{passed, samples, passing_samples, pass_ratio, criterion_3_failures: [index…],
  per_sample: [{index, passed}]}`.
- Nothing is stored. The summary tells the agent to record the outcome with
  `record_review` (kind `episode_cut`, with `rubric_scores` and, for a failing cut, the
  creator's `waiver_text`).

## R3 — Measured metrics

| Operation | Method and path | Query | Returns |
|---|---|---|---|
| `measure_output` | `GET /projects/{project_id}/episodes/{episode_id}/outputs/{output_id}/metrics` | `samples: int = 12` (1–60) | metrics JSON |
| `measure_asset` | `GET /projects/{project_id}/assets/{asset_id}/metrics` | none | metrics JSON (audio assets) |

`measure_output` (video outputs; image outputs get only the `image` block below):

- `container`: format, duration, overall bitrate (ffprobe of the verified file).
- `video`: codec, width, height, fps; and per sampled instant (evenly spaced as in R1)
  from FFmpeg `signalstats`: luma mean (`YAVG`), luma min/max, temporal luma difference
  from the previous frame (`YDIF`, flicker), saturation mean (`SATAVG`); and edge energy
  (mean of an `edgedetect` pass). Also the clip-wide letterbox/pillarbox estimate from
  `cropdetect` (the detected active area versus the full frame), and scene-change
  times above a fixed threshold of 0.3.
- `audio` (when the output has an audio stream): integrated loudness (LUFS), loudness
  range and true peak from `ebur128` (with `peak=true`), and silence intervals from
  `silencedetect` at −50 dB, 0.5 s minimum.
- `image` (image outputs): width, height, mode, and the same luma/saturation/edge
  numbers computed with Pillow.

`measure_asset` accepts only Assets of an audio type with a local `file_path` (else
422), verifies the Asset's bytes (`store.verify_asset` or the equivalent checksum
check), and returns `container` and `audio` as above. Accepted audio extensions and
demuxers: `.wav` → `wav`, `.flac` → `flac`, `.mp3` → `mp3`, `.m4a` → `mov`,
`.ogg` → `ogg`.

- Every number states its unit in its key or a sibling `units` map.
- One FFmpeg pass per filter family is fine; the whole call must finish within 120 s
  (kill FFmpeg and answer `BUDGET_EXCEEDED` otherwise).

## R4 — Descriptions and CLI

Every new operation's summary meets the description standard and says how it fits the
workflow (`rubric_sample` → `contact_sheet` with `times` or `inspect_output` per sample →
`evaluate_rubric` → `record_review`). PNG-returning operations use the same CLI JSON
representation as `inspect_output` (`{media_type, data_base64}`) and declare
`image/png` only in OpenAPI.

## Amendments after review (2026-11-21)

The Codex review reproduced these against real media; each is a requirement now.

- **P1 — Bounded subprocess output (was HIGH).** Every FFmpeg/ffprobe call reads stdout
  and stderr incrementally with hard caps: stdout at most 16 MiB (refuse beyond it),
  stderr keeping only what parsing needs (stream the lines and keep parsed values, plus
  at most the last 64 KiB for error messages). Per-frame filters (cropdetect, scene,
  signalstats) must not make memory grow with clip length beyond the parsed values.
- **P2 — Sample by time, not frame index.** Every sampled instant (contact sheets,
  sampled metrics) is taken by timestamp seek, as `inspect_output` does, so variable
  frame rate clips give the frame visible at that time.
- **P3 — Silent audio is valid.** `-inf` loudness or true peak is reported as `null` with
  `"silent": true` in the audio block, not as an error.
- **P4 — Undecodable audio is refused.** If FFmpeg decodes no audio samples, or decodes
  audio covering less than 90% of the container's audio stream duration, or reports a
  skipped external track, answer `CORRUPT_ARTIFACT` rather than measurements.
- **P5 — Labels fit inside the caps.** Reserve label space when sizing tiles, so every
  valid request (2–16 frames of any aspect) yields a sheet within 1536 px and 4 MiB.
- **P6 — 8-bit units.** Convert frames to 8-bit before `signalstats`/`edgedetect` (e.g.
  `format=yuv420p` or `gray`), so the 0–255 units hold for higher-bit-depth sources; also
  report the source pixel format.
- **P7 — No silent fallback in version pinning.** If a verified credit manifest exists
  but lacks a row for a selected shot, refuse with `INVALID_MANIFEST` naming the shot.
  `shot_versions_source` is `credit_manifest` only when every shot came from it.
- **P8 — Undetermined is not "no bars".** When cropdetect yields no detection, report the
  letterbox estimate as `null` with `"letterbox_determined": false`.
- **P9 — One budget for the whole call.** The 120 s deadline starts when the operation
  starts and covers verification, hashing, subprocesses and Pillow work.
- **P10 — Per-frame shot ids.** Each sample's `shot_id` is the shot on screen at that
  sample's time, not the shot that defined the sequence.

**Second review (2026-11-21).** P1, P3–P8 and P10 confirmed closed. Further:

- **P2b — Sparse VFR.** The frame "visible at t" is the last frame whose presentation
  time is at or before `t`, however long ago it started. Find it without decoding or
  buffering a window of frames: list packet presentation times up to `t` with ffprobe
  (packets only, no decoding, through the same hardened input), take the greatest one
  at or before `t`, then extract exactly that frame by an accurate seek to it with
  `-frames:v 1`. This applies to contact sheets and to sampled `signalstats`. Memory
  must not grow with the number of frames before `t` beyond the timestamps themselves
  (bounded by the P1 stdout cap). A clip with frames at 0, 2, 2.1, 2.2 s sampled at
  0.5 s and 1.5 s gives the frame from 0 s both times.
- **P1b — Reader startup.** If a reader thread fails to start, FFmpeg is still killed
  and every started reader joined. *(Fixed by the parent.)*
- **P9 limit (accepted).** Hashing during receipt or asset verification runs inside the
  store and cannot be interrupted mid-file; the deadline is checked immediately after
  verification, so an overrun is reported as `BUDGET_EXCEEDED` but not prevented. Making
  verification itself interruptible would need a store change and is recorded as a gap.

## Non-goals

The Qwen audio review (a later GPU step). Storing metrics. Any change to approvals.

## File ownership

**Implementer** (Codex `gpt-6-luna`, effort max): a new `orchestrator/service/perception.py`,
`orchestrator/service/inspect.py` (only to share helpers; no behaviour change),
`orchestrator/api/routes.py`, `orchestrator/api/app.py`, `orchestrator/api/cli.py`.
Existing tests may be updated only for operation lists (`tests/api/test_parity.py`,
`tests/api/test_entry_path.py`, `tests/api/test_adapters.py`).

**Test author** (separate agent): only `tests/api/test_perception_contract.py`, using
real FFmpeg on clips it generates (e.g. `testsrc`, a flickering clip, a letterboxed clip,
a `sine` audio track at a known level, a clip with a silent gap) so the metrics are
checked against known inputs.

**Do not touch:** `spikes/**` and any file not listed.

## Done when

Every requirement has a test; the full suite passes (run alone, `--basetemp` on D:);
ruff reports nothing beyond `main`'s.
