# Controlled short-answer versus detailed-prompt comparison

Completed 2026-11-12UTC (2026-11-11 local). One new joint 15-second run used the supplied detailed prompt. Output grew from 63 to 212 tokens (3.37 times), but Qwen contradicted two salient baseline claims and repeated descriptions. This comparison does not establish that the short prompt was suppressing useful, accurate detail. The 384-token budget was not binding: both answers ended at EOS.

## Complete raw new response

The following is Qwen's exact decoded text, also retained as `_work/prompt_detail_comparison_20261112/raw_response.txt` and losslessly in the primary receipt.

```text
### Audio Analysis

The audio begins with a whispering voice, which is likely a male, speaking in a low, eerie tone. The whispering continues throughout the clip, with no other distinct sounds or music present. The whispering is the only audible element, and it maintains a consistent, unsettling quality. There are no other voices, music, or brief sounds detected in the audio.

### Visual Analysis

The video depicts a dark, narrow corridor with a brick wall on both sides. The corridor is illuminated by two bright lights at the end, creating a stark contrast with the surrounding darkness. The walls appear to be made of bricks, and the corridor seems to be part of an old, possibly abandoned building. The lighting is dim, with the only light source being the two bright lights at the end of the corridor. The overall atmosphere is eerie and unsettling, with the darkness and the narrowness of the corridor adding to the sense of unease. There are no visible subjects or movement in the video, and the lighting remains constant throughout the clip.
```

## Matched input and configuration

Baseline: `_work/runs/20261111T234229Z_b9589c00/analysis.json` (63 tokens, EOS). New run: `_work/runs/20261112T000909Z_c3335db7/analysis.json` (212 tokens, EOS; `token_cap_reached=false`). Baseline receipt SHA256 remains `e6f174217aba55d14adf993ea3bd786fc7fd256046cbff007556471626163fb1`.

Both use identical media bytes, source [1417,1432)=23:37?23:52, 640x470, 360 decoded frames, 12 selected frames at 0.8 fps, and 240000 continuous 16 kHz mono audio samples. Complete decoded metadata and selected PTS match exactly. Native 48 kHz stereo reference PCM is retained unchanged. Nontext modality tensor shapes match; input text-token length changed 1170 -> 1255 as expected for the different prompt. The entire model/runtime provenance, empty loading diagnostics,196 NF4/420 unquantized coverage hashes and effective generation options match.

Model remains pinned Qwen2.5-Omni-7B Thinker, NF4 language/BF16 AV, seed 0, greedy,`max_new_tokens=384`,EOS151645/PAD151643. Decoder, processor resolution, sampling, allocator policy, resource guards and process helper are unchanged. Current duration maximum 25 has no effect on caller limit 15 and was not increased for this experiment. No other media/model run or budget/density/duration increase occurred.

Exact supplied prompt is `_work/prompt_detail_comparison_20261112/detailed_prompt.txt`,864 UTF-8 characters, SHA256 `06c8209df27b0081b92da3dac5f8c716aee2eef3fe4cf86ff56fb8a7d34d42a4`. File bytes were read once; exact text agrees across file, worker command JSON, native chat construction, worker result and primary receipt. Its new optional CLI seam is `--prompt-file`; default short prompts are unchanged. Source SHA256 `291ec787d47746951a180cd561db98157d8f2259501d9eca8a3ca989279e9474` was frozen before inference and unchanged afterward. Parent AST audit and independent source review confirmed core decoding/loading/guard functions unchanged from baseline.

## What Qwen gained, lost or changed

| Aspect | Original 15-second answer | New detailed-prompt answer | Assessment |
|---|---|---|---|
| Whisper | Whispering voice | Low,eerie whisper; possibly male; allegedly continuous | Adds qualities and a speculative identity/continuity claim |
| Shout | Distant,distorted shout | Explicitly says whisper is the only sound, with no other voices or brief sounds | Contradiction; loses the baseline's reported shout |
| Subject and movement | Figure moving slowly toward camera | Explicitly says no visible subjects or movement | Contradiction; loses the baseline's reported approaching figure |
| Setting and lighting | Dark,narrow corridor,textured walls,minimal light | Brick walls on both sides,two bright lights at corridor end,stark contrast; possibly abandoned | Adds concrete architectural/light claims plus unsupported location speculation |
| Sequence and overlapping sound | Brief general description | Describes a constant whisper and constant lighting | No additional event sequence or overlapping elements reported |
| Format and uncertainty | Three sentences | Audio/Visual sections,10 sentences; no separate uncertainties section | More structure and length, with repeated whisper/light/mood descriptions; requested uncertainties section omitted |

