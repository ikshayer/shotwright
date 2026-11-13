# Pinned ComfyUI `/object_info` fixture

Real schema, captured from the pinned runtime. **Not hand-written and not edited** --
each retained class entry is copied verbatim from the live server response.

| fact | value |
|---|---|
| ComfyUI version | 0.44.0 |
| source commit | `2c75dc1b379a59e0880619d029607af5169a62ef` |
| captured | 2026-11-13 |
| full response | 1199617 bytes, sha256 `a63c24e3bee96a382a898d4117a08476d0ae31fc435315c87a5ac902000c4aca` |
| this fixture | 39597 bytes, sha256 `2cf0abaf2b98048a2f44e795ae962458b1a5db1466435d250f653c5ea0f0d701` |
| classes in full response | 710 |
| classes retained here | 14 |

## What was dropped, and why

The live response carries 710 node classes and is ~1.2 MB. Retained here are only the
14 classes the adapter's graph references (`core.py::_graph`), plus
`MiniMaxH3ReferenceToVideo` because `SCHEMA_VALIDATION_CONTRACT.md` names it for the
`COMFY_AUTOGROW_V3` case. The other 696 classes are unrelated to the H3 graph.

Retained: `MiniMaxH3EasyLoader`, `MiniMaxH3Easy`, `MiniMaxH3EasyOutput`, `MiniMaxH3EasyMediaLoader`, `BasicGuider`, `BasicScheduler`, `RandomNoise`, `KSamplerSelect`, `SamplerCustomAdvanced`, `VAEDecode`, `VAEDecodeAudio`, `CreateVideo`, `SaveVideo`, `MiniMaxH3ReferenceToVideo`

## Reproducing it

`_work/h3_stage/capture_object_info.py` launches the pinned runtime with the adapter's
own flag set, GETs `/object_info`, stops the process tree and asserts port 8298 is
free. It loads no model; measured ~30 s to an HTTP 200.
