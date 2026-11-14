# MiniMax H3 prompt findings — first-person camera control

## Result

On 2026-11-13 the creator reviewed the Stage J candidate and said, "I would say this passes",
and ruled further audio refinement out of scope ("Refining the audio further goes beyond just
testing the model capabilities"). This closes the authorized first-person capability test only.
No production approval record was created, and this is not Phase 1 ratification, publication
clearance, or default-backend promotion.

Artifact: `generated/h3/s1/v0001/stagejrun1/candidate.mp4` in project `_work/phase1_restage3/exit`,
SHA-256 `66d70d7e7cd3d8d84d0322819d0c8366cf1db1d84edcb4c47bd0cc636b239634`, 384x288, 192 frames
at 24 fps, 8.000 s. Native execution 756.8 s, `cleanup_error: null`, listener ownership verified.

## The finding

**Naming a subject to be retained from `<Video 1>` makes H3 reproduce the reference clip's
camera along with that subject.** The reference here is fixed-ceiling CCTV footage, so every
prompt that asked for the standing figure to be preserved also inherited a static corner camera,
regardless of how explicitly the prompt asked for a moving first-person viewpoint.

This is a prompt property, not an adapter property. Four runs held seed, shape, reference window
and graph constant; the prompt was the only variable. `api_graph.json` for the adapter's run and
for the creator's accepted ComfyUI run differ in exactly two fields, `filename_prefix` and
`prompt` — same `mode: "reference"`, same `keyframe_role: "first"`, same seed
`8263381764361378`, same dimensions, same media node.

| run | prompt | divergence from frame 0 @1 s / @7 s | camera |
|---|---|---|---|
| G | own composition, `<Subject 2>` = figure `attribute_transfer` | 23.1 / 25.9 | fixed corner |
| H | creator prompt verbatim (control) | 25.1 / 38.4 | **first person** |
| I | creator skeleton + figure as prose, no retention marker | 6.2 / 26.3 | slow dolly from corner |
| J | creator prompt + 3 edits, **no figure text** | 25.4 / 47.3 | **first person** |

Divergence is the mean absolute difference between frame 0 and frame t over a 64x48 grayscale
downscale: a fixed camera stays flat, a travelling one diverges and stays diverged. Stage F,
an earlier own-composition run, measured 0.5-0.7 across the whole clip — a locked camera.

Stage I is the load-bearing result: demoting the figure to plain prose with no `<Video N>` tie
and no retention marker was **not** sufficient. Any substantial static-subject description pulls
the framing back toward the reference.

## The rule

Do not describe the retained entity at all. The creator prompt never mentions a person, yet the
figure from the reference frame appears in the output unprompted, and reads as more menacing than
any version produced by describing it. Spend the description budget on the camera and the walk.

This sits alongside, and is narrower than, the official guide's rule ("Do not mark a `<Video N>`
as the source of a person or object. Visible content still needs a `<Subject N>` label."):
labelling the figure as a `<Subject N>` sourced from the video is what reproduced the camera.

## Reproducing Stage J

`_work/h3_stage/stage_j_prompt.txt` is the creator prompt
(SHA-256 `83b2fe747ebe4cade53654be271dd22ab81c3fa9433a80616eb2c2603c9989b1`) plus 1,023
characters across exactly three edits, every other byte identical:

1. the existing lighting sentence extended from one event to five, each at a named time, with
   "the room falls to total darkness";
2. `non_diegetic_music: N/A` replaced by the religious choral score, loudness pinned with
   "remains consistent in loudness throughout, never swelling, spiking or ducking";
3. one soundscape line giving each light failure a matching electrical snap.

Shape: `start_seconds=8.5`, 8.0 s, 384x288, 24 fps, 20 steps — the adapter `_BASELINE` and the
exact shape of the creator's accepted run. Runner: `_work/h3_stage/stage_j_run.py`.

## Known defect, accepted as out of scope

Stage J audio peaks at **-1.2 dBFS** (mean -34.5), against the control's -18.4 / -46.9. Nothing
reaches 0 dBFS so it is hot rather than clipped, and the music bed drives it. Relative-level
wording ("never becomes the loudest sound in the recording", "sits beneath the continuous hiss")
did **not** restrain it; Stage I, worded the same way, peaked at -0.2 dBFS. Gain belongs in the
PostProfile chain, which already owns the CCTV treatment applied after H3.
