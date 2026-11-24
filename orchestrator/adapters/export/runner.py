"""Delivery render: scale an approved output to a delivery standard and composite the
`on_playback` overlay onto it.

See CONTRACT.md for why this exists. In short: the creator accepts candidates at whatever
resolution they were generated at and chooses the delivery resolution at runtime, and the
CCTV overlay cannot be burned in before MiniMax H3 because H3 regenerates image content
and destroys it (measured 2026-11-15: erased at 352x288 and 640x480, hallucinated
non-monotonically at 512x384).

Two invariants this module exists to hold:

* **S4** -- a delivery render is derived from a request, never a `ShotOutput`. Nothing
  here appends to a shot's output list, and nothing here may be fed back into H3.
* **S6** -- export composites, scales and encodes. It never invokes Blender, ComfyUI or
  H3. If an input is missing it fails; it does not produce one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

# Reused rather than reimplemented on purpose: the overlay spec and its filter-value
# escaping are the same data and the same hazard on both sides of the degrade boundary,
# and a second copy of escaping logic is how an injection bug gets introduced.
from orchestrator.adapters.postprofile.profile import (
    OverlaySpec,
    ProfileValidationError,
    serialize_clock_drawtext_filter,
)
from orchestrator.platform.containment import ProjectRoot

#: FFmpeg scalers permitted for the upscale. CONTRACT D3 leaves the house default open;
#: `neighbor` is included deliberately because point-upscaling reproduces the blocky
#: enlargement of real low-resolution video and may be the most authentic for CCTV.
SCALE_FILTERS = frozenset({"lanczos", "bicubic", "bilinear", "neighbor", "spline"})


class DeliveryRequest(BaseModel):
    """One delivery render. Every value that cannot be derived safely is explicit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_relative: str = Field(min_length=1)
    output_relative: str = Field(min_length=1)

    #: CONTRACT D1: required, with no silent default. A default is how a 1080p export of
    #: 352x288 footage happens by accident.
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)
    scale_filter: str = "lanczos"

    #: The `on_playback` overlay to composite, or None to deliver without one.
    overlay: OverlaySpec | None = None
    font_relative: str | None = Field(None, min_length=1)

    #: CONTRACT D4b. H3 resamples time -- a 26.0 s / 10 fps source becomes an 8.0 s /
    #: 24 fps candidate taken from an offset into it -- and `H3Candidate` does not record
    #: that offset. Applying `clock_epoch` directly to the delivery timeline therefore
    #: yields a clock that is monotonic and WRONG, which is worse than the present
    #: failure because it does not announce itself. The caller must state the offset.
    clock_offset_seconds: float = Field(0.0, ge=0)

    #: Overlay geometry is authored against the recording, so at a different delivery
    #: resolution the caller decides the type size rather than this module guessing.
    #: Position expressions using `w`/`h` adapt on their own; fixed pixel offsets do not.
    fontsize: StrictInt | None = Field(None, gt=0)

    codec: str = "libx264"
    preset: str = "medium"
    crf: StrictInt = Field(18, ge=0, le=51)
    pix_fmt: str = "yuv420p"
    threads: StrictInt = Field(1, ge=1)
    audio_codec: str = "aac"
    audio_bitrate: str = "160k"

    @model_validator(mode="after")
    def _consistent(self) -> DeliveryRequest:
        if self.scale_filter not in SCALE_FILTERS:
            raise ValueError(
                f"scale_filter must be one of {sorted(SCALE_FILTERS)}, got {self.scale_filter!r}"
            )
        if self.overlay is not None:
            if self.overlay.placement != "on_playback":
                raise ValueError(
                    "delivery render composites on_playback overlays only; an in_recording "
                    "overlay is already burned into the source"
                )
            if self.font_relative is None:
                raise ValueError("compositing an overlay requires a contained font path")
        return self


class DeliveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    output_relative: str
    width: StrictInt
    height: StrictInt
    filter_graph: str


