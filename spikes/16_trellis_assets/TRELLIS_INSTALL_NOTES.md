# Trellis2 ComfyUI — authoring instance

An image-to-3D authoring tool, **not** part of the production pipeline. It exists to
generate `.glb` meshes that are then registered as project assets (FR-03) and consumed
deterministically by the renderer. Nothing in the pipeline calls this instance.

## Why a separate environment, not a second port on the existing ComfyUI

A second *process* on a second port would have been fine. The separation is about the
**Python environment**, for three measured reasons:

1. `AGENTS.md` records that `D:\ComfyUI_3\ComfyUI_windows_portable` is immutable and that
   nothing may `pip install` into `python_embeded` — a third-party node
   (`comfyui_smznodes`) once patched core sampling and broke generation with an opaque
   CUDA error (spike 03).
2. `ComfyUI-Trellis2-GGUF/requirements.txt` pins **`transformers==5.2.0`**. Installing
   that into the shared environment would move `transformers` under the pipeline's feet.
3. `ComfyUI-Trellis2-GGUF/install.py` calls `apply_patches()`, which **overwrites files
   inside the installed `cumesh` and `o_voxel` packages** from its own `patch/`
   directory. That is the same class of action as the failure in (1).

Separately: the pipeline launches ComfyUI with
`--disable-all-custom-nodes --whitelist-custom-nodes …`, so nodes installed into that
tree would be ignored by pipeline runs anyway. All of the risk, none of the benefit.

## Layout

```
D:\Work\comfyui-trellis\
  ComfyUI\                                  comfyanonymous/ComfyUI (git clone)
    custom_nodes\ComfyUI-Trellis2\          visualbruno - base nodes
    custom_nodes\ComfyUI-Trellis2-GGUF\     Aero-Ex - GGUF quantized loader
    models\Trellis2\                        real directory
      decoders -> D:\Work\trellis2-gguf\decoders    (junction)
      encoders -> D:\Work\trellis2-gguf\encoders    (junction)
      refiner  -> D:\Work\trellis2-gguf\refiner     (junction)
      shape    -> D:\Work\trellis2-gguf\shape       (junction)
      texture  -> D:\Work\trellis2-gguf\texture     (junction)
  .venv\                                    Python 3.11.9
  run_trellis.bat                           launches on port 8189
```

The weights are **junctioned, not copied**, so `D:\Work\trellis2-gguf` keeps the clean
provenance its `PROVENANCE.json` describes: exactly the ten files whose SHA256 matched
the pinned revision `edf81aef93af0823ac8721da3c9cd3c52d539f87`. Anything the pack
downloads for itself lands in the ComfyUI tree instead, leaving that record intact.

The subfolder names match `model_manager.py`'s `REPO_PATH_MAP` exactly
(`decoders/Stage1`, `decoders/Stage2`, `refiner`, `shape`, `texture`, `encoders`), which
is its supported "nested Aero-Ex layout". The 11 GB is therefore reused rather than
re-downloaded.

## Environment, and why these versions

| Component | Version | Reason |
|---|---|---|
| Python | 3.11.9 | `open3d` wheel coverage is reliable here; all three native deps ship `cp311` |
| torch | 2.8.0+cu128 | **sm_120 is required** for the RTX 5070 Ti Laptop |
| CUDA | 12.8 | cu124 — which the pack's own README recommends — cannot target sm_120 |

Measured after install: `torch.cuda.get_arch_list()` contains `sm_120`, the device
reports capability `sm_120`, and an fp16 matmul executes on the GPU.

## Third-party binary sources — know what this install pulled

`install.py` fetches `flash-attn`, `cumesh` and `o-voxel` wheels from
`https://pozzettiandrea.github.io/cuda-wheels/` — a personal GitHub Pages index, **not
PyPI** — selected by `cu{ver}torch{ver}-cp{ver}-win_amd64` tags. It then patches the
installed `cumesh` and `o_voxel` packages in place. This is the pack's documented
procedure. It is confined to this venv and cannot reach `python_embeded`.

The GGUF weights themselves carry a provenance caveat recorded in
`D:\Work\trellis2-gguf\PROVENANCE.json`: the source repo publishes no model card,
licence tag or base-model statement. Upstream is understood to be
`microsoft/TRELLIS.2-4B` (MIT), but the quantisation is undocumented. If a generated
mesh is ever distributed, record the generator's licence in the asset's
`model_or_voice_license` field, which exists for exactly this.

## Running it

```
D:\Work\comfyui-trellis\run_trellis.bat
```

Then open http://127.0.0.1:8189 and load
`D:\Work\video_pipeline\3d-model-guides\Trellis2Multiviews_GGUF.json`.

