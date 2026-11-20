"""The edit itself: spec, plan and sample arithmetic (PerformanceEdit contract sections 3.2, 4, 6).

`plan_edit` turns a take and an edit into an ordered list of pieces whose lengths are exact
in samples; it needs no breath audio, only the breaths' lengths, so a request can be fully
validated before any job exists. `assemble` then fills the pieces in. Gain arithmetic is
`round_half_even(s * g)` clamped to 16 bits, and a piece at exactly 0 dB is copied with no
arithmetic at all, which is what makes the edit provably leave speech bit-identical.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from orchestrator.domain.seeds import INT32_MAX, derive_seed, jitter

from .detect import (
    Pause,
    PauseDetection,
    PauseScan,
    PerformanceEditRequestError,
    TakeAudio,
    detect_pauses,
)

__all__ = [
    "ALGORITHM_VERSION", "Breath", "EditPlan", "FalseStart", "Jitter", "PauseEdit", "Piece",
    "PerformanceEditSpec", "assemble", "breath_seed", "plan_edit",
]

ALGORITHM_VERSION = "1.0.0"
_FADE_SECONDS = 0.050
_INT16_MIN, _INT16_MAX = -32768, 32767


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Breath(_Strict):
    duration_ms: float = Field(ge=100.0, le=2000.0)
    gain_db: float = Field(ge=-40.0, le=0.0)


class PauseEdit(_Strict):
    index: StrictInt = Field(ge=0, description="A detected pause, as numbered by inspect_take_pauses.")
    target_ms: float = Field(ge=30.0, le=10000.0,
                             description="The pause's new length; never less than its natural length.")
    breath: Breath | None = None


class FalseStart(_Strict):
    copy_ms: float = Field(ge=50.0, le=2000.0)
    gain_db: float = Field(ge=-24.0, le=0.0)
    gap_after_ms: float = Field(ge=0.0, le=3000.0)


class Jitter(_Strict):
    gap_ms_range: tuple[float, float]
    gain_db_range: tuple[float, float]

    @field_validator("gap_ms_range", "gain_db_range")
    @classmethod
    def _ordered(cls, value: tuple[float, float]) -> tuple[float, float]:
        if value[0] > value[1]:
            raise ValueError("a range must have lo <= hi")
        return value

    @field_validator("gap_ms_range")
    @classmethod
    def _gap_bounds(cls, value: tuple[float, float]) -> tuple[float, float]:
        if value[0] < -500.0 or value[1] > 500.0:
            raise ValueError("gap_ms_range must lie within -500..500 ms")
        return value

    @field_validator("gain_db_range")
    @classmethod
    def _gain_bounds(cls, value: tuple[float, float]) -> tuple[float, float]:
        if value[0] < -6.0 or value[1] > 6.0:
            raise ValueError("gain_db_range must lie within -6..6 dB")
        return value


class PerformanceEditSpec(_Strict):
    """Everything that decides the edit besides the take. Serialised into the report."""

    seed: StrictInt = Field(ge=0, le=INT32_MAX)
    detection: PauseDetection = Field(default_factory=PauseDetection)
    pauses: list[PauseEdit] = Field(default_factory=list, max_length=32)
    segment_gain_db: list[float] | None = Field(default=None)
    false_start: FalseStart | None = None
    jitter: Jitter | None = None

    @field_validator("pauses")
    @classmethod
    def _ascending_unique(cls, value: list[PauseEdit]) -> list[PauseEdit]:
        indices = [item.index for item in value]
        if any(later <= earlier for earlier, later in zip(indices, indices[1:])):
            raise ValueError("pause indices must be unique and ascending")
        return value

    @field_validator("segment_gain_db")
    @classmethod
    def _gain_range(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and any(not -24.0 <= gain <= 6.0 for gain in value):
            raise ValueError("segment_gain_db entries must lie within -24..+6 dB")
        return value

    @model_validator(mode="after")
    def _gains_fit_segments(self) -> PerformanceEditSpec:
        if self.segment_gain_db is not None and len(self.segment_gain_db) > len(self.pauses) + 1:
            raise ValueError(
                f"segment_gain_db has {len(self.segment_gain_db)} entries but only "
                f"{len(self.pauses) + 1} segments exist")
        return self


@dataclass(frozen=True)
class Piece:
    """One run of the output. `source` and `false_start` read the take; the rest do not."""

    kind: Literal["source", "false_start", "breath", "silence"]
    length: int
    src_start: int = 0
    gain_db: float = 0.0
    #: Samples at the end of a false start that fade out.
    fade: int = 0
    #: The pause `index` a breath belongs to.
    breath_index: int | None = None

    @property
    def unchanged(self) -> bool:
        return self.gain_db == 0.0 and self.fade == 0


@dataclass(frozen=True)
class EditPlan:
    sample_rate: int
    source_length: int
    scan: PauseScan
    detection: PauseDetection
    pauses: tuple[Pause, ...]  # the edited pauses, in edit order
    cuts: tuple[int, ...]
    pieces: tuple[Piece, ...]
    breath_seeds: dict[int, int]
    jitter_values: dict[str, float] = field(default_factory=dict)

    @property
    def output_length(self) -> int:
        return sum(piece.length for piece in self.pieces)

    @property
    def reproduces_source(self) -> bool:
        """True when the output would be the source, byte for byte."""
        return (self.output_length == self.source_length
                and all(p.kind == "source" and p.unchanged for p in self.pieces))

    def segment_map(self) -> list[dict[str, Any]]:
        """Ordered output spans that tile the output exactly (contract section 6)."""
        spans: list[dict[str, Any]] = []
        position = 0
        for piece in self.pieces:
            span: dict[str, Any] = {"kind": piece.kind, "out_start": position, "length": piece.length}
            if piece.kind in {"source", "false_start"}:
                span.update(src_start=piece.src_start, gain_db=piece.gain_db, unchanged=piece.unchanged)
                if piece.kind == "false_start":
                    span["fade_samples"] = piece.fade
            if piece.kind == "breath":
                span["pause_index"] = piece.breath_index
            spans.append(span)
            position += piece.length
        return spans


def breath_seed(take_asset_id: str, seed: int, pause_index: int) -> int:
    return derive_seed(f"perfedit|{take_asset_id}", seed, f"breath{pause_index}", INT32_MAX)


def plan_edit(take: TakeAudio, take_asset_id: str, spec: PerformanceEditSpec) -> EditPlan:
    """Validate `spec` against `take` and lay out the output. Raises on anything invalid."""
    rate, total = take.sample_rate, len(take.samples)
    scan = detect_pauses(take, spec.detection)
    by_index = {pause.index: pause for pause in scan.pauses}
    edited: list[Pause] = []
    for item in spec.pauses:
        pause = by_index.get(item.index)
        if pause is None:
            raise PerformanceEditRequestError(
                f"pause index {item.index} was not detected; the take has {len(scan.pauses)} "
                f"pause(s) at noise_db={spec.detection.noise_db}, "
                f"min_pause_ms={spec.detection.min_pause_ms}")
        if item.target_ms < pause.natural_ms(rate):
            raise PerformanceEditRequestError(
                f"pause {item.index} is {pause.natural_ms(rate):.4f} ms long and target_ms is "
                f"{item.target_ms}; an edit can only widen a pause")
        edited.append(pause)
    if spec.false_start is not None and scan.speech_onset is None:
        raise PerformanceEditRequestError("the take has no speech, so there is nothing to copy for a false start")

    jitter_values: dict[str, float] = {}

    def draw(name: str, bounds: tuple[float, float] | None) -> float:
        if bounds is None:
            return 0.0
        value = jitter(f"perfedit|{take_asset_id}", spec.seed, name, bounds[0], bounds[1])
        jitter_values[name] = value
        return value

    gap_range = spec.jitter.gap_ms_range if spec.jitter else None
    gain_range = spec.jitter.gain_db_range if spec.jitter else None
    cuts = tuple(pause.midpoint for pause in edited)
    pieces: list[Piece] = []

    if spec.false_start is not None:
        pieces += _false_start_pieces(spec.false_start, scan.speech_onset or 0, total, rate,
                                      draw("fs|gap", gap_range))
    boundaries = (0, *cuts, total)
    gains = spec.segment_gain_db or []
    seeds: dict[int, int] = {}
    for i in range(len(boundaries) - 1):
        base = (gains[min(i, len(gains) - 1)] if gains else 0.0)
        gain_db = base + draw(f"seg{i}|gain", gain_range)
        pieces.append(Piece("source", boundaries[i + 1] - boundaries[i], boundaries[i], gain_db))
        if i >= len(edited):
            continue
        item, pause = spec.pauses[i], edited[i]
        extra_ms = item.target_ms - pause.natural_ms(rate) + draw(f"pause{i}|gap", gap_range)
        if item.breath is not None:
            pieces.append(Piece("breath", round(item.breath.duration_ms * rate / 1000),
                                breath_index=item.index))
            seeds[item.index] = breath_seed(take_asset_id, spec.seed, item.index)
            extra_ms -= item.breath.duration_ms
        if extra_ms > 0:
            pieces.append(Piece("silence", round(extra_ms * rate / 1000)))
    pieces = [piece for piece in pieces if piece.length > 0]
    plan = EditPlan(rate, total, scan, spec.detection, tuple(edited), cuts, tuple(pieces),
                    seeds, jitter_values)
    # A nominal change can still reproduce the source sample for sample (a gain on digital
    # silence, or one too small to move any rounded sample), so when nothing is inserted the
    # decision is made on the resulting samples, not on the parameters.
    if plan.reproduces_source or (
            plan.output_length == plan.source_length
            and all(piece.kind == "source" for piece in plan.pieces)
            and assemble(take, plan, {})[0] == take.samples):
        raise PerformanceEditRequestError(
            "this edit would reproduce the source take unchanged; widen a pause, set a gain "
            "or add a false start")
    return plan


def _false_start_pieces(spec: FalseStart, onset: int, total: int, rate: int,
                        gap_jitter_ms: float) -> list[Piece]:
    length = min(round(spec.copy_ms * rate / 1000), total - onset)
    fade = min(round(_FADE_SECONDS * rate), round(length / 2))
    gap_ms = max(0.0, spec.gap_after_ms + gap_jitter_ms)
    return [Piece("false_start", length, onset, spec.gain_db, fade),
            Piece("silence", round(gap_ms * rate / 1000))]


def _scale(samples: array, gain: float, fade: int = 0) -> tuple[array, int]:
    """`round_half_even(s * g)` clamped; the last `fade` samples also times `f_j`."""
    head = len(samples) - fade
    values = [round(s * gain) for s in samples[:head]]
    if fade:
        ramp = [1.0 - j / (fade - 1) if fade > 1 else 1.0 for j in range(fade)]
        values += [round(s * gain * f) for s, f in zip(samples[head:], ramp)]
    clamped = sum(1 for v in values if v > _INT16_MAX or v < _INT16_MIN)
    if clamped:
        values = [min(_INT16_MAX, max(_INT16_MIN, v)) for v in values]
    return array("h", values), clamped


def assemble(take: TakeAudio, plan: EditPlan, breaths: dict[int, array]) -> tuple[array, int]:
    """Build the output samples; returns them with the number of clamped samples."""
    out = array("h")
    clamped = 0
    for piece in plan.pieces:
        if piece.kind in {"source", "false_start"}:
            chunk = take.samples[piece.src_start:piece.src_start + piece.length]
            if not piece.unchanged:
                chunk, count = _scale(chunk, 10 ** (piece.gain_db / 20), piece.fade)
                clamped += count
            out.extend(chunk)
        elif piece.kind == "breath":
            if piece.breath_index is None:
                raise ValueError("a breath piece must name its pause index")
            breath = breaths[piece.breath_index]
            if len(breath) != piece.length:
                raise ValueError(f"breath {piece.breath_index} has {len(breath)} samples, "
                                 f"expected {piece.length}")
            out.extend(breath)
        else:
            out.extend(array("h", bytes(2 * piece.length)))
    return out, clamped
