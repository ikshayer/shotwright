# Spike 09: Low-key living room

## Decision and implementation

Iteration 04 is the selected lighting look. It is an opt-in `american_living_room(look="low_key")`; calling the builder without `look` retains the bright composition. Both variants use the same generic `SceneSpec` material and light fields. Low-key sets world strength to 0, uses explicit wood panels (width 0.18, seam 0.003) with texture scale (10, 10, 0.3), and does not add a worker-side look branch. Its saved camera profile uses monochrome, 6500 K, EV 0, and a 20 mm lens.

The strongest visual gains came from removing ambient fill while leaving one correctly unblocked practical light, balancing the wall panels and materials (including a matte window and darker sofa), and reframing to include a partial figure. Compared with reference `134126`, the directional-lighting goal succeeds, but iteration 04 remains a CG image and is darker than the key reference: near-black pixels are 52.4% versus 25.5%. Other supplied references support a range of extremes. The production worker continues to render the same neutral master path; this harness converts the EXR to display sRGB without adding degradation, contrast, or grain.

## Iterations

- **01:** All-zero image. The point light was trapped inside a closed cone; the no-monochrome diagnostic confirmed a lighting failure rather than a monochrome conversion issue.
- **02:** A convincing black pool, but the room and figure were too occluded.
- **03:** Too bright, with practical-light glare.
- **04:** Selected. It restores room readability while retaining a dark surveillance look and a partially visible figure.

The original `synthetic_interior` function was compared with HEAD and is unchanged.

## Render budget and recovery

- Matched bright benchmark run: **92.672 s** startup-inclusive wall time and **0.4300168 s/frame** warmed cadence. The supplied cadence reference is **0.373 s/frame**.
- **Final iteration 04, 120 frames:** **Telemetry lost.** The agent hit its account usage limit while assembling this report, so the final run figures were never written. The log recorded an NVML idle/peak/delta of 2381.8 / 4635.6 / 2253.8 MiB and interval means of 0.429 s (first 60 frames) and 0.635 s (last 60) before it stopped; these are unverified fragments, not a completed measurement, and should be re-measured rather than cited.
- **Full final test suite:** **1446 passed, 34 skipped, 6 warnings in 278.03 s.** Independent final suite log: `_work/final-pytest.log`.

The first bright `current-rerender` render completed, but its harness crashed while assembling telemetry because `launch_monotonic` was scoped inside the log callback. Its EXR was recovered to PNG without rerendering. The recovery record preserves worker elapsed time (40.821 s), while startup-inclusive wall time and NVML are explicitly unavailable because those samples were in the crashed process.

## Limits and reproduction

This remains a CG composition, not a reference-realism result: surfaces are regular, the figure has primitive geometry and insufficient depth detail, and the image lacks real-lens softness, analog grain, and lived-in dressing. No full humanoid, degradation pass, or creator approval is included. Image statistics are descriptive pixel measurements, not a visual verdict.

Final inputs are preserved in `final-scene.json` and `final-profile.json`; `final.png` is the selected output and `side-by-side.png` compares it with the supplied reference. Runtime outputs remain under `_work`.

From the repository root, reproduce with a new run ID:

```powershell
D:/Work/video_pipeline/.venv/Scripts/python.exe -B spikes/09_low_key/render_room.py `
  --look low_key `
  --scene-json spikes/09_low_key/final-scene.json `
  --profile-json spikes/09_low_key/final-profile.json `
  --run-id REPRODUCTION_ID --frames 120
```

The final Ruff checks across orchestration, tests, and spike code passed. An initial focused test run produced 12 Windows temp-permission errors; rerunning with a contained `--basetemp` resolved them. These were environment errors, not code failures.
