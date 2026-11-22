# Camera fields contract (step 3a, 2026-11-22)

Step 3 of `GAP_REVIEW.md`'s fix order ("stop silent drops"). `CameraProfile` declares
fourteen post-only fields (PRD §4.4), but `postprofile_spec_from_camera`
(`orchestrator/service/runner.py`) maps only `fps`, `storage_resolution`,
`display_aspect_ratio`, `frame_drop_schedule` and `codec`. These nine are accepted and then
ignored: `bitrate_kbps`, `decimation_pattern`, `noise_model`, `chroma_smearing`,
`block_artifacts`, `sharpening`, `grade_white_balance`, `overlay_model`, `analog_artifacts`.

**Creator decision (2026-11-22): implement them**, every one **opt-in per camera
profile**, so the agent turns on what the story and the creator ask for. A field at its
neutral value changes nothing; an active field changes the picture or the encode
measurably and deterministically; nothing is ever accepted and ignored.

`analog_artifacts` is built in step 3c on the `ntscrs` core crate (creator decision). Until
then a non-empty `analog_artifacts` is **refused** at validation with an error saying it
needs the ntscrs stage, never dropped.

## R1 — Typed fields

Replace the free-form dicts with typed, `extra="forbid"` models. A value of `{}` or the
neutral value parses as inactive, so every stored profile keeps loading.

| Field | Type and range | Neutral (default) | Effect when active |
|---|---|---|---|
| `bitrate_kbps` | `int \| None`, 16–20000 | `None` | Rate-controlled encode: `-b:v`, `-maxrate` equal to it, `-bufsize` twice it; CRF is not passed. |
| `decimation_pattern` | `str \| None`: colon-separated positive integers, 1–16 terms, e.g. `"5:5:6:4"` | `None` | An uneven DVR cadence. Each term is how many source frames one output frame spans, cycling. The mean of the terms must equal the source rate ÷ `fps` exactly, so frame count and timing match uniform decimation; otherwise validation fails naming both rates. Replaces the uniform `fps` stage. |
| `noise_model` | `{luma: 0–100, chroma: 0–100, temporal: bool = true, uniform: bool = true}` | `{}` | Seeded FFmpeg `noise` with per-component strengths (`c0s` luma, `c1s`/`c2s` chroma) and flags `t`/`u`. |
| `chroma_smearing` | `float`, 0–16 (storage pixels) | `0` | Horizontal-only blur of the chroma planes; luma and vertical chroma are untouched. |
| `block_artifacts` | `float`, 0–1 | `0` | Coarser quantisation and weakened deblocking at encode. For `libx264`: `qpmin = round(18 + 33 × s)` and the deblocking filter at its weakest (`-6:-6`). Other codecs use their own quantiser floor, or validation refuses the combination naming the codec. |
| `sharpening` | `float`, 0–3 | `0` | `unsharp` luma, 5×5, at that amount. |
| `grade_white_balance` | `{temperature_k: 1000–40000, red_gain/green_gain/blue_gain: 0.25–4, brightness: -1–1, contrast: 0–3, saturation: 0–3}`, every key optional | `{}` | `colortemperature` (when `temperature_k` is set), `colorchannelmixer` diagonal gains (when any gain is set), `eq` (when any of brightness/contrast/saturation is set). |
| `overlay_model` | `{camera_id_text, clock_epoch (int ≥ 0), clock_format, fontsize (6–96), x, y (≥ 0), placement: in_recording \| on_playback}`, every key optional | `{}` | Overrides the matching fields of the PostProfile's clock overlay. `clock_format` is a strftime format rendered by drawtext's `pts:gmtime:<epoch>:<format>`. It may use only `%Y %y %m %d %H %M %S %p %I %b %a %j`, digits, letters, spaces and `/ - : . , [ ] ( )`. Anything else fails validation, because drawtext escaping is a known injection surface. |
| `analog_artifacts` | as today until step 3c | `{}` | Non-empty: refused (see above). |

`drift_rate` and clock instability events (PRD FR-21) are **not** part of this step. The
creator deferred timestamp-unstable events to P1 on 2026-11-05. An `overlay_model`
naming them is refused under `extra="forbid"`.

