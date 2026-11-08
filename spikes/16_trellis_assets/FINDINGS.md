# Spike 16 — TRELLIS2 as an asset source

Proves the chain **FLUX reference image → TRELLIS2 → textured `.glb` → Blender at the
FR-20 envelope**, and measures what it costs on this machine.

The install itself lives outside the repo at `D:\Work\comfyui-trellis` (a separate
ComfyUI on port 8189, Python 3.11 + torch 2.8.0+cu128). Its `INSTALL_NOTES.md` is the
authority on why it is separate, the five undocumented bugs worked around, and how to
run it. These scripts are kept here only so they survive the session that wrote them.

## Measured

| Stage | Time | Peak GPU | Attributable |
|---|---|---|---|
| FLUX schnell fp8, 1024², 4 steps | 41 s | 11,354 MiB | ~9,773 MiB |
| TRELLIS2 Q8_0, `1024_cascade`, 12/12/12 | 309 s | 4,384 MiB | **~2,970 MiB** |

TRELLIS2 is the cheap half — well under the ~8 GB its install guide suggests. The
binding constraint is FLUX, within ~870 MiB of filling the 12,227 MiB card. Run them
sequentially with `POST /free {"unload_models": true, "free_memory": true}` between.

Output: 500,258 faces, 2048² base colour + metallic-roughness, extents normalised to
~1.0 on the up axis, imports under `--factory-startup --disable-autoexec` and scales to
1.8 m (factor 1.797). Blender's glTF importer converts Y-up to Z-up correctly, and wires
the base colour into `Principled BSDF.Base Color` (verified, not assumed).

## Three conclusions worth keeping

**Use one image, not two.** The supplied `Trellis2Multiviews_GGUF.json` wants two
*consistent* views, which is hard to produce for a subject that does not exist yet — two
independent generations disagree and the mesh melts. TRELLIS reconstructs from a single
view. Reserve the multi-view workflow for cases where consistent views genuinely exist,
such as renders of an existing 3D object. A static CCTV camera never sees the far side,
so single-image reconstruction's usual weakness costs nothing here.

**Texture quality is set by the reference prompt, not by settings.** The first entity's
texture is flat mid-grey (mean 148/146/149) because the prompt asked for "pale grey
featureless matte skin" — ideal for *geometry*, but it leaves no albedo to recover. For
a textured result, keep the geometry-friendly parts (isolated subject, full body in
frame, plain background, even lighting) and add intrinsic surface variation: mottling,
discolouration, staining. Never add dramatic lighting to the reference; hard shadows
bake into the albedo as permanent smears that fight the shot's own lighting.

**Set `target_face_num` at generation; never decimate afterwards.** Measured at 640×480
delivery resolution, 25,000 faces is indistinguishable from 500,000. Decimating the
finished mesh to ~11k wrecks the UV layout and the surface goes blotchy — the silhouette
survives, the texture does not. Decimation also floors at ~10,947 faces because the mesh
is not watertight and boundary edges cannot collapse.

Suggested budgets by on-screen role:

| Role | faces | texture |
|---|---|---|
| Entity / hero prop | 25,000 | 1024 |
| Mid-ground (sofa, lamp) | 10,000 | 512 |
| Background clutter | 3,000 | 256–512 |

Texture, not geometry, dominates file size once faces are low: at 10,947 faces the
geometry is ~0.3 MB of an 8.4 MB export.

## Scripts

| File | Purpose |
|---|---|
| `gen_entity_image.py` | FLUX schnell reference image, with VRAM sampling |
| `run_trellis.py` | Single-image TRELLIS2 graph; frees FLUX first, waits for the server |
| `render_glb.py` | Import a `.glb` under `--factory-startup`, scale to 1.8 m, render two views |
| `lod_test.py` | Decimation ladder with per-level `.glb` size and a 640×480 render |
| `check_material.py` | Prove the base colour texture reaches the shader |
| `install_win_wheels.py` | Resolve the correct `win_amd64` CUDA wheels (the pack's installer picks Linux ones) |
| `apply_trellis_patches.py` | Apply the pack's library patches reversibly, with backups |
| `verify_comfy.py` | Wait for :8189 and assert the workflow's node classes are registered |

They hardcode absolute paths for this machine and expect the instance on port 8189.

`finding_degrade_fps.md` is unrelated to Trellis: it records that
`DegradeResult.output_fps` is copied from the request rather than the probe. Kept here
only so it is not lost with the session that found it.

## Copies of the out-of-repo install, kept here deliberately

The TRELLIS2 install lives at `D:\Work\comfyui-trellis`, outside this repository and
outside version control. Two of its files are copied here so a clone is self-sufficient:

- **`TRELLIS_INSTALL_NOTES.md`** — the authority on why the install is a separate Python
  environment, the five undocumented bugs worked around, the measured VRAM table, and how
  to run it. The live copy is `D:\Work\comfyui-trellis\INSTALL_NOTES.md`.
- **`run_trellis.bat.reference`** — the launcher, as `D:\Work\comfyui-trellis\run_trellis.bat`.

These are **copies, not the originals**. If you change the install, update both or delete
the stale copy — a confidently wrong note is worse than none. The weights at
`D:\Work\trellis2-gguf` (11 GB, hash-verified, with their own `PROVENANCE.json`) and the
generated `entity_00001_.glb` are not in the repository and never should be.
