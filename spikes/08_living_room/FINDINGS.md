# Procedural living-room look test

**Orchestrator verdict: substantially improved, but a conservative FAIL on criterion 2.** `before-clean.png` reads as primitive clutter; `after-living-room.png` reads immediately as a domestic living room, yet the uniform upholstery, rigid rug, opaque blue window, clipped shade and sparse personal traces still feel like a digital set to me. Independent read-only review scores the final still PASS under the everyday-place rubric; that disagreement is retained, not presented as creator approval. See `comparison.png` and `ITERATIONS.md` for the three inspected iterations.

The mount is inferable as a high front-left corner security camera. Both final images use position(-2.48,-1.85,2.25), target(.2,.5,.2),12mm lens,3200K profile, zero exposure adjustment/distortion. No reference projection, generated bitmap or external asset is embedded.

The strongest gains were architecture with actual openings/trim and domestic proportions; grounded connected furniture arranged for use; and practical light positions clear of opaque fixture caps. Generic noise alone did little. Brightening a blocked lamp clipped its shade without making a useful pool. A correctly open bookcase still looked like a slab until turned toward the camera.

`scene.py` adds `american_living_room()` and additive material/geometry/light fields. The original `synthetic_interior()` source and its 92de3ed3...03788a plan hash remain unchanged. `worker.py` realizes those fields, keeps old defaults, and fingerprints modifier settings. `compose.py` selects the new builder for new default requests; persisted scene loading is retained. `test_living_room.py` independently checks geometry/support, openings, determinism and compatibility. `render_room.py` uses the production worker with contained outputs/cache/config, explicit EXR conversion and sampled telemetry.

| Final 120-frame run | Wall including startup | Mean completed-frame interval | Idle / peak / delta MiB |
|---|---:|---:|---:|
| Grey box |135.797s|0.818798s|1659.5 /2759.4 /+1100.0|
| Living room |142.844s|0.857008s|1659.5 /2761.5 /+1102.0|

Eevee BLENDER_EEVEE, RTX 5070 Ti Laptop,1280x960,32spp. New room adds 4.7% in this matched test. Historical 0.323s/frame was not reproduced by the baseline. No competing Blender process was observed during these final runs; device-wide memory includes other processes. `render-costs.json` records every manual render, including contaminated/aborted timing attempts. Frames 1/60/120 have identical decoded linear pixels within each final run (`stability.json`); EXR header bytes differ.

Focused verification: 71 tests passed; Ruff passed for production,tests and the spike harness. Final full-suite verification follows the composition switch. Existing service tests pin composition to the old fixture, so that switch has a known hash-expectation conflict; those tests have not been edited. The user's stop rule governs that result and commit remains pending.

Reproduce with the supplied venv: `python -B spikes/08_living_room/render_room.py --scene baseline --run-id NEW_BEFORE --frames 120` and the same command with `--scene living --run-id NEW_AFTER --frames 120`. Native Blender files, EXRs,logs and detailed telemetry remain under `_work/<run-id>/`.

No anomaly integration, alternative camera/profile, creator review or cross-launch pixel determinism was verified. Surface fidelity and irregular used-house dressing need further work.
