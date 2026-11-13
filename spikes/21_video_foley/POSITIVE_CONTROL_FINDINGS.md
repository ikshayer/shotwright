# Owned motion control: results (2026-11-09)

**The weak output reproduced on a different controlled scene.** Neither the original nor a neutral negative prompt produced useful impact audio in this configuration. The codec can reconstruct a separate known signal, and a fresh decoder reproduces the captured generation exactly. Investigate generated latents, conditioning, sampling or the latent-to-decoder interface next. This is not proof of general model failure or a specific implementation bug.

## Scope and provenance

User asked whether the failure was strictly our CCTV clip, then explicitly authorized a clear owned/licensed motion-control test. POSITIVE_CONTROL_CONTRACT.md and Amendments1-3 govern this follow-up. No production adapter, GUI, package environment or ComfyUI change; no new model weights or third-party reference video. Existing accepted shot/master remain unchanged. Same pinned XL/offload runtime and checkpoint identities as FINDINGS.md.

Separate implementer created original procedural basketball geometry/materials and an8s silent video. Renderer uses a fixed bright camera, orange rubber-like ball with attached black seams, visible wood floor/shadows and authored contacts at0.8,2.2,3.6,5.0,6.4,7.8s. Parent visually inspected preview8 and the corrected full-run contact sheet. This synthetic input is a controlled diagnostic, **not an already-established successful end-to-end Hunyuan baseline**.

First full fixture `_work/positive_control_20261109T063004Z` had0.015 scene-unit ground clearance. Independent verifier failed all six contacts; source geometry was corrected and rerendered. Failed fixture/report remain. Corrected artifacts are `_work/positive_control_exact_contact/`:

- `positive_control_silent.mp4`, SHA256 `4ea089940ecdb7f7687fc1151fa0cb00d60fe621d9f55060d03d4757f9445116`.
- `provenance.json`, `blender_worker.py`, `scene.blend`, `frames/`, `contact_sheet.png`, `parent_input_verification.json`.
-640x480,25fps,200frames,8s, H.264 and no audio input. Zero-based contact indices20,55,90,125,160,195 match ball center_z0.58/radius0.58/floor_z0; intervening arcs verified. Parent rechecked the actual rendered sheet. Worker read back BLENDER_EEVEE, Blender5.2.0 LTS. Hardware renderer/vendor was not queried; no backend-selection measurement is claimed.

Initial live preflight:12.339GiB available RAM,86.635GiB disk free, GPU1444/12227MiB used. Rendering and inference were serialized. Exact worker source hash is recorded; original scene/materials use no external visual assets.

## Controlled generation comparison

Same video/hash, positive prompt `Basketball bouncing on a wooden floor.`, seed210915,50steps,guidance4.5, BF16 main model/FP32 decoder, offload, device, package versions and exact runtime source hash. Parent checked those matched variables in `comparison_results.json`. Both runs saved the actual decoder-input tensor as hashed safetensors; capture detaches/copies only and does not change RNG or model operations by inspection.

| Run directory | Negative prompt | Native duration | Integrated LUFS | True peak dBFS | Contact/mid-arc energy ratio | Generation time |
|---|---|---:|---:|---:|---:|---:|
| audio_original_negative | speech, voices, dialogue, music, singing, loud impacts, harsh distortion |7.96s|?67.95|?38.13|1.086|106.360s|
| audio_neutral_negative | speech, music |7.96s|?65.95|?36.25|1.119|103.031s|

Both native files are48kHz mono float WAV,382080samples. Raw RMS7.7908e?5 and8.5887e?5; DC~3.1e?5; no clipped samples. Contact/mid-arc windows are fixed100ms intervals around predeclared contacts/apices, not fitted after generation. These ratios show little contact-associated energy contrast; they do not prove semantic alignment or absence of an off-grid sound. Parent did not claim to hear or approve generated Foley.

Read-only review identified `loud impacts` as a possible conflict in the old negative prompt. Removing it did not resolve weak output in this control. The2LUFS difference is not treated as proof of a causal improvement or useful audio. Only one seed was tested and numerical repeatability was not established. Different scene/positive prompt from CCTV prevents isolating the original video as the sole variable.

Both runs completed under the unchanged memory/time watchdog. Peak whole-device VRAM7684MiB (includes other applications); minimum available RAM5.204/5.115GiB. Whole process166.188/158.188s. Power/clocks were not controlled, so timings are observations rather than a controlled comparison with older runs. No further full-generator takes were run; the conditional CCTV negative-prompt rerun was skipped because the control was not useful.

## Independent component checks

`codec_probe_cuda/` contains a **separate test signal, not Hunyuan generation**:2s48kHz440Hz sine plus seeded decaying noise impulses. It was never supplied to the video-to-audio generator, never mixed into the basketball audition and is labelled NOT_HUNYUAN_GENERATION.

