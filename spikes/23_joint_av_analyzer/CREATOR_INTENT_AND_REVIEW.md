<!-- START H3 CREATOR-REPORTED SUCCESS 2026-11-12 -->
## Current generation guidance: explicit audibility and relative-mix direction

On 2026-11-12, the creator reported successful MiniMax H3 generations in their ComfyUI setup: explicitly telling H3 in the generation prompt to be loud produced loud output. Setup identity supplied by the creator: [ComfyUI-MiniMaxH3-Easy](https://github.com/nkxx188/ComfyUI-MiniMaxH3-Easy); the link identifies the setup only, while loudness is the creator's observation.

The creator has now supplied an exact walking-footage prompt and states that the example has sounds. [The exact supplied prompt](../22_minimax_h3_workflows/CREATOR_SUCCESSFUL_PROMPT.md) retains it verbatim. It explicitly requests clearly audible continuous broadband hiss, quieter overlapping wooden creaks, no hiss fade/ducking, and near-silent movement without shoe impacts or breathing. This is an audibility and relative-mix direction, not a request to make every sound loud.

H3 is creator-demonstrated viable; earlier measured quiet runs remain historical rather than the current overall conclusion. Successful workflow JSON/output paths and exact model/runtime/settings are still not supplied. The prompt alone is not an established automated recipe or controlled same-seed proof. No new listening/measurement, visual/reference-retention or negative-instruction compliance verification, or production approval is claimed.
<!-- END H3 CREATOR-REPORTED SUCCESS 2026-11-12 -->

# Shared creator intent and Qwen review brief

Updated 2026-11-12 UTC. Read this before treating a model caption as the creator's intent.

## Purpose stated by the creator

Qwen Omni is an audio-capable helper for Claude/Codex, which cannot listen to the soundtrack in this session. Its job has TWO parts:

1. Analyze reference footage so Claude/Codex can understand the creator's intentions and ask useful follow-up questions.
2. Review actual generated video/audio, assess the sound against the creator's intended result, and suggest actionable changes for Claude/Codex to make.

The goal is useful communication and a review/improvement loop, not maximizing caption length. Creator explanations supply creative direction. Qwen supplies supplementary perceptual observations; measurements and retained artifacts support decisions. Neither a long caption nor a clean model execution establishes a correct review.

## Creator's reference observations

Reference is The Mandela Catalogue Vol.333, upload 1jMFrVk2d_I. Full excerpt 23:17?23:52; recent 25-second input 23:27?23:52. These are creator-supplied observations, not parent firsthand perception:

- A man moves through a brick hallway in a black-and-white recording with a lost-footage character.
- Around 23:32, a long-armed/long-legged frightening figure descends or floats from above at the far end. Its head initially seems absent and becomes visible later. Looking down then raising its head is a proposed explanation, not an established mechanism.
- The creature uses changing, borrowed-sounding voices, including older-man-like and child-like qualities. They seem distorted/higher-pitched and worsen over time.
- Creator explicitly clarified: these vocalizations are the creature's unsettling performance, not a normal conversation. The observer's "stop or I'll shoot" is a separate reaction.
- The figure is about to approach when the excerpt ends. Creator also thinks the camera is very shaky.

Working interpretation of the desired effect: vulnerable human presence, uncanny body motion, recognizable-but-wrong voices, escalating unease and an abrupt unresolved ending. This is an inference from the reference explanation, not a finalized shot specification. Use original production choices to pursue the effect; reference files remain analysis-only.

## Generated-audio review workflow

Claude/Codex should give Qwen the ACTUAL generated samples plus a concise intended-audio brief. State desired qualities and allowed stylization; do not predetermine whether the candidate is good or bad. An intentionally distorted creature voice, hiss or rough recording may be desirable. Judge fitness to the intended scene, not generic studio cleanliness.

Ask Qwen to report:

- **Audible observations:** distinct elements, voice/vocal qualities, background texture, short sounds, sequence, overlaps, dynamics and AV timing where supportable. Quote speech only when intelligible; separate acoustic qualities from guessed sources.
- **Fit to the brief:** what is present, missing, too strong, too weak, repetitive, discontinuous, overly clean, unintentionally distorted or masking something else. Distinguish intended stylization from suspected defects.
- **Suggested changes:** connect each recommendation to a specific reported observation and the intended result. Examples include reducing transient prominence, preserving an ambience bed, changing vocal texture, or adjusting an onset. Claude/Codex maps those proposals to supported prompt/workflow/edit/post controls; numerical settings require measurement or a declared experiment.
- **Uncertainty:** identify low-confidence claims, possible visual influence, unclear words and anything the supplied window/sampling cannot resolve. Avoid invented dialogue, identities, conversational purpose or unsupported narrative.

Claude/Codex pairs the report with measurable checks such as silence/level, clipping, stereo balance, discontinuities and alignment. Keep raw Qwen output, exact prompt, media/model/config identities, resource measurements and cleanup evidence. Make a focused change when authorized and compare the next candidate against the same brief. Qwen's advice does not itself approve a production version.

## What the current setup has demonstrated

The local analyzer accepts actual embedded audio plus sampled frames and an exact custom prompt. Latest tested input was 25 seconds, 20 frames at 0.8 fps, continuous 16 kHz mono audio, 500 output-token cap, seed 0/greedy. Resource guards remain unchanged and every new run needs fresh admission checks; 25 seconds is a measured configuration, not a universal capacity promise. Native stereo source is retained separately.

Current Qwen captions are semantically unstable. On identical footage it alternated between distorted whisper/static, clear undistorted speech and invented conversational structure. It also supplied an unsupported closed-door ending and disagreed about subjects/motion. More explicit detail plus a 500 cap produced 450 tokens at EOS, not better-established accuracy. Preserve creator annotations separately and do not let model guesses silently replace them. This is a limitation to calibrate when using Qwen as a reviewer, not a reason to claim the audio was heard by Claude/Codex.

Rapid camera shake cannot be settled from one sampled frame every 1.25 seconds. Fine dialogue, voice-identity/age claims and sound causes are not accepted facts. Human feedback can correct the working description; model agreement is not independent corroboration.

Available adapter: `python -B -m orchestrator.adapters.qwen analyze|review|inspect`, documented in `orchestrator/adapters/qwen/README.md`. Generated-audio review requires an intentbrief; defaultwindows are25seconds with2seconds minimumoverlap and500outputtokens. The adapter retains rawQwen reports and separatephysicalsignalmetrics, timestamps, EOS/cap/coverage anderror/cleanup evidence. Realreferenceanalysis and a generatedcandidate review completed; seepackageVALIDATION.md. Automaticgeneration/editapplication or productionapproval is notimplemented by thishelper. The candidate's verylowmeasuredlevel versus Qwen's optimisticfit report remains a reliabilitycaution.
