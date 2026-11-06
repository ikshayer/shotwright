"""Measure the clipped-highlight fraction that spike 10 identified as the remaining gap."""

import sys
from pathlib import Path

from PIL import Image


def stats(path: Path) -> tuple[float, float]:
    im = Image.open(path).convert("L")
    px = list(im.getdata())
    n = len(px)
    clipped = sum(1 for v in px if v >= 242)      # >= 95% luminance
    near_black = sum(1 for v in px if v <= 13)    # <= 5%
    return 100.0 * clipped / n, 100.0 * near_black / n


for arg in sys.argv[1:]:
    p = Path(arg)
    if not p.is_file():
        print(f"{p.name:34} MISSING")
        continue
    c, b = stats(p)
    print(f"{p.name:34} clipped {c:6.3f}%   near-black {b:6.2f}%")
