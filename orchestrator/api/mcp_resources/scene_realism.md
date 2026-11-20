# Scene realism — what makes a generated 3D set read as real, not CGI

Reference for agents building or dressing a Blender scene for this pipeline. It distils the
AT-18 connected-apartment work (spike 30, `spikes/30_at18_found_footage/_work/`), where an
abandoned Soviet-era flat was taken from "obviously CGI" to "looks like real life" over a
series of verified passes. The pass-by-pass record is
`spikes/30_at18_found_footage/_work/z-image-handoff/HANDOFF.md`.

The guidance is ordered by how much each item contributed.

## 1. Generate objects from photographs, do not model them

Hand-built geometry was the largest single tell. Replace it with generated assets:

- **Z-Image** (RealGen) makes a *documentary product photograph of one real, aged object* on
  a plain studio backdrop. **TRELLIS** turns that reference into a mesh. **MatFuse**
  supplies tiling normal and roughness for finishes; flat textiles (rugs, wall carpets) use
  the photograph itself as the texture.
- A photograph of a real object carries the wear nobody thinks to model: chipped varnish,
  dust in a moulding, a bald walking line in a rug, rust at a thread.
- Prompt for exactly one object, its era, its material and its decay. Say what must **not**
  be in the picture: a door *without* its casing, a cabinet that hangs rather than stands,
  a frame with an *empty* opening.

## 2. Review the reference before you build on it

- Generate several seeds and compare them on a contact sheet. Re-rolling is cheap; fixing a
  wrong reference downstream is not.
- **Reject wrong form, proportion or composition outright.** Rejected in AT-18: a door with
  its frame attached, a floor-standing medicine cabinet, a 3:1 runner where a 3:2 carpet was
  needed, a son whose "dark circles" rendered as eyeliner.
- Render a **turntable** of every TRELLIS mesh (yaw 0/90/180/270) and record the yaw whose
  front faces the camera. Do not guess orientation.
- Expect reconstruction artefacts (a sliver of backdrop shadow inside a picture frame's
  opening) and remove them by rule, not by hand.

## 3. Fit to the room that is actually there

- **Ray-cast the real surface across the object's whole footprint** and stand off the most
  protruding hit. Walls have peeling paper and crack curves; a single centre ray leaves
  objects floating or buried.
- Scale uniformly by default. Allow **bounded** non-uniform stretch only (about 1.3× at
  most); beyond that mouldings and rails visibly change thickness.
- Measure features on the mesh itself (a frame's opening, a shade's mouth) instead of
  assuming them from the prompt.
- **Hide replaced objects; never delete them.** Every pass stays reversible.
- **Check the placement from the shot camera, not just in 3D.** A correctly hung photograph
  was invisible because a coat on a rack 0.26 m proud of the wall occluded the whole wall
  beyond it from the doorway camera.

## 4. Make the rooms agree with each other

- One floor material through the whole flat. Different boards in adjacent rooms read as a
  game level.
- Era-correct wall finishes: patterned wallpaper in living spaces, real paint (not flat
  colour) in service spaces.
- Tile without seams: crop the texture to its **measured repeat period** (autocorrelation),
  phase the crop between motifs, and crossfade one repeat at the edge.
- Match decay to the hero room, so the whole interior ages together.

## 5. Lighting carries the mood

- Practical, dim, warm light: **3000 K through a slight yellow filter**. 2000 K reads
  orange, not old.
- Glass fittings should emit from their own glass and not block shadow rays. Keep a bulb
  *below* an opaque shade's disc, or it shadows the entire room.
- Trim per room. After a global change, some rooms (here the kitchen and bathroom) read too
  bright and need their own factor.
- Translate the director's words into numbers and iterate: "the light should cause
  nyctophobia" → much dimmer; "too dark" → ×1.7; "kitchen and bathroom a little less" →
  ×0.7 there.

## 6. Tell a story with the objects

Props that say *who lived here* do more for realism than more props.

- Start from a **character brief** (here: an ordinary, quiet, solitary office clerk born
  about 1955) and choose objects that tell it: a studio family portrait, his own portrait,
  a work group photo where he stands apart, his parents in a frame with a black mourning
  ribbon, a New Year's Eve snapshot of him alone at the kitchen table.
- **Consistent faces from text alone** (no identity conditioning installed): repeat the same
  written description of each person in every prompt.
  - Small faces in group shots exaggerate details — a mole or "dark circles" becomes an ink
    blot or make-up. Use a plainer description for group shots.
  - Ask for the degree you mean ("just slightly weary eyes"), not the symptom ("dark
    circles"), which the model overdraws.
  - Mirror a print if needed to keep an asymmetric feature (a mole) on the same side.
- **Avoid uniformity.** Real collections never match: vary frame size and proportion (a big
  studio print, a tall portrait, a wide group photo, a small snapshot frame).

## 7. Verify every pass the same way

Each pass builds only on the **last verified bundle** and re-runs the same checks before it
counts:

- camera configuration unchanged;
- the doorway ray sweep clear (5,412 rays in AT-18);
- every previously packed image byte-identical;
- final renders made from the **reopened** saved scene, not the live session;
- a receipt (hashes, evidence) and a HANDOFF entry for the pass.

## 8. Keep the generation pipeline running safely

Never relax a guard to get a run through.

- **Z-Image VRAM margin:** foreign VRAM drifts by gigabytes. Run one object per worker and
  retry; do not lower the 512 MiB margin.
- **TRELLIS disk:** each run stages a ~1 GB copy of the background-removal model. Delete it
  only after verifying it byte-identical to the pinned source.
- **MatFuse** must run under its own environment's interpreter.
- **Windows PowerShell 5.1:** launch workers through `cmd /c ... > log 2>&1` (with
  `$ErrorActionPreference = 'Stop'`, a native tool's first stderr line kills the worker),
  and write JSON as UTF-8 **without** a BOM.
- A job killed for low memory is reported and restarted only with the user's go-ahead.
