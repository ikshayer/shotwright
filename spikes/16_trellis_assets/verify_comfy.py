"""Wait for the Trellis ComfyUI instance on :8189 and verify the workflow's nodes exist.

This is the acceptance test for the install: the five node classes the supplied workflow
(Trellis2Multiviews_GGUF.json) references must all be registered. If a custom node failed
to import, ComfyUI still starts and simply omits it, so a running server proves nothing
on its own.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8189"
DEADLINE_S = 240

REQUIRED = [
    "Trellis2LoadModel_GGUF",
    "Trellis2LoadImageWithTransparency",
    "Trellis2MeshWithVoxelMultiViewGenerator",
    "Trellis2PostProcessAndUnWrapAndRasterizer",
    "Trellis2ExportMesh",
]


def get_json(path: str, timeout: float = 20.0):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "install-check/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def main() -> int:
    started = time.monotonic()
    stats = None
    while time.monotonic() - started < DEADLINE_S:
        try:
            stats = get_json("/system_stats")
            break
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(3.0)

    if stats is None:
        print(f"FAILED: :8189 did not answer within {DEADLINE_S}s")
        return 1

    waited = time.monotonic() - started
    system = stats.get("system", {})
    print(f"server up after {waited:.0f}s")
    print(f"  comfyui_version : {system.get('comfyui_version')}")
    print(f"  python          : {str(system.get('python_version', '')).split()[0]}")
    print(f"  pytorch         : {system.get('pytorch_version')}")
    for dev in stats.get("devices", []):
        total = dev.get("vram_total", 0) / 2**20
        free = dev.get("vram_free", 0) / 2**20
        print(f"  device          : {dev.get('name')}  {total:.0f} MiB total, {free:.0f} MiB free")

    print("\nchecking registered node classes...")
    info = get_json("/object_info", timeout=120.0)
    print(f"  {len(info)} node classes registered in total")

    missing = [n for n in REQUIRED if n not in info]
    for name in REQUIRED:
        print(f"  {'OK  ' if name in info else 'MISS'}  {name}")

    trellis = sorted(k for k in info if "trellis" in k.lower())
    print(f"\n  {len(trellis)} Trellis2 node classes present")

    if missing:
        print(f"\nFAILED: {len(missing)} required node(s) missing: {missing}")
        return 1

    print("\nPASS: every node the supplied workflow needs is registered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
