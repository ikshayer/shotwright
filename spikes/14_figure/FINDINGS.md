# Spike 14 — FR-20 humanoid silhouette

## Visual verdict

At **1280×960**, the after image reads clearly as a person in shadow: the round head,
shoulder/torso mass, and hanging arms form a human outline without facial detail. The
sofa hides most of the lower body. At **352×288**, the person read survives but is less
immediate: the head and upper-body outline carry it while the sofa swallows the legs. It
is more person than furniture, but still a simplified procedural silhouette rather than
a convincing natural body.

The before proxy reads as a tall rectangular object at both sizes. The after stills put
the figure against the bright window to match the reference's silhouette contrast.

| Surface | Before | After |
|---|---|---|
| Master, 1280×960 | [before-master-1280x960.png](before-master-1280x960.png) | [after-master-1280x960.png](after-master-1280x960.png) |
| Low-light output, 352×288 | [before-delivered-352x288.png](before-delivered-352x288.png) | [after-delivered-352x288.png](after-delivered-352x288.png) |

The matched sample is frame 211 (3.5 s), the midpoint of a two-second `linear_walk_path`
beginning at 2.5 s in the low-key living room. The declared envelope is 1.8 m high,
0.5 m wide, 0.35 m deep, with a 2.0 m minimum distance and a 400 px maximum projected
height. The 352×288 still was produced with the Low-light B&W filter graph on a looped
copy of the selected master frame; it includes scaling, overlays, noise, encoding, and
the profile's 12:11 sample aspect ratio.

## Unmeasured placeholder proportions

The implementation uses a sphere torso at `(0.62W, 0.72D, 0.46H)`, a smooth sphere head
at `(0.42W, 0.56D, 0.14H)`, box arms at `(0.20W, 0.66D, 0.34H)`, box legs at
`(0.26W, 0.58D, 0.47H)`, and box feet at `(0.30W, 1.00D, 0.045H)`. All centers are
fixed offsets from the envelope center; the feet and arms reach the declared depth and
width limits, and the head/feet preserve the full declared height.

The Blender worker reported **43.4 s** for the before still and **43.6 s** for the after
still, including scene setup and the single render. The render metadata confirms Blender
5.2.0 LTS, `BLENDER_EEVEE`, and the RTX 5070 Ti Laptop GPU.

Reproduce with `python spikes/14_figure/render_figure.py before` and then `after`.
