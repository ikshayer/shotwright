# Contract — render progress, and separating scene assembly from rendering

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## Why this exists

The pipeline now composes its adapter requests and reaches `running`, then dies here:

```
error_kind: budget_exceeded
error_message: "VRAM thrash suspected: job progress rate fell below configured floor"
traceback: queue.py _fail_throughput_stall
```

**The throughput watchdog is killing a healthy render.** A render reports no progress at
all, so once the startup allowance lapses a job sitting at `progress: 0.0` is
indistinguishable from a stall. §8.4 measures 0.56–0.63 s/frame, so a 12-second shot at a
60 fps master is several minutes of entirely legitimate silence.

Three passes have now circled this. The adapter reviews demanded
`throughput_floor_per_second=0`; FR-12 made the queue startup-aware; the floor pass made
the adapters pass a startup allowance. **At no point did anything report progress**, so the
floor still fails a healthy render — it merely fails later. The creator has chosen to fix it
properly rather than exempt render work again.

## Decision 1 — the adapter reports progress, and it counts frames rather than parsing text

**Progress comes from completed output, not from Blender's stdout.** The plan already
carries `frame_end` (`build_scene_plan`), and `runner.py` already computes the expected
frame paths `frames/frame_%06d.<ext>` for `1..frame_end`. So:

> `job.progress` = completed frame files ÷ `frame_end`

Counting produced artifacts is robust against Blender's output format changing, which
parsing `Fra:` lines is not. Do not parse stdout for progress.

Requirements:

1. **Progress updates while the subprocess runs**, through the adapter's existing monitor
   path — not only at the end. A single update at completion is worthless to the watchdog.
2. **Monotonic, and never above 1.0.** A frame file that appears and is later rewritten must
   not make progress go backwards.
3. **Every path still goes through `ProjectRoot`** (FR-25). Counting means resolving a
   contained directory, not globbing an absolute path built by hand.
4. **If progress cannot be determined, fail loudly and say why.** A render that cannot
   report progress is the defect this contract exists to remove; it must not silently sit at
   zero and be killed as a stall. Name the reason — a missing plan field, an unresolvable
   frames directory — rather than letting the watchdog guess.
5. The frame-rate question is already settled in code: the master is **60 fps**
   (`scene.py` sets `"fps": 60` in the plan). Do not re-derive it from `CameraProfile.fps`,
   which is the **post-only output** rate (12.0 by default) and a different thing entirely.
   Confusing them would make progress wrong by a factor of five.

## Decision 2 — `build_scene` assembles; it does not render

As built, `build_scene` and `render_preview` both compose a `RenderRequest` and run the
Blender adapter, so "scene" and "preview" are two names for the same work. They separate:

- **`build_scene`** composes the request, calls `build_scene_plan()`, and persists the plan
  and its `SceneSpec` as a `ShotOutput` of kind **`scene`**. It launches **no Blender
  subprocess**, performs no render, and **takes no GPU lease**.
- **Rendering** (`render_preview`, and a master render) consumes a scene and produces
  `preview`/`master` outputs. It keeps the GPU lease and is where decision 1 applies.

Consequences to carry through, not to discover later:

- **`AdapterKind.SCENE` must stop taking the GPU lease.** The integration contract's
  Amendment 3 says `scene`, `render` and `generate` take it; scene no longer does.
  **Amend `ADAPTER_INTEGRATION_CONTRACT.md`** rather than leaving the two documents
  disagreeing.
- A scene job is CPU-only, so it should use the CPU admission path (`preflight_cpu`) that
  landed in `6256a48`, exactly as degradation does.
- Scene assembly is fast and needs no progress reporting; the floor question does not arise
  for it.

## Also in scope, both small

- **The startup allowance has no margin.** `resources.startup_allowance_seconds` defaults to
  `74.0` and §8.4's measured cold Blender shader cache is **~74 s**, so a bound equal to its
  own measurement fails about half the time. Raise it to a **documented bound at least twice
  the measured figure**, labelled a derived bound rather than a measurement — the same
  treatment already applied to ComfyUI's 33.5 s value. Note that with progress reporting the
  allowance only has to cover startup until the **first frame**, not the whole render.
- **`error_origin` is `null` for queue-raised failures that are not `INTERNAL`.** The
  watchdog's own `BUDGET_EXCEEDED` names no layer. Any failure raised by the queue should
  report `job_queue`, whatever its kind. `DIAGNOSTIC_ORIGINS` already pins the vocabulary.

## Out of scope

`generate_media` (still blocked on a machine-readable pinned-model manifest; §8.4's hashes
are prose, and inventing provenance for executable weights is forbidden by FR-25);
`degrade_media` wiring; the Phase 1 shot itself; a real procedural scene builder.

## Test rules

- No GPU, Blender, ComfyUI, FFmpeg or network. Fake the subprocess; create frame files to
  simulate progress.
- **The regression test is the defect:** a long render that reports progress is **not**
  killed by a nonzero throughput floor. Drive the real `JobQueue` with a nonzero floor and a
  faked render that produces frames slowly, and assert the job completes rather than failing
  `budget_exceeded`. A test that passes with the floor at zero proves nothing.
- Required cases: progress rises as frame files appear and is monotonic; progress never
  exceeds 1.0; a render that cannot determine progress fails loudly naming why, rather than
  stalling; `build_scene` launches no subprocess, takes no GPU lease, and persists a
  `ShotOutput` of kind `scene`; a render still takes the lease; the startup allowance default
  exceeds the measured figure; a queue-raised `BUDGET_EXCEEDED` reports origin `job_queue`.
- Assert the 60 fps master is used for the frame total, **not** `CameraProfile.fps`.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline on `main`: **1323 passed, 33 skipped**, ruff clean, about 170 s.

**The real criterion is the pipeline.** Drive the CLI — project, episode, script, shots,
`build-scene`, then a render — and report the job records. `build_scene` must succeed and
write a `scene` output. The render must not die of `budget_exceeded`; if it fails for a
genuine Blender reason, the record will now say so, and that is progress worth reporting
honestly.