**Legacy `bitrate_kbps`.** Until now the field defaulted to 800 and had no effect. A
CameraProfile persisted before this change reads `bitrate_kbps` as `None`, so existing
projects' output does not change silently. The seeded profiles set none of these fields.
Implement this with the store's existing schema-version mechanism for `CameraProfile`
(bump its schema version; a lower version on disk maps a stored `bitrate_kbps` to `None`).
New writes record the new version.

## R2 — One derived spec, camera fields win

`postprofile_spec_from_camera` builds the spec actually applied: the stored PostProfile's
stages plus the active camera fields. When an active camera field and a stored stage of
the same family both exist, **the camera field wins**: its stage replaces the stored one
under the same position, and the replaced stage's name is recorded in the derived spec's
`provenance` as `{"camera_overrides": [{"field": ..., "replaced_stage": ...}]}`. Families:
`noise_model` ↔ `noise`; `sharpening` ↔ `unsharp`; `grade_white_balance` brightness,
contrast and saturation ↔ `eq`; `decimation_pattern` ↔ `fps`; `overlay_model` ↔ the clock
`drawtext` overlay fields. Camera stages are named `camera_<field>` (e.g.
`camera_noise_model`).

The derived spec is the one validated, hashed (`filter_graph_hash`), recorded on the
ShotOutput receipt and reported. A reader can always see what was applied.

**Stage order** extends the measured order (`profile.py`). It runs: scale, then fps or the
decimation pattern, select, loop, clock setpts, drawtext, setpts. Then `colortemperature`,
`colorchannelmixer`, `eq`, chroma smearing, `noise`, `unsharp` and `setsar` follow in that
order. The encode-level fields (`bitrate_kbps`, `block_artifacts`) go to the encode
arguments, not to the filtergraph.

Add the new filters to the closed filter map with their exact parameter sets and
ranges, and extend `validate_profile`'s camera cross-check so the derived spec must agree
with every active field.

## R3 — Determinism and measurement

- Two degrades with identical inputs, shot id and `degrade_seed` are byte-identical with
  any combination of fields active (the existing `+bitexact` discipline; single-threaded
  encode).
- Stochastic stages take seeds from `derive_seed(shot_id, degrade_seed, stage_name, max)`,
  clamped to that filter's range.
- Each field's effect is checked against a measurement, not just the presence of a
  filter string. Tests use real FFmpeg on generated clips, each against the same clip
  with the field neutral:
  - bitrate: mean bitrate within ±25 % of the target on a high-motion clip;
  - decimation: the kept-frame cadence matches the pattern;
  - noise: luma and chroma noise standard deviations each rise with their own strength;
  - chroma smearing: horizontal chroma edge width grows, while vertical chroma and luma
    stay within a tight tolerance;
  - block artifacts: quantisation error against the master (PSNR) rises monotonically with
    strength, and an 8×8 block-boundary discontinuity metric is above neutral at every
    active strength and rises from low to full strength. *Amended 2026-11-22 after
    measurement:* the boundary metric is not monotonic between neighbouring strengths on a
    smooth clip, even at constant QP (1.68 at 0.4, 1.53 at 0.7, 2.34 at 1.0), while PSNR
    falls steadily (46.4, 44.9, 39.5, 33.7 dB at 0, 0.4, 0.7, 1.0);
  - sharpening: edge energy rises;
  - white balance: the mean R/B ratio moves in the expected direction for warm and cool
    temperatures, and gains scale their channel;
  - overlay: the rendered text matches `clock_format` (checked against the expected
    string drawn by the same font in isolation, or by OCR-free pixel comparison).

## R4 — Agent-facing descriptions

`create_camera_profile` and `revise_camera_profile` (routes) describe each field: its
type, range, neutral value and what it does. They state that every field is opt-in, that
camera fields override a PostProfile stage of the same family, and that `analog_artifacts`
is refused until the ntscrs stage exists. `measure_output` and `contact_sheet` let an agent
check the result (perception step).

