# MiniMax H3 workflow research

Research date: 2026-11-09. Research only: no implementation, uploads, accounts, paid requests, package installs, model downloads or inference. This concerns MiniMax H3, not Hailuo 2.3. It is an optional pipeline extension, not an adopted change to the PRD local-first requirements or Phase 1 scope. Source pages and commercial terms can change.

## Fit for this pipeline

H3 can accept our original Blender video and optionally owned or consented audio, then generate a new audiovisual candidate. Treat that as a generative remake requiring fresh approval. R2V guidance does not establish exact preservation of camera, geometry, motion, event times, voice waveform or performance. Proposed order: clean Blender master -> H3 candidate AV -> human review -> existing CCTV degradation. Feeding an already degraded delivery risks baking its defects into generation; that is an implementation inference, not a measured H3 result.

Three operations differ: R2V transfers reference content into a new shot; structure-guided generation constrains a remake using edges/depth/pose; upscaling increases resolution. MiniMax's H3-Regenerate-2K specifically regenerates an H3 768P result with its original context. It is not documented as an arbitrary Blender-video enhancement endpoint. Official H3-Base generates 768p, 24fps, 32kHz stereo; hosted H3-Context-IR is not released locally. See [MiniMax repository](https://github.com/MiniMax-AI/MiniMax-H3).

## Official local workflows

[Comfy overview](https://docs.comfy.org/tutorials/video/minimax/minimax-h3) links actual templates:

- [R2V JSON](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_r2v.json): use Ref2VA weights and MiniMaxH3ReferenceToVideo. It samples joint audio/video latents; VAEDecode and VAEDecodeAudio decode their respective streams, then CreateVideo/SaveVideo combine them. Default example assets must be replaced with ours.
- [Fun ControlNet Union JSON](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_fun_controlnet_union.json): optional later comparison using our Blender depth/edges instead of unrestrained reference guidance. Supports Canny, Depth, HED, MLSD, Pose and masked inpainting; no exact full-shot preservation guarantee.
- [Multiframe JSON](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_multiframe_reference.json): chained Add Guide nodes anchor compositions at frame indices. Timeline anchors are not automatically visible to the text encoder; also connect images as reference inputs when prompts name them.

[Native documentation](https://docs.comfy.org/tutorials/video/minimax/minimax-h3-native) treats reference frames as 24fps without source metadata: convert the 60fps clean master explicitly before loading. Video frames and soundtrack require separate ref_video_N/ref_video_audio_N connections; standalone audio needs audio_vae. Use <Video 1>, <Audio 1> and <Picture 1> tags and state each reference's role. Audio numbering includes video soundtracks before standalone clips. Voice references convey timbre; specify words and delivery separately. Exact prosody, waveform preservation and event timing are unverified. Native output is one mixed stereo soundtrack containing generated voice/SFX/music, not separate stems. Extracted WAV preserves that combined mix; retain approved original voice WAV separately for editor mixing.

The native guide's advanced latent noise masks use 0=keep and 1=regenerate for video/audio. Add Guide conditioning starts from an empty latent rather than a conventional denoise-strength V2V input. Guides can anchor compositions but do not establish complete identity or timing preservation. Fixed-video/generated-audio is therefore an untested graph experiment, not a verified turnkey workflow or audio-only API. Verify actual connections, masks, encoded-video reconstruction and measured sample preservation before claiming that it keeps our accepted video.

## Local runtime and rights

Native workflows require ComfyUI 0.30.0+, multiframe 0.34.0+, Fun ControlNet 0.35.0+ (Union 2.0: 0.38.0+). Existing immutable ComfyUI 0.21.1 is unsuitable for those documented graphs. Any authorized local spike needs a separate contained installation.

The [Comfy model card](https://huggingface.co/Comfy-Org/MiniMax-H3) supplies repackaged safetensors and prefers INT8 ConvRot with PyTorch cu130. Standard template components include a roughly 19.53GB transformer, 14.61GB text encoder and video/audio VAEs; quoted sizes are files, not GPU requirements. Smaller community quantizations/offload recipes are indexed by [MiniMax integrations](https://github.com/MiniMax-AI/awesome-minimax-h3-integration), but no fit, speed or quality has been measured on our 11.94GB GPU/31.1GB RAM. Do not infer compatibility from sm_120 alone. Pin and hash every eventual component and review third-party loaders independently.

The [actual Community License](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE), dated August 2, 2026, excludes the US, EU, UK and South Korea. Territorial restrictions explicitly include outputs/results. Its revenue threshold does not override those restrictions. [Comfy licensing FAQ](https://comfy.org/minimax/license) confirms that excluded-territory local use needs separate authorization and advertises commercial rights included with Comfy Cloud generation. Hosted/API terms, applicable territory, retention/privacy, input rights and actual plan entitlement must separately be verified before submission; local licence restrictions should not be mechanically substituted for hosted terms. This research grants no clearance.

## Hosted route and bounded proposal

[Official V2 API](https://platform.minimax.io/docs/api-reference/video-generation-v2-create) uses POST /v2/video_generation with explicit model MiniMax-H3 and multimodal content. Published limits: 4-15s, 768P/2K; up to three videos, each 2-15s, total <=15s, <=50MB/file; up to three audio references, each 2-15s, total <=15s, <=15MB/file. No documented seed, edit-strength or audio-only switch was identified. [Official API pricing](https://platform.minimax.io/docs/pricing/overview) lists 768P input video and output each charged $0.08/second: 8s input plus 8s output is $1.28; 10+10 is $1.60. Recheck the current official pricing before approval; these are API estimates, not Cloud subscription prices.

After rights, route, privacy and cost are resolved, propose one 5-8s owned clean-master segment and one R2V candidate at 768P, with restrained room sound and explicit static-camera/motion instructions. Compare original/candidate side by side for geometry, motion timing, temporal flicker and realism; assess raw generated sound separately. Optional future consented voice references need identity consent and explicit attribution of speaker/dialogue; defer them from the first comparison. A guided depth/edge variant is a later controlled comparison, not an automatic retry loop.

Retain original sources, all native candidate AV, extracted native WAV, separately transformed 48kHz editor WAV and JSON with relative-path base, hashes, rational frame rate, integer sample offsets, prompts, model/runtime identities, transformations, rights provenance and approval state. Candidates are new unapproved versions; original visual approval does not transfer. Reconcile H3's valid frame grid and actual measured duration explicitly. Future GUI opens standalone media/JSON without H3 installed.

## Unverified

No graph was executed, no output quality or preservation measured, no machine memory benchmark performed, and no hosted entitlement, retention policy or account pricing verified. Primary sources establish capabilities and constraints, not success on this shot. Independent verification and human audiovisual approval remain required.

## Follow-up: ComfyUI-MiniMaxH3-Easy

Creator supplied the nkxx188 wrapper for inspection. See [EASY_REVIEW.md](EASY_REVIEW.md) for the static review of pinned commit 4b496eedeff944b2f1266c8c92df1b6c89758de0. It adds an existing-video refinement path distinct from the official hosted regeneration endpoint. Its selected-video segment refinement retains input audio latents rather than adding fresh ambience; Digital Human drives visuals from one supplied audio track. Neither path establishes exact visual/audio preservation. No plugin was installed or executed. Local memory fit, output quality and licensing remain unresolved as documented above.
