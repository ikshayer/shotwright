# Provider sourcing continuation - handoff state

Updated 2026-11-07 at creator request to transfer sessions. Read orchestrator/adapters/assets/CONTRACT.md and all amendments for binding details. This report is intentionally explicit about unfinished verification.

## Implemented

- Existing search_assets GET now has typed query fields and real Poly Haven/Sketchfab search.
- New acquire_asset POST uses required expected_version and existing register_asset service. No duplicate local upload operation.
- Poly Haven glTF+resources normalized into self-contained GLB; image maps and actual HDR/EXR accepted with decoder dimension caps.
- Sketchfab fresh catalogue-based CC0/CC-BY licence checks; direct GLB preferred, safe glTF ZIP fallback; key read only at runtime.
- Bounded HTTPS/manual redirects, credential stripping, classified bounded retry policy, checked content-addressed source cache, source hashes and immutable manifests before registry commitment.
- Minimal escaped HTML asset_browser with visible provider and saved-asset credit; CLI/manifest remain driven by OPERATIONS.
- Direct GLB chunk/BIN/bufferView validation was the last source fix after independent review.

## Actual verification

Parent independently ran a combined focused suite: **103 passed, 1 warning in 35.46 s**. This includes the last three direct-GLB negative cases. Ruff across orchestrator/, tests/, and this spike passed; git diff --check passed (only existing Windows LF/CRLF advisory).

The older full suite finished: **1651 passed, 40 skipped, 6 warnings in 382.04 s**. It started before the final direct-GLB guard/new three tests. The later focused 103-test run covers that final guard. Rerun the full suite against the frozen final source as the clean final gate; no test process is still running. Previous fully verified pre-provider baseline was 1575 passed, 36 skipped, 6 warnings (spike17).

Live completed run: `spikes/18_asset_sourcing/_work/20261107T224036_9c46656d`. Real CLI searched both providers and acquired/registered all four assets below. Safe steps and summary.json retain statuses, hashes, rights and timings. The saved asset-browser.html contains visible credit. No GPU render was run in this provider continuation.

| Asset ID | Provider | Type | Licence | SHA256 |
|---|---|---|---|---|
| ph_wooden_table_02 | polyhaven | model | CC0-1.0 | b34bcb468265352371b0b031bf66872732ac34835b92d6fdea059719edd79cd0 |
| ph_brown_planks_03 | polyhaven | texture | CC0-1.0 | 8dfae22286fa9bb0ed62ce7326e8bc14c284e567d38fd64b5a453838080ecf19 |
| ph_studio_small_09 | polyhaven | hdri | CC0-1.0 | e7cfda5f4e98e623db12b8bfd0184e048488e4855d9c83e2751fb44a32e80c45 |
| sf_selected_model | sketchfab | model | CC-BY-4.0 | e8ba53a6cc465669ce79a869f11a2dd256a6e50fd979b190f79613bf1fa140e6 |

The first live run `_work/20261107T223812_03af945a` successfully acquired the table but the smoke verifier rejected the benign normalized_size manifest field. The verifier was corrected and a fresh full live run completed. Preserve this distinction: it was a verifier mismatch, not a failed provider registration.

After the final validator patch, parent applied validate_glb to both real downloaded models successfully. Their hashes match the table above. Parent also scanned all49 artifacts in the successful live run for the actual credential and signed-URL markers: none were present. No secret value was printed.

## Creator authorisation

Automatic approval initially rejected a proposed key read because it lacked explicit permission. Creator then answered: **Use the existing key for the live test** to the question identifying D:/Work/blender_refs/sketchfab_key.txt and sending it to api.sketchfab.com. Authenticated descriptor and one35KB model download then succeeded through the new adapter. Do not read/echo/log/commit the key. Runtime configured path is Settings.asset_providers.sketchfab_token_file; alternative existing credentials mapping contains an environment variable NAME only.

## Known review decisions

Independent reviewer found and implementers fixed: HTTPError exception binding, SF licence UID host parsing, PH author-key/texture-tree mistakes, retries, image decoding caps, aggregate/disk/cache checks, manifest commit ordering, malformed glTF/ZIP cases, buffer/image bounds and GLB direct-download framing. See contract amendments and tests, not chat summaries, for exact behavior.

Store.command refuses nesting. Store._lock is the same class-shared per-resolved-root RLock; sourcing holds it across final duplicate check+manifest and existing register_asset (which itself enters command). Parent verified this source fact after initially giving incorrect nested-command advice.

## Remaining work for next session

1. Rerun full pytest on frozen source with contained TMP/TEMP/PYTEST_DEBUG_TEMPROOT; rerun Ruff/diff check. Current focused 103 is green; the earlier full snapshot finished 1651 passed, 40 skipped. No test process remains active.
2. Independently inspect actual Blender import of the two downloaded GLBs if desired for the final round-trip proof. It has NOT happened yet. No need to repeat spike17's full15-second render.
3. Review remaining implementation against contract and live evidence. Final direct-GLB fix was tested but not re-reviewed by the read-only reviewer afterward.
4. Finish/report BlenderMCP comparison using BLENDERMCP_COMPARISON.md. Better live material/HDRI/preview convenience there; stronger controlled acquisition here. No porting started.
5. Move disposable root .test-tmp/ under _work after tests; test author confirmed it contains only pytest data. Preserve source/tests and previous spike17 work.
6. Update this report/HANDOFF with final full-suite result and any new findings. No commit/push/merge unless creator requests it. Main is untouched.

Publication/export attribution rendering, material application, HDRI lighting hookup, image previews/categories, and full room dressing are not complete. Phase1 remains unaccepted and prior visual ratification is pending. Search/download/registration must not be described as complete scene authoring.

## Final verification closure during PBR continuation (2026-11-07)

Parent ran fresh full pytest against frozen final Python sources (including the direct-GLB framing guard and subsequent PBR integration): **1669 passed,40 skipped,6 warnings in496.86s**, exit0; before/after source hashes matched. This supersedes the older loaded-source snapshot caveat above. Exact logs/hashes/result: ../19_pbr_materials/_work/final-verification/.

Both retained provider GLBs now passed actual Blender5.2.0 factory-startup/disabled-autoexec import, independently repeated by parent. Poly Haven table:1 mesh and3 image links; Sketchfab selected model:1 mesh,0 image links from its untextured source. Mesh bounds and logs: ../19_pbr_materials/_work/20261107T234739_770185d3/. No new downloads/credentials/GPU render were used. This is raw import proof, not provider-shot visual acceptance. The PBR increment applies registered image sets to named room materials; HDRI lighting and thumbnails are still unimplemented. See ../19_pbr_materials/FINDINGS.md for current exact scope. Previous outputs were preserved and no commit/push/merge occurred.
