# Found-footage walkthrough

The fixed room camera introduces a compact, late Soviet-era-inspired apartment. The set measures about 3.7 by 3.3 m with a 2.17 m ceiling, faded floral wallpaper, a wall carpet, dark veneer cabinets, and a curved-screen CRT. The eye-level handheld camera walks forward between the table and TV toward the lit hallway. The hall light falters and cuts out as the operator stops. The recording uses monochrome, light sensor noise, low room tone, and original footsteps.

Run from the worktree using the existing Shotwright virtual environment.

```powershell
& 'D:\Work\shotwright\.venv\Scripts\python.exe' -B 'spikes/30_at18_found_footage/run.py'
```

Use `--preview` to render the establishing frame and five walkthrough frames, or `--plan-only` to write the plan without starting Blender. `--blender`, `--ffmpeg`, and `--ffprobe` can point to existing alternate executables.

Each run writes its own `spikes/30_at18_found_footage/_work/<run-id>/` directory. `walkthrough.blend` contains both cameras, the animated POV, materials, and lighting. `walkthrough.mp4` is the six-second POV. `rough-cut.mp4` starts with a 1.4-second establishing insert. Both videos are 384 × 288, 15 fps, 4:3, with 48 kHz mono AAC. The PNGs, logs, plan, timing receipt, and FFprobe stream reports are retained. The cloth, ceramics, paper props, printed landscape, and sound are original procedural work. No reference media or downloaded assets enter the scene.

The diagnostic uses the existing Blender worker to build a procedural room and enables CPU Cycles in its own Blender process. It applies the local dressing and camera animation before rendering. The 384 × 288 resolution and eight samples intentionally keep the review render bounded. The resulting scene and media are diagnostic artifacts and carry no production acceptance or shot approval.

## FLUX and TRELLIS sofa pilot

Run the isolated, offline generation harness from the worktree with the existing orchestrator environment.

```powershell
& 'D:\Work\shotwright\.venv\Scripts\python.exe' -B -m spikes.30_at18_found_footage.generate_trellis_sofa
```

The harness uses the pinned local authoring runtime from spike 16 and records model hashes, prompt graphs, inference history, and output hashes under the reported `_work/sofa-*` path. A retained FLUX image with a matching receipt can be converted with `--reference <worktree-relative-png>`. The local TRELLIS quantization is used for internal evaluation.

Pass the resulting worktree-relative GLB to `run.py --sofa-asset <path>`. Set `--sofa-yaw` if the asset needs to turn around its vertical axis. The renderer fits the mesh inside the previous footprint and keeps the original cameras and lighting. The run report records bounds, clearance, triangle count, and packed textures.