def build_delivery_filter(request: DeliveryRequest, *, font_path: str | None) -> str:
    """Scale to the delivery resolution, then composite the overlay on top of it.

    Order matters. Scaling first means the overlay is drawn at delivery resolution and
    stays crisp; drawing first and scaling after would upscale rendered glyphs, which is
    the soft, resampled text this change exists to avoid.

    Aspect ratio is preserved by padding, never by stretching: the native resolutions in
    play are not all the same shape (352x288 is 11:9 while 384x288, 512x384 and 640x480
    are 4:3), so a stretch would silently distort some shots and not others.
    """
    width, height = request.width, request.height
    parts = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags={request.scale_filter}",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
        "setsar=1/1",
    ]

    overlay = request.overlay
    if overlay is not None:
        if font_path is None:
            raise ProfileValidationError("overlay compositing requires a contained font path")
        # CONTRACT S3/D4b: the clock is data, recomputed here from `clock_epoch` plus the
        # caller-stated offset into the recording. It is never carried through a
        # generative stage as pixels.
        epoch = overlay.clock_epoch + int(request.clock_offset_seconds)
        fontsize = request.fontsize or overlay.fontsize
        parts.append(serialize_clock_drawtext_filter(
            font_path=font_path,
            overlay=overlay,
            x=overlay.x,
            y=overlay.y,
            fontsize=fontsize,
            clock_epoch=epoch,
        ))

    return ",".join(parts)


def build_delivery_command(
    ffmpeg_exe: Path,
    *,
    source: Path,
    output: Path,
    filter_graph: str,
    request: DeliveryRequest,
    has_audio: bool,
) -> list[str]:
    """Return explicit FFmpeg argv, never a shell command string."""
    argv = [
        str(ffmpeg_exe), "-hide_banner", "-nostdin", "-y",
        "-i", str(source),
        "-map", "0:v:0", "-vf", filter_graph,
        "-c:v", request.codec, "-preset", request.preset, "-crf", str(request.crf),
        "-pix_fmt", request.pix_fmt, "-threads", str(request.threads),
    ]
    if has_audio:
        # FR-13 requires H.264 + AAC. Unlike the degrade stage, which is video-only,
        # a delivery render carries the audio H3 generated.
        argv += ["-map", "0:a:0", "-c:a", request.audio_codec, "-b:a", request.audio_bitrate]
    else:
        argv += ["-an"]
    argv += ["-movflags", "+faststart", str(output)]
    return argv


class ExportAdapter:
    """Renders a delivery copy of an already-approved output.

    All reads and writes go through `ProjectRoot` (FR-25). The adapter never resolves a
    caller-supplied absolute path and never writes outside the project.
    """

    def __init__(
        self,
        root: ProjectRoot,
        ffmpeg_exe: Path,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.root = root
        self.ffmpeg_exe = ffmpeg_exe
        self.popen = popen

    def render(self, request: DeliveryRequest, *, has_audio: bool = True) -> DeliveryResult:
        source = self.root.resolve(request.source_relative)
        if not source.is_file():
            raise FileNotFoundError(f"delivery source is missing: {request.source_relative}")

        font_path = request.font_relative
        if font_path is not None and not self.root.resolve(font_path).is_file():
            raise FileNotFoundError(f"overlay font is missing: {font_path}")

        graph = build_delivery_filter(request, font_path=font_path)
        output = self.root.resolve(request.output_relative)
        output.parent.mkdir(parents=True, exist_ok=True)

        argv = build_delivery_command(
            self.ffmpeg_exe, source=source, output=output,
            filter_graph=graph, request=request, has_audio=has_audio,
        )
        # `cwd` is the project root, matching PostProfileAdapter: the filter graph carries
        # the font as a project-relative path, so FFmpeg must resolve it from the same
        # place. `shell=False` keeps this an argv launch, never a command string (FR-16).
        process = self.popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, cwd=self.root.path,
        )
        _, stderr = process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip() if stderr else ""
            raise RuntimeError(f"delivery render failed (exit {process.returncode}): {detail[-400:]}")

        return DeliveryResult(
            output_relative=request.output_relative,
            width=request.width, height=request.height, filter_graph=graph,
        )
