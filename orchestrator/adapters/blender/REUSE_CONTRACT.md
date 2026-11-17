# AT-18 shared Blender scene and camera

JobRunner-managed master renders for project Shots with the same Episode, versioned Location and camera rig
open one immutable worker-built base blend. Legacy Shots without a Location and framing
previews keep the existing path. Direct adapter renders keep their existing quarantine behavior. Shot build JSON and per-shot render outputs are retained.
No user-supplied blend paths, scripts or external links enter the worker.

Composition binds the current Episode ID, Location ID and Location version, plus the
names of the room's objects, lights and materials from compose_scene_for_location.
The render runner derives the static scene from the fully bound plan: select those
names, strip object and light animation and camera keyframes. Keep world/environment
and base camera position and target. Key the base by the scope above, camera ID,
rebuild-triggering camera fields, static bound scene including asset checksums, worker
script digest and Blender executable digest. Exclude post-only camera fields, run/shot
identity, duration and shot animation. Scope is the current project root.

Worker input plan.shared_base is absent for old paths, or has {key, scene, input, output}.
Hit: input is an authorized absolute path plus verified SHA-256, in
{path, sha256}; output is null. Miss: input null, output is an authorized absolute
per-attempt path to base.blend. The worker initializes a clean room/world/camera
from shared_base.scene, saves the base before applying shot changes, or opens the
verified base with use_scripts=False on a hit. It adds shot-only objects/materials
and updates object, light and camera animation on loaded objects. It still sets
engine, resolution, FPS and render outputs for each shot. Keep the base camera rig
and store shot-local clips in their existing directories. Imported asset bounds,
packed textures and all existing verification stay available on hit. Worker
metadata shared_base records {key, action} with action built or reused.

After worker output and master encode verify successfully, supervisor publishes
a contained immutable JSON receipt for the key. This binds the relative base blend
path and SHA-256; publish atomically with exclusive creation, resolve races by
retaining the first verified receipt. On each hit validate containment and checksum
before Blender opens it. Missing cache is a miss. Malformed receipt, changed bytes,
or inconsistent worker evidence are classified job errors. Record the receipt
identity (relative path, SHA, built/reused) in render metadata and GenerationRun
settings; retain it for project recovery. Re-render after changed Location version
gets a new base; never remove the old one or existing Shot assets.

Public-path verification: two Shots in one Episode share a Location and CameraProfile.
Their differing duration and shot seeds do not induce a rebuild. First master builds,
second reopens the exact same base path and hash; a changed Location revision causes
a new base and marks both dependent approved Shots stale while preserving old
artifacts and leaving an unrelated approved Shot intact. Test post-only camera
edits and shot animation identity when practicable. Use synthetic inputs for tests;
one bounded real Blender run should verify handoff and artifact evidence.

On hybrid graphics, the optional `binaries.blender_vulkan_device` setting selects a
Vulkan adapter for Blender's graphics rendering subprocesses by its enumerated device
ID or index. The launcher requests failure if the selected device is unavailable. The
existing renderer readback, NVML lease and project admission checks apply. When unset,
the existing Blender backend selection applies. The selector contributes to the runtime
and shared-base compatibility identities.
