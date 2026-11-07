# Finding — DegradeResult.output_fps is not measured

File: orchestrator/adapters/postprofile/runner.py

`_probe()` parses and validates `avg_frame_rate`, `width`, `height` and `duration`
into `output_probe`, and the runner DOES verify `output_probe["frame_count"]`
against `validated.predicted_frames` (good).

But the returned `DegradeResult` sets:

    output_fps=request.profile.output_fps

i.e. the REQUESTED rate, not the probed one. Nothing ever compares
`output_probe["avg_frame_rate"]` with `request.profile.output_fps`, and nothing
compares probed `width`/`height` with the profile's output resolution.

Why it matters: `output_fps` presents as measured evidence about the artifact and
is in fact an echo of the input. A profile/FFmpeg divergence would be reported as
conformance. Same shape as the EXR-hash mistake: a value trusted without being
measured.

Fix shape: compare probed avg_frame_rate and dimensions against the profile, fail
with JobErrorKind.CORRUPT_ARTIFACT naming observed vs expected (matching the
existing frame-count refusal), and populate output_fps FROM the probe.

Not a Phase 1 blocker. Needs a contract + blind test pair like the others.
