# Approval integrity contract (2026-11-21)

Step 1 of the fix order in `GAP_REVIEW.md`. Approvals are not reachable through any
operation yet (step 2 exposes them). This contract closes the defects that would make
those approvals untrustworthy the moment they are exposed. It changes no MCP/HTTP
operation except `revise_location`'s request body (R8).

Spec: `CCTV_Analog_Horror_Production_Pipeline_PRD_v1.2.md` §10.1 ("Review is the only
writer of approval state"), §10.2 / line 872 ("any shot moving to `stale` or `draft`
revokes that cut approval"), §16 risk table ("agent recommends but cannot redefine
artistic intent"), AT-12.

## Creator decisions (2026-11-21)

- **D1 — Who approves.** The creator approves by default. An agent may record its
  opinion as a Review, but an agent-authored Review does not approve anything unless
  the operator turns on a setting that lets agent Reviews approve. Default: off.
- **D2 — Shot-list change under an approved cut.** The change goes through and the
  cut approval is revoked automatically.
- **D3 — Invalidation scope.** `revise_location` (and `revise_camera_profile`) find
  the affected episodes themselves; the caller no longer supplies them.

## Defects closed

| Id | Defect | Where today |
|---|---|---|
| G1 | A Review binds only `target_id` + `target_version`. Shot ids are episode-scoped, so a Review for `ep1/S1@1` also approves `ep2/S1@1`; ids also collide across shots, cuts, style bibles and H3 candidates. | `domain/entities.py` `Review`; `domain/state.py` `check_review_approval` |
| G2 | `propose_shots` changes `Episode.shot_ids` and keeps `approved_cut_version` / `approval_review_id`. | `service/dependencies.py` `propose_shots` |
| G3 | The Timeline is one unversioned file; assembling a changed cut overwrites the source edit behind every earlier `EpisodeOutput`. | `store/json_store.py` `_LAYOUTS[Timeline]`; `service/timeline.py` `assemble_episode` |
| G4 | `revise_location` / `revise_camera_profile` invalidate only the episodes the caller lists. | `service/dependencies.py` `_revise` |
| G5 | `Review` records can be replaced by a matching-token `save`; a Review is meant to be a dated, never-revised opinion. | `store/json_store.py` `save` |
| G6 | `approve_framing` writes approval state with no Review at all, so nothing records who approved framing. | `service/approval.py` `approve_framing` |

## Requirements

### R1 — Review scope and actor (G1)

Add three fields to `Review` (`orchestrator/domain/entities.py`):

```python
ReviewTargetKind = Literal["shot", "framing", "episode_cut", "style_bible", "h3_candidate", "location"]

target_kind: ReviewTargetKind | None = None
episode_id: str | None = Field(None, min_length=1)
actor: Literal["creator", "agent"] | None = None
```

Model validation:

- **Scoped record:** `target_kind` and `actor` are both set. **Legacy record:** both are
  `None` and `episode_id` is `None`. Any other combination is invalid.
- For `target_kind` in `{"shot", "framing", "episode_cut", "h3_candidate"}`,
  `episode_id` is required. For `"episode_cut"`, `episode_id == target_id`.
- For `"style_bible"` and `"location"`, `episode_id` must be `None`.
- `schema_version` does not change: the new fields are optional, so every existing
  Review file still loads. Existing validators (`approval_state` is approved or
  rejected) are unchanged.

Export `ReviewTargetKind` from `orchestrator.domain.entities`.

### R2 — The pure Review check (G1, D1)

Replace the signature of `check_review_approval` in `orchestrator/domain/state.py`:

```python
def check_review_approval(
    review: Review, *, target_kind: ReviewTargetKind, target_id: str,
    target_version: int, episode_id: str | None, agent_may_approve: bool,
) -> None
```

It raises `ReviewApprovalError` (message names the Review id and the mismatch) when:

1. the Review is a legacy record (`target_kind is None`): "unscoped legacy Review …;
   record a new Review" — legacy Reviews never approve anything (fail closed);
2. `target_kind`, `target_id`, `target_version` or `episode_id` differ from the request;
3. `approval_state` is not `approved`;
4. `actor == "agent"` and `agent_may_approve` is false: the message must say that agent
   Reviews are opinions and name the setting `approvals.agent_may_approve`.

Checks run in that order. Every caller passes all keywords explicitly:

| Caller | target_kind | target_id | episode_id |
|---|---|---|---|
| `approve_shot` | `shot` | shot_id | episode_id |
| `approve_framing` (R5) | `framing` | shot_id | episode_id |
| `approve_episode` | `episode_cut` | episode_id | episode_id |
| `approve_style_bible` | `style_bible` | project_id | `None` |
| `adapters/export/package.py` `_bind_cut` | `episode_cut` | episode_id | episode_id |
| `service/minimax_h3.py` promotion | `h3_candidate` | candidate_id | the candidate's episode_id |

`agent_may_approve` comes from `Settings.approvals.agent_may_approve` (R9) wherever a
`Settings` is in scope (export). The four `approve_*` functions take a new keyword
`agent_may_approve: bool = False`; the future API layer (step 2) passes the setting.
H3 promotion is already reachable (`promote_h3_candidate`): if no `Settings` is in scope
there, add the same keyword and have its `api/app.py` dispatch pass
`settings.approvals.agent_may_approve`. Export re-checks with the **current** setting: a cut approved by an agent while
the setting was on cannot be exported after it is turned off. That is intended (fail
closed).

### R3 — Recording a Review

Add to `orchestrator/service/approval.py`:

```python
def record_review(
    store: ProjectStore, *, target_kind: ReviewTargetKind, target_id: str,
    target_version: int, episode_id: str | None, actor: Literal["creator", "agent"],
    approval_state: ShotStatus, comment: str = "",
    rubric_scores: Sequence[RubricScores] = (), waiver_text: str | None = None,
    review_id: str | None = None, now: datetime | None = None,
) -> Review
```

- Runs under `store.command()`.
- `review_id` defaults to `uuid.uuid4().hex`; `created_at` defaults to
  `datetime.now(timezone.utc)` (`now` exists for deterministic tests).
- Builds a scoped Review (R1 validation applies) and verifies the target exists before
  writing. Failure is `JobError(INVALID_MANIFEST)` naming the missing target:
  - `shot`, `framing`: `Shot` `target_id` at content version `target_version` in `episode_id`;
  - `episode_cut`: a Timeline for `episode_id` at store version `target_version`
    (via R6's `load_timeline_at`);
  - `style_bible`: `StyleBible` `target_id` at content version `target_version`;
  - `location`: `Location` `target_id` at content version `target_version`;
  - `h3_candidate`: the `H3Candidate` `target_id` exists, its `episode_id` equals
    `episode_id`, and `target_version` equals its `shot_version` (the version promotion
    already checks against).
- Writes with `store.save(review, expected_version=0)`; an existing `review_id` raises
  `VersionConflict` (409).
- Recording a Review never changes approval state. Approval still happens only in the
  `approve_*` functions.

### R4 — Reviews are immutable (G5)

`ProjectStore.save` refuses any `Review` save with `expected_version != 0`, with the same
shape of message as the existing H3Candidate/Export guards ("Review records are
immutable; record a new Review").

Do the same for `PostProfile` **only if** no production code path saves a PostProfile
with `expected_version != 0`. Check with grep first; if such a path exists, do not add
the PostProfile guard and report the path instead.

### R5 — Framing approval needs a Review (G6)

`approve_framing(store, shot_id, *, episode_id, review_id, expected_version,
agent_may_approve=False)`. Before writing, and again immediately before commit (the
same two-phase recheck `approve_shot` uses), it checks the Review with
`target_kind="framing"`, `target_id=shot_id`, `target_version=shot.version`,
`episode_id=episode_id`. The idempotent early return for already-approved framing is
kept and does not require a Review. All other behaviour is unchanged.

### R6 — Timeline history (G3)

Every successful `ProjectStore.save` of a `Timeline` also writes an immutable snapshot of
the same entity JSON at `episodes/{episode_id}/edits/timelines/{token:06d}.json`, where
`token` is the mutation token that save produces.

- Write order: snapshot first, then the head file (`edits/timeline.json`, unchanged).
- A snapshot whose token is **greater** than the current head token is an orphan from a
  save that failed before its head write, and may be replaced. A snapshot whose token is
  **less than or equal to** the head token is immutable: a write that would change its
  bytes raises `JobError(INVALID_MANIFEST)`.
- All writes go through the existing `ProjectRoot` atomic-write path (invariant 4).

Add `ProjectStore.load_timeline_at(episode_id: str, store_version: int) -> Timeline`:

- returns the snapshot when it exists;
- otherwise, if `store_version` equals the head token, returns the head (a legacy head
  saved before this change has no snapshot);
- otherwise raises `RecordNotFound`.

`adapters/export/snapshot.py` captures every `edits/timelines/*.json` file of each
episode into the package alongside `edits/timeline.json`.

When `assemble_episode` saves a **changed** Timeline, it also clears the Episode's
`approved_cut_version` and `approval_review_id` if either is set: the approved cut is no
longer the current cut. The Timeline save and the Episode save happen under one
`store.command()`. If `assemble_episode` is not already inside one, wrap the
persistence part of it, not the encode.

**Amendment after review (2026-11-21).** The Codex review found that the rules above
let a Review bind to an orphan and so approve a cut nobody reviewed. R6 now also requires:

- **R6a — Orphans are never returned.** `load_timeline_at` raises `RecordNotFound` for a
  `store_version` above the head token, even when a snapshot file exists, and returns
  the head itself for the head token. Consequently `record_review` cannot target an
  orphan, and a later save that replaces the orphan cannot change what a Review approved.
- **R6b — Legacy heads are preserved.** When a save replaces a head that has no snapshot
  of its own token (a head written before this change), the store first copies the
  head's exact bytes to `edits/timelines/{head_token:06d}.json`, then writes the new
  snapshot, then the head. Earlier cut receipts therefore keep their source edit.
- **R6c — Export packages the committed history.** The export snapshot captures every
  committed history file (token at or below the head) and the audio its
  `audio_tracks` reference, skips orphans above the head, and refuses a history file
  whose name is not `NNNNNN.json` or that exists without a Timeline head.
- **R8a — Derivation preflight.** `derive_script_scene_plan` refuses, before its first
  write, when an episode folder holds Shots but has no `episode.json` (consistent with
  R8's directory scan).

### R7 — Shot-list changes revoke the cut (G2, D2)

Every production write that changes `Episode.shot_ids` clears `approved_cut_version` and
`approval_review_id` in the same Episode save. `propose_shots` is the known site. Grep
for others, apply the same rule, and list every site in the report.

### R8 — Server-found invalidation scope (G4, D3)

- Add `ProjectStore.episode_ids() -> list[str]`: the sorted ids of every
  `episodes/*/episode.json` that exists, enumerated through `ProjectRoot`.
- `revise_location(store, revised, *, expected_version)` and
  `revise_camera_profile(store, revised, *, expected_version)` drop their `episode_ids`
  parameter and scan every directory under `episodes/` (`store.episode_directories()`),
  including one whose `episode.json` is missing. *(Amended after implementation: scanning
  only `episode_ids()` would silently skip a Shot stored in such a folder; scanning the
  directory lets the plan's existing Episode load refuse the revision before any plan is
  recorded, as before.)* `service/script_authoring.py` drops the `episode_ids` argument
  from its `revise_location` call.
- `api/routes.py` `LocationRevision`: remove `episode_ids` and set
  `model_config = ConfigDict(extra="forbid")`, so a request that still sends it gets a
  422 rather than a silently different meaning.
- `api/app.py` `revise_location` dispatch: stop passing `episode_ids`.
- `api/routes.py` `revise_location` summary: replace the `episode_ids` wording with
  "every episode in the project is checked". Edit no other summary; the file has other
  uncommitted summary edits that must be kept.

### R9 — Setting

Add to `orchestrator/platform/settings.py`:

```python
class ApprovalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: D1. When false (default), an agent-authored Review is an opinion and approves nothing.
    agent_may_approve: bool = False
```

and `approvals: ApprovalSettings = ApprovalSettings()` on `Settings`. Match the style of
the neighbouring settings classes.

## Non-goals (later steps)

No new HTTP/CLI/MCP operations (step 2 exposes review recording and approval; it must
derive `actor` from the transport, never from the request body). No review UI. No
change to overlays, camera-field mapping or IndexTTS hashing (step 3).

## File ownership

**Implementer** (Codex `gpt-6-luna`, effort max) may edit only:
`domain/entities.py`, `domain/state.py`, `service/approval.py`,
`service/dependencies.py`, `service/timeline.py`, `service/minimax_h3.py`,
`store/json_store.py`, `adapters/export/package.py`, `adapters/export/snapshot.py`,
`platform/settings.py`, `api/routes.py` (R8 only), `api/app.py` (R8 and the H3
promotion keyword in R2 only).

The implementer may also update **existing** tests, but only mechanically: where they
construct a `Review`, call `check_review_approval`, call a changed signature
(`approve_framing`, `revise_location`, `revise_camera_profile`), or send `episode_ids`
to `revise_location`. It must not delete a test or weaken an assertion, and must list
every test file it touched.

**Test author** (separate agent, from this contract only, without reading the
implementer's diff) writes only these new files:
`tests/service/test_approval_integrity_contract.py` and
`tests/store/test_timeline_history_contract.py`.

**Do not touch:** `spikes/**` (the creator has uncommitted notes there),
`GAP_REVIEW.md`, and any other file.

## Done when

- Every requirement above has a test in the test author's files, and the full suite
  passes (`.venv` pytest, with `--basetemp` on D:).
- `uvx ruff@0.14.0 check orchestrator tests` reports no findings beyond `main`'s.
- The implementer's report lists files changed, test files touched mechanically, the
  R4 PostProfile decision, the R7 sites, and anything this contract left ambiguous.
