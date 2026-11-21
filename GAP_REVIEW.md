# Shotwright Gap Review

**Date:** 2026-11-21 · **Code:** `main` @ `df57bbd` · **Spec:** `CCTV_Analog_Horror_Production_Pipeline_PRD_v1.2.md`

The code is healthy: the full suite passes (**2910 passed, 0 failed**; the 70 skips are
parameter combinations that do not apply, plus one symlink test this machine cannot run),
and the core invariants hold almost everywhere. The gaps are in what is not connected yet.
The pipeline can render and degrade single shots, but nothing can approve them, cut them
into an episode or export the result, and a handful of latent bugs sit on exactly that
path. Phase 1 is accepted; Phase 2 is blocked.

**Verified** marks findings the reviewer checked in the code. **Reported** marks findings
taken from an agent's static read and not independently re-checked.

## Fix order

1. **Make approvals trustworthy** *(in progress)*: scope reviews to an episode and a
   target type, revoke a cut approval when the shot list changes, version the Timeline,
   and stop location revisions from missing episodes. See [Gap 2](#gap-2-latent-bugs-on-the-approval-path).
2. **Expose the finishing steps**: operations to create reviews, approve framing, shots
   and cuts, assemble a cut, resume interrupted invalidations, and read back outputs so
   an agent can see its work.
3. **Stop silent drops**: composite on-playback overlays into exports or refuse them
   loudly, and apply or reject the camera fields degradation currently ignores.
4. **Housekeeping**: free disk space, put the handoff log under version control, retire
   the stale PRD copy and update the drifted contracts.
5. **Phase 2 build-out**: ambience, effects and a final mix; the `doctor` preflight;
   references and the style bible.

## Gap 1: an episode cannot be finished

Shots stop at `review`. The functions that move them on exist and are tested, but nothing
outside their own module calls them.

| Severity | Finding | Where | Basis |
|---|---|---|---|
| **Blocker** | **No approval can be recorded.** `approve_shot`, `approve_framing`, `approve_episode` and `approve_style_bible` have no API, CLI or MCP operation, and production code never creates the `Review` every approval needs. There is no review UI either (FR-10). | `service/approval.py` | Verified |
| **Blocker** | **No cut can be assembled, so export never succeeds.** `compose_timeline` and `assemble_episode` are library-only. `export` is wired but needs an approved cut nothing can create. Blocks AT-02, AT-09, AT-10, AT-12 and the Phase 2 exit. | `service/timeline.py`, `adapters/export/package.py` | Verified |
| High | **Recovery and profile revision are library-only.** `resume_invalidations` (needed after an interrupted cascade) and `revise_camera_profile` have no operation. General job cancellation is exposed only for H3. | `service/dependencies.py`, `service/runner.py` | Verified |
| High | **The agent cannot see what it makes.** The only image-returning tool shows provider thumbnails. No tool returns a preview frame, master, degraded clip, audio take or cut. The Qwen review helper needs a shell. | `api/mcp.py` | Reported |

## Gap 2: latent bugs on the approval path

These do not bite today because approvals are unreachable. They will as soon as step 2
exposes them, which is why they come first. An independent Codex audit of the eight core
invariants found them; each HIGH below was confirmed in the code.

| Severity | Finding | Where | Basis |
|---|---|---|---|
| High | **A review can approve the wrong episode's shot.** `Review` stores only `target_id` and `target_version`. Shot ids are episode-scoped, so a review for S1 in episode 1 also approves S1 in episode 2, and ids can collide across shots, cuts and style bibles. | `domain/entities.py` (`Review`), `service/approval.py` | Verified |
| High | **Adding a shot keeps the cut approval.** `propose_shots` rewrites `Episode.shot_ids` but keeps `approved_cut_version` and `approval_review_id`; export does not recheck shot states. | `service/dependencies.py`, `adapters/export/package.py` | Verified |
| High | **Each new cut overwrites the last edit.** The Timeline is stored unversioned at `edits/timeline.json`. Assembling cut B replaces the edit behind cut A, whose media and hashes survive without their source. | `store/json_store.py` (layout), `service/timeline.py` | Verified |
| High | **Location revisions trust the caller's episode list.** `revise_location` only checks the episodes passed in `episode_ids`. Omit one and its approved shots and cut approval survive a change to a shared room. | `service/dependencies.py` | Verified |
| High | **Most post-only camera settings do nothing.** Only fps, resolution, aspect ratio, frame drops and codec reach degradation. Bitrate, noise model, chroma smearing, block artifacts, sharpening, white balance, overlay model and analog artifacts are accepted and ignored. | `service/runner.py` (`postprofile_spec_from_camera`) | Verified |
| High | **The on-screen clock silently disappears.** Overlays marked `on_playback` are skipped during degradation, and the delivery adapter that would composite them has no caller. Exports lose the clock without an error; the PRD requires a loud refusal. | `adapters/postprofile/profile.py`, `adapters/export/runner.py` | Verified |
| High | **IndexTTS-2 checkpoints are not hashed.** The runtime check confirms the checkpoint files exist and hashes the config, but not the weights, so a swapped checkpoint keeps the recorded identity. | `adapters/tts/indextts2.py` (`_verify_runtime`) | Verified |
| Medium | Render and degrade operations take no `expected_version`, so a stale caller can act on newer state. | `api/routes.py`, `service/runner.py` | Reported |
| Medium | A shot past `building` cannot get a post-only change without re-rendering. | `service/runner.py`, `service/compose.py` | Reported |
| Medium | `camera_mount` triggers a rebuild but the Blender worker never applies it. | `adapters/blender/scene.py`, `worker.py` | Reported |
| Medium | A worker that raises `SystemExit` is recorded as completed. | `jobs/queue.py` | Reported |
| Medium | Failed and cancelled audio takes are deleted instead of quarantined (FR-27). | `service/take_files.py`, `adapters/tts/core.py` | Reported |

## Gap 3: missing capabilities

| Severity | Finding | Basis |
|---|---|---|
| High | **Audio stops at dialogue.** Voice synthesis, voice degradation and performance edits exist. There is nothing for ambience, effects, room tone or music, and no mix: the cut assembler refuses `Timeline.audio_tracks` ("planned for Phase 2"). FR-08's loudness and sync targets cannot be met yet. | Verified |
| High | **No `doctor` preflight.** FR-24 is a Phase 0 P0 item and does not exist; `health` only answers "ok". | Verified |
| Medium | References and the style bible (FR-17, FR-18): data model only; `sample_references` answers 501. | Reported |
| Medium | Storyboard and framing compare loop (FR-29): fields exist, no workflow. | Reported |
| Medium | Rubric gate (FR-22): no deterministic frame sampler; Phase 1 was scored by hand. | Reported |
| Medium | Production routing (FR-05): every shot is hard-coded to Blender. | Reported |
| Medium | Typed feedback (FR-11): `apply_typed_feedback` answers 501. | Reported |
| Medium | Settings API (FR-23), retention and cleanup (FR-26), migrations beyond v1 (FR-28). | Reported |
| Medium | Golden post check (AT-14) and byte-identical determinism (AT-05) rest on one spike measurement; unit tests use a fake encoder. | Reported |

## Gap 4: nothing is publishable yet

- **Restricted assets** *(reported)*: the TRELLIS figure and the overlay typeface are
  licensed for internal evaluation only.
- **No credits surface** *(reported)*: credits are collected but never shown on screen,
  which CC-BY Sketchfab assets require.
- **No overlay font in new projects** *(verified)*: the default `dvr-2005` profile needs
  `assets/fonts/overlay.ttf`; projects are created with an empty folder, so the first
  degrade fails until someone supplies one.

## Gap 5: unmeasured and never run

- FLUX.2 klein has 19 s of margin under its stall limit; a first boot after reboot
  measured 99 s, so the first FLUX job after a reboot could be stopped.
- Z-Image + RealGen does not fit when other programs hold the 2.7 GB high mark of GPU memory.
- Never run: ComfyUI cancellation, out-of-memory recovery, low-VRAM modes, host-RAM
  admission, power loss, disk exhaustion, cross-process writers.
- The symlink containment test is skipped on this machine.
- Several settings and scene constants are still marked as unmeasured placeholders.

## Gap 6: housekeeping

| Severity | Finding | Basis |
|---|---|---|
| High | **Both drives are nearly full.** C: is 96% full (17 GB free) and D: 95% (31 GB free), close to the queue's 10 GB minimum. `_work` holds 3.6 GB of disposable output. | Verified |
| High | **The project log is not in version control.** `HANDOFF.md` (1,641 lines) and `CLAUDE_HANDOFF.md` are gitignored and exist only on this disk. `PRD.md` is a stale, gitignored copy of the v1.2 PRD. | Verified |
| Medium | **Documents have drifted from the code.** `api/CONTRACT.md`, `service/ENTRY_PATH_CONTRACT.md` and `export/CONTRACT.md` still say export is unimplemented. PRD §8.4 still lists ComfyUI 0.21.1, the old torch nightly and `controlnet_aux`, and its header says Phase 0 is outstanding. `AGENTS.md` says five spikes (there are 29) and points to a sibling IndexTTS checkout that no longer exists; the README says 23 spikes. | Verified |
| Low | **Branches, CI and uncommitted work.** 53 branches, 32 not merged into `main`, and two Codex worktrees outside the repo. No CI, so the 17-minute suite runs only when someone runs it. The tool-description branch and four spike notes are uncommitted. | Verified |

## Functional requirements

Status from a static read of the code against PRD v1.2. "Library only" means the code
exists but no operation reaches it.

| FR | Priority | Status | What is missing |
|---|---|---|---|
| FR-01 | P0 | Partial | The agent writes the script; there is no brief-to-script step. |
| FR-02 | P0 | Done | Shot proposal, revision and derivation from the script. |
| FR-03 | P0 | Partial | Sourcing works; the procedural fallback is unverified. |
| FR-04 | P0 | Done | Scene build, preview and master render. |
| FR-05 | P0 | Partial | Routing is hard-coded to Blender. |
| FR-06 | P0 | Done | Stills via ComfyUI; Z-Image + RealGen by default. |
| FR-06b | P1 | Partial | H3 is opt-in; Wan 2.2 deferred by decision. |
| FR-07 | P0 | Partial | No profile revision operation; most post-only fields ignored. |
| FR-08 | P0 | Partial | Voice only; no ambience, effects or mix. |
| FR-09 | P0 | Library only | Cut assembly has no operation. |
| FR-10 | P0 | **Missing** | The review UI. |
| FR-11 | P0 | Partial | Script and location invalidation work; typed feedback is 501. |
| FR-11b | P1 | Missing | Deferred to Phase 3. |
| FR-12 | P0 | Partial | Lease and queue guards exist; lower-memory retry unverified. |
| FR-13 | P0 | Partial | Export is wired but unreachable. |
| FR-14 | P0 | Partial | HTTP, CLI and MCP agree; approval, timeline, reference and doctor operations are absent. |
| FR-15 | P0 | Done | Shared, versioned locations (AT-18). |
| FR-15b | P1 | Missing | Cross-episode world library, Phase 3. |
| FR-16 | P0 | Partial | Licences and consent recorded; no credits surface. |
| FR-17 | P0 | Partial | Reference schema only; no ingestion. |
| FR-18 | P0 | **Missing** | Reference sampling and the style bible. |
| FR-19 | P0 | Done | Script entity and authoring operations. |
| FR-20 | P0 | Partial | Anomaly plans consumed; full vocabulary unverified. |
| FR-21 | P0 | Partial | Seeded degradation works; on-playback overlays dropped. |
| FR-22 | P0 | Partial | Scoring only; no sampler, gate unreachable. |
| FR-23 | P0 | Partial | Settings validated at startup; no settings API. |
| FR-24 | P0 | **Missing** | The `doctor` preflight. |
| FR-25 | P0 | Done | Containment, loopback token, security tests. |
| FR-26 | P0 | Partial | Budgets enforced; no retention or cleanup. |
| FR-27 | P0 | Done | Closed error taxonomy; audio failures deleted, not quarantined. |
| FR-28 | P0 | Partial | Only the legacy v1 migration exists. |
| FR-29 | P0 | Partial | No storyboard or framing compare loop. |
| FR-30 | P2 | Partial | Motion plans tested; refusal rules unverified. |
| FR-31 | P1 | Missing | Text and noise cards. |
| FR-32 | P0 | Done | Audio post profiles and performance edits. |

## Acceptance tests

| AT | Status | Note |
|---|---|---|
| AT-01 | Partial | No supplied media: one shot, by hand. |
| AT-02 | **Not met** | No 60–180 s episode exists. |
| AT-03 | Partial | Authenticity scored by hand, 83.3%. |
| AT-04 | Partial | Revision works; typed feedback is 501. |
| AT-05 | Partial | Determinism rests on spike 01. |
| AT-06 | Partial | Provenance tested; no episode audit. |
| AT-07 | Partial | Recovery tested; no real force-stop run. |
| AT-08 | Partial | Resource control tested with fakes. |
| AT-09 | Partial | Package tests pass; unreachable end to end. |
| AT-10 | **Not met** | No UI; approvals absent from every surface. |
| AT-11 | **Not met** | No reference ingestion. |
| AT-12 | Partial | Approval logic tested, not reachable. |
| AT-13 | Partial | Lease tested; named-holder case unverified. |
| AT-14 | **Not met** | No committed golden clip. |
| AT-15 | Partial | Render budget measured once. |
| AT-16 | **Not met** | No preflight. |
| AT-17 | Met | Security suite, automated. |
| AT-18 | Partial | Shared location; GPU run manual. |
| AT-19 | Partial | Budget ceiling; no degradation report. |
| AT-20 | **Not met** | No storyboard. |
| AT-21 | **Not met** | No reference tier containment. |
| AT-22 | Partial | Motion plan tests only (P2). |
| AT-23 | **Not met** | No 2D surfaces (P1). |
| AT-24 | Partial | Audio post tested; F0 check unverified. |

## How this was done

Four Claude agents read the PRD and code in parallel (requirements FR-01–16,
requirements FR-17+ with acceptance tests and phases, the end-to-end workflow through
MCP, and open items with documentation drift). Codex (`gpt-6.1-sol`, high effort)
audited the eight core invariants independently. The full test suite was run locally.
The reviewer checked every finding marked Verified in the code.

Not covered: GPU behaviour beyond earlier measurements, byte-identical encodes, a live
MCP client session, and the truth of licence or consent attestations.