## Amendments from review and the real run (2026-11-22)

Three review rounds (Codex `gpt-6.1-sol`) and a real degrade added these rules:

- **Neutral changes nothing, byte for byte.** With every camera field neutral, the graph
  and `filter_graph_hash` equal `main`'s for every PostProfile `main` accepted. A
  `format=<pix_fmt>` stage is inserted only when an RGB filter (`colortemperature`,
  `colorchannelmixer`) is followed by a YUV-plane filter (`eq`, `gblur`, `noise`,
  `unsharp`). Without it, FFmpeg negotiates `gbrp`, so `planes=6` would blur blue/red and
  `c0s` would noise green.
- **drawtext text.** Text uses `main`'s quoted serialization only when there is no custom
  clock format and the camera id uses only `[A-Za-z0-9 _.#()/-]`. Everything else takes
  two-layer escaping (option value and filtergraph), verified by rendering hostile ids
  literally. `camera_id_text` refuses control characters and leading or trailing
  whitespace. `font_relative` is restricted to `[A-Za-z0-9_./-]` segments.
- **Refused rather than ignored:**
  - a noise stage that mixes `all*` with `c0*/c1*/c2*` parameters;
  - `composite_stage` on any non-drawtext stage;
  - `clock_format` on an `on_playback` overlay, until step 3b;
  - `block_artifacts` with a codec other than libx264;
  - a YUV-plane camera effect with an RGB `pix_fmt`. RGB encodes stay allowed otherwise.
- **Legacy records.** A pre-v3 CameraProfile is first validated against the types `main`
  read it with: `bitrate_kbps` int > 0, `dict[str, float]` for noise and white balance,
  and `dict[str, str|int|float]` for the overlay; null keys count as neutral. It is then
  normalised: integral numbers become ints, 0/1 become bools, and `bitrate_kbps` becomes
  None. Fractional, non-finite or unknown values fail, naming the field. The migration
  lives only in the store's record read. A request that merely carries an old
  `schema_version` keeps its explicit `bitrate_kbps`; the real run found that this value
  had been silently nulled.
- **Receipt.** `ShotOutput.postprofile_spec_sha256` is the SHA-256 of the derived spec's
  canonical JSON, which includes encode settings and `camera_overrides`. That JSON is
  retained in the run metadata. `filter_graph_hash` alone does not bind `bitrate_kbps` or
  `block_artifacts`.
- **Decimation timing.** Every output frame lasts 1/fps, including the last. The real run
  first produced a final 1/60 s frame and a 25.917 s container where uniform decimation
  gives 26.000 s.
- **Bitrate and block artifacts interact.** `bitrate_kbps` is a target, and the quantiser
  floor from `block_artifacts` can hold the encode far below it. Real run: 0.5 (qpmin 35)
  gave 12 kb/s against a 250 kb/s target on dark, static footage.

## Non-goals

`analog_artifacts` (step 3c); on-playback overlays at export and the default font (3b);
clock drift and instability events (P1); changing any rebuild-triggering field.

## File ownership

**Implementer** (Codex `gpt-6-luna`, effort max): `orchestrator/domain/entities.py`
(CameraProfile field types, schema version), `orchestrator/store/json_store.py` (only the
CameraProfile legacy read, if the migration lives there), `orchestrator/service/runner.py`
(`postprofile_spec_from_camera`), `orchestrator/adapters/postprofile/profile.py`,
`orchestrator/adapters/postprofile/runner.py` (encode arguments),
`orchestrator/service/dependencies.py` (seeded profiles only if a type change requires it,
values unchanged), `orchestrator/api/routes.py` (descriptions). Existing tests only where
they pin the old dict types or the closed filter map.

**Test author** (separate agent): only a new `tests/adapters/postprofile/test_camera_fields_contract.py`.

**Do not touch:** `spikes/**`, the export adapter (3b), anything else.

## Done when

Every requirement has a test; the full suite passes (run alone, `--basetemp` on D:); ruff
reports nothing beyond `main`'s; one real degrade of an existing master with several fields
active is inspected with `contact_sheet` and `measure_output` by the parent.
