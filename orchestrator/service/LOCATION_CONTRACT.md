# Contract — `Location` becomes a dressed room, and FR-15 closes

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

Read `orchestrator/service/ASSET_CONTRACT.md` (all four amendments) and
`orchestrator/adapters/FIGURE_ASSET_CONTRACT.md` (one amendment) first. This builds on both,
which landed at `b8ace44`.

## Why this exists

**FR-15 — "intra-episode location/camera reuse" — is P0 and completely unmet.**

`Location` exists in `orchestrator/domain/entities.py` with `location_id`, `blend_path`,
`geometry_provenance`, `asset_ids`, `camera_rigs` and `approval_review_id`. It is referenced
by `invalidation.py` and `state.py`, and **nothing ever creates one**. It is the sixth
fully-specified-but-undriven thing found here, after `Timeline`, `Episode.shot_ids`, the shot
lifecycle, `Asset` and the anomaly figure's asset.

Meanwhile a room is `american_living_room()` — a hardcoded function returning a `SceneSpec`,
5.4 x 4.2 m, with 23 `ObjectSpec`s and a `look` of `bright` or `low_key`. Two shots in one
episode cannot share a room because there is no record of a room, only a function call. And
real sourced props cannot be placed, because there is nowhere to record a placement.

## Decision 1 — `Location` gains placements, and there is exactly one source of truth

`asset_ids` is a bare list and cannot say **where** anything goes. Add a placement list.

- New domain model **`AssetPlacement`**: `asset_id`, `position`, `dimensions`, `rotation`.
  `dimensions` is the **declared real-world envelope in metres** and is authoritative, exactly
  as FR-03 and `asset_fit_factors` already require — a sofa declared 2.1 m wide is 2.1 m wide
  whatever scale the asset was authored at. This is the field that stops TRELLIS2's
  normalise-everything-to-1.0 behaviour from producing a doll's house.
- **`Location.placements: list[AssetPlacement]`**, defaulting empty.
- **No duplicate source of truth.** `asset_ids` and `placements` must not independently
  record the same thing. State your rule and enforce it with a validator: either `asset_ids`
  is derived from `placements`, or it is restricted to assets that are *not* placed geometry
  (an HDRI, a texture). Pick one, justify it, make the other impossible.

**`placements` is rebuild-triggering under section 4.4.** Moving or resizing a prop changes
geometry and cannot be a post-only edit. FR-11's dependency graph keys invalidation off these
tags, so tag it correctly and confirm `invalidation.py` does the right thing — a placement
change must invalidate renders, and must not invalidate an unrelated approved shot.

## Decision 2 — a Location is a base shell plus placements

Do **not** attempt to replace the procedural room wholesale. A Location names:

- the **base shell** — which procedural room and which `look`, the existing
  `american_living_room(look=...)` being the only one today; and
- its **placements** — sourced or generated assets dressed into that shell.

Composing a `SceneSpec` means building the base shell as today and appending one `ObjectSpec`
per placement, each carrying its `asset_id`, declared `dimensions`, `position` and `rotation`.
Those go through the **existing** asset binding and checksum path from the FR-03 and figure
contracts. No parallel lookup.

A Location with no placements must compose a `SceneSpec` **byte-identical to calling
`american_living_room(look=...)` directly today.** That is the regression that matters.

## Decision 3 — two shots sharing a location get the same room

This is FR-15's acceptance and the reason the entity exists. Two shots in one episode
referencing the same `location_id` must compose **byte-identical room geometry**, differing
only in camera and shot-specific content. Prove it with a test that composes both and
compares, not by inspection.

## Decision 4 — reachable through the API or CLI

Add a service function and an operation in `OPERATIONS` to create and revise a Location with
its placements. The Phase 1 Exit clause requires the pipeline be reached "through the API or
CLI", and approval, framing approval and episode assembly are **still service-only** because
earlier rounds skipped this. Do not repeat that. Check whether a suitable operation already
exists before adding one — `create_asset` already existed and the FR-03 contract wrongly told
two agents to invent `register_asset`.

## Decision 5 — `blend_path` is not to be used

`Location.blend_path` predates the FR-03 security boundary, which **refuses `.blend` outright
because it can execute Python on load**. Do not build on it, do not populate it, do not add a
loader for it. Leave the field alone and say in your report that it is now dead weight; its
removal is a separate decision about stored records.

## Out of scope

Poly Haven and Sketchfab fetching; new procedural rooms; camera rigs (`camera_rigs` stays
untouched); location approval workflow; cross-episode reuse, which the PRD marks P1.

