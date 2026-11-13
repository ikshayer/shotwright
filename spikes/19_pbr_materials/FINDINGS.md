# PBR material application from registered assets

Updated 2026-11-07. Creator authorized reuse of BlenderMCP code after the source comparison. This increment implements the first recommendation: PBR texture sets on existing room materials. It does not implement thumbnails, HDRI world bindings, category discovery or a new fit mode.

## Delivered behavior

`PBRTextureSet` holds explicit registered map IDs for base_color (required), normal_gl, roughness, metallic and arm, plus UV scale, normal strength and optional AO strength. `Location.material_overrides` binds those sets to existing named room materials through existing create_location/revise_location HTTP and CLI operations. The field is rebuild-triggering: dependent approved shots become stale and artifacts remain. No source downloader or asset registry was duplicated; acquire individual maps with existing acquire_asset and group their registered IDs in the Location.

Example Location field (IDs refer to registered texture assets):

```json
{
  "material_overrides": {
    "Oak planks": {
      "maps": {
        "base_color": "floor_diffuse",
        "normal_gl": "floor_normal",
        "arm": "floor_arm"
      },
      "uv_scale": [2.0, 3.0],
      "normal_strength": 0.65,
      "ao_strength": 0.0
    }
  }
}
```

Textures replace the target material's procedural surface nodes, retaining scalar defaults/emission where appropriate and leaving imported GLB materials intact. Dedicated roughness/metallic maps take precedence over ARM G/B; ARM R affects base color only if AO is explicitly enabled. Each role loads independently, uses sRGB only for base color and Non-Color for data, and packs its image into retained Blender files. OpenGL normals use a tangent Normal Map node. There is no displacement/alpha/DirectX-normal support in this increment.

## Binding and validation

The existing AssetBinding path now carries map identities through composition, scene persistence, standalone ProjectStore reopen, adapter verification and authorized worker paths. `ProjectStore.verify_asset(..., usage='texture')` explicitly opts into texture use; the default remains GLB-only geometry verification. Texture inputs require type=texture and PNG/JPEG/WebP. Reference assets and HDRIs are refused as material maps.

Both registered identity/hash and actual decoder format/extension are checked. Adapter consumption enforces configured byte/dimension caps, treats Pillow bomb warnings as errors, and fully decodes bounded images before Blender admission. Parent measured that Image.verify() alone accepts a JPEG missing its two-byte EOI, while Image.load() rejects it; the final code and independent regression cover that case.

The original pinned static-plan SHA256 remains 92de3ed358ecc3bc4ef91127db9c6a12254e86a59f255645c03579b60b03788a. Empty material overrides keep the legacy plan shape.

## Independent authorship, review and findings

Core and worker implementation authors were separate from the test and runtime-harness authors. An independent read-only reviewer checked the contract and final production changes; no blocking production defect remained. Parent adjudicated three contract amendments: configured decoder caps at the PBR consumer; explicit texture usage on the previously geometry-only store verifier; and full JPEG decoding after bounds checks. Test fixture corrections supplied real adapter Settings and compared normalized Windows paths; no production assertions were weakened.

BlenderMCP source reuse is covered by ../../THIRD_PARTY_NOTICES.md, including its full original MIT notice, fork revision and addon.py SHA256. Its shader-node and packed-channel ideas were adapted; its downloader and temporary-file paths were not imported.

A process violation occurred: the worker implementer used a direct patch in the primary checkout instead of the worktree/ProjectRoot route. Parent proved the entire primary diff consisted of that agent's helper and single conditional, transferred the helper to the intended worktree through ProjectRoot, and restored the primary file to HEAD content and its configured CRLF representation. Parent verified primary tracked status clean afterward; unrelated untracked directories were preserved. Follow-up edits used ProjectRoot. This was corrected, not treated as compliant retroactively.

## Actual runtime evidence

Independently authored harness: run_smoke.py. Agent run: `_work/20261107T234633_519d0dc9`. Parent independently reran the same harness successfully: `_work/20261107T234739_770185d3`. Run command from the api-conformance worktree:

```
D:/Work/video_pipeline/.venv/Scripts/python.exe -B spikes/19_pbr_materials/run_smoke.py
```

Both runs use a fresh contained project, retaining the original spike18 project unchanged. Actual Diffuse/nor_gl/ARM JPG bytes were recovered from the previous Poly Haven source cache, matched to its immutable source manifest hashes, and registered through create-asset with CC0 provenance. No network, credentials, generation or new downloads were used. The existing create-location CLI was exercised; the scene was built/persisted/reopened and all three map paths bound by the adapter.

Blender 5.2.0 LTS, build fbe6228777e7, verified actual node links/color spaces, normal strength (~0.65), UV scale [2,3], shared UV mapping, optional AO/red channel, ARM G/B fallback and dedicated scalar-role precedence. Dedicated scalar-role checks reuse the ARM image only to test routing, not to claim separate roughness/metallic source quality. Images were packed, original paths detached, and the saved .blend reopened in a fresh process with nonzero packed images. The first diagnostic attempt stopped on the harness's raw Windows path-string comparison; normalized-path verification corrected that harness assertion. Failed diagnostic artifacts are retained.

The same probes imported both prior provider GLBs under factory startup and disabled autoexec. Poly Haven table: one mesh, three image shader links. Sketchfab selected model: one mesh, zero image links (untextured source); measured authored bounds are in blender_report.json. Do not treat those authored bounds as normalized placement dimensions. This closes the previously pending raw Blender import check for these two acquired models.

Child scripts/logs/output/temp paths were authorized through ProjectRoot; preview generation, backup saves and autosaves were explicitly disabled before saving. No render queue, GPU render or still was produced. This is structural shader/import/portability verification, not evidence of a delivered visual-quality gain or default-resource acceptance.

## Verification gate

Parent independently ran the full frozen-source suite: **1669 passed, 40 skipped, 6 warnings in 496.86 seconds**. Exit0; before/after Python source hashes matched. This includes the final provider direct-GLB guard and all15 new PBR tests. Warnings comprise the Starlette deprecation and deliberate malformed-Pydantic-payload cases. Full-suite log, exact command, source hashes and machine-readable result are under `_work/final-verification/`. Test-author focused run:15 passed. Parent's earlier combined focused run was82 passed/1 failed due to a test fixture omitting Settings; that fixture was corrected before this final full run.

Parent observed Ruff clean across orchestrator/, tests/, spikes/18_asset_sourcing/ and spikes/19_pbr_materials/. git diff --check passed with Windows line-ending advisories only. The independent test author confirmed root `_pbr_tmp/` was exclusively disposable pytest scratch; parent moved it intact under `_work/pbr-independent-focused-scratch` after resolving and checking both absolute paths inside the workspace. Previous `.test-tmp/` and prior spike outputs were preserved. No commit, push or merge was performed.

## Remaining limits

Phase1 remains unaccepted. No new rendered shot or creator scoring was done. Workbench previews retain their existing flat-material limitation. Multi-map acquisition remains explicit calls through the existing per-asset operation; no atomic bundle downloader was added. App credits do not solve published-episode CC-BY credits. Script-driven room authoring and previous resource-admission caveats remain. Thumbnails, HDRI scene application, categories and optional aspect-preserving fit remain follow-up recommendations.
