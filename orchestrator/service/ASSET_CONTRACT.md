# Contract — FR-03's asset registry, and importing real geometry

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## Why this exists

Every scene in this project is assembled from `box`, `cylinder`, `sphere`, `cone` and
`torus`. The creator's verdict: it looks invented, and real sourced assets would be better.
That is **FR-03**, which is **P0** — "Search approved source APIs for usable props,
textures, and HDRIs... Include license/provenance and hashes in the asset registry" — and
whose §3.4 de-scope lever is "procedural-only assets". The project has been living in the
fallback without ever choosing it.

The `Asset` entity already exists in `orchestrator/domain/entities.py` with exactly the
right shape: `asset_id`, `type`, `generated`, `source_url`, `provider`, `license`,
`model_or_voice_license`, `file_path`, `checksum`, `attribution`,
`attribution_surface_required`, `reuse_scope`.

**Nothing uses it.** It is referenced by no production code and constructed by no test —
the seventh fully-specified-but-undriven thing found in this codebase, after `Timeline`,
`Episode.shot_ids` and the shot lifecycle. Driving it is most of this work.

## Decision 1 — assets are registered, contained, and verified on use

- An asset's bytes live **inside `ProjectRoot`** (FR-25), under a conventional path such as
  `assets/models/<asset_id>/<filename>`. Copy them in; do not reference a path outside the
  project. A project must stay self-contained and hashable.
- Registering an asset records a full `Asset`: where it came from, under what licence, and
  its `checksum`. **An asset with no `license` and no `source_url` is refused** — FR-03's
  acceptance is that "every externally sourced item has a documented origin, reuse right".
- **Verify before use.** Re-hash the file and compare with the registry's `checksum`
  whenever the asset is consumed. A mismatch fails loudly naming the asset — the same
  discipline `ShotOutput` artifacts already get.
- **Carry `attribution_surface_required` through.** Poly Haven's assets are CC0 and need no
  credit, but its live API separately requires visible credit wherever that content is
  surfaced (§8.2, §17 ref 7). The flag exists to record that obligation; losing it loses a
  licence condition.

## Decision 2 — glTF only, and this is a security boundary

**Importable formats are `.gltf` and `.glb`. A `.blend` file must be refused outright**,
with a message saying why.

This is not a preference. A `.blend` can carry executable Python that runs on load, and
this project already treats model weights as executable input. glTF is data. If a provider
offers only `.blend` for something, that asset is out of scope until someone converts it
deliberately.

Do not enable Blender's script auto-run under any circumstance.

## Decision 3 — the scene references assets without disturbing primitives

`ObjectSpec.kind` is a closed set of five primitives, and a pinned plan hash depends on the
current payload shape. So:

- **Add an optional field** to `ObjectSpec` referencing a registered asset by id. Do not add
  a sixth `kind`.
- **A scene using no assets must produce a byte-identical plan to today's.** The pinned
  static-plan hash test must pass untouched. Follow the existing convention where empty
  optional fields are omitted from the payload.
- When the field is set, the worker **imports the asset** rather than creating a primitive.
  `location`, `dimensions` and `rotation` still position and scale it; state how you
  reconcile the imported mesh's own bounds with the declared `dimensions`, because a real
  asset arrives at its own scale and a figure that should be 1.8 m must still be 1.8 m.
- **The plan must identify the asset by checksum, not only by id**, so a changed asset
  changes the plan. Determinism is unaffected otherwise: two builds of one request stay
  byte-identical.

## Decision 4 — registration is reachable, fetching is not in scope

Add a service function, and an operation in `OPERATIONS`, to **register a local file** as a
project asset with its provenance. The Phase 1 Exit clause requires the pipeline be reached
"through the API or CLI", and three services added recently — approval, framing approval and
episode assembly — are still service-only. Do not repeat that.

**Out of scope:** the Poly Haven HTTP client, searching, and downloading. Those need network
access and a provider adapter; this contract is the foundation they will land on. Say in
your report what a fetching adapter would need from what you built.

## Out of scope

Poly Haven search/download; HDRI and texture application (this is geometry); the FR-10
review surface; `Location`; the rubric scorer.

## Test rules

- **No GPU, no Blender, no ComfyUI, no network.** Assert on records, plans and the composed
  worker payload. Use a tiny valid `.glb` fixture rather than a real asset.
- The regression that matters: **a scene with an imported asset reaches the worker payload
  carrying the asset's checksum, and a scene without one is byte-identical to today's.**
- Required cases: registering without a licence or source is refused; a `.blend` is refused
  naming the reason; a checksum mismatch at use time fails loudly; `attribution_surface_required`
  survives a round trip through the store; the declared `dimensions` govern the imported
  mesh's final size; two builds of one request are byte-identical; the pinned static-plan
  hash is unchanged.
- Every assertion carries a message saying what was observed.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline: **1491 passed, 34 skipped**, ruff clean.

## One thing to measure, not assume

The production renderer runs `blender --background --factory-startup`. Confirm that glTF
import actually works under `--factory-startup`, since that flag resets preferences and the
importer is an addon. If it does not, say so — do not work around it by abandoning
`--factory-startup`, which FR-04 requires.