## Test rules

- **No GPU, no Blender, no ComfyUI, no network.** Assert on records, composed `SceneSpec`s
  and worker payloads. Reuse the existing `.glb` fixture and registration helpers in
  `tests/service/test_asset_contract.py`; do not invent another.
- Required cases:
  - a Location with no placements composes byte-identically to `american_living_room(look=...)`;
  - a placement reaches the worker payload carrying the asset's **checksum**;
  - declared `dimensions` govern the placed asset's size;
  - **two shots sharing a `location_id` compose byte-identical room geometry** (FR-15);
  - an unregistered placement asset is refused, naming it;
  - a placement change is **rebuild-triggering** and invalidates renders, while leaving an
    unrelated approved shot intact;
  - no duplicate source of truth — the validator from Decision 1 refuses the contradiction;
  - the pinned static-plan hash
    `92de3ed358ecc3bc4ef91127db9c6a12254e86a59f255645c03579b60b03788a` is unchanged;
  - two builds of one request are byte-identical.
- Every assertion carries a message saying what was observed.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline: **1535 passed, 34 skipped**, ruff clean.

## Report what you could not do

Say explicitly what this contract left ambiguous and anything you believe it got wrong. The
FR-03 contract needed four amendments and twelve defects were found; the figure contract
needed one. Assume this one is wrong somewhere, and say where. The admissions are the most
valuable part of the report.

---

# Amendment 1 — what the parallel pass settled

Final state: **1561 passed, 36 skipped, 0 failed**, ruff clean. FR-15 is met.

## A1.1 — the base shell is two fields, not one

The contract said "which procedural room and which look" without naming fields. The test
author assumed a single `Location.look`; the implementer added **`base_shell`** (which
procedural room; `american_living_room` is the only one today) and **`scene_look`**
(`bright` or `low_key`). **The implementer is right** — a room and its lighting are
independent choices and conflating them would have made a second room impossible to add
without a migration. All nine test failures traced to this one gap.

Worth noting how cleanly it surfaced: the test author wrote a guard assertion naming its own
assumption and printing the observed field list, so the diagnosis was one line rather than
nine opaque `AttributeError`s. That idiom is worth repeating.

## A1.2 — `asset_ids` is for non-geometry resources

Both agents independently chose the same rule: `placements` owns placed geometry, and
`asset_ids` is reserved for non-geometry resources such as textures and HDRIs. A validator
rejects an id appearing in both. No duplicate source of truth.

## A1.3 — placement object names are deterministic by list order

Each placement becomes an `ObjectSpec` named deterministically from its **position in the
list**, so repeated placements of one asset get distinct, stable names and builds stay
byte-identical.

**Known consequence, accepted:** reordering placements renames objects, which changes the
plan and therefore triggers a rebuild, even though the room looks identical. That is
defensible — the stored record did change — but it means a cosmetic reorder is not free. If
that becomes annoying, the fix is a stable per-placement id rather than list position, and
that is a contract change, not a patch.

## A1.4 — section 4.4 tagging sits on top of an existing mechanism

`revision_impact` already treated **any** `Location` change as impactful, so the contract's
instruction to tag `placements` risked layering a second mechanism on the first. As
implemented, Location invalidation honours tags and treats untagged changes conservatively.
Measured: moving a placement impacts only the dependent approved shot, which goes `stale`,
while an unrelated approved shot stays `approved`.

## A1.5 — `blend_path` is dead weight, confirmed

Untouched, as instructed. It predates FR-03's refusal of `.blend` as executable-on-load
content, so nothing should ever populate it. Removing it is a separate decision about stored
records and has not been taken.

## Measured, not asserted

- Two shots bound to one `location_id` composed **identical 50,358-byte scene JSON**,
  SHA-256 `603823b591a99431e83556d1dd07e52ff0584864e952203e4a12cfcdb0d15a07`. That is FR-15's
  acceptance.
- An empty-placement Location composed byte-identically to `american_living_room(look=...)`.
- The pinned static-plan hash
  `92de3ed358ecc3bc4ef91127db9c6a12254e86a59f255645c03579b60b03788a` is unchanged.
- `create_location` and `revise_location` are wired into `OPERATIONS`; revisions require
  `expected_version` and explicit `episode_ids`.

## Still owed

No Blender render has been run against a placed asset, and the generated entity at
`D:\Work\comfyui-trellis\ComfyUI\output\3D\entity_00001_.glb` has still not been registered
through FR-03 or placed in a shot. The whole chain is now built and nothing has travelled
down it end to end.
