# MiniMax H3 Easy: functional source review

Reviewed 2026-11-09, read-only. Pinned repository revision: `4b496eedeff944b2f1266c8c92df1b6c89758de0` (2026-10-15). This report uses the contained snapshot, not mutable main: cached web pages expose materially different line numbers and workflow names. No installation, uploads, model execution or output verification occurred. Optional extension research; no Phase 1 or PRD change.

## What it adds

Easy wraps H3 conditioning, loading, media management, segmentation and refinement; it is not another model or hosted H3 API. Reference mode accepts our clean Blender video, text and later consented audio. Camera, geometry, acting and timing require new approval.

[README at revision](https://github.com/nkxx188/ComfyUI-MiniMaxH3-Easy/blob/4b496eedeff944b2f1266c8c92df1b6c89758de0/README.md) describes media mentions, dialogue blocks and continuity modes. 

## Audio: reference versus driving track

In normal Reference mode, audio is conditioning for jointly generated video/audio. It does not preserve approved dialogue samples or provide separate generated ambience/dialogue stems.

Digital Human is different. In [nodes.py](https://github.com/nkxx188/ComfyUI-MiniMaxH3-Easy/blob/4b496eedeff944b2f1266c8c92df1b6c89758de0/nodes.py#L4955), generate requires one standalone Media audio resource, removes ordinary audio-reference tags, encodes the driver and locks the audio latent. `_lock_audio_latent` at line 3735 gives video a regenerate mask and audio a zero mask. It is not an ambience generator over an untouched voice track. If no standalone audio is supplied, lines 4959-4960 silently change Digital Human to normal Reference mode; a video with embedded audio is not a substitute for the required standalone Media item.

Crucially, preservation depends on the output graph. `MiniMaxH3EasyOutput.unpack` (5462-5487) exports conditioning/latent/VAEs/fps, not source_audio. Actual workflow 1 decodes sound through VAEDecodeAudio and sends that reconstructed waveform to CreateVideo. Thus locking audio latents does not establish sample-exact preservation of the input voice WAV.

Context Segment output takes another route: `_decode_streaming` skips generated-audio decoding when source_audio exists (7395), trims/pads the external complete soundtrack (7420), writes float PCM and encodes AAC (7441). `_segment_trim_audio` (4392) changes length to match the timeline; `_segment_audio_bytes` (4439) converts/clamps samples. These are explicit reasons not to call the final MP4 bit-exact original audio. Selected Video Refine also substitutes trimmed original soundtrack when preserve_audio is enabled (7723). No ambience-mixing operation was identified on these preservation paths. Keep approved voice WAV separately for editor mixing regardless of mode.

## FPS, continuity and memory

Unlike the simpler native reference node, `_video_parts` (3357) reads a VIDEO object's actual frame rate, and `_resample_video_frames` (3394) indexes frames onto the 24fps grid. A raw frame tensor assumes 24fps; mappings need an explicit fps/frame_rate. This is frame selection/repetition, not motion interpolation. Reference encoding additionally truncates to output length and the valid H3 frame grid. Record actual frame counts and duration rather than promising the input timeline survives unchanged.

Context Segments has Motion Context, RGB Guide and Soft/Hard AV Prefix options. Tail latents/frames and audio overlap constrain boundaries, not complete identity or continuity. Segment media binding uses references named in each segment, so repeat a required asset's mention rather than assuming global inheritance.

Segment decoding streams one segment through FFmpeg instead of retaining the whole decoded RGB timeline. Sample intermediates still retain CPU latents, models remain large, and decoded reference video may still occupy RAM. The explicit `REFERENCE_VIDEO_CACHE_ENABLED=False` at line 176 contradicts the README's cache benefit description. No 12GB GPU/31GB RAM fit has been demonstrated.

## Refinement and actual workflows

Workflow 2 is the most relevant new option for visual enhancement of our existing render. `_prepare_selected_video_chain` (5749 onward) encodes the selected source video into starting latents; it does not begin with a new empty visual latent or only treat the video as a reference. Official hosted 768P-to-2K restrictions do not describe this custom local path. Realism and structural fidelity remain unmeasured.

It is not an all-in-one Foley path: Segment Refine discards sampled audio at line 7235 and retains input audio latents at 7250. Its tiled branch masks audio off at 6851. Consequently a silent selected clip starts from empty audio latents and does not gain a newly generated soundtrack through this segment-refine output path. If source audio exists, Segment Decode substitutes that track. Test workflow 2 visual enhancement separately from R2V ambience generation initially. The separate SelectedVideoRefine class at 7482 has different behavior: preserve_audio defaults true, but with no source audio its output uses generated-audio decoding (7726); do not conflate that class with workflow 2 Segment Refine.


Pinned filenames, not newer web listings:

| File under workflow/ | Purpose / inspected defaults |
|---|---|
| `1.MiniMax_H3_Easy.json` | Basic generation; Euler, simple scheduler, 8 steps; 360P portrait reference-mode example. FL2VA configured and Ref2VA absent. Select correct Ref2VA weights explicitly for our experiment. |
| `2.MiniMax_H3_Easy_Selected_Video_Refine.json` | Existing candidate video into selected-video context, then refinement; Pixel Resize and latent alternatives; verify active/bypassed groups before queueing. |
| `3.MiniMax_H3_Easy_Context_Segments.json` | Initial segment chain; Euler/beta, 4 steps; prompts and segment durations require configuration. |
| `4.MiniMax_H3_Easy_Context_Segments_Refine.json` | Active latent upscale path, 1.5x and three-step custom sigmas; Pixel Resize group bypassed. |
| `5.MiniMax_H3_Easy_Context_Segments_Control.json` | Explicit Segment Steps for per-segment seeds/prompt overrides and selective downstream recomputation. |

Source `_prepare_video_body` (6610) distinguishes decode/resize/re-encode from learned 3D latent upscaling. Both feed H3 resampling afterward: refinement can invent/change detail, not simply enlarge pixels. Selected-video preparation encodes an existing candidate without first-pass denoising; the refinement stage performs the generative change. Low-VRAM tiling trades computation for spatial working-set reduction; it does not establish whole-machine feasibility.

## Integration boundaries

Media Bridge (`MiniMaxH3EasyMediaBridge`, line 2807) exposes numbered image/video/audio inputs and counts for an API/headless graph, reducing reliance on browser virtual connections. A future GUI could author a contained graph and export standalone WAV/MP4/JSON without H3 needed to reopen. The bundle is not our production schema.

Existing ComfyUI 0.21.1 remains immutable and lacks the required native H3 dependencies. Use only a separately authorized contained runtime if pursued. Bundled workflows also contain external utility nodes (including RAMCleanup and group bypass controls), LoRAs, attention patches and model selections; do not import them as a minimal production dependency set. Defaults are examples, not our benchmark. Remove unnecessary utility nodes, fix the seed, set our intended 4:3 framing, and explicitly choose Ref2VA for reference generation before evaluation. Resolve dependencies from the pinned graph; node names alone do not establish package requirements. The plugin MIT licence does not replace H3 model licensing, including the previously documented US exclusion and output restrictions. Completed source review found optimizer defaults read_media=False and optimize_on_run=False (nodes.py 308-317). When enabled, a remote provider receives prompts and optionally media. Configuration stores API keys in plaintext beside the plugin (675/729); settings GET returns the whole config (2165). Host authentication was not verified. Disable optimization for the bounded spike. The optional [upscaler loader](https://github.com/nkxx188/ComfyUI-MiniMaxH3-Easy/blob/4b496eedeff944b2f1266c8c92df1b6c89758de0/h3_latent_upscaler.py#L269) accepts pt/pth and loads non-safetensors with weights_only=False (269-274); require pinned, hashed safetensors. Segment output writes under Comfy temp (nodes.py 7338), while configuration writes beside the plugin. Before production, adapt these paths to a ProjectRoot-contained runtime and record provenance. Parent independently verified these findings; they are integration constraints, not a claim of exploitation.

Unverified: actual graph compatibility, audio-mask behavior through each sampler, lip sync, voice fidelity, temporal continuity, realism, model fit and speed. No output was generated. Source inspection does not establish a fix for the Foley failure.

Parent independent verification: all 19 snapshot hashes matched; Python AST and workflow JSON parsing passed without importing or executing plugin code. I inspected local source and parsed workflow defaults. These checks do not validate graph execution, hardware fit or generated quality.
