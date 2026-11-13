# Contract — live schema validation must resolve nested dynamic-combo inputs

Written by the orchestrator, 2026-11-13, after the first authorised GPU run. Implementation
and tests come from **different agents working in parallel**, neither reading the other's
output. Where they disagree, this file decides and the disagreement is reported, never
patched away.

Addendum to `orchestrator/adapters/minimax_h3/CONTRACT.md` and to
`NATIVE_BLOCKERS_CONTRACT.md` (see its Amendment 3, which this contract closes out). Both
stand in full. Nothing here relaxes them.

## Standing facts this work must not contradict

- The adapter is **INCOMPLETE** and has run on GPU exactly **once**, reaching server-ready
  and then failing on the defect below. Nothing here authorises another GPU run, a model
  load, an install, or a download — with the single exception of the `/object_info`
  capture in "Prerequisite", which is explicitly bounded and loads no model.
- `prog-combined` stays at `f0e741a`. Work happens on a branch off `h3-blockers-verify`.
- `spikes/22_minimax_h3_workflows/**` is **pinned and read-only**. `local_h3.py`'s SHA-256
  is recorded in every run's provenance receipt (`provenance.helper`). Editing it silently
  changes that receipt. **Do not modify one byte of it.**
- `D:\ComfyUI_3` is immutable and is not involved. The pinned runtime is ComfyUI **0.44.0**
  at source commit `2c75dc1b379a59e0880619d029607af5169a62ef`, under
  `spikes/22_minimax_h3_workflows/_work/local_runtime/source/ComfyUI`.

## The defect

`native.py::_validate_live_schema` (around line 1577) builds

```python
known = set(required) | set(optional)
absent = set(node["inputs"]) - known
```

from `/object_info`, and refuses any graph input not in that **flat** key set. The real run
died with:

```
H3SetupError: Installed schema for SaveVideo lacks inputs:
  ['format.codec', 'format.codec.encoding']
```

ComfyUI 0.44.0 does not publish those as flat keys. It publishes `format` as a
`COMFY_DYNAMICCOMBO_V3` whose sub-inputs are nested inside
`descriptor[1]["options"][k]["inputs"]["required"]`, and those sub-inputs may themselves be
dynamic combos. Read from the pinned source, `comfy_extras/nodes_video.py` lines 76-166,
`SaveVideo` nests **three levels**:

```
format  (DynamicCombo: auto | mp4 | mkv | webm | mov)
  -> codec     (DynamicCombo: auto | h264 | av1 | prores, per format option)
       -> encoding  (DynamicCombo, optional: auto | re-encode)   [only under h264]
            -> crf  (Float, 0.0-51.0, default 18.0)              [only under re-encode]
codec   (DynamicCombo, optional + hidden, top level)
```

## Adjudication — the graph is correct; the validator is wrong

This is the opposite of the initial reading recorded in Amendment 3 A3.2, and it is
**corrected here**. The adapter's graph at `core.py:227` declares `format`,
`format.codec`, `format.codec.encoding` and a bare `codec`. **Every one of those is a
legitimate input** for ComfyUI 0.44.0: the first three by the nesting above, and `codec`
because `_save_video_codec_input(..., optional=True, hidden=True)` really is a top-level
optional input — which is exactly why the error named the dotted pair but **not** `codec`.

Two consequences that must not be missed:

1. **Do not "fix" this by changing the graph.** Removing the dotted inputs would make the
   validator pass while discarding real codec control. The graph is not the defect.
2. **Do not fix it by copying `local_h3.py`.** The pinned helper expands exactly **one**
   level (`available["format.codec"] = mp4["inputs"]["required"]["codec"]`, line ~815) and
   so cannot validate `format.codec.encoding` either. The helper is a **subset**, not the
   reference implementation. Amendment 3 A3.2 called this "duplicated logic that drifted";
   that was half right — the duplication is real, but the helper's copy is also
   insufficient. Recursion is genuinely new work.

## Required behaviour

**Resolve dotted input names by walking the selected option at each level.** For a graph
input `a.b.c`, resolve `a` in the flat schema, require it to be a dynamic group, select the
option named by the graph's **own value** for `a`, resolve `b` among that option's inputs,
and recurse. Depth is not fixed at two or three; implement it as recursion, not as nested
special cases.

