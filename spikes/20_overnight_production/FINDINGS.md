# Overnight production follow-through

2026-11-08. Creator authorized all five proposed next steps, then resumed after an agent usage-limit interruption. Work remains in api-conformance/prog-combined; no commit/push/merge or publication. Main tracked contents unchanged. Contract.md with Amendments1-6 is binding. Implementation and final regression/media verification are complete. Creator visual acceptance and publication rights remain pending.

## Scope implemented

- Matched real Eevee material before/after proof through CLI, job queue, lease, journal and degradation; sources unchanged during comparison. Registered PBR floor maps change pixels. It remains an authored synthetic set, not a certified photoreal result.
- Discovery: bounded PH taxonomy296 categories / Sketchfab18 categories, keyword search with pre-pagination category filtering, exact stable category IDs, safe public thumbnail selection and decoded image caps. Local binary asset_preview endpoint and HTML browser image cards carry visible escaped provider/creator/licence credits; CLI/manifest use the same typed operations.
- Placement: default exact-axis fit unchanged; opt-in contain uniformly scales within declared envelope under one shared parent. Worker metadata reports authored bounds and actual placed world bounds. Locations retain declared dimensions and rotation; a rotated world AABB is not the unrotated envelope.
- Lighting: typed EnvironmentBinding on Location/SceneSpec, registered HDR/EXR identities across build/persist/reopen/adapter verification, measured ffprobe dimensions before GPU, new scene-owned World with explicit strength/rotation and packed linear image. Explicit environment strength overrides fixture world_strength. Workbench remains geometry-only.
- Script: structured Location blueprints plus Beat.scene_direction drive actual append-only Locations/Shots through derive_script_scene_plan API/CLI. Beat.target_sec alone determines duration; event_sec is relative. This is agent-mediated structured authoring, not a language model that interprets arbitrary prose. Tokens/keysets/creator fields/assets/full master-plan geometry are checked before writes; exact active reruns are no-ops. Shared-location and durable-fence revisions produce renderable successors. Partial IO commits are journaled and reported explicitly, not represented as atomic rollback.
- Credits: exact Shot-version/SCENE receipt and actual-used asset identities feed a snapshot; unused/reference assets are excluded. PH CC0 app credit is distinct from published-video duty. CC-BY attribution appears on bounded visible appended video cards with complete JSON/text sidecars; receipt binds manifest path/hash. Unknown/internal rights remain publication blockers. Narrative timing is unchanged by appended card duration. A proposed episode does not become creator-approved by this feature.

## Actual evidence already observed by parent

Matched run: `_work/20261108T001855_03bdf358`. Six-frame masters each1280x960/60fps, FFV1; delivered0.1s samples352x288/10fps/SAR12:11. `matched_material_review.png` inspected by parent: texture difference visible, room still synthetic/dark. Initial baseline degradation failed only because the new project lacked its local overlay font; source and failed job retained, master reused after fixture correction. All matched source hashes unchanged. No zero watchdog workaround.

Public live requests required normal network escalation after sandbox network refusal; automatic review allowed bounded public no-credential verification. `_work/discovery-live-authorized/`: PH taxonomy296, SFcategories18, SFpreview448x252 JPEG9198bytes, PHpreview256x201 WebP8842bytes, PHcategory filter selected real table assets, HTTPpreview200+nosniff and browser local image URLs/visible credit passed. First PHpreview safely refused an observed legitimate format=webp query; exact allowed-format support was added and a fresh receipt retained. No provider key was read/sent and no model redownload was required.

Independent feature harness: verify_features.py. Agent evidence `_work/feature_probe/`; parent separately reran with fresh output root `_work/feature_probe_parent_20261108T075027_f120b5/` and verified unchanged source hashes. Blender5.2.0 LTS checked HDRI Normal->Mapping->Environment->Background->World nodes, strength1.75, rotation~0.731rad, Non-Color input, packing/detached-source fresh-process reopening. Real table contain-fit and a generated internal multi-root GLB use one transform parent and remain inside target envelope. These direct measurements supplement worker metadata; no GPU renderer was used by this probe.

