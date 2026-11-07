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

---

# Amendment 1 — what the parallel pass found

Both agents worked from the text above. Five of its statements were wrong, and the code
they read decided against me in every case. Recorded here rather than quietly fixed.

## A1.1 — Decision 4 is superseded: the operation already exists

`create_asset` is **already** a wired `Operation` (`POST /projects/{project_id}/assets`,
`upload=True`, `routes.py:130`), sitting beside `search_assets`, with a special case in
`app.py` that performs transport validation and then deliberately answers **501** because
— in its own comment — "storing the asset is the absent adapter's job".

**Fill that adapter. Do not add a `register_asset` operation.** `create_asset` must be
removed from `DELIBERATE_501` in `tests/api/test_entry_path.py` once implemented, and the
two sibling expectations in `tests/api/test_adapters.py` and `tests/api/test_cli_process.py`
updated. Those three are the only legitimate test changes.

## A1.2 — the provenance rule was a contradiction, and both readings were wrong

Decision 1 said refusal required no licence **and** no source; the test rules said **or**.
Neither is right, because `Asset.generated` exists:

- **`license` is required for every asset**, generated or sourced.
- **`source_url` is required only when `generated` is false.**

A generated asset legitimately has no source URL. Refusing it would make FR-03 refuse the
pipeline's own output.

## A1.3 — registration stays as broad as FR-03; geometry-only binds to the import seam

FR-03 covers "props, **textures**, and HDRIs". Narrowing `create_asset` to glTF makes a
well-formed PNG upload a hard `upload_rejected` where it previously reached the adapter.
"Texture *application* is out of scope" licensed no such thing.

**`create_asset` registers any permitted upload kind with its provenance.** The glTF-only
rule and the `.blend` refusal bind to the **geometry path** — an `ObjectSpec.asset_id`
resolved for import — not to registration. A PNG must register and must then be refused
only if something tries to import it as geometry.

## A1.4 — `.glb` only; `.gltf` is withdrawn

Decision 2 named both. `.gltf` is bare JSON referencing external `.bin` and texture files:
it cannot be magic-byte verified without admitting arbitrary JSON, and it is not a single
self-contained file, which defeats containment and single-checksum verification.

**Uploads admit `.glb`.** This needs `MediaKind` widened past `Literal["image", "video"]`,
a `.glb` entry in `_EXTENSION_KIND`, the glTF magic `b"glTF"` in
`_extensions_matching_magic`, and a `max_model_bytes` cap in `UploadLimits`.

Note the existing boundary is **stronger** than Decision 2 asked for: a closed extension
allowlist plus extension/magic agreement, not a `.blend` denylist. Keep it that way.

## A1.5 — the sizing arithmetic must be pure, or its guarantee is untestable

The contract required asserting that declared `dimensions` govern the imported mesh. That
is unprovable while the arithmetic sits inside a `bpy`-dependent function, and the test
author correctly reported its test proves only that the declared value reaches the plan.

**Extract the fit computation into a pure module-level function** — source bounds and
declared dimensions in, per-axis factors out, with the degenerate-extent refusal — so it
is unit-testable with no Blender. Deterministic logic does not belong behind a side effect.

## Measured, not assumed

glTF import **does** work under `blender --background --factory-startup --disable-autoexec`
on Blender 5.2.0: the operator is available and import returned `FINISHED`. FR-04's flag
stands.

The pinned static-plan hash `92de3ed358ecc3bc4ef91127db9c6a12254e86a59f255645c03579b60b03788a`
is unchanged, verified by a full local run, not by report.

### A1.5 addendum — where the pure function lives, measured

`worker.py` opens "Standalone Blender-side worker. Keep imports limited to Blender and
stdlib", so it **cannot** import a helper from `orchestrator.*`. Nor can the factors be
precomputed into the plan: they depend on the imported mesh's bounds, which are unknown
until import. So the arithmetic must stay in `worker.py`.

That is still testable. `import bpy` is **function-local**, not module-level, and I
confirmed by loading the file with `importlib` under the project interpreter that
`worker.py` imports cleanly outside Blender. The fit computation therefore becomes a
**module-level pure function in `worker.py`**:

```
asset_fit_factors(source_min, source_max, desired) -> tuple[float, float, float]
```

raising `ValueError` naming the degenerate axis when an extent is non-finite or <= 1e-9.
Tests load `worker.py` by path and call it directly. Both agents bind to this name.
