"""Canonical form for comparing captured FFmpeg build identities."""

from __future__ import annotations


def normalize_ffmpeg_identity(identity: str) -> str:
    """Normalize line endings and omit at most one terminal line ending."""
    return identity.replace("\r\n", "\n").replace("\r", "\n").removesuffix("\n")