The independently authored probe verified the official DAC checkpoint SHA256 `07e6139ff33bd21ba8a7a7f40ed24aab13ac0d100cd686f8a9f3e03dc5251cb1`, then loaded tensors with weights_only=True/mmap and exact constructor/state. VAE encoder -> posterior.mode -> decoder reconstruction measured:

- Input RMS0.1093799712; reconstruction RMS0.1086261417.
- RMS ratio0.9931081575; zero-lag correlation0.9621370233.
- Both96000samples/2s; no gain or normalization, no arbitrary pass threshold.

This shows useful reconstruction of one controlled signal, not complete codec validation or semantic Foley success. Reports: `codec_probe_cuda/{codec_report.json,codec_watchdog.json,codec_worker.log}`.

A fresh-decoder comparison initially hit the6GiB admission guard at5.224GiB free RAM. No guard was weakened and no user app closed. Creator freed memory and answered try again; parent measured7.379GiB free and resumed successfully. The earlier probe's observed free-RAM delta was about4GiB, supporting keeping the guard.

`captured_decode_cuda/` independently decoded the neutral run's exact saved float32 tensor shape[1,128,398], SHA256 `818d37b9d6b751a002f037fa2f6859be4e413cafe3a8f84db92d77acd6903e51`, in a new process with the same verified decoder weights. Result:382080samples/7.96s, **difference RMS0 and maximum absolute sample difference0** against the original native waveform. WAV container hashes differ, but decoded samples are exactly equal. This is NOT_NEW_GENERATION; no new diffusion call occurred.

Parent recomputed both the reconstruction metrics and sample equality from actual files using independent FFmpeg PCM decoding and Python arrays, retaining `parent_component_verification.json`. This weakens a simple decoder-gain, WAV-writing or leftover full-inference-context explanation. Remaining causes include generated latents, conditioning, sampling, their decoder interface or compatibility. The diagnostics share DAC source/weights/runtime and cannot conclusively rule out every decoder/interface issue.

## Review artifacts and interoperability

Both run directories contain:

- Original `native.wav`, `generation.json`, `watchdog.json`, `runtime.log`, runtime source snapshot and `dac_input_latent.safetensors`.
- `parent_raw_verification.json` and `parent_audition_verification.json`: structural checks passed; no human/semantic quality approval.
- `review_raw/audition.mp4`: unchanged compressed control-video packets with AAC audio encoded from the diagnostic take.
- `review_raw/edit_48k.wav`: separate48kHz PCM24, exactly384000samples/8s; explicit40ms tail padding from the7.96s native. No gain or normalization applied, original preserved.
- Root-relative `editor_manifest.json` and standalone `editor_bundle/project.json` plus source MP4/native WAV/edit WAV/audition, manifest-directory-relative paths and hashes, generation/model provenance and licence restrictions. Model runtime is unnecessary to open media; no particular editor tested.

Independent verifier checked real video frames/duration/contacts/arcs/source hashes, native levels/event windows and identical audition video-packet payloads. Renderer, runtime/postprocessor and verifier had separate authors; read-only reviewer assessed interpretation and source compatibility. Parent reran actual checks. Ruff spike21 and diff whitespace checks passed; no new full production pytest run claimed. Prior production/test sources remain unchanged.

## Conclusions and limits

The same low-level-output problem occurs outside the CCTV scene in this setup. **Do not blame or rebuild the accepted shot based on the current evidence.** The decoder reconstructs a known owned signal well, while a captured generated representation consistently decodes into the same weak audio. Next investigation should focus on the generation/conditioning/sampling path and latent interface, ideally against an independently working reference stack or authorized known-success end-to-end input. No specific root cause was proven, and no runtime/model/seed was globally validated or rejected.

Both video inputs are computer-generated, all generation comparisons used one seed, and the meta-loading workaround prevents assuming numerical identity with upstream initialization. These limits remain. Overall audio production acceptance is not met; none of these generated sounds is creator-approved.

Hunyuan territory restrictions including outputs (EU/UK/SouthKorea excluded) still prevent claiming worldwide release clearance. This controlled visual source is entirely original, but that does not remove the model licence. No model adoption, publication, editor GUI implementation, commit, push or merge occurred.

Final parent verification additionally checks both standalone editor bundles, all media hashes, exact384000-sample PCM24 outputs,384000?382080=1920zero tail samples, and raw/edit differences within PCM24 quantization error (no gain). Stored in parent_bundle_verification.json. Production/test Python hashes match the prior1744-test snapshot; primary tracked checkout is clean. Final Ruff spike21 and git diff --check exited0 (line-ending advisories only). All inference/codec work completed; no pending RAM hold or further jobs.