**Expansion is value-directed, and this is the load-bearing requirement.** `format.codec`
exists only because the graph sets `format = "mp4"`; `format.codec.encoding` exists only
because it then sets `format.codec = "h264"`. A flatten-all-options implementation would
accept `format.codec.encoding` even when `format.codec = "av1"` — which the installed
schema does not offer. **That is a weakening, and it is the failure mode most likely to
pass a careless test.** The guard must still refuse a dotted input whose parent selection
does not offer it.

Also handle **`COMFY_AUTOGROW_V3`**, which the helper expands for
`ref_videos.ref_video_0` via `template["input"]["required"][...]` with a `prefix` and a
`max`. The adapter's own graph uses `MiniMaxH3ReferenceToVideo`, so this path is reachable
and must not be left for the next run to discover. Respect `prefix` and `max`: an index at
or beyond `max`, or a name not matching `prefix`, is refused.

**Everything the check already refuses, it must go on refusing**: an unknown node class, a
genuinely unknown port, a malformed descriptor, a value outside a COMBO's options, and a
numeric outside its bounds. The existing value and link-type checks must apply to resolved
nested descriptors too — a nested input is not exempt from validation just because it was
reached through expansion.

**One implementation, in the orchestrator package.** It must be importable and directly
unit-testable without a server. `local_h3.py` keeps its own copy untouched for the reasons
above; record in a comment that the orchestrator's version is the superset and why the
helper's cannot be shared.

## Prerequisite — capture a real `/object_info` once

Offline tests need real schema evidence, and the run that found this defect did not save
it. **Bounded task, no model load:** launch the pinned server exactly as the adapter does
(the full flag set is recorded in Amendment 3 A3.6 and in the run's
`cleanup.command`), `GET /object_info`, write it verbatim, stop the server, then verify
port 8298 is free and no pinned-runtime python survives.

- Commit it under `tests/adapters/minimax_h3/data/` with its SHA-256 and the ComfyUI
  version and source commit recorded beside it.
- Expect **~166 s** to ready; do not shorten the startup allowance to make it quicker.
- If the capture is large, keep the node entries the adapter's graph actually references
  plus `SaveVideo` and `MiniMaxH3ReferenceToVideo` in full, and say exactly what was
  dropped. **Do not hand-edit the retained entries** — a doctored schema proves nothing.

## The tests that prove it

Blind to the implementation. Against the captured real schema, not a hand-written mock:

1. **The failing case now passes.** The adapter's actual graph from `core.py` validates
   against the real captured `/object_info`. This is the regression test; it must fail
   against today's code.
2. **Depth.** `format.codec.encoding` resolves, and the `re-encode` path's `crf` resolves
   with its Float bounds enforced — proving recursion rather than a two-level hack.
3. **Value-direction, in both directions.** With `format.codec = "h264"`,
   `format.codec.encoding` is accepted. With `format.codec` set to a codec whose option
   does not offer `encoding`, the same input is **refused**. A fix that passes the first
   and not the second has widened the guard and must be reported, not accommodated.
4. **Wrong selection at the top.** A `format` value whose option does not offer `codec`
   refuses `format.codec`.
5. **Autogrow.** `ref_videos.ref_video_0` resolves; a name beyond `max` or not matching
   `prefix` is refused.
6. **Nothing was weakened.** An invented port, an invented dotted port under a valid
   parent, an out-of-range numeric, and a COMBO value outside its options are each still
   refused, with the message naming what was observed.
7. **The hidden top-level `codec` input stays valid**, since it is optional and hidden but
   real.

## What is explicitly out of scope

The four unmeasured reservation numbers; any further GPU run beyond the bounded
`/object_info` capture; the deliberate-stop-recorded-as-cleanup-failure noise in
Amendment 3 A3.8; the unexercised `compose_h3_request` service binding in A3.9; the
listener-ownership question still open in A3.3; Poly Haven or Sketchfab; any change to the
pinned Comfy, model or spike trees.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -B -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Run from the worktree. Note `-B`. Current baseline on `h3-blockers-verify`: **2055 passed,
58 skipped, 0 failed**, and **ruff clean at 0 findings** — both must still hold. Ruff was
only just brought to zero; do not spend that.

## Report what you could not do

State plainly what this contract left ambiguous and anything you believe it got wrong.
Amendment 3 corrected my own A3.2 reading within a day of writing it, and the FR-03
contract needed twelve. Assume this one is wrong somewhere and say where. **The admissions
are the most valuable part of the report.**

In particular, answer: does any node in the adapter's graph other than `SaveVideo` and
`MiniMaxH3ReferenceToVideo` carry a nested or autogrow input that this contract failed to
anticipate?
