# Spike 24 — FilmCraft as the editing front end

**Status:** measured, recommendation made. Fork decision deferred, deliberately.

## The question

The pipeline needs a review-and-assemble surface. The operator's stated vision is
"Adobe Premiere Pro with an AI assistant". Writing that from scratch is months of work
whose hard parts (a timeline, a trimmer, an audio mixer, a colour pipeline) are not the
parts that make this project interesting.

[FilmCraft](https://github.com/storytold/filmcraft) is a Rust NLE on egui + wgpu, dual
MIT / Apache-2.0, with a headless engine and a documented control channel. Two questions
had to be answered before committing to it:

- **Q1 — Can the orchestrator drive it programmatically**, or would integration mean GUI
  automation and screen-scraping?
- **Q2 — What does it cost on the GPU?** MiniMax H3 peaks at **11,372 MiB on a
  12,227 MiB card**, leaving **855 MiB**. The generation queue's free-VRAM precondition
  *refuses a job outright* rather than running it slowly, so an editor that holds the card
  does not slow generation down — it stops it.

## Method

Machine: RTX 5070 Ti Laptop (12,227 MiB, sm_120) with the display on the AMD Radeon 890M
iGPU, so the discrete card carries no desktop compositor. FilmCraft 0.20.1 Windows x64
portable, launched with `WGPU_POWER_PREF=low` — the configuration we would ship — and a
real project: four H3 takes (352×288 through 640×480) plus the 640×480 delivery render
with the composited overlay.

Measurement driver: `scratchpad/fc_live.py`, `fc_probe.py`, `fc_full2.py`. VRAM sampled
by `nvidia-smi` on a background thread at 0.4 s so sampling overlaps playback rather than
bracketing it; playback health read from the app's own `perf.stats`.

Every setting is confirmed by **readback**, because `ui.set` returns `ok: true` for
settings it does not apply — `playbackRes` only took effect for the value `"full"`, which
reads back as `"Full"`; `"1/1"` and `"1"` were accepted and silently ignored.

## Q1 — Programmatic control: yes, two independent routes

**Headless, project on disk.** `filmcraft-cli` with `--project ... --save` runs the
engine with no window. The whole test project was built this way: media imported, a
sequence created, five clips renamed through a batched JSONL script. More than 650
engine commands are exposed, each with an id, typed parameters and an enabled state.

**Live, into a running app.** `filmcraft --control <port>` opens a JSON-lines TCP server
on loopback, and `filmcraft-cli --bridge 127.0.0.1:<port>` forwards commands into the
session the operator is looking at. Verified live: `inspect project` returned the open
project tree with our clip names, and `exec file.newBin name=Generated` created bin 12 in
the running editor.

This is the shape the orchestrator needs. When a generation finishes, it can drop the
clip into the operator's open project without the operator doing anything, and read state
back to confirm it landed.

### Token handling

`FILMCRAFT_CONTROL_TOKEN` is 64 hex characters and authenticates **every request**, not
just a handshake. Their docs are explicit that loopback alone does not restrict clients to
the desktop owner's OS account, so the token is the only thing between any local process
and full control of the editor.

It is generated per session, held in memory, passed to the child through the inherited
environment, and injected into each request at send time. It is never a command-line
argument — the process table is world-readable — never written to a script, config or log,
and the driver scrubs it from anything it prints. **Any production integration must keep
this property.** This is also why the control script cannot be a plain JSONL file the way
the headless commands were: a file would put the token on disk.

## Q2 — GPU cost: negligible, and not the real constraint

| Condition | Discrete VRAM | Delta |
|---|---|---|
| Idle, default power preference (earlier run) | — | +274 MiB |
| Idle, `WGPU_POWER_PREF=low` (earlier run) | — | +72 … +88 MiB |
| Project loaded, not playing | 2,112 MiB | −7 MiB (noise) |
| **Playing, 1/2 preview resolution** | 2,115 MiB | **−4 MiB (noise)** |
| **Playing, full resolution** | 2,205 MiB | **+90 MiB** |

Full-resolution playback costs **90 MiB of the 855 MiB** H3 leaves spare — about 11% of
the headroom. Half-resolution playback is indistinguishable from idle.

Playback quality at full resolution was perfect: **192 of 192 frames shown, 0 dropped,
drop rate 0.0, 0 audio underruns, 0 missing audio frames.** Decode ran on hardware via
Media Foundation — 346 hardware frames, **0 software frames, 0 fallbacks** — so the iGPU's
video engine is carrying the decode and the discrete card is genuinely idle.

### The finding that actually matters

Across four runs the discrete card already had **2,094–2,119 MiB in use before FilmCraft
started.** That is Discord, Spotify, explorer.exe, WindowsTerminal, ProtonVPN and
RadeonSoftware sitting on a GPU that drives no display.

With ~2.1 GB held, only ~10,100 MiB is free. **H3 at 640×480 needs 11,372 MiB, so it
would be refused right now** — with FilmCraft closed.

Ordinary desktop apps cost **more than twenty times** what the video editor costs. The
VRAM risk was never the NLE. Any production run needs a pre-flight that reports what is
holding the card, and the operator needs to know that closing Discord buys back more
headroom than closing the editor ever would.

## What is not proven

Stated plainly, because these are the ways the recommendation could be wrong:

- **Concurrent generation was never tested.** FilmCraft playing *while* H3 generates is
  the real contention case, and it has not been run.
- **Export was not measured.** `filmcraft-cli export` takes `--gpu-rendering auto`, and a
  GPU render is a far heavier draw than playback. This is the most likely place for a
  nasty surprise.
- **The load is light.** 640×480 for 8 seconds on one video track is close to the easiest
  thing an NLE can do. A longer timeline with effects and several tracks will cost more.
- **Looping could not be enabled** — `ui.set {"program":{"loop":true}}` and
  `ui.set {"loop":true}` both read back `false`. The 14 s measurement therefore covered
  one 8 s pass plus an idle tail. The peak was still sampled during playback (~20 samples
  land inside it), so the number stands, but the steady state was not held as intended.

## Recommendation: integrate, do not fork yet

- The integration cost is low and already demonstrated end to end.
- The operator has never used a video editor, so there is no accumulated preference to
  design against. Forking to change an interface nobody has formed an opinion about is
  speculative work.
- The assistant should become the primary interface over time, with the NLE as the manual
  escape hatch. That is a reason to *integrate* rather than to fork and maintain a
  divergent editor.

  An earlier draft of this document claimed most of FilmCraft's surface — 35 multicam
  commands, 24 trim, 23 caption, 20 mixer — was irrelevant to 10–20-clip episodes. That
  was wrong about multicam, and wrong in an instructive way. `clip.createMulticam` groups
  several clips into one switchable source with `method: "in|out|timecode|marker|audio"`
  and `cameraNames: "clip|track|metadata"`; `multicam.switchAngle` then cuts between them
  frame-accurately. Several takes of one shot *are* a multicam source. The subsystem that
  looked least relevant is the one that implements take comparison, for free.
- Upstream is dual MIT / Apache-2.0, so forking stays available. Nothing about choosing
  integration now closes that door. Delaying it buys information for free.

Revisit the fork question only when a concrete need appears that the control channel
cannot serve.

**Outcome: the operator chose Route B** — the docked panel — with the above tradeoff in
front of them. The section below records what that costs, measured rather than estimated.

## Decision: Route B, with the first milestone built

### The build is not a barrier

Source clone is **81 MB**, no toolchain pin. Rust 1.96.1 `x86_64-pc-windows-msvc` and VS
Build Tools 2019 were already installed, along with the `wasm32-unknown-unknown` target.

| | |
|---|---|
| Clean release build, 24 cores | **20m 02s** |
| Incremental rebuild after a `ui-egui` change | **15m 44s** (link-dominated) |
| `clippy --release -D warnings` | **1m 44s**, exit 0 |
| `target/` on disk | **1.76 GB** |
| Binary | 78 MB, reports `filmcraft 0.20.1`, matching the shipped portable build |

The only build warnings are three pre-existing "patch … was not used in the crate graph"
notices for their vendored egui fork. They are present in the untouched baseline too.

Note for rebasing later: they pin a **forked egui** (`storytold/egui` at a fixed rev).

### The Assistant panel shell, built and verified

Branch `assistant-panel` in the clone. **3 files changed, 10 insertions, 1 deletion**,
plus one new 120-line file. Nothing else in their tree touched.

- `crates/ui-egui/src/dock.rs` — `Assistant` variant, `ALL` 28 → 29, `title()` arm
- `crates/ui-egui/src/panels.rs` — `pub mod assistant;` and the dispatch arm
- `crates/ui-egui/src/state.rs` — `AssistantDraft` field and initializer
- `crates/ui-egui/src/panels/assistant.rs` — new; transcript, composer, Send / Clear

Written to their house rules: no `unwrap`/`expect`/`panic`, every widget registers an
automation id, UI state is serde, user-visible strings through `tl!`.

**The dispatch arm is the one that bites.** `panels.rs:170` ends in a catch-all that draws
"coming in a later milestone", so a missing arm is not a compile error — it is a panel that
silently renders a placeholder.

Verified by driving the built binary over its own control channel:

1. `ui.panel.show {panel: "Assistant"}` → ok. The string mapping is generated from
   `PanelKind::ALL`, so this proves no registration was missed.
2. `Assistant` present in the dock layout.
3. `ui.elements` reported all four ids: `assistant.transcript`, `assistant.input`,
   `assistant.send`, `assistant.clear`.
4. Typing and clicking Send, entirely over the control channel.
5. `ui.inspect` returned the panel's state as serde:
   `{"input": "", "turns": [{"role": "you", …}, {"role": "assistant", …}]}`

Point 5 is the important one: **the panel is agent-readable and agent-writable from
birth**, with no plumbing beyond existing conventions.

Known gap: it docks into the Project tab group, because `app/window.rs:44-63` defaults
new panels there. A right-hand sidebar needs the explicit arm.

### What remains, in order of risk

The panel shell was the cheap part. The real work is unchanged by the route:

1. **Orchestrator operations.** `orchestrator/api/app.py` is a FastAPI app with
   declarative operation registration, a tool manifest and loopback+token enforcement —
   the same security posture as FilmCraft's control channel. It serves 24 operations, but
   **none for review/approve, export or the generation queue**. `approval.py` exists as a
   service and is not wired to the API; `export` still returns `missing_adapter`.
2. **A wasm-safe transport.** `ui-egui` has no `reqwest`, `ureq`, `tokio` or `hyper`, and
   no streaming-token UI exists anywhere in the tree. The panel compiles into
   `apps/filmcraft-web` for free, which forbids `std::thread` and blocking requests. Their
   existing pattern is per-frame polling with `ctx.request_repaint_after` (250 ms in
   `events.rs`, 100 ms in `export_mode.rs`).
3. **`cargo xtask ci`** (fmt, clippy, tests, layers, assets, wasm) has **not** been run in
   full — only `cargo fmt` and `clippy -D warnings` on `ui-egui`.

## The assistant surface: what can be added, and what it costs

### There is no extension API

FilmCraft is not VS Code. The README states plainly: **"No plugins."** The panel set is a
fixed Rust enum — AudioClipMixer, AudioMeters, EffectControls, Effects, EssentialGraphics,
History, Info, Libraries, Markers, MediaBrowser, Metadata, Program, Project, Properties,
Source, Text, Timeline, Tools — with no Assistant or Chat panel and no registry to add one
from outside. A docked sidebar means writing egui code in their tree.

The designed-in agent story is the opposite direction. Their "Built for agents" section is
explicit that the UI, CLI, control channel and MCP server all dispatch the same commands,
so an agent drives the app **from outside** rather than living inside it.

Two facts soften the fork cost if a docked panel is wanted later:

- `ui-egui` is one crate among several front ends (`automation`/MCP, `apps/filmcraft`,
  `apps/filmcraft-cli`), and **"nothing below the front ends depends on a UI toolkit or
  OS API."** Adding a panel is a contained change to one crate, not a whole-app fork.
- **"AI features. Few so far"** is listed by upstream as one of their biggest gaps. An
  assistant panel is plausibly a contribution they would take, which would mean no
  maintained fork at all.

### Three routes to the sidebar

| Route | Fork? | Gets you |
|---|---|---|
| **A. Companion window** beside FilmCraft, driving the control channel | none | everything but docking; ships now |
| **B. Assistant panel in `ui-egui`** | contained, possibly upstreamable | a real docked sidebar |
| **C. Custom front end on their engine** | large | total control; premature |

Route A first is not a throwaway. Every feature below except the chat view itself is
control-channel logic that is **identical in A and B** — the same code moves into the
panel unchanged if docking turns out to be what is missing.

### Features that need no fork at all

1. **Provenance in the Project panel.** `metadata.set` writes named fields (`Scene`,
   `Shot`, `Log Note`, `Comment`, `Camera Angle`, `Tape Name`, …) and
   `project.columns.set` chooses which appear as columns. Model, seed, workflow SHA,
   generation seconds, resolution and measured VRAM peak can sit beside each clip in the
   bin. The provenance receipts already exist; this only surfaces them.
2. **Take comparison via multicam.** `clip.createMulticam` over the takes of one shot,
   then `multicam.switchAngle` to A/B them frame-accurately.
3. **Auto-ingest on generation complete.** The orchestrator pushes the finished clip into
   the operator's open project over `--bridge`, into a Generated bin. Proven working.
4. **Shot list as markers.** Episode structure as named markers the assistant navigates.
5. **Export to the delivery standard**, wiring the existing export adapter.
6. **Retention and purge.** On finalisation (decision D5), flag the losing takes through
   metadata and reclaim the disk.
7. **GPU pre-flight** naming the processes holding the card before a run is admitted —
   directly motivated by the 2.1 GB finding above.

### Features that need the panel (route B)

8. **The chat sidebar itself**: prompt to shot, "regenerate this one darker", with the
   clip under the playhead as implicit context.
9. **Inline approve / reject** on the review queue.
10. **Live generation queue** with progress and ETA.

The assistant already has genuine context without any fork: `ui.inspect` reports the
selection, playhead and active sequence, `metadata.get` reads a clip's provenance, and
`ui.screenshot` shows what the operator is actually looking at — the equivalent of VS
Code handing an extension the active file.

## Next

1. Measure FilmCraft during an actual H3 generation — the untested contention case.
2. Measure `filmcraft-cli export`, and decide whether exports must hold the generation
   queue. Note the README says colour grading, keys and export rendering still run on the
   **CPU**, with hardware H.264/H.265 encoding opt-in via Export ▸ Hardware encoding — so
   the GPU contention risk here is smaller than first assumed, but still unmeasured.
3. Add a GPU pre-flight that names the processes holding the card before a run is admitted.
4. Add the review/approve, export and generation-queue operations to
   `orchestrator/api/app.py`, wiring the existing `approval.py` service through.
5. Choose and build the wasm-safe transport, then connect the panel to those operations.
6. Run `cargo xtask ci` in full before treating the fork as green.
