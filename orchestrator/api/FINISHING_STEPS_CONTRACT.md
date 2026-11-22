# Finishing steps contract, part A (2026-11-21)

Step 2 of the fix order in `GAP_REVIEW.md`, first half. Step 1
(`orchestrator/service/APPROVAL_INTEGRITY_CONTRACT.md`) made approvals trustworthy at
the service layer. This contract exposes them, plus recovery, camera-profile revision,
generic cancellation and output read-back, as operations. Cut assembly is part B (a
separate contract), because it needs an episode-scoped job kind.

`orchestrator/api/routes.py` `OPERATIONS` remains the single operation list: every
operation here becomes an HTTP route, a CLI command and (unless R2 hides it) an MCP tool.

## Creator decisions

- **D1 (2026-11-21).** The creator approves by default; an agent may record its opinion
  as a Review; `approvals.agent_may_approve` (default off) lets agent Reviews approve.
- **D4 (2026-11-21).** With no review UI yet, creator approvals go through the CLI. When
  the creator tells the agent in chat to approve something, the agent runs the CLI
  command on their behalf. Over HTTP/MCP an agent can only record opinions.

## R1 — Who is calling

`OperationContext.__init__` gains a keyword `actor: Literal["creator", "agent"] =
"agent"`, stored as `self.actor`.

- The CLI (`api/cli.py`, the one `OperationContext(...)` construction) passes
  `actor="creator"`: the CLI runs in-process in the creator's own terminal.
- The HTTP app (`api/app.py` `create_app`) passes `actor="agent"`. Every HTTP request,
  MCP included, is an agent request. No header, query parameter or body field can
  change the actor.
- The default is `"agent"` so a forgotten construction fails safe.

## R2 — Creator-only operations

`Operation` gains `creator_only: bool = False`, beside `upload`. It is set on
`approve_framing`, `approve_shot` and `approve_cut` (R4).

- **Enforcement.** When a creator-only operation is invoked with `actor == "agent"` and
  `settings.approvals.agent_may_approve` is false, it raises a new `CreatorOnly`
  exception (define it in `api/errors.py` beside the existing API-boundary exceptions)
  before reading or writing any project data. *(Clarified after review: FastAPI
  validates the request body first, so a malformed body is a 422 before the 403; that
  reads no project data and is accepted.)* It maps to **HTTP 403**, error code
  `creator_only`, with a message that says approvals are made by the creator through the
  CLI and names the setting `approvals.agent_may_approve`. It is an API-boundary error,
  not a `JobError` (AGENTS.md invariant 7).
- **Discovery.** `build_manifest` omits creator-only operations from `/tool-manifest`
  when `agent_may_approve` is false, so MCP clients do not see tools that would always
  refuse them. When the setting is true they are listed. The HTTP routes, OpenAPI and
  CLI commands always exist. `mcp.py` needs no change: it only sees the manifest. If
  `build_manifest` cannot reach the app's `Settings`, store them on `app.state` in
  `create_app`.

## R3 — Recording and listing Reviews

| Operation | Method and path | Body / query | Returns |
|---|---|---|---|
| `record_review` | `POST /projects/{project_id}/reviews` | `ReviewRecording` | the `Review` |
| `list_reviews` | `GET /projects/{project_id}/reviews` | query `target_kind`, `target_id`, `episode_id` (all optional, exact-match filters) | list of `Review` |

`ReviewRecording` (extra fields forbidden): `target_kind: ReviewTargetKind`,
`target_id: str`, `target_version: int (>=1)`, `episode_id: str | None = None`,
`approval_state: Literal["approved", "rejected"]`, `comment: str = ""`,
`rubric_scores: list[RubricScores] = []`, `waiver_text: str | None = None`.

- `record_review` calls `service.approval.record_review` with `actor=context.actor`.
  There is no `actor` field in the body.
- `list_reviews` needs `ProjectStore.list_reviews() -> list[Review]`: every
  `reviews/*.json`, read through `ProjectRoot`, sorted by `(created_at, review_id)`.
  Legacy (unscoped) Reviews are listed too, unchanged.
- Neither is creator-only: agents record opinions this way.

## R4 — Approvals

| Operation | Method and path | Body | Returns |
|---|---|---|---|
| `approve_framing` | `POST /projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/framing/approve` | `ApprovalRequest` | the `Shot` |
| `approve_shot` | `POST /projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/approve` | `ApprovalRequest` | the `Shot` |
| `approve_cut` | `POST /projects/{project_id}/episodes/{episode_id}/cut/approve` | `CutApprovalRequest` | the `Episode` |

`ApprovalRequest` (extra forbidden): `expected_version: int (>=1)`,
`review_id: str | None = None`, `comment: str = ""`. `CutApprovalRequest` adds
`cut_version: int (>=1)` (the Timeline store version of the cut being approved).

- All three are creator-only (R2).
- When `review_id` is given, the approval uses that Review.
- When `review_id` is omitted, the operation first records an approving Review with
  `actor=context.actor`, `comment`, and the target: `framing` or `shot` for the path's
  shot at its current content version, or `episode_cut` for the episode at
  `cut_version`. It then approves with that Review. If the approval then fails, the
  Review stays recorded (a Review is a dated opinion, never revised), and the error
  says which Review was recorded.
