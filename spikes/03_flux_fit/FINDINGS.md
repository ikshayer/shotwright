# Spike 03 — FLUX.2 klein 4B fit, and the first measured GPU lease

Run 2026-11-04. ComfyUI 0.21.1, torch 2.12.0.dev20260408+cu128, RTX 5070 Ti
Laptop (11.94 GB), allocator `cudaMallocAsync`.

## Finding 1 — it fits, and all four configurations complete

Every configuration completed with custom nodes disabled: the distilled 4B model with
either text encoder, at 1024×1024 and at 1280×960 (§4.1's delivery geometry at the
master spec's 2×). Wall clock is **27–48 s** per 4-step still, including model load.

Total weights are 7.22 + 3.58 + 0.31 = **11.11 GB** with the fp4 encoder and
**15.6 GB** with the full one — both more than the 11.94 GB card holds. It works only
because ComfyUI loads the text encoder, encodes, frees it, then loads the diffusion
model: the sequential pattern §8.3 already mandates, now demonstrated rather than
assumed.

## Finding 2 — "measured peak VRAM" is not a property of the model, and NVML cannot measure it

This correction matters more than the numbers it replaces.

Across runs, NVML device-used peaked at 9.16, 10.46, 11.62, 11.70 and 11.72 GB for the
*same* configurations. The variable was how much memory happened to be free when the
run started (idle baseline drifted 1.33 → 2.68 GB across the evening as desktop usage
changed). ComfyUI expands to fill available memory and does not return it between jobs.

`--reserve-vram 6` and `--reserve-vram 8` did **not** cap it: peaks stayed at
11.6–11.7 GB. The flag influences model-residency decisions, not total allocation. And
under the `cudaMallocAsync` allocator in use here, freed blocks remain in the pool and
still read as "used" to NVML.

**Consequences for the document:**

1. §8.4's `measured peak VRAM` cell cannot be filled with a single number from NVML.
   The honest entry is a *completion envelope* — which configurations complete, at what
   geometry, with how much free memory at start — plus the allocator in use.
2. NVML remains the correct instrument for the **lease** (§8.1): "is the card free for
   another process?" is exactly what device-used answers, and release was measured at
   0.5 s (Finding 4). It is the wrong instrument for "how much does this job need".
3. For the job-level number, read torch allocator statistics from inside the worker —
   ComfyUI exposes them on `/system_stats` — rather than inferring from the device.
   Spike 04 should do this.

The only hard failure observed under constraint: at `--reserve-vram 8`, `VAEDecode`
raised `RuntimeError: bad allocation` at 1024×1024, while 1280×960 succeeded in the
same run. Memory behaviour near the limit is **not deterministic**, which is itself
the argument for FR-12's precondition and for the throughput watchdog.

## Finding 2 — the documented VRAM figures were never contradictory

§17 ref 4 recorded "self-contradictory official figures: 8.0 / 8.4 / ~13 GB". Measured
here, those are **peak-resident figures on a 32 GB 5090**, not total weight size, and
not a minimum requirement. 8.4 GB for the distilled model is consistent with our
9.16 GB at the same resolution once the ~1 GB foreign baseline is added. The sources
disagreed about *what was being measured*, not about the model. §17 can be corrected
from "contradictory" to "differently scoped".

## Finding 3 — a third-party custom node broke generation entirely

The first run failed at `SamplerCustomAdvanced` with `CUDA error: unknown error`,
thrown inside `comfy/ldm/flux/layers.py:40, timestep_embedding` — trivial sinusoidal
math. The call stack passed through:

```
custom_nodes\comfyui_smznodes\smZNodes.py, line 189, in sampling_function
```

smZNodes monkey-patches `comfy.samplers.sampling_function`, placing itself in the path
of **every** generation. Relaunching with `--disable-all-custom-nodes` made both
configurations succeed unchanged.

The ambient install carries 20+ third-party nodes for unrelated projects. Any of them
can patch core sampling, and the failure surfaced as an opaque CUDA error, not as
"a custom node is incompatible".

**Requirement:** the orchestrator launches ComfyUI with `--disable-all-custom-nodes`
plus `--whitelist-custom-nodes` naming only what the pipeline needs
(`comfyui-videohelpersuite`, `comfyui_controlnet_aux`, `ComfyUI-GGUF`). Both flags
verified present in this build. This also makes §8.4's "custom-node set" pin
*enforceable* rather than merely recorded.

Side benefit: boot time fell from **150.5 s to 33.5 s**, a 4.5× improvement, because
the disabled nodes were no longer imported.

## Finding 4 — measured release works, and fails exactly where §8.1 predicted

- **After a successful job:** `POST /free` returned memory below floor in **0.5 s**.
  AT-13's core postcondition holds, and §13's "unload does not free VRAM" risk does
  not materialise on the success path.
- **After the CUDA error:** release **failed** — 60 s timeout with 6.44 GB still held,
  and the next job failed in 1.0 s on the poisoned context.

So both branches are now empirical: the measured postcondition is cheap and reliable
when the job succeeded, and **worker respawn is mandatory after any CUDA error**,
because the context does not recover and `/free` cannot fix it. §8.1 already requires
respawn-on-failed-release; this is the failure it was written for.

## Finding 5 — startup cost argues for a resident worker

33.5 s clean, 150.5 s with the ambient node set. Restarting ComfyUI per job would add
that to every generation. The resident-process-plus-measured-lease design in §8.1 is
the right one, and the respawn path of Finding 4 is the exception rather than the rule.

## Finding 6 — the onnxruntime defect predicted in §8.4 is real

Startup log carries `AttributeError: _ARRAY_API not found` from
`onnxruntime\capi\_pybind_state.py`, the numpy-2 incompatibility of the pinned
`onnxruntime-gpu` 1.18.0. It does not break generation, but it breaks the import chain
through kornia, which means any ControlNet preprocessor depending on it is unavailable.
Spike 04 must resolve this before the depth-conditioning path can work.

## Pinned configuration

| Item | Value |
|---|---|
| Diffusion | `flux-2-klein-4b.safetensors` (distilled), 7,751,105,712 B |
| Text encoder | `qwen_3_4b_fp4_flux2.safetensors`, 3,848,213,998 B, CLIPLoader type `flux2` |
| VAE | `flux2-vae.safetensors`, 336,211,292 B |
| Sampler | euler, `Flux2Scheduler` 4 steps, `CFGGuider` cfg 1.0 |
| Note | `Flux2Scheduler` takes **no** model input on this build |

The full `qwen_3_4b.safetensors` (8.04 GB) is the configuration ComfyUI's own template
declares; it is being measured separately as the declared-vs-fallback comparison.

## Reproduce

```
.venv\Scripts\python.exe spikes\03_flux_fit\run_spike.py --introspect
.venv\Scripts\python.exe spikes\03_flux_fit\run_spike.py --clean-nodes
```
