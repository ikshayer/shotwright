# Spike 07 experimental contract

Use the creator-supplied completed Phase 1 scene.blend and first frame as the baseline (the corrected spike-02 fixture). Retain source hashes and camera identity. Compare 1280x960 baseline, generated-only FLUX.2 klein still and that still projected onto static room meshes. One fixed camera projector, frozen for the second-angle test; no camera-following billboard or screen composite. Baseline and projection use identical viewing camera/framing. Record exact prompt and seed. No reference image input or new model weights.

Generation uses the verified adapter and FR-06 pinned installed hashes. All writes (including subprocess paths, logs, temp and caches) pass through ProjectRoot and stay in this spike or _work. Disable bytecode, autosaves and external caches. No production/test/spec edits, no commit.

Measure device-used VRAM before each path and sampled maximum while running, with sample cadence and peak-minus-baseline. Record wall times separately for generation cold adapter, prompt execution, Blender startup/setup and individual warm frames; existing historical frame costs remain historical. No requirement inferred from device occupancy.

Test frozen-camera frames 1/450/900; do not imply rendering all 900 unless actually done. Test a second viewing camera without changing the projector or UVs. Apply one existing pinned degradation profile with identical clock/noise seed/schedule to all three paths. Prefer a moving geometry proxy to expose physical occlusion/contact/lighting mismatch.

Implementation author does not author tests or score the result. Independent read-only reviewer reports defects rather than fixing; parent inspects rendered images and independently reruns artifact/provenance checks. Criterion 2 is binary but the relative image judgement is provisional until creator review. Explicitly report geometry mismatch, off-axis stretching, baked lighting, untested full sequence/figure limits and what could reverse a verdict.
