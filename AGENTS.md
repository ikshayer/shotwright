# CCTV Analog Horror Production Pipeline

**Shared contract for every agent working in this repository**, whichever model or tool
you are. Claude Code reads `CLAUDE.md`, which points here; Codex reads this file
directly. There is deliberately one copy of these rules.

A local, agent-operated pipeline that produces short analog-horror episodes shot as if
through cheap security cameras. Blender renders a neutral master; a versioned FFmpeg
profile degrades it; a creator approves shot by shot.

**Authoritative spec:** `CCTV_Analog_Horror_Production_Pipeline_PRD_v1.2.md`. Section and
FR numbers below refer to it. Where this file and the PRD disagree, the PRD wins — tell
the human rather than resolving it yourself.

**Measured facts live in §8.4 and `spikes/*/FINDINGS.md`.** Five Phase 0 spikes measured
this machine. Do not re-derive those numbers from documentation, and do not invent one:
if a value is needed and not recorded, say so.

## Non-negotiable invariants

These are load-bearing. Violating one silently produces a system that looks correct.

1. **Project-local JSON is the source of truth. SQLite is a rebuildable index and job
   queue** (§10.0). Never make SQLite authoritative; an exported package must reopen
   standalone (AT-09).
2. **Concurrency lives at the API layer**, not the storage engine: `expected_version` on
   every mutation, 409 on conflict.
3. **Versioning is append-only.** New versions carry `supersedes`; artifacts are retained.
   Approval binds to `(shot_id, version)`; upstream change moves downstream to `stale`,
   it does not delete.
4. **Every filesystem write passes through the single path-containment helper** (FR-25).
   No exceptions, including temp files and logs.
5. **Determinism is a product feature** (FR-21, AT-05). Stochastic stages take a seed
   derived from `(shot_id, degrade_seed)`, **clamped to each filter's accepted range** —
   FFmpeg's `noise` caps `all_seed` at `INT32_MAX` and fails hard above it.
6. **`CameraProfile` fields are individually tagged post-only or rebuild-triggering**
   (§4.4). Post-only changes must never trigger a re-render; rebuild-triggering changes
   must invalidate through the dependency graph.
7. **Job failures are a closed enumeration** (`JobError`, FR-27), each classified
   retryable or terminal with a remediation hint. Startup and request validation
   failures are a *separate* family (containment, settings, version conflict) with their
   own types, mapped at the API boundary — they have no retry policy and do not belong in
   the job taxonomy. Never swallow an error, never return a silent fallback.
8. **Degradation never re-renders.** `PostProfile` transforms a master; `PerformanceEdit`
   transforms an audio take without regenerating speech (FR-32).

## This machine

Measured 2026-11-03/04. Paths are literal.

