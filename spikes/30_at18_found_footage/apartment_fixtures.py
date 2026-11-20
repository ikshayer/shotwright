"""Original period furnishings for the connected-apartment walkthrough."""

from __future__ import annotations

import math
from typing import Any


def _surface(prim: Any, name: str, rgb: tuple[float, float, float], *, roughness: float = 0.7,
             wear: float = 0.75, grain: tuple[float, float, float] = (32, 32, 24)) -> Any:
    material = prim._material(name, rgb, roughness=roughness)
    prim._surface_detail(material, grain, strength=0.30, distance=0.007, dark_level=wear)
    return material


def _ring(cx: float, cy: float, z: float, width: float, depth: float,
          *, exponent: float = 1.0, steps: int = 40) -> list[tuple[float, float, float]]:
    points = []
    for i in range(steps):
        angle = math.tau * i / steps
        c, s = math.cos(angle), math.sin(angle)
        points.append((
            cx + width * 0.5 * math.copysign(abs(c) ** exponent, c),
            cy + depth * 0.5 * math.copysign(abs(s) ** exponent, s),
            z,
        ))
    return points


def _well(prim: Any, name: str, center: tuple[float, float], material: Any,
          sections: tuple[tuple[float, float, float], ...], *, exponent: float = 0.43) -> Any:
    """Turn outer wall, rounded lip, and interior into one hollow enamel mesh."""
    steps = 40
    x, y = center
    vertices = [
        point for width, depth, z in sections
        for point in _ring(x, y, z, width, depth, exponent=exponent, steps=steps)
    ]
    faces = [tuple(reversed(range(steps)))]
    for level in range(len(sections) - 1):
        a, b = level * steps, (level + 1) * steps
        for i in range(steps):
            nxt = (i + 1) % steps
            faces.append((a + i, a + nxt, b + nxt, b + i))
    if sections[-1][0] < sections[-2][0]:
        faces.append(tuple(range(len(vertices) - steps, len(vertices))))
    return prim._mesh(name, vertices, faces, material)


def _handle(prim: Any, name: str, x: float, y: float, z: float, material: Any, *,
            outward: float, width: float = 0.105) -> None:
    prim._line(name, [(x, y - width / 2, z), (x + outward * 0.024, y - width / 2, z),
                      (x + outward * 0.024, y + width / 2, z), (x, y + width / 2, z)],
               material, 0.006)


def _crt(prim: Any, veneer: Any, bakelite: Any, chrome: Any, glass: Any) -> None:
    box, line = prim._box, prim._line
    x, y, z = -0.84, -2.64, 0.765
    box("CRT rear veneered cabinet", (x, y, z), (0.37, 0.59, 0.43), veneer, bevel=0.044)
    box("CRT thick plastic front", (x - 0.185, y, z), (0.065, 0.58, 0.41), bakelite, bevel=0.025)
    for dx in (-0.11, 0.11):
        for dy in (-0.19, 0.19):
            box("Receiver rubber foot", (x + dx, y + dy, 0.536), (0.045, 0.047, 0.027), bakelite,
                bevel=0.008)
    box("Pressed glass tube surround", (x - 0.221, y - 0.043, z), (0.014, 0.465, 0.343), chrome,
        bevel=0.011)

    slices, rings = 36, 6
    vertices = [(x - 0.256, y - 0.043, z)]
    for row in range(1, rings + 1):
        r = row / rings
        for i in range(slices):
            angle = math.tau * i / slices
            c, s = math.cos(angle), math.sin(angle)
            vertices.append((
                x - 0.256 + 0.028 * r ** 2,
                y - 0.043 + 0.215 * r * math.copysign(abs(c) ** 0.58, c),
                z + 0.151 * r * math.copysign(abs(s) ** 0.58, s),
            ))
    faces = [(0, 1 + (i + 1) % slices, 1 + i) for i in range(slices)]
    for row in range(rings - 1):
        a, b = 1 + row * slices, 1 + (row + 1) * slices
        for i in range(slices):
            nxt = (i + 1) % slices
            faces.append((a + i, a + nxt, b + nxt, b + i))
    prim._mesh("Curved inactive CRT glass", vertices, faces, glass)
    for idx in range(5):
        box("CRT speaker grille slot", (x - 0.222, y + 0.224, 0.671 + idx * 0.021),
            (0.007, 0.078, 0.006), chrome, bevel=0.002)
    for height in (0.822, 0.91):
        dial = [(x - 0.228, y + 0.22 + 0.024 * math.cos(i * math.tau / 24),
                 height + 0.024 * math.sin(i * math.tau / 24)) for i in range(24)]
        line("CRT tuning dial", dial, chrome, 0.010, closed=True)
    line("Receiver mains lead", [(-0.653, -2.88, 0.662), (-0.642, -2.94, 0.322),
                                 (-0.638, -3.12, 0.063), (-0.638, -3.22, 0.128)], bakelite, 0.004)


