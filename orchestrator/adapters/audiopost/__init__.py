"""AudioPostProfile: a versioned, seeded degradation chain applied to one voice take (FR-32)."""

from .runner import (
    AudioPostAdapter,
    AudioPostRequest,
    AudioPostRequestError,
    AudioPostResult,
    build_command,
    capture_ffmpeg_identity,
)

__all__ = [
    "AudioPostAdapter", "AudioPostRequest", "AudioPostRequestError", "AudioPostResult",
    "build_command", "capture_ffmpeg_identity",
]