- Every call passes `agent_may_approve=settings.approvals.agent_may_approve` to the
  service function. The service's checks from step 1 remain the authority.

## R5 — Recovery, profile revision and cancellation

| Operation | Method and path | Body | Returns |
|---|---|---|---|
| `resume_invalidations` | `POST /projects/{project_id}/invalidations/resume` | `{}` (extra forbidden) | `{"resumed": [cascade_id, ...]}` |
| `revise_camera_profile` | `POST /projects/{project_id}/camera-profiles/{camera_id}/revise` | `{camera_profile, expected_version}` (extra forbidden) | `{"camera_profile": ..., "invalidated": [shot_id, ...]}` |
| `cancel_job` | `POST /projects/{project_id}/jobs/{job_id}/cancel` | `{}` (extra forbidden) | `{"job_id", "state", "cancellation_requested": true}` |

- `revise_camera_profile` requires `camera_profile.camera_id` to equal the path (the
  existing path-mismatch 400), and calls `service.dependencies.revise_camera_profile`,
  which scans every episode itself.
- `cancel_job` calls `JobRunner.cancel(project_id, job_id)` for any job kind. An unknown
  job is 404. A job already completed, failed or cancelled is returned unchanged with
  `cancellation_requested: false`. `cancel_h3_candidate` stays as it is.

## R6 — Reading outputs back

| Operation | Method and path | Query | Returns |
|---|---|---|---|
| `list_shot_outputs` | `GET /projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/outputs` | `record_version`, `kind` (optional filters) | list of `ShotOutput` |
| `list_cuts` | `GET /projects/{project_id}/episodes/{episode_id}/cuts` | none | list of `EpisodeOutput` |
| `inspect_output` | `GET /projects/{project_id}/episodes/{episode_id}/outputs/{output_id}/frame` | `at_sec: float = 0.0` | one PNG image |

`inspect_output` is how an agent sees its work. Over MCP the PNG becomes an image block
(`mcp.py` already renders `image/png`).

- It finds `output_id` among the episode's ShotOutputs (all shots, all versions) and
  EpisodeOutputs. Unknown id: 404.
- It verifies the artifact against its recorded `content_sha256` with the store's
  existing verification before reading it. A mismatch is `JobError(CORRUPT_ARTIFACT)`.
- **Images** (`preview`, `still`, and a `generated` output whose file is an image):
  decoded with Pillow and returned as PNG. `at_sec` is ignored.
- **Video** (`master`, `degraded`, a `generated` video, and cuts): FFmpeg
  (`settings.binaries.ffmpeg_exe`) seeks to `at_sec` and writes one PNG frame to its
  stdout. Use `-ss <at_sec> -i <file> -frames:v 1 -f image2pipe -vcodec png -`. Nothing
  is written to disk. Enforce a 60-second timeout and kill FFmpeg on expiry. `at_sec`
  must be finite and >= 0 (else 422). If FFmpeg produces no frame (past the end), the
  error is 422 and says `at_sec` is past the end of the output.
- `scene` outputs (`.blend`) have no frame: 422 telling the caller to use
  `render_preview` and inspect the preview.
- **Size.** The returned image's long edge is at most 1024 px (downscale with Pillow,
  keeping the aspect ratio) and its bytes at most 2 MiB (else `CORRUPT_ARTIFACT`).
- Every read goes through `ProjectRoot`. The HTTP endpoint returns the bytes as
  `image/png` with `X-Content-Type-Options: nosniff`, the same way `asset_preview` does.
- Audio takes are out of scope: MCP has no audio content type here (the Qwen helper
  remains the way to review audio).

## R7 — Descriptions

Every new operation's `summary` follows the standard the existing ones now meet: what it
does, what to send, what must happen first, what it returns. Creator-only operations say
so and name the setting. `inspect_output` says which output kinds give a frame.

## Non-goals

Cut assembly (part B). A review UI. Approving style bibles (nothing creates one yet).
Changes to `promote_h3_candidate`: its Review check from step 1 already refuses agent
Reviews unless the setting is on.

## File ownership

**Implementer** (Codex `gpt-6-luna`, effort max) may edit: `api/routes.py`,
`api/app.py`, `api/cli.py`, `api/manifest.py`, `api/errors.py`,
`store/json_store.py` (R3 `list_reviews` only), and a new module for frame extraction
if one is warranted (e.g. `orchestrator/service/inspect.py`). It may update existing
tests only mechanically: lists of operations and request fixtures in
`tests/api/test_parity.py`, `tests/api/test_entry_path.py`,
`tests/api/test_adapters.py`, and any manifest test that assumes every route is listed
(adjust it for R2's omission). It must not delete or weaken an assertion, and must list
every test file it touched.

**Test author** (separate agent, from this contract only) writes only
`tests/api/test_finishing_steps_contract.py`. Video frame tests use real FFmpeg (on PATH
on this machine) against a tiny clip the test generates itself in its temp project
(e.g. FFmpeg `testsrc`), and skip with a stated reason if FFmpeg is absent.

**Do not touch:** `spikes/**`, `orchestrator/service/approval.py`,
`orchestrator/service/dependencies.py`, and any file not listed above.

## Done when

Every requirement has a test in the test author's file. The full suite passes with
`--basetemp` on D:. Ruff reports no findings beyond `main`'s. The implementer reports
files changed, tests touched mechanically, and anything this contract left ambiguous.