def _living(prim: Any, veneer: Any, chrome: Any, bakelite: Any, has_living_sofa: bool) -> None:
    box, line = prim._box, prim._line
    wool = _surface(prim, "Faded madder living-room wool", (0.29, 0.102, 0.082), roughness=0.93)
    pattern = _surface(prim, "Dusty wool figure yarn", (0.46, 0.32, 0.18), roughness=0.95)
    ink = _surface(prim, "Dark wool border yarn", (0.13, 0.081, 0.05), roughness=0.96)
    box("Living room low-pile woven rug", (-2.02, -2.06, 0.011), (1.68, 1.72, 0.018), wool, bevel=0.008)
    for inset, tone in ((0.055, pattern), (0.10, ink), (0.15, pattern)):
        x0, x1 = -2.86 + inset, -1.18 - inset
        y0, y1 = -2.92 + inset, -1.20 - inset
        line("Woven rug border", [(x0, y0, 0.022), (x1, y0, 0.022), (x1, y1, 0.022),
                                 (x0, y1, 0.022)], tone, 0.006, closed=True)
    for x, y in ((-2.48, -1.49), (-1.56, -1.49), (-2.48, -2.63), (-1.56, -2.63)):
        line("Rug woven diamond", [(x - 0.115, y, 0.022), (x, y + 0.09, 0.022),
                                   (x + 0.115, y, 0.022), (x, y - 0.09, 0.022)],
             pattern, 0.007, closed=True)

    if not has_living_sofa:
        fabric = _surface(prim, "Faded green check upholstery", (0.195, 0.19, 0.118),
                          roughness=0.91, grain=(53, 62, 51))
        seam = _surface(prim, "Dark upholstered welting", (0.104, 0.096, 0.059), roughness=0.98)
        box("Fallback sofa wooden plinth", (-3.13, -1.88, 0.115), (0.82, 1.70, 0.18), veneer,
            bevel=0.023)
        box("Fallback sofa upholstered base", (-3.11, -1.88, 0.28), (0.89, 1.81, 0.25), fabric, bevel=0.065)
        box("Fallback sofa curved upholstered back", (-3.47, -1.88, 0.59), (0.19, 1.74, 0.58), fabric,
            bevel=0.080)
        for y in (-2.672, -1.088):
            box("Fallback sofa rolled arm", (-3.08, y, 0.456), (0.84, 0.208, 0.36), fabric, bevel=0.088)
        for i, y in enumerate((-2.295, -1.88, -1.465)):
            box("Fallback sofa seat cushion", (-3.04, y, 0.452 + 0.006 * (i % 2)),
                (0.71, 0.402, 0.155), fabric, bevel=0.062, angle=0.013 * (i - 1))
            line("Fallback sofa seat welt", [(-2.703, y - 0.17, 0.448), (-2.691, y, 0.445),
                                            (-2.703, y + 0.17, 0.449)], seam, 0.004)
        box("Loose sofa cushion", (-3.22, -2.34, 0.646), (0.22, 0.36, 0.31), wool, bevel=0.083, angle=0.17)

    box("Varnished television sideboard", (-0.84, -2.64, 0.292), (0.46, 1.04, 0.46), veneer, bevel=0.022)
    box("Television sideboard polished lip", (-0.85, -2.64, 0.528), (0.48, 1.07, 0.045), veneer, bevel=0.012)
    for y in (-2.89, -2.39):
        box("Inset television sideboard door", (-1.081, y, 0.302), (0.027, 0.473, 0.348), veneer,
            bevel=0.007)
        _handle(prim, "Sideboard tarnished pull", -1.098, y, 0.40, chrome, outward=-1)
    _crt(prim, veneer, bakelite, chrome,
         prim._material("Unlit convex phosphor glass", (0.028, 0.039, 0.034), roughness=0.22))
    box("Small coffee table top", (-2.03, -2.26, 0.49), (0.58, 0.76, 0.047), veneer,
        bevel=0.026, angle=0.065)
    for x in (-2.25, -1.81):
        for y in (-2.55, -1.97):
            box("Coffee table splayed foot", (x, y, 0.25), (0.049, 0.047, 0.46), veneer, bevel=0.008)
    box("Old volume on table", (-2.19, -2.40, 0.527), (0.20, 0.27, 0.024), bakelite,
        bevel=0.003, angle=-0.22)
    box("Yellowed book block", (-2.194, -2.40, 0.528), (0.184, 0.257, 0.018), pattern,
        bevel=0.002, angle=-0.22)
    box("Side table paper handbill", (-1.89, -2.13, 0.518), (0.16, 0.23, 0.003), pattern,
        bevel=0.001, angle=0.28)

    box("Living-room hanging carpet backing", (-2.61, -3.356, 1.39), (1.62, 0.029, 1.04), wool,
        bevel=0.008)
    for dx, dz, tone in ((0.73, 0.44, ink), (0.68, 0.40, pattern), (0.62, 0.34, ink)):
        line("Hanging carpet woven edge", [(-2.61 - dx, -3.333, 1.39 - dz), (-2.61 + dx, -3.333, 1.39 - dz),
                                          (-2.61 + dx, -3.333, 1.39 + dz), (-2.61 - dx, -3.333, 1.39 + dz)],
             tone, 0.007, closed=True)
    for i in range(5):
        x = -3.11 + i * 0.25
        line("Carpet woven lozenge", [(x, -3.331, 1.62), (x + 0.098, -3.331, 1.40),
                                    (x, -3.331, 1.18), (x - 0.098, -3.331, 1.40)],
             pattern, 0.011, closed=True)


