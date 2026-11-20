"""Reading a take and finding the pauses in it (PerformanceEdit contract sections 1 and 2).

Pure Python on the stdlib: no numpy, no FFmpeg. Everything here is deterministic and
works in whole samples; a duration in milliseconds is only ever derived from a sample
count, never the other way round.
"""

from __future__ import annotations

import io
import math
import struct
import sys
import wave
from array import array
from dataclasses import dataclass
from operator import mul

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.adapters.audiopost.runner import read_wav_facts

__all__ = [
    "FRAME_SECONDS", "MAX_RATE", "MIN_RATE", "Pause", "PauseDetection", "PauseScan",
    "PerformanceEditRequestError", "TakeAudio", "detect_pauses", "pcm16_wav_bytes",
    "read_take",
]

#: Detection frames are 10 ms: `round(FRAME_SECONDS * rate)` samples.
FRAME_SECONDS = 0.010
MIN_RATE, MAX_RATE = 8000, 48000
_WAVE_FORMAT_PCM, _WAVE_FORMAT_MULAW = 1, 7
_FULL_SCALE = 32768.0


class PerformanceEditRequestError(ValueError):
    """A request to inspect or edit a take is invalid (the API layer maps this to 422)."""


class PauseDetection(BaseModel):
    """Contract section 2's parameters; also the query of `inspect_take_pauses`."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    noise_db: float = Field(-38.0, ge=-80.0, le=-10.0,
                            description="A 10 ms frame is quiet when its RMS (dBFS) is below this.")
    min_pause_ms: float = Field(120.0, ge=30.0, le=3000.0,
                                description="The shortest quiet run that counts as a pause.")


@dataclass(frozen=True)
class TakeAudio:
    sample_rate: int
    samples: array  # signed 16-bit, mono

    @property
    def duration_ms(self) -> float:
        return len(self.samples) * 1000 / self.sample_rate


@dataclass(frozen=True)
class Pause:
    """`[start, end)` in samples."""

    index: int
    start: int
    end: int

    def natural_ms(self, sample_rate: int) -> float:
        return (self.end - self.start) * 1000 / sample_rate

    @property
    def midpoint(self) -> int:
        return (self.start + self.end) // 2


@dataclass(frozen=True)
class PauseScan:
    pauses: tuple[Pause, ...]
    #: First sample of the first non-quiet frame; `None` when every frame is quiet.
    speech_onset: int | None


def read_take(data: bytes) -> TakeAudio:
    """Decode a 16-bit PCM mono WAV of 8000..48000 Hz, refusing anything else with a reason."""
    try:
        facts = read_wav_facts(data)
    except (ValueError, struct.error, IndexError) as exc:
        # A truncated or malformed chunk surfaces as struct.error; it is a bad request (422),
        # never an unhandled server error.
        raise PerformanceEditRequestError(f"take is not a readable WAV file: {exc}") from exc
    if facts.format_tag == _WAVE_FORMAT_MULAW:
        raise PerformanceEditRequestError(
            "take is mu-law encoded; the performance edit needs 16-bit PCM. Apply the edit "
            "before the AudioPostProfile, whose encode stage produces the mu-law")
    if (facts.format_tag, facts.channels, facts.bits_per_sample) != (_WAVE_FORMAT_PCM, 1, 16):
        raise PerformanceEditRequestError(
            f"take must be 16-bit PCM mono; it has format tag {facts.format_tag}, "
            f"{facts.channels} channel(s) and {facts.bits_per_sample} bits per sample")
    if not MIN_RATE <= facts.sample_rate <= MAX_RATE:
        raise PerformanceEditRequestError(
            f"take sample rate {facts.sample_rate} Hz is outside {MIN_RATE}..{MAX_RATE} Hz")
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            frames = reader.readframes(reader.getnframes())
    except (wave.Error, EOFError, struct.error) as exc:
        raise PerformanceEditRequestError(f"take is not a readable WAV file: {exc}") from exc
    samples = array("h")
    samples.frombytes(frames[: len(frames) - len(frames) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if len(samples) < 1:
        raise PerformanceEditRequestError("take contains no samples")
    return TakeAudio(facts.sample_rate, samples)


def pcm16_wav_bytes(samples: array, sample_rate: int) -> bytes:
    """A canonical 16-bit PCM mono WAV: the same samples always give the same bytes."""
    payload = array("h", samples)
    if sys.byteorder == "big":
        payload.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(payload.tobytes())
    return buffer.getvalue()


def frame_length(sample_rate: int) -> int:
    return max(1, round(FRAME_SECONDS * sample_rate))


def _is_quiet(frame: array, noise_db: float) -> bool:
    energy = sum(map(mul, frame, frame))
    if energy == 0:
        return True  # An all-zero frame is -inf dBFS.
    rms = math.sqrt(energy / len(frame))
    return 20.0 * math.log10(rms / _FULL_SCALE) < noise_db


def detect_pauses(take: TakeAudio, detection: PauseDetection) -> PauseScan:
    """Maximal quiet runs of at least `min_pause_ms`, excluding leading and trailing silence."""
    samples, rate, total = take.samples, take.sample_rate, len(take.samples)
    size = frame_length(rate)
    minimum = round(detection.min_pause_ms * rate / 1000)
    pauses: list[Pause] = []
    onset: int | None = None
    run_start: int | None = None
    for start in range(0, total, size):
        if _is_quiet(samples[start:start + size], detection.noise_db):
            if run_start is None:
                run_start = start
            continue
        if onset is None:
            onset = start
        if run_start is not None:
            _close_run(pauses, run_start, start, total, minimum)
            run_start = None
    if run_start is not None:
        _close_run(pauses, run_start, total, total, minimum)
    return PauseScan(tuple(pauses), onset)


def _close_run(pauses: list[Pause], start: int, end: int, total: int, minimum: int) -> None:
    # A run touching either end of the take is leading or trailing silence, not a pause.
    if start > 0 and end < total and end - start >= minimum:
        pauses.append(Pause(len(pauses), start, end))
