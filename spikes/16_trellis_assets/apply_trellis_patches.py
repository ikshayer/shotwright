"""Apply the Trellis2-GGUF library patches, reversibly.

Replicates apply_patches() from the pack's install.py, which cannot be re-run here
because its wheel resolution picks Linux wheels on Windows and fails before reaching the
patch step.

The pack's version copies over the installed file with no backup. This keeps a
.orig-trellis2 copy beside each target first, so the edit can be undone, and refuses
rather than guessing if a target is missing or already patched.

Run with the Trellis venv's interpreter: the libraries must be importable.
"""

from __future__ import annotations

import filecmp
import importlib.util
import os
import shutil
import sys
from pathlib import Path

PACK = Path(
    r"D:\Work\comfyui-trellis\ComfyUI\custom_nodes\ComfyUI-Trellis2-GGUF"
)
PATCH_DIR = PACK / "patch"

# filename in patch/ -> target library, exactly as install.py declares it
PATCH_MAPPING = {
    "remeshing.py": "cumesh",
    "flexible_dual_grid.py": "o_voxel",
}

BACKUP_SUFFIX = ".orig-trellis2"


def library_dir(name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(name)
    except Exception as exc:  # noqa: BLE001 - report which library and continue
        print(f"  error locating {name}: {exc}")
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent


def find_target(root: Path, filename: str) -> Path | None:
    for base, _dirs, files in os.walk(root):
        if filename in files:
            return Path(base) / filename
    return None


def main() -> int:
    if not PATCH_DIR.is_dir():
        print(f"REFUSED: patch directory absent at {PATCH_DIR}")
        return 1

    failures = 0
    for patch_file, lib_name in PATCH_MAPPING.items():
        print(f"\n--- {patch_file} -> {lib_name} ---")
        src = PATCH_DIR / patch_file
        if not src.is_file():
            print(f"  REFUSED: {src} missing")
            failures += 1
            continue

        lib_path = library_dir(lib_name)
        if lib_path is None:
            print(f"  REFUSED: library {lib_name!r} is not importable - install its wheel first")
            failures += 1
            continue
        print(f"  library at {lib_path}")

        target = find_target(lib_path, patch_file)
        if target is None:
            print(f"  REFUSED: no {patch_file} found inside {lib_path}; version mismatch")
            failures += 1
            continue

        if filecmp.cmp(src, target, shallow=False):
            print(f"  already patched, identical: {target}")
            continue

        backup = target.with_name(target.name + BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(target, backup)
            print(f"  backed up original -> {backup.name}")
        else:
            print(f"  backup already present, keeping it: {backup.name}")

        shutil.copy2(src, target)
        print(f"  patched {target}")
        print(f"    {src.stat().st_size} bytes written")

    print(f"\n{len(PATCH_MAPPING) - failures}/{len(PATCH_MAPPING)} patches applied")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