def _kitchen(prim: Any, enamel: Any, chrome: Any, bakelite: Any, veneer: Any) -> None:
    box, line = prim._box, prim._line
    paint = _surface(prim, "Aged pistachio kitchen cabinet paint", (0.33, 0.38, 0.255), roughness=0.78)
    edge = _surface(prim, "Yellowed loose laminate", (0.38, 0.32, 0.209), roughness=0.55)
    oxides = _surface(prim, "Brown oxide exposed through enamel", (0.24, 0.12, 0.071), roughness=0.91)
    for y, width in ((-0.82, 0.76), (-2.37, 0.40)):
        # The cabinet has open space behind its fronts so the sink well is actually hollow.
        body_height = 0.59 if width > 0.5 else 0.74
        box("Kitchen cupboard painted body", (2.355, y, 0.065 + body_height / 2),
            (0.585, width, body_height), paint, bevel=0.014)
        box("Kitchen recessed plinth", (2.39, y, 0.068), (0.48, width, 0.10), veneer, bevel=0.010)
        for dy in (-width * 0.24, width * 0.24):
            box("Kitchen cupboard inset door", (2.047, y + dy, 0.455), (0.028, width * 0.44, 0.65),
                paint, bevel=0.009)
            _handle(prim, "Kitchen flattened cabinet pull", 2.026, y + dy, 0.622, chrome,
                    outward=-1, width=0.08)
    box("Kitchen laminate prep surface", (2.348, -2.38, 0.83), (0.637, 0.414, 0.055), edge, bevel=0.014)
    box("Sink front enamel drainboard", (2.067, -0.818, 0.827), (0.105, 0.773, 0.044), enamel,
        bevel=0.011)
    box("Sink narrow rear lip", (2.634, -0.818, 0.838), (0.073, 0.773, 0.057), enamel, bevel=0.009)
    for y in (-1.17, -0.466):
        box("Sink enamel side flange", (2.358, y, 0.829), (0.58, 0.102, 0.049), enamel, bevel=0.010)
    _well(prim, "Cream enamel kitchen sink bowl", (2.351, -0.823), enamel,
          ((0.39, 0.48, 0.665), (0.53, 0.64, 0.818), (0.53, 0.64, 0.848),
           (0.478, 0.583, 0.850), (0.453, 0.557, 0.821), (0.349, 0.44, 0.680)))
    line("Sink chrome well drain", _ring(2.34, -0.83, 0.687, 0.044, 0.044), chrome, 0.004, closed=True)
    line("Old sink curved gooseneck", [(2.575, -0.82, 0.845), (2.575, -0.82, 1.055),
                                     (2.525, -0.82, 1.083), (2.385, -0.82, 1.083),
                                     (2.349, -0.82, 1.05), (2.349, -0.82, 1.023)], chrome, 0.013)
    for y in (-0.929, -0.711):
        line("Sink cross-head tap", [(2.575, y, 0.849), (2.575, y, 0.932)], chrome, 0.012)
        line("Sink tap grip", [(2.550, y, 0.945), (2.601, y, 0.945)], chrome, 0.006)

    box("Freestanding cream enamel gas range", (2.348, -1.786, 0.428), (0.627, 0.60, 0.81), enamel,
        bevel=0.019)
    box("Range enamel hob lip", (2.347, -1.786, 0.841), (0.641, 0.61, 0.048), enamel, bevel=0.012)
    box("Inset range oven door", (2.018, -1.786, 0.441), (0.035, 0.535, 0.467), edge, bevel=0.015)
    box("Dark oven inspection glass", (1.997, -1.786, 0.421), (0.019, 0.413, 0.249), bakelite,
        bevel=0.024)
    _handle(prim, "Oven door dark squared handle", 1.991, -1.786, 0.594, bakelite, outward=-1, width=0.40)
    for i in range(4):
        box("Range bakelite selector", (2.004, -1.985 + i * 0.132, 0.75), (0.025, 0.044, 0.038),
            bakelite, bevel=0.014)
    for x in (2.20, 2.47):
        for y in (-1.93, -1.64):
            line("Cast-iron hob burner", _ring(x, y, 0.884, 0.15, 0.15), bakelite, 0.008, closed=True)
            for angle in (0, math.pi / 2):
                box("Bent burner support", (x, y, 0.901), (0.20, 0.017, 0.020), bakelite,
                    bevel=0.005, angle=angle)
    for i, (y, z) in enumerate(((-1.69, 0.51), (-1.99, 0.25), (-1.57, 0.14), (-1.89, 0.62))):
        line("Range enamel chip", [(1.99, y, z), (1.99, y + 0.009, z + 0.003),
                                  (1.991, y + 0.015 + 0.002 * i, z - 0.003)], oxides, 0.002)

    box("Short reclaimed kitchen shelf", (2.49, -2.35, 1.59), (0.37, 0.46, 0.035), veneer, bevel=0.011)
    for y in (-2.48, -2.22):
        line("Kitchen shelf folded bracket", [(2.62, y, 1.43), (2.347, y, 1.569),
                                            (2.62, y, 1.57)], chrome, 0.007)
    _well(prim, "Cream enamel cooking saucepan", (2.2, -1.94), enamel,
          ((0.145, 0.145, 0.911), (0.18, 0.18, 1.026), (0.18, 0.18, 1.037),
           (0.162, 0.162, 1.035), (0.14, 0.14, 0.927)), exponent=1)
    line("Saucepan dark phenolic handle", [(2.12, -1.94, 0.98), (2.078, -1.94, 0.984),
                                          (1.999, -1.94, 0.999)], bakelite, 0.012)
    box("Old wooden kitchen cutting board", (2.245, -2.41, 0.877), (0.27, 0.224, 0.021),
        veneer, bevel=0.018, angle=-0.16)
    _well(prim, "Kitchen chipped soap dish", (2.436, -2.33), enamel,
          ((0.09, 0.06, 0.862), (0.11, 0.079, 0.888), (0.097, 0.067, 0.892),
           (0.074, 0.051, 0.873)))


