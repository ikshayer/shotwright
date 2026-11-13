# Source comparison: BlenderMCP and our sourcing adapters

Reviewed 2026-11-07 at creator request. Other checkout: D:/Work/blender_mcp/blender-codex-mcp. Read-only static review, independently checked by the parent; no execution, credential access or edits in that checkout. This is a capability/code comparison, not a benchmark.

## Judgment

BlenderMCP has richer interactive Blender scene assembly. Our adapter is better aligned with the production pipeline's controlled asset acquisition. Keep our acquisition/registry boundary and adapt useful Blender-side application ideas downstream.

| Area | BlenderMCP | Current pipeline |
|---|---|---|
| HDRIs | Builds world environment shader nodes immediately | Downloads/validates/registers HDR/EXR; does not apply world lighting |
| PBR textures | Downloads multiple maps, creates materials, assigns by object name | Acquires one requested texture map; no general PBR bundle/material assignment yet |
| Preview | Sketchfab thumbnail returned as MCP image | Minimal browser has source/licence/creator text, without image previews or acquire button |
| Import feedback | Current scene import, world bounds, root-only target-size normalization | Acquisition is outside Blender; existing worker applies declared dimensions at use time |
| Search | Category-filtered PH catalogue and Sketchfab categories | PH real keyword search, typed/bounded pagination; SF approved-licence filtering |
| Rights | Search prints licence labels; download does not independently enforce our CC0/BY policy | Fresh catalogue and model rights checks before acquisition; recorded attribution |
| Portability | Primarily current Blender scene and temporary source files | Project-local copied asset, verified hashes, source cache snapshots and immutable acquisition manifest |
| Download boundaries | Ordinary Requests, response.content, temporary extraction | Bounded reads, host-checked manual redirects, no CDN auth forwarding, caps, ZIP/glTF validation |

## Concrete evidence in BlenderMCP

- addon.py:485 download_polyhaven_asset; HDRI world-node setup around520-578. Parent did NOT find image.pack() in that HDRI branch, unlike the texture branch. Do not claim HDRI packing/durability without runtime verification.
- addon.py:588-718 multi-map material creation, image packing, color-space and shader links; addon.py:808 onward set_texture assigns materials by object name.
- Their first texture builder recognizes normal/nor but not the measured Poly Haven nor_gl key, so feature breadth is not proof every map is wired correctly. Later assignment code has additional aliases. Test exact provider maps before borrowing it.
- addon.py:1602-1678 Sketchfab preview; src/blender_codex_mcp/server.py:800 returns it as an MCP image.
- addon.py:1688 onward Sketchfab download/import. It asks for gltf specifically, downloads response.content, extracts ZIP, imports into current Blender; around1800-1897 computes world bounds and normalizes root objects.
- src/blender_codex_mcp/server.py:842-873 requires target_size; strategy around1249 still asks the agent to place/rotate after inspection. It does not choose semantic room placement automatically.
- addon.py:440-479 categories/catalogue filtering; PH 'search' uses /assets and returns a limited catalogue slice, not a natural-language q query.
- addon.py:1706-1737 requests a download without a fresh CC0/BY eligibility check. Search labels appear at server.py:780-783, which is different from enforcement.
- addon.py:1749-1787 has traversal checks then extractall, no equivalent archive expanded-byte/member/type limits, chooses first root-level glTF. PH includes are joined to temporary paths and include-download errors may just print (around755-768).
- PH download requests generally omit timeouts; source download bodies are buffered without our configured caps. These paths have no project asset registry/hash/version contract comparable to ours.

## Reuse recommendation

First borrow the interaction ideas: thumbnail previews, categories/variant discovery, multi-map PBR bundles, HDRI application, target dimensions and bounding-box feedback. Implement these as typed stored records and Blender worker operations behind our provenance/versioning/dependency rules. Avoid routing production downloads directly through their MCP command, which bypasses our registry and rights checks.

The other checkout's LICENSE is MIT, copyrights Siddharth Ahuja (2025) and Webita/Giorgio Sanna (2026). If substantial source is copied, retain the required notices; asset licences are separate. No source was copied during this comparison. Inspect telemetry before adopting the whole server: provider tools are telemetry-decorated and the add-on has a consent setting.

## Limits