On first run the pack downloads DINOv3 (~1.2 GB) from `Aero-Ex/Dinov3`, because it wants
an HF snapshot directory and the 11 GB set only contains a single
`Vision/dinov3-vitl16-pretrain-lvd1689m.safetensors`.

**Do not run this at the same time as a pipeline render.** Both want the GPU, and the
machine has 12,227 MiB with a foreign baseline that already drifts 1.3–2.7 GB.

## Four bugs this install had to work around

None of these are configuration mistakes. A future reinstall will hit all of them again.

### 1. `install.py` resolves Linux wheels on Windows — the install cannot work as documented

`get_env_info()` correctly computes `platform = "win_amd64"` and then **never uses it**.
The matching regex ends:

```
...-{python_tag}-{python_tag}-[^"\s]+\.whl
```

The platform tag is wildcarded, so on Windows it returns whichever wheel appears first in
the index — `manylinux_2_34_x86_64` — for all six native packages. pip rejects those
outright. Caught by running `install.py --dry-run` first; nothing was installed.

Fixed by resolving the correct `win_amd64` URLs from the same index and installing those
directly. All six exist for exactly `cu128 / torch2.8 / cp311`, which is why those
versions were chosen. `install.py` itself must **not** be run.

### 2. Three wheels ship malformed `.dist-info` directory names, which breaks pip

`flex-gemm`, `nvdiffrec-render` and `o-voxel` install `.dist-info` directories using
hyphenated project names. A `.dist-info` directory must use the *normalized* name, with
underscores. pip splits the directory name at the first hyphen, so it read:

| Directory | pip's reading |
|---|---|
| `flex-gemm-1.0.0+cu128torch2.8.dist-info` | name `flex`, version `gemm-1.0.0+cu128torch2.8` |
| `nvdiffrec-render-0.0.1+...` | name `nvdiffrec`, version `render-0.0.1+...` |
| `o-voxel-0.0.1+...` | name `o`, version `voxel-0.0.1+...` |

and raised `InvalidVersion`. Under pip 26.x this is **fatal to every later `pip install`
in the environment**, because the new post-install summary scans installed distributions.
The install itself succeeds and then pip dies while reporting it, which makes it look
like a failure when it is not.

Fixed by renaming all three directories to underscores and rewriting their `RECORD`
paths so uninstall still works. `pip check` is clean afterwards.

### 3. `rembg` is listed without an ONNX backend

Bare `rembg` pulls no `onnxruntime`, so background removal fails at runtime — and that is
what produces the alpha the `LoadImageWithTransparency` nodes consume. Installed
`rembg[cpu]`; removing the background from two images does not need the GPU, and the CPU
backend avoids cuDNN version matching entirely.

### 4. `triton-windows` resolved to the wrong major version

A transitive dependency pulled `triton-windows 3.9.0.post30`, while `install.py`
deliberately selects `<3.5` (3.4.x) for torch ≥ 2.8. Pinned back to `3.4.0.post21` to
match that intent. Only matters for SDNQ paths, which this GGUF workflow does not use.

### 5. `ComfyUI-GGUF` is a required dependency and is documented nowhere

The first generation failed at the generator node with:

```
"blocks.0.self_attn.to_qkv.weight" ... tensor a (1536) must match tensor b (1632)
```

1536 x 34/32 = 1632 — exactly Q8_0 block overhead, 32 values plus a 2-byte scale. The
GGUF tensors were being copied into the model **raw, never dequantized**.