def _bath(prim: Any, enamel: Any, chrome: Any, bakelite: Any) -> None:
    box, line = prim._box, prim._line
    sanitary = _surface(prim, "Mottled white bathroom china", (0.61, 0.59, 0.51), roughness=0.31, wear=0.86)
    oxide = _surface(prim, "Lime and oxide stains", (0.29, 0.19, 0.109), roughness=0.85, wear=0.72)
    deep = prim._material("Dark unlit drain recess", (0.055, 0.050, 0.036), roughness=0.48)
    _well(prim, "Deep enamel bathtub and rolled rim", (1.905, -3.991), enamel,
          ((0.40, 1.28, 0.105), (0.61, 1.52, 0.543), (0.63, 1.54, 0.566),
           (0.62, 1.53, 0.591), (0.567, 1.468, 0.596), (0.536, 1.438, 0.560),
           (0.384, 1.224, 0.165)))
    for y in (-4.50, -3.48):
        for x in (1.74, 2.07):
            box("Cast tub foot", (x, y, 0.057), (0.108, 0.101, 0.112), chrome, bevel=0.019)
    for z, tone, radius in ((0.176, oxide, 0.003), (0.18, chrome, 0.004)):
        line("Tub bath drain ring", _ring(1.908, -3.452, z, 0.063, 0.063), tone, radius, closed=True)
    for y in (-3.33, -3.48):
        line("Old bath surface water line", [(2.219, y, 0.10), (2.219, y, 0.81),
                                           (2.171, y, 0.84), (2.1, y, 0.84)], chrome, 0.010)
        line("Bath water tap grip", [(2.077, y - 0.024, 0.845), (2.077, y + 0.024, 0.845)],
             chrome, 0.007)
    line("Bath wall spout", [(2.136, -3.408, 0.785), (2.061, -3.408, 0.786),
                            (2.02, -3.408, 0.753), (2.02, -3.408, 0.715)], chrome, 0.015)
    line("Dangling loop of handheld shower hose", [(2.133, -3.408, 0.755), (2.08, -3.504, 0.543),
                                                  (2.062, -3.566, 0.515), (2.115, -3.61, 0.564),
                                                  (2.183, -3.61, 0.875), (2.219, -3.61, 1.198)],
         chrome, 0.007)
    box("Old shower handset", (2.218, -3.602, 1.234), (0.035, 0.052, 0.15), chrome, bevel=0.016)
    line("Tub enamel hairline wear", [(1.612, -4.404, 0.572), (1.609, -4.393, 0.577),
                                    (1.608, -4.386, 0.584), (1.612, -4.376, 0.59)], oxide, 0.002)

    box("High toilet cistern", (1.006, -4.674, 1.093), (0.37, 0.204, 0.318), sanitary, bevel=0.035)
    box("Cistern tank cover", (1.006, -4.672, 1.263), (0.383, 0.213, 0.04), sanitary, bevel=0.011)
    line("Exposed cistern flush pipe", [(1.006, -4.602, 0.932), (1.006, -4.602, 0.469),
                                       (1.006, -4.562, 0.409), (1.006, -4.487, 0.372)], chrome, 0.024)
    line("Chrome cistern pull chain", [(1.153, -4.58, 1.243), (1.154, -4.58, 1.064),
                                     (1.161, -4.578, 0.925)], chrome, 0.002)
    box("Brown cistern chain pull", (1.16, -4.578, 0.916), (0.027, 0.023, 0.053), bakelite, bevel=0.008)
    _well(prim, "Toilet pedestal and open china bowl", (1.035, -4.425), sanitary,
          ((0.25, 0.30, 0.005), (0.23, 0.29, 0.22), (0.351, 0.48, 0.374),
           (0.360, 0.488, 0.415), (0.305, 0.426, 0.419), (0.274, 0.374, 0.367),
           (0.187, 0.238, 0.254)), exponent=0.71)
    line("Yellowed toilet seat", _ring(1.035, -4.425, 0.433, 0.334, 0.46, exponent=0.71),
         bakelite, 0.016, closed=True)

    _well(prim, "Wall-hung hand basin and rim", (1.021, -3.018), sanitary,
          ((0.27, 0.22, 0.705), (0.445, 0.35, 0.824), (0.461, 0.37, 0.847),
           (0.397, 0.311, 0.854), (0.364, 0.281, 0.815), (0.224, 0.175, 0.727)))
    line("Basin waste trap", [(1.021, -3.002, 0.708), (1.021, -3.002, 0.558),
                             (1.021, -2.983, 0.537), (1.021, -2.887, 0.543),
                             (1.021, -2.874, 0.625), (1.021, -2.824, 0.627)], chrome, 0.017)
    line("Basin bent chrome tap", [(1.021, -2.884, 0.849), (1.021, -2.884, 0.963),
                                  (1.021, -2.911, 0.974), (1.021, -2.984, 0.96),
                                  (1.021, -3.003, 0.928)], chrome, 0.012)
    line("Basin drain recess", _ring(1.021, -3.024, 0.732, 0.033, 0.033), deep, 0.005, closed=True)
    mirror = prim._material("Clouded bathroom mirror", (0.37, 0.405, 0.39), roughness=0.31, metal=0.74)
    box("Chipped wall mirror frame", (1.023, -2.834, 1.334), (0.389, 0.031, 0.49), enamel, bevel=0.018)
    box("Wall mirror silvered glass", (1.023, -2.853, 1.334), (0.337, 0.009, 0.441), mirror, bevel=0.010)
    line("Hairline crack in mirror corner", [(1.115, -2.859, 1.552), (1.107, -2.859, 1.487),
                                            (1.124, -2.859, 1.446), (1.107, -2.859, 1.405)],
         deep, 0.0012)