| | |
|---|---|
| Blender | `D:\Blender\blender.exe` — **5.2.0 LTS**, engine id is `BLENDER_EEVEE` (4.4's `BLENDER_EEVEE_NEXT` does **not** work here) |
| ComfyUI | `D:\ComfyUI_3\ComfyUI_windows_portable` — 0.21.1, embedded Python 3.12 |
| FFmpeg | 8.1.1-full (Gyan), on PATH |
| GPU | RTX 5070 Ti Laptop, 11.94 GB, sm_120. Foreign baseline drifts 1.3–2.7 GB |
| RAM | 31.1 GB, and a browser routinely holds 9+ GB |
| Orchestrator env | `.venv` in this repo |

**The ComfyUI tree is immutable.** Never `pip install` into `python_embeded`. The
orchestrator keeps its own `.venv` and owns ComfyUI's lifecycle, launching it with
`--disable-all-custom-nodes --whitelist-custom-nodes ...` — a third-party node
(`comfyui_smznodes`) patches core sampling and breaks generation with an opaque CUDA
error (spike 03).

**Detect, don't assume:** engine ids by assigning and reading back; node inputs from
`/object_info`; GPU devices explicitly pinned (this is a hybrid-graphics laptop and
Cycles enables the iGPU by default).

## Working rules

- **Search before adding an abstraction.** Extend what exists rather than creating a
  parallel one. Do not create a file merely to make files smaller — only at a real
  responsibility boundary.
- **Measure, don't assume.** This project has repeatedly been wrong about documented
  values: official VRAM figures were differently scoped, a licence was misreported by its
  own README, `--reserve-vram` does not cap allocation, and NVML cannot measure a job's
  memory need. If a number matters, measure it and record where.
- **Report verification honestly.** Never say tests, type checks or builds passed unless
  you ran them. State what you did not verify.
- **Rights and consent are requirements, not etiquette** (FR-16). Asset licences are
  recorded per asset; voice identity needs consent, not just a licence; reference media is
  never packaged into an export nor fed to a generative model as an image input.
- **Model weights are executable input** (FR-25). Third-party loaders `torch.load()`
  without `weights_only`. Fetch only from pinned, hashed sources; prefer `safetensors`.

## Layout

```
CCTV_..._PRD_v1.2.md    the spec
references/             REF-001 and the style bible (frames are gitignored, third-party)
spikes/0N_*/            Phase 0 measurements; each has FINDINGS.md and is reproducible
  01 determinism  02 render budget  03 flux fit  04 geometry conditioning  05 voice
.venv/                  orchestrator environment (gitignored)
```

`_work/` directories are disposable spike output and gitignored. `D:\Work\index-tts` is a
sibling checkout, not vendored here.

## Conventions

Python 3.11+ for the orchestrator, Pydantic v2 for entities, FastAPI for the API, pytest
for tests. Type hints on public interfaces. Prefer pure functions for anything
deterministic — the degradation chain, seed derivation, the dependency graph — so they can
be tested without a GPU, a renderer or a filesystem.

## Orchestrating (applies to whichever agent is driving)

Work on this repository is **delegated and adjudicated, never done alone**. Whoever
holds the session is an orchestrator first: it designs, dispatches, verifies and
decides. It does not hand-write what a sub-agent can write, and it does not accept a
sub-agent's report as fact.

**Spawning sub-agents.** Any agent with a shell can spawn one by calling `codex exec`
recursively:

```powershell
$env:CODEX_HOME = "$HOME\.codex-alt"
Get-Content prompt.txt -Raw | codex exec -m <model> --sandbox read-only -C <repo> -o out.txt
```

- Pass long prompts on **stdin**, never as an argument. PowerShell 5.1 mangles native
  arguments somewhere above ~500 characters and Codex then rejects them.
- `-o` writes only the final message, so a whole-package review costs a few lines of
  the orchestrator's context. This is the main lever for working cheaply.
- `--sandbox read-only` for review, `workspace-write` for implementation.
- Models: `gpt-6-luna` implements, `gpt-6.1-sol` reviews. Claude models are reached
  through the Agent tool when the session is Claude Code.

**Rules that were learned the hard way here.**

1. **Never let one agent write both an implementation and its own tests.** It produced
   nine green tests over production code that could not run: a VRAM probe with no
   total-memory accessor, respawn callbacks defaulting to no-ops. The tests validated
   the fakes. A green suite proved nothing.
2. **Write a contract first, then run implementer and test author in parallel from it.**
   Neither reads the other's output. Where they disagree, the contract decides and the
   disagreement is a finding. Done this way the same package produced 12 honest
   failures naming nine real defects — see `orchestrator/jobs/CONTRACT.md`.
3. **Tell test and review agents to report, not fix.** Adjudication stays in one place.
4. **Partition by file** and create package skeletons before dispatching, so concurrent
   agents cannot collide.
5. **Verify independently.** Re-run the suite yourself. Check a claimed enumeration
   against the spec. Every report so far has been honest; that is not a reason to stop.
6. **Ask what they could not do.** The admissions are the most valuable lines in a
   sub-agent report, and have repeatedly exposed gaps in the contract itself.
7. **Review is cheaper than rework.** One read-only review pass returned 18 HIGH
   findings; one discarded package cost far more than several reviews.


## Local Qwen audio/video helper (advisory)

Qwen Omni is available as a local perceptual helper for reference analysis and generated-audio review. This is useful when the driving agent cannot listen directly. Read `orchestrator/adapters/qwen/README.md` and `spikes/23_joint_av_analyzer/CREATOR_INTENT_AND_REVIEW.md`.

From this checkout, use the existing orchestrator Python with `-B -m orchestrator.adapters.qwen`:

- `analyze --media <project-relative-file>` for reference observations.
- `review --media <project-relative-file> --brief-file <project-relative-UTF8-brief>` for generated-audio review. A nonempty intended-sound brief is required; intentional hiss/distortion may be desirable.
- `inspect` checks local runtime presence without model loading; `--dry-run` on analyze/review probes and plans without inference.

Defaults are 25-second windows, 2-second minimum overlap and 500 output tokens per window. End-anchored windows may overlap more. Inputs/reports stay inside this repository; the real backend uses the already isolated spike23 runtime, not the orchestrator/ComfyUI environment for model loading. Do not start competing GPU work, install into existing environments, stop unrelated apps, bypass admission guards or retry automatically.

Read the saved JSON/Markdown paths returned by the CLI. Retain raw Qwen text, window/source times, intent brief, signal metrics, EOS/cap and partial-coverage/error flags. Distinguish creator direction, model claims and measured facts. Qwen advice is supplementary and does not approve versions or establish semantic truth. The helper is separate from the production job/API registry; its lock does not serialize the production scheduler or other GPU apps.

## MiniMax H3 candidate adapter (opt-in)

- Before using H3, read `orchestrator/adapters/minimax_h3/README.md`, the adapter contract, and `spikes/22_minimax_h3_workflows/CREATOR_PROMPT_RUN.md`. Preserve the creator prompt byte-for-byte after UTF-8 decoding and keep the mode video-plus-prompt only.
- Bind the source to a hash-matching MASTER or DEGRADED receipt for the requested historical Shot version; never use references or unbound video. `expected_version` is the current mutation token, separate from `shot_version`.
- Keep H3 disabled unless explicit positive operator VRAM/RAM/disk reservations and throughput floor are configured. Defaults are 8 seconds, 384x288, 24 fps, 20 steps; non-default settings require `experimental=true` and must still satisfy the live schema and all existing guards.
- Do not add automatic H3 retries. Do not install, download, or mutate pinned model/runtime environments. H3 and the default wired GPU queues share the repository-root execution slot with the Qwen native helper; foreign applications and standalone historical spikes are not covered.
- Treat outputs as immutable, unapproved candidates with provenance and relative bundle links. Never change Shot/Episode approvals, replace a master, or promote H3 automatically. Inspect remains model-free.
