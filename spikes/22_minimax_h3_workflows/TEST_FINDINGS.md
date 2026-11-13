# H3 video-and-text ambience test preparation

2026-11-09. Creator authorized looking into testing H3 generation from our existing silent video and text. Offline preparation and verification are complete. No H3 task was submitted: no known MiniMax/Comfy API credential is configured, and the available browser initially had no signed-in session. An asynchronous question about Comfy Cloud, MiniMax API or separate local licensing remains unanswered. Do not interpret elapsed time as account access or approval.

## Ready input

Run: `_work/test_inputs/20261109T165721Z_e1e1bf74/` (all manifest media paths are relative to the worktree root).

- `input_silent.mp4`: actual H264/yuv420p,1024x768,SAR1:1,24/1fps,192frames,8.000seconds,zero audio streams,140832bytes.
- SHA256: `a27d7f2a4d56f144f79c4efbb1740ff59a68e1610b9010f5dc764cb7aa3d0a2c`.
- `prompt.txt`: preserve the fixed empty room and generate natural ventilation/room tone; no speech/music/footsteps/impacts. Hum is a creative sound instruction; no visible fan was identified.
- `request_template.json`: explicit MiniMax-H3,768P,8seconds,4:3; text plus reference video only. Media URL is deliberately a placeholder. No audio reference, Digital Human lock, invented seed or API key.
- `manifest.json`, source credits JSON/text, independent and parent verification reports, contact sheet and parent_review.json retain identities, transformations and pending states.

Source is the exact spike20 master specified in TEST_CONTRACT.md, SHA2569dbb789b260bfde899a549eaf18cfc20c4d8feed050f59edfeb2f25848f61389. Select [0,8), before the internal-only figure becomes visible at8s; the copied review-overlay font is absent from this clean master. Parent inspected samples0,48,96,144,191: empty room, no figure/timestamp. This is sampled agent inspection, not exhaustive scene or human approval. The source/accepted delivery and approval records were not mutated.

## Verification and process limits

Separate authors prepared media and wrote the independent verifier from TEST_CONTRACT.md. Parent independently reran verify_test_package.py against actual files: technical_preparation_pass=true, failed_checks=[] and exit0. All source/artifact/code hashes, actual input frame count/duration/streams, full H264 decode, PNG CRC/decompression and exact request/prompt passed. Ruff --no-cache passed for both new scripts. No production pytest run is claimed for this isolated spike.

The source's historical900-frame count is bound to the frozen master hash/receipt; it was not needlessly recounted. Early full-source count probes were stopped and their three empty initialized directories remain. A later failed preparation retains video and logs, including a contact-sheet -n/-y argument conflict. Successful final run uses explicit container formats for atomic staging paths and distinct source/input probe logs. The first verifier report had an overconstraint requiring no_input_audio at top level; the contract also permits its provenance location. The author corrected only the verifier. That original report was overwritten; a distinct reconstruction is explicitly labelled as such, not original failure bytes. Final independent and parent reports are actual successful runs. No FFmpeg/FFprobe processes remained after completion.

## Rights, route and next actual test

Preserve Chair by Arline Tjandra under CC BY4.0 with source/licence/modification notice. Table/maps, floor and HDRI are recorded CC0. Full credit sidecars accompany this input. This does not establish provider account entitlement, H3-specific hosted rights or publication clearance. Existing immutable ComfyUI0.21.1 remains unsuitable; local H3 licence and memory fit are unresolved, so no local installation or weight downloads occurred.

The direct API's listed main-H3 768P input8s + output8s estimate is USD1.28, not Cloud subscription pricing. Once route/access and applicable terms are established, submit one bounded reference-to-video task with this silent video + prompt. Retain returned AV before extracting native audio and a separate48kHz editing WAV. Measure actual durations/channels/sample rates, raw sound, geometry/camera changes and timing; inspect output before applying CCTV processing. Neither generated sound nor realism has been tested yet.

Browser rendering made the previously unreadable [MiniMax API terms](https://platform.minimax.io/protocol/terms-of-service) and [privacy policy](https://platform.minimax.io/protocol/privacy-policy) accessible. General terms permit using input/output for service development/improvement and require lawful input rights. No zero-retention promise or H3-specific worldwide entitlement was established. This supersedes only the prior statement that the pages could not be read; provider-specific clearance remains open. Comfy Cloud's [official documentation](https://docs.comfy.org/get_started/cloud) says custom node packs are restricted to its maintained set; use a supported native H3 R2V graph there rather than assuming Easy can be installed.