Cause: `trellis2_gguf/utils/gguf_utils.py::_setup_native_gguf()` looks for
`custom_nodes/ComfyUI-GGUF` (city96's node pack) and loads its `dequant.py`, `ops.py`
and `loader.py` for `GGMLTensor`, `GGMLLayer` and `GGMLOps`. If that directory is absent
it does `return False` and falls back to a path that lacks the layer wiring, so
`load_state_dict` copies quantized blocks straight into fp16 parameters.

It is a **node pack, not a pip package**, so it cannot appear in `requirements.txt`, and
`install.py` never mentions it. Nothing fails at startup; it only fails mid-generation
with a shape error that looks like a corrupt download.

Fixed by cloning `https://github.com/city96/ComfyUI-GGUF` into `custom_nodes/`. Confirm
it took effect by looking for this line at startup:

```
[Trellis2] Using native ComfyUI-GGUF support (ops/dequant/loader)
```

Note the fallback *does* implement `dequantize_blocks_Q8_0`; the missing piece is the
ops/layer wiring, not the dequant maths.

### Patches applied reversibly

`apply_patches()` copies over installed library files with no backup. The same two patches
were applied here with a `.orig-trellis2` copy kept beside each target:

- `patch/remeshing.py` → `site-packages/cumesh/remeshing.py` (57,408 bytes)
- `patch/flexible_dual_grid.py` → `site-packages/o_voxel/convert/flexible_dual_grid.py` (18,679 bytes)

## Verified working

Measured, not assumed:

- `torch.cuda.get_arch_list()` contains `sm_120`; device capability reports `sm_120`; an
  fp16 matmul executes on the GPU.
- ComfyUI 0.44.0 starts on :8189 in ~40s under Python 3.11.9 / torch 2.8.0+cu128.
- GPU visible to it: 12,227 MiB total, 11,042 MiB free at launch.
- **1,170 node classes registered, 134 of them Trellis2**, and all five classes the
  supplied workflow references are present: `Trellis2LoadModel_GGUF`,
  `Trellis2LoadImageWithTransparency`, `Trellis2MeshWithVoxelMultiViewGenerator`,
  `Trellis2PostProcessAndUnWrapAndRasterizer`, `Trellis2ExportMesh`.
- No custom-node import failures in the launch log.

## First generation — measured

End-to-end: FLUX reference image -> TRELLIS2 -> textured `.glb` -> Blender.

| Stage | Wall time | Peak GPU | Attributable |
|---|---|---|---|
| FLUX schnell, fp8, 1024², 4 steps | 41 s | 11,354 MiB | ~9,773 MiB |
| TRELLIS2 Q8_0, `1024_cascade`, 12/12/12 steps | 309 s | 4,384 MiB | **~2,970 MiB** |

**TRELLIS2 is the cheap half.** It used under 3 GB — comfortably below the ~8 GB the
install guide suggests, and a third of what FLUX needed. The binding constraint on this
machine is the image generator, not the 3D model. They must still be run **sequentially**,
with `POST /free {"unload_models": true, "free_memory": true}` between: FLUX alone peaks
within ~870 MiB of the card's 12,227 MiB.

Output mesh: 20.5 MB `.glb`, **359,235 vertices / 500,258 faces** (the 500k target),
2048² base-colour texture, extents `0.196 x 1.002 x 0.129` normalised to ~1.0 on the up
axis. Not watertight, which is normal for generated geometry and fine for rendering.

Verified in Blender under `--background --factory-startup --disable-autoexec`: imports
as one mesh object, the importer converts Y-up to Z-up correctly, and it scales cleanly
to the 1.8 m FR-20 envelope (factor 1.797). Engine resolved to `BLENDER_EEVEE`.

Quality: recognisably the reference figure, with separated limbs, distinct hands and
feet, and a head that reads correctly in profile. The three-quarter view holds up, with
no melting on the side the single input image never showed — the usual failure mode for
single-image reconstruction.

**Single image, not two.** The supplied `Trellis2Multiviews_GGUF.json` wants two
*consistent* views, which is hard to produce for a subject that does not exist yet —
two independent generations disagree and the mesh melts. TRELLIS reconstructs from one
view, so `Simple.json`'s graph sidesteps the problem entirely. Use the multi-view
workflow only when genuinely consistent views exist, such as renders of an existing 3D
object.

### Texture quality is set by the reference prompt, not by the settings

The first run produced a real texture — 2048² base colour and metallic-roughness, UVs
present, wired correctly into `Principled BSDF.Base Color` on import (verified in
Blender, not assumed). But it is **flat mid-grey**: mean 148/146/149 per channel, with
speckle where the single view saw nothing. It is visually indistinguishable from an
untextured grey material.

That is a faithful result, not a fault. The reference prompt asked for "pale grey
featureless matte skin" on plain white under even diffuse light, which is optimal for
**geometry** — isolated subject, clean silhouette, no baked shadows — and the geometry
is good. It simply left no albedo for the texture stage to recover.

For a textured result, keep the geometry-friendly parts (isolated subject, full body in
frame, plain background, even lighting) and add *intrinsic* surface variation: mottling,
discolouration, staining, veining. Do **not** add dramatic lighting to the reference —
hard shadows bake into the albedo as permanent smears that then fight the shot's own
lighting.

For this project specifically, colour matters little: the entity ends up in a low-light
B&W profile, heavily degraded and small in frame. Tonal *variation* is what survives and
keeps the figure from reading as CG-clean.

**Still not verified:** nothing has been registered through FR-03 or rendered inside an
actual shot.

## The workflow needs two input views

`Trellis2Multiviews_GGUF.json` has two `Trellis2LoadImageWithTransparency` nodes: it
expects **two consistent views of the same subject, with alpha**. Two views that disagree
produce a melted mesh. Sourcing those is an open question, not a solved step.