All entries are Qwen claims. No firsthand listening or independently verified semantic truth is available. A more detailed answer may contain extra false positives or false negatives; neither answer is an accepted transcript. The new response's denials conflict with baseline claims, so extra length alone cannot establish improved coverage or accuracy.

The supplied prompt changes both the sentence restriction and content/format instructions. This single historical-baseline comparison cannot isolate the sentence restriction alone, establish repeatability/determinism, or distinguish omitted real events from baseline hallucinations. No automatic retry or token increase was made.

## Resources and worker cleanup

| Observed metric | Original 15 s baseline | Detailed-prompt 15 s |
|---|---:|---:|
| Whole-device GPU baseline/peak/post |1709/10888/1672MiB |1290/10547/1290MiB |
| Minimum sampled free GPU |1057MiB |1398MiB |
| Minimum sampled available RAM |6.626GiB |9.769GiB |
| Peak aggregate owned-process RSS |2.309GiB |2.297GiB |
| Generation time |10.188s |70.547s |
| Supervision time,including imports/hash/load/cleanup |106.094s |177.438s |

Samples are taken about every 5 seconds and may miss peaks. Whole-device GPU usage includes foreign processes; baseline memory availability differed between runs, so these figures do not isolate prompt memory cost or establish minimum requirements. Exact post-import RAM admission value was not retained; unchanged source enforces 6 GiB available RAM,8 GiB free GPU and8 GiB disk. Owned-worker stop guards remain below 2 GiB for three samples or 900 seconds.

Cleanup reports `verified_stopped=true`, no survivors, no unverifiable identities and no failures. Parent independently checked all three captured PID/create-time identities (18632,57380,63012): all absent. No analyzer Python processes remain. Resources/coverage/media/source/prompt hashes, worker/primary text and raw token agreement passed parent actual-artifact audit. No unrelated applications were stopped.

## Verification and retained evidence

- Independent prompt verifier: 43 checks passed after parent rerun, `_work/prompt_verification/ff99064242504e97a698eb9c35446220/report.json`. Real CPU tensors and actual analyze_one wiring; native processor/model and child launch intercepted. No GPU/model inference in this verifier.
- Existing CPU/serialization/generation/actual supervisor checks: 121 passed, `_work/verification/20261112T000709Z_509f6b55/report.json`.
- Strengthened actual 15 s receipt audit: 2 checks passed, `_work/verification/medium/20261112T001358Z_a30e3dd077/report.json`.
- Parent configuration/worker/hash/EOS/resources/freshidentity audit: `_work/prompt_detail_comparison_20261112/parent_runtime_audit.json`.
- Parent source invariants: `_work/prompt_detail_comparison_20261112/parent_source_invariant_audit.json`. Independent read-only source and actual-artifact reviews found no blocker.
- Scoped no-cache Ruff passed. Full repository pytest was not rerun for this spike-only change; older 1744 pass result is historical.

The first parent prompt-verifier run had 41 passes/two failures caused by Windows test fixtures (relative-path handling and reserved `nul.txt` name). Original reports/source were retained; corrected frozen verifier SHA256 is `465e6eda5491e22e61d46c4b450f892193ad6b4178b1aa5ada6767ace4422310`. No analyzer change was needed to pass those corrected cases. Synthetic CPU failure receipts under `_work/runs` are explicitly not model attempts.

Experiment prompt/frozen sources/raw response/audits live under `_work/prompt_detail_comparison_20261112/`. `attempt/` contains invocation, logs,result and source-before/after identities. All prior receipts and failures remain immutable. No downloads, installs, deletes, production approval/state changes, commits, pushes or publication occurred. Reference media remains analysis-only and must not enter production generation/export.
