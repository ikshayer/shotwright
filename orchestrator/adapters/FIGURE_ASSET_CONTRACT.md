# Contract — the FR-20 figure becomes a registered asset

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

Read `orchestrator/service/ASSET_CONTRACT.md` and **all four of its amendments** first. This
contract builds directly on FR-03, which landed at `ec82cff`.

## Why this exists

`_append_figure` in `orchestrator/adapters/blender/scene.py` builds the anomaly figure from
**eight primitives** — body and head spheres plus boxes for arms, legs and feet — under an
explicit `UNMEASURED PLACEHOLDER` comment. The creator's verdict is that it does not look
like an entity, and they are right.

FR-03 now provides registered assets with provenance, checksums and verify-on-use, and
`ObjectSpec.asset_id` already carries one into the worker payload. A real mesh has been
generated and proven to import at the correct envelope. **Nothing connects the two.** This
contract connects them.

## Decision 1 — the asset is named on the anomaly, not on the scene

All four figure anomalies (`StaticSilhouette`, `LinearWalkPath`, `AppearDisappear`,
`PartialOcclusion`) derive from `_FigureAnomaly` in `orchestrator/domain/anomaly.py`.

- **Add `figure_asset_id: str | None = None` to `_FigureAnomaly`**, so every figure anomaly
  inherits it and the three non-figure anomalies do not.
- It is **optional**. When absent, behaviour is exactly today's.
- The field must be **omitted from serialisation when `None`**, following the existing
  convention, so no stored `Shot` changes shape and no pinned hash moves.

Do not add it to `Shot`, to `SceneSpec`, or to a camera profile. It describes the figure the
anomaly stages, so it belongs on the anomaly.

## Decision 2 — one object replaces eight, and the declared envelope still governs

When `figure_asset_id` is set, `_append_figure` emits **exactly one** `ObjectSpec`:

- `name` — `"FR20AnomalyFigure"`, the existing body component's name, so anything matching on
  it keeps working.
- `asset_id` — the declared id.
- `dimensions` — the **declared** `(width_m, depth_m, height_m)` the function already
  receives. This is authoritative: `asset_fit_factors` in `worker.py` scales the imported
  mesh to it. A figure declared 1.8 m renders 1.8 m whatever scale the asset was authored at.
- `location`, `keyframes`, `visibility_keyframes` — exactly as the primitive path uses them.

**The eight primitives are not emitted at all** in this case. No `FR20AnomalyFigureHead`, no
arms, legs or feet.

**Do not apply the `FR20AnomalySilhouette` material to an imported asset.** A real asset
carries its own material; overriding it would discard the texture the asset was registered
with. The material stays on the primitive path only.

## Decision 3 — the geometric constraints are unchanged

`_validate_figure_geometry` enforces FR-20's minimum subject distance and maximum on-frame
height **against the declared envelope**. That envelope is the same in both paths, so the
validation call and its behaviour **do not change**. An asset-backed figure is subject to
exactly the same refusals as a primitive one. Do not weaken or branch it.

## Decision 4 — the asset must be resolved and verified like any other

`build_scene_plan` already resolves `ObjectSpec.asset_id` against `request.asset_bindings`,
refusing an unregistered asset and carrying the asset's **checksum** into the worker payload.
The figure's asset takes that same path — no parallel lookup, no special case.

This means the composing layer must bind the figure's asset. **Find where `asset_bindings` is
populated and make sure an asset named only by an anomaly is included.** This is the seam
most likely to be broken, and the one this repository has got wrong seven times: a field that
is set on one side and never read on the other.

## Decision 5 — a scene with no figure asset is byte-identical to today

The pinned static-plan hash
`92de3ed358ecc3bc4ef91127db9c6a12254e86a59f255645c03579b60b03788a` must pass untouched, and
every existing anomaly test must pass unchanged. If the hash moves, the payload shape changed
for every scene that never used an asset.

## Out of scope

The `Location` entity and room dressing; Poly Haven or Sketchfab fetching; rigging or
articulation (`linear_walk_path` is pure translation today and stays that way); the rubric
scorer; changing the primitive proportions.

## Test rules

- **No GPU, no Blender, no ComfyUI, no network.** Assert on plans and the composed worker
  payload. Use a tiny valid `.glb` fixture built in the test, as `tests/service/test_asset_contract.py`
  already does — reuse that helper rather than inventing another.
- Required cases:
  - a figure anomaly with no `figure_asset_id` produces today's eight primitives, and the
    pinned static-plan hash is unchanged;
  - a figure anomaly with `figure_asset_id` produces **exactly one** object named
    `FR20AnomalyFigure`, carrying the asset's **checksum**, and **no** head/arm/leg/foot
    objects exist in the plan;
  - the declared dimensions reach that object unchanged, and `asset_fit_factors` maps the
    fixture's bounds onto them;
  - an unregistered `figure_asset_id` is refused, naming the asset;
  - `_validate_figure_geometry`'s refusals fire identically on both paths — assert one
    refusal case with an asset set;
  - two builds of one request are byte-identical;
  - `figure_asset_id` survives a round trip through the store and is **absent** from the
    serialised payload when `None`.
- Every assertion carries a message saying what was observed.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline: **1529 passed, 34 skipped**, ruff clean.

## Report what you could not do

Say explicitly what this contract left ambiguous and anything you believe it got wrong. Four
amendments were needed on the FR-03 contract and twelve defects were found; assume this one
is wrong somewhere too, and say where.

---

# Amendment 1 — four gaps, resolved in flight

Both agents found the same class of gap and the implementer resolved each sensibly. The
merged tree was green on the first attempt — **1535 passed, 34 skipped, 0 failed** — which
had not happened before in sixteen rounds here. That is not evidence the contract was right;
it is evidence the gaps were resolved consistently by judgement. Recording them so they are
decisions rather than accidents.

## A1.1 — the figure's anchor is the declared envelope's centre

"`location` exactly as the primitive path uses them" was ambiguous: the primitive body
sphere sits at an internal offset of `(0, 0, 0.15)` within the figure, so the two readings
place the mesh differently. **The anchor is the centre of the declared envelope**, which is
also what `_validate_figure_geometry` measures against, so placement and the FR-20 geometric
checks agree by construction. Any future keyframe convention must use the same anchor.

## A1.2 — binding resolution runs after anomaly expansion

Composition scans scene objects to build `asset_bindings`, but the anomaly figure is appended
during anomaly expansion — so a naive ordering binds nothing. **Binding resolution runs
against the final plan objects**, after expansion. `_asset_bindings_for_scene()` in
`service/compose.py` includes the anomaly's asset id. This was the seam flagged as most
likely to break, and it is the one that needed the ordering change.

## A1.3 — `ObjectSpec.material` on an asset-backed figure

Decision 2 said not to apply `FR20AnomalySilhouette` but did not say what to use instead,
and `ObjectSpec` requires `kind` and `material`. The primitive material is omitted so the
asset's own registered material and texture survive. A future change here must not
reintroduce an override: discarding the asset's texture would silently undo what FR-03
registered.

## A1.4 — omitting `None` is explicit, not automatic

The store serialises with `model_dump(mode="json")` and no `exclude_none`, so "omitted when
`None`" needs a deliberate mechanism rather than being a property of pydantic. It is handled
explicitly; do not assume a future optional field on these models is omitted for free.

## Unverified

Composition and plan output were exercised, but **no Blender render was run** against an
asset-backed figure. The generated entity at
`D:\Work\comfyui-trellis\ComfyUI\output\3D\entity_00001_.glb` has not been registered through
FR-03 or placed in a shot. That end-to-end run is the obvious next proof and is still owed.
