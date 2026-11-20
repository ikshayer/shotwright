# AT-09 portable project export (FR-13, FR-28)

## Operation
Implement the existing POST /projects/{project_id}/export operation and its generated
CLI and MCP entry points. The typed body has episode_id, cut_output_id and strict positive
expected_version (current Episode store token). Use synchronous 200 with bounded local
FFmpeg. This is an export of an already assembled cut, with no Shot, Episode or Timeline
mutation, browser access, GPU lease or model regeneration. Return export_id, package_path,
output_path, manifest_path and receipt_path as project-relative strings.

Bind the exact EpisodeOutput by ID and require its timeline_store_version and
timeline_sha256 to equal the current persisted Timeline. Require Episode gate-C approval
to bind the same timeline version; the Review must exist and match. Check the Episode
token before writes (409 on mismatch) and recheck token, selected receipt and Timeline
before publication. Use a separate source package fixture in tests; no creator approval
is inferred or recorded by export. Missing or mismatched sources are explicit failures.

## Snapshot
orchestrator/adapters/export/snapshot.py implements plan_snapshot(store) returning
an ordered tuple of SnapshotFile(relative_path: str, expected_sha256: str | None).
Use the entire project's native JSON management layout. Include project, all episodes,
ordered timelines, Script/Shot/Location/camera/style bible histories and lifecycle
sidecars, Post/AudioPostProfile, reviews, asset registry, successful/historical
GenerationRun and H3 manifests, and intact Shot/EpisodeOutput ledgers. Preserve native
relative paths and exact manifest bytes. Verify ledger/manifests using store where
available and capture a digest for JSON snapshots under the command fence. Include
declared payloads and checksums, related producer source scripts, native Blender
.blend next to production MASTER and PREVIEW artifacts and AT-18 shared base/receipt,
audio stems named by Shot/Timeline/Asset, post-profile fonts, cut credits and generated
cards, and bound run inputs, outputs and logs as applicable. Historical absolute argv
or plan context is documentary; omit raw runtime-only plans that are not typed artifact
dependencies. Legacy outputs without GenerationRun must still retain their scene.blend
and worker.py when they are native Blender masters.

Never copy SQLite, settings/secrets, jobs/state, caches, nested exports, arbitrary
unregistered files, private reference media, retrieved frames, or their aliases.
Only style bible manifest JSON may be included under references/. Skip private
framing-panel media and reference-only optional provenance paths, and fail explicitly
if a required artifact would violate exclusions. Reject path aliases and escapes.
Do not follow cloud URLs. Fail on bad/missing required artifacts. Administrative
journal records and runtime environments are outside the editable package.
Keep source manifests exact, including their historical schema and approval metadata.

## Packaging and delivered media
orchestrator/adapters/export/package.py implements
export_project(store, settings, *, episode_id, cut_output_id, expected_version) -> dict.
Export to exports/<id>/project/ (a ProjectRoot), plus sibling manifest.json and
export.json (native Export record). Stage privately on the same volume and publish
atomically to an unused generated ID after all checks. Every write, temp path and
rename passes ProjectRoot. Do not overwrite existing exports or mutate authoritative
source records. Stream files and verify known hashes; include SHA-256 and sizes
of every copied and generated project file in the outer manifest. Verify reopened
Project, Timeline and matching ledger receipt from package. Record approval and
source cut identities, source project checksum, FFmpeg identity, resolution, fps,
stream codecs and validation status. Existing rights/consent metadata stays intact.

Create project/delivery.mp4 from the copied cut with local FFmpeg and FFprobe only.
Preserve applicable H.264/AAC streams, or encode them when needed. If the cut
has no audio and no intended timeline audio tracks, add a duration-matched silent
AAC stream and explicitly record this in the receipt. If its timeline names audio
tracks but the cut lacks audio, fail with explicit remediation. Package all stems
even when already mixed. Real FFprobe must validate H.264 and AAC, resolution,
frame rate, and positive duration before publication. Use bounded subprocess
timeouts and no shell/network. Existing delivery adapter can be reused where
it preserves the source's cut, AR, timing and audio.

## Schema
Register the native Export layout exports/<id>/export.json and validate output_path.
Introduce schema 2 with explicit v1-to-v2 load migration of project-native entity
manifests, including ledger rows; v1 is the repository's actual predecessor.
Apply migration in memory so historical/approved bytes remain untouched; normal
writes produce v2. Leave independent Blender plan/metadata schema versions alone.
Keep the Timeline digest canonicalization compatible with existing schema-1
receipts; a migration alone cannot invalidate a cut hash. Preserve mutation and
predecessor tokens. Refuse unknown older and newer schemas explicitly.

## Verification
Independent tests cover the normal API and CLI route, a relocated package opened
with fresh ProjectStore with no SQLite or source dependency, historical approval/
receipt preservation, migration and old Timeline digest stability, reference media
and SQLite exclusions, missing/tampered files, version conflict and atomic failure,
plus a real FFmpeg H.264/AAC receipt and re-probe after relocation.
