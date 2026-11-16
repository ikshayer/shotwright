"""Text-to-speech adapters: Piper (CPU) and IndexTTS-2 (GPU)."""

from .core import (
    IndexTts2Request,
    PiperRequest,
    TtsRequestError,
    TtsResult,
    TtsSetupError,
    load_piper_catalog,
)
from .indextts2 import IndexTTS2Adapter
from .piper import PiperAdapter

__all__ = [
    "IndexTTS2Adapter", "IndexTts2Request", "PiperAdapter", "PiperRequest",
    "TtsRequestError", "TtsResult", "TtsSetupError", "load_piper_catalog",
]
