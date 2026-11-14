# Phase 1 traceability note — the accepted 16.5 s shot

PRD §14's Phase 1 Exit clause requires "a traceable source package". The creator decision
of 2026-11-06 recorded in the PRD settles what that means here:

> **"A traceable source package" means the provenance chain, not a built export.** […]
> Phase 1 exits on the provenance that already exists and is verifiable […] **FR-13 stays
> Phase 2**, and the `export` operation may continue to answer 501 through the Phase 1
> gate. A short written traceability note, demonstrating the chain on the accepted shot,
> stands in for the archive.

This is that note. Every value below was read back out of the project store by
`_work/h3_stage/dump_chain.py`, not transcribed from a run log, and the two content hashes
were **recomputed from the bytes on disk** and compared against their records. Three hashes
were re-verified in total: the promoted output, the source master, and the prompt (twice —
from `prompt.txt` and from the run record's retained `invocation`).

Accepted shot: project `exit`, episode `ep1`, shot `s1` version 1, in
`_work/phase1_restage4`.

## The chain, from delivered frames backwards

### 1. The promoted output — what was accepted

| field | value |
|---|---|
| `output_id` | `e9b3bbecfcd34aab8f66d3951a0b7bf2` |
| `kind` | `generated` |
| `relative_path` | `generated/h3/s1/v0001/3f4b87199fb444949f9e2c1f82aac453/candidate.mp4` |
| `content_sha256` | `0d70d0f821968435c568edbd38f1e56aedcd4b04774403d9d397670a9b63b25a` |
| `engine_id` | `MINIMAX_H3` |
| `engine_build` | `comfyui 0.44.0+2c75dc1b379a59e0880619d029607af5169a62ef` |
| `record_version` | 1 (the Shot version it was produced from) |

Re-hashing the file on disk reproduces `0d70d0f8…` exactly. The `engine_build` string is
not a constant in the code: it is assembled at promotion time from the candidate's own
`report.json`, whose `server_ready.system_stats.system.comfyui_version` reads `0.44.0` and
whose `runtime.source_receipt.commit` reads `2c75dc1b379a59e0880619d029607af5169a62ef`.

### 2. The candidate — how those frames were made

| field | value |
|---|---|
| `candidate_id` / `run_id` | `3f4b87199fb444949f9e2c1f82aac453` |
| `job_id` | `b7f1d057652d43dda58a3b7e3f1cbf82` |
| `prompt_sha256` | `1f5f639d562e531c45f568e498a092132c46f2897ec8127bad703fdcbe84c005` |
| `seed` | `8263381764361378` |
| shape | 396 frames @ 24 fps, 352×288, 20 steps, 16.5 s |
| `experimental` | `False` |
| `source_output_id` | `22cb6ab39a2a411684b70e4933c8811a` |
| `source_sha256` | `a92435ffa9eba95dda1139ece41c950774a851330e18b15b0761e184d7c886b7` |

The prompt bytes at `…/3f4b8719…/prompt.txt` re-hash to the recorded `prompt_sha256`, so
the generation intent the §4.3 score was judged against is pinned, not remembered.

`experimental=False` is load-bearing rather than decorative: `promote_h3_candidate` refuses
to promote an experimental candidate, so the accepted output is necessarily one produced at
a shape in `_BASELINE_SHAPES` — here `(16.5, 352, 288, 24, 20)`, the shape Stage D
established.

### 3. The link to its input — the join that makes the chain a chain

`H3Candidate.source_sha256` equals the master's `ShotOutput.content_sha256`:

```
candidate.source_sha256   a92435ffa9eba95dda1139ece41c950774a851330e18b15b0761e184d7c886b7
master.content_sha256     a92435ffa9eba95dda1139ece41c950774a851330e18b15b0761e184d7c886b7
recomputed from disk      a92435ffa9eba95dda1139ece41c950774a851330e18b15b0761e184d7c886b7
```

So the H3 stage can be shown to have consumed *this* master and no other, by content rather
than by filename.

### 4. The master — the render the candidate consumed

| field | value |
|---|---|
| `output_id` | `22cb6ab39a2a411684b70e4933c8811a` |
| `relative_path` | `renders/s1/v0001/09898ffcd7e4414ca491b3e0aedadd9a/attempt-0001/master.mkv` |
| `engine_id` / `engine_build` | `BLENDER_EEVEE` / `fbe6228777e7` |
| render `run_id` | `09898ffcd7e4414ca491b3e0aedadd9a` (in the path) |

26.000 s at 60 fps. The H3 window consumed 8.5 s → 25.0 s of it.

### 5. The run record

`GenerationRun` `3f4b87199fb444949f9e2c1f82aac453`: tool `minimax_h3`, status `completed`,
seed `8263381764361378`, settings `{duration_seconds: 16.5, width: 352, height: 288,
fps: 24, steps: 20, experimental: False}`, input `…/master.mkv`, outputs `candidate.mp4`,
`audio.wav`, `report.json`. Its `invocation` field retains the prompt text verbatim: those
3,910 bytes re-hash to `1f5f639d…`, the same `prompt_sha256` the candidate pins, so the
intent can be recovered from the run record alone even if `prompt.txt` were lost.

### 6. The shot and its anomaly

Shot `s1` v1: 26.0 s, event at 8.0 s, camera profile `low-light-bw`, post profile
`low-light-bw`, anomaly `linear_walk_path` with `figure_asset_id = entity`. Episode `ep1`,
number 1.

### 7. The asset — no user-supplied media

| field | value |
|---|---|
| `asset_id` | `entity` |
| `type` / `provider` | `model` / `TRELLIS2` |
| `generated` | `True` |
| `checksum` | `8ce785cb432a185a31d4d5467b52d90c250dc24aa9af3df57355edadc77a28f6` |
| `license` | Restricted to internal evaluation; not cleared for redistribution. |
| `reuse_scope` | internal evaluation only |

The figure is **generated**, not supplied, which is what the Exit clause's "no
user-supplied assets" turns on. The only non-generated file in the shot is the overlay
typeface, copied from the machine's own `C:/Windows/Fonts/consola.ttf`: the pipeline ships
no font because redistributing one carries a licence obligation, and a font is not footage,
an image or a model. `_work` is gitignored, so nothing is redistributed.

### 8. The approval

`Review` `rev-16-5-creator` targets `3f4b87199fb444949f9e2c1f82aac453` v1, state
`approved`, carrying **6 `RubricScores` records, 5 passing** (83.3%, against §4.3's ≥80%
with criterion 3 mandatory). `waiver_text` is null — none was needed, because the cut
passes on the rubric's own arithmetic. The scoring is written up in
`PHASE1_RUBRIC_SCORE_H3.md`.

## What this demonstrates

Any frame of the accepted shot can be traced to the Shot version, the scene plan, the
engine build and the run that made it, with content hashes joining each hop and two of them
re-verified against the bytes on disk while writing this note. The append-only job journal
at `state/jobs/*.jsonl` holds the per-job history behind the same ids, including the failed
attempts.

`export` still answers 501, as the 2026-11-06 decision permits through this gate. FR-13
remains Phase 2.