def dress_rooms(prim: Any, scene: Any, *, has_living_sofa: bool) -> dict[str, object]:
    """Dress the architectural rooms without changing any camera, wall, or light."""
    veneer = _surface(prim, "Scuffed dark varnished veneer", (0.183, 0.099, 0.057), roughness=0.44,
                      wear=0.56, grain=(52, 5, 31))
    chrome = prim._material("Pitted nickel-plated fixture hardware", (0.38, 0.35, 0.29),
                            roughness=0.46, metal=0.59)
    bakelite = _surface(prim, "Scrolled dark brown Bakelite", (0.076, 0.052, 0.031), roughness=0.49)
    enamel = _surface(prim, "Chipped cream enamel", (0.56, 0.522, 0.405), roughness=0.33, wear=0.81)
    before = len(scene.objects)
    _living(prim, veneer, chrome, bakelite, has_living_sofa)
    living_count = len(scene.objects) - before
    before = len(scene.objects)
    _kitchen(prim, enamel, chrome, bakelite, veneer)
    kitchen_count = len(scene.objects) - before
    before = len(scene.objects)
    _bath(prim, enamel, chrome, bakelite)
    return {
        "asset_rights": "original procedural furnishings",
        "living_sofa": "provided asset" if has_living_sofa else "original procedural sofa",
        "added_objects": {
            "living_room": living_count,
            "kitchen": kitchen_count,
            "bathroom": len(scene.objects) - before,
        },
        "placement_m": {
            "coffee_table_xy": (-2.03, -2.26),
            "tv_console_xy": (-0.84, -2.64),
            "kitchen_fixtures_min_x": 1.94,
            "bath_tub_min_x": 1.59,
            "bath_toilet_max_y": -4.17,
            "bath_basin_min_y": -3.21,
        },
    }