A real collected CC-BY chair entry passed actual FFmpeg textfile-card encoding at352x288,10fps,50frames/5s. Parent inspected decoded/enlarged PNG: title/creator, licence and complete wrapped URLs are visible without edge clipping. Metrics on agent run: bboxx21..328,y58..229, maxfontline320px within320px budget; full UTF8 sidecar834bytes. Diagnostic did not fake real creator approval or create an approved episode cut. Parent rerun also passed collection/encoding.

## Dressed Script-derived shot (completed)

Fresh project `scripted_03bdf358` under matched run projects/. Beat.target_sec15; event8; linear figure path(.65,1.15,0)->(.8,1.9,0), duration4. Registered table/chair contain-fit, one-map BrownPlanks Diffuse floor, registered StudioSmall09 environment strength.15/rotation0. Review-only camera exposure+.5EV/practical multiplier1.3 are explicit agent proposals, not production defaults. No downloads or new generation.

Initial v1 had table/chair centers atz0, which parent caught as half-buried under the existing center-anchor contract. The harness revised Script->Location->Shot append-only to v2, tablecenterz.26/chaircenterz.375, and rebuilt. Queued Workbench preview confirmed actual minZ approximately0 (floating tolerance), table dimensions.7378x.4592x.52m, chair.5420x.5035x.75m; parent inspected clear footprints. This preview is not PBR verification.

The queued Eevee master completed at `renders/s1/v0002/3859f220afd54b6f9bcee4a0743336c9/attempt-0001`: 900 frames, 1280x960, 60fps, FFV1. Render elapsed 464.344s; master assembly 170.797s; total job 635.141s. Parent inspected first, event-onset (frame481) and 12s (frame721) master samples, then the final nine-sample delivered review sheet. Placement and floor detail are visible. The room still looks synthetic; the figure has low contrast and its lower body merges with foreground/occlusion. These are review observations, not creator acceptance. Prior generated figure rights are explicitly internal-only; the credit collector correctly reports a publication blocker. The copied local Windows overlay font is also limited to internal evaluation without redistribution clearance.

Diagnostic resources reuse spike17's documented local positive floor0.0002487528445856717 and wall3600s. Production defaults unchanged; declared resource demands remain zero, so this is functional evidence, not default-admission/VRAM-requirement/AT15 acceptance. Observed matched-job VRAM numbers are total-device journal observations, not attributable demand or recommended reservations.

## Independent authoring and adjudication

Feature implementation, tests and runtime harnesses have separate authors. Parent independently reruns checks, inspects images and adjudicates contracts; read-only reviewer reported no remaining concrete HIGH after the final master-validation boundary fix. Important found/fixed seams: default ffmpeg Path incorrectly used as ffprobe; safe PHformat query; unsupported unversioned Asset enumeration; actual scene-receipt hash and conflicting binding checks; full path master validation instead of preview-only first point; Script timing duplication and overstrict episode-target equality; stale/fenced successors and cross-episode location cascades; partial IO journals; legacy no-credit encoder call compatibility. Earlier failing tests also exposed stale taxonomy mocks, wrong pure-helper argument count, wrong unversioned store method, and fixture approval/order assumptions. Assertions were corrected to the contract, not blanket-skipped. Legacy timeline fixtures gained real hashed SCENE receipts while retaining original conformance assertions.

One agent usage-limit interruption stopped work overnight; user explicitly resumed. No requested feature was silently marked complete from that interruption. Ownership transfer of credit modules to pbr_core and pure compose helper to authoring implementer is recorded in Contract amendments.

## Final verification gate

