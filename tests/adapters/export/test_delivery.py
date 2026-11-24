"""Delivery render contract — see orchestrator/adapters/export/CONTRACT.md §5."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.adapters.export import (
    DeliveryRequest,
    build_delivery_command,
    build_delivery_filter,
)
from orchestrator.adapters.postprofile.profile import OverlaySpec

#: 2005-11-14 16:00:00 UTC, the epoch the demo project's degrade stage uses.
EPOCH = 1131984000


def _overlay(**kwargs):
    base = dict(camera_id="CAM 03", clock_epoch=EPOCH, fontsize=16,
                x=8, y="h-24", placement="on_playback")
    base.update(kwargs)
    return OverlaySpec(**base)


def _request(**kwargs):
    base = dict(
        source_relative="generated/h3/s1/v0001/run/candidate.mp4",
        output_relative="export/s1/delivery.mp4",
        width=640, height=480,
        overlay=_overlay(), font_relative="assets/fonts/overlay.ttf",
    )
    base.update(kwargs)
    return DeliveryRequest(**base)


def test_overlay_is_drawn_after_scaling_so_text_is_native_resolution():
    """Drawing first and scaling after would upscale rendered glyphs, which is the soft
    resampled text this adapter exists to avoid."""
    graph = build_delivery_filter(_request(), font_path="assets/fonts/overlay.ttf")
    assert graph.index("scale=") < graph.index("drawtext=")


def test_aspect_is_padded_never_stretched():
    """352x288 is 11:9 while 384x288, 512x384 and 640x480 are 4:3, so a stretch would
    distort some shots and not others."""
    graph = build_delivery_filter(_request(), font_path="f.ttf")
    assert "force_original_aspect_ratio=decrease" in graph
    assert "pad=640:480" in graph


def test_clock_offset_is_applied_to_the_epoch():
    """CONTRACT D4b: H3 resamples time, so the delivery clock must be offset by where in
    the recording the clip was taken, or it is monotonic and wrong."""
    graph = build_delivery_filter(_request(clock_offset_seconds=12), font_path="f.ttf")
    assert f"gmtime\\:{EPOCH + 12}" in graph
    plain = build_delivery_filter(_request(), font_path="f.ttf")
    assert f"gmtime\\:{EPOCH}" in plain


def test_in_recording_overlay_is_refused():
    """An in_recording overlay is already burned into the source; compositing it again
    would double it."""
    with pytest.raises(ValueError, match="on_playback"):
        _request(overlay=_overlay(placement="in_recording"))


def test_overlay_requires_a_font():
    with pytest.raises(ValueError, match="font"):
        _request(font_relative=None)


def test_unknown_scale_filter_is_refused():
    with pytest.raises(ValueError, match="scale_filter"):
        _request(scale_filter="magic")


def test_neighbour_scaling_is_available():
    """Point upscaling reproduces the blocky enlargement of real low-resolution video and
    is a legitimate choice for this genre (CONTRACT D3)."""
    graph = build_delivery_filter(_request(scale_filter="neighbor"), font_path="f.ttf")
    assert "flags=neighbor" in graph


def test_delivery_without_overlay_draws_no_text():
    graph = build_delivery_filter(
        _request(overlay=None, font_relative=None), font_path=None
    )
    assert "drawtext" not in graph
    assert "scale=640:480" in graph


def test_fontsize_override_wins_over_the_overlay_default():
    """Overlay geometry is authored against the recording, so the caller sets type size
    at a different delivery resolution rather than this module guessing."""
    graph = build_delivery_filter(_request(fontsize=24), font_path="f.ttf")
    assert "fontsize=24" in graph


def test_command_is_argv_with_audio_mapped():
    request = _request()
    graph = build_delivery_filter(request, font_path="f.ttf")
    argv = build_delivery_command(Path("ffmpeg"), source=Path("in.mp4"),
                                  output=Path("out.mp4"), filter_graph=graph,
                                  request=request, has_audio=True)
    assert isinstance(argv, list) and all(isinstance(part, str) for part in argv)
    # FR-13 requires H.264 + AAC; unlike the degrade stage this is not video-only.
    assert "-c:a" in argv and "aac" in argv
    assert argv[argv.index("-c:v") + 1] == "libx264"
    assert "-map" in argv and "0:a:0" in argv


def test_command_disables_audio_when_the_source_has_none():
    request = _request()
    graph = build_delivery_filter(request, font_path="f.ttf")
    argv = build_delivery_command(Path("ffmpeg"), source=Path("in.mp4"),
                                  output=Path("out.mp4"), filter_graph=graph,
                                  request=request, has_audio=False)
    assert "-an" in argv
    assert "0:a:0" not in argv


def test_filter_values_are_escaped():
    """A camera_id carrying filter syntax must not break out of the drawtext option."""
    graph = build_delivery_filter(
        _request(overlay=_overlay(camera_id="CAM:03,X")), font_path="f.ttf"
    )
    # Export now reuses the PostProfile two-layer drawtext serializer so playback
    # escaping matches the in-recording overlay path exactly.
    assert "CAM\\\\:03\\,X" in graph
