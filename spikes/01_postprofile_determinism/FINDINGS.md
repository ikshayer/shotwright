# Spike 01 — PostProfile determinism and the overlay clock model

Run 2026-11-03. `ffmpeg 8.1.1-full_build-www.gyan.dev`. No GPU, no model downloads.

Validates the premise behind **FR-21**, **AT-05** and **AT-14**. Three layers hashed
separately (graph string, filtered frames via `framemd5`, encoded file via sha256),
plus a negative control that changes only `degrade_seed`.

## Result: 10/10 on both profiles

| Check | `dvr-2005` 640×480/12 | `cif-lowlight` 352×288/10 |
|---|---|---|
| Filtered frames reproducible | PASS | PASS |
| Seed change alters output (negative control) | PASS | PASS |
| Encode reproducible, `threads=1` | PASS | PASS |
| Encode reproducible, `threads=auto` | PASS | PASS |
| Frame count matches `artifact_schedule` | PASS (54) | PASS (44) |
| Display AR is 4:3 | PASS (sar 1:1) | PASS (sar 12:11 → dar 4:3) |
| Clock jumps only at drop boundaries | PASS (7/12 s) | PASS (0.7 s) |
| Image static across freeze | PASS | PASS |
| Clock keeps ticking across freeze | PASS | PASS |
| Output is CFR | PASS | PASS |

`post_profile_cif_bad.json` is rejected at load, by design:

```
PROFILE REJECTED AT LOAD: freeze window frames 48-58 falls outside the decimated
range 0-49 (10 fps x 5 s). Profile rejected at load.
```

Golden hashes in `results_dvr-2005.json` and `results_cif-lowlight.json`.

**x264 at `threads=auto` was byte-identical across runs**, so determinism does not
cost single-threaded encoding. Same-machine, same-build result; do not assume it
travels.

## Finding 1 — seed derivation must be range-aware per filter

`noise` caps `all_seed` at `INT32_MAX`; a derived 32-bit seed produced

```
Value 2574725062.000000 for parameter 'all_seed' out of range [-1 - 2.14748e+09]
```

and FFmpeg **failed the job** rather than ignoring the seed. Good failure mode, but
the per-filter accepted range belongs in the stage schema. → FR-21 updated.

## Finding 2 — out-of-range schedule windows were silently discarded

A schedule written for 12 fps, reused at 10 fps, put a freeze at frame 48 of a
44-frame stream. `loop` **did nothing, exited 0, and emitted no warning** — the
manifest would have claimed a freeze the video did not contain. Fixed by validating
every window against the post-decimation frame count at load. → FR-21, AT-14 updated.

## Finding 3 — the overlay clock model was specified wrong

v1.2 said overlay burn-in is the final stage computed from the post-decimation
timebase. All three parts of that were wrong:

- Computed from **output PTS**, a dropped-frame window leaves no trace in the clock.
  A real DVR burns the timestamp at record time, so surviving frames keep their true
  time and the clock jumps. Now measured: a 6-frame drop at 12 fps yields a single
  **0.583333 s** delta (7/12) at exactly the drop boundary and a normal tick
  everywhere else; at 10 fps, **0.7 s**.
- Burned **last**, the timestamp stays crisp over degraded footage — a believability
  tell under §4.3. A real burn-in is recorded into the picture, so the overlay now
  precedes the noise and compression stages.
- A freeze must hold the **image** while the clock **advances**. Proven by hashing the
  freeze run twice: identical frames before the overlay stage, all frames distinct
  after it.

Freeze semantics changed accordingly: a freeze **replaces** source frames rather than
adding them, so it is length-neutral and the clock stays continuous. The earlier
insert-frames model had no physical analogue.

## Finding 4 — do not trust `FRAME_RATE` and `TB` in filter expressions

`setpts='(N+offset)/FRAME_RATE/TB'` produced PTS `0,1,2,2,4,5,5,6` at 10 fps, because
those variables resolve from upstream metadata that `fps`, `select` and `loop` leave
in an unexpected state. Every timing stage now sets the timebase explicitly:

```
settb=1/12,setpts='N+if(gte(N,24),6,0)'
```

This is the difference between a clock that is right and one that is plausible at a
glance. → FR-21 updated.

## Finding 5 — PTS deltas need a tolerance, not equality

`dvr-2005` output delta distribution: `{0.083333: 35, 0.083334: 18}`. Rational
timebase rounding alternates the nominal tick by 1 µs, so timing is asserted as a
distribution with a half-frame-interval tolerance. §12.2 already said this — and the
first version of this harness's own jump detector fell into the trap anyway, flagging
all 53 deltas as jumps.

## Finding 6 — FFmpeg's `loop` filter `start` is offset by one

`loop=loop=11:size=1:start=41` repeats the frame at 0-based position **40**. Verified
by per-frame hashing. The graph builder compensates (`position + 1`).

This is a behaviour of the **pinned** FFmpeg build, not a stable contract, so it is
recorded in §8.4's FFmpeg row. An FFmpeg upgrade must re-run this spike before the
golden hashes are re-blessed: if the off-by-one changed, every artifact would move one
frame while each run's hashes still matched themselves.

## Reproduce

```
python run_spike.py                            # dvr-2005      expect 10/10
python run_spike.py post_profile_cif.json      # cif-lowlight  expect 10/10
python run_spike.py post_profile_cif_bad.json  # expect rejection at load
```

`_work/` holds the generated master and encodes; it is disposable and should be
gitignored.