Parent independently ran the full suite against frozen source: **1744 passed, 50 skipped, 6 warnings in 517.12s**. Exit code 0; Python source hashes matched before/after and were rechecked against current files at final media verification. Durable evidence: `_work/final-verification-v2/{pytest.log,result.json,source-hashes.json}`. The earlier failed full gate below is retained history and is superseded by this run. Parent focused authoring/credits/timeline run also passed 32 tests in 40.87s.

Final Ruff across orchestrator/, tests/, spikes18/19/20 passed with `--no-cache`. Worktree `git diff --check` passed with Windows line-ending advisories only. Primary checkout tracked contents remain clean. No commit, push or merge occurred.

## Remaining limits to retain

Human review/ratification remains pending. No creator approvals forged and no media published. The final generated figure and local font have internal-evaluation limitations; visible credit does not clear those rights. Script authorship remains agent-authored structured directions; general prose-to-world interpretation is not implemented. FR15 same-blend no-rebuild reuse, export packaging and previous resource-admission caveats are not automatically closed by these improvements. Workbench cannot preview PBR/HDRI appearance. Full-room geometry remains the existing procedural shell plus authored sourced placements.

## First full-suite gate (2026-11-08 morning)
Parent full frozen-source suite finished:1739 passed,5 failed,50 skipped,6 warnings in544.99s; source hashes unchanged. Failures are retained at _work/final-verification/pytest.log and result.json. Exact triage: legacy API adapter test word-matching falsely selected new Script derive as build_scene and new asset thumbnail as render_preview; old browser HTTP fixture lacked taxonomy; one old drop-frame/episode integration fixture lacked its new mandatory SCENE provenance receipt. The independent test author corrected only those selectors/fixtures while retaining all original assertions. The final 1744-pass full gate above supersedes this failed gate. Production visual source remains unchanged, so no master rerender is implied by these test-only corrections.
Parent inspected masterframe481(onset8s) andframe721(12s): head/torso remain visible and fully within frame; lower legs merge with dark foreground/occlusion. Figure styling is still visibly generated and room still synthetic. Final delivery/sample review is now complete as recorded below; creator acceptance and publication rights clearance remain pending.

## Final delivery and post-only repair

All paths below are under `_work/20261108T001855_03bdf358/`:

- Review video: `projects/scripted_03bdf358/post/s1/v0002/4cacb702506344519e0728ef067f41ff/degraded.mp4`.
- Contact sheet: `final_review_sheet.png`; sampled frames and provenance: `samples.json`.
- Delivery/job receipt summary: `final_delivery.json`; parent independent actual-file SHA256, receipt, ffprobe and source-hash checks: `parent_final_verification.json`.
- Matched material proof: `matched_material_review.png`.

The delivered H.264 video is exactly 15.000s, 150 frames at 10fps, 352x288 with SAR12:11. Master SHA256 is `9dbb789b260bfde899a549eaf18cfc20c4d8feed050f59edfeb2f25848f61389`; delivery SHA256 is `9860e9ab850b52fb19d03aa9b73aa5b4330f7bc998ec06318ebacae6ea9a8317`. Both match actual files and exact Shot v2 output receipts.

The first final degrade attempt failed with INVALID_MANIFEST: the harness review CameraProfile had inherited default 640x480/12fps, while the existing low-light PostProfile requires 352x288/10fps. The harness used the existing typed `revise_camera_profile` service to append review profile v2, changing only tagged post-only storage resolution and fps. It verified requires_rebuild=false, no invalidated shots, pure profile validation, and identical master hash before re-running degradation only. Evidence: `final_postonly_profile_repair.json`. No second full master render was required. The failed attempt remains visible in job history; final scene/render/degrade jobs completed and all reported leases are released. Production defaults are unchanged.

Final render-source before/after snapshots match. Full-suite source hashes match current code. Human shot approval, Phase1 acceptance and publication remain pending; the internal-only figure and font restrictions are not cleared by credits. This completes the authorized implementation and internal review deliverables.
