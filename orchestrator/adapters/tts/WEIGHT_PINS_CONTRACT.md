# IndexTTS-2 weight pins contract (step 3d, 2026-11-22)

`GAP_REVIEW.md` (verified finding): `_verify_runtime` in `indextts2.py` checks that files
exist and hashes `config.yaml`, but not the weights. A swapped `gpt.pth` keeps the recorded
identity, so a GenerationRun's `tool_version` does not prove which model spoke.

## R1 — Pin table

Add to `orchestrator/adapters/tts/core.py` a pin table of every checkpoint file the
IndexTTS-2 runtime **actually loads**, following `adapters/comfyui/profiles.py`'s
`PinSpec` pattern: relative path under `checkpoints/`, size in bytes, SHA-256, and source
(`repo@commit:path`). Determine "actually loads" from the pinned IndexTTS source
(`INDEXTTS2_COMMIT`), its `config.yaml` and `indextts2_worker.py`, not by guessing. Files
present but not loaded (for example `hf_cache/w2v-bert-2.0/conformer_shaw.pt`) are not
pinned. Report the list and how each file was established.

The parent verified on 2026-11-22 that the local files match the publishers. The pins are
the published SHA-256 values:

| Path | Size | SHA-256 | Source |
|---|---|---|---|
| `gpt.pth` | 3484663079 | `baaaeb8b56328da81731dc540a85a7dee32eca9da28f174b05757cb651c602a4` | `IndexTeam/IndexTTS-2@740dcaff396282ffb241903d150ac011cd4b1ede` |
| `s2mel.pth` | 1202198223 | `aae1bb12017cbb47e7a5ce537fc82f40b6b1deb71acdb9b8f25686f32714b636` | same |
| `bpe.model` | 475997 | `b2a5ce8090d32da3642cc4f81fdc996376bc6dd3f4cd5e3d165f71120d9f2bc8` | same |
| `feat1.pt` | 57170 | `f219cb447d80216ba615666da2ff8d63ac544eee26657f3a7b278692bf7a67c4` | same |
| `feat2.pt` | 374866 | `9c4292e96dee535aea9a6206e9a0c856dd578dde9212acdb16dd3ada4d12bf80` | same |
| `wav2vec2bert_stats.pt` | 9343 | `c9c176c2b8850ab2e3ba828bbfa969deaf4566ce55db5f2687b8430b87526ad2` | same |
| `qwen0.6bemo4-merge/model.safetensors` | 1192135096 | `11293257a8df593c154a8ecd5fc039f3076de35411e35f06d41b471e136f6641` | same |
| `qwen0.6bemo4-merge/tokenizer.json` | 11422654 | `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4` | same |
| `hf_cache/w2v-bert-2.0/model.safetensors` | 2322063736 | `eb890c9660ed6e3414b6812e27257b8ce5454365d5490d3ad581ea60b93be043` | `facebook/w2v-bert-2.0@da985ba0987f70aaeb84a80f2851cfac8c697a7b` |
| `hf_cache/semantic_codec_model.safetensors` | 177183712 | `ec947271175d8cad75ec37e83aa487e27c97a0f72a303393772da5ffa84bddf2` | `amphion/MaskGCT@265c6cef07625665d0c28d2faafb1415562379dc:semantic_codec/model.safetensors` |
| `hf_cache/campplus_cn_common.bin` | 28036335 | `3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8` | `funasr/campplus@e4b6ede7ce16997aff4ae69fbca1f0175e2afede` |
| `hf_cache/bigvgan/bigvgan_generator.pt` | 449228171 | `e95ba25972d3de0628d99cd156e9315a9c018899bf739988959ebe3544080ced` | `nvidia/bigvgan_v2_22khz_80band_256x@633ff708ed5b74903e86ff1298cf4a98e921c513` |

Small files that the publishers store as plain git blobs have no published SHA-256. These
are the JSON configs, the remaining `qwen0.6bemo4-merge` tokenizer files, and the existing
`config.yaml` pin. For those that are loaded, pin the local SHA-256, and record the source as
`local, size matches publisher (no published SHA-256)`. The parent checked the three config
files' git blob ids against the publishers (`config.yaml`, `w2v-bert-2.0/config.json`,
`bigvgan/config.json`: all match). The local SHA-256 values are in
`indextts-local-hashes.txt`, which the parent passes with this contract.

## R2 — Verification

- `_verify_runtime` checks every pinned file: it must exist, be a regular file and not a
  symlink, have the pinned size (checked first, so a wrong size fails fast) and the pinned
  SHA-256. A mismatch fails `MISSING_MODEL_ARTIFACT` naming the file, the expected and the
  observed value. It never launches the worker after a mismatch.
- Hashing is chunked and honours the job's cancel check (ComfyUI's `_sha` pattern), so a
  cancelled job does not wait for 8.9 GB to finish hashing.
- **Cost control.** A successful verification is cached **in memory for the process**,
  keyed by `(path, size, mtime_ns)` for each file. Within one process a later job
  re-hashes only a file whose key changed. Nothing is cached on disk, so each new process
  verifies once. Measure and report the cold verification time.
- Verification runs **before** the worker is launched. Report whether it runs inside the
  GPU lease. If it does, its time must fit inside `indextts2.startup_allowance_seconds`
  with margin, or it moves before lease acquisition without weakening the check.

## R3 — Identity

`tool_version` (and the report's `engine_version`/`runtime`) gains a weights digest: the
SHA-256 of the sorted `path:sha256` lines of the pin table. A GenerationRun therefore
identifies the exact weights. Existing persisted GenerationRuns are unchanged (they keep
their old string); nothing rewrites history.

## Non-goals

Re-downloading or moving weights; pinning Piper (separate); changing IndexTTS behaviour.

## File ownership

**Implementer:** `orchestrator/adapters/tts/core.py`, `orchestrator/adapters/tts/indextts2.py`;
existing TTS tests only where they pin the old identity string or `_verify_runtime`'s
file list. **Test author:** only a new `tests/adapters/tts/test_weight_pins_contract.py`
(no real 8.9 GB hashing in tests — use a temporary checkpoint tree with small files and an
injected pin table; plus one test that the real table has the R1 published values).
**Do not touch:** `spikes/**`, the weights themselves.

## Done when

Every requirement has a test; the full suite passes; ruff clean against `main`; one real
IndexTTS-2 take is generated with verification on (GPU) and its GenerationRun shows the
weights digest.
