# Cut approval binding contract (2026-11-21)

A fix to the finishing steps before the perception step. Read with
`orchestrator/service/APPROVAL_INTEGRITY_CONTRACT.md`,
`orchestrator/api/FINISHING_STEPS_CONTRACT.md` and
`orchestrator/api/CUT_ASSEMBLY_CONTRACT.md` (amendment A3 is superseded here).

## The defect

Gate C binds to a **Timeline version** (`Episode.approved_cut_version`). Credit cards
are rendered outside the Timeline, so two different cut files can share one version.
An automated security review showed the consequence: approve cut A; assemble a
different cut B of the same version (A3 revokes); the creator re-approves the version
after watching B; re-assemble A byte-identically (A3 keeps the approval); export can
now ship A, which the creator did not watch, under the approval of B.

## Creator decisions (2026-11-21)

- **D6.** A cut approval binds to **one exact cut file**. Export may ship only that cut,
  or a byte-identical one.
- **D7.** The gate-C shot rule is unchanged: approving a cut is refused only while a
  shot is `stale` or `draft` (`check_cut_approval`).

## R1 — The Episode records the approved cut

Add `approved_cut_output_id: str | None = None` to `Episode`
(`orchestrator/domain/entities.py`; same id pattern as `EpisodeOutput.output_id`).

- It is set together with `approved_cut_version` and `approval_review_id`, and cleared
  together with them **everywhere** they are cleared: `service/dependencies.py`
  (`_revoke_gates`, `propose_shots`), `service/timeline.py` (the changed or stale Timeline
  revocation), and any other site a grep finds.
- `store/json_store.py` `_check_creation_approval_fields` refuses it on creation, like
  the other approval fields.
- Existing episode files without the field still load (`None`).

## R2 — Approving a cut names the cut

`service/approval.py` `approve_episode(store, episode_id, *, cut_output_id, review_id,
expected_version, agent_may_approve=False)` replaces the `cut_version` parameter.

- It loads the EpisodeOutput `cut_output_id` from the episode's ledger (unknown id:
  `RecordNotFound`). The cut's `timeline_store_version` must equal the current Timeline
  head token and its `timeline_sha256` must equal the current Timeline's content hash;
  otherwise `JobError(INVALID_MANIFEST)` saying the cut is not of the current Timeline.
- The Review check is unchanged in shape: kind `episode_cut`, target the episode,
  `target_version` = that cut's `timeline_store_version`.
- On success it sets `approved_cut_version` (the cut's Timeline version),
  `approved_cut_output_id` and `approval_review_id`.
- All existing re-checks (pending cascades, `check_cut_approval`, the two-phase
  recheck) stay.

`api/routes.py` `CutApprovalRequest` replaces `cut_version` with `cut_output_id: str`
(pattern `^[A-Za-z0-9_-]+$`). When `review_id` is omitted, the auto-recorded Review
targets that cut's Timeline version. Update the `approve_cut` summary to say it
approves one exact cut (from `list_cuts`) and that export can ship only that cut.

## R3 — Export ships only the approved cut

`adapters/export/package.py` `_bind_cut` additionally requires an approved cut id:

- `episode.approved_cut_output_id` must be set; if it is `None` (including an episode
  approved before this change), refuse with `INVALID_MANIFEST` telling the creator to
  re-approve the cut with `approve_cut`.
- The selected cut must be the approved cut, or have the **same `content_sha256`** as the
  approved cut's receipt. Otherwise refuse, naming both ids.
- All existing checks (current Timeline, Review) stay.

## R4 — New cuts no longer touch the approval

Remove `CUT_ASSEMBLY_CONTRACT.md` amendment A3's revocation from `assemble_episode`
(the block after the receipt append). With R1–R3 a new cut cannot inherit an approval:
the approval names its cut, and export refuses any other. A changed or stale Timeline
still revokes (that logic stays, now also clearing `approved_cut_output_id`). Mark A3 as
superseded in `CUT_ASSEMBLY_CONTRACT.md`.

## File ownership

**Implementer** (Codex `gpt-6-luna`, effort max): `domain/entities.py` (Episode field
only), `store/json_store.py` (creation guard only), `service/approval.py`,
`service/dependencies.py` (clearing sites only), `service/timeline.py`,
`adapters/export/package.py`, `api/routes.py`, `api/app.py`,
`api/CUT_ASSEMBLY_CONTRACT.md` (mark A3 superseded). Existing tests may be updated only
where they call `approve_episode`/`approve_cut` with `cut_version`, construct approved
episodes, or assert A3's revocation (now superseded): list every file touched.

**Test author** (separate agent, from this contract only) writes only
`tests/service/test_cut_approval_binding_contract.py`, including the exact attack
scenario above end to end (it must fail before this change and pass after), export
refusing a non-approved cut of the approved version, export accepting a byte-identical
re-assembly of the approved cut, legacy episodes refused at export, and every clearing
site clearing `approved_cut_output_id`.

**Do not touch:** `spikes/**` and any file not listed.

## Done when

Every requirement has a test; the full suite passes (run alone, `--basetemp` on D:);
ruff reports nothing beyond `main`'s.
