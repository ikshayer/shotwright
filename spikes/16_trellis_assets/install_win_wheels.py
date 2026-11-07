"""Install the Trellis2 native CUDA wheels, correctly, for Windows.

Why this exists instead of just running the pack's install.py:

install.py builds its wheel-matching regex as
    ...-{python_tag}-{python_tag}-[^"\\s]+\\.whl
leaving the PLATFORM tag unconstrained. It computes platform="win_amd64" in
get_env_info() and then never uses it in the pattern, so on Windows it resolves the
manylinux wheels that happen to appear first in the index. pip rejects those outright
("not a supported wheel on this platform"), so the documented install cannot work here.

This resolves the same index but requires win_amd64, and refuses rather than guessing if
an exact match is missing. It prints every URL before installing so the binary source is
on the record.
"""

from __future__ import annotations

import re
import subprocess
import sys
import urllib.request

VENV_PY = r"D:\Work\comfyui-trellis\.venv\Scripts\python.exe"

# install.py's own package list, plus flash-attn which it handles separately.
LIBS = ["flash-attn", "cumesh", "flex-gemm", "nvdiffrast", "nvdiffrec-render", "o-voxel"]

CUDA = "cu128"
TORCH = "torch2.8"
PYTAG = "cp311"
PLATFORM = "win_amd64"

INDEX = "https://pozzettiandrea.github.io/cuda-wheels/{lib}/"
UA = "cctv-analog-horror-pipeline/0.1 (wheel resolution)"
WHEEL_URL = re.compile(r'https://github\.com/[^"\s]+\.whl')


def resolve(lib: str) -> str:
    req = urllib.request.Request(INDEX.format(lib=lib), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8", errors="replace")

    candidates = sorted(set(WHEEL_URL.findall(body)))
    matches = [
        u for u in candidates
        if CUDA in u
        and TORCH in u.replace("%2B", "+")
        and f"-{PYTAG}-{PYTAG}-" in u
        and PLATFORM in u
    ]
    if len(matches) != 1:
        raise SystemExit(
            f"REFUSED {lib}: expected exactly 1 wheel for "
            f"{PLATFORM}/{CUDA}/{TORCH}/{PYTAG}, found {len(matches)}.\n"
            + "\n".join(f"  {m}" for m in matches)
        )
    return matches[0]


def main() -> None:
    print(f"Resolving {len(LIBS)} wheels for {PLATFORM} / {CUDA} / {TORCH} / {PYTAG}\n")
    urls = []
    for lib in LIBS:
        url = resolve(lib)
        print(f"  {lib:18} {url.split('/')[-1]}")
        urls.append(url)

    print("\nBinary source (NOT PyPI): github.com/PozzettiAndrea/cuda-wheels releases\n")
    print("Installing all six in one pip invocation so resolution is consistent...\n",
          flush=True)

    proc = subprocess.run(
        [VENV_PY, "-m", "pip", "install", *urls],
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"pip failed with exit {proc.returncode}")
    print("\nwheels installed")


if __name__ == "__main__":
    sys.exit(main())