Neither the other add-on's material/HDRI behavior nor its performance was run here. Our provider live checks are recorded separately in FINDINGS.md. Our app credit browser is not publication/export credit rendering; CC-BY episode-credit output remains deferred. No implementation of these additional comparison ideas has been started.

## Takeover recheck and priorities (2026-11-07)

At the creator's follow-up, the parent reread both local implementations and commissioned a fresh independent read-only review. This recheck made no provider requests, read no credentials, ran no Blender process or tests, and changed no production code. The source comparison above remains valid, with these refinements:

1. **PBR material bundles and application are the highest-value next implementation.** Our `service/sourcing.py:146-163` selects one requested image map, and `adapters/blender/scene.py:33` has no registered image-map bindings on MaterialSpec. BlenderMCP builds multi-map materials (`addon.py:595-717`) and applies them (`:808` onward). Extend our existing material/scene records with explicit registered-map bindings, UV scale and map roles, then resolve them through the worker and dependency graph. Preserve imported GLB materials. Prove the seam on a small actual Blender scene before claiming improvement in delivered footage.
2. **Thumbnail previews should support asset selection before acquisition.** Their Sketchfab preview is at `addon.py:1602-1678` and `src/blender_codex_mcp/server.py:800-834`. Our Sketchfab results explicitly set thumbnail_url=None (`assets/providers.py:131`); our HTML browser does not render thumbnails for either provider (`api/app.py:399-433`). Use bounded, host-checked preview retrieval with existing visible source/licence credits. Category filters are useful secondary discovery controls; our genuine Poly Haven keyword search should be retained.
3. **HDRI application is a distinct scene feature.** Our SceneSpec exposes world_strength but no HDRI binding (`scene.py:182-189`); worker worlds use fixed backgrounds (`worker.py:354-394`). Borrow their environment-node idea with explicit registered asset, strength and rotation, preserving camera-profile lighting behavior and reproducible scene records. Their HDRI branch does not pack the loaded image and modifies the first world datablock (`addon.py:520-570`); its durability was not runtime-tested.
4. **Import fitting already exists here.** Our worker measures combined mesh bounds, recenters under one shared parent, and fits each axis to declared dimensions (`worker.py:824-880`). Their uniform longest-axis scaling and returned world bounds (`addon.py:1818-1898`) suggest an optional aspect-preserving fit mode and bounds feedback. Exact declared dimensions are our existing contract: change only through an explicit new mode and contract, not by silently changing current placements or anomaly envelopes.

### Additional source findings worth retaining

- BlenderMCP's normalization changes each root object's scale but leaves root translations unchanged (`addon.py:1844-1855`). Thus the distance between separate roots is not scaled, and the combined model is not guaranteed to meet the promised target size. It returns recomputed dimensions without enforcing that promise. Our shared parent applies one transformation to the whole imported hierarchy.
- The two material paths have different capabilities. The initial material builder does not recognize nor_gl or wire packed ARM channels; set_texture splits map names on underscores (`addon.py:825`), accepts gl/dx aliases later, and separates ARM channels (`:998` onward). Do not treat the first path as having the second path's behavior, or copy their alias handling without actual provider-map fixtures and Blender checks. Failed texture-map HTTP responses can be skipped while the overall material operation reports success (`:604-605`, `:636-717`).

Suggested order for follow-up work: PBR bundles/application, visual previews, then bounds feedback/optional fit mode. Add HDRI controls when a shot needs environment lighting, followed by category discovery. These are recommendations from source inspection, not implemented features or measured visual-quality gains. The provider GLBs' pending real Blender import check remains a separate verification item in FINDINGS.md.

## Implementation follow-up (2026-11-07)
Creator subsequently authorized source reuse. The first recommendation, registered PBR material sets and room-surface application, is now implemented with the source MIT notice preserved in ../../THIRD_PARTY_NOTICES.md. See ../19_pbr_materials/FINDINGS.md and CONTRACT.md for scope, independent authorship, real Blender graph/packing proof, and the final test gate. The remaining recommendations above are still recommendations. No BlenderMCP downloader was adopted.

Final verification for that increment: parent full suite1669 passed/40 skipped, Ruff clean, actual Blender5.2 PBR graph+packed-image reopen and both provider GLB imports checked. No delivered visual-quality claim is made.
